"""Virlo Studio: accounts, workspace isolation, quotas, channel parsing, CSRF guard.

Run: python -m pytest tests -q
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from saas import auth, settings, store, worker, workspace


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA", tmp_path)
    monkeypatch.setattr(settings, "WORKSPACES", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "saas.db")
    (tmp_path / "workspaces").mkdir()
    monkeypatch.setattr(worker, "enqueue", lambda *a, **k: None)  # web tests don't start runs
    from saas import web
    monkeypatch.setattr(web, "signup_limit", auth.RateLimiter(100, 3600))
    monkeypatch.setattr(web, "login_limit", auth.RateLimiter(100, 600))
    yield tmp_path


@pytest.fixture
def client():
    from saas.web import app
    return TestClient(app)


def signup(client, email, pw="password123"):
    r = client.post("/signup", data={"email": email, "password": pw, "terms": "on"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers.get("location")
    return r


def test_signup_rate_limit(client, monkeypatch):
    from saas import web
    monkeypatch.setattr(web, "signup_limit", auth.RateLimiter(1, 3600))
    signup(client, "first@example.com")
    r = client.post("/signup", data={"email": "second@example.com", "password": "password123", "terms": "on"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]


def add_channel(client, link="https://twitch.tv/SomeStreamer", rights="own"):
    return client.post("/app/channels", data={"link": link, "style": "gaming", "rights": rights}, follow_redirects=False)


def fake_clip(user_id: int, cid: int = 1, inside: bool = True) -> None:
    """A finished clip in the user's engine DB, as the engine would leave it."""
    ws = workspace.root(user_id)
    (ws / "data" / "state").mkdir(parents=True, exist_ok=True)
    (ws / "data" / "ready_clips").mkdir(parents=True, exist_ok=True)
    video = (ws / "data" / "ready_clips" / f"clip_{cid}.mp4") if inside else (settings.DATA / "elsewhere.mp4")
    video.write_bytes(b"\x00" * 64)
    c = sqlite3.connect(ws / "data" / "state" / "clipper.db")
    c.execute("CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY, streamer_id TEXT, platform TEXT, profile TEXT,"
              " source_type TEXT, source_ref TEXT, source_offset REAL, trigger TEXT, raw_path TEXT, status TEXT,"
              " gemini_json TEXT, score_json TEXT, viral_score REAL, hook TEXT, title TEXT, caption TEXT, out_path TEXT,"
              " error TEXT, trypost_post_ids TEXT, scheduled_at TEXT, created_at REAL, updated_at REAL, post_url TEXT,"
              " posted_at REAL, views INTEGER, views_checked_at REAL)")
    c.execute("INSERT INTO candidates (id, streamer_id, status, viral_score, hook, caption, score_json, out_path, created_at,"
              " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (cid, "twitch:somestreamer", "ready", 8.1, "He did WHAT", "caption", json.dumps({"hashtags": ["gta6"]}),
               str(video), time.time(), time.time()))
    c.commit()
    c.close()


# ---------- units ----------
def test_password_hashing():
    h = auth.hash_password("hunter22!")
    assert auth.verify_password("hunter22!", h)
    assert not auth.verify_password("nope", h)
    assert not auth.verify_password("x", "garbage")


@pytest.mark.parametrize("text,hint,expected", [
    ("https://www.twitch.tv/KaiCenat", "", ("twitch", "kaicenat")),
    ("twitch.tv/kaicenat/videos", "", ("twitch", "kaicenat")),
    ("https://kick.com/adinross", "", ("kick", "adinross")),
    ("https://www.youtube.com/@MrBeast", "", ("youtube", "MrBeast")),
    ("https://youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA", "", ("youtube", "UCX6OQ3DkcsbYNE6H8uQQuVA")),
    ("@xqc", "kick", ("kick", "xqc")),
])
def test_parse_channel(text, hint, expected):
    assert workspace.parse_channel(text, hint) == expected


@pytest.mark.parametrize("text,hint", [("", ""), ("https://example.com/x", ""), ("justaname", ""),
                                       ("https://twitch.tv/<script>", "")])
def test_parse_channel_rejects(text, hint):
    with pytest.raises(workspace.ChannelError):
        workspace.parse_channel(text, hint)


def test_plan_limits_and_quota():
    uid = auth.signup("q@example.com", "password123")
    u = store.one("SELECT * FROM users WHERE id=?", (uid,))
    p, clips_left, moments_left = worker.remaining(u)
    assert p["name"] == "free" and clips_left == 10 and moments_left == 150
    store.add_usage(uid, "clip", list(range(10)))
    store.add_usage(uid, "clip", [1, 2])  # duplicates don't double count
    _, clips_left, _ = worker.remaining(u)
    assert clips_left == 0


def test_schedule_skips_users_out_of_clips(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "enqueue", lambda uid, delay=0: calls.append(uid) or 1)
    a = auth.signup("a@example.com", "password123")
    b = auth.signup("b@example.com", "password123")
    for uid in (a, b):
        store.x("INSERT INTO channels (user_id, platform, channel, rights_basis, rights_at, created_at) VALUES (?,?,?,?,?,?)",
                (uid, "twitch", f"ch{uid}", "own", time.time(), time.time()))
    store.add_usage(b, "clip", list(range(10)))
    worker.schedule_due()
    assert calls == [a]


def test_sync_writes_engine_config():
    uid = auth.signup("s@example.com", "password123")
    store.x("INSERT INTO channels (user_id, platform, channel, style, picky, rights_basis, rights_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)", (uid, "kick", "adinross", "gta6", 7.0, "own", time.time(), time.time()))
    active = workspace.sync(uid)
    ws = workspace.root(uid)
    s = json.loads((ws / "config" / "streamers.json").read_text())["streamers"]
    assert s == [{"platform": "kick", "channel": "adinross", "profile": f"c{active[0]['id']}", "live": False,
                  "vods": True, "paused": False}]
    prof = json.loads((ws / "config" / "profiles" / f"c{active[0]['id']}.json").read_text())
    assert prof["min_viral_score"] == 7.0 and prof["publish"]["mode"] == "off"
    assert prof["auto_discover"]["enabled"] is False


def test_blocked_channel_is_paused_in_workspace():
    uid = auth.signup("blk@example.com", "password123")
    store.x("INSERT INTO channels (user_id, platform, channel, rights_basis, rights_at, created_at) VALUES (?,?,?,?,?,?)",
            (uid, "twitch", "optedout", "own", time.time(), time.time()))
    store.x("INSERT INTO blocked_channels (platform, channel, created_at) VALUES ('twitch','optedout',?)", (time.time(),))
    assert workspace.sync(uid) == []
    s = json.loads((workspace.root(uid) / "config" / "streamers.json").read_text())["streamers"]
    assert s[0]["paused"] is True


def test_clip_file_must_stay_inside_workspace():
    uid = auth.signup("p@example.com", "password123")
    fake_clip(uid, 1, inside=False)
    assert workspace.clip_file(uid, workspace.clip(uid, 1)) is None


# ---------- web ----------
def test_signup_onboarding_and_dashboard(client):
    r = signup(client, "new@example.com")
    assert r.headers["location"] == "/app/onboarding"
    assert client.get("/app", follow_redirects=False).headers["location"] == "/app/onboarding"
    r = add_channel(client)
    assert r.status_code == 303 and r.headers["location"].startswith("/app?msg=")
    page = client.get("/app")
    assert page.status_code == 200 and "somestreamer" in page.text


def test_login_logout(client):
    signup(client, "li@example.com")
    client.post("/logout")
    assert client.get("/app", follow_redirects=False).headers["location"].startswith("/login")
    bad = client.post("/login", data={"email": "li@example.com", "password": "wrong-pass"}, follow_redirects=False)
    assert "err=" in bad.headers["location"]
    ok = client.post("/login", data={"email": "li@example.com", "password": "password123"}, follow_redirects=False)
    assert ok.headers["location"] == "/app"


def test_free_plan_channel_limit_and_blocklist(client):
    signup(client, "lim@example.com")
    assert "msg=" in add_channel(client).headers["location"]
    assert "Upgrade" in client.get(add_channel(client, "https://kick.com/other").headers["location"]).text
    store.x("INSERT INTO blocked_channels (platform, channel, created_at) VALUES ('kick','nope',?)", (time.time(),))
    store.x("UPDATE users SET plan='pro' WHERE email='lim@example.com'")
    r = client.get(add_channel(client, "https://kick.com/nope").headers["location"])
    assert "asked not to be clipped" in r.text


def test_users_only_see_their_own_clips(tmp_data):
    from saas.web import app
    alice, bob = TestClient(app), TestClient(app)
    signup(alice, "alice@example.com")
    signup(bob, "bob@example.com")
    aid = store.one("SELECT id FROM users WHERE email='alice@example.com'")["id"]
    fake_clip(aid, 7)
    assert alice.get("/app/clips/7").status_code == 200
    assert alice.get("/app/clips/7/video").status_code == 200
    assert bob.get("/app/clips/7").status_code == 404
    assert bob.get("/app/clips/7/video").status_code == 404
    assert "He did WHAT" in alice.get("/app/clips").text
    assert "He did WHAT" not in bob.get("/app/clips").text


def test_mark_posted_and_edit(client):
    signup(client, "post@example.com")
    uid = store.one("SELECT id FROM users WHERE email='post@example.com'")["id"]
    fake_clip(uid, 3)
    client.post("/app/clips/3", data={"title": "T", "caption": "New caption", "hashtags": "#a b,c"})
    c = workspace.clip(uid, 3)
    assert c["caption"] == "New caption" and c["hashtags"] == ["a", "b", "c"]
    client.post("/app/clips/3/posted", data={"url": "https://www.tiktok.com/@me/video/1 javascript:alert(1)"})
    c = workspace.clip(uid, 3)
    assert c["status"] == "posted" and c["post_url"] == "https://www.tiktok.com/@me/video/1"


def test_cross_site_post_blocked(client):
    r = client.post("/signup", data={"email": "x@example.com", "password": "password123", "terms": "on"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_loud_windows_survive_muted_vod_stretches():
    """Twitch mutes parts of VODs (-180 dBFS); that silence must not hide the real loud moments."""
    import numpy as np

    from clipper.media import loud_windows
    rng = np.random.default_rng(0)
    db = -30 + rng.normal(0, 2, 6000)
    db[1000:2500] = -180.0          # muted stretch
    db[4000:4010] = -8.0            # a real hype moment
    picks = loud_windows(db, window=90, top_n=3)
    assert picks and any(s <= 4000 <= e for s, e, _ in picks)
