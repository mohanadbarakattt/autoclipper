"""Rolling 30-minute buffer of 30 s .ts segments: streamlink -> ffmpeg segment muxer."""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from pathlib import Path

from ..config import CANDIDATES_DIR, RING_DIR
from ..media import FFMPEG, STREAMLINK, run

log = logging.getLogger("ring")

SEG_SEC = 30
SEGMENTS = 60  # 30 minutes


class RingRecorder:
    def __init__(self, streamer_id: str):
        self.dir = RING_DIR / streamer_id.replace(":", "_")
        self.sl: subprocess.Popen | None = None
        self.ff: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return bool(self.ff and self.ff.poll() is None and self.sl and self.sl.poll() is None)

    def start(self, url: str, quality: str = "1080p60,1080p,best") -> None:
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        log.info("recording %s -> %s", url, self.dir)
        self.sl = subprocess.Popen([*STREAMLINK, "--stdout", "--twitch-disable-ads", url, quality],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.ff = subprocess.Popen(
            [FFMPEG, "-v", "error", "-i", "pipe:0", "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
             "-f", "segment", "-segment_time", str(SEG_SEC), "-segment_wrap", str(SEGMENTS),
             "-reset_timestamps", "1", str(self.dir / "seg%03d.ts")],
            stdin=self.sl.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        for p in (self.sl, self.ff):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.sl = self.ff = None

    def finished_segments(self) -> list[tuple[float, Path]]:
        """(mtime, path) for segments that are no longer being written, oldest first."""
        segs = sorted(((p.stat().st_mtime, p) for p in self.dir.glob("seg*.ts")), key=lambda t: t[0])
        return segs[:-1]  # newest one is still open

    async def extract(self, trigger_ts: float, before: float, after: float, name: str) -> Path | None:
        """Wait for the post-trigger footage to land, then stitch [T-before, T+after] into a candidate mp4."""
        await asyncio.sleep(after + SEG_SEC + 5)
        lo, hi = trigger_ts - before, trigger_ts + after
        # a segment's mtime is when it finished; it covers roughly [mtime-SEG_SEC, mtime]
        picked = [(m, p) for m, p in self.finished_segments() if m >= lo and m - SEG_SEC <= hi]
        if not picked:
            log.warning("no segments for trigger at %s", trigger_ts)
            return None
        listfile = self.dir / f"concat_{int(trigger_ts)}.txt"
        listfile.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for _, p in picked), encoding="utf-8")
        first_start = picked[0][0] - SEG_SEC
        ss = max(0.0, lo - first_start)
        out = CANDIDATES_DIR / f"{name}.mp4"
        try:
            await asyncio.to_thread(run, [FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listfile,
                                          "-ss", f"{ss:.2f}", "-t", f"{before + after:.2f}", "-c", "copy",
                                          "-bsf:a", "aac_adtstoasc", out], 300)
        finally:
            listfile.unlink(missing_ok=True)
        return out if out.exists() and out.stat().st_size > 100_000 else None

    def segment_count(self) -> int:
        return len(list(self.dir.glob("seg*.ts"))) if self.dir.exists() else 0

    @staticmethod
    def now() -> float:
        return time.time()
