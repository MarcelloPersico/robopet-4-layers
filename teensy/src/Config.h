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
constexpr uint8_t PIN_ENC_R_A = 16;
constexpr uint8_t PIN_ENC_R_B = 17;

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

}  // namespace cfg
