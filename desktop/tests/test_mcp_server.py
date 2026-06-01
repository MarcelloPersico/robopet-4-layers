"""End-to-end tests for the human-in-loop MCP path. Plan §3.3, §5.5, §8.4.

Covers the full "defer to a human, learn the answer back" loop that replaces
automated cloud escalation:

  robot uncertain -> queue_question -> SQLite row + saved frame
    -> human triages over MCP (list / get / resolve)
    -> resolution shared -> recent-answers buffer (live)  AND  resolved_knowledge
    -> on the robot's next start, seeded back into the buffer

The MCP server is a thin wire wrapper over the same RobotTools the local agent
uses (Plan §8.7), so we verify both the registered tool surface and that driving
a tool *through the MCP layer* produces the right side effects on WorldState.
"""

from __future__ import annotations

import asyncio

import pytest

import mcp_server
from pet_queue import QueueDB
from state import WorldState
from tools import RobotTools


# --- fakes (mirror test_tools.py) --------------------------------------------
class _Motion:
    async def drive(self, *a): ...
    async def play_animation(self, *a): ...
    async def stop(self): ...
    async def set_idle_intensity(self, lvl): ...


class _VLM:
    async def describe(self, jpeg): return "a desk with a mug"


class _TTS:
    def __init__(self): self.said = []
    async def say(self, text): self.said.append(text)


class _Frames:
    def __init__(self, frame=b"\xff\xd8jpeg"): self._f = frame
    def take_latest_frame(self): return self._f


class _Notifier:
    async def notify(self, count, last): ...


def _make(tmp_path):
    q = QueueDB(tmp_path / "q.sqlite", tmp_path / "frames")
    st = WorldState()
    tools = RobotTools(_Motion(), _VLM(), _TTS(), q, st, _Frames(), _Notifier())
    return tools, st, q


EXPECTED_TOOLS = {
    "drive", "play_animation", "stop", "see", "speak", "set_idle_intensity",
    "set_emotion", "look",
    "list_pending_questions", "get_pending_question", "next_pending_question",
    "resolve_pending_question", "dismiss_pending_question", "summarize_queue",
}


@pytest.mark.asyncio
async def test_mcp_registers_full_surface(tmp_path):
    tools, _, _ = _make(tmp_path)
    server = mcp_server.build_server(tools)
    names = {t.name for t in await server.list_tools()}
    assert EXPECTED_TOOLS <= names, f"missing: {EXPECTED_TOOLS - names}"
    # queue_question is intentionally NOT on the human MCP surface (Plan §3.3):
    # the human resolves, they don't defer.
    assert "queue_question" not in names


@pytest.mark.asyncio
async def test_resolve_over_mcp_updates_live_robot(tmp_path):
    """Driving resolve_pending_question through the MCP wire layer must update the
    same live WorldState the local agent reads each turn (in-process path)."""
    tools, st, _ = _make(tmp_path)
    server = mcp_server.build_server(tools)

    await tools.queue_question("object_identification", "a mug?", "low confidence",
                               "what is this on my desk?")
    assert "(none yet)" in st.render_recent_answers()

    await server.call_tool(
        "resolve_pending_question",
        {"id": 1, "resolution_text": "it's a blue coffee mug", "share_with_robot": True},
    )
    assert "blue coffee mug" in st.render_recent_answers()


@pytest.mark.asyncio
async def test_list_and_get_over_mcp(tmp_path):
    tools, _, _ = _make(tmp_path)
    server = mcp_server.build_server(tools)
    await tools.queue_question("novelty", "no idea", "never seen this", "what's that?")

    _, listed = await server.call_tool("list_pending_questions", {})
    # FastMCP wraps a list return as {"result": [...]}.
    rows = listed["result"] if isinstance(listed, dict) and "result" in listed else listed
    assert len(rows) == 1 and rows[0]["category"] == "novelty"

    # get_pending_question inlines the saved JPEG frame; the call must succeed and
    # carry content (a text summary plus the image block).
    content, _ = await server.call_tool("get_pending_question", {"id": 1})
    assert content, "get_pending_question returned no content"


@pytest.mark.asyncio
async def test_resolution_persists_and_seeds_on_restart(tmp_path):
    """A shared resolution persists into resolved_knowledge; the orchestrator
    seeds it back into the recent-answers buffer on its next start, so the robot
    doesn't re-ask across restarts (orchestrator.run -> load_recent_resolutions
    -> seed)."""
    tools, st, q = _make(tmp_path)
    await tools.queue_question("object_identification", "a mug?", "low confidence", "what is this?")
    await tools.resolve_pending_question(1, "it's a coffee mug", share_with_robot=True)

    # Simulate a fresh boot: a brand-new WorldState seeded from the queue.
    fresh = WorldState()
    fresh.load_resolutions(q.load_recent_resolutions())
    assert "coffee mug" in fresh.render_recent_answers()


@pytest.mark.asyncio
async def test_next_pending_question_oldest_first(tmp_path):
    """next_pending_question returns the OLDEST pending one (one-at-a-time triage),
    and a plain 'empty' note when the queue drains."""
    tools, _, _ = _make(tmp_path)
    server = mcp_server.build_server(tools)

    await tools.queue_question("novelty", "first guess", "unsure", "first?")
    await tools.queue_question("reasoning", "second guess", "unsure", "second?")

    got = await server.call_tool("next_pending_question", {})
    assert got and got[0], "expected the oldest question, got nothing"

    # Resolve #1 and #2; the queue should then report empty.
    await tools.resolve_pending_question(1, "answer one", share_with_robot=False)
    await tools.resolve_pending_question(2, "answer two", share_with_robot=False)
    got = await server.call_tool("next_pending_question", {})
    assert "empty" in str(got[0]).lower()


@pytest.mark.asyncio
async def test_resolve_pushes_answer_to_live_agent(tmp_path):
    """Sharing a resolution must hand the answer to the live agent so the robot
    reacts now (not just on its next utterance)."""
    tools, _, _ = _make(tmp_path)
    delivered = []

    async def fake_deliver(topic, resolution):
        delivered.append((topic, resolution))

    tools.agent_deliver = fake_deliver
    await tools.queue_question("object_identification", "a mug?", "low confidence", "what is this?")
    await tools.resolve_pending_question(1, "it's a blue mug", share_with_robot=True)
    await asyncio.sleep(0)  # let the fire-and-forget delivery task run
    assert delivered and delivered[0][1] == "it's a blue mug"


@pytest.mark.asyncio
async def test_dismiss_not_shared(tmp_path):
    tools, st, q = _make(tmp_path)
    server = mcp_server.build_server(tools)
    await tools.queue_question("opinion", "maybe", "out of my depth", "do you like it?")
    await server.call_tool("dismiss_pending_question", {"id": 1, "reason": "not useful"})
    assert q.list_pending() == []          # no longer pending
    assert "(none yet)" in st.render_recent_answers()  # nothing taught to the robot
