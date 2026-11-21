"""Candidate -> Gemini verify -> trim -> transcribe -> Claude score/hook -> render -> TryPost."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from .. import db
from ..config import READY_DIR, WORK_DIR, load_profile
from ..media import cut, make_proxy, probe
from . import ai, export, learn, publish, render

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


def apply_campaign_rules(score: dict, profile: dict) -> None:
    """Clipping-campaign requirements from the profile: required hashtags first, required caption text appended."""
    req = [t.lstrip("#").strip() for t in profile.get("required_hashtags") or [] if t.strip()]
    if req:
        rest = [t for t in score.get("hashtags", []) if t.lstrip("#").lower() not in {r.lower() for r in req}]
        score["hashtags"] = (req + rest)[: max(len(req), int(profile.get("publish", {}).get("max_hashtags", 5)))]
    suffix = (profile.get("caption_suffix") or "").strip()
    if suffix and suffix not in score.get("caption", ""):
        score["caption"] = f"{score.get('caption', '').rstrip()}\n{suffix}"


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
        if dur < 5 or raw.stat().st_size / max(dur, 1) < 60_000:
            # < ~0.5 Mbit/s for a 1080p stream = frozen frame, ad break or "offline" screen
            db.update_candidate(cid, status="rejected", error="footage looks frozen/offline (skipped before AI)")
            raw.unlink(missing_ok=True)
            return

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

        # 3. Gemini scores everything; Claude gives the final word on clips that pass
        db.update_candidate(cid, status="rendering")
        streamer = cand["streamer_id"].split(":", 1)[-1]
        examples = learn.performance_examples(profile["_file"])
        score, scorer = ai.score_clip(transcript, verdict, profile, cand["platform"], streamer, examples)
        score["scorer"] = scorer
        score["transcript"] = transcript
        apply_campaign_rules(score, profile)
        db.update_candidate(cid, score_json=score, viral_score=score["viral_score"], hook=score["top_banner_hook"],
                            title=score["title"], caption=score["caption"])
        if score["viral_score"] < float(profile["min_viral_score"]):
            db.update_candidate(cid, status="rejected",
                                error=f"{scorer} score {score['viral_score']} < {profile['min_viral_score']}")
            raw.unlink(missing_ok=True)
            return

        # 4. same moment already kept? keep only the better one
        dup = learn.find_duplicate(cid, transcript, verdict.get("summary", ""), cand.get("source_ref") or "",
                                   float(cand.get("source_offset") or 0) + start)
        if dup:
            if dup["status"] == "posted" or (dup["viral_score"] or 0) >= score["viral_score"]:
                db.update_candidate(cid, status="rejected", error=f"duplicate of clip #{dup['id']} (kept that one)")
                raw.unlink(missing_ok=True)
                return
            db.update_candidate(dup["id"], status="rejected", scheduled_at=None,
                                error=f"duplicate - replaced by better clip #{cid}")
            log.info("clip %s replaces duplicate %s", cid, dup["id"])

        # 5. render vertical (shift Gemini's timings from the raw excerpt to the trimmed clip)
        rv = dict(verdict)
        rv["action_track"] = [{"t": float(p["t"]) - start, "x": p["x"]} for p in verdict.get("action_track") or []
                              if isinstance(p, dict) and "t" in p and "x" in p]
        if verdict.get("payoff_sec") is not None:
            rv["payoff_sec"] = float(verdict["payoff_sec"]) - start
        out = READY_DIR / f"clip_{cid}.mp4"
        render.render_vertical(clip, out, work, words, score["top_banner_hook"], rv, profile)
        db.update_candidate(cid, status="ready", out_path=str(out))
        raw.unlink(missing_ok=True)

        # 6. TryPost / schedule
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
        bumped = res.get("bumped") and db.q("SELECT * FROM candidates WHERE id=?", (res["bumped"],))
        if bumped:  # a better clip took this one's slot: move it to the back of the queue
            export.unexport(bumped[0])
            db.update_candidate(bumped[0]["id"], scheduled_at=None)
        db.update_candidate(cid, status=res["status"], trypost_post_ids=res.get("post_ids"),
                            scheduled_at=res.get("scheduled_at"), error=res.get("note"))
        if bumped:
            b = bumped[0]
            new_at = publish.next_slot(load_profile(b["profile"]), ignore_id=b["id"]).isoformat()
            db.update_candidate(b["id"], scheduled_at=new_at)
            log.info("clip %s (score %s) took the slot of clip %s, which moves to %s", cid, cand.get("viral_score"),
                     b["id"], new_at)
            try:
                export.export_clip(b["id"])
            except Exception:
                log.exception("re-export failed for %s", b["id"])
    except Exception as e:
        log.exception("publish failed for %s", cid)
        db.update_candidate(cid, status="publish_failed", error=str(e)[:1000])
    try:
        export.export_clip(cid)
    except Exception:
        log.exception("export to EXPORT_DIR failed for %s", cid)


def run_once() -> bool:
    cand = db.claim_next_pending()
    if not cand:
        return False
    log.info("processing candidate %s (%s, %s)", cand["id"], cand["streamer_id"], cand["trigger"])
    process(cand)
    return True
