"""One engine workspace per user: settings written in, clips read out.

saas_data/workspaces/u<id>/
    config/streamers.json        <- the user's channels (written here)
    config/profiles/c<chan>.json <- the channel's style (engine profile + user overrides)
    data/state/clipper.db        <- written by the engine; read here
    data/ready_clips/*.mp4       <- finished clips
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from . import settings, store

PLATFORMS = ("twitch", "kick", "youtube")
DONE = ("ready", "scheduled", "drafted", "posted")
STATUS_LABEL = {
    "pending": "Checking", "analyzing": "Checking", "rendering": "Editing", "ready": "Ready",
    "scheduled": "Scheduled", "drafted": "Ready", "posted": "Posted", "rejected": "Skipped",
    "failed": "Error", "publish_failed": "Ready",
}


def root(user_id: int) -> Path:
    return settings.WORKSPACES / f"u{int(user_id)}"


# ---------- channel links ----------
class ChannelError(ValueError):
    pass


_NAME = re.compile(r"^[A-Za-z0-9_.\-]{2,60}$")


def parse_channel(text: str, platform_hint: str = "") -> tuple[str, str]:
    """'https://twitch.tv/kaicenat' -> ('twitch', 'kaicenat'); '@MrBeast' + hint 'youtube' -> ('youtube', 'MrBeast')."""
    t = (text or "").strip()
    if not t:
        raise ChannelError("Paste a channel link.")
    if "://" not in t and ("." in t.split("/")[0]) and not t.startswith("@"):
        t = "https://" + t
    if "://" in t:
        u = urlparse(t)
        host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
        parts = [p for p in u.path.split("/") if p]
        if host in ("twitch.tv",) and parts:
            return "twitch", _clean(parts[0], "twitch")
        if host in ("kick.com",) and parts:
            return "kick", _clean(parts[0], "kick")
        if host in ("youtube.com", "youtu.be") and parts:
            if parts[0].startswith("@"):
                return "youtube", _clean(parts[0][1:], "youtube")
            if parts[0] in ("channel",) and len(parts) > 1 and parts[1].startswith("UC"):
                return "youtube", _clean(parts[1], "youtube")
            if parts[0] in ("c", "user") and len(parts) > 1:
                return "youtube", _clean(parts[1], "youtube")
        raise ChannelError("That link isn't a Twitch, Kick or YouTube channel. Try the channel's main page link.")
    hint = (platform_hint or "").lower()
    if hint not in PLATFORMS:
        raise ChannelError("Paste the full channel link (twitch.tv/…, kick.com/… or youtube.com/@…).")
    return hint, _clean(t.lstrip("@"), hint)


def _clean(name: str, platform: str) -> str:
    if not _NAME.match(name):
        raise ChannelError("That channel name doesn't look right.")
    return name if platform == "youtube" else name.lower()


def channel_url(platform: str, channel: str) -> str:
    if platform == "twitch":
        return f"https://twitch.tv/{channel}"
    if platform == "kick":
        return f"https://kick.com/{channel}"
    if channel.startswith("UC") and len(channel) == 24:
        return f"https://youtube.com/channel/{channel}"
    return f"https://youtube.com/@{channel}"


def engine_id(platform: str, channel: str) -> str:
    """Same id the engine gives a streamer (clipper.config.load_streamers)."""
    return f"{platform}:{channel.lstrip('@').lower()}"


# ---------- settings in ----------
def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def style_profile(style: str) -> dict:
    name = style if style in settings.STYLES else "default"
    p = settings.ENGINE_PROFILES / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def channel_profile(ch: dict, plan: dict) -> dict:
    prof = style_profile(ch["style"])
    over: dict = {
        "profile_name": f"c{ch['id']}",
        "publish": {"mode": "off"},             # users download and post themselves (M1)
        "auto_discover": {"enabled": False},
    }
    if ch.get("picky") is not None:
        over["min_viral_score"] = float(ch["picky"])
    cap = plan.get("max_clip_seconds")
    if cap:  # the plan's longest clip: the engine never cuts past it
        d = prof.get("clip_duration") or {"min": 18, "max": 45}
        mx = max(5, min(int(d.get("max", 45)), int(cap)))
        over["clip_duration"] = {"min": min(int(d.get("min", 18)), mx), "max": mx}
    if not plan.get("campaign_rules"):
        for k in ("campaign_rules", "required_hashtags", "caption_suffix"):
            prof.pop(k, None)
    return _deep_merge(prof, over)


def sync(user_id: int, max_clip_seconds: int | None = None) -> list[dict]:
    """Write the user's channels + styles into their workspace. Returns the active channels.
    max_clip_seconds (from the billing allowance) overrides the plan's own cap when given."""
    user = store.one("SELECT * FROM users WHERE id=?", (user_id,))
    plan = settings.limits(user) if user else settings.plan(None)
    if max_clip_seconds:
        plan = {**plan, "max_clip_seconds": int(max_clip_seconds)}
    ws = root(user_id)
    prof_dir = ws / "config" / "profiles"
    prof_dir.mkdir(parents=True, exist_ok=True)
    chans = store.q("SELECT * FROM channels WHERE user_id=? ORDER BY id", (user_id,))
    streamers, active = [], []
    for ch in chans:
        blocked = store.is_blocked(ch["platform"], ch["channel"])
        paused = bool(ch["paused"]) or blocked
        (prof_dir / f"c{ch['id']}.json").write_text(json.dumps(channel_profile(ch, plan), indent=2), encoding="utf-8")
        streamers.append({"platform": ch["platform"], "channel": ch["channel"], "profile": f"c{ch['id']}",
                          "live": False, "vods": bool(ch["vods"]), "paused": paused})
        if not paused and ch["vods"]:
            active.append(ch)
    keep = {f"c{c['id']}.json" for c in chans}
    for f in prof_dir.glob("c*.json"):
        if f.name not in keep:
            f.unlink(missing_ok=True)
    (ws / "config" / "streamers.json").write_text(json.dumps({"streamers": streamers}, indent=2), encoding="utf-8")
    return active


def delete(user_id: int) -> None:
    shutil.rmtree(root(user_id), ignore_errors=True)


# ---------- clips out ----------
def _db_path(user_id: int) -> Path:
    return root(user_id) / "data" / "state" / "clipper.db"


def _read(user_id: int, sql: str, args: tuple = ()) -> list[dict]:
    p = _db_path(user_id)
    if not p.exists():
        return []
    c = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []  # engine hasn't created its tables yet
    finally:
        c.close()


def _write(user_id: int, sql: str, args: tuple = ()) -> None:
    p = _db_path(user_id)
    if not p.exists():
        return
    c = sqlite3.connect(p, timeout=30)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _decorate(user_id: int, r: dict) -> dict:
    r["label"] = STATUS_LABEL.get(r.get("status") or "", (r.get("status") or "").title())
    r["done"] = r.get("status") in DONE and bool(clip_file(user_id, r))
    for k in ("score_json", "gemini_json"):
        try:
            r[k] = json.loads(r[k]) if r.get(k) else {}
        except (TypeError, ValueError):
            r[k] = {}
    r["hashtags"] = (r.get("score_json") or {}).get("hashtags") or []
    r["channel"] = (r.get("streamer_id") or "").split(":", 1)[-1]
    return r


def clips(user_id: int, status: str = "", search: str = "", limit: int = 120) -> list[dict]:
    sql, args = "SELECT * FROM candidates WHERE 1=1", []
    groups = {"ready": ("ready", "drafted", "publish_failed"), "scheduled": ("scheduled",), "posted": ("posted",),
              "skipped": ("rejected", "failed"), "working": ("pending", "analyzing", "rendering")}
    if status in groups:
        sql += f" AND status IN ({','.join('?' * len(groups[status]))})"
        args += groups[status]
    elif not status:
        sql += f" AND status IN ({','.join('?' * len(DONE))}, 'publish_failed')"
        args += DONE
    if search:
        sql += " AND (hook LIKE ? OR title LIKE ? OR caption LIKE ? OR streamer_id LIKE ?)"
        args += [f"%{search}%"] * 4
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [_decorate(user_id, r) for r in _read(user_id, sql, tuple(args))]


def activity(user_id: int, limit: int = 200) -> list[dict]:
    return [_decorate(user_id, r) for r in _read(user_id, "SELECT * FROM candidates ORDER BY id DESC LIMIT ?", (limit,))]


def clip(user_id: int, cid: int) -> dict | None:
    rows = _read(user_id, "SELECT * FROM candidates WHERE id=?", (int(cid),))
    return _decorate(user_id, rows[0]) if rows else None


def counts(user_id: int) -> dict:
    rows = _read(user_id, "SELECT status, COUNT(*) n FROM candidates GROUP BY status")
    by = {r["status"]: r["n"] for r in rows}
    return {"ready": sum(by.get(s, 0) for s in ("ready", "drafted", "publish_failed", "scheduled")),
            "posted": by.get("posted", 0),
            "working": sum(by.get(s, 0) for s in ("pending", "analyzing", "rendering")),
            "skipped": by.get("rejected", 0) + by.get("failed", 0)}


def stream_status(user_id: int) -> dict[str, dict]:
    return {r["streamer_id"]: r for r in _read(user_id, "SELECT * FROM stream_status")}


def finished_ids(user_id: int) -> set[int]:
    return {r["id"] for r in _read(user_id, "SELECT id FROM candidates WHERE out_path IS NOT NULL AND out_path != ''")}


def clip_file(user_id: int, r: dict) -> Path | None:
    """The clip's video, only if it really is inside this user's workspace."""
    p = r.get("out_path")
    if not p:
        return None
    path = Path(p).resolve()
    ws = root(user_id).resolve()
    if ws not in path.parents or not path.exists():
        return None
    return path


def thumb_file(user_id: int, r: dict) -> Path | None:
    video = clip_file(user_id, r)
    if not video:
        return None
    jpg = video.with_suffix(".jpg")
    if not jpg.exists():
        try:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1.5", "-i", str(video), "-frames:v", "1",
                            "-vf", "scale=360:-2", "-q:v", "4", str(jpg)], timeout=60, check=True,
                           capture_output=True)
        except Exception:
            return None
    return jpg if jpg.exists() else None


def update_text(user_id: int, cid: int, title: str, caption: str, hashtags: list[str]) -> None:
    r = clip(user_id, cid)
    if not r:
        return
    score = r.get("score_json") or {}
    score["hashtags"] = hashtags
    _write(user_id, "UPDATE candidates SET title=?, caption=?, score_json=? WHERE id=?",
           (title[:200], caption[:2200], json.dumps(score), int(cid)))


def mark_posted(user_id: int, cid: int, url: str) -> None:
    import time
    _write(user_id, "UPDATE candidates SET status='posted', post_url=?, posted_at=?, updated_at=? WHERE id=?",
           (url[:1000], time.time(), time.time(), int(cid)))


def remove_clip(user_id: int, cid: int) -> None:
    r = clip(user_id, cid)
    if not r:
        return
    f = clip_file(user_id, r)
    if f:
        f.unlink(missing_ok=True)
        f.with_suffix(".jpg").unlink(missing_ok=True)
    import time
    _write(user_id, "UPDATE candidates SET status='rejected', out_path=NULL, scheduled_at=NULL, error=?, updated_at=? "
           "WHERE id=?", ("deleted by you", time.time(), int(cid)))


# ---------- channels (shared by the web app and the service API) ----------
RIGHTS = ("own", "permission", "campaign", "other")


def add_channel(user: dict, link: str, platform: str, style: str, rights: str, note: str = "", ip: str = "") -> dict:
    """Validate and add a channel. Raises ChannelError with a user-facing reason."""
    import time
    plat, name = parse_channel(link, platform)
    if rights not in RIGHTS:
        raise ChannelError("Tell us why you can clip this channel.")
    if store.is_blocked(plat, name):
        raise ChannelError("This streamer has asked not to be clipped.")
    lim = settings.limits(user)
    have = store.one("SELECT COUNT(*) n FROM channels WHERE user_id=?", (user["id"],))["n"]
    if have >= lim["channels"]:
        raise ChannelError(f"Your {lim['label']} plan has {lim['channels']} channel(s). Upgrade to add more.")
    if store.one("SELECT 1 FROM channels WHERE user_id=? AND platform=? AND lower(channel)=lower(?)",
                 (user["id"], plat, name)):
        raise ChannelError("You already added this channel.")
    now = time.time()
    cid = store.x("INSERT INTO channels (user_id, platform, channel, style, rights_basis, rights_note, rights_ip,"
                  " rights_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (user["id"], plat, name, style if style in settings.STYLES else "default", rights,
                   (note or "")[:500], ip, now, now))
    sync(user["id"])
    store.x("UPDATE users SET last_scan_at=NULL WHERE id=?", (user["id"],))
    return store.one("SELECT * FROM channels WHERE id=?", (cid,))
