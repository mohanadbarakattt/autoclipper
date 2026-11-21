"""Paths, env loading, streamer list and niche profiles."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(os.getenv("CLIPPER_ROOT", Path(__file__).resolve().parent.parent))
if load_dotenv:
    load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
PROFILES_DIR = CONFIG_DIR / "profiles"
DATA_DIR = ROOT / "data"
STATE_DIR = DATA_DIR / "state"
RING_DIR = DATA_DIR / "ring_buffers"
CANDIDATES_DIR = DATA_DIR / "candidates"
READY_DIR = DATA_DIR / "ready_clips"
WORK_DIR = DATA_DIR / "work"
FONTS_DIR = ROOT / "assets" / "fonts"
DB_PATH = STATE_DIR / "clipper.db"
STREAMERS_FILE = CONFIG_DIR / "streamers.json"

for _d in (STATE_DIR, RING_DIR, CANDIDATES_DIR, READY_DIR, WORK_DIR, PROFILES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8090"))
VOD_POLL_MINUTES = int(os.getenv("VOD_POLL_MINUTES", "30"))
KEEP_PUBLISHED_DAYS = int(os.getenv("KEEP_PUBLISHED_DAYS", "7"))

DEFAULT_PROFILE: dict = {
    "profile_name": "default",
    "niche_description": "Livestream highlights",
    "game_or_topic_filter": "",
    "layout_mode": "auto",  # auto | stacked_facecam | speaker_crop | blur_fit
    "clip_duration": {"min": 20, "max": 59},
    "min_viral_score": 7.5,
    "max_posts_per_day": 6,
    "post_spacing_hours": 3,
    # fixed daily posting slots (peak US short-form times by default)
    "posting_times": ["12:00", "17:00", "21:00"],
    "posting_timezone": "America/New_York",
    "cooldown_sec": 180,
    "window_before_sec": 60,
    "window_after_sec": 30,
    "spike_multiplier": 3.0,
    "min_window_score": 40,
    "emote_weights": {},
    "topic_keywords": [],
    "heatmap_threshold": 0.85,
    "max_vod_windows": 6,
    "whisper_model": "large-v3-turbo",  # best accuracy/speed on CPU; "small" for slow PCs
    "language": None,
    "caption_style": {
        "font": "Anton",
        "font_size": 86,
        "primary_color": "&H00FFFFFF",
        "highlight_color": "&H0000FFFF",
        "outline_color": "&H00000000",
        "words_per_line": 3,
        "margin_v": 520,
    },
    "hook_style": {"font": "Montserrat", "font_size": 64, "seconds": 3.5, "margin_v": 230},
    "publish": {
        "mode": "scheduled",  # scheduled | draft | off
        "account_ids": [],  # empty = every connected TryPost account
        "tiktok_privacy": "SELF_ONLY",  # PUBLIC_TO_EVERYONE once your TikTok app passes audit
        "youtube_privacy": "public",
        "max_hashtags": 5,
    },
}

_lock = threading.Lock()


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def list_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def load_profile(name: str) -> dict:
    path = PROFILES_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    prof = _deep_merge(DEFAULT_PROFILE, data)
    prof.setdefault("profile_name", name)
    prof["_file"] = name
    return prof


def save_profile(name: str, data: dict) -> None:
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    (PROFILES_DIR / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_streamers() -> list[dict]:
    with _lock:
        src = STREAMERS_FILE if STREAMERS_FILE.exists() else CONFIG_DIR / "streamers.example.json"
        if not src.exists():
            return []
        raw = json.loads(src.read_text(encoding="utf-8"))
    out = []
    for s in raw.get("streamers", []):
        s = {"live": True, "vods": True, "paused": False, "profile": "default", **s}
        s["platform"] = s["platform"].lower()
        s["channel"] = s["channel"].strip().lstrip("@").lower() if s["platform"] != "youtube" else s["channel"].strip()
        s["id"] = f"{s['platform']}:{s['channel'].lstrip('@').lower()}"
        out.append(s)
    return out


def save_streamers(streamers: list[dict]) -> None:
    clean = [{k: v for k, v in s.items() if k != "id"} for s in streamers]
    with _lock:
        STREAMERS_FILE.write_text(json.dumps({"streamers": clean}, indent=2), encoding="utf-8")


def get_streamer(sid: str) -> dict | None:
    return next((s for s in load_streamers() if s["id"] == sid), None)
