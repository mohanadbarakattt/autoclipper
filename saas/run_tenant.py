"""Run the unchanged engine once for one user's workspace, then exit.

    python -m saas.run_tenant --workspace saas_data/workspaces/u1 --clips 10 --moments 150 --scorer gemini

CLIPPER_ROOT must be set before `clipper` is imported (every engine path comes from it), which is why this runs
in its own process. The last stdout line is a JSON summary the worker reads.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--clips", type=int, required=True, help="finished clips still allowed this period")
    ap.add_argument("--moments", type=int, required=True, help="AI checks still allowed this period")
    ap.add_argument("--scorer", default="auto")
    ap.add_argument("--keep-days", type=int, default=7)
    ap.add_argument("--skip-scan", action="store_true", help="only process moments already found")
    a = ap.parse_args(argv)

    ws = Path(a.workspace).resolve()
    os.environ["CLIPPER_ROOT"] = str(ws)
    os.environ["SCORER"] = a.scorer
    os.environ["KEEP_PUBLISHED_DAYS"] = str(a.keep_days)
    os.environ.setdefault("CLIPPER_FONTS_DIR", str(Path(__file__).resolve().parent.parent / "assets" / "fonts"))
    os.environ["EXPORT_DIR"] = ""  # hosted users download from the site, no shared Drive folder
    os.environ.pop("TRYPOST_API_KEY", None)

    import logging
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "google_genai", "google_genai.models"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from clipper import db
    from clipper.processor import pipeline

    db.conn()
    summary = {"found": 0, "checked": 0, "rendered": [], "checked_ids": [], "stopped": None}
    before = {r["id"] for r in db.q("SELECT id FROM candidates")}
    if not a.skip_scan and a.clips > 0 and a.moments > 0:
        from clipper.ingest import vod
        vod.poll_all()
    summary["found"] = len({r["id"] for r in db.q("SELECT id FROM candidates")} - before)

    def finished() -> set[int]:
        return {r["id"] for r in db.q("SELECT id FROM candidates WHERE out_path IS NOT NULL AND out_path != ''")}

    done_before = finished()
    deadline = time.time() + float(os.getenv("SAAS_RUN_MAX_SEC", "7200"))
    while True:
        rendered = finished() - done_before
        if len(rendered) >= a.clips:
            summary["stopped"] = "clip limit"
            break
        if summary["checked"] >= a.moments:
            summary["stopped"] = "moment limit"
            break
        if time.time() > deadline:
            summary["stopped"] = "time limit"
            break
        nxt = db.q("SELECT id FROM candidates WHERE status='pending' ORDER BY created_at LIMIT 1")
        if not nxt or not pipeline.run_once():
            break
        summary["checked"] += 1
        summary["checked_ids"].append(nxt[0]["id"])
    summary["rendered"] = sorted(finished() - done_before)

    try:
        from clipper.__main__ import cleanup
        cleanup()
        from clipper.processor import learn
        learn.refresh_views()
    except Exception:
        logging.getLogger("run_tenant").exception("cleanup/view refresh failed")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
