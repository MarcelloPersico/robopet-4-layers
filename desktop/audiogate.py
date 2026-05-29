"""VAD gate with pre-roll and hangover. Plan §7.2.

Mirrors the Pi's ``capture._AudioGate`` so local desktop capture (local_io.py)
produces the same clean, ASR-ready utterance bursts the Pi would. Pure logic
(the VAD object is injected), so it's unit-testable without a microphone.
"""

from __future__ import annotations

from collections import deque

import protocol


class AudioGate:
    def __init__(self, vad, sr: int = protocol.AUDIO_SAMPLE_RATE, frame_ms: int = 20,
                 preroll_ms: int = 300, hangover_ms: int = 500):
        self.vad = vad
        self.sr = sr
        self.frame_bytes = int(sr * frame_ms / 1000) * 2  # int16
        self.preroll = deque(maxlen=max(1, preroll_ms // frame_ms))
        self.hangover_frames = max(1, hangover_ms // frame_ms)
        self.active = False
        self._silence = 0

    def feed(self, frame: bytes) -> tuple[str | None, bytes]:
        """Return (event, audio_to_send). event in {None, 'start', 'end'}."""
        speech = self.vad.is_speech(frame, self.sr)
        if not self.active:
            self.preroll.append(frame)
            if speech:
                self.active = True
                self._silence = 0
                burst = b"".join(self.preroll) + frame
                self.preroll.clear()
                return "start", burst
            return None, b""
        if speech:
            self._silence = 0
        else:
            self._silence += 1
            if self._silence >= self.hangover_frames:
                self.active = False
                return "end", b""
        return None, frame
