// Pin assignments and tunable robot parameters. Plan §6.
//
// Hardware: Teensy 4.1 + L298N H-bridge + 2x brushed DC motors with quadrature
// encoders (differential drive). All Teensy 4.1 digital pins are
// interrupt-capable, so the encoder pins below are free to move.
//
// Teensy 4.1 is a 3.3 V part and its pins are NOT 5 V tolerant. The L298N
// logic inputs accept 3.3 V fine; encoder outputs must be 3.3 V (use 3.3 V
// encoders or level-shift). The Pi UART link is 3.3 V on both ends.

#pragma once

#include <Arduino.h>

namespace cfg {

// ---- L298N motor driver pins ------------------------------------------------
// ENA/ENB are PWM (speed); IN1..IN4 set direction.
constexpr uint8_t PIN_ENA = 2;   // left motor PWM   (must be a PWM pin)
constexpr uint8_t PIN_IN1 = 3;   // left motor dir A
constexpr uint8_t PIN_IN2 = 4;   // left motor dir B
constexpr uint8_t PIN_ENB = 5;   // right motor PWM  (must be a PWM pin)
constexpr uint8_t PIN_IN3 = 6;   // right motor dir A
constexpr uint8_t PIN_IN4 = 7;   // right motor dir B

// ---- Quadrature encoder pins ------------------------------------------------
constexpr uint8_t PIN_ENC_L_A = 14;
constexpr uint8_t PIN_ENC_L_B = 15;
// RIGHT encoder relocated 16/17 -> 20/21 to free Wire1 (pins 16/17) for the right
// OLED eye: U8g2's 2nd HW-I2C bus is hardwired to Wire1. 20/21 are free and (like
// all Teensy 4.1 digital pins) interrupt-capable, so the Encoder lib is unaffected.
constexpr uint8_t PIN_ENC_R_A = 20;
constexpr uint8_t PIN_ENC_R_B = 21;

// ---- Serial links -----------------------------------------------------------
// Serial1 = pins 0 (RX) / 1 (TX): the production link to the Pi.
// Serial   = USB: bench/bringup link (Plan §1 "USB serial remains exposed").
#define PI_SERIAL Serial1
constexpr uint32_t LINK_BAUD = 921600;

// ---- PWM --------------------------------------------------------------------
constexpr uint8_t  PWM_RESOLUTION_BITS = 12;          // analogWrite 0..4095
constexpr uint32_t PWM_FREQUENCY_HZ    = 20000;       // above audible range
constexpr uint16_t PWM_MAX             = (1 << PWM_RESOLUTION_BITS) - 1;
constexpr float    PWM_CEILING_FRAC    = 0.90f;        // Plan §6: 90 % ceiling

// ---- Drivetrain geometry (overridable at runtime via `config` command) ------
struct Geometry {
  float wheel_radius_m   = 0.0325f;  // 65 mm wheels
  float track_width_m    = 0.150f;   // wheel centre-to-centre
  float counts_per_rev   = 1440.0f;  // encoder CPR * gear ratio (output shaft)
  float max_wheel_speed  = 0.6f;     // m/s, clamp for safety
};

// ---- Control loop rates (Hz) ------------------------------------------------
constexpr uint32_t CONTROL_HZ   = 1000;
constexpr uint32_t TELEMETRY_HZ = 50;
constexpr uint32_t REFLEX_HZ    = 10;
constexpr uint32_t WATCHDOG_HZ  = 100;

// ---- Timeouts (ms) ----------------------------------------------------------
constexpr uint32_t LINK_LOSS_MS   = 1500;  // no ping -> soft stop (Plan §6)
constexpr uint32_t IDLE_TIMEOUT_MS = 4000; // no command -> reflex engine
constexpr uint32_t STALL_MS       = 500;   // commanded != measured -> fault

// ---- PID defaults (per-wheel) -----------------------------------------------
// Error is in m/s; output is a normalized motor effort in [-1, 1] that adds to
// a velocity feed-forward (setpoint / max_wheel_speed). Starting estimates —
// real tuning happens on the bench in M1.
struct PIDGains {
  float kp = 1.2f;
  float ki = 2.5f;
  float kd = 0.0f;
};

// ---- Dual-OLED "eyes" face subsystem (Plan §6) ------------------------------
// Two 0.96" SSD1306 128x64 panels = the robot's eyes. LEFT on Wire (LPI2C1,
// pins 18/19) via U8g2 _HW_I2C; RIGHT on Wire1 (LPI2C3, pins 16/17) via U8g2
// _2ND_HW_I2C. Same address on separate buses, no jumper, zero custom I2C code.
// The RIGHT encoder was relocated 16/17 -> 20/21 to free Wire1 for the eye.
//
// 3.3 V ONLY: power each OLED module VCC from the Teensy 3.3 V rail so SDA/SCL
// (and the module pull-ups) idle at 3.3 V. Teensy 4.1 pins are NOT 5 V tolerant;
// never power the OLED logic from the neck's 5 V (motor/Pi power only).
//
// Real-time: the I2C flush lives OUTSIDE the 1 kHz control gate and is byte-
// budgeted (FLUSH_BYTES_PER_LOOP) so no loop() iteration's face work exceeds
// ~300 us. The headline data-byte cost is FLUSH_BYTES_PER_LOOP @ ~9 us/byte, BUT
// each updateDisplayArea() call also pays a FIXED per-call I2C command overhead
// (page/column address set + data-command framing + START/addr/STOP): ~6-10
// command bytes ≈ +55-90 us at 1 MHz. The default 16 bytes (2 tiles) @ ~9 us/byte
// = ~144 us data + ~90 us worst-case command overhead ≈ ~234 us, leaving margin
// under the 300 us hard rule (see §6 / BRINGUP.md timing table for the split).
// Drop to <=8 (1 tile) if the bus is run at 400 kHz instead of FM+, where each
// data byte costs ~23 us.
namespace face {

// LEFT eye on Wire (LPI2C1): SDA=18, SCL=19.  RIGHT eye on Wire1 (LPI2C3):
// SDA=17, SCL=16.  Both panels at 0x3C (7-bit) — separate buses, no conflict.
// (U8g2's bus classes own these pins; the constants drive begin()'s setSDA/setSCL
// belt-and-suspenders and are the source of truth for the wiring docs.)
constexpr uint8_t  ADDR_LEFT       = 0x3C;
constexpr uint8_t  ADDR_RIGHT      = 0x3C;
constexpr uint8_t  PIN_SDA_LEFT    = 18;
constexpr uint8_t  PIN_SCL_LEFT    = 19;
constexpr uint8_t  PIN_SDA_RIGHT   = 17;
constexpr uint8_t  PIN_SCL_RIGHT   = 16;

// Bus clock: 1 MHz FM+ (default) gives ~9 us/byte; drop to 400000 if a marginal
// module NAKs/clock-stretches (and lower FLUSH_BYTES_PER_LOOP to 8 / 1 tile to
// match, since each data byte then costs ~23 us).
constexpr uint32_t BUS_CLOCK_HZ    = 1000000;

// Logical refresh cap: FaceState::tick + rasterize run at most this often so
// the eyes never repaint faster than ~30 Hz regardless of loop() spin rate.
constexpr uint32_t REFRESH_HZ      = 30;

// Hard per-loop I2C DATA byte budget (one panel per loop(), ping-pong L/R). Snaps
// down to whole 8-column U8g2 tiles in flush_one(). Default 16 = 2 tiles: ~144 us
// data + the fixed per-updateDisplayArea command overhead (~55-90 us) keeps the
// flush comfortably under the 300 us/tick budget WITH margin. (The previous 24
// left too little headroom once the command-byte overhead is counted.) At 400 kHz
// FM drop to 8 (1 tile). Raise toward ~192 ONLY after a bench micros() measurement
// shows the optimistic ~1.1 us/byte (see realtime notes / BRINGUP.md).
constexpr size_t   FLUSH_BYTES_PER_LOOP = 16;

// Idle blink cadence (ms): random interval in [MIN, MAX] between idle blinks.
constexpr uint32_t IDLE_BLINK_MIN_MS = 2800;
constexpr uint32_t IDLE_BLINK_MAX_MS = 6000;

// begin()-time per-panel probe ceiling so a missing/floating display can never
// wedge setup() (bounded, non-hanging probe).
constexpr uint32_t PROBE_TIMEOUT_US = 2000;

}  // namespace face

}  // namespace cfg
