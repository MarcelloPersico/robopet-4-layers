"""Background cognitive tick: internal monologue + reflection + mood. Plan §12.

This is what makes the robot feel *alive* between conversations. A timer-driven
loop that — only when the agent is otherwise idle (the ``busy`` lock is free) —
has the robot **think to itself**: perceive its situation, recall relevant
memories, generate a *private* thought (via :meth:`agent.AgentBrain.think`),
update its mood, express that mood on the OLED eyes, and occasionally (gated)
say something out loud. Periodically it **reflects** — synthesizing higher-level
insights from recent memories (Park et al. 2023) — which is how its behavior
changes over time.

Added to the orchestrator task list and gated by ``[cognition].enable``; it
*subsumes* the old random idle loop. It never preempts a user turn: each tick
checks ``busy.locked()`` and bails, so a live utterance always wins the lock.
The loop never crashes the process — every tick is wrapped in try/except.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from mood import MoodState
from observatory import emit

log = logging.getLogger("cognition")


class CognitionEngine:
    def __init__(self, *, agent, state, memory, mood, tools, embedder, busy, cfg: dict):
        self.agent = agent
        self.state = state
        self.memory = memory
        self.mood = mood
        self.tools = tools
        self.embedder = embedder
        self.busy = busy
        self.interval = float(cfg.get("tick_interval_s", 30.0))
        self.jitter = float(cfg.get("jitter_s", 10.0))
        self.speak_cooldown = float(cfg.get("speak_cooldown_s", 180.0))
        self.speak_probability = float(cfg.get("speak_probability", 0.15))
        self.reflect_threshold = float(cfg.get("reflection_importance_threshold", 12.0))
        self.k = int(cfg.get("max_retrieved_memories", 5))
        self.sleep_hours = set(cfg.get("sleep_hours", []) or [])
        self._last_spoke = 0.0  # monotonic
        saved_ts = self.memory.kv_get("last_reflect_ts")
        self._last_reflect_ts = float(saved_ts) if saved_ts else time.time()

    # --- main loop ------------------------------------------------------------
    async def run(self) -> None:
        self._restore_mood()
        while True:
            await asyncio.sleep(self.interval + random.uniform(0.0, self.jitter))
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - cognition must never crash the orchestrator
                log.warning("cognition tick failed", exc_info=True)

    def _restore_mood(self) -> None:
        saved = self.memory.kv_get("mood")
        if not saved:
            return
        s = MoodState.from_json(saved)
        # Restore the affective state but keep this run's configured knobs.
        self.mood.pleasure, self.mood.arousal, self.mood.dominance = (
            s.pleasure, s.arousal, s.dominance,
        )

    # --- one tick -------------------------------------------------------------
    async def _tick(self) -> None:
        if self.busy.locked():
            return  # a user / MCP / idle action owns the agent — skip (no preemption)
        hour = time.localtime().tm_hour
        self.mood.decay(hour=hour)
        if hour in self.sleep_hours:
            await self._sleep_tick()
            return
        async with self.busy:
            perception = self._perceive()
            # Resting expression from current mood; a deliberate emote in think() overrides it.
            await self.tools.set_emotion(self.mood.suggest_emotion(), intensity=0.6)
            mems = await self._recall(perception)
            allow_speak = self._may_speak()
            thought, spoke = await self.agent.think(perception, mems, self.mood, allow_speak)
            if thought:
                await self._store_thought(thought)
            if spoke:
                self._last_spoke = time.monotonic()
                self.mood.update(da=0.05)
            if any(getattr(m, "kind", "") in ("reflection", "resolution") for m in mems):
                self.mood.update(dp=0.03)  # quiet pleasure of recalling something it learned
            emit("lmstudio", "exec", "mood", self.mood.render(),
                 {"p": self.mood.pleasure, "a": self.mood.arousal, "d": self.mood.dominance})
        await self._exec(self.memory.kv_set, "mood", self.mood.to_json())
        await self._maybe_reflect()

    async def _recall(self, perception: str):
        if self.embedder is not None:
            q = await self._exec(self.embedder.encode, perception)
            return await self._exec(self.memory.retrieve, q, None, self.k)
        return await self._exec(self.memory.recent, self.k)

    async def _store_thought(self, thought: str) -> None:
        emit("lmstudio", "exec", "thought", thought[:80], {"thought": thought})
        emb = await self._exec(self.embedder.encode, thought) if self.embedder else None
        await self._exec(self.memory.add, "thought", thought, None, emb, "cognition")

    def _perceive(self) -> str:
        bits = [f"Time: {time.strftime('%H:%M')}."]
        vision = self.state.fresh_vision()
        if vision:
            bits.append(f"You can see: {vision}.")
        bits.append(f"Body: {self.state.render_telemetry_line()}.")
        idle = int(self.state.idle_seconds())
        bits.append(f"No one has spoken for {idle}s." if idle > 30 else "Your human is nearby.")
        return " ".join(bits)

    def _may_speak(self) -> bool:
        if (time.monotonic() - self._last_spoke) < self.speak_cooldown:
            return False
        speaking = getattr(getattr(self.tools, "tts", None), "_speaking", None)
        if speaking is not None and speaking.is_speaking():
            return False
        return random.random() < self.speak_probability

    # --- reflection (the "learning") ------------------------------------------
    async def _maybe_reflect(self) -> None:
        acc = await self._exec(self.memory.accumulated_importance_since, self._last_reflect_ts)
        if acc < self.reflect_threshold:
            return
        if self.busy.locked():
            return
        async with self.busy:
            await self._reflect()
        self._last_reflect_ts = time.time()
        await self._exec(self.memory.kv_set, "last_reflect_ts", str(self._last_reflect_ts))

    async def _reflect(self) -> None:
        recent = await self._exec(self.memory.recent, 50)
        if not recent:
            return
        lines = "\n".join(f"- {m.content}" for m in recent)
        questions_text = await self.agent.complete_text(
            "Here are recent things you've noticed and thought:\n" + lines +
            "\n\nWhat are 2 or 3 high-level questions you could ask about what's going on with "
            "your human or your surroundings? List them, one per line, as plain questions — no "
            "function calls."
        )
        questions = [
            q.strip(" -*0123456789.").strip()
            for q in questions_text.splitlines() if q.strip()
        ][:3]
        for q in questions:
            if not q:
                continue
            support = await self._recall_for(q)
            sup_lines = "\n".join(f"- {m.content}" for m in support)
            insight = await self.agent.complete_text(
                f"Question: {q}\nWhat you know that's relevant:\n{sup_lines}\n\n"
                "In one sentence, what general thing have you learned? Start with 'I've noticed' "
                "or 'It seems'. Keep it short, in plain words — no function calls or quotes."
            )
            if insight:
                emb = await self._exec(self.embedder.encode, insight) if self.embedder else None
                await self._exec(self.memory.add, "reflection", insight, None, emb, "reflect")
                emit("lmstudio", "exec", "reflection", insight[:80], {"text": insight})
        self.mood.update(dp=0.05)  # the small satisfaction of understanding

    async def _recall_for(self, query: str):
        if self.embedder is not None:
            q = await self._exec(self.embedder.encode, query)
            return await self._exec(self.memory.retrieve, q, None, 8)
        return await self._exec(self.memory.recent, 8)

    # --- quiet hours ----------------------------------------------------------
    async def _sleep_tick(self) -> None:
        await self.tools.set_emotion("sleepy", intensity=0.5)
        await self.tools.set_idle_intensity(0.2)

    # --- helper ---------------------------------------------------------------
    async def _exec(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)
