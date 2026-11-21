"""Live workers: one per streamer. Detect live -> record ring buffer -> watch chat/audio -> cut candidates."""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time

from .. import db
from ..config import get_streamer, load_profile
from ..media import loudness_per_second
from . import platforms
from .ringbuffer import SEG_SEC, RingRecorder
from .spikes import SpikeDetector, score_message

log = logging.getLogger("live")

CHECK_OFFLINE_SEC = 120
CHECK_LIVE_SEC = 300
MAX_RECORDINGS = int(__import__("os").getenv("MAX_LIVE_RECORDINGS", "4"))
RECORDING: set[str] = set()


class LiveWorker:
    def __init__(self, streamer: dict):
        self.s = streamer
        self.sid = streamer["id"]
        self.rec = RingRecorder(self.sid)
        self.det: SpikeDetector | None = None
        self.stop_evt = asyncio.Event()
        self._chat_task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()

    # ---------- main loop ----------
    async def run(self) -> None:
        await asyncio.sleep(random.uniform(0, 10))  # spread API calls at startup
        while not self.stop_evt.is_set():
            s = get_streamer(self.sid)
            if not s or s.get("paused") or not s.get("live", True):
                await self._go_offline(paused=True)
                await self._sleep(30)
                continue
            self.s = s
            profile = load_profile(s["profile"])
            info = await asyncio.to_thread(platforms.live_info, s)
            want = info["live"] and platforms.matches_filter(info, profile)
            if want and not self.rec.running and s.get("auto") and len(RECORDING) >= MAX_RECORDINGS:
                want = False  # laptop at capacity: auto-found channels wait, your own channels always record
            db.upsert_status(self.sid, platform=s["platform"], channel=s["channel"], is_live=int(info["live"]),
                             title=info["title"], category=info["category"], recording=int(want))
            if want and not self.rec.running:
                log.info("%s is live (%s) - starting capture", self.sid, info["category"] or info["title"])
                if self._chat_task:  # capture died mid-stream: restart cleanly
                    self._chat_task.cancel()
                self.det = SpikeDetector(profile)
                self.rec.start(info["url"])
                RECORDING.add(self.sid)
                self._chat_task = asyncio.create_task(self._watch(info["url"], profile))
            elif not want and self.rec.running:
                log.info("%s went offline / off-topic", self.sid)
                await self._go_offline()
            await self._sleep(CHECK_LIVE_SEC if want else CHECK_OFFLINE_SEC)
        await self._go_offline()

    async def _sleep(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self.stop_evt.wait(), timeout=sec)
        except asyncio.TimeoutError:
            pass

    async def _go_offline(self, paused: bool = False) -> None:
        if self._chat_task:
            self._chat_task.cancel()
            self._chat_task = None
        if self.rec.running:
            self.rec.stop()
        RECORDING.discard(self.sid)
        db.upsert_status(self.sid, platform=self.s["platform"], channel=self.s["channel"], recording=0, mps=0,
                         **({"is_live": 0} if paused else {}))

    def stop(self) -> None:
        self.stop_evt.set()

    # ---------- signals ----------
    async def _watch(self, url: str, profile: dict) -> None:
        tasks = [self._roller(profile)]
        if self.s["platform"] == "twitch":
            tasks.append(self._twitch_irc(profile))
            if platforms.TWITCH.configured:
                tasks.append(self._twitch_clip_clusters())
        elif self.s["platform"] == "youtube":
            tasks.append(self._youtube_chat(url, profile))
        else:  # kick: no public chat feed without webhooks -> audio energy
            tasks.append(self._audio_energy())
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("signal watcher crashed for %s", self.sid)

    async def _roller(self, profile: dict) -> None:
        while True:
            await asyncio.sleep(self.det.window_sec)
            if not self.rec.running:
                continue
            fired = self.det.roll()
            db.upsert_status(self.sid, mps=round(self.det.mps, 2), window_score=round(self.det.last_score, 1),
                             baseline=round(self.det.baseline, 1))
            if fired:
                self._trigger(f"chat spike {self.det.last_score:.0f} vs baseline {self.det.baseline:.0f}", profile)

    def _trigger(self, reason: str, profile: dict) -> None:
        if self.rec.segment_count() < 3:
            return  # not enough buffer yet
        ts = time.time()
        log.info("SPIKE %s: %s", self.sid, reason)
        db.upsert_status(self.sid, last_spike=ts)
        t = asyncio.create_task(self._cut(ts, reason, profile))
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    async def _cut(self, ts: float, reason: str, profile: dict) -> None:
        name = f"{self.sid.replace(':', '_')}_{int(ts)}"
        out = await self.rec.extract(ts, profile["window_before_sec"], profile["window_after_sec"], name)
        if out:
            db.add_candidate(self.s, "live", platforms.stream_url(self.s), str(out), reason)

    async def _twitch_irc(self, profile: dict) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_connection("irc.chat.twitch.tv", 6667)
                writer.write(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
                writer.write(f"JOIN #{self.s['channel']}\r\n".encode())
                await writer.drain()
                while True:
                    line = (await reader.readline()).decode("utf-8", "ignore")
                    if not line:
                        break
                    if line.startswith("PING"):
                        writer.write(b"PONG :tmi.twitch.tv\r\n")
                        await writer.drain()
                    elif " PRIVMSG " in line:
                        msg = line.split(" :", 1)[-1].strip()
                        self.det.add(score_message(msg, profile), msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("twitch irc %s: %s", self.sid, e)
            await asyncio.sleep(5)

    async def _twitch_clip_clusters(self) -> None:
        seen: set[str] = set()
        profile = load_profile(self.s["profile"])
        while True:
            await asyncio.sleep(60)
            try:
                clips = await asyncio.to_thread(platforms.twitch_recent_clips, self.s["channel"], 3)
                new = [c for c in clips if c["id"] not in seen]
                seen.update(c["id"] for c in clips)
                if len(new) >= 2 and self.det.force():
                    self._trigger(f"{len(new)} viewer clips in 3 min", profile)
            except Exception as e:
                log.warning("helix clips %s: %s", self.sid, e)

    async def _youtube_chat(self, url: str, profile: dict) -> None:
        loop = asyncio.get_running_loop()
        stop = threading.Event()

        def pump() -> None:
            from chat_downloader import ChatDownloader
            while not stop.is_set():
                try:
                    for m in ChatDownloader().get_chat(url, message_types=["text_message"]):
                        if stop.is_set():
                            return
                        loop.call_soon_threadsafe(self.det.add, score_message(m.get("message") or "", profile), m.get("message") or "")
                except Exception as e:
                    log.warning("youtube chat %s: %s", self.sid, e)
                stop.wait(15)

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            stop.set()

    async def _audio_energy(self) -> None:
        """Feed loudness of each finished segment into the detector (1 pt per dB above the stream median)."""
        done: set[str] = set()
        hist: list[float] = []
        while True:
            await asyncio.sleep(SEG_SEC / 2)
            for mtime, seg in self.rec.finished_segments()[-2:]:
                key = f"{seg.name}:{int(mtime)}"
                if key in done:
                    continue
                done.add(key)
                db_arr = await asyncio.to_thread(loudness_per_second, seg, 60)
                if not len(db_arr):
                    continue
                peak = float(max(db_arr))
                hist = (hist + [float(db_arr.mean())])[-120:]
                med = sorted(hist)[len(hist) // 2]
                self.det.add(max(0.0, peak - med) * 3)
                self.det.msgs = 0
