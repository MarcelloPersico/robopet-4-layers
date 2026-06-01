# CLAUDE.md

Guidance for Claude Code working in this repository.

> **State (2026-05-30):**
> - **`desktop/`** — fully implemented and verified on this machine; runs
>   end-to-end (text / voice / vision). 62 tests pass (`desktop/tests`).
> - **`pi/`** — implemented (`bridge.py`, `capture.py`, `wsclient.py`,
>   `protocol.py`, `setup.sh`; 6 tests).
> - **`teensy/`** — firmware implemented (`main.cpp` + MotorDriver, EncoderReader,
>   PID, MotionPlanner, AnimationPlayer, ReflexEngine, CommandParser, Telemetry,
>   Watchdog headers); not yet bench-verified on hardware (M1/M2 bringup).
>
> **Source of truth:** the full implementation plan (revision **r2**,
> 2026-05-26) lives at
> `C:\Users\persi\.claude\plans\project-robot-desk-abundant-lollipop.md`.
> Each module docstring cites it as **"Plan §N"**; that file is authoritative,
> this is the quick-reference summary.

## What this is

"Jarvis 1.0" — a **differential-drive robot desk pet**. A small mobile robot
that sees (camera), hears (mic), moves/emotes, and holds spoken conversations,
driven by a **local** LLM on a desktop. When it hits something it genuinely
can't handle, it **defers the question to a human** (a SQLite-backed
pending-questions queue) instead of calling the cloud — the human later triages
the queue by chatting with Claude over their own subscription, with the robot's
tools attached via MCP.

Design priorities: **debuggability over peak performance**, **layered
independence** (each tier testable alone), **latency hiding** (visible reaction
within ~300 ms even while slow paths run), **never visibly inert** (the Teensy
"breathes" autonomously), and **no pay-per-token surface** (the robot software
makes zero outbound LLM calls).

## Hardware (fixed — do not propose changes)

- **Body — Teensy 4.1:** 2× brushed DC motors + quadrature encoders, **L298N**
  H-bridge, 2 driven wheels (differential drive) + 1 caster. No IMU, no display.
- **Head — Raspberry Pi Zero 2 W:** Raspberry Pi OS Lite, headless. USB webcam
  with integrated mic. Connects to the Teensy via UART + 5 V + GND through the neck.
- **Desktop:** Windows, **RTX 5070 Ti (16 GB VRAM)**, 64 GB RAM. Same LAN as Pi.
- **Cloud:** the human's existing Claude **Pro/Max subscription** — used only
  when the *human* opens a chat. No API key.

## Four-tier architecture

```
  HUMAN ✕ CLAUDE  (Claude Desktop / claude.ai, subscription-billed)
        │  connects on demand to triage the pending-questions queue (MCP)
  ┌─────▼──────────────────────────── DESKTOP (Windows, 5070 Ti) ──────────┐
  │  orchestrator.py (single asyncio process)                              │
  │   ├─ wsserver (:8765, to Pi)   ├─ tts (Piper/Kokoro, CPU)               │
  │   ├─ asr (faster-whisper, GPU) ├─ pet_queue (SQLite + frame snapshots)  │
  │   ├─ vlm (Moondream2, GPU)     ├─ mcp_server (:8770, tools surface)     │
  │   ├─ agent (OpenAI-compat,GPU) ├─ notifier (toast/webhook)             │
  │   └─ motion → Teensy cmds      └─ WorldState (+ recent-answers deque)   │
  └─────┬──────────────────────────────────────────────────────────────────┘
        │  WiFi LAN — single WebSocket, channel-tagged frames
  ┌─────▼──────────────── PI ZERO 2 W (head) ──────────┐
  │  bridge.py  : UART ↔ WS transparent fwd + heartbeat │
  │  capture.py : cam (motion-gated JPEG) + mic (VAD)   │
  └─────┬───────────────────────────────────────────────┘
        │  UART /dev/serial0 @ 921600 8N1, line-delimited JSON
  ┌─────▼──────────────── TEENSY 4.1 (body) ───────────┐
  │  1 kHz loop: PID/wheel · MotionPlanner · Animation  │
  │  · ReflexEngine(idle) · Telemetry · Watchdog → L298N│
  └─────────────────────────────────────────────────────┘
```

The MCP server runs **in-process** in the orchestrator and exposes the *same*
tool surface the local agent uses, plus the queue tools — so the human's Claude
session and the local agent see the same robot through the same interface. The
Teensy's USB serial stays exposed for bench bringup.

### Channels & links
- **Pi ↔ Desktop WS (8765):** `0x01` control (JSON) · `0x02` audio (16 kHz PCM,
  VAD-gated on Pi) · `0x03` video (640×480 JPEG @ 15 fps, motion-gated on Pi) ·
  `0x04` UART passthrough. WS heartbeat 2 s; Pi reconnects with backoff.
- **Teensy ↔ Pi UART:** `/dev/serial0` @ **921600 8N1**, newline-delimited JSON.
  Commands down (`drive`, `stop`, `play`, `set_idle`, `ping`, `config`);
  telemetry/events up at 50 Hz. Heartbeat 2 Hz; link-loss → soft-stop at 1500 ms.

## Components (desktop/)

| File | Role | Plan |
|------|------|------|
| `orchestrator.py` | asyncio entry point; supervises all tasks + subprocesses | §8 |
| `config.py` | Loads `config.toml` + deep-merges `config.local.toml` overlay | §2.3 |
| `protocol.py` | Channel-tagged WS frame encode/decode (mirrored in `pi/protocol.py`) | §3.2 |
| `wsserver.py` | Pi WS server; framing + channel demux; multi-connection | §3.2, §8 |
| `asr.py` | faster-whisper; streaming partial+final transcripts; anti-hallucination gate | §4 |
| `vlm.py` | Moondream2 `describe(jpeg_bytes) -> str` | §4 |
| `agent.py` | Agent brain: OpenAI-compatible client, tool-call loop, prompt builder, streaming TTS | §5 |
| `llama_server.py` | Launch/readiness; `manages()` picks managed (we spawn llama.cpp) vs external (e.g. LM Studio) | §5, §8.1 |
| `tts.py` | Sentence-streaming TTS; `build_tts()` → `TTS` (Piper) / `KokoroTTS` (Kokoro-82M) | §4, §8 |
| `half_duplex.py` | `SpeakingState` — mutes the mic while the pet speaks (anti-echo) | §8.7 |
| `motion.py` | Motion intents → Teensy JSON cmds (via WS UART channel) | §3.1 |
| `mcp_server.py` | In-process MCP server (live HTTP/SSE only): robot + queue tools | §3.3 |
| `pet_queue.py` | SQLite `pending_questions` + `resolved_knowledge`, frame snapshots. Named `pet_queue` (not `queue`) to avoid shadowing the stdlib `queue` that `concurrent.futures` imports. | §8.4 |
| `tools.py` | Shared robot + queue tool implementations; agent loop and MCP server both call these in-process | §3.3, §5.1 |
| `state.py` | `WorldState`; recent-answers buffer (deque 50) | §8.3, §5.5 |
| `notifier.py` | toast / webhook / silent, throttled (1 per 10 min) | §8.5 |
| `cli_queue.py` | CLI to inspect/resolve/dismiss the queue | §9 M8 |
| `local_loop.py` | Run the pet brain on the desktop alone (no Pi/Teensy) | §8.7 |
| `persona.md` | Static, prefix-cacheable system-prompt portion | §5.4 |

### Agent loop (§5)
Local LLM via an OpenAI-compatible server (managed `llama-server.exe` or external
LM Studio), function-calling. Tools: `drive`, `play_animation`, `stop`, `see`,
`speak`, `set_idle_intensity`, and **`queue_question`** (the deferral path).
System prompt = static `persona.md` + dynamic context (recent-answers buffer,
last ~6 turns, recent `see()`, telemetry one-liner, new utterance).

**Deferral criteria (§5.2)** — call `queue_question` only when at least one holds:
low object-identity confidence, reasoning beyond ~3 steps, opinion/judgment
beyond competence, or genuine novelty. Otherwise answer locally; don't queue
trivial questions.

### Defer-to-human loop (§5, §8.4) — the cloud replacement
`queue_question` → SQLite row + saved camera frame + pose/excerpt snapshot →
throttled toast. The agent speaks a short in-character ack and continues. The
human triages later via `cli_queue.py` or by chatting with Claude over MCP. The
server ships **`instructions`** (mcp_server.py `INSTRUCTIONS`) that keep the
human's Claude on a tight loop — `next_pending_question` (oldest one, inlines the
JPEG) → answer → `resolve_pending_question(..., share_with_robot=true)` → repeat —
instead of free-form reasoning or driving the body itself (`list_pending_questions`,
`get_pending_question`, `dismiss_pending_question`, `summarize_queue` round out the
surface). Shared resolutions land in `resolved_knowledge`.

The robot **learns answers back** immediately: resolving over the live in-process
HTTP binding updates the `WorldState` recent-answers buffer **and hands the answer
to the agent so the robot reacts on the spot** (`tools.agent_deliver` →
`orchestrator._deliver_to_agent` → `agent.deliver_answer`, run under `_busy`). The
resolution is also persisted to `resolved_knowledge`, and **`orchestrator.run()`
re-seeds the buffer from it at boot**
(`state.load_resolutions(queue.load_recent_resolutions())`) so the robot doesn't
re-ask across restarts. The buffer is injected into the prompt every turn; eviction
is by recency (deliberately simple). The server is **live-only** — there is no
offline/stdio queue-only mode. See **`desktop/MCP_SETUP.md`** for wiring the
HTTP/SSE binding.

### Runtime switches (gitignored `config.local.toml`, deep-merged over `config.toml`)
- **`[agent] manage_server`** — `true` launches & owns `llama-server.exe`;
  `false` connects to a running OpenAI-compatible server (e.g. LM Studio).
- **`[agent] stream`** — `true` streams the reply and feeds `speak()` text to TTS
  sentence-by-sentence. Only `speak()` is voiced; private reasoning is never spoken.
- **`[agent] model`** — id sent in the request (llama.cpp ignores it; LM Studio selects on it).
- **`[vlm] mode`** — `split` (Moondream captions frames for `see()`, default) or
  `unified` (no separate VLM; the agent LLM is multimodal and `see()` injects the
  raw JPEG as an `image_url`).
- **`[tts] backend`** — `piper` (CPU, default) or `kokoro` (Kokoro-82M neural).
- **`[asr] vad_filter`** — adds Silero VAD on the final pass; off by default.
- **`[mcp]`** — `enable_http`/`http_host`/`http_port`/`http_bearer_token` for the
  live in-process HTTP/SSE binding (localhost-only by default).

**This machine's live config:** LM Studio serving **Gemma-4-26B-A4B** (multimodal
MoE) on `:1234` (`manage_server=false`), `mode="unified"`, `stream=true`,
`backend="kokoro"` (CPU). Tool-calling + vision verified through LM Studio.

## Components (pi/, teensy/)

- `pi/bridge.py` (§7.1) — transparent UART↔WS forwarder + **2 Hz** Teensy heartbeat.
- `pi/capture.py` (§7.2) — motion-gated JPEG (0x03) + VAD-gated PCM (0x02),
  300 ms pre-roll / 500 ms hangover. **No ML on the Pi.**
- `pi/wsclient.py` — shared exponential-backoff reconnect loop.
- `pi/protocol.py` — verbatim copy of `desktop/protocol.py` (keep in sync).
- `pi/systemd/` — `pet-bridge.service`, `pet-capture.service` (`Restart=always`).
- `teensy/src/main.cpp` + headers (§6) — firmware: MotorDriver, EncoderReader,
  PID, MotionPlanner, AnimationPlayer, ReflexEngine, CommandParser, Telemetry,
  Watchdog. Loops: control 1 kHz, telemetry 50 Hz, reflex 10 Hz, watchdog 100 Hz.
  Safety: link timeout 1500 ms → soft stop; stall → fault; PWM ceiling 90 %.

## Build / run

### Desktop (Python 3.11–3.12, CUDA)
```powershell
cd desktop
pip install -e ".[dev]"
python orchestrator.py          # launches the LLM server, wsserver, asr, vlm, tts, mcp
python cli_queue.py             # inspect / resolve / dismiss the queue
```

Run on the desktop alone (no Pi/Teensy; motion echoed to console):
```powershell
python local_loop.py            # text REPL
python local_loop.py --voice            # mic (Whisper, GPU by default)
python local_loop.py --voice --vision   # add see() via the webcam (Moondream)
python local_loop.py --no-tts           # print speech instead of synthesizing
```

External binaries/models (paths in `config.toml`; override in `config.local.toml`):
- `C:/tools/llama.cpp/llama-server.exe` (Vulkan) + `C:/tools/llama.cpp-cuda/` (CUDA)
  + `C:/models/qwen2.5-7b-instruct-q4_k_m.gguf`
- `C:/tools/piper/piper.exe` + `C:/models/piper/en_US-amy-medium.onnx`
- ASR `large-v3-turbo`, VLM `vikhyatk/moondream2` (auto-downloaded)
- Kokoro-82M (`pip install kokoro`, auto-downloads); LM Studio runs Gemma on `:1234`

GPU notes: Whisper runs on GPU (~0.14 s/utterance) via ctranslate2's CUDA-12 build
(`nvidia-cublas-cu12` + `nvidia-cudnn-cu12`; `asr.py` registers their DLL dirs).
torch **2.12.0+cu132** (CUDA 13.2, `sm_120`) for Moondream. **VRAM budget (§4):**
ASR ~1.6 GB + VLM ~3.0 GB + 7B ~5.5 GB + ~1.5 GB KV ≈ 11.6 GB of 16 GB.

### Pi (Raspberry Pi Zero 2 W)
```bash
sudo ./setup.sh                 # provisions venv /opt/pet, apt deps, systemd units
sudo systemctl enable --now pet-bridge pet-capture
```
Edit `pi/config.toml` in place (set `[desktop].host`). No `config.local.toml` on the Pi.

### Teensy 4.1 (PlatformIO + Teensyduino)
```bash
cd teensy && pio run -e teensy41 -t upload
```

### Lint / test
A `.venv/` (gitignored) holds the lightweight deps to lint/test without the
GPU/hardware stack (heavy libs are lazy-imported):
```powershell
python -m venv .venv
.\.venv\Scripts\pip install ruff pytest pytest-asyncio websockets httpx "mcp>=1.0" numpy
.\.venv\Scripts\ruff check desktop pi          # clean
.\.venv\Scripts\python -m pytest desktop       # 62 tests (protocol, queue, state, config,
                                               #   tts, tools, agent loop, asr, half-duplex,
                                               #   ws loopback, MCP defer-to-human end-to-end)
.\.venv\Scripts\python -m pytest pi            # 6 tests (protocol, VAD audio-gate)
```
The Teensy firmware needs PlatformIO; the full orchestrator needs the models + a
connected Pi/Teensy.

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
- Gitignored: `data/queue.sqlite*`, `data/pending_frames/*.jpg`, `__pycache__`,
  `.pio/`, `.venv/`, `config.local.toml`.

## Build & test order (§9)

| M | Goal |
|---|------|
| M1 | Teensy motion bringup (USB serial): MotorDriver/Encoder/PID/MotionPlanner/CommandParser |
| M2 | Teensy animations + ReflexEngine idle + Watchdog (unplug → stop ≤1.5 s) |
| M3 | Pi bridge bringup (UART↔WS through the neck); systemd `Restart=always` |
| M4 | Pi capture: motion-gated JPEG + VAD-bounded PCM on correct channels |
| M5 | Desktop models concurrently (VRAM < 13 GB); wav → ASR → agent → TTS |
| M6 | Full voice loop + motion; tune "ack within ~300 ms" |
| M7 | VLM `see()` into the agent; 30-min sustained-operation test |
| M8 | Pending-questions queue + `queue_question` + `cli_queue.py` + recent-answers buffer ✅ |
| M9 | MCP server (live HTTP/SSE) + Claude triage end-to-end + toast notifier ✅ |
| M10 | Persona + animation library + idle/TTS/ack polish |
| M11 | Reliability hardening: fault injection, structured logging, webhook backend |

## Open risks (validate while building)

1. **Pi UART at 921600 needs the PL011, not the mini-UART.** On the Pi Zero 2 W,
   `/dev/serial0` defaults to the mini-UART (flaky at high baud). Add
   `dtoverlay=disable-bt` + disable the serial console — bake into `setup.sh` (M3).
2. **Firmware must read the Pi link on `Serial1`, not USB `Serial`.** USB serial
   is bench-only (M1); production is Pi→Teensy on pins 0/1 = `Serial1`. Keep signal
   at 3.3 V (Teensy 4.1 pins are not 5 V tolerant; the neck's 5 V is power only).
3. **Pi↔Desktop WS is unauthenticated and binds `0.0.0.0:8765`.** Anyone on the
   LAN could drive motors or read A/V. Mirror the MCP bearer token, or bind to the
   Pi's address + firewall. The MCP HTTP binding already warns if bound off-localhost
   with the default token; the WS link still needs the same treatment.
4. **VRAM is tight** (~11.6 GB of 16 GB). M5's "< 13 GB" gate is the real check;
   watch fragmentation across the three resident models.

**Cloud is human-only by design.** Plan r2 deliberately removed the automated
cloud path; cloud capability arrives only through the human's Claude subscription
chat over MCP. The architecture is model-agnostic on the cloud side. The one place
a future model could re-enter automatically is §10's deferred Agent-SDK
subscription-credit item — `agent.py` (§5 tool set) is the single seam, but it's a
§10 decision, kept deferred.
