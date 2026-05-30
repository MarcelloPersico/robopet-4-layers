"""Half-duplex speak/listen arbitration. Plan §8.7 (local debug loop).

Without acoustic echo cancellation, the desktop speaker's output leaks into the
desktop microphone: Piper says a sentence, the mic hears it, Whisper transcribes
it, and the pet answers itself in a loop. The cheap, robust fix for a single-box
debug loop is to go *half-duplex* — the mic stops listening while the pet is
talking, and for a short ``hangover`` afterwards so the speaker's acoustic tail
(and room reverb) doesn't trip the VAD the instant playback ends.

``SpeakingState`` is the shared flag between :class:`tts.TTS` (which marks itself
speaking around each playback) and :class:`local_io.LocalMic` (which drops input
while it's set). It's refcounted so overlapping sentence playbacks nest cleanly,
and ``time.monotonic`` based so it needs no event loop.
"""

from __future__ import annotations

import time


class SpeakingState:
    """Refcounted 'the pet is talking' flag with a release hangover."""

    def __init__(self, hangover_s: float = 0.6):
        self.hangover_s = max(0.0, hangover_s)
        self._depth = 0
        self._released_at = 0.0  # monotonic time the last playback ended

    def enter(self) -> None:
        """Mark a playback as started (nestable)."""
        self._depth += 1

    def exit(self) -> None:
        """Mark a playback as finished; starts the hangover countdown."""
        self._depth = max(0, self._depth - 1)
        if self._depth == 0:
            self._released_at = time.monotonic()

    def is_speaking(self) -> bool:
        """True while playing, or within ``hangover_s`` of the last playback."""
        if self._depth > 0:
            return True
        return (time.monotonic() - self._released_at) < self.hangover_s
