# CLAUDE.md

Guidance for Claude Code working in this repository.

> **Status:** This repo is a **scaffold**. Every Python module under `desktop/`
> and `pi/` is currently a one-line docstring stub, the Teensy firmware is a
> blink placeholder, and `pi/setup.sh` is unimplemented. The *configuration*
> files are real and reflect the intended design.
>
> **Source of truth:** the full implementation plan (revision **r2**,
> 2026-05-26) lives at
> `C:\Users\persi\.claude\plans\project-robot-desk-abundant-lollipop.md`.
> Every module docstring cites it as **"Plan §N"**. That file — not this one —
> is authoritative; CLAUDE.md is the quick-reference summary.
>
> Not a git repository yet (`git init` when ready to version).

## What this is

"Jarvis 1.0" — a **differential-drive robot desk pet**. A small mobile robot
that sees (camera), hears (mic), moves/emotes, and holds spoken conversations,
driven by a **local** LLM on a desktop. When it hits something it genuinely
can't handle, it **defers the question to a human** (a SQLite-backed
pending-questions queue) instead of calling the cloud — the human later triages
the queue by chatting with Claude over their own subscription, with the robot's
tools attached via MCP.

Design priorities (from the plan): **debuggability over peak performance**,
**layered independence** (each tier testable alone), **latency hiding** (visible
reaction within ~300 ms even while slow paths run), **never visibly inert** (the
Teensy "breathes" autonomously), and **no pay-per-token surface** (the robot
software itself makes zero outbound LLM calls).

## Hardware (fixed — do not propose changes)

- **Body — Teensy 4.1:** 2× brushed DC motors + quadrature encoders, **L298N**
  H-bridge, 2 driven wheels (differential drive) + 1 caster. No IMU, no display.
- **Head — Raspberry Pi Zero 2 W:** Raspberry Pi OS Lite, headless. USB webcam
  with integrated mic. Sits on the head; connects to the Teensy in the body via
  UART + 5 V + GND through the neck.
- **Desktop:** Windows, **RTX 5070 Ti (16 GB VRAM)**, 64 GB RAM. Same LAN as Pi.
- **Cloud:** the human's existing Claude **Pro/Max subscription** (Claude
  Desktop or claude.ai) — used only when the *human* opens a chat. No API key.

## Four-tier architecture

```
  HUMAN ✕ CLAUDE  (Claude Desktop / claude.ai, subscription-billed)
        │  connects on demand to triage the pending-questions queue
        │  MCP (stdio or HTTP/SSE, localhost)
  ┌─────▼──────────────────────────── DESKTOP (Windows, 5070 Ti) ──────────┐
  │  orchestrator.py (single asyncio process)                              │
  │   ├─ wsserver (:8765, to Pi)   ├─ tts (Piper, CPU)                      │
  │   ├─ asr (faster-whisper, GPU) ├─ queue (SQLite + frame snapshots)      │
  │   ├─ vlm (Moondream2, GPU)     ├─ mcp_server (:8770, tools surface)     │
  │   ├─ agent (llama.cpp srv,GPU) ├─ notifier (toast/webhook)             │
  │   └─ motion → Teensy cmds      └─ WorldState (+ recent-answers deque)   │
  └─────┬──────────────────────────────────────────────────────────────────┘
        │  WiFi LAN — single WebSocket, channel-tagged frames
  ┌─────▼──────────────── PI ZERO 2 W (head) ──────────┐
  │  bridge.py  : UART ↔ WS transparent fwd + heartbeat │
  │  capture.py : cam (motion-gated JPEG) + mic (VAD)   │
  │  systemd: pet-bridge, pet-capture (Restart=always)  │
  └─────┬───────────────────────────────────────────────┘
        │  UART /dev/serial0 @ 921600 8N1, line-delimited JSON
  ┌─────▼──────────────── TEENSY 4.1 (body) ───────────┐
  │  1 kHz loop: PID/wheel · MotionPlanner · Animation  │
  │  Player · ReflexEngine(idle) · CommandParser ·      │
  │  Telemetry(50 Hz) · Watchdog → L298N → 2× DC+enc    │
  └─────────────────────────────────────────────────────┘
```

The MCP server runs **in-process** in the orchestrator and exposes the *same*
tool surface the local agent uses, plus the queue tools — so the human's Claude
session and the local agent see the same robot through the same interface. The
Teensy's USB serial stays exposed for bench bringup (desktop talks to it
directly, skipping the Pi).

### Transport channels (Pi ↔ Desktop WebSocket, port 8765)
- `0x01` control (JSON) · `0x02` audio (16 kHz PCM, **VAD-gated on Pi**) ·
  `0x03` video (640×480 JPEG @ 15 fps, **motion-gated on Pi**) · `0x04` UART
  passthrough. WS heartbeat 2 s; Pi reconnects with exponential backoff.
  Worst-case ~5.6 Mbps.

### Teensy ↔ Pi (UART)
`/dev/serial0` @ **921600 8N1**, newline-delimited JSON. Commands down
(`drive`, `stop`, `play`, `set_idle`, `ping`, `config`); telemetry/events up at
50 Hz. **Heartbeat 2 Hz; Teensy link-loss → soft-stop at 1500 ms.** (Note:
`bridge.py`'s docstring currently says "1 Hz heartbeat" — reconcile to the
plan's 2 Hz; see review note below.)

## Components (desktop/)

| File | Role | Plan |
|------|------|------|
| `orchestrator.py` | asyncio entry point; supervises all tasks + subprocesses | §8 |
| `config.py` | Loads `config.toml` + deep-merges `config.local.toml` overlay | §2.3 |
| `protocol.py` | Channel-tagged WS frame encode/decode (mirrored in `pi/protocol.py`) | §3.2 |
| `wsserver.py` | Pi WS server; framing + channel demux; multi-connection (bridge + capture) | §3.2, §8 |
| `asr.py` | faster-whisper; streaming partial+final transcripts | §4 |
| `vlm.py` | Moondream2 `describe(jpeg_bytes) -> str` | §4 |
| `agent.py` | **Agent brain**: llama.cpp client, tool-call loop, prompt builder | §5 |
| `tts.py` | Piper subprocess pool; sentence-streaming to local speaker | §4, §8 |
| `motion.py` | Motion intents → Teensy JSON cmds (via WS UART channel) | §3.1 |
| `mcp_server.py` | In-process MCP server: robot + queue tools (stdio + HTTP/SSE) | §3.3 |
| `pet_queue.py` | SQLite `pending_questions` + `resolved_knowledge`, frame snapshots. **Named `pet_queue`, not the plan's `queue`** — a module called `queue.py` shadows the stdlib `queue` that `concurrent.futures` imports for every `run_in_executor`, crashing the orchestrator. | §8.4 |
| `tools.py` | Shared robot + queue tool implementations; both the agent loop and the MCP server call these in-process (Plan §8.7) | §3.3, §5.1 |
| `state.py` | `WorldState`; recent-answers buffer (deque 50) | §8.3, §5.5 |
| `notifier.py` | toast / webhook / silent, throttled (1 per 10 min) | §8.5 |
| `cli_queue.py` | CLI to inspect/resolve/dismiss the queue | §9 M8 |
| `persona.md` | Static, prefix-cacheable system-prompt portion | §5.4 |

### Agent loop (§5)
Local **Qwen2.5-7B-Instruct (Q4_K_M GGUF)** via a `llama-server.exe` subprocess
(`127.0.0.1:8080`, `n_gpu_layers=99`, `ctx_size=8192`), OpenAI-compatible
function-calling. Tools: `drive`, `play_animation`, `stop`, `see`, `speak`,
`set_idle_intensity`, and **`queue_question`** (the deferral path). System prompt
= static `persona.md` (identity, rules, tool schemas, **deferral policy**, idle
guidance) + dynamic (recent-answers buffer, last ~6 turns, recent `see()`,
telemetry one-liner, new utterance).

**Deferral criteria (§5.2)** — call `queue_question` only when at least one
holds: low object-identity confidence, reasoning beyond ~3 steps, opinion/
judgment beyond competence, or genuine novelty (would be a guess). Otherwise
answer locally. *Do not queue trivial questions.*

### Deferred-question pattern (§5, §8.4) — this is the cloud replacement
`queue_question` → SQLite row + saved camera frame + pose/excerpt snapshot →
async notification (toast, throttled). The agent immediately speaks a short
in-character ack and continues. The human triages later via `cli_queue.py` or by
chatting with Claude (MCP tools: `list_pending_questions`, `get_pending_question`
— inlines the JPEG —, `resolve_pending_question(..., share_with_robot=true)`,
`dismiss_pending_question`, `summarize_queue`). Resolutions with
`share_with_robot=true` land in the **recent-answers buffer** (deque 50,
persisted to `resolved_knowledge`) and are injected into the prompt every turn,
so the robot stops re-asking. Eviction is by recency, not relevance (deliberately
simple). The agent may mention the backlog conversationally once it exceeds ~5
unresolved (max ~once/30 min).

## Components (pi/, teensy/)

- `pi/bridge.py` (§7.1) — transparent UART↔WS forwarder + **2 Hz** local Teensy
  heartbeat (keeps the body's watchdog fed independent of the desktop).
- `pi/capture.py` (§7.2) — motion-gated JPEG (0x03) + VAD-gated PCM (0x02),
  300 ms pre-roll / 500 ms hangover, `vad start/end` control bracketing.
  **No ML on the Pi.**
- `pi/wsclient.py` — shared exponential-backoff reconnect loop.
- `pi/protocol.py` — verbatim copy of `desktop/protocol.py` (keep in sync).
- `pi/systemd/` — `pet-bridge.service`, `pet-capture.service` (user `pet`,
  `WorkingDirectory=/opt/pet`, `Restart=always`).
- `teensy/src/main.cpp` (§6) — firmware. Real modules in M1/M2: MotorDriver,
  EncoderReader, PID, MotionPlanner, AnimationPlayer, ReflexEngine, CommandParser,
  Telemetry, Watchdog. Loops: control 1 kHz, telemetry 50 Hz, reflex 10 Hz,
  watchdog 100 Hz. Safety: link timeout 1500 ms → soft stop; stall → fault;
  PWM ceiling 90 %.

## Build / run

### Desktop (Python 3.11–3.12, CUDA)
```powershell
cd desktop
pip install -e ".[dev]"
python orchestrator.py          # launches llama-server, wsserver, asr, vlm, tts, mcp
python cli_queue.py             # inspect / resolve / dismiss the queue
```
External binaries/models (paths in `config.toml`; override in gitignored
`config.local.toml`):
- `C:/tools/llama.cpp/llama-server.exe` + `C:/models/qwen2.5-7b-instruct-q4_k_m.gguf`
- `C:/tools/piper/piper.exe` + `C:/models/piper/en_US-amy-medium.onnx`
- ASR `large-v3-turbo`, VLM `vikhyatk/moondream2` (auto-downloaded)

**VRAM budget (§4):** ASR ~1.6 GB + VLM ~3.0 GB + Qwen2.5-7B ~5.5 GB + ~1.5 GB
KV ≈ **11.6 GB resident of 16 GB** (~4 GB margin). Piper is CPU-only.

### Pi (Raspberry Pi Zero 2 W)
```bash
sudo ./setup.sh                 # STUB (M3): venv /opt/pet, apt deps, enable units
sudo systemctl enable --now pet-bridge pet-capture
```
Edit `pi/config.toml` in place (set `[desktop].host`). No `config.local.toml` on
the Pi.

### Teensy 4.1 (PlatformIO + Teensyduino)
```bash
cd teensy
pio run -e teensy41 -t upload   # 921600 monitor, teensy-cli upload
```

### Lint / test
A project virtualenv at `.venv/` (gitignored) holds the lightweight deps needed
to lint and test without the GPU/hardware stack (heavy libs are lazy-imported):
```powershell
python -m venv .venv
.\.venv\Scripts\pip install ruff pytest pytest-asyncio websockets httpx "mcp>=1.0" numpy
.\.venv\Scripts\ruff check desktop pi      # clean
.\.venv\Scripts\python -m pytest desktop   # 32 tests: protocol, queue, state, config,
                                           #   tts splitter, tools, agent loop, ws loopback
.\.venv\Scripts\python -m pytest pi        # 6 tests: protocol, VAD audio-gate
```
Tests cover the hardware-/model-free logic. The Teensy firmware needs PlatformIO
to build; the full orchestrator needs the models + a connected Pi/Teensy.

## Conventions

- Each module docstring cites its **Plan §** — keep cross-refs accurate.
- Config split: committed `config.toml` = defaults; secrets/per-machine →
  gitignored `config.local.toml` (desktop) / edit in place (Pi).
- Channel bytes (`0x01–0x04`), 921600 baud, and timeouts (1500 ms link-loss,
  2 s WS heartbeat) must stay in sync across `wsserver.py`, `bridge.py`,
  `capture.py`, and the firmware.
- The local agent calls tool **implementations directly** (in-process); the MCP
  wire protocol is for the *human's* Claude client only (§8.7). Don't route the
  local loop through the MCP socket.
- Robot never drives motors directly — always via the Teensy's high-level cmds.
- Gitignored artifacts: `data/queue.sqlite*`, `data/pending_frames/*.jpg`,
  `__pycache__`, `.pio/`, `.venv/`, `config.local.toml`.

## Build & test order (§9)

| M | Goal |
|---|------|
| M1 | Teensy motion bringup (USB serial only): MotorDriver/Encoder/PID/MotionPlanner/CommandParser |
| M2 | Teensy animations + ReflexEngine idle + Watchdog (unplug → stop ≤1.5 s) |
| M3 | Pi bridge bringup (UART↔WS through the neck); systemd `Restart=always` |
| M4 | Pi capture: motion-gated JPEG + VAD-bounded PCM on correct channels |
| M5 | Desktop models concurrently (verify VRAM < 13 GB); wav → ASR → agent → TTS |
| M6 | Full voice loop + motion; tune "ack within ~300 ms" |
| M7 | VLM `see()` into the agent; 30-min sustained-operation test |
| M8 | Pending-questions queue + `queue_question` + `cli_queue.py` + recent-answers buffer |
| M9 | MCP server (stdio + HTTP/SSE) + Claude Desktop end-to-end + toast notifier |
| M10 | Persona + animation library + idle/TTS/ack polish |
| M11 | Reliability hardening: fault injection, structured logging, webhook backend |

---

## Architecture review — notes & open risks

Reviewed by Claude (Opus 4.8) against plan r2. The design is **sound** and
notably well-reasoned: clean tier independence, local reflexes that keep the body
safe through Pi/desktop dropout, an explicit latency-hiding order of operations,
and a deliberate no-API-key posture. The plan already anticipates most failure
modes (§8.7) and carries its own revisit triggers (§10). Items below are things
to **validate while building**, not design flaws.

1. **Pi UART at 921600 needs the PL011, not the mini-UART.** On the Pi Zero 2 W,
   `/dev/serial0` defaults to the mini-UART (Bluetooth holds the stable PL011),
   whose baud tracks the core clock and is flaky at high rates. Add
   `dtoverlay=disable-bt` + disable the serial console so PL011 lands on the GPIO
   header — or drop the baud. **Bake this into `setup.sh` (M3).** Not mentioned
   in §7.

2. **Firmware must read the Pi link on `Serial1`, not USB `Serial`.** The plan
   uses USB serial for bench bringup (M1) — correct — but the production path is
   Pi→Teensy UART on pins 0/1 = `Serial1`. The placeholder uses USB `Serial`;
   `CommandParser`/`Telemetry` need to bind `Serial1` (ideally both, switchable).
   Voltages are fine (both 3.3 V; the "5 V through the neck" is power, not signal
   — and Teensy 4.1 pins are **not** 5 V tolerant, so keep signal at 3.3 V).

3. **Pi↔Desktop WebSocket is unauthenticated and binds `0.0.0.0:8765`.** §3.2
   defines no handshake auth, so anyone on the LAN could connect, drive the
   motors, or read the camera/mic streams. The MCP HTTP binding already uses a
   bearer token (§3.3) — mirror that on the WS link, or bind to the Pi's
   specific address + firewall. The A/V feed is the bigger privacy exposure.

4. **Heartbeat-rate inconsistency.** `bridge.py`'s docstring says "1 Hz
   heartbeat"; plan §3.1 says **2 Hz** with a 1500 ms Teensy link-loss timeout.
   At 1 Hz a single dropped heartbeat (2000 ms gap) trips a false fault; 2 Hz
   (500 ms) is the safe choice. Reconcile the docstring to the plan.

5. **The pet speaks from the desktop, not its body.** `tts.py`/§8.2 send TTS to
   the *desktop* local speaker, and there's no downstream audio channel to the
   Pi. That's a legitimate choice for a desk pet sitting by the PC — just confirm
   it's intended; giving it a body-voice would mean adding a Pi-ward audio
   channel + playback.

6. **VRAM is tight but accounted for.** §4 budgets ~11.6 GB on the 16 GB 5070 Ti.
   Realistic — but Windows/WDDM reserves VRAM and KV grows with context, so M5's
   "< 13 GB" gate is the real check. Watch fragmentation across the three
   resident models; §8.7's OOM-reload path is the backstop.

7. **Default bearer token.** `http_bearer_token = "change-me-in-config.local.toml"`
   — consider failing fast if it's left at the default while `enable_http = true`.

### On the original ask — "Opus 4.8 released, revise the plan"

**No plan change is required, and that's by design.** Plan **r2 deliberately
removed** the automated `ask_claude` cloud path (r1 had it); cloud capability now
arrives only through the *human's* Claude subscription chat over MCP. That makes
the architecture **model-agnostic on the cloud side** — Opus 4.8 shipping simply
means the human's triage chats are now smarter, with nothing in the repo to
update. There is no cloud model ID pinned anywhere to bump.

The one place a future model *could* re-enter automatically is §10's deferred
item: the Anthropic Agent SDK's expected **2026-06-15** monthly subscription
credit for Pro/Max users, which would make an automated escalation path feasible
*alongside* (not replacing) the queue. That remains explicitly deferred, and per
your instruction the architecture stays as-is. If you ever want to revisit it,
`agent.py` (§5 tool set) is the single seam — but it's a §10 decision, not a
consequence of Opus 4.8's release.
