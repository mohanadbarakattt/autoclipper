"""Account database (users, sessions, channels, jobs, usage). Each user's clips live in their own engine DB."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone

from . import settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_path = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    is_admin INTEGER NOT NULL DEFAULT 0,
    last_scan_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    channel TEXT NOT NULL,
    style TEXT NOT NULL DEFAULT 'default',
    vods INTEGER NOT NULL DEFAULT 1,
    live INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    picky REAL,
    rights_basis TEXT NOT NULL,
    rights_note TEXT,
    rights_ip TEXT,
    rights_at REAL NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (user_id, platform, channel)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after REAL NOT NULL,
    started_at REAL, finished_at REAL,
    result TEXT, error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, run_after);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- clip | moment
    ref TEXT NOT NULL,           -- engine candidate id
    at REAL NOT NULL,
    PRIMARY KEY (user_id, kind, ref)
);
CREATE TABLE IF NOT EXISTS charges (
    user_id INTEGER NOT NULL,
    clip_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | paid | insufficient | error
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_at REAL NOT NULL,
    error TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (user_id, clip_id)
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    url TEXT NOT NULL,
    video_id TEXT NOT NULL,
    rights_basis TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | waiting (still live) | done | failed
    found INTEGER,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blocked_channels (
    platform TEXT NOT NULL,
    channel TEXT NOT NULL,
    reason TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (platform, channel)
);
"""


def conn() -> sqlite3.Connection:
    global _conn, _path
    with _lock:
        if _conn is None or _path != settings.DB_PATH:
            _path = settings.DB_PATH
            _conn = sqlite3.connect(_path, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            have = {r[1] for r in _conn.execute("PRAGMA table_info(users)")}
            for col in ("ext_id", "tier"):  # Virlo Studio link (added after M1)
                if col not in have:
                    _conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            _conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_ext ON users(ext_id)")
            # a worker that died mid-job: put its jobs back in the queue
            _conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            _conn.commit()
        return _conn


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql: str, args: tuple = ()) -> int:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid


def period_start(now: float | None = None) -> float:
    """Start of the current usage period (calendar month, UTC). Billing periods replace this in M2."""
    d = datetime.fromtimestamp(now or time.time(), timezone.utc)
    return datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp()


def usage(user_id: int, kind: str) -> int:
    return one("SELECT COUNT(*) n FROM usage WHERE user_id=? AND kind=? AND at>=?",
               (user_id, kind, period_start()))["n"]


def add_usage(user_id: int, kind: str, refs: list) -> None:
    now = time.time()
    with _lock:
        conn().executemany("INSERT OR IGNORE INTO usage (user_id, kind, ref, at) VALUES (?,?,?,?)",
                           [(user_id, kind, str(r), now) for r in refs])
        conn().commit()


def is_blocked(platform: str, channel: str) -> bool:
    return bool(one("SELECT 1 FROM blocked_channels WHERE platform=? AND lower(channel)=lower(?)", (platform, channel)))
