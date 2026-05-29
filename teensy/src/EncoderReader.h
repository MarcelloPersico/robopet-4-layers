// Quadrature encoder wrapper: exposes raw counts and a filtered linear wheel
// velocity in m/s. Plan §6.
#pragma once

#include <Arduino.h>
#include <Encoder.h>
#include "Config.h"

class EncoderReader {
 public:
  EncoderReader(uint8_t pin_a, uint8_t pin_b) : enc_(pin_a, pin_b) {}

  void begin(const cfg::Geometry& geo) {
    geo_ = geo;
    last_count_ = enc_.read();
    velocity_mps_ = 0.0f;
  }

  void set_geometry(const cfg::Geometry& geo) { geo_ = geo; }

  // Call from the control loop; dt in seconds. Updates the filtered velocity.
  void update(float dt) {
    if (dt <= 0.0f) return;
    const int32_t count = enc_.read();
    const int32_t delta = count - last_count_;
    last_count_ = count;

    const float revs = static_cast<float>(delta) / geo_.counts_per_rev;
    const float dist_m = revs * (2.0f * PI * geo_.wheel_radius_m);
    const float inst = dist_m / dt;

    // Single-pole IIR low-pass to tame quantisation noise at 1 kHz.
    constexpr float alpha = 0.15f;
    velocity_mps_ = alpha * inst + (1.0f - alpha) * velocity_mps_;
  }

  int32_t count() const { return last_count_; }
  float velocity_mps() const { return velocity_mps_; }

 private:
  Encoder enc_;
  cfg::Geometry geo_{};
  int32_t last_count_ = 0;
  float velocity_mps_ = 0.0f;
};
