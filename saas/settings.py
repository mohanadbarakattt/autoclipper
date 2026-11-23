"""Paths, env and plan limits for the hosted app. Secrets come from saas.env (gitignored) or the environment."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

REPO = Path(__file__).resolve().parent.parent
if load_dotenv:
    load_dotenv(REPO / "saas.env")

DATA = Path(os.getenv("SAAS_DATA_DIR") or REPO / "saas_data")
WORKSPACES = DATA / "workspaces"
DB_PATH = DATA / "saas.db"
ENGINE_PROFILES = REPO / "config" / "profiles"
FONTS_DIR = Path(os.getenv("CLIPPER_FONTS_DIR") or REPO / "assets" / "fonts")
for _d in (DATA, WORKSPACES):
    _d.mkdir(parents=True, exist_ok=True)

PORT = int(os.getenv("SAAS_PORT", "8100"))
COOKIE_SECURE = os.getenv("SAAS_COOKIE_SECURE", "0") == "1"  # set 1 behind HTTPS
SESSION_DAYS = int(os.getenv("SAAS_SESSION_DAYS", "30"))
SCAN_MINUTES = int(os.getenv("SAAS_SCAN_MINUTES", "30"))
MAX_PARALLEL_JOBS = int(os.getenv("SAAS_MAX_PARALLEL_JOBS", str(max(1, (os.cpu_count() or 4) // 4))))
JOB_TIMEOUT_SEC = int(os.getenv("SAAS_JOB_TIMEOUT_SEC", str(3 * 3600)))
MAX_ATTEMPTS = 3

# Styles users can pick (engine profiles that ship with the repo). Label shown in the UI.
STYLES = {
    "gaming": "Gaming",
    "gta6": "GTA 6",
    "podcast": "Podcast / Talk",
    "arabic": "Arabic",
    "default": "General",
}

# clips = finished clips per month; moments = AI checks per month (hidden guard: dead channels can't burn AI spend)
PLANS: dict[str, dict] = {
    "free":    {"label": "Free",    "price": 0,  "clips": 10,  "channels": 1,  "live": 0, "scorer": "gemini",
                "history_days": 7,  "campaign_rules": False},
    "starter": {"label": "Starter", "price": 9,  "clips": 40,  "channels": 2,  "live": 0, "scorer": "auto",
                "history_days": 30, "campaign_rules": False},
    "creator": {"label": "Creator", "price": 19, "clips": 120, "channels": 5,  "live": 1, "scorer": "auto",
                "history_days": 60, "campaign_rules": True},
    "pro":     {"label": "Pro",     "price": 39, "clips": 300, "channels": 15, "live": 3, "scorer": "auto",
                "history_days": 90, "campaign_rules": True},
}
MOMENTS_PER_CLIP = 15

# ---------- Clips inside Virlo Studio (docs/VIRLO_INTEGRATION.md) ----------
SAAS_MODE = os.getenv("SAAS_MODE", "app")          # app = standalone site | service = API for Virlo Studio
BILLING = os.getenv("BILLING", "local")            # local = plan quotas here | virlo = points in Virlo Studio
SERVICE_TOKEN = os.getenv("CLIPS_SERVICE_TOKEN", "")
MEDIA_SECRET = os.getenv("SAAS_MEDIA_SECRET", "") or SERVICE_TOKEN
MEDIA_TTL_SEC = int(os.getenv("SAAS_MEDIA_TTL_SEC", str(6 * 3600)))
PUBLIC_URL = os.getenv("SAAS_PUBLIC_URL", "").rstrip("/")   # e.g. https://clips-api.virlo.studio
VIRLO_URL = os.getenv("VIRLO_URL", "").rstrip("/")           # e.g. https://virlo.studio
# Virlo tier -> limits here (clips themselves are paid in Virlo points; `moments` = monthly AI-check cap)
VIRLO_TIERS: dict[str, dict] = {
    "free":     {"label": "Free",     "channels": 1,  "scorer": "gemini", "history_days": 7,  "campaign_rules": False, "moments": 300, "max_clip_seconds": 30},
    "creator":  {"label": "Creator",  "channels": 3,  "scorer": "auto",   "history_days": 30, "campaign_rules": True,  "moments": 2000, "max_clip_seconds": 60},
    "pro":      {"label": "Pro",      "channels": 5,  "scorer": "auto",   "history_days": 60, "campaign_rules": True,  "moments": 6000, "max_clip_seconds": 90},
    "studio":   {"label": "Studio",   "channels": 15, "scorer": "auto",   "history_days": 90, "campaign_rules": True,  "moments": 15000, "max_clip_seconds": 180},
    "business": {"label": "Business", "channels": 15, "scorer": "auto",   "history_days": 90, "campaign_rules": True,  "moments": 15000, "max_clip_seconds": 180},
}


def plan(name: str | None) -> dict:
    p = PLANS.get(name or "free", PLANS["free"])
    return {**p, "name": name if name in PLANS else "free", "moments": p["clips"] * MOMENTS_PER_CLIP}


def limits(user: dict) -> dict:
    """What a user may do: a Virlo tier for Virlo-linked users, else a local plan."""
    if user.get("ext_id") and user.get("tier"):
        t = VIRLO_TIERS.get(user["tier"], VIRLO_TIERS["free"])
        return {**t, "name": user["tier"], "clips": None, "billing": "virlo"}
    return {**plan(user.get("plan")), "billing": "local"}
