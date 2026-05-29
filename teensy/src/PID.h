// Minimal velocity PID with integral anti-windup and output clamping. Plan §6.
#pragma once

#include <Arduino.h>
#include "Config.h"

class PID {
 public:
  void begin(const cfg::PIDGains& g, float out_limit) {
    g_ = g;
    out_limit_ = out_limit;
    reset();
  }

  void set_gains(const cfg::PIDGains& g) { g_ = g; }

  void reset() {
    integral_ = 0.0f;
    prev_error_ = 0.0f;
  }

  // setpoint and measurement in the same units (m/s); dt in seconds.
  // Returns a control effort clamped to [-out_limit, out_limit].
  float update(float setpoint, float measured, float dt) {
    if (dt <= 0.0f) return 0.0f;
    const float error = setpoint - measured;

    // Provisional integral, then clamp via back-calculation anti-windup.
    integral_ += error * dt;
    const float i_max = (g_.ki > 0.0f) ? (out_limit_ / g_.ki) : 0.0f;
    integral_ = constrain(integral_, -i_max, i_max);

    const float derivative = (error - prev_error_) / dt;
    prev_error_ = error;

    float out = g_.kp * error + g_.ki * integral_ + g_.kd * derivative;
    return constrain(out, -out_limit_, out_limit_);
  }

 private:
  cfg::PIDGains g_{};
  float out_limit_ = 1.0f;
  float integral_ = 0.0f;
  float prev_error_ = 0.0f;
};
