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


def next_slot(profile: dict) -> datetime:
    """Next free posting time (UTC). Uses the profile's fixed daily `posting_times` in `posting_timezone`
    (one shared queue across all profiles, since they usually post to the same accounts)."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(profile.get("posting_timezone") or "America/New_York")
    times = sorted(profile.get("posting_times") or [])[: int(profile["max_posts_per_day"])]
    earliest = datetime.now(timezone.utc) + timedelta(minutes=20)
    taken = [datetime.fromisoformat(r["scheduled_at"]) for r in
             db.q("SELECT scheduled_at FROM candidates WHERE scheduled_at IS NOT NULL AND status NOT IN ('rejected','failed')")]
    taken = [t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in taken]
    if not times:  # fallback: fixed spacing
        spacing = timedelta(hours=float(profile["post_spacing_hours"]))
        return max([earliest] + [t + spacing for t in taken]).replace(microsecond=0)
    day = earliest.astimezone(tz).date()
    for d in range(90):
        date = day + timedelta(days=d)
        for t in times:
            hh, mm = (int(x) for x in t.split(":"))
            cand = datetime(date.year, date.month, date.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)
            if cand >= earliest and not any(abs((cand - x).total_seconds()) < 600 for x in taken):
                return cand
    return earliest


def publish(cand: dict, profile: dict) -> dict:
    tp = TryPost()
    pub = profile["publish"]
    if pub.get("mode") == "off" or not tp.configured:
        # manual posting: still give every clip its own slot in the schedule
        return {"status": "ready", "scheduled_at": next_slot(profile).isoformat()}
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
