"""Scheduler + worker pool: queues a run for every user with work due and runs the engine per workspace.

- One job per user at a time; SAAS_MAX_PARALLEL_JOBS users in parallel.
- Quotas are checked before a run starts and passed into it, so a run never overshoots a plan.
- Failed runs retry with backoff (5 min, 20 min), then show as failed in the user's activity / admin.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import billing, settings, store, workspace

log = logging.getLogger("worker")
BACKOFF = [300, 1200]


def remaining(user: dict) -> tuple[dict, int, int]:
    """(limits, clips the user can still get, AI checks left this month)."""
    lim = settings.limits(user)
    a = billing.allowance(user)
    lim = {**lim, "max_clip_seconds": a["max_clip_seconds"]}
    return lim, a["clips"], max(0, lim["moments"] - store.usage(user["id"], "moment"))


def enqueue(user_id: int, delay: float = 0) -> int | None:
    """Queue a run unless one is already queued or running."""
    if store.one("SELECT 1 FROM jobs WHERE user_id=? AND status IN ('queued','running')", (user_id,)):
        return None
    now = time.time()
    return store.x("INSERT INTO jobs (user_id, run_after, created_at) VALUES (?,?,?)", (user_id, now + delay, now))


def schedule_due() -> int:
    """Queue users whose channels are due for a scan and who still have clips left."""
    cutoff = time.time() - settings.SCAN_MINUTES * 60
    # users with an active channel, or a pasted video still waiting (e.g. it was live when pasted)
    users = store.q("SELECT u.* FROM users u WHERE (u.last_scan_at IS NULL OR u.last_scan_at < ?) AND ("
                    "EXISTS (SELECT 1 FROM channels c WHERE c.user_id=u.id AND c.paused=0 AND c.vods=1) OR "
                    "EXISTS (SELECT 1 FROM links l WHERE l.user_id=u.id AND l.status IN ('queued','waiting')))",
                    (cutoff,))
    n = 0
    for u in users:
        _, clips_left, moments_left = remaining(u)
        if clips_left and moments_left and enqueue(u["id"]):
            n += 1
    return n


def claim() -> dict | None:
    with store._lock:
        job = store.one("SELECT * FROM jobs WHERE status='queued' AND run_after<=? ORDER BY run_after LIMIT 1",
                        (time.time(),))
        if not job:
            return None
        store.x("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE id=?", (time.time(), job["id"]))
        job["attempts"] += 1
        return job


def _cmd(ws: Path, clips: int, moments: int, scorer: str, keep_days: int) -> list[str]:
    return [sys.executable, "-m", "saas.run_tenant", "--workspace", str(ws), "--clips", str(clips),
            "--moments", str(moments), "--scorer", scorer, "--keep-days", str(keep_days)]


def record_links(results: dict) -> None:
    """Store what happened to each pasted video in this run (see run_tenant.clip_links)."""
    now = time.time()
    for lid, r in results.items():
        status = r.get("status") if r.get("status") in ("done", "waiting", "failed") else "failed"
        store.x("UPDATE links SET status=?, found=?, error=?, updated_at=? WHERE id=?",
                (status, r.get("found"), r.get("error"), now, int(lid)))


def run_job(job: dict) -> None:
    uid = job["user_id"]
    user = store.one("SELECT * FROM users WHERE id=?", (uid,))
    try:
        if not user:
            raise RuntimeError("user deleted")
        plan, clips_left, moments_left = remaining(user)
        active = workspace.sync(uid, max_clip_seconds=plan.get("max_clip_seconds"))
        if (not active and not workspace.pending_links(uid)) or not clips_left or not moments_left:
            store.x("UPDATE jobs SET status='done', finished_at=?, result=? WHERE id=?",
                    (time.time(), json.dumps({"skipped": "nothing to do or limit reached"}), job["id"]))
            store.x("UPDATE users SET last_scan_at=? WHERE id=?", (time.time(), uid))
            return
        ws = workspace.root(uid)
        log_path = ws / "last_run.log"
        with open(log_path, "w", encoding="utf-8", errors="replace") as errf:
            p = subprocess.run(_cmd(ws, clips_left, moments_left, plan["scorer"], plan["history_days"]),
                               stdout=subprocess.PIPE, stderr=errf, text=True, timeout=settings.JOB_TIMEOUT_SEC,
                               cwd=str(settings.REPO), env={**os.environ, "PYTHONUNBUFFERED": "1"})
        if p.returncode != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            raise RuntimeError(f"engine exited {p.returncode}: {tail}")
        summary = json.loads(p.stdout.strip().splitlines()[-1])
        record_links(summary.get("links") or {})
        store.add_usage(uid, "clip", summary.get("rendered", []))
        billing.record(uid, summary.get("rendered", []))
        billing.settle_due(uid)
        store.add_usage(uid, "moment", summary.get("checked_ids", []))
        store.x("UPDATE jobs SET status='done', finished_at=?, result=?, error=NULL WHERE id=?",
                (time.time(), json.dumps({k: v for k, v in summary.items() if k != "checked_ids"}), job["id"]))
        store.x("UPDATE users SET last_scan_at=? WHERE id=?", (time.time(), uid))
        log.info("user %s: %s new moments, %s checked, %s clips", uid, summary["found"], summary["checked"],
                 len(summary["rendered"]))
    except Exception as e:
        msg = str(e)[:1000]
        if job["attempts"] < settings.MAX_ATTEMPTS:
            delay = BACKOFF[min(job["attempts"] - 1, len(BACKOFF) - 1)]
            store.x("UPDATE jobs SET status='queued', run_after=?, error=? WHERE id=?", (time.time() + delay, msg, job["id"]))
            log.warning("user %s run failed (attempt %s), retry in %ss: %s", uid, job["attempts"], delay, msg[:200])
        else:
            store.x("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?", (time.time(), msg, job["id"]))
            store.x("UPDATE users SET last_scan_at=? WHERE id=?", (time.time(), uid))
            log.error("user %s run failed for good: %s", uid, msg[:300])


def loop(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    store.conn()  # also re-queues jobs left 'running' by a crash
    running: list[threading.Thread] = []
    log.info("worker started: %s parallel runs, scan every %s min", settings.MAX_PARALLEL_JOBS, settings.SCAN_MINUTES)
    while not stop.is_set():
        try:
            billing.settle_due()
            schedule_due()
            running = [t for t in running if t.is_alive()]
            while len(running) < settings.MAX_PARALLEL_JOBS:
                job = claim()
                if not job:
                    break
                t = threading.Thread(target=run_job, args=(job,), daemon=True, name=f"job-{job['id']}")
                t.start()
                running.append(t)
        except Exception:
            log.exception("worker loop error")
        stop.wait(15)
