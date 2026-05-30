"""faster-whisper wrapper: streaming partial + final transcripts. Plan §4, §8.

Audio arrives already VAD-gated from the Pi (Plan §7.2): a burst of 16 kHz mono
int16 PCM bracketed by ``vad start`` / ``vad end`` control events. This class
buffers a burst, emits interim *partials* on a timer, and a *final* transcript
when the burst ends.

The heavy `faster_whisper` import is deferred to :meth:`load` so the module can
be imported (and unit-tested) on a machine without the model or CUDA.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import numpy as np

import protocol

log = logging.getLogger("asr")


def _add_cuda_dll_dirs() -> None:
    """Make ctranslate2 (CUDA-12 build) find cuBLAS/cuDNN on Windows.

    ctranslate2's GPU build links the CUDA 12 runtime (cublas64_12.dll) and
    cuDNN 9, which we provide via the `nvidia-cublas-cu12` / `nvidia-cudnn-cu12`
    pip packages rather than a full CUDA toolkit. Their DLL dirs aren't on PATH,
    so register them before faster_whisper imports ctranslate2. Best-effort and
    Windows-only (no-op elsewhere / if the packages are absent)."""
    if not hasattr(os, "add_dll_directory"):
        return
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            mod = importlib.import_module(pkg)
            bin_dir = os.path.join(list(mod.__path__)[0], "bin")
            if os.path.isdir(bin_dir):
                os.add_dll_directory(bin_dir)
        except Exception as e:  # noqa: BLE001 - fall back to CPU/system libs
            log.debug("could not register CUDA dll dir for %s: %s", pkg, e)

PARTIAL_INTERVAL_S = 0.7
MIN_PARTIAL_SAMPLES = protocol.AUDIO_SAMPLE_RATE // 2  # 0.5 s

# --- anti-hallucination thresholds ------------------------------------------
# Whisper invents canned phrases ("Thank you.", "you") when fed silence/noise.
# We guard three ways: a cheap energy gate before the model, and two
# per-segment confidence filters after it (faster-whisper exposes both).
SILENCE_RMS = 0.006        # clips below this RMS (normalized) are treated as silence
MAX_NO_SPEECH_PROB = 0.6   # drop segments Whisper itself flags as non-speech
MIN_AVG_LOGPROB = -1.0     # drop very low-confidence (usually hallucinated) text
# Common bare hallucinations on silence; dropped if they're the whole transcript.
_HALLUCINATIONS = {
    "you", "thank you.", "thank you", "thanks for watching!", "thanks for watching.",
    "bye.", ".", "so", "the", "i'm sorry.",
}


@dataclass
class _Event:
    kind: str  # "start" | "audio" | "end"
    pcm: bytes = b""


class ASR:
    def __init__(self, model: str, device: str = "cuda", compute_type: str = "int8_float16",
                 vad_filter: bool = False):
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        # faster-whisper's own (Silero) VAD trims non-speech inside the final
        # clip — a strong extra hallucination guard for local desktop capture.
        self._vad_filter = vad_filter
        self._model = None
        self.events: asyncio.Queue[_Event] = asyncio.Queue(maxsize=400)

    async def load(self) -> None:
        if self._device.startswith("cuda"):
            _add_cuda_dll_dirs()
        from faster_whisper import WhisperModel  # lazy

        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(
            None,
            lambda: WhisperModel(self._model_name, device=self._device, compute_type=self._compute_type),
        )
        log.info("ASR model loaded: %s (%s/%s)", self._model_name, self._device, self._compute_type)

    # --- ingestion (called by the orchestrator) ------------------------------
    def post_audio(self, pcm: bytes) -> None:
        self._offer(_Event("audio", pcm))

    def post_vad(self, event: str) -> None:
        if event == "start":
            self._offer(_Event("start"))
        elif event == "end":
            self._offer(_Event("end"))

    def _offer(self, ev: _Event) -> None:
        if self.events.full():
            try:
                self.events.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self.events.put_nowait(ev)
        except asyncio.QueueFull:
            pass

    # --- main loop -----------------------------------------------------------
    async def run(
        self,
        on_partial: Optional[Callable[[str], Awaitable[None]]] = None,
        on_final: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        buf = bytearray()
        active = False
        last_partial = 0.0

        while True:
            ev = await self.events.get()
            if ev.kind == "start":
                buf.clear()
                active = True
                last_partial = time.monotonic()
            elif ev.kind == "audio" and active:
                buf.extend(ev.pcm)
                now = time.monotonic()
                if (
                    on_partial
                    and now - last_partial >= PARTIAL_INTERVAL_S
                    and len(buf) >= MIN_PARTIAL_SAMPLES * 2
                ):
                    last_partial = now
                    text = await self._transcribe(bytes(buf), partial=True)
                    if text:
                        await on_partial(text)
            elif ev.kind == "end" and active:
                active = False
                if len(buf) >= MIN_PARTIAL_SAMPLES:
                    text = await self._transcribe(bytes(buf), partial=False)
                    if text and on_final:
                        await on_final(text)
                buf.clear()

    @staticmethod
    def _too_quiet(audio: "np.ndarray") -> bool:
        if audio.size == 0:
            return True
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        return rms < SILENCE_RMS

    async def _transcribe(self, pcm: bytes, partial: bool) -> str:
        if self._model is None:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        # Energy gate: skip the model entirely on near-silent clips (VAD false
        # triggers on mic hiss) — the cheapest way to avoid a hallucinated reply.
        if self._too_quiet(audio):
            return ""
        loop = asyncio.get_running_loop()
        return (await loop.run_in_executor(None, self._run_model, audio, partial)).strip()

    def _run_model(self, audio: "np.ndarray", partial: bool) -> str:
        beam = 1 if partial else 5
        segments, _info = self._model.transcribe(
            audio, language="en", beam_size=beam,
            # Silero VAD trims internal non-speech on the final pass (not partials,
            # to keep them cheap). condition_on_previous_text=False + temperature 0
            # stop the model seeding/inventing text on weak audio.
            vad_filter=self._vad_filter and not partial,
            condition_on_previous_text=False,
            temperature=0.0,
            no_speech_threshold=MAX_NO_SPEECH_PROB,
            log_prob_threshold=MIN_AVG_LOGPROB,
        )
        kept = [
            seg.text for seg in segments
            if getattr(seg, "no_speech_prob", 0.0) < MAX_NO_SPEECH_PROB
            and getattr(seg, "avg_logprob", 0.0) > MIN_AVG_LOGPROB
        ]
        text = " ".join(kept).strip()
        # Drop a transcript that is *only* a known silence-hallucination phrase.
        if text.lower() in _HALLUCINATIONS:
            return ""
        return text
