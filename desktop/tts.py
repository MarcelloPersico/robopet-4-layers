"""Piper TTS: sentence-streaming synthesis to the local speaker. Plan §4, §8.

Piper runs as a short-lived subprocess per sentence (`--output-raw` -> raw int16
PCM on stdout), played via sounddevice. Text is fed incrementally as the agent
streams tokens; complete sentences are synthesized and played in order so audio
overlaps generation (Plan §5.6 step 4).

`sounddevice` is imported lazily so the module imports without PortAudio present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger("tts")

_SENTENCE_END = re.compile(r"(.+?[.!?]+[\"')\]]*\s+)", re.DOTALL)


class TTS:
    def __init__(self, piper_exe: str, voice_model: str, workers: int = 2):
        self.piper_exe = piper_exe
        self.voice_model = voice_model
        self.workers = max(1, workers)
        self.sample_rate = self._read_sample_rate(voice_model)
        self._sentences: asyncio.Queue[str | None] = asyncio.Queue()
        self._pending = ""  # partial sentence buffer
        self._sem = asyncio.Semaphore(self.workers)

    @staticmethod
    def _read_sample_rate(voice_model: str) -> int:
        cfg = Path(str(voice_model) + ".json")
        if cfg.exists():
            try:
                return int(json.loads(cfg.read_text())["audio"]["sample_rate"])
            except Exception:  # noqa: BLE001
                pass
        return 22050  # en_US-amy-medium default

    # --- incremental feeding --------------------------------------------------
    def feed(self, text_chunk: str) -> None:
        """Accumulate streamed text; enqueue any complete sentences."""
        self._pending += text_chunk
        while True:
            m = _SENTENCE_END.match(self._pending)
            if not m:
                break
            sentence = m.group(1).strip()
            self._pending = self._pending[m.end():]
            if sentence:
                self._sentences.put_nowait(sentence)

    def flush(self) -> None:
        """Emit any trailing partial sentence (end of a turn)."""
        tail = self._pending.strip()
        self._pending = ""
        if tail:
            self._sentences.put_nowait(tail)

    async def say(self, text: str) -> None:
        """Convenience: speak a full string now."""
        self.feed(text)
        self.flush()

    # --- player loop ----------------------------------------------------------
    async def run(self) -> None:
        while True:
            sentence = await self._sentences.get()
            if sentence is None:
                continue
            try:
                pcm = await self._synthesize(sentence)
                if pcm:
                    await self._play(pcm)
            except Exception as e:  # noqa: BLE001 - never let TTS crash the loop
                log.warning("tts failure on %r: %s", sentence[:40], e)

    async def _synthesize(self, sentence: str) -> bytes:
        async with self._sem:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._piper, sentence)

    def _piper(self, sentence: str) -> bytes:
        proc = subprocess.run(
            [self.piper_exe, "--model", self.voice_model, "--output-raw"],
            input=sentence.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return proc.stdout

    async def _play(self, pcm: bytes) -> None:
        import sounddevice as sd  # lazy

        audio = np.frombuffer(pcm, dtype=np.int16)
        loop = asyncio.get_running_loop()

        def _blocking() -> None:
            sd.play(audio, self.sample_rate)
            sd.wait()

        await loop.run_in_executor(None, _blocking)
