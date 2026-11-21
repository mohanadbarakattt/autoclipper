"""Cheap local telemetry: chat scoring and spike detection. No AI runs here."""
from __future__ import annotations

import re
import statistics
import time
from collections import deque


def score_message(msg: str, profile: dict) -> float:
    # plain chatter barely counts: a spike must come from *reactions*, not from volume alone
    score = 0.25
    low = msg.lower()
    for emote, weight in profile.get("emote_weights", {}).items():
        if re.search(rf"(?<!\w){re.escape(emote.lower())}(?!\w)", low):
            score += float(weight)
    for kw in profile.get("topic_keywords", []):
        if kw.lower() in low:
            score += 1.5
    letters = [c for c in msg if c.isalpha()]
    if len(letters) >= 5 and msg.isupper():
        score += 1.5
    if "!!" in msg or "??" in msg:
        score += 0.5
    return score


class SpikeDetector:
    """Rolls fixed windows of chat score; fires when a window beats the rolling median by `spike_multiplier`."""

    def __init__(self, profile: dict, window_sec: int = 10, history: int = 60):
        self.window_sec = window_sec
        self.mult = float(profile.get("spike_multiplier", 3.0))
        self.min_score = float(profile.get("min_window_score", 25))
        self.cooldown = float(profile.get("cooldown_sec", 180))
        self.hist: deque[float] = deque(maxlen=history)
        self.cur = 0.0
        self.msgs = 0
        self.last_fire = 0.0
        self.mps = 0.0
        self.last_score = 0.0
        self.baseline = 0.0
        self.seen: dict[str, int] = {}

    def add(self, score: float, text: str | None = None) -> None:
        """`text` lets us damp copy-pasta / sub-train spam: the 3rd+ identical message in a window counts ~nothing."""
        self.msgs += 1
        if text is not None:
            key = re.sub(r"\W+", " ", text.lower()).strip()[:60]
            n = self.seen.get(key, 0) + 1
            self.seen[key] = n
            if n > 2:
                score *= 0.05
        self.cur += score

    def roll(self) -> bool:
        score, self.cur = self.cur, 0.0
        self.mps, self.msgs = self.msgs / self.window_sec, 0
        self.seen = {}
        self.last_score = score
        ready = len(self.hist) >= 6
        self.baseline = statistics.median(self.hist) if self.hist else 0.0
        self.hist.append(score)
        now = time.time()
        if ready and score >= max(self.baseline * self.mult, self.min_score) and now - self.last_fire >= self.cooldown:
            self.last_fire = now
            return True
        return False

    def force(self) -> bool:
        """External signal (e.g. a viewer clip cluster) — still respects the cooldown."""
        now = time.time()
        if now - self.last_fire >= self.cooldown:
            self.last_fire = now
            return True
        return False
