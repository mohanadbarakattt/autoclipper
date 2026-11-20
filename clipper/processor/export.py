"""Copy finished clips + a caption file into a folder (e.g. your Google Drive) organised by posting day.

EXPORT_DIR=G:\\My Drive\\AutoClipper  ->  G:\\My Drive\\AutoClipper\\2026-11-21\\1700 ET - Kai jumps off the tower.mp4
                                                                              1700 ET - Kai jumps off the tower.txt
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import db

log = logging.getLogger("export")
SHOW_TZ = os.getenv("SCHEDULE_TIMEZONE", "America/New_York")
LOCAL_TZ = os.getenv("LOCAL_TIMEZONE", "Africa/Cairo")


def _safe(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    return name[:70] or "clip"


def export_clip(cid: int) -> Path | None:
    root = os.getenv("EXPORT_DIR", "").strip()
    if not root:
        return None
    c = db.q("SELECT * FROM candidates WHERE id=?", (cid,))[0]
    if not c.get("out_path") or not Path(c["out_path"]).exists():
        return None
    score = json.loads(c["score_json"] or "{}")
    when = datetime.fromisoformat(c["scheduled_at"]) if c.get("scheduled_at") else datetime.now(timezone.utc)
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    show, local = when.astimezone(ZoneInfo(SHOW_TZ)), when.astimezone(ZoneInfo(LOCAL_TZ))
    folder = Path(root) / show.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{show:%H%M} {show.tzname()} - {_safe(c.get('title') or c.get('hook') or f'clip {cid}')}"
    dest = folder / f"{base}.mp4"
    if dest.exists() and dest.stat().st_size == Path(c["out_path"]).stat().st_size:
        return dest
    shutil.copy2(c["out_path"], dest)
    tags = " ".join("#" + t.lstrip("#").replace(" ", "") for t in score.get("hashtags", [])[:5])
    streamer = (c.get("streamer_id") or "").split(":", 1)[-1]
    (folder / f"{base}.txt").write_text(
        f"POST AT:  {show:%a %d %b %H:%M} {show.tzname()}  =  {local:%a %d %b %H:%M} Cairo\n"
        f"SCORE:    {c.get('viral_score')}   (streamer: {streamer})\n\n"
        f"YOUTUBE TITLE:\n{c.get('title')}\n\n"
        f"CAPTION (TikTok / Reels / Shorts description):\n{c.get('caption')}\n\n{tags}\n",
        encoding="utf-8")
    log.info("exported clip %s -> %s", cid, folder / base)
    return dest


def backfill() -> None:
    """Export upcoming clips that missed it (e.g. Google Drive wasn't signed in yet)."""
    root = os.getenv("EXPORT_DIR", "").strip()
    if not root or not Path(root).anchor or not Path(Path(root).anchor).exists():
        return
    for r in db.q("SELECT id FROM candidates WHERE status IN ('ready','scheduled','drafted') AND out_path IS NOT NULL"):
        try:
            export_clip(r["id"])
        except Exception as e:
            log.warning("backfill export %s failed: %s", r["id"], e)
