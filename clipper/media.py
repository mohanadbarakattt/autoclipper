"""ffmpeg / ffprobe / yt-dlp / streamlink helpers."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger("media")

FFMPEG = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.getenv("FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"
YTDLP = [sys.executable, "-m", "yt_dlp"]
STREAMLINK = [sys.executable, "-m", "streamlink"]


def run(cmd: list[str], timeout: float | None = None, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(map(str, cmd)))
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=cwd)
    if check and p.returncode != 0:
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed ({p.returncode}): {p.stderr[-1500:]}")
    return p


def probe(path: str | Path) -> dict:
    out = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]).stdout
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), {})
    return {
        "duration": float(data["format"].get("duration", 0) or 0),
        "width": int(v.get("width", 0) or 0),
        "height": int(v.get("height", 0) or 0),
        "has_audio": any(s["codec_type"] == "audio" for s in data["streams"]),
    }


def make_proxy(src: Path, dst: Path) -> Path:
    """480p, 1 fps, low-bitrate mono audio: what Gemini actually samples, at a fraction of the upload."""
    # a burned-in seconds counter: Gemini reads it instead of guessing timestamps (it used to drift past the end)
    from .config import FONTS_DIR
    font = next((f for f in sorted(FONTS_DIR.glob("*.ttf"))), None) if FONTS_DIR.exists() else None
    clock = (f",drawtext=fontfile='{font.as_posix().replace(':', chr(92) + ':')}':text='%{{eif\\:t\\:d}}s':"
             "x=10:y=10:fontsize=36:fontcolor=yellow:box=1:boxcolor=black@0.7:boxborderw=6") if font else ""
    run([FFMPEG, "-y", "-v", "error", "-i", src, "-vf", f"scale=-2:480,fps=1{clock}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
         "-c:a", "aac", "-ac", "1", "-b:a", "48k", dst], timeout=600)
    return dst


def cut(src: Path, dst: Path, start: float, length: float) -> Path:
    run([FFMPEG, "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", src, "-t", f"{length:.3f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", dst], timeout=900)
    return dst


def loudness_per_second(src: str | Path, timeout: float = 3600) -> np.ndarray:
    """Mean dBFS per second of the audio track, decoded at 8 kHz mono."""
    sr = 8000
    p = subprocess.run([FFMPEG, "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr),
                        "-f", "s16le", "-"], capture_output=True, timeout=timeout)
    pcm = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    n = len(pcm) // sr
    if n == 0:
        return np.zeros(0)
    frames = pcm[: n * sr].reshape(n, sr)
    rms = np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-9
    return 20 * np.log10(rms)


def loud_windows(db: np.ndarray, window: int = 75, top_n: int = 5, min_gap: int = 120) -> list[tuple[int, int, float]]:
    """Pick the top non-overlapping windows whose loudness spikes furthest above the stream's norm."""
    if len(db) < window:
        return []
    z = (db - np.median(db)) / (np.std(db) + 1e-6)
    short = np.convolve(z, np.ones(8) / 8, mode="same")  # 8 s bursts (laughs, screams, hype)
    order = np.argsort(short)[::-1]
    picks: list[tuple[int, int, float]] = []
    for peak in order:
        if len(picks) >= top_n or short[peak] < 1.5:
            break
        if any(abs(peak - (s + window * 2 // 3)) < min_gap for s, _, _ in picks):
            continue
        start = max(0, int(peak) - window * 2 // 3)  # keep the setup before the peak
        picks.append((start, min(len(db), start + window), float(short[peak])))
    return picks


def ytdlp_json(url: str, extra: list[str] | None = None, timeout: float = 120) -> dict | None:
    p = run([*YTDLP, "--dump-single-json", "--no-warnings", "--skip-download", *(extra or []), url],
            timeout=timeout, check=False)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def _ffloc() -> list[str]:
    return ["--ffmpeg-location", str(Path(FFMPEG).parent)] if os.path.isabs(FFMPEG) else []


def ytdlp_section(url: str, start: float, end: float, dst: Path, timeout: float = 1800) -> Path:
    run([*YTDLP, "--no-warnings", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
         "--download-sections", f"*{start:.0f}-{end:.0f}", "--force-keyframes-at-cuts",
         "--merge-output-format", "mp4", *_ffloc(), "-o", str(dst), url], timeout=timeout)
    return dst


def ytdlp_audio(url: str, dst_noext: Path, timeout: float = 3600) -> Path | None:
    run([*YTDLP, "--no-warnings", "-f", "worstaudio/ba/worst", "-o", f"{dst_noext}.%(ext)s", url], timeout=timeout)
    return next(iter(sorted(dst_noext.parent.glob(dst_noext.name + ".*"))), None)


def streamlink_live(url: str) -> tuple[bool, dict]:
    """(is_live, metadata) - metadata has title/category/author for Twitch and Kick, no API keys needed."""
    p = run([*STREAMLINK, "--json", url], timeout=60, check=False)
    try:
        d = json.loads(p.stdout or "{}")
        return "streams" in d, d.get("metadata") or {}
    except json.JSONDecodeError:
        return False, {}
