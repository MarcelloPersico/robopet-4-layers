"""Manual smoke test for the stdio MCP server (the Claude Desktop path).

Spawns `mcp_server.py` as a subprocess and drives it over the real MCP stdio
transport — no orchestrator, no models, no Node. Seeds one question, lists it,
fetches it (with the inlined frame), resolves it, and confirms it leaves the
pending list. Run with the venv python (from desktop/)::

    ..\\.venv\\Scripts\\python tests\\mcp_smoke.py

It writes to the real data/queue.sqlite, so the rows it seeds are visible to
cli_queue.py and Claude Desktop afterwards (clear them with cli_queue.py or by
deleting data/queue.sqlite*).
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config import load_config
from pet_queue import QueueDB


def _seed_question() -> int:
    cfg = load_config()
    db = QueueDB(cfg["queue"]["db_path"], cfg["queue"]["frames_dir"])
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
    # the subprocess has the `mcp` package on its path.
    params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
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
