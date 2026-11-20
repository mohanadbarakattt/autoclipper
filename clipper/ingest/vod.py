"""Finished-stream (VOD) poller. Finds peak windows WITHOUT downloading the full VOD:

- YouTube: built-in "Most Replayed" heatmap (falls back to audio energy once the video is 2 days old)
- Twitch: clusters of viewer-made clips (needs free Twitch app keys), else audio energy
Then only the peak sections are downloaded with yt-dlp --download-sections.
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import CANDIDATES_DIR, WORK_DIR, load_profile, load_streamers
from ..media import loud_windows, loudness_per_second, ytdlp_audio, ytdlp_json, ytdlp_section
from . import platforms

log = logging.getLogger("vod")

MAX_VOD_HOURS = 8
LOOKBACK = 3  # newest N VODs per channel


def list_recent(s: dict) -> list[dict]:
    if s["platform"] == "youtube":
        ch = s["channel"]
        base = f"https://www.youtube.com/channel/{ch}" if ch.startswith("UC") and len(ch) == 24 else f"https://www.youtube.com/@{ch.lstrip('@')}"
        url = f"{base}/streams"
    elif s["platform"] == "twitch":
        url = f"https://www.twitch.tv/{s['channel']}/videos?filter=archives&sort=time"
    else:
        return []  # Kick VOD listing has no public API; Kick is live-only
    j = ytdlp_json(url, ["--flat-playlist", "--playlist-end", str(LOOKBACK)], timeout=120) or {}
    return [e for e in j.get("entries") or [] if e.get("id")]


def _merge(windows: list[tuple[float, float, float]], dur: float, min_len: float, max_len: float) -> list[tuple[float, float, float]]:
    out: list[list[float]] = []
    for s, e, v in sorted(windows):
        if out and s <= out[-1][1] + 5:
            out[-1][1] = max(out[-1][1], e)
            out[-1][2] = max(out[-1][2], v)
        else:
            out.append([s, e, v])
    res = []
    for s, e, v in out:
        mid = (s + e) / 2
        length = min(max(e - s + 30, min_len + 20), max_len + 30)  # pad so Gemini can find clean cut points
        s2 = max(0.0, mid - length / 2)
        res.append((s2, min(dur or s2 + length, s2 + length), v))
    return res


def heatmap_windows(info: dict, profile: dict) -> list[tuple[float, float, float]]:
    hm = info.get("heatmap") or []
    thr = float(profile.get("heatmap_threshold", 0.85))
    peaks = [(h["start_time"], h["end_time"], h["value"]) for h in hm if h.get("value", 0) >= thr]
    if not peaks and hm:  # nothing above threshold: take the 3 hottest buckets
        peaks = [(h["start_time"], h["end_time"], h["value"]) for h in sorted(hm, key=lambda h: -h["value"])[:3]]
    d = profile["clip_duration"]
    return _merge(peaks, info.get("duration") or 0, d["min"], d["max"])


def twitch_clip_windows(s: dict, info: dict, profile: dict) -> list[tuple[float, float, float]]:
    ts = info.get("timestamp")
    if not ts:
        return []
    started = datetime.fromtimestamp(ts, timezone.utc)
    ended = started + timedelta(seconds=info.get("duration") or 0)
    clips = platforms.twitch_clips_for_vod(s["channel"], started, ended, str(info["id"]).lstrip("v"))
    wins = [(float(c["vod_offset"]), float(c["vod_offset"]) + float(c.get("duration", 30)), float(c.get("view_count", 1)))
            for c in clips]
    d = profile["clip_duration"]
    return _merge(wins, info.get("duration") or 0, d["min"], d["max"])


def audio_windows(url: str, key: str, profile: dict) -> list[tuple[float, float, float]]:
    tmp = WORK_DIR / f"aud_{key.replace(':', '_')}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        audio = ytdlp_audio(url, tmp / "a")
        if not audio:
            return []
        arr = loudness_per_second(audio)
        d = profile["clip_duration"]
        return [(float(s), float(e), v) for s, e, v in loud_windows(arr, window=d["max"] + 30, top_n=profile["max_vod_windows"])]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def process_vod(s: dict, entry: dict) -> None:
    key = f"{s['platform']}:{entry['id']}"
    if db.vod_seen(key):
        return
    url = entry.get("url") or entry.get("webpage_url")
    if s["platform"] == "twitch" and url and not url.startswith("http"):
        url = f"https://www.twitch.tv/videos/{str(entry['id']).lstrip('v')}"
    if s["platform"] == "youtube" and (not url or not url.startswith("http")):
        url = f"https://www.youtube.com/watch?v={entry['id']}"
    info = ytdlp_json(url, timeout=120)
    if not info:
        return
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        return  # still live / still processing: the live worker owns it, check later
    if (info.get("duration") or 0) > MAX_VOD_HOURS * 3600:
        log.info("skip %s: longer than %sh", key, MAX_VOD_HOURS)
        db.mark_vod_seen(key, s["id"])
        return
    profile = load_profile(s["profile"])
    if not platforms.matches_filter({"title": info.get("title", ""), "category": ", ".join(info.get("categories") or [])}, profile):
        db.mark_vod_seen(key, s["id"])
        return

    wins, method = [], ""
    if s["platform"] == "youtube":
        wins, method = heatmap_windows(info, profile), "replay heatmap"
        age_h = (time.time() - (info.get("timestamp") or info.get("release_timestamp") or time.time())) / 3600
        if not wins and age_h < 48:
            log.info("%s has no heatmap yet - retry later", key)
            return
    elif s["platform"] == "twitch" and platforms.TWITCH.configured:
        wins, method = twitch_clip_windows(s, info, profile), "viewer clip cluster"
    if not wins:
        wins, method = audio_windows(url, key, profile), "audio energy"

    wins = sorted(wins, key=lambda w: -w[2])[: profile["max_vod_windows"]]
    log.info("%s: %d peak windows via %s", key, len(wins), method)
    for i, (st, en, val) in enumerate(wins):
        out = CANDIDATES_DIR / f"{key.replace(':', '_')}_{int(st)}.mp4"
        try:
            ytdlp_section(url, st, en, out)
            db.add_candidate(s, "vod", url, str(out), f"{method} ({val:.2f})", source_offset=st)
        except Exception as e:
            log.warning("section download failed %s @%s: %s", key, st, e)
    db.mark_vod_seen(key, s["id"])


def poll_all() -> None:
    for s in load_streamers():
        if s.get("paused") or not s.get("vods", True):
            continue
        try:
            for entry in list_recent(s):
                process_vod(s, entry)
        except Exception:
            log.exception("vod poll failed for %s", s["id"])
