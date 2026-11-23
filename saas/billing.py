"""Paying for clips.

local: plan quotas in this app (standalone site).
virlo: points in Virlo Studio. Before a run we ask Virlo how many clips the user can afford; each finished clip is
charged once (idempotent key). A clip whose charge fails for lack of points stays locked until it's paid.
"""
from __future__ import annotations

import logging
import time

import httpx

from . import settings, store

log = logging.getLogger("billing")
RETRY_INSUFFICIENT_SEC = 600
RETRY_ERROR_SEC = [60, 300, 1800]


def _virlo(path: str, body: dict) -> httpx.Response:
    if not settings.VIRLO_URL or not settings.SERVICE_TOKEN:
        raise RuntimeError("VIRLO_URL / CLIPS_SERVICE_TOKEN not set")
    return httpx.post(f"{settings.VIRLO_URL}{path}", json=body, timeout=20,
                      headers={"Authorization": f"Bearer {settings.SERVICE_TOKEN}"})


def allowance(user: dict) -> dict:
    """What this user's plan covers right now: {"clips": finished clips left, "max_clip_seconds": longest clip}."""
    lim = settings.limits(user)
    cap = lim.get("max_clip_seconds")
    if lim["billing"] == "local":
        return {"clips": max(0, lim["clips"] - store.usage(user["id"], "clip")), "max_clip_seconds": cap}
    try:
        r = _virlo("/api/internal/clips/allowance", {"userId": user["ext_id"]})
        r.raise_for_status()
        j = r.json()
        sent = j.get("maxClipSeconds")
        return {"clips": max(0, int(j.get("clips", 0))),
                "max_clip_seconds": int(sent) if isinstance(sent, (int, float)) and sent > 0 else cap}
    except Exception as e:
        log.warning("allowance check failed for user %s: %s", user["id"], str(e)[:200])
        return {"clips": 0, "max_clip_seconds": cap}  # fail closed: never clip for free because Virlo was unreachable


def clips_allowed(user: dict) -> int:
    """How many more finished clips this user can get right now."""
    return allowance(user)["clips"]


def record(user_id: int, clip_ids: list) -> None:
    user = store.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not user or settings.limits(user)["billing"] != "virlo" or not clip_ids:
        return
    now = time.time()
    with store._lock:
        store.conn().executemany(
            "INSERT OR IGNORE INTO charges (user_id, clip_id, next_try_at, created_at) VALUES (?,?,?,?)",
            [(user_id, int(c), now, now) for c in clip_ids])
        store.conn().commit()


def settle_due(user_id: int | None = None) -> int:
    """Try every pending/locked charge that's due. Returns how many got paid."""
    sql = ("SELECT c.*, u.ext_id FROM charges c JOIN users u ON u.id=c.user_id "
           "WHERE c.status != 'paid' AND c.next_try_at <= ?")
    args: tuple = (time.time(),)
    if user_id is not None:
        sql, args = sql + " AND c.user_id=?", (*args, user_id)
    paid = 0
    for ch in store.q(sql + " ORDER BY c.created_at LIMIT 200", args):
        key = f"clip:{ch['user_id']}:{ch['clip_id']}"
        try:
            r = _virlo("/api/internal/clips/charge", {"userId": ch["ext_id"], "clipId": ch["clip_id"],
                                                       "idempotencyKey": key})
            if r.status_code == 200:
                store.x("UPDATE charges SET status='paid', error=NULL, attempts=attempts+1 WHERE user_id=? AND clip_id=?",
                        (ch["user_id"], ch["clip_id"]))
                paid += 1
                continue
            if r.status_code == 402:
                store.x("UPDATE charges SET status='insufficient', attempts=attempts+1, next_try_at=?, error=? "
                        "WHERE user_id=? AND clip_id=?",
                        (time.time() + RETRY_INSUFFICIENT_SEC, "not enough points", ch["user_id"], ch["clip_id"]))
                continue
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            delay = RETRY_ERROR_SEC[min(ch["attempts"], len(RETRY_ERROR_SEC) - 1)]
            store.x("UPDATE charges SET status='error', attempts=attempts+1, next_try_at=?, error=? "
                    "WHERE user_id=? AND clip_id=?", (time.time() + delay, str(e)[:300], ch["user_id"], ch["clip_id"]))
    return paid


def unlocked(user_id: int, clip_id: int) -> bool:
    """Local users: always. Virlo users: once the clip's charge is paid (clips without a charge row are free)."""
    ch = store.one("SELECT status FROM charges WHERE user_id=? AND clip_id=?", (user_id, int(clip_id)))
    return ch is None or ch["status"] == "paid"
