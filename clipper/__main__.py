"""Run everything in one process: live workers, VOD poller, processor, cleanup, dashboard."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import uvicorn

from . import db
from .config import (CANDIDATES_DIR, DASHBOARD_PORT, KEEP_PUBLISHED_DAYS, READY_DIR, VOD_POLL_MINUTES, WORK_DIR,
                     load_streamers)
from .dashboard.app import EVENTS, app
from .ingest import vod
from .ingest.live import LiveWorker
from .processor import pipeline

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("main")
for _noisy in ("httpx", "httpx2", "google_genai", "google_genai.models"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


async def live_supervisor() -> None:
    workers: dict[str, tuple[LiveWorker, asyncio.Task]] = {}
    while True:
        wanted = {s["id"]: s for s in load_streamers() if s.get("live", True)}
        for sid, s in wanted.items():
            if sid not in workers or workers[sid][1].done():
                w = LiveWorker(s)
                workers[sid] = (w, asyncio.create_task(w.run()))
        for sid in list(workers):
            if sid not in wanted:
                workers[sid][0].stop()
                del workers[sid]
        await asyncio.sleep(30)


async def vod_loop() -> None:
    EVENTS["vod_scan"] = evt = asyncio.Event()
    await asyncio.sleep(20)
    while True:
        log.info("VOD scan")
        await asyncio.to_thread(vod.poll_all)
        evt.clear()
        try:
            await asyncio.wait_for(evt.wait(), timeout=VOD_POLL_MINUTES * 60)
        except asyncio.TimeoutError:
            pass


async def processor_loop() -> None:
    from .processor import export
    last_backfill = 0.0
    while True:
        worked = await asyncio.to_thread(pipeline.run_once)
        if not worked:
            if time.time() - last_backfill > 600:
                last_backfill = time.time()
                await asyncio.to_thread(export.backfill)
            await asyncio.sleep(10)


def cleanup() -> None:
    cutoff = time.time() - KEEP_PUBLISHED_DAYS * 86400
    for r in db.q("SELECT id, out_path FROM candidates WHERE status IN ('scheduled','drafted','posted') AND updated_at < ?", (cutoff,)):
        if r["out_path"]:
            Path(r["out_path"]).unlink(missing_ok=True)
    for r in db.q("SELECT raw_path FROM candidates WHERE status IN ('rejected','failed') AND updated_at < ?", (time.time() - 86400,)):
        if r["raw_path"]:
            Path(r["raw_path"]).unlink(missing_ok=True)
    for d in WORK_DIR.iterdir():
        if d.stat().st_mtime < time.time() - 86400:
            shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
    referenced = {r["raw_path"] for r in db.q("SELECT raw_path FROM candidates WHERE status IN ('pending','analyzing','rendering')")}
    for f in CANDIDATES_DIR.glob("*.mp4"):
        if str(f) not in referenced and f.stat().st_mtime < time.time() - 3 * 86400:
            f.unlink(missing_ok=True)
    free = shutil.disk_usage(READY_DIR).free / 1e9
    log.info("cleanup done, %.1f GB free", free)


async def cleanup_loop() -> None:
    from .processor import learn
    while True:
        await asyncio.to_thread(cleanup)
        try:
            await asyncio.to_thread(learn.refresh_views)
        except Exception:
            log.exception("view refresh failed")
        await asyncio.sleep(6 * 3600)


async def discover_loop() -> None:
    from .ingest import discover
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(discover.run_once)
        except Exception:
            log.exception("auto-discover failed")
        await asyncio.sleep(int(os.getenv("DISCOVER_MINUTES", "60")) * 60)


async def main(mode: str) -> None:
    db.conn()
    tasks = []
    if mode in ("all", "worker"):
        tasks += [live_supervisor(), vod_loop(), processor_loop(), cleanup_loop(), discover_loop()]
    if mode in ("all", "dashboard"):
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=DASHBOARD_PORT, log_level="warning"))
        tasks.append(server.serve())
        log.info("dashboard on http://localhost:%s", DASHBOARD_PORT)
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "process-file":  # python -m clipper process-file video.mp4 [profile]
        from .config import CANDIDATES_DIR as CD
        src = Path(sys.argv[2])
        dst = CD / f"file_{int(time.time())}{src.suffix}"
        shutil.copy(src, dst)
        cid = db.add_candidate({"id": "file:" + src.stem, "platform": "file",
                                "profile": sys.argv[3] if len(sys.argv) > 3 else "default"}, "file", str(src), str(dst), "manual file")
        pipeline.run_once()
        print(db.q("SELECT id,status,viral_score,hook,out_path,error FROM candidates WHERE id=?", (cid,))[0])
        sys.exit(0)
    try:
        asyncio.run(main(mode))
    except KeyboardInterrupt:
        pass
