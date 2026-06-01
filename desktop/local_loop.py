"""Teensy/Pi-free local runner. Plan §8.7 (debug), §5.

Runs the pet brain on the desktop alone, using the desktop's own mic/webcam/
speaker. Motion is echoed to the console (no body). Reuses RobotTools +
AgentBrain unchanged; only the I/O endpoints differ (see local_io.py).

    python local_loop.py                 # text REPL (type to the pet)
    python local_loop.py --voice         # talk via the desktop mic
    python local_loop.py --voice --vision  # also enable see() via the webcam
    python local_loop.py --no-tts        # don't synthesize speech (print only)
    python local_loop.py --voice --mcp   # + expose the live MCP HTTP server so the
                                         #   human's Claude can drive this same pet

Needs the llama-server + Qwen GGUF from config (the agent brain). Whisper is
only loaded in --voice mode; Moondream + webcam only with --vision.

With --mcp the same RobotTools that the local loop uses are also served over the
MCP HTTP/SSE binding ([mcp] in config) — so a Claude client (the MCP inspector, or
Claude Desktop via the mcp-remote bridge) drives the *live* pet and shares its
WorldState, exactly as in the full orchestrator (Plan §3.3, §8.7).
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
from half_duplex import SpeakingState
from local_io import LocalCamera, LocalMic, NullMotion, _NoFrames
from notifier import Notifier
from pet_queue import QueueDB
from state import WorldState
from tools import RobotTools
from tts import build_tts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("local_loop")
_HERE = Path(__file__).resolve().parent


class ConsoleTTS:
    """Print-only 'speech' for --no-tts runs."""

    async def say(self, text: str) -> None:
        print(f"  🗣  {text}")

    async def run(self) -> None:  # nothing to play
        await asyncio.Event().wait()


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

    # Half-duplex gate: only meaningful when we actually drive a speaker (real
    # TTS) and listen on the same box (--voice). It mutes the mic while the pet
    # talks so its own voice doesn't feed back into ASR.
    speaking = SpeakingState(hangover_s=args.mic_hangover) if (args.voice and not args.no_tts) else None

    tts = ConsoleTTS() if args.no_tts else build_tts(cfg["tts"], speaking=speaking, echo=True)

    vision_mode = cfg.get("vlm", {}).get("mode", "split")
    if args.vision:
        camera = LocalCamera(cfg.get("camera", {}).get("device_index", 0))
        await camera.start()
        frames = camera
        if vision_mode == "unified":
            # The agent LLM does the seeing (must be a multimodal model); no Moondream.
            vlm = _StubVLM()
            log.info("vision: unified mode — the agent LLM sees frames (no separate VLM)")
        else:
            from vlm import VLM
            vlm = VLM(cfg["vlm"]["model"], args.vlm_device, cfg["vlm"]["dtype"])
            await vlm.load()
    else:
        vlm, frames, camera = _StubVLM(), _NoFrames(), None
        vision_mode = "split"  # nothing to attach without a camera

    tools = RobotTools(motion, vlm, tts, queue, state, frames, notifier, vision_mode=vision_mode)
    persona = (_HERE / cfg["paths"]["persona"]).read_text(encoding="utf-8")
    agent = AgentBrain(
        base_url=f"http://{cfg['agent']['host']}:{cfg['agent']['port']}",
        tools=tools, state=state, persona_text=persona,
        model=cfg["agent"].get("model", "local"),
        temperature=cfg["agent"].get("temperature", 0.7),
        stream=cfg["agent"].get("stream", True),
    )

    # Managed mode: we launch llama-server. External mode (manage_server=false,
    # e.g. LM Studio): the server is already running — just connect to it.
    managed = llama_server.manages(cfg["agent"])
    proc = await llama_server.launch(cfg["agent"]) if managed else None
    if not managed:
        log.info("using external LLM server at %s:%s (not launching one)",
                 cfg["agent"]["host"], cfg["agent"]["port"])
    tasks: list[asyncio.Task] = []
    try:
        await llama_server.wait_ready(f"http://{cfg['agent']['host']}:{cfg['agent']['port']}")
        if not args.no_tts:
            await tts.load()  # pre-warm (no-op for Piper; loads Kokoro's model)
            tasks.append(asyncio.create_task(tts.run()))

        # Optionally expose the live MCP HTTP server against this very pet, so a
        # human's Claude can drive the same RobotTools / WorldState the local loop
        # uses (the "invoke Claude live" half of the deferral story). Plan §3.3, §8.7.
        if args.mcp:
            import mcp_server
            m = cfg["mcp"]
            tasks.append(asyncio.create_task(
                mcp_server.serve_http(tools, m["http_host"], m["http_port"]),
                name="mcp_http",
            ))
            log.info("MCP HTTP server on http://%s:%s/mcp — Claude can drive this pet",
                     m["http_host"], m["http_port"])

        async def handle(text: str) -> None:
            print(f"\n🧑 {text}")
            with contextlib.suppress(Exception):
                await agent.handle_utterance(text)

        _banner(args)
        if args.voice:
            from asr import ASR
            device = args.asr_device
            compute = "int8" if device == "cpu" else cfg["asr"]["compute_type"]
            asr = ASR(cfg["asr"]["model"], device, compute,
                      vad_filter=cfg["asr"].get("vad_filter", False))
            await asr.load()
            mic = LocalMic(asr, cfg.get("audio", {}).get("vad_aggressiveness", 2), speaking=speaking)
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
        if proc is not None and proc.returncode is None:  # only if we launched it
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
    if args.mcp:
        extras.append("mcp")
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
    p.add_argument("--mcp", action="store_true",
                   help="also serve the live MCP HTTP binding so Claude can drive this pet")
    p.add_argument("--asr-device", default="cuda", choices=["cpu", "cuda"],
                   help="Whisper device (default cuda; ~0.14s/utterance on the 5070 Ti)")
    p.add_argument("--vlm-device", default="cuda", choices=["cpu", "cuda"],
                   help="Moondream device (default cuda; verified ~0.6s/image on the 5070 Ti)")
    p.add_argument("--mic-hangover", type=float, default=0.6,
                   help="seconds to keep the mic muted after the pet stops talking, so the "
                        "speaker's tail doesn't feed back into ASR (half-duplex; --voice only)")
    args = p.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain(args))


if __name__ == "__main__":
    main()
