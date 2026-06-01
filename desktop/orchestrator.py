"""Entry point. Asyncio main loop, supervises every other component. Plan §8.

Process model (Plan §8.1): one asyncio process + the llama-server subprocess
(launched here). GPU models (faster-whisper, Moondream2) and blocking I/O run in
the default executor. The MCP HTTP binding runs in-process.

Task graph (Plan §8.2): ws server, audio/control routers, ASR, agent, TTS
player, telemetry, idle, health, MCP. Latency hiding (Plan §5.6): the ack
animation dispatches the instant a transcript finalizes, before the agent loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

import llama_server
from agent import AgentBrain
from asr import ASR
from config import load_config
from motion import Motion
from notifier import Notifier
from pet_queue import QueueDB
from state import WorldState
from tools import RobotTools
from tts import build_tts
from vlm import VLM
from wsserver import WsServer

import mcp_server
import observatory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("orchestrator")

_HERE = Path(__file__).resolve().parent


class Orchestrator:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = WorldState()
        self.queue = QueueDB(_resolve(cfg["queue"]["db_path"]), _resolve(cfg["queue"]["frames_dir"]))

        ws = cfg["wsserver"]
        self.ws = WsServer(ws["host"], ws["port"], ws["ping_interval_s"], ws["ping_timeout_s"])
        self.motion = Motion(self.ws)

        self.asr = ASR(cfg["asr"]["model"], cfg["asr"]["device"], cfg["asr"]["compute_type"],
                       vad_filter=cfg["asr"].get("vad_filter", False))
        self.vision_mode = cfg["vlm"].get("mode", "split")
        self.vlm = VLM(cfg["vlm"]["model"], cfg["vlm"]["device"], cfg["vlm"]["dtype"])
        self.tts = build_tts(cfg["tts"])
        self.notifier = Notifier(
            cfg["notifier"]["backend"], cfg["notifier"]["throttle_seconds"], cfg["notifier"].get("webhook_url", "")
        )
        self.tools = RobotTools(self.motion, self.vlm, self.tts, self.queue, self.state, self.ws,
                                self.notifier, vision_mode=self.vision_mode)

        persona = (_HERE / cfg["paths"]["persona"]).read_text(encoding="utf-8")
        a = cfg["agent"]
        self.agent = AgentBrain(
            base_url=f"http://{a['host']}:{a['port']}",
            tools=self.tools, state=self.state, persona_text=persona,
            model=a.get("model", "local"),
            temperature=a.get("temperature", 0.7),
            stream=a.get("stream", True),
        )
        self._llama_proc: asyncio.subprocess.Process | None = None
        self._busy = asyncio.Lock()  # serialize agent turns / guard idle vs speech
        # When the human resolves a queued question over MCP, make the robot react
        # now instead of waiting for its next utterance (Plan §5.5).
        self.tools.agent_deliver = self._deliver_to_agent

    # --- LLM server -----------------------------------------------------------
    async def _launch_llama(self) -> None:
        a = self.cfg["agent"]
        # Managed mode: spawn & own llama-server. External mode
        # (manage_server=false, e.g. LM Studio): just connect to it.
        if llama_server.manages(a):
            self._llama_proc = await llama_server.launch(a)
        else:
            self._llama_proc = None
            log.info("using external LLM server at %s:%s (not launching one)",
                     a["host"], a["port"])
        await llama_server.wait_ready(f"http://{a['host']}:{a['port']}")

    # --- lifecycle ------------------------------------------------------------
    async def run(self) -> None:
        await self.ws.start()
        # Seed the recent-answers buffer from resolutions shared in earlier
        # sessions (persisted to resolved_knowledge). Without this, the robot
        # re-asks questions a human already answered. Plan §5.5, §8.4.
        seeded = self.queue.load_recent_resolutions(self.state.recent_answers.maxlen or 50)
        self.state.load_resolutions(seeded)
        if seeded:
            log.info("seeded %d resolved answer(s) from the queue", len(seeded))
        log.info("loading models...")
        # In unified-vision mode the agent LLM sees frames itself, so skip Moondream.
        if self.vision_mode == "unified":
            log.info("vision: unified mode — agent LLM sees frames (no separate VLM)")
            await self.asr.load()
        else:
            await asyncio.gather(self.asr.load(), self.vlm.load())
        await self.tts.load()  # pre-warm (no-op for Piper; loads Kokoro's model)
        await self._launch_llama()

        # Push drivetrain config + initial idle intensity to the Teensy.
        await self.motion.set_idle_intensity(self.cfg["idle"].get("default_intensity", 0.6))

        tasks = [
            asyncio.create_task(self._audio_router(), name="audio_router"),
            asyncio.create_task(self._control_router(), name="control_router"),
            asyncio.create_task(self._telemetry_loop(), name="telemetry"),
            asyncio.create_task(self.asr.run(on_final=self._on_utterance), name="asr"),
            asyncio.create_task(self.tts.run(), name="tts"),
            asyncio.create_task(self._idle_loop(), name="idle"),
            asyncio.create_task(self._health_loop(), name="health"),
            asyncio.create_task(self._serve_mcp(), name="mcp"),
            asyncio.create_task(self._serve_dashboard(), name="dashboard"),
        ]
        log.info("orchestrator running (%d tasks)", len(tasks))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await self.aclose()

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self.agent.aclose()
        with contextlib.suppress(Exception):
            await self.ws.close()
        if self._llama_proc and self._llama_proc.returncode is None:
            self._llama_proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._llama_proc.wait(), timeout=5.0)
        self.queue.close()

    # --- routers / loops ------------------------------------------------------
    async def _audio_router(self) -> None:
        while True:
            self.asr.post_audio(await self.ws.audio_in.get())

    async def _control_router(self) -> None:
        while True:
            msg = await self.ws.control_in.get()
            if msg.get("type") == "vad":
                self.asr.post_vad(msg.get("event", ""))

    async def _telemetry_loop(self) -> None:
        import json
        while True:
            line = await self.ws.uart_in.get()
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "telemetry":
                self.state.set_telemetry(obj)
            else:
                # Observatory tap (Plan §11): surface the event/log/pong lines the
                # firmware sends up that aren't telemetry (telemetry has its own
                # tap in state.set_telemetry). No-op when the dashboard is off.
                observatory.emit("teensy", "send", obj.get("type", "event"),
                                 str(obj.get("msg") or obj.get("type") or obj)[:120], obj)

    async def _on_utterance(self, text: str) -> None:
        log.info("user: %s", text)
        self.state.add_transcript(text)
        # Latency hiding (Plan §5.6 step 1): visible reaction within ~300 ms.
        await self.motion.play_animation("perk_up")
        async with self._busy:
            with contextlib.suppress(Exception):
                await self.agent.handle_utterance(text)

    async def _deliver_to_agent(self, topic: str, resolution: str) -> None:
        """A human answered a deferred question over MCP; drive one agent turn so
        the pet reacts (speaks/moves), under the same lock that serializes voice
        turns so it can't collide with the idle loop or a live utterance."""
        async with self._busy:
            with contextlib.suppress(Exception):
                await self.agent.deliver_answer(topic, resolution)
        self.state.mark_activity()

    async def _idle_loop(self) -> None:
        import random
        quiet = self.cfg["idle"].get("no_user_quiet_s", 30)
        while True:
            await asyncio.sleep(5.0)
            if self._busy.locked() or not self.ws.connected:
                continue
            if self.state.idle_seconds() < quiet:
                continue
            roll = random.random()
            async with self._busy:
                if roll < 0.15 and self.ws.video_latest:
                    desc = await self.tools.see()
                    self.state.set_vision(desc)
                elif roll < 0.30:
                    await self.motion.play_animation(random.choice(["nod", "wiggle"]))
                elif roll < 0.40 and self.queue.count_pending() >= self.cfg["queue"].get("mention_threshold", 5):
                    await self.tts.say("i've got a few things i've been wondering about, if you want to take a look.")
            self.state.mark_activity()  # don't fire again immediately

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(10.0)
            if self._llama_proc and self._llama_proc.returncode is not None:
                log.error("llama-server exited (%s); relaunching", self._llama_proc.returncode)
                with contextlib.suppress(Exception):
                    await self._launch_llama()
            log.debug("health: pi=%s pending=%d", self.ws.connected, self.queue.count_pending())

    async def _serve_mcp(self) -> None:
        m = self.cfg["mcp"]
        if not m.get("enable_http", True):
            return
        host = m["http_host"]
        # The HTTP surface can drive motors and read the camera (Plan §3.3,
        # review note #3). It's localhost-only by default; warn loudly if it's
        # exposed to the LAN while the bearer token is still the placeholder.
        if host not in ("127.0.0.1", "localhost", "::1") and \
                m.get("http_bearer_token", "").startswith("change-me"):
            log.warning("MCP HTTP bound to %s with the DEFAULT bearer token — "
                        "set [mcp].http_bearer_token in config.local.toml", host)
        with contextlib.suppress(asyncio.CancelledError):
            await mcp_server.serve_http(self.tools, m["http_host"], m["http_port"])

    async def _serve_dashboard(self) -> None:
        """Read-only "Observatory" dashboard (Plan §11). Off by default: returns
        before importing/binding anything when [dashboard].enable is false, so the
        guarded emit() taps stay pure no-ops and the robot pays zero overhead."""
        d = self.cfg.get("dashboard", {})
        if not d.get("enable", False):
            return
        import dashboard  # lazy: only when explicitly enabled
        obs = observatory.get_observatory()
        obs.configure(
            ring_size=d.get("ring_size", 500),
            frame_max_bytes=d.get("frame_max_bytes", 65536),
            frame_min_interval_s=d.get("frame_min_interval_s", 0.5),
        )
        with contextlib.suppress(asyncio.CancelledError):
            await dashboard.serve_dashboard(
                obs, d.get("host", "127.0.0.1"), d.get("port", 8772),
                replay=d.get("replay", 200), mdns_name=d.get("mdns_name") or None,
            )


def _resolve(path: str) -> str:
    """Resolve a possibly-relative config path against the desktop/ directory."""
    p = Path(path)
    return str(p if p.is_absolute() else _HERE / p)


async def _amain() -> None:
    cfg = load_config()
    orch = Orchestrator(cfg)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    with contextlib.suppress(NotImplementedError):  # add_signal_handler is POSIX-only
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    runner = asyncio.create_task(orch.run())
    done, _ = await asyncio.wait({runner, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
    if runner not in done:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
