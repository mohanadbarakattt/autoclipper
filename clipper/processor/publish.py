"""Self-hosted TryPost publisher (REST API: docs.trypost.it/api-reference)."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .. import db

log = logging.getLogger("publish")

CONTENT_TYPES = {
    "youtube": "youtube_short",
    "instagram": "instagram_reel",
    "instagram-facebook": "instagram_reel",
    "tiktok": "tiktok_video",
}


class TryPost:
    def __init__(self):
        self.base = os.getenv("TRYPOST_API_URL", "http://trypost").rstrip("/") + "/api"
        self.key = os.getenv("TRYPOST_API_KEY", "")

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.key}", "Accept": "application/json"}

    def accounts(self) -> list[dict]:
        out, page = [], 1
        with httpx.Client(timeout=30) as c:
            while True:
                r = c.get(f"{self.base}/social-accounts", params={"page": page}, headers=self._h())
                r.raise_for_status()
                j = r.json()
                out += j.get("data", [])
                meta = j.get("meta") or {}
                if not meta or meta.get("current_page", 1) >= meta.get("last_page", 1):
                    break
                page += 1
        return out

    def upload(self, path: Path) -> str:
        with open(path, "rb") as f, httpx.Client(timeout=600) as c:
            r = c.post(f"{self.base}/uploads", headers=self._h(), files={"media": (path.name, f, "video/mp4")})
        r.raise_for_status()
        return r.json()["upload_token"]

    def post_batch(self, body: dict) -> list[dict]:
        r = httpx.post(f"{self.base}/posts/batch", json=body, headers=self._h(), timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"TryPost {r.status_code}: {r.text[:800]}")
        return r.json().get("posts", [])


def _mins(hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.strip().split(":"))
    return hh * 60 + mm


def daily_times(profile: dict) -> list[str]:
    """The day's posting slots. Fixed `posting_times` are kept; if `max_posts_per_day` asks for more, extra
    slots are spread as far apart as possible inside `posting_window` (e.g. "11:00-23:30")."""
    n = max(1, int(profile.get("max_posts_per_day") or 3))
    fixed = sorted({t.strip() for t in (profile.get("posting_times") or []) if t.strip()}, key=_mins)
    if len(fixed) >= n:
        return fixed[:n]
    start, end = (profile.get("posting_window") or "11:00-23:30").split("-")
    s, e = _mins(start), _mins(end)
    if e <= s:
        e += 24 * 60  # window past midnight, e.g. 18:00-02:00
    if not fixed:  # no peak times given: spread evenly over the window
        step = (e - s) / max(n - 1, 1)
        return [f"{(m // 60) % 24:02d}:{m % 60:02d}" for m in (int(round((s + i * step) / 5) * 5) for i in range(n))]
    chosen = [m if m >= s else m + 24 * 60 for m in map(_mins, fixed)]
    grid = range(s, e + 1, 15)
    while len(chosen) < n:
        best = max(grid, key=lambda m: min((abs(m - c) for c in chosen), default=10 ** 6) - (m - s) / 10 ** 4)
        if best in chosen:
            break
        chosen.append(best)
    return [f"{(m // 60) % 24:02d}:{m % 60:02d}" for m in sorted(chosen)]


def next_slot(profile: dict, ignore_id: int | None = None) -> datetime:
    """Next free posting time (UTC), from `daily_times` in `posting_timezone`
    (one shared queue across all profiles, since they usually post to the same accounts)."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(profile.get("posting_timezone") or "America/New_York")
    times = daily_times(profile)
    earliest = datetime.now(timezone.utc) + timedelta(minutes=20)
    taken = [datetime.fromisoformat(r["scheduled_at"]) for r in
             db.q("SELECT scheduled_at FROM candidates WHERE scheduled_at IS NOT NULL AND status NOT IN ('rejected','failed')"
                  " AND id != ?", (ignore_id or -1,))]
    taken = [t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in taken]
    day = earliest.astimezone(tz).date()
    for d in range(90):
        date = day + timedelta(days=d)
        for t in times:
            hh, mm = (int(x) for x in t.split(":"))
            cand = datetime(date.year, date.month, date.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)
            if cand >= earliest and not any(abs((cand - x).total_seconds()) < 600 for x in taken):
                return cand
    return earliest


def claim_slot(cand: dict, profile: dict) -> tuple[datetime, int | None]:
    """Slot for a manually posted clip. If the queue is already full for `max_queue_hours`, a better clip takes
    the slot of the weakest clip still waiting in that window (returned as the second value, to be re-queued)."""
    slot = next_slot(profile, ignore_id=cand.get("id"))
    hours = float(profile.get("max_queue_hours") or 0)
    now = datetime.now(timezone.utc)
    if not hours or slot <= now + timedelta(hours=hours):
        return slot, None
    waiting = []
    for r in db.q("SELECT id, viral_score, scheduled_at FROM candidates WHERE status='ready' AND scheduled_at IS NOT NULL"
                  " AND id != ?", (cand.get("id") or -1,)):
        at = datetime.fromisoformat(r["scheduled_at"])
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        if now + timedelta(minutes=20) <= at <= now + timedelta(hours=hours):
            waiting.append((r["viral_score"] or 0, at.timestamp(), r["id"], at))  # weakest, then soonest
    if waiting:
        score, _, weak_id, at = min(waiting)
        if (cand.get("viral_score") or 0) >= score + 0.5:  # clearly better only, so the schedule doesn't churn
            return at, weak_id
    return slot, None


def publish(cand: dict, profile: dict) -> dict:
    tp = TryPost()
    pub = profile["publish"]
    if pub.get("mode") == "off" or not tp.configured:
        # manual posting: still give every clip its own slot in the schedule (best clips first)
        slot, bumped = claim_slot(cand, profile)
        return {"status": "ready", "scheduled_at": slot.isoformat(), "bumped": bumped}
    accts = [a for a in tp.accounts() if a.get("status") == "connected" and a.get("platform") in CONTENT_TYPES]
    if pub.get("account_ids"):
        accts = [a for a in accts if a["id"] in pub["account_ids"]]
    if not accts:
        return {"status": "ready", "note": "no connected YouTube/Instagram/TikTok accounts in TryPost"}

    tags = " ".join("#" + t.lstrip("#").replace(" ", "") for t in (cand.get("hashtags") or [])[: pub.get("max_hashtags", 5)])
    content = f"{cand['caption']}\n\n{tags}".strip()
    dests = []
    for a in accts:
        ct = CONTENT_TYPES[a["platform"]]
        meta: dict = {}
        if a["platform"] == "youtube":
            meta = {"title": (cand.get("title") or cand.get("hook") or "Stream highlight")[:100].replace("<", "").replace(">", ""),
                    "privacy_status": pub.get("youtube_privacy", "public")}
        elif a["platform"] == "tiktok":
            meta = {"privacy_level": pub.get("tiktok_privacy", "SELF_ONLY")}
        elif a["platform"].startswith("instagram"):
            meta = {"share_to_feed": True}
        dests.append({"social_account_id": a["id"], "content_type": ct, "meta": meta})

    token = tp.upload(Path(cand["out_path"]))
    body = {"status": "draft" if pub.get("mode") == "draft" else "scheduled", "content": content,
            "media": [{"upload_token": token}], "destinations": dests}
    slot = None
    if body["status"] == "scheduled":
        slot = next_slot(profile)
        body["scheduled_at"] = slot.isoformat().replace("+00:00", "Z")
    posts = tp.post_batch(body)
    return {"status": "scheduled" if slot else "drafted", "post_ids": [p.get("id") for p in posts],
            "scheduled_at": slot.isoformat() if slot else None}
