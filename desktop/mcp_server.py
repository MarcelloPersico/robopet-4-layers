"""MCP server exposing the robot + queue tools. Plan §3.3.

Two bindings, one tool surface (Plan §8.1):

  * In-process HTTP/SSE — built by :func:`build_server` against the *live*
    RobotTools the orchestrator already owns, so the human's Claude session
    drives the same robot and the same WorldState as the local agent. This is
    the primary, fully-wired path.

  * Standalone stdio — ``python mcp_server.py`` (spawned by Claude Desktop via
    claude_desktop_config.json). The orchestrator may not be running, so this
    mode opens the queue SQLite directly and serves the *queue* tools only, for
    offline triage. Live robot tools require the orchestrator's HTTP binding.

Targets the official `mcp` Python SDK (FastMCP, mcp>=1.0).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("mcp_server")


def build_server(tools, name: str = "robot-desk-pet"):
    """Register the full tool surface against a live RobotTools object."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name)

    # --- robot tools (also let the human drive the pet from chat for demos) ---
    @mcp.tool()
    async def drive(linear: float, angular: float, duration_ms: int = 0) -> str:
        """Drive the robot: linear m/s (forward+), angular rad/s (left+)."""
        return await tools.drive(linear, angular, duration_ms)

    @mcp.tool()
    async def play_animation(name: str, loops: int = 1) -> str:
        """Play a named animation (perk_up, nod, wiggle, spin, retreat)."""
        return await tools.play_animation(name, loops)

    @mcp.tool()
    async def stop() -> str:
        """Stop all motion immediately."""
        return await tools.stop()

    @mcp.tool()
    async def see() -> str:
        """Describe what the robot's camera currently sees."""
        return await tools.see()

    @mcp.tool()
    async def speak(text: str) -> str:
        """Make the robot say something out loud."""
        return await tools.speak(text)

    @mcp.tool()
    async def set_idle_intensity(level: float) -> str:
        """Set idle 'breathing' intensity, 0 (still) to 1 (lively)."""
        return await tools.set_idle_intensity(level)

    _register_queue_tools(mcp, tools)
    return mcp


def _register_queue_tools(mcp, tools) -> None:
    """Queue triage tools — the meaningful surface for the human path (Plan §3.3)."""

    @mcp.tool()
    async def list_pending_questions(status_filter: str = "pending", limit: int = 20) -> list[dict]:
        """List queued questions: id, ts, category, utterance, agent_guess, status."""
        return await tools.list_pending_questions(status_filter, limit)

    @mcp.tool()
    async def get_pending_question(id: int) -> Any:
        """Full record for one question, including the saved camera frame."""
        rec = await tools.get_pending_question(id)
        if not rec:
            return f"no such question #{id}"
        frame_path = rec.get("frame_abspath")
        if frame_path:
            try:
                from mcp.server.fastmcp import Image

                return [_summarize(rec), Image(path=frame_path)]
            except Exception as e:  # noqa: BLE001 - fall back to text-only
                log.debug("image inline failed: %s", e)
        return rec

    @mcp.tool()
    async def resolve_pending_question(
        id: int, resolution_text: str, share_with_robot: bool = True
    ) -> str:
        """Resolve a question; if shared, the robot learns the answer."""
        return await tools.resolve_pending_question(id, resolution_text, share_with_robot)

    @mcp.tool()
    async def dismiss_pending_question(id: int, reason: str) -> str:
        """Dismiss a question without resolving it."""
        return await tools.dismiss_pending_question(id, reason)

    @mcp.tool()
    async def summarize_queue() -> str:
        """One-line natural-language summary of the pending queue."""
        return await tools.summarize_queue()


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


# --- standalone stdio entry (Claude Desktop) ---------------------------------
class _QueueOnlyTools:
    """Adapts a bare QueueDB to the queue-tool method names RobotTools exposes,
    for offline triage when the orchestrator isn't running."""

    def __init__(self, db) -> None:
        self.db = db

    async def list_pending_questions(self, status_filter="pending", limit=20):
        return self.db.list_pending(status_filter, limit)

    async def get_pending_question(self, id: int):
        return self.db.get_question(id)

    async def resolve_pending_question(self, id: int, resolution_text: str, share_with_robot: bool = True) -> str:
        fact = self.db.resolve_question(id, resolution_text, share_with_robot)
        if fact is None:
            return f"resolved #{id}" if not share_with_robot else f"no such question #{id}"
        return f"resolved #{id} (robot will learn this on its next start)"

    async def dismiss_pending_question(self, id: int, reason: str) -> str:
        return f"dismissed #{id}" if self.db.dismiss_question(id, reason) else f"no such question #{id}"

    async def summarize_queue(self) -> str:
        return self.db.summarize_queue()


def main() -> None:
    """`python mcp_server.py` — stdio server with queue-only tools."""
    from mcp.server.fastmcp import FastMCP

    from config import load_config
    from pet_queue import QueueDB

    cfg = load_config()
    db = QueueDB(cfg["queue"]["db_path"], cfg["queue"]["frames_dir"])
    mcp = FastMCP("robot-desk-pet-queue")
    _register_queue_tools(mcp, _QueueOnlyTools(db))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
