"""Desktop-local I/O for running without the Pi or Teensy. Plan §8.7 (debug).

Replaces the Pi's capture/bridge and the Teensy body with the desktop's own
peripherals so you can talk to the pet brain on one machine:

  * LocalMic    — desktop microphone -> VAD-gated bursts -> ASR (same gating the
                  Pi would do).
  * LocalCamera — desktop webcam -> latest JPEG for the agent's see() tool.
  * NullMotion  — no body: motion commands are echoed to the console as the
                  pet's "body language" instead of driving wheels.

These satisfy the same interfaces the orchestrator wires (ASR ingestion,
FrameSource.take_latest_frame, the Motion command API), so RobotTools and
AgentBrain are reused unchanged. cv2/sounddevice/webrtcvad are imported lazily.
"""

from __future__ import annotations

import asyncio
import logging

import protocol
from audiogate import AudioGate

log = logging.getLogger("local_io")

# Console glyphs for the bodyless pet's movements.
_MOTION_GLYPHS = {
    "perk_up": "( ^_^) *perks up*",
    "nod": "(-.-) *nods*",
    "wiggle": "(~‿~) *wiggles*",
    "spin": "(↻) *spins*",
    "retreat": "(<_<) *backs away*",
}


class NullMotion:
    """Stand-in for motion.Motion when there's no Teensy. Prints intent."""

    async def drive(self, linear: float, angular: float, duration_ms: int = 0) -> bool:
        print(f"  🤖 *moves* (linear={linear}, angular={angular}, {duration_ms}ms)")
        return True

    async def stop(self) -> bool:
        print("  🤖 *stops*")
        return True

    async def play_animation(self, name: str, loops: int = 1) -> bool:
        print(f"  🤖 {_MOTION_GLYPHS.get(name, f'*{name}*')}")
        return True

    async def set_idle_intensity(self, level: float) -> bool:
        log.debug("idle intensity -> %s (no body)", level)
        return True

    async def configure(self, **fields) -> bool:
        return True


class LocalCamera:
    """Desktop webcam -> most-recent JPEG. Satisfies the FrameSource protocol."""

    def __init__(self, device_index: int = 0, width: int = 640, height: int = 480, fps: int = 15):
        self.device_index = device_index
        self.width, self.height, self.fps = width, height, fps
        self._latest: bytes | None = None
        self._cap = None
        self._cv2 = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        import cv2

        self._cv2 = cv2
        # Try the platform default (Media Foundation on Windows) first, then
        # DirectShow as a fallback.
        for backend in (0, getattr(cv2, "CAP_DSHOW", 0)):
            cap = cv2.VideoCapture(self.device_index, backend) if backend else cv2.VideoCapture(self.device_index)
            if cap.isOpened():
                self._cap = cap
                break
            cap.release()
        if self._cap is None:
            raise RuntimeError(
                f"could not open webcam index {self.device_index}. "
                "Check the camera is connected and its driver is healthy "
                "(Device Manager), and that nothing else is using it."
            )
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._cap.set(cv2.CAP_PROP_FPS, self.fps)
        self._task = asyncio.create_task(self._grab_loop())
        log.info("local webcam %d open", self.device_index)

    async def _grab_loop(self) -> None:
        loop = asyncio.get_running_loop()
        period = 1.0 / max(1, self.fps)
        while True:
            ok, frame = await loop.run_in_executor(None, self._cap.read)
            if ok:
                ok2, buf = await loop.run_in_executor(
                    None, lambda: self._cv2.imencode(".jpg", frame, [self._cv2.IMWRITE_JPEG_QUALITY, 80])
                )
                if ok2:
                    self._latest = buf.tobytes()
            await asyncio.sleep(period)

    def take_latest_frame(self) -> bytes | None:
        return self._latest

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._cap:
            self._cap.release()


class _NoFrames:
    """FrameSource for --no-vision runs."""

    def take_latest_frame(self) -> bytes | None:
        return None


class LocalMic:
    """Desktop microphone -> VAD-gated bursts -> ASR ingestion. Plan §7.2 logic."""

    def __init__(self, asr, aggressiveness: int = 2, frame_ms: int = 20):
        self.asr = asr
        self.aggressiveness = aggressiveness
        self.frame_ms = frame_ms
        self._stream = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        import sounddevice as sd
        import webrtcvad

        sr = protocol.AUDIO_SAMPLE_RATE
        frame_samples = int(sr * self.frame_ms / 1000)
        self._stream = sd.RawInputStream(
            samplerate=sr, channels=protocol.AUDIO_CHANNELS, dtype="int16", blocksize=frame_samples
        )
        self._stream.start()
        gate = AudioGate(webrtcvad.Vad(self.aggressiveness), sr, self.frame_ms)
        self._task = asyncio.create_task(self._loop(gate, frame_samples))
        log.info("local microphone open")

    async def _loop(self, gate: AudioGate, frame_samples: int) -> None:
        loop = asyncio.get_running_loop()
        while True:
            data, _overflow = await loop.run_in_executor(None, self._stream.read, frame_samples)
            frame = bytes(data)
            if len(frame) < gate.frame_bytes:
                continue
            event, audio = gate.feed(frame)
            if event == "start":
                self.asr.post_vad("start")
                if audio:
                    self.asr.post_audio(audio)
            elif event == "end":
                self.asr.post_vad("end")
            elif audio:
                self.asr.post_audio(audio)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._stream:
            self._stream.stop()
            self._stream.close()
