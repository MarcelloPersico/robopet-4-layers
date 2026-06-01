# Teensy 4.1 bench bringup — M1 (motion) + M2 (animations/reflex/watchdog) + dual-OLED eyes

Bench runbook for bringing the body firmware up on the desk over **USB serial**
(no Pi, no neck) and then power-on testing the two **SSD1306 OLED "eyes."**
Plan §6, §9 (milestones M1/M2). Cites the actual firmware sources in `teensy/src/`.

> **Status / what is verified where**
> - **BENCH-VERIFIED ON HARDWARE 2026-06-01 (USB serial, Teensy 4.1 + both OLEDs +
>   both motors/encoders on a 6 V pack):**
>   - ✅ PlatformIO build + headless `teensy-cli` flash (built `teensy_loader_cli`
>     from source — no prebuilt Windows binary exists).
>   - ✅ 50 Hz telemetry stream, line-JSON command parser, `ping`→`pong`, watchdog
>     link-feed, idle "breathing" (reflex), `set_emotion`/`look`/`blink`.
>   - ✅ **Dual-OLED eyes** render + animate on both panels. **Required a fix:**
>     `FaceController::begin()` now calls `setPowerSave(0)` after `initDisplay()` —
>     the SSD1306 init sequence ends with the panel OFF (`0x0ae`), so both eyes
>     ACK'd on I2C (0x3C on `Wire` 18/19 + `Wire1` 17/16) but stayed dark until
>     powered on.
>   - ✅ Both motors drive; both encoders count; closed-loop PID (left side shows
>     full loop closure); 90 % PWM ceiling held; **stall-fault watchdog** latches +
>     cuts motors on a no-movement wheel.
> - **STILL TODO ON HARDWARE (deferred to assembly / M1 tuning):**
>   - Per-wheel polarity convention — the RIGHT encoder sign is inverted (forward
>     command → negative counts), so the right wheel runs open-loop/pegged until you
>     set "forward = positive counts" at assembly (swap right encoder ChA↔ChB on
>     20/21 *or* M+/M−, whichever matches the left).
>   - `counts_per_rev`/geometry velocity scaling (reported `vel` is mis-scaled).
>   - The link-loss **unplug → soft-stop ≤ 1.5 s** test (M2 safety gate).
>   - The `play` animation set (`nod`/`wiggle`/…).
> - **Dev-verifiable without the robot (and green):** the pure emotion/tween math
>   (`teensy/test/test_emotion_logic.cpp` via `run_host_tests.{ps1,sh}`, needs only
>   host `g++`) and the entire desktop side (`pytest desktop` → 84 passing,
>   `ruff check desktop pi` → clean).

---

## Step 0 — SAFETY FIRST: 3.3 V-only OLED power (do this before any I2C wire)

**Teensy 4.1 pins are NOT 5 V tolerant.** Power **each** OLED module's `VCC`
from the Teensy **3.3 V** rail so its SDA/SCL lines (and the module's on-board
pull-ups) idle at **3.3 V**.

- **NEVER** power the OLED logic from the neck's **5 V** — that 5 V is motor/Pi
  power only. A 5 V-powered SSD1306 module pulls the I2C lines to 5 V and can
  **damage the MCU**.
- **Verify `VCC = 3.3 V` with a meter on each module BEFORE you connect SDA/SCL.**
- If you must use a 5 V-only display module, add a proper bidirectional I2C level
  shifter between it and the Teensy. (The plain modules in this build are 3.3 V.)

This is a hard design dependency, not a suggestion — it is repeated in
`Config.h` (`namespace cfg::face`) and `FaceController.h`.

---

## Final wiring table (Teensy 4.1)

Everything except the two eye buses is **unchanged** from the original M1/M2 pin
map (`teensy/src/Config.h`). The eyes are purely additive.

| Function              | Teensy pin(s)         | Bus / notes                                              |
|-----------------------|-----------------------|---------------------------------------------------------|
| Left motor (L298N)    | ENA=2, IN1=3, IN2=4   | L298N **logic** inputs; motor leads + power → next section |
| Right motor (L298N)   | ENB=5, IN3=6, IN4=7   | "                                                       |
| LEFT encoder          | 14, 15                | **DO NOT MOVE**                                          |
| RIGHT encoder         | **20, 21**            | relocated from 16/17 to free **Wire1** for the right eye |
| Pi link `Serial1`     | RX=0, TX=1            | 921600 8N1 line-JSON (production)                        |
| USB Serial            | USB                   | bench/bringup link (M1)                                  |
| LED                   | 13 (`LED_BUILTIN`)    | heartbeat                                                |
| **LEFT eye (SSD1306)**| **SDA=18, SCL=19**    | **`Wire` (LPI2C1)** via U8g2 `_HW_I2C`, addr **0x3C**, clock `cfg::face::BUS_CLOCK_HZ` |
| **RIGHT eye (SSD1306)**| **SDA=17, SCL=16**   | **`Wire1` (LPI2C3)** via U8g2 `_2ND_HW_I2C`, addr **0x3C** (separate bus ⇒ same addr OK, no jumper) |

**Zero pin conflict:** eye pins `{16,17,18,19}` (Wire1 + Wire) do not overlap
motors `{2..7}`, encoders `{14,15,20,21}`, `Serial1 {0,1}`, or the LED `{13}`.

**Why Wire1 + relocate the encoder:** U8g2's stock HW-I2C classes drive only
`Wire` (`_HW_I2C`) and `Wire1` (`_2ND_HW_I2C`) — there is **no `Wire2` class and no
`TwoWire*` constructor argument**. Rather than hand-write a custom Wire2 byte
callback (non-standard, harder to debug), we put the right eye on **`Wire1`**
(pins 16/17) and **relocate the RIGHT encoder to 20/21** (any Teensy 4.1 digital
pins work — all are interrupt-capable, so the `Encoder` lib is unaffected).
`FaceController::begin()` calls `Wire.setSDA(18)/setSCL(19)` and
`Wire1.setSDA(17)/setSCL(16)` before `initDisplay()` to guard against a future core
default change (17/16 are already the Wire1 defaults).

Each OLED also needs `GND` common with the Teensy and `VCC` → **3.3 V** (Step 0).

---

## L298N + motor/encoder harness wiring

The table above is the **Teensy-side** pin map: it covers the L298N *logic
inputs* (ENA/IN1/IN2, ENB/IN3/IN4) and the encoder *signal* pins, but not the
L298N's power/output side or how the motor+encoder connector itself lands. This
section closes that gap — it's the board-level harness, not anything in firmware.

### The motor+encoder connector (6 pins: `M+ M− Vcc Gnd ChA ChB`)

Each gearmotor breaks out as **two electrically separate halves on one
connector**: the motor winding (`M+`/`M−`, high-current, → L298N) and the
quadrature encoder (`Vcc`/`Gnd`/`ChA`/`ChB`, logic-level, → Teensy). **`M+`/`M−`
never touch the Teensy.**

| Connector pin | What it is            | Connects to                              |
|---------------|-----------------------|------------------------------------------|
| `M+`          | Motor winding lead    | L298N **output** terminal                |
| `M−`          | Motor winding lead    | L298N **output** terminal (the other)    |
| `Vcc`         | Encoder logic power   | Teensy **3.3 V** ⚠️ (not 5 V — see below) |
| `Gnd`         | Encoder / common gnd  | Teensy **GND** (and the common-gnd star) |
| `ChA`         | Quadrature channel A  | Teensy encoder pin A                     |
| `ChB`         | Quadrature channel B  | Teensy encoder pin B                     |

### Per-motor mapping (matches `cfg::` pins)

| Motor     | `M+`/`M−` → L298N output | `ChA` → Teensy | `ChB` → Teensy |
|-----------|--------------------------|----------------|----------------|
| **LEFT**  | **OUT1 / OUT2** (ENA side: pins 2/3/4) | **14** | **15** |
| **RIGHT** | **OUT3 / OUT4** (ENB side: pins 5/6/7) | **20** | **21** |

Both encoders share `Vcc → 3.3 V` and `Gnd → GND`.

### L298N power side

| L298N terminal      | Connects to                                                        |
|---------------------|--------------------------------------------------------------------|
| `+12V` / `Vs` (motor supply) | Motor battery/PSU **+** (the neck's 5 V here, or a separate motor pack) |
| `GND`               | Common ground star (see below) — **required**                      |
| `+5V` / `Vss` (logic) | Leave on the **on-board regulator** (keep the 5V-EN jumper fitted) **or** feed 5 V; do **not** back-feed it to the Teensy 3.3 V rail |
| `ENA`/`ENB`         | Teensy **2** / **5** (PWM)                                          |
| `IN1`/`IN2`/`IN3`/`IN4` | Teensy **3** / **4** / **6** / **7**                           |
| `OUT1`/`OUT2`       | LEFT motor `M+` / `M−`                                              |
| `OUT3`/`OUT4`       | RIGHT motor `M+` / `M−`                                             |

The L298N logic inputs (`ENx`/`INx`) accept the Teensy's **3.3 V** drive fine —
no level shifting needed on the input side.

### ⚠️ Two hard rules

1. **Encoder `Vcc` = 3.3 V, NOT 5 V.** Teensy 4.1 pins are **not 5 V tolerant**
   (same trap as the OLEDs, Step 0). A 5 V-powered encoder swings `ChA`/`ChB`
   0–5 V and can damage pins 14/15/20/21. Power encoder `Vcc` from the Teensy
   **3.3 V** rail so the channels idle at 3.3 V. (If you only have a 5 V-only
   encoder, add bidirectional level shifters on `ChA`/`ChB` — but most
   hall/optical encoders run at 3.3 V.)
2. **One common ground.** Tie L298N `GND`, the motor supply `GND`, the Teensy
   `GND`, and both encoder `Gnd` pins to a single ground star. Without a shared
   reference the encoder signals are meaningless and the L298N direction logic
   floats.

### Polarity — expected to need one swap per motor (M1)

After wiring, a wheel may drive or count "backwards" — this is normal, the
firmware doesn't know your physical motor orientation yet:

- Wheel **counts** backward when hand-spun forward → swap **`ChA` ↔ `ChB`** on
  that encoder (BRINGUP M1 step 2).
- Wheel **drives** the wrong way under a forward `drive` → swap that motor's
  **`M+` ↔ `M−`** (equivalently its `IN*` leads) (M1 step 3).

Swap **only one** of the two per motor — swapping both cancels out. Get each
wheel to "forward = positive counts = forward motion" independently before
trusting closed-loop drive.

---

## Build / upload (bench, USB)

Requires PlatformIO + Teensyduino on the bench box (NOT this dev machine).

```bash
cd teensy
pio run -e teensy41                 # compile only
pio run -e teensy41 -t upload       # flash over USB
pio device monitor -b 921600        # watch the line-JSON telemetry
```

`platformio.ini` pulls `olikraus/U8g2@^2.35.30` (the OLED driver) alongside
ArduinoJson + Encoder. The firmware reads commands from **both** USB `Serial`
and `Serial1` (Pi); on the bench you drive it over USB.

You send commands by typing one JSON object per line into the serial monitor.
All commands are line-delimited JSON (Plan §3.1). Telemetry streams back at
50 Hz.

---

## M1 — motion bringup (MotorDriver / Encoder / PID / MotionPlanner / CommandParser)

Goal: closed-loop differential drive over USB serial.

1. **Telemetry is alive.** On power-up you should see a 50 Hz stream of
   `{"type":"telemetry",...}` lines (see schema below). `mode` starts `active`
   (it flips to `idle` after `IDLE_TIMEOUT_MS` = 4000 ms with no command, and to
   `fault` on a stall). `enc_l`/`enc_r` should read 0 at rest.

2. **Encoders count the right way.** Spin each wheel **forward by hand**; the
   matching `enc_*` count must **increase**. If a wheel counts backward, swap that
   encoder's A/B (or the motor leads) so forward = positive. Verify `vel_l`/`vel_r`
   track hand-spin direction.

3. **Motor direction + PWM.** Send a slow forward twist:
   ```json
   {"type":"drive","linear":0.1,"angular":0.0,"duration_ms":1500}
   ```
   Both wheels should drive **forward**; the robot tracks roughly straight. A wheel
   spinning the wrong way ⇒ swap that motor's `IN*` leads. PWM is 12-bit @ 20 kHz
   with a **90 % ceiling** — you should never see full-rail duty in `duty_l/duty_r`.

4. **Stop + hold.** `{"type":"stop"}` halts immediately. A `drive` with
   `duration_ms:0` holds until the next command; with `duration_ms>0` it
   auto-stops after that window (MotionPlanner timeout).

5. **PID sanity / tuning.** Defaults live in `cfg::PIDGains` (`kp=1.2, ki=2.5,
   kd=0`). Command a step velocity and watch `vel_*` converge to the implied wheel
   setpoint without sustained oscillation. Push tuned geometry/gains live without
   reflashing:
   ```json
   {"type":"config","wheel_radius_m":0.0325,"track_width_m":0.15,"counts_per_rev":1440,"max_wheel_speed":0.6,"kp":1.2,"ki":2.5,"kd":0.0}
   ```
   You should get a `{"type":"event","name":"config_applied"}` ack.

**M1 acceptance:** forward/turn/stop all behave; encoders sign-correct; closed-loop
velocity tracks the setpoint; no full-rail PWM; `config` re-tunes without reflash.

---

## M2 — animations + reflex idle + watchdog

1. **Animations.** `{"type":"play","name":"nod","loops":1}` plays a named body
   animation (`perk_up`, `nod`, `wiggle`, `spin`, `retreat`). An unknown name
   emits `{"type":"log","level":"warn","msg":"unknown animation"}`. An explicit
   `drive`/`stop` cancels a running animation (command arbitration: drive/stop >
   animation > reflex/idle).

2. **Reflex idle "breathing."** Stop sending commands. After
   `IDLE_TIMEOUT_MS` (4000 ms) with the link alive and no animation, `mode` goes
   `idle` and the ReflexEngine drives a gentle autonomous wobble (the body is
   **never visibly inert**). `{"type":"set_idle","level":0.0..1.0}` scales it.

3. **Watchdog — the unplug → stop test (the key M2 safety gate).**
   - Start a sustained motion: `{"type":"drive","linear":0.15,"angular":0.0,"duration_ms":0}`.
   - **Yank the link** (pull USB on the bench, or the neck UART in the rig).
   - The watchdog must **soft-stop the motors within `LINK_LOSS_MS` = 1500 ms**
     of the last inbound traffic. Confirm wheels cut.
   - Reconnect; the LED heartbeat (pin 13) reflects link state.
   - **Stall fault:** if commanded velocity ≠ measured for `STALL_MS` (500 ms),
     the watchdog latches a fault: `{"type":"event","name":"fault_stall"}`,
     `mode:"fault"`, motors cut. A `stop` (or a `drive`) clears the latched fault;
     a `face` command does **not** (faults stay latched while the eyes still emote).

**M2 acceptance:** animations play + cancel correctly; idle breathing kicks in
after 4 s; **unplug → motors stopped ≤ 1.5 s**; stall latches a fault that only a
`stop`/`drive` clears.

---

## Eyes — power-on + expressivity bench test

Prerequisite: **Step 0 done and metered** (3.3 V VCC on both modules).

1. **Detection.** On boot, `FaceController::begin()` probes each panel with a
   bounded I2C probe (`cfg::face::PROBE_TIMEOUT_US` = 2000 µs) so a missing or
   floating display can never wedge `setup()`. If **neither** eye answers you'll
   see `{"type":"log","level":"warn","msg":"no OLED eyes detected"}` and the body
   still runs fully headless (every face method becomes a safe no-op). Both eyes
   should light to the **neutral** boot face.

2. **Single-bus isolation.** If only one eye lights: that bus's pull-ups/wiring
   are fine and the other isn't. Because LEFT=`Wire`(18/19) and RIGHT=`Wire1`
   (17/16) are **independent hardware buses**, you can debug them one at a time;
   a dead right eye never blocks the left.

3. **Emotions.** Drive expressions over serial (the same `face` command the
   desktop sends):
   ```json
   {"type":"face","emotion":"happy","intensity":1.0}
   {"type":"face","emotion":"angry"}
   {"type":"face","emotion":"surprised"}
   {"type":"face","emotion":"wink"}            // asymmetric: LEFT shut, RIGHT happy
   {"type":"face","emotion":"suspicious"}      // asymmetric: ONE eye narrowed
   ```
   Confirm the 15 core emotions render and that the **asymmetric** ones
   (`wink`, `suspicious`, `curious`) differ left vs. right. Tweening between
   emotions should look smooth (~90 ms expo approach), not a hard cut.

4. **Gaze, blink, hold, "keep current."**
   - `{"type":"face","look_x":0.6,"look_y":-0.3}` points the gaze **without**
     changing the held expression (omitted `emotion` = keep current mood).
   - `{"type":"face","blink":true}` fires a one-shot blink (lid closes/opens over
     ~180 ms) that touches **height only** — width/expression are untouched.
   - `{"type":"face","emotion":"excited","hold_ms":1500}` shows excited, then
     auto-reverts to **neutral** after 1.5 s.

5. **Idle micro-behaviors.** Let the eyes sit idle (mode → `idle`): you should
   see slow "breathing" (sine openness wobble), occasional micro-saccades, and
   spontaneous blinks (`cfg::face::IDLE_BLINK_MIN/MAX_MS` cadence).

6. **Fault face.** Trigger a stall fault (M2 step 3): the eyes overlay a dead
   `x_x` look. Note `telemetry.emotion` still reports the **last commanded**
   emotion string (the `mode:"fault"` field + the eye visual carry the fault, not
   the emotion name).

---

## Real-time acceptance gate (the central constraint) — `≤ 300 µs/tick`

The control loop runs at **1 kHz** (1000 µs/tick). A full 1024-byte SSD1306
flush is ~9.2 ms @ 1 MHz FM+ (≈ 9 control ticks) and is **forbidden**. The face
subsystem instead lives **outside** the 1 kHz control gate (in `loop()` after the
control block, before reflex/telemetry) and is bounded three ways
(`FaceController::update`):

- **(a) ≤ 30 Hz logical refresh** — `FaceState::tick` + rasterize run at most
  every `1000/cfg::face::REFRESH_HZ` ms; rendering into RAM is cheap.
- **(b) dirty-page diff** — only changed 8-px pages are queued; a blink/saccade
  touches a few pages, not the whole frame.
- **(c) byte-budgeted ping-pong flush** — each flushing `loop()` ships **at most
  `cfg::face::FLUSH_BYTES_PER_LOOP` (default 16) bytes to ONE panel**, alternating
  L/R, resuming via `cur_page`/`cur_col`. Never blocks.
- **(d) render/flush split across iterations** — `FaceController::update()` does
  the raster+diff on its own `loop()` iteration and returns; it does **not** also
  flush that same iteration. So the heavy CPU (tick + 2×`draw_eye` + 2×1024-byte
  `memcmp`) and the I2C wire time **never stack in one tick**.

**Timing math (proves the budget).** Two costs, kept on separate iterations by (d):

*Flush iteration (I2C only).* The headline is **data bytes + a FIXED per-call
command overhead**. Each `updateDisplayArea(tx,ty,tw,th)` first issues, per page
row, a control sequence (set-page-address + set-column lo/hi nibble + the
data-mode command), plus the I2C START/addr/STOP framing — roughly **6–10 command
bytes** at the same ~9 µs/byte, i.e. **~55–90 µs fixed overhead per flush** on top
of the data time. The default budget is sized to absorb that and still clear 300 µs:

| scenario                          | data bytes | data @ ~9 µs/byte | + cmd overhead | total | ticks | verdict |
|-----------------------------------|-----------|-------------------|----------------|-------|-------|---------|
| full frame (FORBIDDEN)            | 1024      | 9216 µs           | —              | 9216 µs | ~9.2 | ✗ |
| one page (FORBIDDEN, too coarse)  | 128       | 1152 µs           | —              | 1152 µs | >1   | ✗ |
| old chunk `=24` (too tight)       | 24        | 216 µs            | +55–90 µs      | ~271–306 µs | ~0.3 | ✗ brushes/exceeds 300 µs |
| **default chunk** `=16` (2 tiles) | 16        | **144 µs**        | **+55–90 µs**  | **~199–234 µs** | 0.23 | ✓ margin < 300 µs |
| 400 kHz fallback, chunk `=8` (1 tile) | 8     | 184 µs @ ~23 µs/byte | +~90 µs     | ~274 µs | 0.27 | ✓ < 300 µs |

*Render iteration (CPU only — different `loop()` pass).* `FaceState::tick`
(2×10-field expo/`sqrt`) + 2×`draw_eye` (per-column `sqrtf` over ~46 columns + a
pupil/highlight disc each) + 2×1024-byte `memcmp`. On the 600 MHz Cortex-M7 with
hardware FP this is plausibly tens of µs, and because (d) guarantees it lands on an
iteration with **no** I2C, it does not add to the flush wire time. Confirm the
actual number with the bench gate below — that is the one un-analyzed spike, so
**measure it, don't assume it.**

A worst-case full repaint of both eyes (2048 dirty bytes / 16 B per flush pass)
drains over many cheap `loop()` spins between control ticks — a few ms of wall
time, invisible, **never stalling a single tick** (assumes `loop()` iteration time
stays dominated by the bounded face slice, which holds because the rest of `loop()`
is rate-gated).

**Bench check (M2 acceptance gate):** wrap the face block in `main.cpp`
(`face.set_mode(...); face.update(now);`) with `micros()` and record the max.
**Crucially, drive it so the gate observes the RENDER iteration, not just flush
iterations** — rapid emotion changes + blinks force a non-trivial dirty diff every
~33 ms, so a long enough run with continuous emotion churn will sample the
render-pass max:

```cpp
const uint32_t t0 = micros();
face.set_mode(current_mode(face_idle), now);
face.update(now);
const uint32_t dt = micros() - t0;
// Track TWO running maxima and assert BOTH < 300 us:
//   - flush-pass max   (the iterations that ship I2C)
//   - render-pass max  (the ~30 Hz iterations doing tick + 2x draw_eye + 2x memcmp)
// e.g. classify by whether this iteration shipped any I2C, or simply track the
// single overall max while spamming emotion changes so render passes are sampled.
```

Drive a worst case (rapid emotion changes + blinks, sustained for several seconds)
and confirm **no iteration — render OR flush — exceeds 300 µs**. Record the
render-pass max explicitly; the timing table above only bounds the I2C side. If you
measure the optimistic ~1.1 µs/byte on the flush side, you *may* raise
`FLUSH_BYTES_PER_LOOP` toward ~192 — **only after** that measurement. Both the
chunk size and bus clock are `cfg::face::` constants so the per-loop cost stays
bounded regardless.

**FM+ vs 400 kHz fallback:** the default bus is **1 MHz FM+**
(`cfg::face::BUS_CLOCK_HZ = 1000000`). If a marginal module NAKs or
clock-stretches, drop to **400 kHz** (`BUS_CLOCK_HZ = 400000`) **and** lower
`FLUSH_BYTES_PER_LOOP` to **8** (1 tile) so the per-loop wire time (data + command
overhead) stays under 300 µs.

---

## Command vocabulary (bench cheat-sheet, line-delimited JSON)

```json
{"type":"drive","linear":0.1,"angular":0.0,"duration_ms":1500}
{"type":"stop"}
{"type":"play","name":"nod","loops":1}
{"type":"set_idle","level":0.6}
{"type":"ping"}
{"type":"config","wheel_radius_m":0.0325,"track_width_m":0.15,"counts_per_rev":1440,"max_wheel_speed":0.6,"kp":1.2,"ki":2.5,"kd":0.0}
{"type":"face","emotion":"happy","look_x":0.0,"look_y":0.0,"intensity":1.0,"blink":false,"hold_ms":0}
```

**`face` field rules** (`CommandParser.h`, presence-flagged — "omitted = keep
current"): all fields except `type` are optional. Omitting `emotion` keeps the
held mood; omitting `look_x`/`look_y` keeps the current gaze; `intensity`
defaults 1.0; `blink:true` is a one-shot; `hold_ms>0` reverts to neutral after
that many ms. A `face` command is **not** a motion command — it feeds the link
heartbeat but does **not** reset the idle timer, cancel an animation, or clear a
fault.

15 core emotions: `neutral, happy, sad, angry, surprised, curious, sleepy, love,
suspicious, dizzy, focused, scared, excited, bored, wink`.

## Telemetry state line (Teensy → desktop, 50 Hz)

```json
{"type":"telemetry","enc_l":0,"enc_r":0,"vel_l":0.0,"vel_r":0.0,"duty_l":0.0,"duty_r":0.0,"link_age_ms":12,"mode":"active","emotion":"happy"}
```

`emotion` is the additive trailing field (`Telemetry::emit_state`'s last arg):
the current `FaceController::emotion_name()` — one of the 15 core strings. In
`fault` mode the eyes show `x_x` but `emotion` still reports the last commanded
emotion (the `mode` field is the fault carrier). Other line types: `pong`,
`event` (e.g. `fault_stall`, `config_applied`), `log`.

---

## What's dev-verifiable here vs. bench-only

| Check | Where | Status |
|-------|-------|--------|
| Emotion/tween math (`test_emotion_logic.cpp`) | host `g++` via `run_host_tests.{ps1,sh}` | runnable on any box with a C++17 compiler (no Teensy) |
| Desktop face path (`test_face.py`) + full suite | `.venv` `pytest desktop` | **green here** (84 tests) |
| Lint | `.venv` `ruff check desktop pi` | **clean here** |
| Firmware compile + upload | PlatformIO/Teensyduino | **bench-only** (no toolchain here) |
| M1 motion, M2 watchdog/animations, all OLED steps | the real robot | **bench-only** |

Run the host emotion test (needs only `g++`/`clang++`, no Teensy toolchain):

```powershell
teensy\test\run_host_tests.ps1            # Windows
```
```bash
teensy/test/run_host_tests.sh             # Linux/macOS/WSL
```

> **Do not build the host test with `-ffast-math`.** `set_gaze()` uses `NaN` as a
> "keep this axis" sentinel; `-ffast-math` folds `isnan()` to false and silently
> breaks the "omitted gaze = keep current" path. The run scripts already omit it.
