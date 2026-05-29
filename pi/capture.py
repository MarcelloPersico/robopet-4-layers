"""USB webcam + mic capture. Motion-gated JPEG (channel 0x03) + VAD-gated PCM
(channel 0x02). Plan §7.2.

Runs on the Pi as the `pet-capture` systemd service. Cheap pre-filtering only
(Plan §2.2): frame differencing for motion, webrtcvad for speech. Sends nothing
while disconnected (no buffering across drops). Speech bursts are bracketed by
``{"type":"vad","event":"start|end"}`` control messages with a pre-roll and
hangover so the desktop ASR sees clean utterances.

Heavy libs (cv2, sounddevice, webrtcvad, numpy) are imported in :func:`main` so
the module imports on any machine.
"""

from __future__ import annotations

import asyncio
import logging
import tomllib
from collections import deque
from pathlib import Path

import protocol
from wsclient import run_with_reconnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("capture")


def load_config() -> dict:
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        return tomllib.load(f)


# --- video -------------------------------------------------------------------
async def video_sender(ws, cap, np, cv2, cam: dict) -> None:
    """Motion-gated JPEG sender. A frame is sent on significant motion or at
    least every `keepalive_s`."""
    prev_small = None
    last_sent = 0.0
    period = 1.0 / max(1, cam.get("fps", 15))
    loop = asyncio.get_running_loop()

    while True:
        ok, frame = await loop.run_in_executor(None, cap.read)
        if not ok:
            await asyncio.sleep(period)
            continue

        small = cv2.cvtColor(cv2.resize(frame, (80, 60)), cv2.COLOR_BGR2GRAY)
        moved = True
        if prev_small is not None:
            diff = float(np.mean(cv2.absdiff(small, prev_small)))
            moved = diff >= cam.get("motion_threshold", 8.0)
        prev_small = small

        now = loop.time()
        if moved or (now - last_sent) >= cam.get("keepalive_s", 2.0):
            ok, buf = await loop.run_in_executor(
                None, lambda: cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            )
            if ok:
                await ws.send(protocol.encode_frame(protocol.CH_VIDEO, buf.tobytes()))
                last_sent = now
        await asyncio.sleep(period)


# --- audio -------------------------------------------------------------------
class _AudioGate:
    """VAD state machine with pre-roll and hangover (Plan §7.2)."""

    def __init__(self, vad, sr: int, frame_ms: int, preroll_ms: int, hangover_ms: int):
        self.vad = vad
        self.sr = sr
        self.frame_bytes = int(sr * frame_ms / 1000) * 2  # int16
        self.preroll = deque(maxlen=max(1, preroll_ms // frame_ms))
        self.hangover_frames = max(1, hangover_ms // frame_ms)
        self.active = False
        self._silence = 0

    def feed(self, frame: bytes):
        """Return (event, audio_to_send). event in {None,'start','end'}."""
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
        # active
        if speech:
            self._silence = 0
        else:
            self._silence += 1
            if self._silence >= self.hangover_frames:
                self.active = False
                return "end", b""
        return None, frame


async def audio_sender(ws, stream, vad_mod, cfg_audio: dict) -> None:
    sr = protocol.AUDIO_SAMPLE_RATE
    frame_ms = cfg_audio.get("frame_ms", 20)
    frame_samples = int(sr * frame_ms / 1000)
    gate = _AudioGate(
        vad_mod.Vad(cfg_audio.get("vad_aggressiveness", 2)),
        sr, frame_ms, cfg_audio.get("preroll_ms", 300), cfg_audio.get("hangover_ms", 500),
    )
    loop = asyncio.get_running_loop()

    while True:
        data, _overflow = await loop.run_in_executor(None, stream.read, frame_samples)
        frame = bytes(data)
        if len(frame) < gate.frame_bytes:
            continue
        event, audio = gate.feed(frame)
        if event == "start":
            await ws.send(protocol.encode_control({"type": "vad", "event": "start"}))
            if audio:
                await ws.send(protocol.encode_frame(protocol.CH_AUDIO, audio))
        elif event == "end":
            await ws.send(protocol.encode_control({"type": "vad", "event": "end"}))
        elif audio:
            await ws.send(protocol.encode_frame(protocol.CH_AUDIO, audio))


# --- main --------------------------------------------------------------------
async def main() -> None:
    import cv2  # noqa: F401
    import numpy as np
    import sounddevice as sd
    import webrtcvad

    cfg = load_config()
    cam = cfg["camera"]
    aud = cfg["audio"]
    url = f"ws://{cfg['desktop']['host']}:{cfg['desktop']['port']}"

    cap = cv2.VideoCapture(cam.get("device_index", 0))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cam.get("width", 640))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cam.get("height", 480))
    cap.set(cv2.CAP_PROP_FPS, cam.get("fps", 15))

    frame_samples = int(protocol.AUDIO_SAMPLE_RATE * aud.get("frame_ms", 20) / 1000)
    stream = sd.RawInputStream(
        samplerate=protocol.AUDIO_SAMPLE_RATE, channels=protocol.AUDIO_CHANNELS,
        dtype="int16", blocksize=frame_samples,
    )
    stream.start()
    log.info("camera + mic open; connecting to %s", url)

    async def handler(ws) -> None:
        vt = asyncio.create_task(video_sender(ws, cap, np, cv2, cam))
        at = asyncio.create_task(audio_sender(ws, stream, webrtcvad, aud))
        try:
            done, pending = await asyncio.wait({vt, at}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                t.result()
        finally:
            for t in (vt, at):
                t.cancel()

    try:
        await run_with_reconnect(
            url, handler,
            cfg["desktop"].get("reconnect_min_s", 1.0),
            cfg["desktop"].get("reconnect_max_s", 30.0),
        )
    finally:
        cap.release()
        stream.stop()
        stream.close()


if __name__ == "__main__":
    asyncio.run(main())
