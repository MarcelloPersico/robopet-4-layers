"""MCP server exposing the robot + queue tools. Plan §3.3.

One binding, one tool surface (Plan §8.1): an in-process HTTP/SSE server built by
:func:`build_server` against the *live* RobotTools the orchestrator already owns,
so the human's Claude session drives the same robot and the same WorldState as the
local agent. A Claude client (the MCP inspector, or Claude Desktop via the
mcp-remote bridge) connects to it while the orchestrator is running; there is no
offline/queue-only mode.

Targets the official `mcp` Python SDK (FastMCP, mcp>=1.0).
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from observatory import emit, frame_event, get_observatory

log = logging.getLogger("mcp_server")


def _kwargs_summary(name: str, kwargs: dict) -> str:
    """One-line 'name(k=v, ...)' summary for the Observatory dashboard (Plan §11)."""
    inner = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    return f"{name}({inner})"


def _observed():
    """Decorator that taps a FastMCP tool coroutine for the Observatory dashboard
    (Plan §11): emits an ``mcp.recv`` ``mcp:<name>`` before the call and an
    ``mcp.send`` ``mcp-result`` after. Stacked directly BELOW ``@mcp.tool()`` and
    uses ``functools.wraps`` (which sets ``__wrapped__``) so FastMCP's
    ``inspect.signature(fn, eval_str=True)`` still sees the *original* signature
    and the introspected JSON schema is unchanged — guarded by
    ``test_mcp_registers_full_surface``. All no-op when the dashboard is disabled;
    this is the MCP-originated tool path, kept distinct from the agent's.
    """

    def deco(fn):
        name = fn.__name__

        @functools.wraps(fn)
        async def wrapper(**kwargs):
            on = get_observatory().enabled  # cheap; keeps the wrapper ~free when off
            if on:
                emit("mcp", "recv", "mcp:" + name, _kwargs_summary(name, kwargs), dict(kwargs))
            result = await fn(**kwargs)
            if on:
                brief = result if isinstance(result, str) else _brief(result)
                emit("mcp", "send", "mcp-result", brief[:80], {"result": brief})
            return result

        return wrapper

    return deco


def _brief(result: Any) -> str:
    """Short string form of a tool result for the dashboard (lists/records/etc.)."""
    if isinstance(result, list):
        return f"[{len(result)} item(s)]"
    return str(result)[:120]

# Surfaced to the human's Claude client so triage is a tight loop, not free-form
# reasoning. The robot reacts to a shared resolution on its own (speaks + moves),
# so the human's job is just: read the next question, answer it, resolve it.
INSTRUCTIONS = (
    "You triage a robot desk pet's queue of questions it couldn't answer on its "
    "own. Keep it tight: minimal tool calls, no narration, no thinking out loud.\n"
    "\n"
    "The loop is:\n"
    "1. Call `next_pending_question` to pull the oldest one. Its record already "
    "includes the camera frame the robot saved when it asked, so you normally do "
    "NOT need to look again.\n"
    "2. Answer it directly and briefly. Only call `see` if the question is about "
    "what the robot is looking at *right now* rather than the saved frame.\n"
    "3. Call `resolve_pending_question` with a short, plain-language answer and "
    "share_with_robot=true. That hands the answer to the robot, which then speaks "
    "it in its own voice and moves if it fits. Do NOT call `speak`, `drive`, or "
    "`play_animation` yourself — acting is the robot's job; you just supply the "
    "answer.\n"
    "4. Repeat from step 1 until the queue is empty.\n"
    "\n"
    "Dismiss a question only when it's junk or genuinely unanswerable. A short "
    "honest answer is better than a long one."
)


def build_server(tools, name: str = "robot-desk-pet"):
    """Register the full tool surface against a live RobotTools object."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name, instructions=INSTRUCTIONS)

    # --- robot tools (also let the human drive the pet from chat for demos) ---
    # @_observed() (Plan §11) sits BELOW @mcp.tool() so FastMCP registers the
    # wrapped coroutine; functools.wraps keeps the introspected schema intact.
    @mcp.tool()
    @_observed()
    async def drive(linear: float, angular: float, duration_ms: int = 0) -> str:
        """Drive the robot: linear m/s (forward+), angular rad/s (left+)."""
        return await tools.drive(linear, angular, duration_ms)

    @mcp.tool()
    @_observed()
    async def play_animation(name: str, loops: int = 1) -> str:
        """Play a named animation (perk_up, nod, wiggle, spin, retreat)."""
        return await tools.play_animation(name, loops)

    @mcp.tool()
    @_observed()
    async def stop() -> str:
        """Stop all motion immediately."""
        return await tools.stop()

    @mcp.tool()
    @_observed()
    async def see() -> str:
        """Describe what the robot's camera currently sees."""
        return await tools.see()

    @mcp.tool()
    @_observed()
    async def speak(text: str) -> str:
        """Make the robot say something out loud."""
        return await tools.speak(text)

    @mcp.tool()
    @_observed()
    async def set_idle_intensity(level: float) -> str:
        """Set idle 'breathing' intensity, 0 (still) to 1 (lively)."""
        return await tools.set_idle_intensity(level)

    @mcp.tool()
    @_observed()
    async def set_emotion(
        emotion: str, intensity: float = 1.0,
        look_x: float | None = None, look_y: float | None = None, hold_ms: int = 0,
    ) -> str:
        """Set the robot's two OLED eyes. emotion is one of: neutral, happy, sad,
        angry, surprised, curious, sleepy, love, suspicious, dizzy, focused,
        scared, excited, bored, wink. intensity 0..1 (default 1.0); optional
        look_x/look_y gaze in [-1,1]; hold_ms>0 reverts to neutral after that long."""
        return await tools.set_emotion(emotion, intensity, look_x, look_y, hold_ms)

    @mcp.tool()
    @_observed()
    async def look(x: float, y: float) -> str:
        """Point the robot's eyes' gaze. x,y in [-1,1] (x: -1 left .. +1 right;
        y: -1 down .. +1 up). Keeps the current expression."""
        return await tools.look(x, y)

    _register_queue_tools(mcp, tools)
    return mcp


def _register_queue_tools(mcp, tools) -> None:
    """Queue triage tools — the meaningful surface for the human path (Plan §3.3)."""

    @mcp.tool()
    @_observed()
    async def next_pending_question() -> Any:
        """Start here. The oldest unanswered question, with the camera frame the
        robot saved when it asked already inlined. Returns a 'queue empty' note
        when nothing is pending."""
        rec = await tools.next_pending_question()
        if not rec:
            return "The queue is empty — no pending questions."
        _emit_queued_frame(rec)
        return _payload_with_frame(rec)

    @mcp.tool()
    @_observed()
    async def list_pending_questions(status_filter: str = "pending", limit: int = 20) -> list[dict]:
        """List queued questions: id, ts, category, utterance, agent_guess, status."""
        return await tools.list_pending_questions(status_filter, limit)

    @mcp.tool()
    @_observed()
    async def get_pending_question(id: int) -> Any:
        """Full record for one specific question, including the saved camera frame."""
        rec = await tools.get_pending_question(id)
        if not rec:
            return f"no such question #{id}"
        _emit_queued_frame(rec)
        return _payload_with_frame(rec)

    @mcp.tool()
    @_observed()
    async def resolve_pending_question(
        id: int, resolution_text: str, share_with_robot: bool = True
    ) -> str:
        """Answer a question. With share_with_robot=true the robot is handed the
        answer and reacts on its own — it speaks it in its own voice and moves if
        it fits, so you don't need to call speak/drive yourself."""
        result = await tools.resolve_pending_question(id, resolution_text, share_with_robot)
        # Observatory exec tap (Plan §11): the human's resolution that the frontend
        # connects MCP -> LM Studio ("learn answers back"). No-op when off.
        emit("mcp", "exec", "resolve+share", f"#{id}: {resolution_text[:60]}",
             {"id": id, "topic": resolution_text[:80], "resolution": resolution_text})
        return result

    @mcp.tool()
    @_observed()
    async def dismiss_pending_question(id: int, reason: str) -> str:
        """Dismiss a question without resolving it."""
        return await tools.dismiss_pending_question(id, reason)

    @mcp.tool()
    @_observed()
    async def summarize_queue() -> str:
        """One-line natural-language summary of the pending queue."""
        return await tools.summarize_queue()


def _emit_queued_frame(rec: dict) -> None:
    """Observatory tap (Plan §11): if the question has a saved camera frame on
    disk, emit it as an ``mcp.send`` ``queued-frame`` thumbnail (the frame the
    robot saw when it deferred). No-op when the dashboard is disabled; never
    raises (a missing/oversized frame just yields no preview)."""
    if not get_observatory().enabled:
        return
    path = rec.get("frame_abspath")
    if not path:
        return
    try:
        from pathlib import Path

        data = Path(path).read_bytes()
    except OSError:
        return
    frame_event("mcp", "send", "queued-frame", f"frame for #{rec.get('id')}",
                data, key="mcp_frame")


def _payload_with_frame(rec: dict) -> Any:
    """Text summary + the saved camera frame inlined as an image, when present;
    falls back to the raw record if the image can't be loaded."""
    frame_path = rec.get("frame_abspath")
    if frame_path:
        try:
            from mcp.server.fastmcp import Image

            return [_summarize(rec), Image(path=frame_path)]
        except Exception as e:  # noqa: BLE001 - fall back to text-only
            log.debug("image inline failed: %s", e)
    return rec


def _summarize(rec: dict) -> str:
    return (
        f"Question #{rec['id']} [{rec['category']}] ({rec['status']})\n"
        f"User said: {rec.get('utterance') or '(agent-initiated)'}\n"
        f"Robot's guess: {rec.get('agent_guess')}\n"
        f"Why unsure: {rec.get('why_unsure')}\n"
        f"Pose: {rec.get('pose')}"
    )


async def serve_http(tools, host: str, port: int) -> None:
    """Run the HTTP/SSE binding in-process inside the orchestrator's loop."""
    mcp = build_server(tools)
    mcp.settings.host = host
    mcp.settings.port = port
    # FastMCP's async runner for the streamable-HTTP transport.
    await mcp.run_streamable_http_async()
