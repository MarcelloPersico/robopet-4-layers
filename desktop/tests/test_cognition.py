"""CognitionEngine unit tests: the tick stores a private thought + emotes + emits;
it skips when the busy lock is held; speech is cooldown/probability gated; quiet
hours sleep; and reflection fires past the importance threshold. Plan §12.

Drives _tick()/_maybe_reflect() directly (never the infinite run()) with fakes,
plus a real MemoryStore(tmp_path) and the real MoodState."""

import asyncio
import time

import numpy as np

from cognition import CognitionEngine
from memory import MemoryStore
from mood import MoodState
from observatory import get_observatory


class FakeState:
    def fresh_vision(self):
        return None

    def render_telemetry_line(self):
        return "mode=idle"

    def idle_seconds(self):
        return 120.0


class FakeTools:
    tts = None  # no SpeakingState in this harness → _may_speak skips the speaking guard

    def __init__(self):
        self.calls = []

    async def set_emotion(self, emotion, intensity=1.0, look_x=None, look_y=None, hold_ms=0):
        self.calls.append(("set_emotion", emotion))
        return "ok"

    async def set_idle_intensity(self, level):
        self.calls.append(("idle", level))
        return "ok"


class FakeAgent:
    def __init__(self, thought="a calm, quiet desk", spoke=False):
        self.thought = thought
        self.spoke = spoke
        self.think_calls = []
        self.complete_calls = []

    async def think(self, perception, mems, mood, allow_speak):
        self.think_calls.append((perception, allow_speak))
        return self.thought, self.spoke

    async def complete_text(self, prompt, system=None):
        self.complete_calls.append(prompt)
        if "high-level questions" in prompt:
            return "What is the human up to lately?\nWhy is it so quiet?"
        return "I've noticed the human is around in the afternoons."


class FakeEmbedder:
    def encode(self, text):
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)


def _engine(tmp_path, *, agent=None, tools=None, busy=None, **cfg):
    base = {"speak_probability": 0.0, "reflection_importance_threshold": 1e9,
            "tick_interval_s": 0.0, "jitter_s": 0.0}
    base.update(cfg)
    store = MemoryStore(tmp_path / "m.sqlite")
    engine = CognitionEngine(
        agent=agent or FakeAgent(), state=FakeState(), memory=store, mood=MoodState(),
        tools=tools or FakeTools(), embedder=FakeEmbedder(),
        busy=busy or asyncio.Lock(), cfg=base,
    )
    return engine, store


async def test_tick_stores_thought_emotes_and_emits(tmp_path):
    obs = get_observatory()
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True)
    q = obs.subscribe()
    tools = FakeTools()
    engine, store = _engine(tmp_path, tools=tools)
    try:
        await engine._tick()
        assert store.count() == 1
        assert store.recent(1)[0].kind == "thought"
        assert any(c[0] == "set_emotion" for c in tools.calls)  # mood-driven expression
        kinds = set()
        while not q.empty():
            kinds.add(q.get_nowait()["kind"])
        assert "thought" in kinds and "mood" in kinds  # surfaced to the dashboard
    finally:
        obs.unsubscribe(q)
        obs.configure(enabled=False)
        store.close()


async def test_tick_skips_when_busy(tmp_path):
    busy = asyncio.Lock()
    await busy.acquire()
    tools = FakeTools()
    engine, store = _engine(tmp_path, tools=tools, busy=busy)
    try:
        await engine._tick()
        assert store.count() == 0   # never thought
        assert tools.calls == []    # never emoted
    finally:
        busy.release()
        store.close()


async def test_tick_records_last_spoke_when_it_speaks(tmp_path):
    engine, store = _engine(tmp_path, agent=FakeAgent(thought="hi", spoke=True))
    try:
        assert engine._last_spoke == 0.0
        await engine._tick()
        assert engine._last_spoke > 0.0  # speaking starts the cooldown clock
    finally:
        store.close()


async def test_may_speak_cooldown_and_probability(tmp_path):
    engine, store = _engine(tmp_path)
    try:
        engine.speak_probability = 1.0
        engine._last_spoke = time.monotonic()           # just spoke
        assert engine._may_speak() is False             # within cooldown
        engine._last_spoke = time.monotonic() - 99999   # long ago
        assert engine._may_speak() is True
        engine.speak_probability = 0.0
        assert engine._may_speak() is False             # probability gate
    finally:
        store.close()


async def test_sleep_hours_sleep_and_do_not_think(tmp_path):
    hour = time.localtime().tm_hour
    tools = FakeTools()
    engine, store = _engine(tmp_path, tools=tools, sleep_hours=[hour])
    try:
        await engine._tick()
        assert ("set_emotion", "sleepy") in tools.calls
        assert ("idle", 0.2) in tools.calls
        assert store.count() == 0  # quiet hours: no thinking
    finally:
        store.close()


async def test_reflection_triggers_past_threshold(tmp_path):
    agent = FakeAgent()
    engine, store = _engine(tmp_path, agent=agent, reflection_importance_threshold=0.1)
    try:
        engine._last_reflect_ts = 0.0  # count all memories
        store.add("thought", "earlier idle musing", importance=0.5, source="cognition")
        await engine._maybe_reflect()
        reflections = [m for m in store.recent(50) if m.kind == "reflection"]
        assert reflections, "expected at least one synthesized reflection"
        assert agent.complete_calls  # the salient-questions + insight prompts ran
    finally:
        store.close()


async def test_no_reflection_below_threshold(tmp_path):
    agent = FakeAgent()
    engine, store = _engine(tmp_path, agent=agent, reflection_importance_threshold=1e9)
    try:
        engine._last_reflect_ts = 0.0
        store.add("thought", "tiny", importance=0.1, source="cognition")
        await engine._maybe_reflect()
        assert not [m for m in store.recent(50) if m.kind == "reflection"]
        assert agent.complete_calls == []
    finally:
        store.close()
