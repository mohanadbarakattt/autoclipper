"""Service API for Virlo Studio: bearer auth, per-user isolation, signed media, points billing."""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from saas import api, billing, settings, store, worker, workspace
from tests.test_saas import fake_clip  # noqa: F401  (reuses the autouse tmp_data fixture below)
from tests.test_saas import tmp_data  # noqa: F401

TOKEN = "test-service-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def service_env(monkeypatch):
    monkeypatch.setattr(settings, "SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "MEDIA_SECRET", "media-secret")
    monkeypatch.setattr(settings, "PUBLIC_URL", "")
    monkeypatch.setattr(settings, "VIRLO_URL", "https://virlo.test")


@pytest.fixture
def client():
    from saas.web import app
    return TestClient(app)


def make_user(client, uid="u-1", tier="creator"):
    r = client.put(f"/v1/users/{uid}", json={"tier": tier}, headers=H)
    assert r.status_code == 200, r.text
    return store.one("SELECT * FROM users WHERE ext_id=?", (uid,))


class FakeVirlo:
    """Stands in for virlo.studio's /api/internal/clips/* endpoints."""

    def __init__(self, balance: int, per_clip: int = 20):
        self.balance, self.per_clip, self.charged = balance, per_clip, {}

    def __call__(self, url, json=None, headers=None, timeout=None):
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        req = httpx.Request("POST", url)
        if url.endswith("/allowance"):
            return httpx.Response(200, json={"clips": self.balance // self.per_clip}, request=req)
        key = json["idempotencyKey"]
        if key in self.charged:
            return httpx.Response(200, json={"ok": True}, request=req)
        if self.balance < self.per_clip:
            return httpx.Response(402, json={"error": "insufficient_credits"}, request=req)
        self.balance -= self.per_clip
        self.charged[key] = True
        return httpx.Response(200, json={"ok": True}, request=req)


def test_requires_service_token(client):
    assert client.put("/v1/users/u-1", json={"tier": "free"}).status_code == 401
    assert client.put("/v1/users/u-1", json={"tier": "free"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/v1/users/bad id!/overview", headers=H).status_code in (400, 404)


def test_user_channels_and_tier_limits(client, monkeypatch):
    monkeypatch.setattr(worker, "enqueue", lambda *a, **k: 1)
    make_user(client, "u-free", "free")
    ok = client.post("/v1/users/u-free/channels", headers=H,
                     json={"link": "https://kick.com/adinross", "style": "gaming", "rights": "campaign"})
    assert ok.status_code == 200 and ok.json()["channel"]["channel"] == "adinross"
    second = client.post("/v1/users/u-free/channels", headers=H,
                         json={"link": "https://twitch.tv/other", "style": "gaming", "rights": "own"})
    assert second.status_code == 400 and second.json()["error"] == "channel_rejected"
    chid = ok.json()["channel"]["id"]
    r = client.patch(f"/v1/users/u-free/channels/{chid}", headers=H, json={"picky": 7.5, "paused": True})
    assert r.json()["channel"]["picky"] == 7.5 and r.json()["channel"]["paused"] is True
    ov = client.get("/v1/users/u-free/overview", headers=H).json()
    assert ov["limits"]["name"] == "free" and ov["limits"]["scorer"] == "gemini" and len(ov["channels"]) == 1


def test_users_are_isolated(client):
    a = make_user(client, "alice")
    make_user(client, "bob")
    fake_clip(a["id"], 5)
    assert client.get("/v1/users/alice/clips/5", headers=H).status_code == 200
    assert client.get("/v1/users/bob/clips/5", headers=H).status_code == 404
    assert client.get("/v1/users/bob/clips", headers=H).json()["clips"] == []
    assert client.patch("/v1/users/bob/channels/1", headers=H, json={}).status_code == 404


def test_signed_media_links(client):
    a = make_user(client, "media")
    fake_clip(a["id"], 2)
    clip = client.get("/v1/users/media/clips/2", headers=H).json()["clip"]
    assert clip["videoUrl"] and clip["downloadUrl"] and not clip["locked"]
    assert client.get(clip["videoUrl"]).status_code == 200
    assert "attachment" in client.get(clip["downloadUrl"]).headers["content-disposition"]
    tampered = clip["videoUrl"].replace("/2/video", "/3/video")
    assert client.get(tampered).status_code == 403
    exp = int(time.time()) - 10
    expired = f"/media/{a['id']}/2/video?exp={exp}&sig={api._sig(a['id'], 2, 'video', exp)}"
    assert client.get(expired).status_code == 403


def test_points_allowance_charge_and_lock(client, monkeypatch):
    virlo = FakeVirlo(balance=30)  # enough for one 20-point clip
    monkeypatch.setattr(httpx, "post", virlo)
    u = make_user(client, "payer")
    assert billing.clips_allowed(u) == 1
    fake_clip(u["id"], 1)
    fake_clip(u["id"], 2)
    billing.record(u["id"], [1, 2])
    assert billing.settle_due(u["id"]) == 1
    assert billing.unlocked(u["id"], 1) and not billing.unlocked(u["id"], 2)
    locked = client.get("/v1/users/payer/clips/2", headers=H).json()["clip"]
    assert locked["locked"] and locked["videoUrl"] is None
    # user tops up; the retry pays and unlocks it, and replays never double-charge
    virlo.balance += 100
    store.x("UPDATE charges SET next_try_at=0")
    assert billing.settle_due(u["id"]) == 1
    assert billing.unlocked(u["id"], 2) and len(virlo.charged) == 2
    store.x("UPDATE charges SET status='pending', next_try_at=0 WHERE clip_id=1")
    billing.settle_due(u["id"])
    assert len(virlo.charged) == 2 and virlo.balance == 130 - 40


def test_allowance_fails_closed(client, monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("virlo down")
    monkeypatch.setattr(httpx, "post", down)
    u = make_user(client, "offline")
    assert billing.clips_allowed(u) == 0


def test_service_mode_hides_standalone_pages(client, monkeypatch):
    monkeypatch.setattr(settings, "SAAS_MODE", "service")
    assert client.get("/signup").status_code == 404
    assert client.get("/health").status_code == 200
    assert client.put("/v1/users/x", json={"tier": "free"}, headers=H).status_code == 200
