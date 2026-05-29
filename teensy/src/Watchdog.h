// Safety supervisor: link-loss soft-stop + stall detection. Plan §6.
//
//  - Link loss: no `ping` within cfg::LINK_LOSS_MS -> motors must soft-stop.
//  - Stall:     a non-trivial wheel command not reflected in measured velocity
//               for cfg::STALL_MS -> latch a fault until explicitly cleared.
#pragma once

#include <Arduino.h>
#include "Config.h"

class Watchdog {
 public:
  void begin(uint32_t now_ms) { last_ping_ = now_ms; }

  void feed_ping(uint32_t now_ms) { last_ping_ = now_ms; }

  uint32_t link_age_ms(uint32_t now_ms) const { return now_ms - last_ping_; }
  bool link_alive(uint32_t now_ms) const { return link_age_ms(now_ms) < cfg::LINK_LOSS_MS; }

  bool faulted() const { return faulted_; }
  void clear_fault(uint32_t now_ms) { faulted_ = false; stall_since_ = 0; (void)now_ms; }

  // Call at watchdog rate. Compares commanded vs measured velocity per wheel.
  void update(uint32_t now_ms, float cmd_l, float cmd_r, float meas_l, float meas_r) {
    if (faulted_) return;
    const bool stalling = wheel_stalled(cmd_l, meas_l) || wheel_stalled(cmd_r, meas_r);
    if (stalling) {
      if (stall_since_ == 0) stall_since_ = now_ms;
      else if (now_ms - stall_since_ >= cfg::STALL_MS) faulted_ = true;
    } else {
      stall_since_ = 0;
    }
  }

 private:
  static bool wheel_stalled(float cmd, float meas) {
    constexpr float kCmdFloor = 0.05f;   // ignore near-zero commands
    constexpr float kMoveFloor = 0.02f;  // measured "is actually moving"
    return fabsf(cmd) > kCmdFloor && fabsf(meas) < kMoveFloor;
  }

  uint32_t last_ping_ = 0;
  uint32_t stall_since_ = 0;
  bool faulted_ = false;
};
