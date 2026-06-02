# Cognition — the "alive" loop (Plan §12)

This is what makes the robot feel like a *creature* rather than a voice assistant:
a continuous **internal monologue**, a **persistent memory that learns**, a
**mood** that drifts over the day, and **proactive behavior** driven by what it's
thinking. It is **on by default** (`[cognition].enable = true`); set it to `false`
as a kill switch. When on, it **subsumes** the old random `[idle]` loop.

## How it works

A single background task (`cognition.CognitionEngine.run`, supervised by the
orchestrator) wakes every `tick_interval_s` (± `jitter_s`). Each tick, **only when
the agent is otherwise idle** (the `_busy` lock is free — a live conversation
always wins):

1. **Perceive** — time of day, what it can see right now (`state.fresh_vision()`,
   in-the-moment only), body telemetry, how long it's been quiet.
2. **Recall** — embed the perception and retrieve the top-K relevant memories,
   scored **recency × importance × relevance** (Park et al. 2023, *Generative
   Agents*).
3. **Think** — `agent.think(...)` runs a *private* completion. The returned text is
   a **thought**, never spoken. The model may emote/glance (silent tools); it is
   only given the `speak` tool on speak-eligible ticks.
4. **Express & record** — show the mood on the OLED eyes, store the thought as a
   memory, nudge the mood, and emit `thought`/`mood` events to the dashboard.
5. **Reflect** — when enough has happened (summed memory importance since the last
   reflection > `reflection_importance_threshold`), synthesize higher-level
   insights from recent memories and store them as high-importance `reflection`
   memories. This is the mechanism by which behavior **changes over time**.

### Why it isn't chatty (Balanced)
Spontaneous speech passes **three independent code gates**: a probability
(`speak_probability`), a cooldown (`speak_cooldown_s`), and tool-availability
(non-eligible ticks aren't given the `speak` tool at all). Emoting and glancing
are free and frequent; talking is occasional.

## Memory (`memory.py`, `data/memory.sqlite`)

A separate SQLite stream of `dialogue`, `resolution` (facts a human taught it via
the queue), `thought`, and `reflection` rows — each with an importance score and a
384-d embedding. Retrieval is numpy brute-force over a cached matrix (no native
vector extension; fine to thousands of rows). It is injected into **every spoken
turn** too (via `agent.memory_render`), so the robot stops meeting you for the
first time each time.

**Vision is not persisted.** Per the project decision, raw camera observations are
used only in the moment; a thought may *mention* what was seen, but no
`observation`/image memory is written (enforced by simply not wiring a
`see()` → memory hook). `[memory].persist_vision` is a reserved flag for the
future, not currently read.

## Mood (`mood.py`)

A PAD (pleasure / arousal / dominance) vector in [-1, 1] that decays toward a
gentle circadian baseline (`[mood]`), is nudged by events, colors generation (one
short prompt line), and maps to one of the 15 OLED expressions. Persisted in the
memory DB's `kv` table, so a restart resumes the same disposition.

## Configuration

All knobs live in `config.toml` (`[cognition]`, `[memory]`, `[mood]`); override in
the gitignored `config.local.toml`. Start conservative and tune by watching the
Observatory dashboard (`[dashboard].enable = true`) — the LM Studio column's
EXECUTING pane streams `thought` / `mood` / `reflection` events live.

## Observability

The seam emits `("lmstudio", "exec", "thought"|"mood"|"reflection", …)` through the
existing event bus, so they appear on the four-layer dashboard with **zero**
dashboard code changes. All no-ops when the dashboard is off.

## Tuning latency & the future model swap

The cognition loop calls the LLM *frequently* (a tick plus per-turn memory
injection), so a smaller/faster model with a bigger context helps. Today this
machine runs **LM Studio + Gemma-26B (unified vision)**; cognition works on it,
but a tick on a 26B model is heavy — keep `tick_interval_s` generous and watch the
logs.

**Recommended future step — move off LM Studio to managed llama.cpp + a small
model (e.g. Qwen3-8B):**

- Set `[agent].manage_server = true` and point `model_path` at the GGUF.
- Switch vision back to `[vlm].mode = "split"` (Moondream2 captions frames) since a
  small text model isn't multimodal — *or* load a small multimodal GGUF and keep
  `unified`.
- Enable prompt/KV caching so frequent ticks are cheap (the static `persona.md`
  prefix stays cached; only the short dynamic block is re-encoded each tick). The
  `[agent]` keys are already plumbed through `llama_server.build_args`:
  `cache_reuse`, `cache_type_k`/`cache_type_v` (KV quant → bigger context fits in
  16 GB), `flash_attn`, `slot_save_path`. See the commented block in `config.toml`.
- Then lower `tick_interval_s` for a livelier robot.

LM Studio also caches a stable prefix automatically and exposes KV-cache quant +
context length in its model-load UI, so a smaller model there is a pure-config
swap (no `build_args` involved) — but managed llama.cpp gives the most control over
caching for this frequent-tick workload.

## Model compatibility notes (small / local models)

Benchmarked Gemma-4-E4B (NVFP4) vs Gemma-4-26B on a 5070 Ti: the E4B is ~2.7× faster
(~75 tok/s vs ~28, TTFT ~0.27s), giving sub-second replies / ~0.25s ticks / ~0.5s
reflections — a snappy, lifelike feel. Getting a small model to drive the robot
reliably surfaced a few things the agent now handles, and a couple of rules:

- **The agent tolerates tool calls emitted as text.** Small models often write
  `set_emotion("happy")` as plain content instead of using the function-calling
  interface. `agent._parse_text_tool_calls` / `_dispatch_text_calls` parse those
  (handling markdown bullets, backticks, several per line) and *execute* them, so the
  robot acts instead of speaking the syntax aloud. Larger models that call tools
  natively are unaffected.
- **Leaked chain-of-thought is stripped** (`_strip_reasoning`): `<think>…</think>`,
  Gemma `<|channel>…`, and stray `<|thought|…` never reach the voice or the memory.
- **`persona.md` must not contain literal tool-call syntax.** A 4B will imitate any
  `name("arg")` example as text output (confirmed: with examples → ~1/3 used the real
  API; without → 3/3). Keep tool usage described in prose; let the JSON tool schema
  carry the signatures.
- **Silent cognition ticks offer no tools** (`think()` passes `tools=[]` when not
  speaking) so the model returns a plain-words thought instead of tool-call syntax;
  the eyes are driven from mood by the loop regardless.
- **Chat-template gotcha (LM Studio / minja).** A GGUF's bundled Jinja template may
  use full-Jinja features minja doesn't support. The FreedomAISVR Gemma-4-E4B-NVFP4
  build needed two fixes to render `tools`: (1) reorder macros so `format_argument`
  is defined before the macros that call it (minja binds macro names eagerly), and
  (2) `is sequence` → `is iterable`. Fix is applied as the model's Prompt Template
  override in LM Studio; a copy lives next to the model as
  `FIXED-chat-template-paste-into-lmstudio.jinja`. If tool calls 400 with a Jinja
  error, prefer an `lmstudio-community` build (ships minja-safe templates).
- **Tradeoff to know:** the E4B is fast and acts reliably, but its *prose* (inner
  thoughts, reflection insights) is thinner than the 26B's. For both snappiness and a
  richer inner life, **Qwen3-8B-instruct** is the sweet spot (native tool-calling +
  better prose, still fast).
