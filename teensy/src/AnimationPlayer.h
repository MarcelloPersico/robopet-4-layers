// Named animations: sequences of (left_mps, right_mps, duration_ms) steps,
// optionally looped. Drives the MotionPlanner one step at a time. Plan §6, §5.7.
#pragma once

#include <Arduino.h>
#include <string.h>
#include "MotionPlanner.h"

class AnimationPlayer {
 public:
  struct Step { float l; float r; uint32_t ms; };

  // Returns false if the name is unknown (so the caller can report an error).
  bool play(const char* name, uint16_t loops, uint32_t now_ms) {
    const Anim* a = find(name);
    if (!a) return false;
    cur_ = a;
    loops_left_ = (loops == 0) ? 1 : loops;
    step_idx_ = 0;
    step_started_ = now_ms;
    active_ = true;
    new_step_ = true;
    return true;
  }

  void cancel() { active_ = false; cur_ = nullptr; }
  bool active() const { return active_; }

  // Feed the planner. Call every tick; no-op when inactive.
  void update(MotionPlanner& planner, uint32_t now_ms) {
    if (!active_ || !cur_) return;
    const Step& s = cur_->steps[step_idx_];
    if (new_step_) {
      planner.set_wheels(s.l, s.r, s.ms, now_ms);
      new_step_ = false;
    }
    if (now_ms - step_started_ >= s.ms) {
      if (++step_idx_ >= cur_->count) {
        step_idx_ = 0;
        if (--loops_left_ == 0) { active_ = false; cur_ = nullptr; return; }
      }
      step_started_ = now_ms;
      new_step_ = true;
    }
  }

 private:
  struct Anim { const char* name; const Step* steps; uint8_t count; };

  // Starter library (Plan M2: 4-5 animations). Velocities are deliberately gentle.
  static const Anim* table(uint8_t& n) {
    static const Step perk_up[] = {{0.15f, 0.15f, 120}, {0.0f, 0.0f, 80}};
    static const Step nod[]     = {{0.12f, 0.12f, 90}, {-0.12f, -0.12f, 90}, {0.0f, 0.0f, 60}};
    static const Step wiggle[]  = {{0.10f, -0.10f, 110}, {-0.10f, 0.10f, 110}, {0.0f, 0.0f, 60}};
    static const Step spin[]    = {{-0.18f, 0.18f, 600}, {0.0f, 0.0f, 100}};
    static const Step retreat[] = {{-0.20f, -0.20f, 400}, {0.0f, 0.0f, 100}};
    static const Anim anims[] = {
        {"perk_up", perk_up, 2}, {"nod", nod, 3}, {"wiggle", wiggle, 3},
        {"spin", spin, 2},       {"retreat", retreat, 2},
    };
    n = sizeof(anims) / sizeof(anims[0]);
    return anims;
  }

  static const Anim* find(const char* name) {
    uint8_t n = 0;
    const Anim* anims = table(n);
    for (uint8_t i = 0; i < n; ++i)
      if (strcmp(anims[i].name, name) == 0) return &anims[i];
    return nullptr;
  }

  const Anim* cur_ = nullptr;
  uint8_t step_idx_ = 0;
  uint16_t loops_left_ = 0;
  uint32_t step_started_ = 0;
  bool active_ = false;
  bool new_step_ = false;
};
