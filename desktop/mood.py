"""Persistent affective state (PAD model). Plan §12 (cognition/mood).

Pleasure / Arousal / Dominance floats in [-1, 1] that (a) decay toward a gentle
circadian baseline over time, (b) are nudged by events, (c) color generation via
one short prompt line (:meth:`render`), and (d) map to an autonomous OLED-eye
expression (:meth:`suggest_emotion`). Persisted as JSON in ``MemoryStore.kv``
under the ``mood`` key, so a restart resumes the same disposition.

Deliberately game-AI-simple: a slow tide, not a spike. Nudges are small (±0.05–
0.15); the half-life controls inertia (default 30 min). The robot never announces
its mood — it just expresses it.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Optional

# The 15 OLED "eyes" expressions (mirror the set_emotion enum, tools.py:111-114).
# suggest_emotion() only ever returns a member of this set.
OLED_EMOTIONS = {
    "neutral", "happy", "sad", "angry", "surprised", "curious", "sleepy", "love",
    "suspicious", "dizzy", "focused", "scared", "excited", "bored", "wink",
}


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass
class MoodState:
    pleasure: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0
    half_life_s: float = 1800.0
    circadian: bool = True
    baseline_pleasure: float = 0.1
    _last: Optional[float] = None  # monotonic of last decay (None until first decay)

    # --- events ---------------------------------------------------------------
    def update(self, dp: float = 0.0, da: float = 0.0, dd: float = 0.0) -> None:
        self.pleasure = _clamp(self.pleasure + dp)
        self.arousal = _clamp(self.arousal + da)
        self.dominance = _clamp(self.dominance + dd)

    def decay(self, now_mono: Optional[float] = None, hour: Optional[int] = None) -> None:
        """Exponentially relax PAD toward the (circadian) baseline by elapsed time."""
        now = time.monotonic() if now_mono is None else now_mono
        if self._last is None:  # first call just anchors the clock; no decay yet
            self._last = now
            return
        dt = max(0.0, now - self._last)
        self._last = now
        if dt <= 0.0:
            return
        base_p, base_a, base_d = self._baseline(hour)
        k = 0.5 ** (dt / self.half_life_s)
        self.pleasure = base_p + (self.pleasure - base_p) * k
        self.arousal = base_a + (self.arousal - base_a) * k
        self.dominance = base_d + (self.dominance - base_d) * k

    def _baseline(self, hour: Optional[int]) -> tuple[float, float, float]:
        if not self.circadian or hour is None:
            return (self.baseline_pleasure, 0.0, 0.0)
        # Gentle day/night arousal swing: calm before dawn (~3am), brighter midday (~2pm).
        arousal = 0.15 * math.sin(2 * math.pi * (hour - 8) / 24.0)
        return (self.baseline_pleasure, arousal, 0.0)

    # --- expression -----------------------------------------------------------
    def render(self) -> str:
        """One short prompt line describing the current mood (never spoken)."""
        return (
            f"Right now you feel {self._word()} "
            f"(pleasure {self.pleasure:+.1f}, energy {self.arousal:+.1f})."
        )

    def _word(self) -> str:
        p, a = self.pleasure, self.arousal
        if a < -0.4:
            return "drowsy and calm"
        if p > 0.3 and a > 0.3:
            return "bright and lively"
        if p > 0.3:
            return "content"
        if p < -0.3 and a > 0.3:
            return "tense"
        if p < -0.3:
            return "a little down"
        if a > 0.4:
            return "alert"
        return "steady"

    def suggest_emotion(self) -> str:
        """Map the current PAD state to one of the 15 OLED expressions."""
        p, a = self.pleasure, self.arousal
        if a < -0.4:
            return "sleepy"
        if p > 0.4 and a > 0.4:
            return "excited"
        if p > 0.3:
            return "happy"
        if p < -0.4 and a > 0.3:
            return "angry"
        if p < -0.3:
            return "sad"
        if a > 0.5:
            return "surprised"
        if p < -0.1 and a < 0.0:
            return "bored"
        return "neutral"

    # --- persistence ----------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps({
            "pleasure": self.pleasure, "arousal": self.arousal, "dominance": self.dominance,
            "half_life_s": self.half_life_s, "circadian": self.circadian,
            "baseline_pleasure": self.baseline_pleasure,
        })

    @classmethod
    def from_json(cls, s: str) -> "MoodState":
        try:
            d = json.loads(s)
            return cls(
                pleasure=float(d.get("pleasure", 0.0)),
                arousal=float(d.get("arousal", 0.0)),
                dominance=float(d.get("dominance", 0.0)),
                half_life_s=float(d.get("half_life_s", 1800.0)),
                circadian=bool(d.get("circadian", True)),
                baseline_pleasure=float(d.get("baseline_pleasure", 0.1)),
            )
        except Exception:  # noqa: BLE001 - corrupt/empty kv must never crash startup
            return cls()
