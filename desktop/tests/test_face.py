"""The dual-OLED "eyes" face path, end-to-end on the desktop side. Plan §3.1.

Covers the new expressivity surface that drives the robot's two procedural-vector
OLED eyes (Anki Vector style) added alongside M1/M2:

  agent / human  --(set_emotion / look tool)-->  RobotTools
      --> Motion.emote()/look()  --> {"type":"face",...} line on the UART channel
      --> Teensy FaceController

The firmware half (EmotionLibrary tween math) is covered by a host g++ test
(teensy/test/test_emotion_logic.cpp). Here we pin the *desktop* contract:

  * Motion.emote()/look() serialize the exact `face` wire JSON, clamp gaze to
    [-1,1], and DROP omitted fields so "absent = keep current" survives the wire.
  * AGENT_TOOL_SPECS exposes set_emotion + look with the locked 15-emotion enum.
  * RobotTools.set_emotion()/look() call through to Motion.
  * The agent's tool dispatch routes both tools.
  * mcp_server.build_server() surfaces both tools to the human's Claude path.

Mirrors the fakes/fixtures style of test_tools.py and test_mcp_server.py.
"""

from __future__ import annotations

import json

import pytest

import mcp_server
from motion import Motion
from state import WorldState
from tools import AGENT_TOOL_SPECS, RobotTools

# The 15 core emotions, byte-identical to the firmware enum (EmotionLibrary.h),
# the tools.py spec, the mcp_server docstring, and persona.md (Plan face spec).
EMOTIONS = [
    "neutral", "happy", "sad", "angry", "surprised", "curious", "sleepy",
    "love", "suspicious", "dizzy", "focused", "scared", "excited", "bored", "wink",
]


# --- fakes -------------------------------------------------------------------
class FakeSink:
    """Captures the raw UART lines Motion serializes (the wire boundary)."""

    def __init__(self, ok: bool = True):
        self.lines: list[str] = []
        self._ok = ok

    async def send_uart(self, line: str) -> bool:
        self.lines.append(line)
        return self._ok

    def objs(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]

    def last(self) -> dict:
        return self.objs()[-1]


class RecordingMotion:
    """A Motion stand-in for the RobotTools layer that records emote()/look()
    calls without serializing (mirrors test_tools.FakeMotion)."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def emote(self, emotion=None, intensity=1.0, look_x=None, look_y=None,
                    blink=False, hold_ms=0):
        self.calls.append(("emote", emotion, intensity, look_x, look_y, blink, hold_ms))
        return True

    async def look(self, x, y):
        self.calls.append(("look", x, y))
        return True

    # tools.RobotTools only touches motion.emote / motion.look for the face path,
    # but give it the rest so it can stand in fully if needed.
    async def drive(self, *a): return True
    async def stop(self): return True
    async def play_animation(self, *a): return True
    async def set_idle_intensity(self, lvl): return True


class _VLM:
    async def describe(self, frame, prompt=None): return "a desk"


class _TTS:
    async def say(self, text): ...


class _Frames:
    def take_latest_frame(self): return b"\xff\xd8jpeg"


class _Notifier:
    async def notify(self, count, last): ...


def _tools_with(motion):
    return RobotTools(motion, _VLM(), _TTS(), None, WorldState(), _Frames(), _Notifier())


def _spec(name: str) -> dict:
    for s in AGENT_TOOL_SPECS:
        if s["function"]["name"] == name:
            return s["function"]
    raise AssertionError(f"{name} not in AGENT_TOOL_SPECS")


# === Motion wire serialization ===============================================
async def test_emote_minimal_emits_type_emotion_intensity():
    """motion.emote('happy') must emit exactly type+emotion+intensity, no gaze
    keys and no hold_ms (omitted = keep current gaze)."""
    sink = FakeSink()
    ok = await Motion(sink).emote("happy")
    assert ok is True
    obj = sink.last()
    assert obj == {"type": "face", "emotion": "happy", "intensity": 1.0}
    # explicit: omitted optional keys are NOT serialized
    assert "look_x" not in obj and "look_y" not in obj
    assert "hold_ms" not in obj and "blink" not in obj


async def test_emote_full_payload_serializes_all_present_fields():
    sink = FakeSink()
    await Motion(sink).emote("angry", intensity=0.5, look_x=0.25, look_y=-0.5,
                             blink=True, hold_ms=1500)
    obj = sink.last()
    assert obj["type"] == "face"
    assert obj["emotion"] == "angry"
    assert obj["intensity"] == 0.5
    assert obj["look_x"] == 0.25 and obj["look_y"] == -0.5
    assert obj["blink"] is True
    assert obj["hold_ms"] == 1500


async def test_emote_drops_none_emotion_so_look_only_keeps_mood():
    """emotion omitted (None) must NOT be serialized — the firmware keeps the
    held mood. Only None-valued keys are dropped, mirroring Motion.configure."""
    sink = FakeSink()
    await Motion(sink).emote(emotion=None, look_x=0.3)
    obj = sink.last()
    assert "emotion" not in obj          # keep current mood
    assert obj["look_x"] == 0.3
    assert obj["type"] == "face"


async def test_emote_clamps_gaze_to_unit_range():
    sink = FakeSink()
    await Motion(sink).emote("curious", look_x=5.0, look_y=-9.0)
    obj = sink.last()
    assert obj["look_x"] == 1.0          # clamped +1
    assert obj["look_y"] == -1.0         # clamped -1


async def test_emote_blink_false_is_omitted_not_sent():
    """blink only goes on the wire when True (one-shot); a default False blink is
    dropped so it doesn't ride along on every emote."""
    sink = FakeSink()
    await Motion(sink).emote("sad")
    assert "blink" not in sink.last()


async def test_emote_hold_zero_is_omitted():
    sink = FakeSink()
    await Motion(sink).emote("happy", hold_ms=0)
    assert "hold_ms" not in sink.last()  # 0 == persist == omit


async def test_look_emits_only_gaze_no_emotion_or_intensity():
    """motion.look(x,y) must emit ONLY type+look_x+look_y (no emotion key, no
    intensity key) so pointing the gaze never wipes the current expression."""
    sink = FakeSink()
    ok = await Motion(sink).look(0.5, -0.25)
    assert ok is True
    obj = sink.last()
    assert obj == {"type": "face", "look_x": 0.5, "look_y": -0.25}
    assert "emotion" not in obj
    assert "intensity" not in obj


async def test_look_clamps_both_axes():
    sink = FakeSink()
    await Motion(sink).look(2.0, -3.0)
    obj = sink.last()
    assert obj["look_x"] == 1.0
    assert obj["look_y"] == -1.0


async def test_face_line_is_compact_json():
    """Same compact separators as every other Motion command (no spaces between
    tokens). Key order is an implementation detail, so compare parsed content."""
    sink = FakeSink()
    await Motion(sink).emote("happy")
    line = sink.lines[-1]
    assert " " not in line                       # compact separators, no spaces
    assert json.loads(line) == {"type": "face", "emotion": "happy", "intensity": 1.0}


async def test_emote_returns_false_when_link_down():
    sink = FakeSink(ok=False)
    assert await Motion(sink).emote("happy") is False


# === Tool specs (the LLM-facing surface) =====================================
def test_agent_specs_expose_set_emotion_and_look():
    names = {s["function"]["name"] for s in AGENT_TOOL_SPECS}
    assert {"set_emotion", "look"} <= names


def test_set_emotion_spec_enum_is_the_15_locked_emotions():
    props = _spec("set_emotion")["parameters"]["properties"]
    assert props["emotion"]["enum"] == EMOTIONS
    assert len(props["emotion"]["enum"]) == 15
    assert _spec("set_emotion")["parameters"]["required"] == ["emotion"]
    # optional gaze + intensity + hold_ms exist on the spec
    for opt in ("intensity", "look_x", "look_y", "hold_ms"):
        assert opt in props


def test_look_spec_requires_x_and_y():
    fn = _spec("look")
    props = fn["parameters"]["properties"]
    assert set(props) == {"x", "y"}
    assert sorted(fn["parameters"]["required"]) == ["x", "y"]


def test_set_emotion_spec_sits_between_set_idle_and_queue_question():
    """Contract: the two face specs are inserted after set_idle_intensity and
    before queue_question."""
    order = [s["function"]["name"] for s in AGENT_TOOL_SPECS]
    assert order.index("set_idle_intensity") < order.index("set_emotion")
    assert order.index("set_emotion") < order.index("look")
    assert order.index("look") < order.index("queue_question")


# === RobotTools methods call through to Motion ===============================
async def test_robottools_set_emotion_calls_motion_emote():
    motion = RecordingMotion()
    tools = _tools_with(motion)
    out = await tools.set_emotion("happy", intensity=0.8, look_x=0.2, look_y=0.1, hold_ms=500)
    assert isinstance(out, str) and "happy" in out
    kind, emotion, intensity, lx, ly, _blink, hold = motion.calls[-1]
    assert kind == "emote"
    assert emotion == "happy" and intensity == 0.8
    assert lx == 0.2 and ly == 0.1 and hold == 500


async def test_robottools_set_emotion_defaults():
    motion = RecordingMotion()
    out = await _tools_with(motion).set_emotion("sad")
    assert "sad" in out
    _, emotion, intensity, lx, ly, _blink, hold = motion.calls[-1]
    assert emotion == "sad" and intensity == 1.0
    assert lx is None and ly is None and hold == 0   # omitted gaze stays None


async def test_robottools_look_calls_motion_look():
    motion = RecordingMotion()
    out = await _tools_with(motion).look(0.5, -0.5)
    assert isinstance(out, str)
    assert motion.calls[-1] == ("look", 0.5, -0.5)


# === Agent tool dispatch routes the two tools ================================
def test_agent_dispatch_registers_face_tools():
    from agent import AgentBrain

    motion = RecordingMotion()
    tools = _tools_with(motion)
    agent = AgentBrain(base_url="http://unused", tools=tools, state=WorldState(),
                       persona_text="persona")
    # Bound methods compare unequal by identity each access; pin them to the same
    # underlying function on the same RobotTools instance instead.
    assert agent._dispatch["set_emotion"].__func__ is RobotTools.set_emotion
    assert agent._dispatch["set_emotion"].__self__ is tools
    assert agent._dispatch["look"].__func__ is RobotTools.look
    assert agent._dispatch["look"].__self__ is tools


# === MCP surface exposes the two tools to the human's Claude =================
@pytest.mark.asyncio
async def test_mcp_build_server_surfaces_face_tools():
    motion = RecordingMotion()
    tools = _tools_with(motion)
    server = mcp_server.build_server(tools)
    names = {t.name for t in await server.list_tools()}
    assert {"set_emotion", "look"} <= names


@pytest.mark.asyncio
async def test_set_emotion_over_mcp_reaches_motion():
    """Driving set_emotion through the MCP wire layer must reach Motion.emote
    (same in-process RobotTools the local agent uses)."""
    motion = RecordingMotion()
    tools = _tools_with(motion)
    server = mcp_server.build_server(tools)
    await server.call_tool("set_emotion", {"emotion": "love", "intensity": 1.0})
    assert any(c[0] == "emote" and c[1] == "love" for c in motion.calls)
