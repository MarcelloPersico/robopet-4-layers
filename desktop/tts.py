"""Sentence-streaming TTS to the local speaker. Plan §4, §8.

Text is fed incrementally as the agent streams tokens; complete sentences are
synthesized and played in order so audio overlaps generation (Plan §5.6 step 4).
The shared machinery (sentence splitting, the player loop, the half-duplex
speaking gate, the print/echo hook) lives in :class:`BaseTTS`; concrete backends
implement one method, :meth:`_render` (sentence -> int16 PCM bytes):

  * :class:`TTS`        — Piper (CPU subprocess, fast, neutral). The default.
  * :class:`KokoroTTS`  — Kokoro-82M neural voice (tiny GPU/CPU model, far more
                          natural than Piper). Select via ``[tts] backend``.

`sounddevice` and the heavy `kokoro` package are imported lazily so this module
imports without PortAudio / the model present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("tts")

_SENTENCE_END = re.compile(r"(.+?[.!?]+[\"')\]]*\s+)", re.DOTALL)


class BaseTTS:
    """Streaming TTS plumbing shared by all backends. Subclasses implement
    :meth:`_render` to turn one sentence into int16 PCM bytes at ``sample_rate``."""

    def __init__(self, sample_rate: int, workers: int = 2, speaking=None):
        self.sample_rate = sample_rate
        self.workers = max(1, workers)
        self._sentences: asyncio.Queue[str | None] = asyncio.Queue()
        self._pending = ""  # partial sentence buffer
        self._sem = asyncio.Semaphore(self.workers)
        # Optional half-duplex gate: when set, the mic mutes while we play so the
        # speaker output doesn't feed back into ASR (local_loop). See half_duplex.
        self._speaking = speaking

    async def load(self) -> None:
        """Optional pre-warm hook so the first reply isn't delayed by model load.
        No-op for backends with no load step (e.g. Piper)."""

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
                self._enqueue(sentence)

    def flush(self) -> None:
        """Emit any trailing partial sentence (end of a turn)."""
        tail = self._pending.strip()
        self._pending = ""
        if tail:
            self._enqueue(tail)

    def _enqueue(self, sentence: str) -> None:
        """Single path for queuing a sentence to the player; calls the echo hook
        so subclasses (e.g. the printing variants) can print it as produced."""
        self._echo(sentence)
        self._sentences.put_nowait(sentence)

    def _echo(self, sentence: str) -> None:
        """Hook: notified of each sentence as it's queued. No-op by default."""

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
            return await loop.run_in_executor(None, self._render, sentence)

    def _render(self, sentence: str) -> bytes:
        """Backend hook: synthesize one sentence to int16 PCM bytes."""
        raise NotImplementedError

    async def _play(self, pcm: bytes) -> None:
        import sounddevice as sd  # lazy

        audio = np.frombuffer(pcm, dtype=np.int16)
        loop = asyncio.get_running_loop()

        def _blocking() -> None:
            sd.play(audio, self.sample_rate)
            sd.wait()

        if self._speaking is not None:
            self._speaking.enter()
        try:
            await loop.run_in_executor(None, _blocking)
        finally:
            if self._speaking is not None:
                self._speaking.exit()


class TTS(BaseTTS):
    """Piper TTS: a short-lived subprocess per sentence (`--output-raw` -> raw
    int16 PCM on stdout). Fast and CPU-only. Kept as the default backend."""

    def __init__(self, piper_exe: str, voice_model: str, workers: int = 2, speaking=None):
        super().__init__(self._read_sample_rate(voice_model), workers, speaking)
        self.piper_exe = piper_exe
        self.voice_model = voice_model

    @staticmethod
    def _read_sample_rate(voice_model: str) -> int:
        cfg = Path(str(voice_model) + ".json")
        if cfg.exists():
            try:
                return int(json.loads(cfg.read_text())["audio"]["sample_rate"])
            except Exception:  # noqa: BLE001
                pass
        return 22050  # en_US-amy-medium default

    def _render(self, sentence: str) -> bytes:
        proc = subprocess.run(
            [self.piper_exe, "--model", self.voice_model, "--output-raw"],
            input=sentence.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return proc.stdout


class KokoroTTS(BaseTTS):
    """Kokoro-82M neural TTS — much more natural than Piper, tiny (~0.3GB on GPU,
    runs on CPU too). Outputs 24kHz float audio; we convert to int16 PCM. The
    `kokoro` pip package (+ its model) is imported lazily on first use."""

    SAMPLE_RATE = 24000

    def __init__(self, voice: str = "af_heart", lang_code: str = "a",
                 device: str = "cuda", workers: int = 1, speaking=None):
        # 1 worker by default: the torch model isn't safe for concurrent forward
        # passes; a lock guards it regardless.
        super().__init__(self.SAMPLE_RATE, workers, speaking)
        self._voice = voice
        self._lang_code = lang_code
        self._device = device
        self._pipeline = None
        self._lock = threading.Lock()

    def _ensure_pipeline(self):
        if self._pipeline is None:
            from kokoro import KPipeline  # lazy/heavy import
            try:
                self._pipeline = KPipeline(lang_code=self._lang_code, device=self._device)
            except TypeError:  # older kokoro without a device kwarg
                self._pipeline = KPipeline(lang_code=self._lang_code)
        return self._pipeline

    async def load(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ensure_pipeline)
        log.info("Kokoro TTS ready (voice=%s, device=%s)", self._voice, self._device)

    def _render(self, sentence: str) -> bytes:
        pipe = self._ensure_pipeline()
        chunks: list[np.ndarray] = []
        with self._lock:
            for _gs, _ps, audio in pipe(sentence, voice=self._voice):
                arr = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
                chunks.append(arr.astype(np.float32))
        if not chunks:
            return b""
        audio = np.clip(np.concatenate(chunks), -1.0, 1.0)
        return (audio * 32767.0).astype(np.int16).tobytes()


class _EchoPrintMixin:
    """Mixin: print each sentence as it's queued (the console `🗣` line)."""

    def _echo(self, sentence: str) -> None:
        print(f"  🗣  {sentence}")


class PrintingTTS(_EchoPrintMixin, TTS):
    pass


class PrintingKokoroTTS(_EchoPrintMixin, KokoroTTS):
    pass


def build_tts(cfg: dict, speaking=None, echo: bool = False):
    """Construct the TTS backend named by ``cfg['backend']`` ('piper' | 'kokoro').
    ``echo=True`` returns the printing variant (local_loop's console echo)."""
    backend = (cfg.get("backend") or "piper").lower()
    if backend == "kokoro":
        cls = PrintingKokoroTTS if echo else KokoroTTS
        return cls(
            voice=cfg.get("kokoro_voice", "af_heart"),
            lang_code=cfg.get("kokoro_lang", "a"),
            device=cfg.get("kokoro_device", "cuda"),
            workers=cfg.get("workers", 1),
            speaking=speaking,
        )
    cls = PrintingTTS if echo else TTS
    return cls(cfg["piper_exe"], cfg["voice_model"], cfg.get("workers", 2), speaking=speaking)
