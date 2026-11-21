"""Email + password accounts and cookie sessions (stdlib only: scrypt hashes, random tokens stored hashed)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque

from . import settings, store

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$%s$%s" % (base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt_b64), n=_N, r=_R, p=_P, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


class AuthError(ValueError):
    pass


def signup(email: str, password: str) -> int:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise AuthError("Enter a valid email address.")
    if len(password or "") < 8:
        raise AuthError("Use at least 8 characters for your password.")
    if store.one("SELECT 1 FROM users WHERE email=?", (email,)):
        raise AuthError("That email already has an account. Log in instead.")
    return store.x("INSERT INTO users (email, pw_hash, created_at) VALUES (?,?,?)",
                   (email, hash_password(password), time.time()))


def login(email: str, password: str) -> int:
    u = store.one("SELECT id, pw_hash FROM users WHERE email=?", ((email or "").strip().lower(),))
    if not u or not verify_password(password or "", u["pw_hash"]):
        raise AuthError("Wrong email or password.")
    return u["id"]


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    store.x("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (_h(token), user_id, now, now + settings.SESSION_DAYS * 86400))
    store.x("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    return store.one("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                     (_h(token), time.time()))


def end_session(token: str | None) -> None:
    if token:
        store.x("DELETE FROM sessions WHERE token_hash=?", (_h(token),))


class RateLimiter:
    """At most `n` attempts per `window` seconds per key (in memory; enough for one server)."""

    def __init__(self, n: int = 10, window: int = 600):
        self.n, self.window = n, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, dq = time.time(), self.hits[key]
        while dq and dq[0] < now - self.window:
            dq.popleft()
        if len(dq) >= self.n:
            return False
        dq.append(now)
        return True
