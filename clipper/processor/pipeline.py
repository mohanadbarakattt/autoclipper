"""Candidate -> Gemini verify -> trim -> transcribe -> Claude score/hook -> render -> TryPost."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from .. import db
from ..config import READY_DIR, WORK_DIR, load_profile
from ..media import cut, make_proxy, probe
from . import ai, publish, render

log = logging.getLogger("pipeline")


def _clamp(start: float, end: float, dur: float, mn: float, mx: float) -> tuple[float, float]:
    start = max(0.0, min(start, dur))
    end = max(start, min(end, dur))
    if end - start < mn:  # widen around the middle
        pad = (mn - (end - start)) / 2
        start, end = max(0.0, start - pad), min(dur, end + pad)
        if end - start < mn:  # hit an edge of the footage: grow the other way
            if start <= 0:
                end = min(dur, mn)
            else:
                start = max(0.0, end - mn)
    if end - start > mx:
        end = start + mx
    return start, end


def process(cand: dict) -> None:
    cid = cand["id"]
    profile = load_profile(cand["profile"])
    raw = Path(cand["raw_path"])
    work = WORK_DIR / str(cid)
    work.mkdir(parents=True, exist_ok=True)
    try:
        if not raw.exists():
            raise FileNotFoundError(f"raw clip missing: {raw}")
        dur = probe(raw)["duration"]

        # 1. Gemini looks at a cheap 480p/1fps proxy
        proxy = make_proxy(raw, work / "proxy.mp4")
        verdict = ai.gemini_verify(proxy, profile, cand.get("trigger") or "", dur)
        proxy.unlink(missing_ok=True)
        db.update_candidate(cid, gemini_json=verdict)
        if not verdict.get("is_viral_moment"):
            db.update_candidate(cid, status="rejected", error=f"Gemini: {verdict.get('reason', '')[:300]}")
            raw.unlink(missing_ok=True)
            return

        # 2. exact cut + transcript
        d = profile["clip_duration"]
        start, end = _clamp(float(verdict["start_sec"]), float(verdict["end_sec"]), dur, d["min"], d["max"])
        clip = cut(raw, work / "clip.mp4", start, end - start)
        words, transcript = render.transcribe(clip, profile.get("whisper_model", "small"), profile.get("language"))

        # 3. Claude (subscription) scores + writes the hook
        db.update_candidate(cid, status="rendering")
        streamer = cand["streamer_id"].split(":", 1)[-1]
        score, scorer = ai.score_clip(transcript, verdict, profile, cand["platform"], streamer)
        score["scorer"] = scorer
        score["transcript"] = transcript
        db.update_candidate(cid, score_json=score, viral_score=score["viral_score"], hook=score["top_banner_hook"],
                            title=score["title"], caption=score["caption"])
        if score["viral_score"] < float(profile["min_viral_score"]):
            db.update_candidate(cid, status="rejected",
                                error=f"{scorer} score {score['viral_score']} < {profile['min_viral_score']}")
            raw.unlink(missing_ok=True)
            return

        # 4. render vertical
        out = READY_DIR / f"clip_{cid}.mp4"
        render.render_vertical(clip, out, work, words, score["top_banner_hook"], verdict, profile)
        db.update_candidate(cid, status="ready", out_path=str(out))
        raw.unlink(missing_ok=True)

        # 5. TryPost
        publish_candidate(cid)
    except Exception as e:
        log.exception("candidate %s failed", cid)
        db.update_candidate(cid, status="failed", error=str(e)[:1000])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def publish_candidate(cid: int) -> None:
    cand = db.q("SELECT * FROM candidates WHERE id=?", (cid,))[0]
    profile = load_profile(cand["profile"])
    score = json.loads(cand["score_json"] or "{}")
    cand = {**cand, "hashtags": score.get("hashtags", [])}
    try:
        res = publish.publish(cand, profile)
        db.update_candidate(cid, status=res["status"], trypost_post_ids=res.get("post_ids"),
                            scheduled_at=res.get("scheduled_at"), error=res.get("note"))
    except Exception as e:
        log.exception("publish failed for %s", cid)
        db.update_candidate(cid, status="publish_failed", error=str(e)[:1000])


def run_once() -> bool:
    cand = db.claim_next_pending()
    if not cand:
        return False
    log.info("processing candidate %s (%s, %s)", cand["id"], cand["streamer_id"], cand["trigger"])
    process(cand)
    return True
