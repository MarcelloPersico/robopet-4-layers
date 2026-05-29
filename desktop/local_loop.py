"""Teensy/Pi-free local runner. Plan §8.7 (debug), §5.

Runs the pet brain on the desktop alone, using the desktop's own mic/webcam/
speaker. Motion is echoed to the console (no body). Reuses RobotTools +
AgentBrain unchanged; only the I/O endpoints differ (see local_io.py).

    python local_loop.py                 # text REPL (type to the pet)
    python local_loop.py --voice         # talk via the desktop mic
    python local_loop.py --voice --vision  # also enable see() via the webcam
    python local_loop.py --no-tts        # don't synthesize speech (print only)

Needs the llama-server + Qwen GGUF from config (the agent brain). Whisper is
only loaded in --voice mode; Moondream + webcam only with --vision.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from pathlib import Path

import llama_server
from agent import AgentBrain
from config import load_config
from local_io import LocalCamera, LocalMic, NullMotion, _NoFrames
from notifier import Notifier
from pet_queue import QueueDB
from state import WorldState
from tools import RobotTools
from tts import TTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("local_loop")
_HERE = Path(__file__).resolve().parent


class ConsoleTTS:
    """Print-only 'speech' for --no-tts runs."""

    async def say(self, text: str) -> None:
        print(f"  🗣  {text}")

    async def run(self) -> None:  # nothing to play
        await asyncio.Event().wait()


class PrintingTTS(TTS):
    """Real Piper TTS that also prints what the pet says."""

    async def say(self, text: str) -> None:
        print(f"  🗣  {text}")
        await super().say(text)


class _StubVLM:
    async def describe(self, frame, prompt=None) -> str:
        return "(vision disabled)"


def _resolve(path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else _HERE / p)


async def amain(args: argparse.Namespace) -> None:
    cfg = load_config()

    queue = QueueDB(_resolve(cfg["queue"]["db_path"]), _resolve(cfg["queue"]["frames_dir"]))
    state = WorldState()
    state.load_resolutions(queue.load_recent_resolutions())

    motion = NullMotion()
    notifier = Notifier(cfg["notifier"]["backend"], cfg["notifier"]["throttle_seconds"],
                        cfg["notifier"].get("webhook_url", ""))

    tts = ConsoleTTS() if args.no_tts else PrintingTTS(
        cfg["tts"]["piper_exe"], cfg["tts"]["voice_model"], cfg["tts"].get("workers", 2)
    )

    if args.vision:
        from vlm import VLM
        vlm = VLM(cfg["vlm"]["model"], args.vlm_device, cfg["vlm"]["dtype"])
        await vlm.load()
        camera = LocalCamera(cfg.get("camera", {}).get("device_index", 0))
        await camera.start()
        frames = camera
    else:
        vlm, frames, camera = _StubVLM(), _NoFrames(), None

    tools = RobotTools(motion, vlm, tts, queue, state, frames, notifier)
    persona = (_HERE / cfg["paths"]["persona"]).read_text(encoding="utf-8")
    agent = AgentBrain(
        base_url=f"http://{cfg['agent']['host']}:{cfg['agent']['port']}",
        tools=tools, state=state, persona_text=persona,
        temperature=cfg["agent"].get("temperature", 0.7),
    )

    proc = await llama_server.launch(cfg["agent"])
    tasks: list[asyncio.Task] = []
    try:
        await llama_server.wait_ready(f"http://{cfg['agent']['host']}:{cfg['agent']['port']}")
        if not args.no_tts:
            tasks.append(asyncio.create_task(tts.run()))

        async def handle(text: str) -> None:
            print(f"\n🧑 {text}")
            with contextlib.suppress(Exception):
                await agent.handle_utterance(text)

        _banner(args)
        if args.voice:
            from asr import ASR
            device = args.asr_device
            compute = "int8" if device == "cpu" else cfg["asr"]["compute_type"]
            asr = ASR(cfg["asr"]["model"], device, compute)
            await asr.load()
            mic = LocalMic(asr, cfg.get("audio", {}).get("vad_aggressiveness", 2))
            await mic.start()
            print("🎙  listening — speak into your mic (Ctrl+C to quit)\n")
            await asr.run(on_final=handle)
        else:
            await _text_repl(handle)
    finally:
        for t in tasks:
            t.cancel()
        if camera:
            await camera.stop()
        await agent.aclose()
        if proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
        queue.close()


async def _text_repl(handle) -> None:
    loop = asyncio.get_running_loop()
    print("💬 text mode — type to the pet, 'quit' to exit\n")
    while True:
        line = await loop.run_in_executor(None, lambda: input("you> "))
        if line.strip().lower() in {"quit", "exit", ":q"}:
            break
        if line.strip():
            await handle(line)


def _banner(args: argparse.Namespace) -> None:
    mode = "voice" if args.voice else "text"
    extras = []
    if args.vision:
        extras.append("vision")
    if args.no_tts:
        extras.append("no-tts")
    print(f"\n=== robot desk pet — local loop ({mode}{', ' + ', '.join(extras) if extras else ''}) ===")
    print("    (no Teensy: movements are printed; no Pi: using desktop mic/webcam)\n")


def main() -> None:
    import sys
    # The pet's console "body language" uses emoji; Windows consoles default to
    # cp1252 and would crash on them. Emit UTF-8 (replace anything unrenderable).
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description="Run the pet brain locally (no Pi/Teensy).")
    p.add_argument("--voice", action="store_true", help="use the desktop microphone (else text REPL)")
    p.add_argument("--vision", action="store_true", help="enable see() via the desktop webcam (loads Moondream)")
    p.add_argument("--no-tts", action="store_true", help="print speech instead of synthesizing it")
    p.add_argument("--asr-device", default="cpu", choices=["cpu", "cuda"], help="Whisper device (default cpu)")
    p.add_argument("--vlm-device", default="cpu", choices=["cpu", "cuda"], help="Moondream device (default cpu)")
    args = p.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain(args))


if __name__ == "__main__":
    main()
