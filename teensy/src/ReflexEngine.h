// Autonomous idle "breathing" so the pet is never visibly inert. Plan §6, §5.7.
//
// Runs only when the link is alive but no command has arrived for
// cfg::IDLE_TIMEOUT_MS. Picks small spontaneous movements at low probability,
// scaled by an intensity in [0, 1] (0 = fully still, set via `set_idle`).
#pragma once

#include <Arduino.h>
#include "Config.h"
#include "MotionPlanner.h"

class ReflexEngine {
 public:
  void set_intensity(float level) { intensity_ = constrain(level, 0.0f, 1.0f); }
  float intensity() const { return intensity_; }

  // Tick at cfg::REFLEX_HZ. `idle` = no command for IDLE_TIMEOUT_MS and link OK.
  void update(bool idle, MotionPlanner& planner, uint32_t now_ms) {
    if (!idle || intensity_ <= 0.0f) { acting_ = false; return; }
    if (acting_) {
      if (now_ms >= action_until_) acting_ = false;
      return;  // let the current micro-movement finish
    }
    if (now_ms < next_eligible_) return;

    // ~ (intensity) chance to act each tick once eligible.
    if ((random(1000) / 1000.0f) < (0.20f * intensity_)) {
      trigger(planner, now_ms);
    }
    // Space out attempts: 0.4-1.4 s, shorter at higher intensity.
    next_eligible_ = now_ms + (uint32_t)(1400 - 1000 * intensity_) + random(400);
  }

 private:
  void trigger(MotionPlanner& planner, uint32_t now_ms) {
    const float mag = 0.06f + 0.10f * intensity_;  // m/s
    const long pick = random(3);
    uint32_t dur = 120 + random(180);
    switch (pick) {
      case 0:  // tiny forward/back jitter ("breath")
        planner.set_wheels(mag, mag, dur, now_ms);
        break;
      case 1:  // gentle in-place turn
        if (random(2)) planner.set_wheels(-mag, mag, dur, now_ms);
        else           planner.set_wheels(mag, -mag, dur, now_ms);
        break;
      default:  // deliberate pause (no motion, just hold the timer)
        planner.set_wheels(0.0f, 0.0f, dur, now_ms);
        dur += 300;
        break;
    }
    acting_ = true;
    action_until_ = now_ms + dur;
  }

  float intensity_ = 0.6f;  // matches desktop config default
  bool acting_ = false;
  uint32_t action_until_ = 0;
  uint32_t next_eligible_ = 0;
};
