"""The two brains, both $0:

- Gemini (free AI Studio key, no billing) watches the 480p/1fps proxy and finds the moment + facecam.
- Claude runs through the Claude Code CLI in headless mode (`claude -p`) on YOUR existing
  Claude subscription (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`). No Anthropic API key.
  If Claude isn't available, Gemini does the scoring too.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger("ai")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
# Tried in order. Free-tier Flash is often overloaded (503); Flash-Lite almost always answers.
# The -latest aliases follow Google's current models, so retirements don't break us.
GEMINI_CHAIN = list(dict.fromkeys([GEMINI_MODEL, "gemini-flash-latest", "gemini-flash-lite-latest"]))
GEMINI_TIMEOUT_MS = int(os.getenv("GEMINI_TIMEOUT_SEC", "90")) * 1000
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "sonnet")
SCORER = os.getenv("SCORER", "auto")  # auto | claude | gemini


class TrackPoint(BaseModel):
    t: float = Field(description="seconds from the start of this video")
    x: int = Field(description="0-1000 horizontal centre of the main action/person at that moment")


class Verdict(BaseModel):
    is_viral_moment: bool
    reason: str
    summary: str = Field(description="What happens in the clip, 1-2 sentences")
    start_sec: float
    end_sec: float
    facecam_box: list[int] = Field(description="[ymin, xmin, ymax, xmax] 0-1000 of the streamer webcam overlay, or [] if none")
    subject_center_x: int = Field(description="0-1000 horizontal centre of the main person/action")
    layout_hint: str = Field(description="stacked_facecam | speaker_crop | blur_fit")
    payoff_sec: float = Field(description="second (from start of this video) of the single biggest payoff/reaction")
    action_track: list[TrackPoint] = Field(description="where the main action is, one point every ~3 s between start_sec and end_sec")


class Score(BaseModel):
    viral_score: float
    top_banner_hook: str
    title: str
    caption: str
    hashtags: list[str]


def _extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON in model reply: {text[:300]}")
    return json.loads(text[start : end + 1])


# ---------------- Gemini ----------------
_client = None


def _gemini():
    global _client
    if _client is None:
        from google import genai
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY missing - get a free one at https://aistudio.google.com/apikey")
        from google.genai import types
        # v1beta: the -latest aliases and preview models only exist there
        # attempts=1: the SDK's own retries stack on top of ours and can stall a clip for 10+ minutes
        _client = genai.Client(api_key=key, http_options=types.HttpOptions(
            timeout=GEMINI_TIMEOUT_MS, api_version="v1beta", retry_options=types.HttpRetryOptions(attempts=1)))
    return _client


_BUSY = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL", "504", "DEADLINE",
         "timed out", "Timeout", "NOT_FOUND", "404")
last_model = ""
_cooldown: dict[str, float] = {}


def _gen(contents, config):
    """Try each model in GEMINI_CHAIN (2 quick attempts each) until one answers."""
    global last_model
    err: Exception | None = None
    for rnd in range(3):  # if every model is busy, wait and go round again
        for i, model in enumerate(GEMINI_CHAIN):
            last = i == len(GEMINI_CHAIN) - 1
            if not last and rnd == 0 and _cooldown.get(model, 0) > time.time():
                continue  # was busy recently: don't wait on it again
            for attempt in range(2 if last else 1):  # busy primary -> straight to the next model
                try:
                    from .. import guard
                    guard.add("gemini")  # every request counts toward the daily budget, answered or not
                    resp = _gemini().models.generate_content(model=model, contents=contents, config=config)
                    if model != last_model:
                        log.info("Gemini answering with %s", model)
                    last_model = model
                    return resp
                except Exception as e:
                    err, msg = e, str(e)
                    if not any(b in msg for b in _BUSY):
                        raise
                    if "NOT_FOUND" in msg or "404" in msg:
                        break  # retired model: skip straight to the next one
                    log.warning("Gemini %s busy (%s) - %s", model, msg[:80], "next model" if not last else "retrying")
                    if not last:
                        _cooldown[model] = time.time() + 900
                    time.sleep(5)
        time.sleep(60 * (rnd + 1))
    raise RuntimeError(f"all Gemini models busy: {err}")


def _retry(fn, tries: int = 4):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i < tries - 1 and any(b in str(e) for b in _BUSY):
                log.warning("gemini upload busy, waiting %ss", 20 * (i + 1))
                time.sleep(20 * (i + 1))
                continue
            raise


def gemini_verify(proxy: Path, profile: dict, context: str, duration: float) -> dict:
    from google.genai import types
    client = _gemini()
    f = _retry(lambda: client.files.upload(file=str(proxy)))
    try:
        while f.state and f.state.name == "PROCESSING":
            time.sleep(3)
            f = client.files.get(name=f.name)
        if f.state and f.state.name == "FAILED":
            raise RuntimeError("Gemini could not process the proxy video")
        d = profile["clip_duration"]
        prompt = f"""You are a senior short-form video editor for the niche: {profile['niche_description']}.
This is a {duration:.0f}s candidate excerpt from a livestream (480p, 1 frame per second, with audio).
Why it was flagged: {context}
{("Campaign rules this clip must follow (reject it if it breaks them):" + chr(10) + profile['campaign_rules'][:2000] + chr(10)) if profile.get('campaign_rules') else ''}
Decide if it contains a standalone moment that would hold a cold viewer on TikTok/Reels/Shorts
(big reaction, funny line, fail, clutch play, shocking reveal, strong take). Ordinary gameplay,
driving around, menus, missions without a twist, or chatting is NOT viral.
Be strict: answer is_viral_moment=false for most excerpts (at least 7 out of 10). Only say true if
there is a clear peak - someone laughs hard, screams, is shocked, or something absurd happens on screen -
that a stranger would understand and share within the first 3 seconds.

The yellow counter in the top-left corner shows the exact second of each frame: read every timestamp
from it. All times must be between 0 and {duration:.0f}.
If yes, choose cut points (seconds from the start of THIS video) that include the setup and end right
after the payoff. Length must be {d['min']}-{d['max']} seconds. Never start mid-sentence.
facecam_box: bounding box of the streamer webcam overlay as [ymin, xmin, ymax, xmax] scaled 0-1000, or [] if there is none.
subject_center_x: 0-1000 horizontal centre of the main person or action.
layout_hint: stacked_facecam if gameplay + webcam overlay; speaker_crop if one/two people on camera (podcast/IRL); blur_fit if neither works.
payoff_sec: the exact second of the punchline / biggest reaction (used for a zoom-in).
action_track: horizontal position (0-1000) of the main action or speaker every ~3 seconds from start_sec to end_sec
(ignore the webcam overlay; follow the game action or the person talking), so a vertical crop can follow it."""
        resp = _gen([f, prompt], types.GenerateContentConfig(response_mime_type="application/json",
                                                             response_schema=Verdict, temperature=0.2))
        return _extract_json(resp.text)
    finally:
        try:
            client.files.delete(name=f.name)
        except Exception:
            pass


def gemini_text_json(prompt: str) -> dict:
    from google.genai import types
    resp = _gen(prompt, types.GenerateContentConfig(response_mime_type="application/json", response_schema=Score,
                                                     temperature=0.6))
    return _extract_json(resp.text)


# ---------------- Claude (subscription via Claude Code CLI) ----------------
def claude_bin() -> str | None:
    return os.getenv("CLAUDE_BIN") or shutil.which("claude")


def claude_json(prompt: str, timeout: int = 240) -> dict:
    exe = claude_bin()
    if not exe:
        raise RuntimeError("claude CLI not found")
    cmd = [exe, "-p", "--output-format", "json", "--model", CLAUDE_MODEL,
           "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,NotebookEdit,Task"]
    env = dict(os.environ)
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)  # empty value would override the normal `claude` login
    from .. import guard
    guard.add("claude")
    with tempfile.TemporaryDirectory() as cwd:  # empty dir: nothing for it to touch
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=cwd, env=env)
    if p.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {(p.stderr or p.stdout)[-800:]}")
    out = json.loads(p.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {out.get('result')}")
    return _extract_json(out.get("result", ""))


_claude_off_until = 0.0


def _claude_available() -> bool:
    from .. import guard
    return bool(claude_bin()) and time.time() > _claude_off_until and guard.ok("claude")


def score_clip(transcript: str, verdict: dict, profile: dict, platform: str, streamer: str,
               learn: str = "") -> tuple[dict, str]:
    """Gemini scores every clip (free, high volume). Only clips that pass the gate get a second, final
    opinion from Claude - so Claude's subscription limit is spent on the few clips that matter."""
    global _claude_off_until
    prompt = _score_prompt(transcript, verdict, profile, platform, streamer, learn)
    gate = float(os.getenv("CLAUDE_FINAL_MIN") or profile["min_viral_score"])
    if SCORER == "claude":
        return Score(**claude_json(prompt)).model_dump(), "claude"
    first = Score(**gemini_text_json(prompt)).model_dump()
    if SCORER == "gemini" or first["viral_score"] < gate or not _claude_available():
        return first, "gemini"
    try:
        final = Score(**claude_json(prompt)).model_dump()
        final["gemini_score"] = first["viral_score"]
        return final, "claude (final)"
    except Exception as e:
        msg = str(e)
        if "limit" in msg.lower():
            _claude_off_until = time.time() + 1800  # plan limit hit: don't retry for 30 min
            log.warning("Claude limit reached - Gemini-only scoring for 30 min")
        else:
            log.warning("Claude final check failed (%s) - keeping Gemini's score", msg[:200])
        return first, "gemini"


def _score_prompt(transcript: str, verdict: dict, profile: dict, platform: str, streamer: str, learn: str) -> str:
    learn_block = f"\nWhat has actually performed on THIS page (real view counts - weigh this heavily):\n{learn}\n" if learn else ""
    if profile.get("campaign_rules"):
        learn_block += ("\nThis clip is for a paid clipping campaign. Its rules (a clip that breaks them scores 1, "
                        f"and the hook/title/caption must follow them):\n{profile['campaign_rules'][:3000]}\n")
    return f"""You decide which livestream clips get posted as Shorts/Reels/TikToks for a page about: {profile['niche_description']}.{learn_block}
Source: {platform} streamer "{streamer}".
What happens ON SCREEN (a video model watched the whole clip - trust this, you can't see the video):
  {verdict.get('summary')}
  Why it stood out: {verdict.get('reason')}
Spoken words (transcript - often short or messy in gaming clips; visual fails/reactions don't need dialogue):
\"\"\"{transcript[:6000] or '(no speech)'}\"\"\"

Score standalone viral potential 1-10 for a cold viewer scrolling TikTok who doesn't know the streamer.
Judge the MOMENT (what happens + the reaction), not how much is said.
  9-10: instantly shareable - absurd/shocking event or huge reaction anyone gets in 2 seconds
  7-8:  clear payoff a stranger understands: a funny fail, a glitch, a big reaction, a great line
  5-6:  mildly amusing but needs context, or the payoff is weak
  1-4:  normal gameplay, chatting, menus, nothing happens
Return ONLY JSON:
{{"viral_score": float,
  "top_banner_hook": "max 6 words, curiosity hook shown on screen for 3.5s, no emojis, no hashtags",
  "title": "max 90 chars YouTube Shorts title",
  "caption": "1-2 punchy sentences for TikTok/Reels, credit the streamer by name",
  "hashtags": ["5 relevant tags without #"]}}"""
