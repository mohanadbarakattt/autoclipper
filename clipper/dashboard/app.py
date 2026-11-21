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
        out.append(r)
    return out


class PostedIn(BaseModel):
    url: str = ""


@app.post("/api/clips/{cid}/posted")
def mark_posted(cid: int, body: PostedIn | None = None):
    """Mark posted; optional link(s) to the live post(s) so views can be tracked and learned from."""
    url = (body.url if body else "").strip()
    db.update_candidate(cid, status="posted", posted_at=time.time(), **({"post_url": url} if url else {}))
    return {"ok": True}


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
