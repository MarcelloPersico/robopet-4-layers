# Triaging the robot's questions with Claude (MCP)

When the robot is genuinely uncertain it calls `queue_question`, which writes a
row to `data/queue.sqlite`, saves the camera frame to `data/pending_frames/`,
and fires a throttled toast. There is **no automated cloud call** — the question
waits for a human, who answers it by chatting with their own Claude
subscription. Claude reaches the queue through this MCP server (Plan §3.3, §8.7).

The robot then *learns the answer back*: a resolution shared with the robot lands
in `resolved_knowledge` and is injected into the agent's prompt every turn, so it
stops re-asking (the recent-answers buffer, Plan §5.5).

## Tool surface

One tool surface, two consumers (the local agent calls these in-process; you
reach the same methods over MCP):

| Tool | Use |
|------|-----|
| `list_pending_questions(status_filter="pending", limit=20)` | What's waiting |
| `get_pending_question(id)` | Full record — **inlines the saved camera frame** so Claude can see what the robot saw |
| `resolve_pending_question(id, resolution_text, share_with_robot=True)` | Answer it; if shared, the robot learns it |
| `dismiss_pending_question(id, reason)` | Drop it without teaching the robot |
| `summarize_queue()` | One-line backlog summary |
| `drive` / `play_animation` / `stop` / `see` / `speak` / `set_idle_intensity` | Drive the pet live (HTTP binding only — see below) |

## Two ways to connect

### A. Claude Desktop over stdio (offline triage — recommended for the human)

Claude Desktop spawns the server as a subprocess. This mode opens the SQLite
queue **directly** and serves the *queue* tools only, so it works even when the
orchestrator isn't running. Resolutions are persisted; the robot picks them up
on its **next start** (the orchestrator seeds the recent-answers buffer from
`resolved_knowledge` at boot — `orchestrator.run()`).

Add this to your `claude_desktop_config.json`
(`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "robot-desk-pet": {
      "command": "python",
      "args": ["C:\\Users\\persi\\Desktop\\Jarvis 1.0\\desktop\\mcp_server.py"]
    }
  }
}
```

Use the same Python that has the project deps (the `mcp` package). If you use a
venv, point `command` at that interpreter, e.g.
`C:\\Users\\persi\\Desktop\\Jarvis 1.0\\.venv\\Scripts\\python.exe`.
Restart Claude Desktop; "robot-desk-pet" appears in the tools menu. Then just ask
Claude things like *"what's the robot been wondering about?"* and
*"tell it #3 is a stapler."*

### B. HTTP / SSE while the orchestrator is running (live robot)

The orchestrator runs the same tools **in-process** against the *live* robot, so
resolutions update the running agent's buffer immediately (no restart needed) and
the robot tools (`drive`, `see`, …) actually move the pet. Configured under
`[mcp]` in `config.toml`:

```toml
[mcp]
enable_http = true
http_host = "127.0.0.1"   # localhost-only by default — not exposed to the LAN
http_port = 8770
http_bearer_token = "change-me-in-config.local.toml"
```

The endpoint is the streamable-HTTP transport at `http://127.0.0.1:8770/mcp`.
Bind it to `127.0.0.1` (the default) unless you understand the exposure: this
surface can drive the motors and read the camera. If you change `http_host` to a
LAN address, set a real `http_bearer_token` in `config.local.toml` first — the
orchestrator warns at startup if you expose it with the default token.

## Quick checks

```powershell
python cli_queue.py                       # inspect / resolve / dismiss from the CLI
python -m pytest tests/test_mcp_server.py # the end-to-end human-in-loop tests
```
