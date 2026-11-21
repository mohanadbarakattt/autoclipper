"""Virlo Studio web app: landing, accounts, onboarding, dashboard, clips, channels, activity, account."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, settings, store, worker, workspace

HERE = Path(__file__).parent
app = FastAPI(title="Virlo Studio", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))
COOKIE = "vs_session"
login_limit = auth.RateLimiter(10, 600)
signup_limit = auth.RateLimiter(5, 3600)
RIGHTS = {
    "own": "It's my channel",
    "permission": "I have the streamer's permission",
    "campaign": "It's for a clipping campaign",
    "other": "Other (I take responsibility)",
}


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 0:
        s = -s
        return "in " + (f"{s // 60} min" if s < 3600 else f"{s // 3600} h")
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso)
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return d.strftime("%a %d %b, %H:%M UTC")
    except ValueError:
        return iso


templates.env.filters["ago"] = _ago
templates.env.filters["when"] = _when
templates.env.globals.update(STYLES=settings.STYLES, PLANS=settings.PLANS, RIGHTS=RIGHTS,
                             channel_url=workspace.channel_url)


# ---------- plumbing ----------
@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    """CSRF guard: state-changing requests must come from our own pages (cookies are also SameSite=Lax)."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"error": "cross-site request blocked"}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


def current_user(request: Request) -> dict | None:
    return auth.session_user(request.cookies.get(COOKIE))


class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _to_login(request: Request, exc: LoginRequired):
    return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)


def need_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise LoginRequired()
    return u


def page(request: Request, name: str, user: dict | None = None, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"user": user, "msg": request.query_params.get("msg"),
                                                       "err": request.query_params.get("err"), **ctx})


def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


def _ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def _login_response(user_id: int, to: str) -> RedirectResponse:
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(COOKIE, auth.new_session(user_id), max_age=settings.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=settings.COOKIE_SECURE)
    return resp


def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/app"


def usage_info(user: dict) -> dict:
    p = settings.plan(user["plan"])
    used = store.usage(user["id"], "clip")
    return {"plan": p, "used": used, "left": max(0, p["clips"] - used),
            "pct": min(100, round(100 * used / p["clips"])) if p["clips"] else 100}


# ---------- public ----------
@app.get("/health")
def health():
    store.one("SELECT 1 x")
    return {"ok": True, "queued": store.one("SELECT COUNT(*) n FROM jobs WHERE status='queued'")["n"],
            "running": store.one("SELECT COUNT(*) n FROM jobs WHERE status='running'")["n"]}


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    u = current_user(request)
    return page(request, "landing.html", u)


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return page(request, "auth.html", current_user(request), mode="signup")


@app.post("/signup")
def signup(request: Request, email: str = Form(""), password: str = Form(""), terms: str = Form("")):
    if not signup_limit.allow(_ip(request)):
        return back("/signup", err="Too many sign-ups from here. Try again later.")
    if terms != "on":
        return back("/signup", err="Please accept the terms to continue.")
    try:
        uid = auth.signup(email, password)
    except auth.AuthError as e:
        return back("/signup", err=str(e))
    return _login_response(uid, "/app/onboarding")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return page(request, "auth.html", current_user(request), mode="login", next=request.query_params.get("next", ""))


@app.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("")):
    if not login_limit.allow(_ip(request)):
        return back("/login", err="Too many tries. Wait a few minutes.")
    try:
        uid = auth.login(email, password)
    except auth.AuthError as e:
        return back("/login", err=str(e))
    return _login_response(uid, _safe_next(next))


@app.post("/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return page(request, "terms.html", current_user(request))


# ---------- app ----------
def _channels(uid: int) -> list[dict]:
    chans = store.q("SELECT * FROM channels WHERE user_id=? ORDER BY id", (uid,))
    status = workspace.stream_status(uid)
    for c in chans:
        c["status"] = status.get(workspace.engine_id(c["platform"], c["channel"]), {})
        c["blocked"] = store.is_blocked(c["platform"], c["channel"])
        c["picky_value"] = c["picky"] or workspace.style_profile(c["style"]).get("min_viral_score", 7.5)
    return chans


def _job_info(user: dict) -> dict:
    job = store.one("SELECT * FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],))
    nxt = (user["last_scan_at"] or time.time()) + settings.SCAN_MINUTES * 60
    return {"job": job, "next_scan": nxt}


@app.get("/app", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(need_user)):
    chans = _channels(user["id"])
    if not chans:
        return RedirectResponse("/app/onboarding", status_code=303)
    return page(request, "dashboard.html", user, channels=chans, clips=workspace.clips(user["id"], limit=6),
                counts=workspace.counts(user["id"]), usage=usage_info(user), **_job_info(user))


@app.get("/app/status")
def status_json(user: dict = Depends(need_user)):
    info = _job_info(user)
    job = info["job"] or {}
    return {"job": job.get("status"), "counts": workspace.counts(user["id"]), "next_scan": info["next_scan"]}


@app.get("/app/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, user: dict = Depends(need_user)):
    return page(request, "onboarding.html", user, usage=usage_info(user))


@app.post("/app/channels")
def add_channel(request: Request, user: dict = Depends(need_user), link: str = Form(""), platform: str = Form(""),
                style: str = Form("default"), rights: str = Form(""), rights_note: str = Form(""),
                origin: str = Form("channels")):
    src = "/app/onboarding" if origin == "onboarding" else "/app/channels"
    try:
        plat, name = workspace.parse_channel(link, platform)
    except workspace.ChannelError as e:
        return back(src, err=str(e))
    if rights not in RIGHTS:
        return back(src, err="Tell us why you can clip this channel.")
    if store.is_blocked(plat, name):
        return back(src, err="This streamer has asked not to be clipped on Virlo Studio.")
    p = settings.plan(user["plan"])
    have = store.one("SELECT COUNT(*) n FROM channels WHERE user_id=?", (user["id"],))["n"]
    if have >= p["channels"]:
        return back(src, err=f"Your {p['label']} plan has {p['channels']} channel(s). Upgrade to add more.")
    if store.one("SELECT 1 FROM channels WHERE user_id=? AND platform=? AND lower(channel)=lower(?)",
                 (user["id"], plat, name)):
        return back(src, err="You already added this channel.")
    now = time.time()
    store.x("INSERT INTO channels (user_id, platform, channel, style, rights_basis, rights_note, rights_ip, rights_at,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user["id"], plat, name, style if style in settings.STYLES else "default", rights, rights_note[:500],
             _ip(request), now, now))
    workspace.sync(user["id"])
    store.x("UPDATE users SET last_scan_at=NULL WHERE id=?", (user["id"],))
    worker.enqueue(user["id"])
    return back("/app", msg=f"Added {name}. We're scanning the latest streams now; first clips usually arrive in 20-40 minutes.")


@app.get("/app/channels", response_class=HTMLResponse)
def channels_page(request: Request, user: dict = Depends(need_user)):
    return page(request, "channels.html", user, channels=_channels(user["id"]), usage=usage_info(user))


def _own_channel(user: dict, chid: int) -> dict:
    ch = store.one("SELECT * FROM channels WHERE id=? AND user_id=?", (chid, user["id"]))
    if not ch:
        raise HTTPException(404)
    return ch


@app.post("/app/channels/{chid}/toggle")
def toggle_channel(chid: int, user: dict = Depends(need_user)):
    ch = _own_channel(user, chid)
    store.x("UPDATE channels SET paused=? WHERE id=?", (0 if ch["paused"] else 1, chid))
    workspace.sync(user["id"])
    return back("/app/channels", msg=f"{ch['channel']} {'resumed' if ch['paused'] else 'paused'}.")


@app.post("/app/channels/{chid}/style")
def style_channel(chid: int, user: dict = Depends(need_user), style: str = Form("default"), picky: str = Form("")):
    ch = _own_channel(user, chid)
    try:
        pk = min(9.0, max(4.0, float(picky))) if picky.strip() else None
    except ValueError:
        pk = None
    store.x("UPDATE channels SET style=?, picky=? WHERE id=?",
            (style if style in settings.STYLES else ch["style"], pk, chid))
    workspace.sync(user["id"])
    return back("/app/channels", msg="Style saved. It applies to new clips.")


@app.post("/app/channels/{chid}/delete")
def delete_channel(chid: int, user: dict = Depends(need_user)):
    ch = _own_channel(user, chid)
    store.x("DELETE FROM channels WHERE id=?", (chid,))
    workspace.sync(user["id"])
    return back("/app/channels", msg=f"Removed {ch['channel']}. Clips already made stay in your library.")


@app.post("/app/scan")
def scan_now(user: dict = Depends(need_user)):
    if not usage_info(user)["left"]:
        return back("/app", err="You've used all your clips this month. Upgrade for more.")
    store.x("UPDATE users SET last_scan_at=NULL WHERE id=?", (user["id"],))
    queued = worker.enqueue(user["id"])
    return back("/app", msg="Scan queued. New clips show up here as they're ready." if queued else "A scan is already running.")


@app.get("/app/clips", response_class=HTMLResponse)
def clips_page(request: Request, user: dict = Depends(need_user), status: str = "", q: str = ""):
    return page(request, "clips.html", user, clips=workspace.clips(user["id"], status, q.strip()[:80]),
                status=status, q=q, counts=workspace.counts(user["id"]))


def _own_clip(user: dict, cid: int) -> dict:
    c = workspace.clip(user["id"], cid)
    if not c:
        raise HTTPException(404)
    return c


@app.get("/app/clips/{cid}", response_class=HTMLResponse)
def clip_page(request: Request, cid: int, user: dict = Depends(need_user)):
    return page(request, "clip.html", user, c=_own_clip(user, cid))


@app.post("/app/clips/{cid}")
def clip_save(cid: int, user: dict = Depends(need_user), title: str = Form(""), caption: str = Form(""),
              hashtags: str = Form("")):
    _own_clip(user, cid)
    tags = [t.strip().lstrip("#") for t in hashtags.replace(",", " ").split() if t.strip().lstrip("#")][:15]
    workspace.update_text(user["id"], cid, title.strip(), caption.strip(), tags)
    return back(f"/app/clips/{cid}", msg="Saved.")


@app.post("/app/clips/{cid}/posted")
def clip_posted(cid: int, user: dict = Depends(need_user), url: str = Form("")):
    _own_clip(user, cid)
    links = " ".join(u for u in url.split() if u.startswith(("http://", "https://")))
    workspace.mark_posted(user["id"], cid, links)
    return back(f"/app/clips/{cid}", msg="Marked as posted." + (" We'll track its views." if links else ""))


@app.post("/app/clips/{cid}/delete")
def clip_delete(cid: int, user: dict = Depends(need_user)):
    _own_clip(user, cid)
    workspace.remove_clip(user["id"], cid)
    return back("/app/clips", msg="Clip deleted.")


@app.get("/app/clips/{cid}/video")
def clip_video(cid: int, user: dict = Depends(need_user), download: int = 0):
    c = _own_clip(user, cid)
    f = workspace.clip_file(user["id"], c)
    if not f:
        raise HTTPException(404)
    name = "".join(ch for ch in (c.get("hook") or f"clip {cid}") if ch.isalnum() or ch in " -_")[:60].strip() or "clip"
    return FileResponse(f, media_type="video/mp4", filename=f"{name}.mp4" if download else None)


@app.get("/app/clips/{cid}/thumb")
def clip_thumb(cid: int, user: dict = Depends(need_user)):
    c = _own_clip(user, cid)
    f = workspace.thumb_file(user["id"], c)
    if not f:
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg")


@app.get("/app/activity", response_class=HTMLResponse)
def activity_page(request: Request, user: dict = Depends(need_user)):
    jobs = store.q("SELECT * FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],))
    return page(request, "activity.html", user, items=workspace.activity(user["id"]), jobs=jobs)


@app.get("/app/account", response_class=HTMLResponse)
def account_page(request: Request, user: dict = Depends(need_user)):
    return page(request, "account.html", user, usage=usage_info(user))


@app.post("/app/account/delete")
def account_delete(request: Request, user: dict = Depends(need_user), confirm: str = Form("")):
    if confirm.strip().lower() != user["email"]:
        return back("/app/account", err="Type your email exactly to delete your account.")
    workspace.delete(user["id"])
    store.x("DELETE FROM users WHERE id=?", (user["id"],))
    resp = RedirectResponse("/?msg=" + quote("Your account and clips were deleted."), status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
