"""WorldState: the desktop's single source of runtime truth. Plan §8.3, §5.5.

Holds rolling buffers (transcripts, conversation, last vision/telemetry) plus
the recent-answers buffer — the one piece of state that crosses session
boundaries (loaded from queue.sqlite on startup, kept in sync on each
resolution; see queue.py). Pure in-memory; persistence lives in queue.py.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

RECENT_ANSWERS_MAX = 50
CONVERSATION_MAX = 30
TRANSCRIPTS_MAX = 12
SEE_FRESH_S = 10.0


@dataclass
class ResolvedFact:
    category: str
    topic: str
    resolution: str


@dataclass
class WorldState:
    recent_transcripts: deque[str] = field(default_factory=lambda: deque(maxlen=TRANSCRIPTS_MAX))
    conversation: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=CONVERSATION_MAX))
    recent_answers: deque[ResolvedFact] = field(default_factory=lambda: deque(maxlen=RECENT_ANSWERS_MAX))

    last_see_text: Optional[str] = None
    _last_see_mono: float = field(default=0.0, repr=False)

    last_telemetry: Optional[dict] = None
    motion_goal: Optional[str] = None
    idle_since: float = field(default_factory=time.monotonic)

    # --- transcripts / conversation ------------------------------------------
    def add_transcript(self, text: str) -> None:
        self.recent_transcripts.append(text)
        self.mark_activity()

    def add_user_turn(self, text: str) -> None:
        self.conversation.append(("user", text))
        self.mark_activity()

    def add_assistant_turn(self, text: str) -> None:
        self.conversation.append(("assistant", text))

    def mark_activity(self) -> None:
        self.idle_since = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.idle_since

    # --- perception / telemetry ----------------------------------------------
    def set_vision(self, text: str) -> None:
        self.last_see_text = text
        self._last_see_mono = time.monotonic()

    def fresh_vision(self) -> Optional[str]:
        if self.last_see_text is None:
            return None
        if time.monotonic() - self._last_see_mono > SEE_FRESH_S:
            return None
        return self.last_see_text

    def set_telemetry(self, snap: dict) -> None:
        self.last_telemetry = snap

    # --- recent-answers buffer ------------------------------------------------
    def add_resolution(self, fact: ResolvedFact) -> None:
        self.recent_answers.append(fact)

    def load_resolutions(self, facts: list[ResolvedFact]) -> None:
        """Seed the buffer from persisted history (most-recent last)."""
        self.recent_answers.clear()
        for f in facts[-RECENT_ANSWERS_MAX:]:
            self.recent_answers.append(f)

    # --- prompt rendering -----------------------------------------------------
    def render_recent_answers(self) -> str:
        if not self.recent_answers:
            return "(none yet)"
        return "\n".join(
            f"[{f.category}] {f.topic} -> {f.resolution}" for f in self.recent_answers
        )

    def render_conversation(self, turns: int = 6) -> str:
        recent = list(self.conversation)[-turns:]
        return "\n".join(f"{role}: {text}" for role, text in recent)

    def render_telemetry_line(self) -> str:
        t = self.last_telemetry
        if not t:
            return "(no telemetry)"
        return (
            f"mode={t.get('mode', '?')} "
            f"vel_l={t.get('vel_l', 0):.2f} vel_r={t.get('vel_r', 0):.2f} "
            f"link_age_ms={t.get('link_age_ms', '?')}"
        )
