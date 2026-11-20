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

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "sonnet")
SCORER = os.getenv("SCORER", "auto")  # auto | claude | gemini


class Verdict(BaseModel):
    is_viral_moment: bool
    reason: str
    summary: str = Field(description="What happens in the clip, 1-2 sentences")
    start_sec: float
    end_sec: float
    facecam_box: list[int] = Field(description="[ymin, xmin, ymax, xmax] 0-1000 of the streamer webcam overlay, or [] if none")
    subject_center_x: int = Field(description="0-1000 horizontal centre of the main person/action")
    layout_hint: str = Field(description="stacked_facecam | speaker_crop | blur_fit")


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
        _client = genai.Client(api_key=key)
    return _client


def _retry(fn, tries: int = 5):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            msg = str(e)
            if i < tries - 1 and any(c in msg for c in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500")):
                wait = 30 * (i + 1)
                log.warning("gemini busy/rate-limited, waiting %ss", wait)
                time.sleep(wait)
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

Decide if it contains a standalone moment that would hold a cold viewer on TikTok/Reels/Shorts
(big reaction, funny line, fail, clutch play, shocking reveal, strong take). Ordinary gameplay or
chatting is NOT viral.

If yes, choose cut points (seconds from the start of THIS video) that include the setup and end right
after the payoff. Length must be {d['min']}-{d['max']} seconds. Never start mid-sentence.
facecam_box: bounding box of the streamer webcam overlay as [ymin, xmin, ymax, xmax] scaled 0-1000, or [] if there is none.
subject_center_x: 0-1000 horizontal centre of the main person or action.
layout_hint: stacked_facecam if gameplay + webcam overlay; speaker_crop if one/two people on camera (podcast/IRL); blur_fit if neither works."""
        resp = _retry(lambda: client.models.generate_content(
            model=GEMINI_MODEL, contents=[f, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Verdict,
                                               temperature=0.2)))
        return _extract_json(resp.text)
    finally:
        try:
            client.files.delete(name=f.name)
        except Exception:
            pass


def gemini_text_json(prompt: str) -> dict:
    from google.genai import types
    resp = _retry(lambda: _gemini().models.generate_content(
        model=GEMINI_MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Score, temperature=0.6)))
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
    with tempfile.TemporaryDirectory() as cwd:  # empty dir: nothing for it to touch
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=cwd)
    if p.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {(p.stderr or p.stdout)[-800:]}")
    out = json.loads(p.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {out.get('result')}")
    return _extract_json(out.get("result", ""))


def score_clip(transcript: str, verdict: dict, profile: dict, platform: str, streamer: str) -> tuple[dict, str]:
    prompt = f"""You decide which livestream clips get posted as Shorts/Reels/TikToks for a page about: {profile['niche_description']}.
Source: {platform} streamer "{streamer}".
Scene (from a video model): {verdict.get('summary')} | why flagged: {verdict.get('reason')}
Transcript of the clip:
\"\"\"{transcript[:6000] or '(no speech)'}\"\"\"

Score standalone viral potential 1-10 for a cold viewer who doesn't know the streamer
(hook in first 2s, clear payoff, emotion, rewatch/share value). Be strict: most clips are 5-7.
Return ONLY JSON:
{{"viral_score": float,
  "top_banner_hook": "max 6 words, curiosity hook shown on screen for 3.5s, no emojis, no hashtags",
  "title": "max 90 chars YouTube Shorts title",
  "caption": "1-2 punchy sentences for TikTok/Reels, credit the streamer by name",
  "hashtags": ["5 relevant tags without #"]}}"""
    use_claude = SCORER == "claude" or (SCORER == "auto" and claude_bin())
    if use_claude:
        try:
            return Score(**claude_json(prompt)).model_dump(), "claude"
        except Exception as e:
            if SCORER == "claude":
                raise
            log.warning("Claude scoring failed (%s) - falling back to Gemini", e)
    return Score(**gemini_text_json(prompt)).model_dump(), "gemini"
