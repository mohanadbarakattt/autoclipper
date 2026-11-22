"""Guardrails: daily budgets + storage limits so AutoClipper can run unattended without filling the disk,
burning through the free Gemini quota, your Claude plan, or your internet.

Every limit is a .env setting (0 = no limit). Usage resets at midnight (this PC's time).

    MAX_CLIPS_PER_DAY=15            finished clips kept per day; when reached, the queue waits until tomorrow
    MAX_AI_CHECKS_PER_DAY=200       Gemini calls per day (free tier allows a few hundred)
    MAX_CLAUDE_CHECKS_PER_DAY=25    Claude final checks per day (protects your plan's limit)
    MAX_DOWNLOAD_GB_PER_DAY=2       video/audio downloaded per day
    MAX_STORAGE_GB=20               size of the data folder; over it, old files are cleaned and new work pauses
    MIN_FREE_DISK_GB=15             always leave this much free on the disk
    MAX_PENDING=40                  candidates waiting in the queue; extra ones are skipped
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from . import db
from .config import CANDIDATES_DIR, DATA_DIR, READY_DIR, RING_DIR, WORK_DIR

log = logging.getLogger("guard")

LIMITS = {  # key: (env var, default, label, unit)
    "clips": ("MAX_CLIPS_PER_DAY", 15, "Clips kept", ""),
    "gemini": ("MAX_AI_CHECKS_PER_DAY", 200, "Gemini calls", ""),
    "claude": ("MAX_CLAUDE_CHECKS_PER_DAY", 25, "Claude checks", ""),
    "download_gb": ("MAX_DOWNLOAD_GB_PER_DAY", 2, "Downloaded", "GB"),
}
_warned: dict[str, str] = {}

SCHEMA = "CREATE TABLE IF NOT EXISTS usage (day TEXT, key TEXT, n REAL DEFAULT 0, PRIMARY KEY (day, key))"


def _num(env: str, default: float) -> float:
    try:
        return float(os.getenv(env, "").strip() or default)
    except ValueError:
        return float(default)


def today() -> str:
    return time.strftime("%Y-%m-%d")


def _ensure() -> None:
    if not getattr(_ensure, "done", False):
        db.x(SCHEMA)
        _ensure.done = True


def add(key: str, amount: float = 1.0) -> None:
    _ensure()
    db.x("INSERT INTO usage (day, key, n) VALUES (?,?,?) ON CONFLICT(day, key) DO UPDATE SET n = n + excluded.n",
         (today(), key, float(amount)))


def used(key: str) -> float:
    _ensure()
    r = db.q("SELECT n FROM usage WHERE day=? AND key=?", (today(), key))
    return float(r[0]["n"]) if r else 0.0


def limit(key: str) -> float:
    env, default, _, _ = LIMITS[key]
    return _num(env, default)


def ok(key: str, need: float | None = None) -> bool:
    """True if there's room for `need` more today (default: one more item; for GB: any room left). 0 = unlimited."""
    lim = limit(key)
    if need is None:
        need = 0.0 if LIMITS[key][3] == "GB" else 1.0
    u = used(key)
    if lim <= 0 or (u + need <= lim + 1e-9 if need else u < lim):
        return True
    if _warned.get(key) != today():
        _warned[key] = today()
        log.warning("daily limit reached: %s %.1f/%g - waiting until tomorrow (raise %s in .env to allow more)",
                    LIMITS[key][2], used(key), lim, LIMITS[key][0])
    return False


def add_download(path: str | Path | None) -> None:
    try:
        if path and Path(path).exists():
            add("download_gb", Path(path).stat().st_size / 1e9)
    except OSError:
        pass


# ---------------- storage ----------------
def _dir_gb(p: Path) -> float:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total / 1e9


def data_gb() -> float:
    return sum(_dir_gb(d) for d in (READY_DIR, CANDIDATES_DIR, RING_DIR, WORK_DIR) if d.exists())


def free_gb() -> float:
    return shutil.disk_usage(DATA_DIR).free / 1e9


def storage_ok() -> bool:
    max_gb, min_free = _num("MAX_STORAGE_GB", 20), _num("MIN_FREE_DISK_GB", 15)
    over = (max_gb > 0 and data_gb() > max_gb) or (min_free > 0 and free_gb() < min_free)
    if over and _warned.get("storage") != today():
        _warned["storage"] = today()
        log.warning("storage guard: data %.1f GB (max %g), %.1f GB free (min %g) - pausing new downloads",
                    data_gb(), max_gb, free_gb(), min_free)
    return not over


def enforce_storage() -> float:
    """Over the limit? Free space in a safe order. Never deletes clips that are ready but not yet posted.
    1) rejected/failed leftovers  2) posted clips, oldest first (copies stay in Google Drive)
    3) the oldest waiting candidates. Returns GB freed."""
    if storage_ok():
        return 0.0
    freed = 0.0

    def rm(p: str | Path | None) -> None:
        nonlocal freed
        for q in ([Path(p), Path(p).with_suffix(".jpg")] if p else []):
            try:
                if q.exists():
                    freed += q.stat().st_size / 1e9
                    q.unlink()
            except OSError:
                pass

    for r in db.q("SELECT raw_path, out_path FROM candidates WHERE status IN ('rejected','failed')"):
        rm(r["raw_path"]); rm(r["out_path"])
    for r in db.q("SELECT id, out_path FROM candidates WHERE status='posted' AND out_path IS NOT NULL ORDER BY posted_at"):
        if storage_ok():
            break
        rm(r["out_path"])
    for r in db.q("SELECT id, raw_path FROM candidates WHERE status='pending' ORDER BY created_at"):
        if storage_ok():
            break
        rm(r["raw_path"])
        db.update_candidate(r["id"], status="rejected", error="storage guard: skipped to stay under MAX_STORAGE_GB")
    if freed:
        log.info("storage guard freed %.1f GB", freed)
    return freed


# ---------------- gates used by ingest / pipeline ----------------
def can_add_candidate() -> bool:
    """Before downloading or cutting new footage."""
    max_pending = int(_num("MAX_PENDING", 40))
    if max_pending > 0:
        n = db.q("SELECT COUNT(*) n FROM candidates WHERE status='pending'")[0]["n"]
        if n >= max_pending:
            if _warned.get("pending") != today():
                _warned["pending"] = today()
                log.warning("queue full (%d waiting, MAX_PENDING=%d) - skipping new moments for now", n, max_pending)
            return False
    return ok("download_gb") and storage_ok()


def can_process() -> bool:
    """Before the pipeline starts on the next candidate."""
    return ok("clips") and ok("gemini")


def status() -> dict:
    out = {}
    for key, (env, default, label, unit) in LIMITS.items():
        out[key] = {"label": label, "used": round(used(key), 2), "limit": limit(key), "unit": unit, "env": env}
    out["storage"] = {"label": "Data folder", "used": round(data_gb(), 2), "limit": _num("MAX_STORAGE_GB", 20),
                      "unit": "GB", "env": "MAX_STORAGE_GB"}
    out["free_disk"] = {"label": "Free disk", "used": round(free_gb(), 1), "limit": _num("MIN_FREE_DISK_GB", 15),
                        "unit": "GB", "env": "MIN_FREE_DISK_GB", "min": True}
    pending = db.q("SELECT COUNT(*) n FROM candidates WHERE status='pending'")[0]["n"]
    out["pending"] = {"label": "Waiting in queue", "used": pending, "limit": _num("MAX_PENDING", 40), "unit": "",
                      "env": "MAX_PENDING"}
    out["paused"] = [v["label"] for k, v in out.items() if isinstance(v, dict) and v.get("limit") and (
        (v["used"] < v["limit"]) if v.get("min") else (v["used"] >= v["limit"]))]
    return out
