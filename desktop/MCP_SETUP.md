# Triaging the robot's questions with Claude (MCP)

When the robot is genuinely uncertain it calls `queue_question`, which writes a
row to `data/queue.sqlite`, saves the camera frame to `data/pending_frames/`,
and fires a throttled toast. There is **no automated cloud call** — the question
waits for a human, who answers it by chatting with their own Claude
subscription. Claude reaches the queue through this MCP server (Plan §3.3, §8.7).

The server is **live only**: it runs in-process inside the orchestrator over a
localhost HTTP/SSE binding, against the same `RobotTools` + `WorldState` the local
agent uses. (There is no offline/stdio queue-only mode — connect while the robot
is running.)

The robot then *learns the answer back*: a resolution shared with the robot lands
in `resolved_knowledge` and is injected into the agent's prompt every turn, so it
stops re-asking (the recent-answers buffer, Plan §5.5). The resolution also goes
one step further — it's handed straight to the local agent, which reacts on the
spot (speaks the answer in the robot's own voice and moves if it fits) instead of
waiting for the next utterance.

## Intended workflow — keep it tight

The server ships **instructions** (surfaced to your Claude client) that frame
triage as a short loop, so Claude doesn't over-reason or micromanage the body:

1. `next_pending_question()` → the oldest question, with its saved frame inlined.
2. Answer it directly (only `see()` for a *live* look if the question is about now).
3. `resolve_pending_question(id, answer, share_with_robot=True)` — **the robot
   speaks and moves on its own**; don't call `speak`/`drive` yourself.
4. Repeat until the queue is empty.

## Tool surface

One tool surface, two consumers (the local agent calls these in-process; you
reach the same methods over MCP):

| Tool | Use |
|------|-----|
| `next_pending_question()` | **Start here.** Oldest pending one, **inlines the saved camera frame** |
| `list_pending_questions(status_filter="pending", limit=20)` | What's waiting (overview) |
| `get_pending_question(id)` | Full record for a specific id — inlines the saved frame |
| `resolve_pending_question(id, resolution_text, share_with_robot=True)` | Answer it; if shared, the robot learns it **and reacts now** |
| `dismiss_pending_question(id, reason)` | Drop it without teaching the robot |
| `summarize_queue()` | One-line backlog summary |
| `drive` / `play_animation` / `stop` / `see` / `speak` / `set_idle_intensity` | Drive the pet live for demos — usually let the robot react on its own instead |

## Connecting (HTTP / SSE, while the robot is running)

Start the robot first (`python orchestrator.py`, or `run_full_stack.ps1` for the
desktop-only runner). It serves the tools **in-process** against the *live* robot,
so resolutions update the running agent's buffer immediately, hand the answer to
the agent so it reacts on the spot, and the robot tools (`drive`, `see`, …)
actually move the pet. Configured under `[mcp]` in `config.toml`:

```toml
[mcp]
enable_http = true
http_host = "127.0.0.1"   # localhost-only by default — not exposed to the LAN
http_port = 8770
http_bearer_token = "change-me-in-config.local.toml"
```

The endpoint is the streamable-HTTP transport at `http://127.0.0.1:8770/mcp`.
Point your MCP client at it: the [MCP inspector](https://github.com/modelcontextprotocol/inspector),
a claude.ai remote connector, or Claude Desktop via the
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge — e.g. in
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "robot-desk-pet-live": {
      "command": "npx",
      "args": ["mcp-remote", "http://127.0.0.1:8770/mcp"]
    }
  }
}
```

Then ask Claude things like *"what's the robot been wondering about?"* — it walks
the `next_pending_question` → answer → `resolve_pending_question` loop and the pet
reacts live.

Bind to `127.0.0.1` (the default) unless you understand the exposure: this surface
can drive the motors and read the camera. If you change `http_host` to a LAN
address, set a real `http_bearer_token` in `config.local.toml` first — the
orchestrator warns at startup if you expose it with the default token.

## Quick checks

```powershell
..\.venv\Scripts\python cli_queue.py list                  # inspect / resolve / dismiss from the CLI
..\.venv\Scripts\python -m pytest tests\test_mcp_server.py # the in-process human-in-loop tests
```

`cli_queue.py` and the pytest suite need only `mcp` + stdlib (no models), so the
lightweight `.venv` is enough. To exercise the live wire, start the robot and
connect an MCP client to `http://127.0.0.1:8770/mcp` as above.
