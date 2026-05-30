"""Manual smoke test for the stdio MCP server (the Claude Desktop path).

Spawns ``mcp_server.py`` as a subprocess and drives it over the real MCP stdio
transport — no orchestrator, no models, no Node. Seeds one question, lists it,
fetches it (with the inlined frame), resolves it, and prints the updated summary.
This is the closest check to Claude Desktop without launching it.

Run with the venv python (cwd-independent — works from anywhere)::

    ..\\.venv\\Scripts\\python desktop\\tests\\mcp_smoke.py

It writes to the real data/queue.sqlite, so the rows it seeds are visible to
cli_queue.py and Claude Desktop afterwards (clear them with cli_queue.py or by
deleting data/queue.sqlite*).

Not a pytest module (no ``test_`` prefix) — the in-process equivalents live in
tests/test_mcp_server.py and run under pytest.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Run standalone from any cwd: put desktop/ (this file's parent's parent) on the
# path so `import config` etc. resolve, and address mcp_server.py absolutely.
_DESKTOP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DESKTOP))
_SERVER = _DESKTOP / "mcp_server.py"

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from config import load_config  # noqa: E402
from pet_queue import QueueDB  # noqa: E402


def _seed_question() -> int:
    cfg = load_config(_DESKTOP / "config.toml")
    db = QueueDB(_DESKTOP / cfg["queue"]["db_path"], _DESKTOP / cfg["queue"]["frames_dir"])
    qid = db.queue_question(
        "object_identification", "what is this on my desk?", "maybe a mug",
        "low confidence", {}, [], b"\xff\xd8\xff\xe0fake-jpeg",
    )
    db.close()
    return qid


async def main() -> None:
    qid = _seed_question()
    print(f"seeded question #{qid}\n")

    # Spawn the same server Claude Desktop would, with this very interpreter so
    # the subprocess has the `mcp` package on its path. cwd=desktop/ so the
    # server's own relative config/db paths resolve.
    params = StdioServerParameters(
        command=sys.executable, args=[str(_SERVER)], cwd=str(_DESKTOP)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = [t.name for t in (await session.list_tools()).tools]
            print("tools exposed:", ", ".join(tools), "\n")

            listed = await session.call_tool("list_pending_questions", {})
            print("list_pending_questions ->", listed.structuredContent or listed.content, "\n")

            got = await session.call_tool("get_pending_question", {"id": qid})
            kinds = [getattr(c, "type", "?") for c in got.content]
            print(f"get_pending_question(#{qid}) -> content blocks: {kinds}")
            print("  (an 'image' block means the saved camera frame was inlined)\n")

            res = await session.call_tool(
                "resolve_pending_question",
                {"id": qid, "resolution_text": "it's a blue coffee mug", "share_with_robot": True},
            )
            print("resolve_pending_question ->", res.content[0].text, "\n")

            after = await session.call_tool("summarize_queue", {})
            print("summarize_queue ->", after.content[0].text)

    print("\nOK — the stdio MCP server answered over the wire.")
    print("The resolution is persisted to resolved_knowledge; a running robot")
    print("picks it up on its next orchestrator start (the boot-seed path).")


if __name__ == "__main__":
    asyncio.run(main())
