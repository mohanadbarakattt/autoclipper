"""Clipping service API for Virlo Studio (docs/VIRLO_INTEGRATION.md).

Virlo's server calls /v1/* with `Authorization: Bearer $CLIPS_SERVICE_TOKEN`; users are Virlo user ids.
Browsers only ever get /media/* links, signed and expiring.
"""
from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import billing, settings, store, worker, workspace

router = APIRouter()


# ---------- auth + errors ----------
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        self.status, self.code, self.message = status, code, message


async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)


def service_auth(authorization: str = Header("")) -> None:
    token = settings.SERVICE_TOKEN
    given = authorization.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(given.encode(), token.encode()):
        raise ApiError(401, "unauthorized")


def _uid_ok(uid: str) -> str:
    if not uid or len(uid) > 64 or not all(c.isalnum() or c in "-_" for c in uid):
        raise ApiError(400, "bad_user_id")
    return uid


def ext_user(uid: str) -> dict:
    u = store.one("SELECT * FROM users WHERE ext_id=?", (_uid_ok(uid),))
    if not u:
        raise ApiError(404, "unknown_user", "Call PUT /v1/users/{id} first.")
    return u


# ---------- signed media ----------
KINDS = ("video", "thumb", "download")


def _sig(user_id: int, cid: int, kind: str, exp: int) -> str:
    msg = f"{user_id}:{cid}:{kind}:{exp}".encode()
    return hmac.new(settings.MEDIA_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:40]


def media_url(user_id: int, cid: int, kind: str) -> str:
    exp = int(time.time()) + settings.MEDIA_TTL_SEC
    return f"{settings.PUBLIC_URL}/media/{user_id}/{cid}/{kind}?exp={exp}&sig={_sig(user_id, cid, kind, exp)}"


@router.get("/media/{user_id}/{cid}/{kind}")
def media(user_id: int, cid: int, kind: str, exp: int = 0, sig: str = ""):
    if (kind not in KINDS or not settings.MEDIA_SECRET or exp < time.time()
            or not hmac.compare_digest(sig, _sig(user_id, cid, kind, exp))):
        raise HTTPException(403)
    c = workspace.clip(user_id, cid)
    if not c or not billing.unlocked(user_id, cid):
        raise HTTPException(404)
    f = workspace.thumb_file(user_id, c) if kind == "thumb" else workspace.clip_file(user_id, c)
    if not f:
        raise HTTPException(404)
    if kind == "thumb":
        return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
    name = "".join(ch for ch in (c.get("hook") or f"clip {cid}") if ch.isalnum() or ch in " -_")[:60].strip() or "clip"
    return FileResponse(f, media_type="video/mp4", filename=f"{name}.mp4" if kind == "download" else None,
                        headers={"Cache-Control": "private, max-age=3600"})


# ---------- shaping ----------
def clip_out(user_id: int, c: dict, full: bool = False) -> dict:
    unlocked = billing.unlocked(user_id, c["id"])
    out = {
        "id": c["id"], "status": c["status"], "label": c["label"], "channel": c["channel"],
        "platform": c.get("platform"), "score": c.get("viral_score"), "hook": c.get("hook"),
        "title": c.get("title"), "caption": c.get("caption"), "hashtags": c.get("hashtags", []),
        "scheduledAt": c.get("scheduled_at"), "postUrl": c.get("post_url"), "views": c.get("views"),
        "createdAt": c.get("created_at"), "ready": bool(c["done"]), "locked": bool(c["done"]) and not unlocked,
        "thumbUrl": media_url(user_id, c["id"], "thumb") if c["done"] and unlocked else None,
    }
    if full:
        gj = c.get("gemini_json") or {}
        out.update({
            "summary": gj.get("summary"), "reason": gj.get("reason"), "trigger": c.get("trigger"),
            "error": c.get("error") if c["status"] in ("rejected", "failed") else None,
            "videoUrl": media_url(user_id, c["id"], "video") if c["done"] and unlocked else None,
            "downloadUrl": media_url(user_id, c["id"], "download") if c["done"] and unlocked else None,
        })
    return out


def channel_out(c: dict, status: dict) -> dict:
    st = status.get(workspace.engine_id(c["platform"], c["channel"]), {})
    return {"id": c["id"], "platform": c["platform"], "channel": c["channel"],
            "url": workspace.channel_url(c["platform"], c["channel"]), "style": c["style"],
            "picky": c["picky"] or workspace.style_profile(c["style"]).get("min_viral_score", 7.5),
            "paused": bool(c["paused"]), "blocked": store.is_blocked(c["platform"], c["channel"]),
            "live": bool(st.get("is_live")), "liveTitle": st.get("title") or st.get("category"),
            "rights": c["rights_basis"], "createdAt": c["created_at"]}


def _scan_info(u: dict) -> dict:
    job = store.one("SELECT status, started_at, finished_at, error FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT 1",
                    (u["id"],)) or {}
    return {"status": job.get("status") or "idle", "lastScanAt": u["last_scan_at"],
            "nextScanAt": (u["last_scan_at"] or time.time()) + settings.SCAN_MINUTES * 60,
            "failed": job.get("status") == "failed"}


# ---------- users ----------
class UserIn(BaseModel):
    email: str = ""
    tier: str = "free"


@router.put("/v1/users/{uid}", dependencies=[Depends(service_auth)])
def put_user(uid: str, body: UserIn):
    tier = body.tier if body.tier in settings.VIRLO_TIERS else "free"
    u = store.one("SELECT * FROM users WHERE ext_id=?", (_uid_ok(uid),))
    if u:
        store.x("UPDATE users SET tier=? WHERE id=?", (tier, u["id"]))
        if u["tier"] != tier:
            workspace.sync(u["id"])
    else:
        # placeholder email/password: Virlo users never log in here
        store.x("INSERT INTO users (email, pw_hash, ext_id, tier, created_at) VALUES (?,?,?,?,?)",
                (f"virlo:{uid}", "!", uid, tier, time.time()))
    u = ext_user(uid)
    return {"ok": True, "tier": u["tier"], "limits": settings.limits(u)}


@router.get("/v1/users/{uid}/overview", dependencies=[Depends(service_auth)])
def overview(uid: str):
    u = ext_user(uid)
    status = workspace.stream_status(u["id"])
    chans = store.q("SELECT * FROM channels WHERE user_id=? ORDER BY id", (u["id"],))
    links = store.q("SELECT * FROM links WHERE user_id=? ORDER BY id DESC LIMIT 20", (u["id"],))
    return {"limits": settings.limits(u), "counts": workspace.counts(u["id"]), "scan": _scan_info(u),
            "channels": [channel_out(c, status) for c in chans],
            "links": [link_out(x) for x in links],
            "latest": [clip_out(u["id"], c) for c in workspace.clips(u["id"], limit=8)]}


def link_out(x: dict) -> dict:
    return {"id": x["id"], "platform": x["platform"], "url": x["url"], "status": x["status"],
            "found": x["found"], "error": x["error"], "createdAt": x["created_at"]}


# ---------- one box: any link (a video gets clipped once, a channel gets watched) ----------
class LinkIn(BaseModel):
    link: str
    rights: str
    rightsNote: str = ""
    style: str = "default"
    ip: str = ""


@router.post("/v1/users/{uid}/links", dependencies=[Depends(service_auth)])
def add_link(uid: str, body: LinkIn):
    u = ext_user(uid)
    try:
        if workspace.parse_video(body.link):
            link = workspace.add_link(u, body.link, body.rights)
            store.x("UPDATE users SET last_scan_at=NULL WHERE id=?", (u["id"],))
            worker.enqueue(u["id"])
            return {"kind": "video", "link": link_out(link)}
        ch = workspace.add_channel(u, body.link, "", body.style, body.rights, body.rightsNote, body.ip)
    except workspace.ChannelError as e:
        raise ApiError(400, "link_rejected", str(e))
    worker.enqueue(u["id"])
    return {"kind": "channel", "channel": channel_out(ch, {})}


# ---------- channels ----------
class ChannelIn(BaseModel):
    link: str
    platform: str = ""
    style: str = "default"
    rights: str
    rightsNote: str = ""
    ip: str = ""


class ChannelPatch(BaseModel):
    style: str | None = None
    picky: float | None = Field(None, ge=4, le=9)
    paused: bool | None = None


@router.get("/v1/users/{uid}/channels", dependencies=[Depends(service_auth)])
def list_channels(uid: str):
    u = ext_user(uid)
    status = workspace.stream_status(u["id"])
    return {"channels": [channel_out(c, status) for c in store.q("SELECT * FROM channels WHERE user_id=? ORDER BY id", (u["id"],))],
            "styles": settings.STYLES, "limits": settings.limits(u)}


@router.post("/v1/users/{uid}/channels", dependencies=[Depends(service_auth)])
def add_channel(uid: str, body: ChannelIn):
    u = ext_user(uid)
    try:
        ch = workspace.add_channel(u, body.link, body.platform, body.style, body.rights, body.rightsNote, body.ip)
    except workspace.ChannelError as e:
        raise ApiError(400, "channel_rejected", str(e))
    worker.enqueue(u["id"])
    return {"channel": channel_out(ch, {})}


def _own_channel(u: dict, chid: int) -> dict:
    ch = store.one("SELECT * FROM channels WHERE id=? AND user_id=?", (chid, u["id"]))
    if not ch:
        raise ApiError(404, "not_found")
    return ch


@router.patch("/v1/users/{uid}/channels/{chid}", dependencies=[Depends(service_auth)])
def patch_channel(uid: str, chid: int, body: ChannelPatch):
    u = ext_user(uid)
    ch = _own_channel(u, chid)
    style = body.style if body.style in settings.STYLES else ch["style"]
    picky = body.picky if body.picky is not None else ch["picky"]
    paused = int(body.paused) if body.paused is not None else ch["paused"]
    store.x("UPDATE channels SET style=?, picky=?, paused=? WHERE id=?", (style, picky, paused, chid))
    workspace.sync(u["id"])
    return {"channel": channel_out(_own_channel(u, chid), workspace.stream_status(u["id"]))}


@router.delete("/v1/users/{uid}/channels/{chid}", dependencies=[Depends(service_auth)])
def delete_channel(uid: str, chid: int):
    u = ext_user(uid)
    _own_channel(u, chid)
    store.x("DELETE FROM channels WHERE id=?", (chid,))
    workspace.sync(u["id"])
    return {"ok": True}


@router.post("/v1/users/{uid}/scan", dependencies=[Depends(service_auth)])
def scan(uid: str):
    u = ext_user(uid)
    store.x("UPDATE users SET last_scan_at=NULL WHERE id=?", (u["id"],))
    return {"queued": bool(worker.enqueue(u["id"]))}


# ---------- clips ----------
class ClipPatch(BaseModel):
    title: str = ""
    caption: str = ""
    hashtags: list[str] = []


class PostedIn(BaseModel):
    url: str = ""


def _own_clip(u: dict, cid: int) -> dict:
    c = workspace.clip(u["id"], cid)
    if not c:
        raise ApiError(404, "not_found")
    return c


@router.get("/v1/users/{uid}/clips", dependencies=[Depends(service_auth)])
def list_clips(uid: str, status: str = "", q: str = ""):
    u = ext_user(uid)
    return {"clips": [clip_out(u["id"], c) for c in workspace.clips(u["id"], status, q.strip()[:80])],
            "counts": workspace.counts(u["id"])}


@router.get("/v1/users/{uid}/clips/{cid}", dependencies=[Depends(service_auth)])
def get_clip(uid: str, cid: int):
    u = ext_user(uid)
    return {"clip": clip_out(u["id"], _own_clip(u, cid), full=True)}


@router.patch("/v1/users/{uid}/clips/{cid}", dependencies=[Depends(service_auth)])
def patch_clip(uid: str, cid: int, body: ClipPatch):
    u = ext_user(uid)
    _own_clip(u, cid)
    tags = [t.strip().lstrip("#") for t in body.hashtags if t.strip().lstrip("#")][:15]
    workspace.update_text(u["id"], cid, body.title.strip(), body.caption.strip(), tags)
    return {"clip": clip_out(u["id"], _own_clip(u, cid), full=True)}


@router.post("/v1/users/{uid}/clips/{cid}/posted", dependencies=[Depends(service_auth)])
def posted(uid: str, cid: int, body: PostedIn):
    u = ext_user(uid)
    _own_clip(u, cid)
    links = " ".join(x for x in body.url.split() if x.startswith(("http://", "https://")))
    workspace.mark_posted(u["id"], cid, links)
    return {"clip": clip_out(u["id"], _own_clip(u, cid), full=True)}


@router.delete("/v1/users/{uid}/clips/{cid}", dependencies=[Depends(service_auth)])
def delete_clip(uid: str, cid: int):
    u = ext_user(uid)
    _own_clip(u, cid)
    workspace.remove_clip(u["id"], cid)
    return {"ok": True}


@router.get("/v1/users/{uid}/activity", dependencies=[Depends(service_auth)])
def activity(uid: str):
    u = ext_user(uid)
    jobs = store.q("SELECT id, status, started_at, finished_at, result, error FROM jobs WHERE user_id=? "
                   "ORDER BY id DESC LIMIT 10", (u["id"],))
    for j in jobs:
        j["error"] = (j["error"] or "")[:200] if j["status"] == "failed" else None
    return {"scans": jobs, "moments": [clip_out(u["id"], c, full=True) for c in workspace.activity(u["id"], 150)]}
