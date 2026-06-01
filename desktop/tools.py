"""The robot's tool surface — one canonical implementation. Plan §3.3, §5.1.

Both consumers call these same methods in-process: the local agent loop
(agent.py) and the human's Claude session via the MCP server (mcp_server.py).
The MCP server is a thin wire wrapper around this object; the agent never goes
through the MCP socket (Plan §8.7).

All methods are async and return a short human/LLM-readable string (the MCP
layer adapts richer payloads, e.g. inlining the saved frame for
get_pending_question).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Protocol

log = logging.getLogger("tools")


class FrameSource(Protocol):
    def take_latest_frame(self) -> Optional[bytes]: ...


# OpenAI-compatible function schemas presented to the local LLM (Plan §5.1).
# The queue read/resolve tools are intentionally NOT exposed to the local agent
# (the agent's only queue verb is queue_question); they exist for the human path.
AGENT_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "drive",
            "description": "Drive the robot. linear m/s (forward+), angular rad/s (left+). "
                           "duration_ms 0 means hold until the next command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "linear": {"type": "number"},
                    "angular": {"type": "number"},
                    "duration_ms": {"type": "integer", "default": 0},
                },
                "required": ["linear", "angular"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_animation",
            "description": "Play a named body animation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "enum": ["perk_up", "nod", "wiggle", "spin", "retreat"]},
                    "loops": {"type": "integer", "default": 1},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {"name": "stop", "description": "Stop all motion immediately.",
                     "parameters": {"type": "object", "properties": {}}},
    },
    {
        "type": "function",
        "function": {
            "name": "see",
            "description": "Look through the camera and get a short description of the current view.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": "Say something out loud to the user.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_idle_intensity",
            "description": "Set autonomous idle 'breathing' intensity, 0 (still) to 1 (lively).",
            "parameters": {
                "type": "object",
                "properties": {"level": {"type": "number"}},
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_question",
            "description": "Defer a question you genuinely cannot answer well to the human. "
                           "Use only per the deferral policy; do not queue trivial questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string",
                                 "enum": ["object_identification", "reasoning", "opinion", "novelty"]},
                    "utterance": {"type": "string", "description": "The user's question, if any."},
                    "agent_guess": {"type": "string", "description": "Your best guess."},
                    "why_unsure": {"type": "string", "description": "Why you're deferring."},
                },
                "required": ["category", "agent_guess", "why_unsure"],
            },
        },
    },
]


class RobotTools:
    def __init__(self, motion, vlm, tts, queue, state, frame_source: FrameSource, notifier,
                 vision_mode: str = "split"):
        self.motion = motion
        self.vlm = vlm
        self.tts = tts
        self.queue = queue
        self.state = state
        self.frames = frame_source
        self.notifier = notifier
        # "split"   -> see() captions the frame with the dedicated VLM (Moondream).
        # "unified" -> the agent LLM is itself multimodal; see() stashes the raw
        #              frame and agent.py injects it as an image so the model sees
        #              pixels directly (no separate VLM). See half a dozen lines in
        #              see() + AgentBrain._maybe_attach_images.
        self.vision_mode = vision_mode
        self._pending_images: list[bytes] = []
        # Set by the orchestrator: an ``async (topic, resolution) -> None`` hook
        # that drives one agent turn so the robot *reacts now* to a human's
        # answer (speaks/moves), instead of waiting for the next utterance.
        # None in tests, where there's no live agent. (Plan §5.5)
        self.agent_deliver = None

    async def _exec(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # --- robot tools ----------------------------------------------------------
    async def drive(self, linear: float, angular: float, duration_ms: int = 0) -> str:
        await self.motion.drive(linear, angular, duration_ms)
        return f"driving linear={linear} angular={angular} for {duration_ms}ms"

    async def play_animation(self, name: str, loops: int = 1) -> str:
        await self.motion.play_animation(name, loops)
        return f"playing animation {name} x{loops}"

    async def stop(self) -> str:
        await self.motion.stop()
        return "stopped"

    async def see(self) -> str:
        frame = self.frames.take_latest_frame()
        if not frame:
            return "(no camera frame available)"
        if self.vision_mode == "unified":
            # Hand the raw frame to the multimodal agent LLM instead of captioning.
            # agent.py drains _pending_images and injects them as an image message,
            # so the model sees actual pixels on its next step.
            self._pending_images.append(frame)
            self.state.set_vision("(looking at the live camera now)")
            return "Looking now — the current camera frame is attached below."
        desc = await self.vlm.describe(frame)
        self.state.set_vision(desc)
        return desc

    def take_pending_images(self) -> list[bytes]:
        """Drain frames captured by see() in unified mode (agent.py injects them
        as image messages). Always empty in split mode."""
        imgs = self._pending_images
        self._pending_images = []
        return imgs

    async def speak(self, text: str) -> str:
        await self.tts.say(text)
        self.state.add_assistant_turn(text)
        return "spoke"

    # --- streaming speech (agent.py feeds the speak() text as Gemma generates) -
    @property
    def tts_streamable(self) -> bool:
        """True if the TTS can accept incremental text (feed/flush) so spoken
        sentences start playing before the LLM finishes the full reply."""
        return callable(getattr(self.tts, "feed", None)) and callable(getattr(self.tts, "flush", None))

    def speak_feed(self, chunk: str) -> None:
        fn = getattr(self.tts, "feed", None)
        if fn:
            fn(chunk)  # enqueues + plays each sentence as it completes

    def speak_flush(self) -> None:
        fn = getattr(self.tts, "flush", None)
        if fn:
            fn()  # play any trailing partial sentence at end of a reply

    async def set_idle_intensity(self, level: float) -> str:
        await self.motion.set_idle_intensity(level)
        return f"idle intensity set to {level}"

    async def queue_question(
        self, category: str, agent_guess: str, why_unsure: str, utterance: Optional[str] = None
    ) -> str:
        frame = self.frames.take_latest_frame()
        pose = self.state.last_telemetry or {}
        excerpt = list(self.state.conversation)[-6:]
        qid = await self._exec(
            self.queue.queue_question, category, utterance, agent_guess, why_unsure,
            pose, excerpt, frame,
        )
        count = await self._exec(self.queue.count_pending)
        # Fire-and-forget notification; never block the agent (Plan §5.3 step 2).
        asyncio.create_task(self.notifier.notify(count, utterance or agent_guess))
        log.info("queued question #%d (%s); %d pending", qid, category, count)
        return f"queued question #{qid} for the human ({count} pending)"

    # --- queue tools (human path / MCP) --------------------------------------
    async def list_pending_questions(self, status_filter: str = "pending", limit: int = 20) -> list[dict]:
        return await self._exec(self.queue.list_pending, status_filter, limit)

    async def get_pending_question(self, id: int) -> Optional[dict]:
        return await self._exec(self.queue.get_question, id)

    async def next_pending_question(self) -> Optional[dict]:
        return await self._exec(self.queue.next_pending)

    async def resolve_pending_question(
        self, id: int, resolution_text: str, share_with_robot: bool = True
    ) -> str:
        fact = await self._exec(self.queue.resolve_question, id, resolution_text, share_with_robot)
        if fact is None and not share_with_robot:
            return f"resolved #{id} (not shared with robot)"
        if fact is None:
            return f"no such question #{id}"
        self.state.add_resolution(fact)  # feed the recent-answers buffer (Plan §5.5)
        # Push the answer to the live agent so the robot reacts immediately
        # (speaks it in character, moves if it fits). Fire-and-forget so the MCP
        # caller — the human's Claude — doesn't block on the robot's reaction.
        if self.agent_deliver is not None:
            asyncio.create_task(self.agent_deliver(fact.topic, fact.resolution))
        return f"resolved #{id} and shared with the robot — it will react on its own now"

    async def dismiss_pending_question(self, id: int, reason: str) -> str:
        ok = await self._exec(self.queue.dismiss_question, id, reason)
        return f"dismissed #{id}" if ok else f"no such question #{id}"

    async def summarize_queue(self) -> str:
        return await self._exec(self.queue.summarize_queue)
