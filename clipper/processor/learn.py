"""Feedback loop + duplicate detection.

- View tracking: when you mark a clip posted and paste its link (TikTok / YouTube Shorts / Instagram),
  the views are fetched with yt-dlp every few hours.
- performance_examples(): the best and worst performers (48 h+ old) go into the scoring prompt so the
  scorer learns what actually works on YOUR accounts.
- find_duplicate(): stops the same moment being posted twice (e.g. Kai's live clip and later his VOD
  upload, or two streams in the same lobby).
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

from .. import db
from ..media import ytdlp_json

log = logging.getLogger("learn")

MIN_AGE_H = 48
MIN_EXAMPLES = 6


def refresh_views(max_age_days: int = 21) -> int:
    """Fetch current view counts for posted clips that have a link. Returns how many updated."""
    rows = db.q("SELECT id, post_url, posted_at FROM candidates WHERE post_url IS NOT NULL AND post_url != ''"
                " AND (posted_at IS NULL OR posted_at > ?)", (time.time() - max_age_days * 86400,))
    n = 0
    for r in rows:
        total, errors = 0, []
        for url in [u for u in re.split(r"[\s,]+", r["post_url"]) if u.startswith("http")]:
            info = ytdlp_json(url, timeout=90)
            if info and info.get("view_count") is not None:
                total += int(info["view_count"])
            else:
                errors.append(url)
        if total or not errors:
            db.update_candidate(r["id"], views=total, views_checked_at=time.time())
            n += 1
        else:
            log.warning("couldn't read views for clip %s (%s)", r["id"], ", ".join(errors)[:200])
    if n:
        log.info("view counts refreshed for %d clips", n)
    return n


def performance_examples(profile_name: str, k: int = 4) -> str:
    rows = db.q("SELECT hook, gemini_json, viral_score, views FROM candidates WHERE profile=? AND views IS NOT NULL"
                " AND posted_at IS NOT NULL AND posted_at < ? ORDER BY views DESC",
                (profile_name, time.time() - MIN_AGE_H * 3600))
    if len(rows) < MIN_EXAMPLES:
        return ""

    def line(r: dict) -> str:
        g = json.loads(r["gemini_json"] or "{}")
        return f'- {r["views"]:,} views | hook "{r["hook"]}" | {(g.get("summary") or "")[:140]} (we scored it {r["viral_score"]})'

    med = rows[len(rows) // 2]["views"]
    k = min(k, len(rows) // 2)  # best and worst never overlap
    top, bottom = rows[:k], rows[-k:]
    return (f"Median views: {med:,}.\nBest performers:\n" + "\n".join(map(line, top)) +
            "\nWorst performers:\n" + "\n".join(map(line, bottom)) +
            "\nScore new clips higher when they resemble the best performers, lower when they resemble the worst.")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def find_duplicate(cid: int, transcript: str, summary: str, source_ref: str, offset: float,
                   window_days: int = 4) -> dict | None:
    """Another kept clip of (very likely) the same moment, or None."""
    rows = db.q("SELECT id, viral_score, status, source_ref, source_offset, score_json, gemini_json FROM candidates"
                " WHERE id != ? AND status IN ('ready','scheduled','drafted','posted') AND created_at > ?",
                (cid, time.time() - window_days * 86400))
    tw, sw = _words(transcript), _words(summary)
    for r in rows:
        if r["source_ref"] == source_ref and abs((r["source_offset"] or 0) - (offset or 0)) < 90 and offset:
            return r
        other = json.loads(r["score_json"] or "{}").get("transcript", "")
        ow = _words(other)
        if len(tw) >= 8 and len(ow) >= 8 and _jaccard(tw, ow) >= 0.55:
            return r
        osum = _words(json.loads(r["gemini_json"] or "{}").get("summary", ""))
        if len(sw) >= 8 and _jaccard(sw, osum) >= 0.6 and _jaccard(tw, ow) >= 0.3:
            return r
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
