"""Control panel + JSON API (also what OpenBot talks to)."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import db
from ..config import (DATA_DIR, READY_DIR, list_profiles, load_profile, load_streamers, save_profile,
                      save_streamers)
from ..processor import ai, pipeline
from ..processor.publish import TryPost

app = FastAPI(title="AutoClipper")
app.mount("/media", StaticFiles(directory=str(READY_DIR)), name="media")
STATIC = Path(__file__).parent / "static"
STARTED = time.time()
EVENTS: dict = {"vod_scan": None}  # set by __main__ to trigger a scan


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _thumb(r: dict) -> str | None:
    """Poster for a rendered clip; made on demand for clips rendered before thumbnails existed."""
    if not r.get("out_path") or not Path(r["out_path"]).exists():
        return None
    jpg = Path(r["out_path"]).with_suffix(".jpg")
    if not jpg.exists():
        from ..processor.render import make_thumb
        make_thumb(Path(r["out_path"]))
    return f"/media/{jpg.name}" if jpg.exists() else None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    tp = TryPost()
    tp_ok = None
    if tp.configured:
        try:
            tp_ok = len(tp.accounts())
        except Exception as e:
            tp_ok = f"error: {e}"[:200]
    du = shutil.disk_usage(DATA_DIR)
    return {
        "ok": True, "uptime_sec": int(time.time() - STARTED),
        "gemini_key": bool(__import__("os").getenv("GEMINI_API_KEY")),
        "claude_cli": bool(ai.claude_bin()), "scorer": ai.SCORER,
        "trypost_accounts": tp_ok,
        "queue": {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM candidates GROUP BY status")},
        "disk_free_gb": round(du.free / 1e9, 1), "data_gb": round(_dir_size(DATA_DIR) / 1e9, 2),
    }


@app.get("/api/overview")
def overview():
    day = time.time() - 86400
    clips_today = db.q("SELECT COUNT(*) n FROM candidates WHERE status IN ('ready','scheduled','drafted','posted') AND updated_at>?", (day,))[0]["n"]
    avg = db.q("SELECT AVG(viral_score) a FROM candidates WHERE viral_score IS NOT NULL AND updated_at>?", (day,))[0]["a"]
    live = db.q("SELECT COUNT(*) n FROM stream_status WHERE recording=1")[0]["n"]
    pending = db.q("SELECT COUNT(*) n FROM candidates WHERE status IN ('pending','analyzing','rendering')")[0]["n"]
    return {"live_recording": live, "clips_today": clips_today, "avg_score_24h": round(avg, 2) if avg else None,
            "queue": pending, "data_gb": round(_dir_size(DATA_DIR) / 1e9, 2)}


@app.get("/api/streams")
def streams():
    status = {r["streamer_id"]: r for r in db.q("SELECT * FROM stream_status")}
    return [{**s, **{k: v for k, v in status.get(s["id"], {}).items() if k != "streamer_id"}} for s in load_streamers()]


class StreamerIn(BaseModel):
    platform: str
    channel: str
    profile: str = "default"
    live: bool = True
    vods: bool = True


@app.post("/api/streamers")
def add_streamer(s: StreamerIn):
    if s.platform.lower() not in ("twitch", "kick", "youtube"):
        raise HTTPException(400, "platform must be twitch, kick or youtube")
    cur = load_streamers()
    new = {"platform": s.platform.lower(), "channel": s.channel.strip(), "profile": s.profile,
           "live": s.live, "vods": s.vods, "paused": False}
    sid = f"{new['platform']}:{new['channel'].lstrip('@').lower()}"
    if any(x["id"] == sid for x in cur):
        raise HTTPException(409, "already added")
    save_streamers(cur + [new])
    return {"ok": True, "id": sid}


@app.post("/api/streamers/{sid}/toggle")
def toggle_streamer(sid: str):
    cur = load_streamers()
    for s in cur:
        if s["id"] == sid:
            s["paused"] = not s.get("paused")
            save_streamers(cur)
            return {"paused": s["paused"]}
    raise HTTPException(404)


@app.patch("/api/streamers/{sid}")
def edit_streamer(sid: str, patch: dict):
    cur = load_streamers()
    for s in cur:
        if s["id"] == sid:
            s.update({k: v for k, v in patch.items() if k in ("profile", "live", "vods", "paused")})
            save_streamers(cur)
            return s
    raise HTTPException(404)


@app.delete("/api/streamers/{sid}")
def delete_streamer(sid: str):
    save_streamers([s for s in load_streamers() if s["id"] != sid])
    db.x("DELETE FROM stream_status WHERE streamer_id=?", (sid,))
    return {"ok": True}


@app.get("/api/profiles")
def profiles():
    return {name: load_profile(name) for name in list_profiles()}


@app.put("/api/profiles/{name}")
def put_profile(name: str, data: dict):
    if not name.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "bad profile name")
    save_profile(name, data)
    return {"ok": True}


@app.get("/api/clips")
def clips(limit: int = 60, status: str | None = None):
    sql, args = "SELECT * FROM candidates", ()
    if status:
        sql, args = sql + " WHERE status=?", (status,)
    rows = db.q(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
    for r in rows:
        for k in ("gemini_json", "score_json", "trypost_post_ids"):
            r[k] = json.loads(r[k]) if r.get(k) else None
        r["video_url"] = f"/media/{Path(r['out_path']).name}" if r.get("out_path") and Path(r["out_path"]).exists() else None
        r["thumb_url"] = _thumb(r)
    return rows


@app.post("/api/clips/{cid}/requeue")
def requeue(cid: int):
    r = db.q("SELECT * FROM candidates WHERE id=?", (cid,))
    if not r:
        raise HTTPException(404)
    r = r[0]
    if r.get("out_path") and Path(r["out_path"]).exists():
        pipeline.publish_candidate(cid)  # rendered already: just (re)publish
        return {"ok": True, "action": "publish"}
    if Path(r["raw_path"] or "").exists():
        db.update_candidate(cid, status="pending", error=None)
        return {"ok": True, "action": "reprocess"}
    raise HTTPException(409, "source footage already deleted")


@app.get("/api/schedule")
def schedule():
    rows = db.q("SELECT id, streamer_id, status, viral_score, hook, title, caption, score_json, out_path, scheduled_at, post_url, views "
                "FROM candidates WHERE scheduled_at IS NOT NULL AND status IN ('ready','scheduled','drafted','posted') "
                "ORDER BY scheduled_at")
    cutoff = time.time() - 86400
    out = []
    for r in rows:
        from datetime import datetime
        if datetime.fromisoformat(r["scheduled_at"]).timestamp() < cutoff:
            continue
        r["score_json"] = json.loads(r["score_json"]) if r.get("score_json") else None
        r["video_url"] = f"/media/{Path(r['out_path']).name}" if r.get("out_path") and Path(r["out_path"]).exists() else None
        r["thumb_url"] = _thumb(r)
        out.append(r)
    return out


class PostedIn(BaseModel):
    url: str = ""


@app.post("/api/clips/{cid}/posted")
def mark_posted(cid: int, body: PostedIn | None = None):
    """Mark posted; optional link(s) to the live post(s) so views can be tracked and learned from."""
    url = (body.url if body else "").strip()
    cur = db.q("SELECT post_url, posted_at FROM candidates WHERE id=?", (cid,))
    if not cur:
        raise HTTPException(404)
    links = (cur[0]["post_url"] or "").split()
    links += [u for u in url.split() if u not in links]  # posting to another platform adds a link
    db.update_candidate(cid, status="posted", posted_at=cur[0]["posted_at"] or time.time(),
                        **({"post_url": " ".join(links)} if links else {}))
    return {"ok": True, "links": links}


_PLATFORM_HOSTS = {"youtube": ("youtube.com", "youtu.be"), "tiktok": ("tiktok.com",), "instagram": ("instagram.com",)}


@app.get("/api/posting-queue")
def posting_queue(platform: str = "youtube", limit: int = 3):
    """Ready-to-upload clips for an agent (Grokbot, OpenBot, ...) or a script: absolute file path, title,
    description and the scheduled time, best slot first. Clips already posted on `platform` are skipped.
    After uploading, POST /api/clips/{id}/posted {"url": "<link to the post>"}."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    hosts = _PLATFORM_HOSTS.get(platform.lower(), (platform.lower(),))
    rows = db.q("SELECT id, streamer_id, viral_score, hook, title, caption, score_json, out_path, scheduled_at, post_url "
                "FROM candidates WHERE out_path IS NOT NULL AND status IN ('ready','scheduled','drafted','posted') "
                "AND substr(COALESCE(profile, ''), 1, 7) != 'client_' "  # client work never goes on your channel
                "ORDER BY scheduled_at IS NULL, scheduled_at")
    out = []
    for r in rows:
        if any(h in (r["post_url"] or "") for h in hosts) or not Path(r["out_path"]).exists():
            continue
        score = json.loads(r["score_json"] or "{}")
        tags = [t.lstrip("#").replace(" ", "") for t in score.get("hashtags", [])][:5]
        title = (r["title"] or r["hook"] or "Stream highlight").replace("<", "").replace(">", "")[:95]
        if platform.lower() == "youtube" and "#shorts" not in title.lower() and len(title) <= 90:
            title += " #Shorts"
        when = datetime.fromisoformat(r["scheduled_at"]) if r["scheduled_at"] else datetime.now(timezone.utc)
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        local = when.astimezone()  # this PC's timezone = what YouTube Studio's scheduler shows in its browser
        out.append({
            "id": r["id"], "file": str(Path(r["out_path"]).resolve()), "streamer": r["streamer_id"],
            "score": r["viral_score"], "title": title,
            "description": f"{(r['caption'] or '').strip()}\n\n" + " ".join("#" + t for t in tags),
            "hashtags": tags,
            "post_at_utc": when.isoformat(),
            "post_at_local": {"date": local.strftime("%Y-%m-%d"), "time": local.strftime("%H:%M"),
                              "pretty": local.strftime("%a %d %b %Y, %I:%M %p")},
            "post_at_new_york": when.astimezone(ZoneInfo("America/New_York")).strftime("%a %d %b %I:%M %p ET"),
            "in_past": when < datetime.now(timezone.utc),
            "mark_posted": f"POST /api/clips/{r['id']}/posted  body: {{\"url\": \"<link to the {platform} post>\"}}",
        })
        if len(out) >= max(1, min(limit, 20)):
            break
    return {"platform": platform, "count": len(out), "items": out}


@app.get("/api/guardrails")
def guardrails():
    """Today's usage vs. the daily limits (MAX_* settings in .env)."""
    from .. import guard
    return guard.status()


@app.post("/api/refresh-views")
def refresh_views():
    from ..processor import learn
    return {"updated": learn.refresh_views()}


@app.post("/api/clips/{cid}/approve")
def approve(cid: int):
    """Publish a clip even if the score gate rejected it (needs it rendered or the raw kept)."""
    return requeue(cid)


@app.delete("/api/clips/{cid}")
def delete_clip(cid: int):
    r = db.q("SELECT raw_path, out_path FROM candidates WHERE id=?", (cid,))
    for p in (r[0].values() if r else []):
        if p:
            Path(p).unlink(missing_ok=True)
    db.x("DELETE FROM candidates WHERE id=?", (cid,))
    return {"ok": True}


@app.post("/api/scan-vods")
def scan_vods():
    if EVENTS["vod_scan"]:
        EVENTS["vod_scan"].set()
    return {"ok": True}


class ManualIn(BaseModel):
    url: str
    start: float
    end: float
    profile: str = "default"


@app.post("/api/manual")
def manual(m: ManualIn):
    """Queue any VOD/YouTube section by hand (handy for testing)."""
    from ..config import CANDIDATES_DIR
    from ..media import ytdlp_section
    out = CANDIDATES_DIR / f"manual_{int(time.time())}.mp4"
    ytdlp_section(m.url, m.start, m.end, out)
    cid = db.add_candidate({"id": "manual:" + m.url[-30:], "platform": "manual", "profile": m.profile},
                           "manual", m.url, str(out), "manual", m.start)
    return {"ok": True, "id": cid}
