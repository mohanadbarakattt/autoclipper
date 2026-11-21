"""Virlo Studio entry point.

    python -m saas                 web app + worker in one process (dev / single server)
    python -m saas web             web app only
    python -m saas worker          worker only
    python -m saas set-plan EMAIL PLAN        free | starter | creator | pro
    python -m saas make-admin EMAIL
    python -m saas block-channel PLATFORM CHANNEL [reason]   creator opt-out / takedown
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

from . import settings, store


def _cli(args: list[str]) -> int:
    cmd = args[0]
    if cmd == "set-plan" and len(args) == 3:
        if args[2] not in settings.PLANS:
            print("plan must be one of:", ", ".join(settings.PLANS))
            return 2
        store.x("UPDATE users SET plan=? WHERE email=?", (args[2], args[1].lower()))
    elif cmd == "make-admin" and len(args) == 2:
        store.x("UPDATE users SET is_admin=1 WHERE email=?", (args[1].lower(),))
    elif cmd == "block-channel" and len(args) >= 3:
        plat, ch = args[1].lower(), args[2].lstrip("@")
        store.x("INSERT OR REPLACE INTO blocked_channels (platform, channel, reason, created_at) VALUES (?,?,?,?)",
                (plat, ch if plat == "youtube" else ch.lower(), " ".join(args[3:]), time.time()))
        # pause it everywhere right away
        from . import workspace
        for u in store.q("SELECT DISTINCT user_id FROM channels WHERE platform=? AND lower(channel)=lower(?)", (plat, ch)):
            workspace.sync(u["user_id"])
    else:
        print(__doc__)
        return 2
    print("done")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = args[0] if args else "all"
    if mode not in ("all", "web", "worker"):
        return _cli(args)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s", datefmt="%H:%M:%S")
    store.conn()
    from . import worker
    if mode == "worker":
        worker.loop()
        return 0
    if mode == "all":
        threading.Thread(target=worker.loop, daemon=True, name="worker").start()
    import uvicorn
    from .web import app
    logging.getLogger("main").info("Virlo Studio on http://localhost:%s", settings.PORT)
    uvicorn.run(app, host=os.getenv("SAAS_HOST", "127.0.0.1"), port=settings.PORT, log_level="warning",
                proxy_headers=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
