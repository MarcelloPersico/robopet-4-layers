# Robot Desk Pet — Implementation Plan

## Revision history

- **2026-05-26 — r2.** Removed automated Anthropic API escalation. The local agent no longer holds an API key or calls `ask_claude`. Hard cases are now queued to a local SQLite-backed **pending-questions** queue and surfaced to the human via an MCP server; the human resolves them by chatting with Claude over their existing Pro/Max subscription (Claude Desktop or claude.ai), which inverts the integration: the robot pulls the human into the loop instead of pulling the cloud in automatically.
- **2026-05-26 — r1.** Initial planning document.

---

## Context

Building a small differential-drive desk pet with a tiered control architecture: a Teensy 4.1 owns motion, a Pi Zero 2 W is a sensor + UART/WiFi bridge, a Windows desktop with an RTX 5070 Ti runs all ML and decision-making, and Claude (via the user's existing subscription) is involved only when the *human* opens a chat with the robot's MCP server connected.

The plan optimizes for:
- **Debuggability at 11pm** over absolute performance.
- **Layered independence** — each tier testable without the ones above it.
- **Latency hiding** — the pet must appear to react in ~300 ms even when slow paths (queueing, VLM) are in flight.
- **Never visibly inert** — the Teensy runs autonomous idle behavior so the pet "breathes" even when the brain is silent.
- **No pay-per-token surface** — the robot software costs nothing to run; cloud capability arrives via the human's subscription at chat time.

This document is the source of truth for the build.

---

## 1. System architecture

```
  ┌──────────────────────────────────────┐
  │ HUMAN ✕ CLAUDE (chat, subscription)  │
  │  Claude Desktop or claude.ai         │
  │  connects to the robot's MCP server  │
  │  on demand to triage the queue       │
  └──────────────┬───────────────────────┘
                 │  MCP (stdio or HTTP/SSE, localhost)
                 │  human-initiated, subscription-billed
  ┌──────────────▼──────────────────────────┐
  │  DESKTOP  (Windows, 5070 Ti)            │
  │  ─────────────────────────────────────  │
  │  orchestrator.py (asyncio)              │
  │    ├── wsserver       (to Pi)           │
  │    ├── asr            (faster-whisper)  │
  │    ├── vlm            (Moondream2)      │
  │    ├── agent          (llama.cpp srv)   │
  │    ├── tts            (Piper)           │
  │    ├── queue          (SQLite + frames) │
  │    ├── mcp_server     (tools surface)   │
  │    ├── notifier       (toast/webhook)   │
  │    └── world state                      │
  └──────────────┬──────────────────────────┘
                 │  WiFi LAN (WebSocket, JSON + binary frames)
                 │
  ┌──────────────▼───────────────┐
  │  PI ZERO 2 W  (Raspbian Lite)│
  │  ──────────────────────────  │
  │  bridge.py    (UART ↔ WS)    │
  │  capture.py   (cam + mic)    │
  │     ├── motion-gate (cv2)    │
  │     └── VAD (webrtcvad)      │
  │  systemd: pet-bridge,        │
  │           pet-capture        │
  └──────────────┬───────────────┘
                 │  UART  (921600 baud, line-delimited JSON)
                 │  + 5 V + GND through neck
  ┌──────────────▼───────────────┐
  │  TEENSY 4.1  (PlatformIO)    │
  │  ──────────────────────────  │
  │  main loop @ 1 kHz           │
  │    ├── PID per wheel         │
  │    ├── motion planner        │
  │    ├── animation player      │
  │    ├── reflex / idle engine  │
  │    ├── command parser (JSON) │
  │    ├── telemetry emitter     │
  │    └── watchdog              │
  │  L298N ◀── 2× brushed DC + quad encoders
  └──────────────────────────────┘
```

All tiers run independently. The Teensy's USB serial remains exposed for bench development so the desktop can talk directly to it during bringup (skipping the Pi). The MCP server runs in-process inside the orchestrator and exposes the same tool surface the local agent uses, plus the pending-questions tools — so the human's Claude session and the local agent see the *same* robot through the *same* interface.

---

## 2. Per-layer responsibility breakdown

### 2.1 Teensy 4.1 (body firmware)

*Unchanged from r1.*

**Does:**
- Closed-loop velocity control of both drive wheels via PID on encoder counts.
- Executes a small command vocabulary: `drive`, `stop`, `play`, `set_idle`, `ping`.
- Plays named animations (sequences of `(left_vel, right_vel, duration_ms)` tuples).
- Runs a reflex/idle engine when no command arrives for `idle_timeout_ms` (default 4000 ms): occasional wheel jitter, slow turns, deliberate pauses.
- Emits 50 Hz telemetry: encoder counts, measured wheel velocities, motor current proxy (PWM duty), command-link age, mode (`active|idle|fault`).
- Safety: link-loss timeout (stop motors if no `ping` in 1500 ms), stall detection (commanded ≠ measured for > 500 ms → fault), thermal/over-current backoff via PWM ceiling.

**Does not:**
- Know anything about LLMs, audio, vision, or the queue.
- Parse natural language.
- Maintain world state beyond its own motion state.
- Generate trajectories longer than the currently-running command/animation.

**Stack:** PlatformIO with the Teensyduino Arduino core. `Encoder.h`, `ArduinoJson` v7, custom PID. See §6.

### 2.2 Pi Zero 2 W (head)

*Unchanged from r1.*

**Does:**
- Two systemd services: `pet-bridge` (UART ↔ WebSocket, transparent forwarder + 1 Hz heartbeat) and `pet-capture` (USB webcam frames + mic audio with motion-gate and VAD pre-filtering).
- Auto-reconnects to the desktop WebSocket with exponential backoff.
- Buffers nothing across drops.

**Does not:** Run any ML. Make decisions. Process Teensy commands beyond passing them through.

**Stack:** Python 3.11 on Raspberry Pi OS Lite. `pyserial-asyncio`, `websockets`, `opencv-python-headless`, `sounddevice`, `webrtcvad`. See §7.

### 2.3 Desktop (Windows, 5070 Ti)

**Does:**
- Hosts the WebSocket server the Pi connects to.
- Runs ASR streaming (faster-whisper, GPU), the agent LLM (llama.cpp server subprocess, GPU), the VLM on demand (Moondream2, GPU), and TTS (Piper, CPU).
- Maintains world state: recent transcripts, last VLM observation, last Teensy telemetry, current motion goal, conversation history, **resolved-knowledge buffer** (see §5).
- Implements latency-hiding: acknowledgment animation + filler speech dispatch the instant ASR finalizes, in parallel with the agent loop.
- Hosts an **MCP server** exposing the robot's tool surface (motion, perception, speech) plus the **pending-questions** tools. Both the local agent and the human's Claude chat client connect to this server.
- Owns the **pending-questions queue** (SQLite + on-disk frame snapshots).
- Sends **notifications** (Windows toast, optional HTTP webhook) when new questions are queued, throttled.

**Does not:**
- Run on the robot.
- Hold any Anthropic API key.
- Make any outbound calls to Anthropic services.
- Trust the Pi for anything except as a sensor pipe.
- Drive motors directly — always through the Teensy's high-level command API.

**Stack:** Python 3.11, asyncio. Single process. Subprocesses for `llama-server` (llama.cpp) and `piper`. In-process: `faster-whisper`, `transformers` (Moondream2), `websockets` (server), `mcp` (the official Python MCP SDK), `sqlite3` (stdlib), `winrt-Windows.UI.Notifications` or the simpler `win10toast-click` for notifications, `httpx` for the optional webhook.

**Files:**
- `desktop/orchestrator.py` — entry point, asyncio main, supervises everything.
- `desktop/wsserver.py` — Pi connection: framing, demux of audio/video/telemetry/command channels.
- `desktop/asr.py` — faster-whisper wrapper, streaming partial + final transcripts.
- `desktop/vlm.py` — Moondream2 wrapper: `describe(jpeg_bytes) -> str`.
- `desktop/agent.py` — llama.cpp client, tool-call loop, system-prompt builder.
- `desktop/tts.py` — Piper subprocess wrapper, sentence-streaming.
- `desktop/queue.py` — SQLite schema + CRUD for pending questions and resolved knowledge.
- `desktop/mcp_server.py` — MCP server registering all tools (robot + queue).
- `desktop/notifier.py` — toast / webhook / silent backends, throttled dispatcher.
- `desktop/state.py` — `WorldState` dataclass + update helpers; owns the recent-answers buffer.
- `desktop/motion.py` — high-level motion intents → Teensy JSON commands.
- `desktop/config.toml` — model paths, ports, notification settings, persona file pointer.
- `desktop/persona.md` — the static (cacheable) part of the agent system prompt.
- `desktop/data/queue.sqlite` — the queue database (gitignored).
- `desktop/data/pending_frames/<question_id>.jpg` — saved camera snapshots referenced by queue entries.
- `desktop/cli_queue.py` — small CLI to inspect/dismiss/resolve the queue for debugging.

### 2.4 Cloud (human-initiated only)

There is **no automated cloud integration** in this version of the robot. The robot software has no API credentials and makes no outbound LLM calls.

The "cloud" role in the architecture is filled entirely by the human voluntarily opening Claude Desktop (or claude.ai with a remote MCP connector) with the robot's MCP server attached, and chatting about the queue. Capability is billed against the human's Pro/Max subscription via that chat session, on demand. The robot software does not see or care about that billing.

Future revisit: the Anthropic Agent SDK is expected to add a monthly subscription credit for Pro/Max users on **June 15, 2026**. When that lands, an automated path billed against the subscription becomes feasible. It would be complementary to — not a replacement for — the pending-questions pattern: automated for time-critical needs, queued for things that benefit from human attention. See §10.

---

## 3. Communication protocols

Design rule for all links: **human-readable JSON unless bandwidth forces binary, line-delimited, schema-tagged with a `type` field.**

### 3.1 Teensy ↔ Pi (UART)

*Unchanged from r1.* 921 600 baud 8N1, line-delimited JSON. Commands (`drive`, `stop`, `play`, `set_idle`, `ping`, `config`) downstream; telemetry/event/log/pong upstream at 50 Hz for telemetry. Heartbeat 2 Hz; link-loss timeout 1500 ms. Bandwidth ~10–12 kB/s.

### 3.2 Pi ↔ Desktop (WebSocket over WiFi)

*Unchanged from r1.* Single WebSocket on port 8765 with channel-tagged frames: `0x01` JSON control, `0x02` audio PCM, `0x03` video JPEG, `0x04` UART passthrough. Heartbeat 2 s. Exponential-backoff reconnect on the Pi side. Worst-case bandwidth ~5.6 Mbps.

### 3.3 Desktop ↔ Human's Claude chat (MCP)

This replaces the prior Anthropic API link.

- **Transport:** the MCP server in `desktop/mcp_server.py` supports both stdio and HTTP/SSE bindings, configured via `config.toml`. Stdio is the path for Claude Desktop on the same machine (Claude Desktop spawns the server as a subprocess based on its `claude_desktop_config.json`); HTTP/SSE on localhost is the path if the human prefers to use claude.ai with a custom remote connector or to inspect the server with `mcp-inspector`.
- **Why MCP:** it is the supported, documented way to expose tools and resources to Claude Desktop / claude.ai. Anthropic ships first-party MCP SDKs in Python; the protocol is JSON-RPC over the chosen transport, which is human-debuggable. Using MCP means the *same* tool surface the local agent uses can be reused for the human's chat session with no duplication.
- **Authentication:** the stdio binding inherits process-level access (no auth needed); the HTTP/SSE binding binds to `127.0.0.1` only and requires a static bearer token from `config.toml`. Documented in setup, not auto-rotated.
- **Schema:** all message shapes are defined by the MCP SDK. The tools we register are:

  **Robot tools** (the local agent also calls these; exposed so the human can drive the robot from chat for demos):
  - `drive(linear, angular, duration_ms)`
  - `play_animation(name, loops)`
  - `stop()`
  - `see() -> string`
  - `speak(text)`
  - `set_idle_intensity(level)`

  **Queue tools** (only the human path uses these meaningfully; the agent writes via `queue_question`, see §5):
  - `list_pending_questions(status_filter="pending", limit=20)` — returns array of `{id, ts, category, utterance, agent_guess, status}`.
  - `get_pending_question(id)` — returns the full record including the saved frame as an inline image content block, robot pose, and conversation excerpt.
  - `resolve_pending_question(id, resolution_text, share_with_robot=true)` — marks resolved; if shared, pushes the resolution onto the recent-answers buffer (§5).
  - `dismiss_pending_question(id, reason)` — marks dismissed without resolution.
  - `summarize_queue()` — short natural-language summary suitable as a conversation opener.
  - `queue_question(category, utterance, agent_guess, why_unsure)` — write path used by the local agent; exposed via MCP for symmetry and for debugging from chat.

- **Heartbeat / lifecycle:** MCP transports handle their own connection lifecycle. If the human's chat disconnects, the queue and tools remain available; the robot keeps running. There is no concept of a "session" that the robot depends on.
- **Bandwidth:** negligible. Tool calls are sparse and small except `get_pending_question`, which inlines one JPEG (~40 kB).

---

## 4. Model selection for the desktop

*Unchanged from r1.*

| Role  | Model                                              | Quant         | VRAM      | Throughput            |
|-------|----------------------------------------------------|---------------|-----------|-----------------------|
| ASR   | Whisper-large-v3-turbo (faster-whisper / CT2)      | int8_float16  | ~1.6 GB   | RTF ~0.05             |
| VLM   | Moondream2 (vikhyatk/moondream2)                   | fp16          | ~3.0 GB   | ~1.5 s / image        |
| Agent | Qwen2.5-7B-Instruct (GGUF, llama.cpp)              | Q4_K_M        | ~5.5 + 1.5 GB KV | 80–110 tok/s   |
| TTS   | Piper (en_US-amy-medium)                           | onnx CPU      | 0 GB GPU  | RTF ~0.03             |

**Total GPU:** ≈ 11.6 GB resident, ≈ 4 GB margin for fragmentation and KV growth.

Justifications: faster-whisper for the best-maintained Whisper backend; Moondream2 for a fast, small VLM that fits alongside everything; Qwen2.5-7B for best-in-class open tool-calling at this size with native llama.cpp support; Piper for stable, low-latency, CPU-only TTS that doesn't compete for GPU. llama.cpp server is the Windows-friendly inference path. Revisit triggers in §10.

---

## 5. Agent loop design

### 5.1 Tool set

The agent's tool set (registered on the in-process MCP server, presented to the LLM via llama.cpp's OpenAI-compatible function-calling interface):

- `drive(linear, angular, duration_ms)`
- `play_animation(name, loops=1)`
- `stop()`
- `see() -> str`
- `speak(text)`
- `set_idle_intensity(level)`
- `queue_question(category, utterance, agent_guess, why_unsure)` — **new**: the deferral path. Writes a row to `queue.sqlite`, saves the most recent camera frame to `pending_frames/<id>.jpg`, snapshots robot pose and the last few conversation turns, fires a notification.

There is no `ask_claude` tool. There is no automated cloud call from inside the loop.

### 5.2 Decision criteria for queueing

The local agent is instructed (in the system prompt) to call `queue_question` when, and only when, **at least one** of the following holds:

- **Low object-identity confidence.** The agent (often after a `see()` call) cannot confidently identify a salient object or scene element relevant to the user's question.
- **Multi-step reasoning beyond ~3 steps.** The question requires planning or chained inference that a small local LLM is likely to bungle. Heuristic: if the agent finds itself needing more than three internal "and therefore…" links, defer.
- **Opinion or judgment beyond competence.** Requests for subjective evaluation, recommendations on topics outside the agent's persona, or questions about the user's life and decisions that warrant care.
- **Novelty.** The agent's planned response would be a guess — the topic is unfamiliar and an answer would risk fabrication.

In all other cases the agent answers locally. The system prompt explicitly says: **do not queue trivial questions; the queue is for things you genuinely cannot do well, not for things you could try.**

### 5.3 Behavior when queueing

When `queue_question` is called:
1. The tool writes the row and saves the frame synchronously (SQLite write + JPEG write — both fast).
2. The notifier is invoked asynchronously (does not block the agent).
3. The agent immediately speaks a short, in-character acknowledgment: e.g., *"hmm, i'm not sure about that one — i'll save it for later,"* *"that's a good one, i'll think on it,"* *"i don't know yet, but i'll remember to ask."* A small varied set is provided in the system prompt to avoid robotic repetition.
4. The agent then continues the conversation normally. It does not "wait" for a resolution; the resolution arrives later via the recent-answers buffer (§5.5).

If the queue grows past five unresolved questions, the agent may, during idle moments, mention this conversationally: *"i've got a few things i've been wondering about, if you want to take a look."* Once per ~30 minutes maximum.

### 5.4 System prompt structure

Static (lives in `desktop/persona.md`, hashed for llama.cpp prefix-cache hits):
1. Identity and persona.
2. Behavioral rules (short responses, no narration of tool calls, prefer one short spoken sentence + one motion).
3. Tool schemas with examples.
4. **Deferral policy** for `queue_question` (the criteria in §5.2 plus the "do not queue trivial" guidance).
5. Idle-time guidance.

Dynamic (appended each turn):
1. **Recent-answers buffer** — see §5.5. Always included.
2. Last ~6 conversation turns (rolling).
3. Latest `see()` result, if recent (< 10 s).
4. Latest telemetry one-liner.
5. The new user utterance.

### 5.5 Resolution learning (recent-answers buffer)

When the human resolves a question with `share_with_robot=true`, the resolution text is appended to a **recent-answers buffer** in `state.py`. The buffer is a deque of the last **50** resolutions, oldest-evicted-first. The entire buffer is included in the dynamic part of the system prompt on every turn (formatted compactly: `[<short category>] <utterance or topic> → <resolution>`).

**Persistence model:** the buffer is persisted to `queue.sqlite` (table `resolved_knowledge`, full history) but only the last 50 entries are loaded into the prompt. Older resolutions remain in the database for retrieval/inspection but do not bias the agent indefinitely.

**Tradeoff considered:** longer persistence (hours, days, forever) means the robot remembers more, but the local context window is finite (8k for Qwen2.5-7B in our config) and old resolutions can become stale or outright misleading ("the cup on the desk was Eric's" stops being true when the cup moves). 50 entries × ~40 tokens each ≈ 2k tokens, leaves room for conversation + persona + dynamic state. Eviction by recency rather than relevance is deliberately simple — relevance scoring would need embeddings and a retrieval step, which is more machinery than the problem warrants right now. Revisit if the recent-answers buffer routinely contains stale facts that mislead the agent (§10).

### 5.6 Latency hiding — concrete order of operations

Unchanged conceptually from r1. When ASR finalizes:

1. **t = 0 ms:** Dispatch acknowledgment animation (`play_animation("perk_up")`).
2. **t = 0–50 ms:** If the heuristic predicts a slow turn (long utterance, `see()` likely, deferral likely), pick a filler phrase and start TTS.
3. **t = 0 ms:** Submit the prompt to llama.cpp server, streamed.
4. **As tokens stream:** sentence-by-sentence to Piper so playback overlaps generation.
5. **If a `queue_question` tool call appears in the stream:** the write and frame snapshot complete in tens of milliseconds — effectively zero added latency — and the agent emits its acknowledgment phrase as its spoken reply.
6. **`see()` mid-turn:** ~1.5 s; the filler phrase from step 2 covers this.

The slow paths in this version are local (VLM) and bounded. There is no cloud round trip to hide.

### 5.7 Idle behavior

After > 30 s of no user activity, with low probability per tick the orchestrator may:
- Call `see()` and comment if the scene changed materially.
- Trigger an animation directly.
- Mention an unresolved-queue summary if the queue is long.

Rate-limited; mutable via `set_idle_intensity(0)`. The Teensy's reflex engine handles the always-on "breathing" beneath all of this.

---

## 6. Teensy firmware structure

*Unchanged from r1.*

PlatformIO + Teensyduino. Modules: `MotorDriver`, `EncoderReader`, `PID`, `MotionPlanner`, `AnimationPlayer`, `ReflexEngine`, `CommandParser`, `Telemetry`, `Watchdog`, `main`. Control loop 1 kHz, telemetry 50 Hz, reflex 10 Hz, watchdog 100 Hz. Safety: link timeout 1500 ms → soft stop; stall detection → fault; PWM ceiling 90 %.

---

## 7. Pi software structure

*Unchanged from r1.*

Two systemd services (`pet-bridge`, `pet-capture`) running Python 3.11. Bridge transparently forwards UART ↔ WebSocket. Capture does motion-gated JPEG at 15 fps + VAD-gated PCM at 16 kHz with 300 ms pre-roll / 500 ms hangover. Reconnects with exponential backoff; nothing buffered across drops.

---

## 8. Desktop orchestrator structure

### 8.1 Process model

Single Python process with `asyncio`, plus two subprocesses (`llama-server.exe`, `piper`). In-process GPU models (faster-whisper, Moondream2) run on a dedicated thread pool via `loop.run_in_executor`. The MCP server runs in-process and is registered with both bindings (stdio for Claude Desktop spawn-and-attach, HTTP/SSE on `127.0.0.1` for inspector / remote connectors).

### 8.2 Components

Already enumerated in §2.3. Runtime asyncio task graph:
- `ws_server_task` — handles the Pi connection.
- `asr_task` — audio queue → transcript queue.
- `agent_task` — transcripts → tool loop → motion + TTS dispatch.
- `tts_task` — text → audio out (local speaker).
- `motion_task` — intents → Teensy JSON.
- `telemetry_task` — UART passthrough → state updates.
- `idle_task` — occasional idle behaviors.
- `health_task` — pings, subprocess supervision, queue size check.
- `mcp_task` — serves the MCP transport(s).
- `notifier_task` — drains notification queue with throttling.

Bounded `asyncio.Queue` everywhere; drop-oldest on overflow for non-critical streams.

### 8.3 State

`WorldState` in `state.py`: recent transcripts (deque 12), last VLM result + age, last telemetry snapshot, current motion goal, conversation history (deque 30), idle-since timestamp, **recent-answers buffer (deque 50)**. The buffer is the only piece of state that crosses session boundaries (loaded from `queue.sqlite` on startup, kept in sync on each resolution).

### 8.4 Queue schema

`queue.sqlite`:

```sql
CREATE TABLE pending_questions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,                  -- ISO 8601
  category      TEXT NOT NULL,
  utterance     TEXT,                            -- nullable: agent-initiated wonderings
  agent_guess   TEXT NOT NULL,
  why_unsure    TEXT NOT NULL,
  pose_json     TEXT NOT NULL,                  -- {linear_vel, angular_vel, mode}
  excerpt_json  TEXT NOT NULL,                  -- last N conversation turns
  frame_path    TEXT,                            -- relative path under data/pending_frames/
  status        TEXT NOT NULL DEFAULT 'pending', -- pending|seen|resolved|dismissed
  resolved_ts   TEXT,
  resolution    TEXT,
  dismiss_reason TEXT
);
CREATE INDEX idx_status ON pending_questions(status);

CREATE TABLE resolved_knowledge (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id   INTEGER REFERENCES pending_questions(id),
  ts            TEXT NOT NULL,
  category      TEXT NOT NULL,
  topic         TEXT NOT NULL,                  -- short human-readable topic
  resolution    TEXT NOT NULL,
  evicted       INTEGER NOT NULL DEFAULT 0      -- 0 = still in recent-answers buffer; 1 = aged out
);
```

Migrations are not planned: schema is small and stable. If we need a change, we'll write a one-shot migration script rather than carry a framework.

### 8.5 Notifications

`notifier.py` supports three backends, selectable via `config.toml`:

- `toast` (default on Windows): Windows toast via `win10toast-click` — clicking opens a small details window. Throttled to **at most one toast per 10 minutes** to avoid spam, with a counter ("3 new questions").
- `webhook`: HTTP POST to a user-configured URL with a JSON body `{count, last_question}`. The user wires this to ntfy / Pushover / Discord themselves. No throttling beyond the same 10-minute rule.
- `silent`: nothing; user checks via `cli_queue.py` or Claude chat when curious.

Default config: `toast`, 10-minute throttle, includes count in title.

### 8.6 Connection manager

Pi connection management unchanged from r1. MCP connection management is handled by the SDK; the server runs continuously and accepts connects/disconnects without affecting other subsystems.

### 8.7 Graceful degradation

- **No Pi:** desktop still runs; ASR/agent/TTS work via local mic/speaker if a debug flag is set.
- **No Teensy:** motion tools disabled in the agent's tool set after a 5 s no-telemetry warning.
- **MCP server down:** the local agent still runs (it uses the same in-process implementations, not the MCP wire protocol); only the human's Claude chat loses access until the server is restarted.
- **GPU OOM:** detected via CUDA error; offending subsystem reloaded with smaller settings.

There is no "cloud down" failure mode anymore — the system makes no outbound calls.

---

## 9. Build and test order

**M1 — Teensy motion bringup (no Pi, USB serial only).**
Wire L298N + motors + encoders. Implement `MotorDriver`, `EncoderReader`, `PID`, `MotionPlanner`, `CommandParser` on USB Serial. Minimum demo: `screen` into the Teensy, type a JSON `drive` command, the wheels go; encoder telemetry streams back at 50 Hz.

**M2 — Teensy animations + reflex + watchdog.**
Add `AnimationPlayer` with 4–5 starter animations, `ReflexEngine` idle behaviors, `Watchdog` link-loss-to-stop. Minimum demo: stop typing for 4 s, the robot starts breathing/jittering; unplug USB, motors stop within 1.5 s.

**M3 — Pi bridge bringup.**
Pi OS install, `bridge.py`, UART wiring through the neck. From the desktop, `wscat` into the Pi WS endpoint; commands typed in `wscat` reach the Teensy; telemetry comes back. Systemd up with `Restart=always`.

**M4 — Pi capture (camera + mic).**
Write `capture.py`. From `wscat`, observe motion-gated JPEGs and VAD-bounded PCM arriving in correct channels. Wave at the camera, see frames flow; speak, see VAD `start`/`stop` bracketing audio.

**M5 — Desktop models, no robot.**
Bring up faster-whisper, llama.cpp server with Qwen2.5-7B, Piper, Moondream2 concurrently; verify VRAM stays under 13 GB. CLI: type or pipe a wav → ASR → agent → TTS to local speaker. Test `see()` against a sample JPEG.

**M6 — Full voice loop with motion.**
Connect M3 + M4 + M5. End-to-end: speak to the robot, get a spoken reply, see it move. First measurable latency numbers; tune to "ack within ~300 ms."

**M7 — VLM integration in the agent.**
Wire `see()` into the agent's tool set. Agent can ask about its surroundings. 30 min sustained-operation test; observe memory.

**M8 — Pending-questions queue + agent deferral.**
Implement `queue.py` (SQLite schema, frame-save), add `queue_question` to the agent's tool set and to the system prompt's deferral policy. Implement `cli_queue.py` for inspection. Implement the recent-answers buffer in `state.py` and confirm it flows into the prompt. Minimum demo: show the robot something it can't identify; it speaks the in-character acknowledgment, writes a queue row, saves a frame; `cli_queue.py list` shows it; manually run `cli_queue.py resolve <id> "<text>"`; later in the same session the agent answers correctly because the resolution is in the recent-answers buffer.

**M9 — MCP server + Claude Desktop end-to-end.**
Implement `mcp_server.py` with all robot and queue tools, both stdio and HTTP/SSE bindings. Add the server to `claude_desktop_config.json`. Implement `notifier.py` with the toast backend and 10-minute throttle. Minimum demo (satisfying end-to-end): intentionally queue a real question by showing the robot something it can't identify; Windows toast fires; open Claude Desktop and say "what's my robot been wondering about?"; Claude calls `summarize_queue` and `get_pending_question`, sees the saved frame, helps interpret, calls `resolve_pending_question(id, "...", share_with_robot=true)`; come back to the robot and ask the same question — it answers from the recent-answers buffer.

**M10 — Persona + animation library polish.**
Iterate on `persona.md`, expand animations, tune idle behaviors, select final TTS voice, refine deferral acknowledgments.

**M11 — Reliability hardening.**
Inject failures: WiFi drops, killed subprocesses, Teensy USB yank, Claude Desktop closed mid-resolution, SQLite locked. Confirm graceful recovery. Add structured logging across all tiers with correlation IDs. Add the optional webhook backend to `notifier.py`.

---

## 10. Open questions and decisions to revisit

- **Subscription-billed automated escalation (the June 15, 2026 question).** When the Anthropic Agent SDK ships its monthly credit for Pro/Max subscribers, evaluate adding an automated escalation path alongside the pending-questions queue. They're complementary, not exclusive — automated for things that genuinely can't wait, queued for things that benefit from human attention. Triggers to revisit: (a) the credit lands as expected on or after 2026-06-15; (b) the queue grows faster than the human resolves it, and a chunk of the unresolved entries are time-sensitive rather than reflective; (c) latency on hard questions is becoming a felt problem.
- **Wake word vs always-on VAD.** Plan goes with always-on VAD to meet the 300 ms target. If false triggers become annoying, add a Porcupine or openWakeWord stage on the Pi. Trigger: > 3 false activations per hour in normal use.
- **Recent-answers buffer eviction policy.** Currently last-50-by-recency. If users find the robot routinely misled by stale facts (e.g., "the cup is Eric's" after the cup is gone), switch to a TTL-based eviction (drop entries older than 24 h) or add a "this is no longer true" tool for the human to mark a resolution as outdated. Trigger: the user (or Claude in chat, during triage) reports being misled by an old buffer entry.
- **Agent model tier.** Qwen2.5-7B is the starting point. If tool-calling reliability is < 95 % on real prompts or deferral decisions are noticeably bad, try Qwen2.5-14B-Instruct Q4_K_M (~9 GB) — VLM stays Moondream. Trigger: agent visibly mis-handles tool schemas more than once per session, or queues things it should answer locally.
- **VLM tier.** Moondream2 is fast but limited. If `see()` answers feel shallow and the queue fills with "I can't tell what that is" entries, swap to Qwen2-VL-2B (~5 GB). Trigger: > 50 % of new queue entries are `object_identification`.
- **TTS voice.** Piper `en_US-amy-medium` is a placeholder. Replace once a persona is chosen.
- **MCP transport binding.** Default stdio (Claude Desktop). If the user wants to chat with the robot from claude.ai instead, switch the HTTP/SSE binding to be accessible from the relevant connector flow. Deferred until that workflow is actually used.
- **Conversation persistence across restarts.** Conversation history is in-memory only; the recent-answers buffer is the one thing that persists. Add SQLite-backed conversation logging if restarts during interesting conversations become annoying.
- **Runtime animation uploads.** Deferred. Trigger: I'm rebuilding firmware just to add wiggles.
- **Battery monitoring, AEC.** Deferred to "future hardware revision" / "if it becomes a problem" respectively — same as r1.
- **Notification fatigue.** If the 10-minute toast throttle is still too noisy, consider quiet hours from `config.toml`. Trigger: user explicitly silences the toasts more than twice.
