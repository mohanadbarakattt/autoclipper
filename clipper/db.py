"""SQLite state shared by ingest, processor and dashboard."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS stream_status (
    streamer_id TEXT PRIMARY KEY,
    platform TEXT, channel TEXT,
    is_live INTEGER DEFAULT 0, recording INTEGER DEFAULT 0,
    title TEXT, category TEXT,
    mps REAL DEFAULT 0, window_score REAL DEFAULT 0, baseline REAL DEFAULT 0,
    last_spike REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    streamer_id TEXT, platform TEXT, profile TEXT,
    source_type TEXT, source_ref TEXT, source_offset REAL DEFAULT 0,
    trigger TEXT, raw_path TEXT,
    status TEXT DEFAULT 'pending',
    gemini_json TEXT, score_json TEXT,
    viral_score REAL, hook TEXT, title TEXT, caption TEXT,
    out_path TEXT, error TEXT,
    trypost_post_ids TEXT, scheduled_at TEXT,
    created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status);
CREATE TABLE IF NOT EXISTS seen_vods (
    vod_key TEXT PRIMARY KEY, streamer_id TEXT, seen_at REAL
);
"""

# pending -> analyzing -> rejected | rendering -> ready | scheduled | publish_failed | failed


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            # anything stuck mid-processing after a crash goes back to the queue
            _conn.execute("UPDATE candidates SET status='pending' WHERE status IN ('analyzing','rendering')")
            _conn.commit()
        return _conn


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def x(sql: str, args: tuple = ()) -> int:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid


def upsert_status(streamer_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    with _lock:
        exists = conn().execute("SELECT 1 FROM stream_status WHERE streamer_id=?", (streamer_id,)).fetchone()
        if exists:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn().execute(f"UPDATE stream_status SET {sets} WHERE streamer_id=?", (*fields.values(), streamer_id))
        else:
            cols = ", ".join(["streamer_id", *fields])
            marks = ", ".join("?" * (len(fields) + 1))
            conn().execute(f"INSERT INTO stream_status ({cols}) VALUES ({marks})", (streamer_id, *fields.values()))
        conn().commit()


def add_candidate(streamer: dict, source_type: str, source_ref: str, raw_path: str,
                  trigger: str, source_offset: float = 0.0) -> int:
    now = time.time()
    return x(
        "INSERT INTO candidates (streamer_id, platform, profile, source_type, source_ref, source_offset,"
        " trigger, raw_path, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, 'pending', ?, ?)",
        (streamer["id"], streamer["platform"], streamer["profile"], source_type, source_ref,
         source_offset, trigger, raw_path, now, now),
    )


def update_candidate(cid: int, **fields) -> None:
    for k, v in list(fields.items()):
        if isinstance(v, (dict, list)):
            fields[k] = json.dumps(v)
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    x(f"UPDATE candidates SET {sets} WHERE id=?", (*fields.values(), cid))


def claim_next_pending() -> dict | None:
    with _lock:
        row = conn().execute(
            "SELECT * FROM candidates WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        conn().execute("UPDATE candidates SET status='analyzing', updated_at=? WHERE id=?", (time.time(), row["id"]))
        conn().commit()
        return dict(row)


def vod_seen(key: str) -> bool:
    return bool(q("SELECT 1 FROM seen_vods WHERE vod_key=?", (key,)))


def mark_vod_seen(key: str, streamer_id: str) -> None:
    x("INSERT OR IGNORE INTO seen_vods (vod_key, streamer_id, seen_at) VALUES (?,?,?)", (key, streamer_id, time.time()))
