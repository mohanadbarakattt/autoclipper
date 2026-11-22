"""Auto-discover the biggest live streams for a profile's game/topic, every hour.

Profile config (e.g. config/profiles/gta6.json):
    "auto_discover": {"enabled": true, "twitch_game": "Grand Theft Auto VI", "kick_category": "Grand Theft Auto VI",
                      "youtube_query": "GTA 6", "top_n": 3, "min_viewers": 3000}

Only official/public sources: Twitch Helix and Kick's public API (free app keys in .env; skipped without them)
and YouTube's public live search via yt-dlp. Found channels are added to streamers.json with "auto": true and
removed again when they drop out of the top list (never while recording). Your own entries are never touched.
"""
from __future__ import annotations

import logging
import os

import httpx

from .. import db
from ..config import LIVE_RECORDING, list_profiles, load_profile, load_streamers, save_streamers
from ..media import ytdlp_json
from . import platforms

log = logging.getLogger("discover")
MAX_AUTO = int(os.getenv("MAX_AUTO_STREAMERS", "6"))


def twitch_top(game: str, n: int, min_viewers: int) -> list[tuple[str, int]]:
    if not platforms.TWITCH.configured:
        return []
    g = platforms.helix("games", {"name": game}).get("data", [])
    if not g:
        return []
    data = platforms.helix("streams", {"game_id": g[0]["id"], "first": 20, "type": "live"}).get("data", [])
    return [(s["user_login"].lower(), int(s["viewer_count"])) for s in data if s["viewer_count"] >= min_viewers][:n]


def kick_top(category: str, n: int, min_viewers: int) -> list[tuple[str, int]]:
    if not platforms.KICK.configured:
        return []
    h = {"Authorization": f"Bearer {platforms.KICK.get()}"}
    cats = httpx.get("https://api.kick.com/public/v1/categories", params={"q": category}, headers=h, timeout=20)
    cats.raise_for_status()
    match = [c for c in cats.json().get("data", []) if c.get("name", "").lower() == category.lower()] or cats.json().get("data", [])
    if not match:
        return []
    r = httpx.get("https://api.kick.com/public/v1/livestreams", headers=h, timeout=20,
                  params={"category_id": match[0]["id"], "sort": "viewer_count", "limit": 20})
    r.raise_for_status()
    out = []
    for s in r.json().get("data", []):
        slug = s.get("slug") or (s.get("channel") or {}).get("slug")
        viewers = int(s.get("viewer_count") or 0)
        if slug and viewers >= min_viewers:
            out.append((slug.lower(), viewers))
    return out[:n]


def youtube_top(query: str, n: int) -> list[tuple[str, int]]:
    url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}&sp=EgJAAQ%253D%253D"  # live only
    j = ytdlp_json(url, ["--flat-playlist", "--playlist-end", str(n * 3)], timeout=120) or {}
    out, seen = [], set()
    for e in j.get("entries") or []:
        ch = (e.get("channel_id") or "").strip()
        if not ch or ch in seen:
            continue
        seen.add(ch)
        out.append((ch, int(e.get("concurrent_view_count") or e.get("view_count") or 0)))
    return out[:n]


def run_once() -> None:
    found: dict[str, dict] = {}
    for name in list_profiles():
        p = load_profile(name)
        cfg = p.get("auto_discover") or {}
        if not cfg.get("enabled"):
            continue
        n, mv = int(cfg.get("top_n", 3)), int(cfg.get("min_viewers", 3000))
        for platform, fn, arg in (("twitch", twitch_top, cfg.get("twitch_game")),
                                  ("kick", kick_top, cfg.get("kick_category")),
                                  ("youtube", None, cfg.get("youtube_query"))):
            if not arg:
                continue
            try:
                if platform == "youtube":
                    res = [r for r in youtube_top(arg, n * 2) if r[1] >= mv][:n]
                else:
                    res = fn(arg, n, mv)
            except Exception as e:
                log.warning("discover %s/%s failed: %s", platform, arg, e)
                continue
            for ch, viewers in res:
                found.setdefault(f"{platform}:{ch.lower()}", {"platform": platform, "channel": ch, "profile": name,
                                                              "viewers": viewers})
    cur = load_streamers()
    manual = {s["id"] for s in cur if not s.get("auto")}
    recording = {r["streamer_id"] for r in db.q("SELECT streamer_id FROM stream_status WHERE recording=1")}
    ranked = sorted((v for k, v in found.items() if k not in manual), key=lambda v: -v["viewers"])[:MAX_AUTO]
    keep_ids = {f"{v['platform']}:{v['channel'].lower()}" for v in ranked}
    new_list, added, removed = [], [], []
    for s in cur:
        if s.get("auto") and s["id"] not in keep_ids and s["id"] not in recording:
            removed.append(s["id"])
            continue
        new_list.append(s)
    have = {s["id"] for s in new_list}
    for v in ranked:
        sid = f"{v['platform']}:{v['channel'].lower()}"
        if sid not in have:
            new_list.append({"platform": v["platform"], "channel": v["channel"], "profile": v["profile"],
                             "live": True, "vods": not LIVE_RECORDING, "paused": False, "auto": True})
            added.append(f"{sid} ({v['viewers']:,})")
    if added or removed:
        save_streamers(new_list)
    log.info("auto-discover: %d found, added %s, removed %s", len(found), added or "-", removed or "-")
