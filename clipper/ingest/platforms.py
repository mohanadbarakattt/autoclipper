"""Platform lookups: is the channel live, what is it playing, where do its VODs live.

Every API here is free. Twitch and Kick developer apps are optional: without them we fall back
to streamlink/yt-dlp checks (no category filter, no viewer-clip signal).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..media import streamlink_live, ytdlp_json

log = logging.getLogger("platforms")


def stream_url(s: dict) -> str:
    ch = s["channel"]
    if s["platform"] == "twitch":
        return f"https://www.twitch.tv/{ch}"
    if s["platform"] == "kick":
        return f"https://kick.com/{ch}"
    if ch.startswith("UC") and len(ch) == 24:
        return f"https://www.youtube.com/channel/{ch}/live"
    return f"https://www.youtube.com/@{ch.lstrip('@')}/live"


class _AppToken:
    def __init__(self, url: str, cid: str | None, secret: str | None):
        self.url, self.cid, self.secret = url, cid, secret
        self.token, self.exp = None, 0.0

    @property
    def configured(self) -> bool:
        return bool(self.cid and self.secret)

    def get(self) -> str:
        if not self.token or time.time() > self.exp - 300:
            r = httpx.post(self.url, data={"client_id": self.cid, "client_secret": self.secret,
                                           "grant_type": "client_credentials"}, timeout=20)
            r.raise_for_status()
            j = r.json()
            self.token, self.exp = j["access_token"], time.time() + float(j.get("expires_in", 3600))
        return self.token


TWITCH = _AppToken("https://id.twitch.tv/oauth2/token", os.getenv("TWITCH_CLIENT_ID"), os.getenv("TWITCH_CLIENT_SECRET"))
KICK = _AppToken("https://id.kick.com/oauth/token", os.getenv("KICK_CLIENT_ID"), os.getenv("KICK_CLIENT_SECRET"))
_twitch_ids: dict[str, str] = {}


def helix(path: str, params: dict) -> dict:
    r = httpx.get(f"https://api.twitch.tv/helix/{path}", params=params, timeout=20,
                  headers={"Client-Id": TWITCH.cid, "Authorization": f"Bearer {TWITCH.get()}"})
    r.raise_for_status()
    return r.json()


def twitch_user_id(login: str) -> str | None:
    if login not in _twitch_ids:
        data = helix("users", {"login": login}).get("data", [])
        if not data:
            return None
        _twitch_ids[login] = data[0]["id"]
    return _twitch_ids[login]


def twitch_recent_clips(login: str, minutes: int = 5) -> list[dict]:
    uid = twitch_user_id(login)
    if not uid:
        return []
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return helix("clips", {"broadcaster_id": uid, "started_at": since, "first": 50}).get("data", [])


def twitch_clips_for_vod(login: str, started: datetime, ended: datetime, video_id: str) -> list[dict]:
    uid = twitch_user_id(login)
    if not uid:
        return []
    clips, cursor = [], None
    for _ in range(5):
        params = {"broadcaster_id": uid, "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "ended_at": ended.strftime("%Y-%m-%dT%H:%M:%SZ"), "first": 100}
        if cursor:
            params["after"] = cursor
        j = helix("clips", params)
        clips += [c for c in j.get("data", []) if c.get("video_id") == video_id and c.get("vod_offset") is not None]
        cursor = j.get("pagination", {}).get("cursor")
        if not cursor:
            break
    return clips


def live_info(s: dict) -> dict:
    """{'live': bool, 'title': str, 'category': str, 'url': str}"""
    url = stream_url(s)
    info = {"live": False, "title": "", "category": "", "url": url}
    try:
        if s["platform"] == "twitch" and TWITCH.configured:
            d = helix("streams", {"user_login": s["channel"]}).get("data", [])
            if d:
                info.update(live=True, title=d[0].get("title", ""), category=d[0].get("game_name", ""))
            return info
        if s["platform"] == "kick" and KICK.configured:
            r = httpx.get("https://api.kick.com/public/v1/channels", params={"slug": s["channel"]}, timeout=20,
                          headers={"Authorization": f"Bearer {KICK.get()}"})
            r.raise_for_status()
            d = (r.json().get("data") or [{}])[0]
            st = d.get("stream") or {}
            info.update(live=bool(st.get("is_live")), title=d.get("stream_title", ""),
                        category=(d.get("category") or {}).get("name", ""))
            return info
        if s["platform"] == "youtube":
            j = ytdlp_json(url, ["--playlist-items", "1"], timeout=90)
            if j and j.get("is_live"):
                info.update(live=True, title=j.get("title", ""), category=", ".join(j.get("categories") or []),
                            url=j.get("webpage_url", url))
            return info
        info["live"] = streamlink_live(url)
    except Exception as e:  # network hiccups must never kill a worker
        log.warning("live check failed for %s: %s", s["id"], e)
    return info


def matches_filter(info: dict, profile: dict) -> bool:
    f = (profile.get("game_or_topic_filter") or "").lower().strip()
    if not f:
        return True
    hay = f"{info.get('category', '')} {info.get('title', '')}".lower()
    if not info.get("category") and not info.get("title"):
        return True  # no metadata available (no API keys) -> don't block
    return any(part.strip() and part.strip() in hay for part in f.split("|"))
