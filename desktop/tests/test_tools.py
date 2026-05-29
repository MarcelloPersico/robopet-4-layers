import asyncio

import pytest

from pet_queue import QueueDB
from state import WorldState
from tools import RobotTools


class FakeMotion:
    def __init__(self):
        self.calls = []

    async def drive(self, *a):
        self.calls.append(("drive", a))
        return True

    async def stop(self):
        self.calls.append(("stop",))
        return True

    async def play_animation(self, name, loops=1):
        self.calls.append(("play", name, loops))
        return True

    async def set_idle_intensity(self, level):
        self.calls.append(("idle", level))
        return True


class FakeVLM:
    async def describe(self, frame, prompt=None):
        return "a green plant"


class FakeTTS:
    def __init__(self):
        self.said = []

    async def say(self, text):
        self.said.append(text)


class FakeFrames:
    def __init__(self, frame):
        self.frame = frame

    def take_latest_frame(self):
        return self.frame


class FakeNotifier:
    def __init__(self):
        self.notified = []

    async def notify(self, count, last):
        self.notified.append((count, last))
        return True


@pytest.fixture
def kit(tmp_path):
    db = QueueDB(tmp_path / "q.sqlite", tmp_path / "frames")
    state = WorldState()
    motion, vlm, tts, notifier = FakeMotion(), FakeVLM(), FakeTTS(), FakeNotifier()
    frames = FakeFrames(b"\xff\xd8jpeg")
    tools = RobotTools(motion, vlm, tts, db, state, frames, notifier)
    yield tools, db, state, motion, tts, notifier
    db.close()


async def test_queue_question_writes_and_notifies(kit):
    tools, db, state, _motion, _tts, notifier = kit
    msg = await tools.queue_question(
        category="object_identification", agent_guess="a plant",
        why_unsure="low confidence", utterance="what's that?",
    )
    assert "queued question #1" in msg
    assert db.count_pending() == 1
    await asyncio.sleep(0)  # let the fire-and-forget notify task run
    assert notifier.notified and notifier.notified[0][0] == 1


async def test_resolve_pushes_to_recent_answers(kit):
    tools, db, state, *_ = kit
    qid = db.queue_question("reasoning", "why?", "guess", "hard")
    out = await tools.resolve_pending_question(qid, "the answer", share_with_robot=True)
    assert "shared with the robot" in out
    assert len(state.recent_answers) == 1
    assert state.recent_answers[0].resolution == "the answer"


async def test_see_updates_vision(kit):
    tools, _db, state, *_ = kit
    desc = await tools.see()
    assert desc == "a green plant"
    assert state.fresh_vision() == "a green plant"


async def test_see_no_frame():
    tools = RobotTools(FakeMotion(), FakeVLM(), FakeTTS(), None, WorldState(), FakeFrames(None), FakeNotifier())
    assert "no camera frame" in await tools.see()


async def test_speak_records_turn(kit):
    tools, _db, state, _motion, tts, _ = kit
    await tools.speak("hello")
    assert tts.said == ["hello"]
    assert state.conversation[-1] == ("assistant", "hello")
