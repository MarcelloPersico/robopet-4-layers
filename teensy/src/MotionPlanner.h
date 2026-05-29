// Turns high-level motion intents into per-wheel velocity setpoints with slew
// limiting, and expires them after a commanded duration. Plan §6.
//
// Accepts either a differential-drive twist (linear m/s, angular rad/s) or
// direct per-wheel velocities (used by the animation/reflex layers). A command
// with duration_ms == 0 holds until superseded; otherwise it zeroes out when
// the duration elapses, so a dropped follow-up command can never run away.
#pragma once

#include <Arduino.h>
#include "Config.h"

class MotionPlanner {
 public:
  void begin(const cfg::Geometry& geo) { geo_ = geo; }
  void set_geometry(const cfg::Geometry& geo) { geo_ = geo; }

  void set_twist(float linear, float angular, uint32_t duration_ms, uint32_t now_ms) {
    const float half_track = 0.5f * geo_.track_width_m;
    set_wheels(linear - angular * half_track,
               linear + angular * half_track, duration_ms, now_ms);
  }

  void set_wheels(float left_mps, float right_mps, uint32_t duration_ms, uint32_t now_ms) {
    tgt_left_ = constrain(left_mps, -geo_.max_wheel_speed, geo_.max_wheel_speed);
    tgt_right_ = constrain(right_mps, -geo_.max_wheel_speed, geo_.max_wheel_speed);
    expires_at_ = (duration_ms == 0) ? 0 : now_ms + duration_ms;
  }

  void stop(uint32_t now_ms) { set_wheels(0.0f, 0.0f, 0, now_ms); }

  // Advance the slew-limited setpoints toward target; call each control tick.
  void update(float dt, uint32_t now_ms) {
    if (expires_at_ != 0 && now_ms >= expires_at_) {
      tgt_left_ = 0.0f;
      tgt_right_ = 0.0f;
      expires_at_ = 0;
    }
    const float max_step = SLEW_MPS_PER_S * dt;
    cmd_left_ = slew(cmd_left_, tgt_left_, max_step);
    cmd_right_ = slew(cmd_right_, tgt_right_, max_step);
  }

  float left_setpoint() const { return cmd_left_; }
  float right_setpoint() const { return cmd_right_; }
  bool moving() const { return fabsf(tgt_left_) > 1e-3f || fabsf(tgt_right_) > 1e-3f; }

 private:
  static constexpr float SLEW_MPS_PER_S = 2.0f;  // accel/decel limit

  static float slew(float cur, float tgt, float max_step) {
    const float d = tgt - cur;
    if (d > max_step) return cur + max_step;
    if (d < -max_step) return cur - max_step;
    return tgt;
  }

  cfg::Geometry geo_{};
  float tgt_left_ = 0.0f, tgt_right_ = 0.0f;
  float cmd_left_ = 0.0f, cmd_right_ = 0.0f;
  uint32_t expires_at_ = 0;
};
