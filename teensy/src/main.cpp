// Teensy 4.1 firmware entry point — robot desk pet body controller. Plan §6.
//
// Multi-rate cooperative loop on a single core:
//   control   1 kHz  — encoder sampling, PID, motor output
//   watchdog  100 Hz — link-loss soft-stop, stall -> fault
//   reflex    10 Hz  — autonomous idle "breathing"
//   telemetry 50 Hz  — state report upstream
//
// Command arbitration (highest first): explicit drive/stop > animation >
// reflex/idle. Any inbound traffic counts as proof of a live link.

#include <Arduino.h>

#include "Config.h"
#include "PID.h"
#include "MotorDriver.h"
#include "EncoderReader.h"
#include "MotionPlanner.h"
#include "AnimationPlayer.h"
#include "ReflexEngine.h"
#include "CommandParser.h"
#include "Telemetry.h"
#include "Watchdog.h"
#include "FaceController.h"

namespace {

cfg::Geometry geo;
cfg::PIDGains gains;

MotorDriver motor_l, motor_r;
EncoderReader enc_l(cfg::PIN_ENC_L_A, cfg::PIN_ENC_L_B);
EncoderReader enc_r(cfg::PIN_ENC_R_A, cfg::PIN_ENC_R_B);
PID pid_l, pid_r;
MotionPlanner planner;
AnimationPlayer anim;
ReflexEngine reflex;
CommandParser parser_usb, parser_pi;
Telemetry telem;
Watchdog wd;
FaceController eyes;

// Timekeeping
elapsedMicros control_dt_us;
uint32_t last_telemetry_ms = 0, last_reflex_ms = 0, last_watchdog_ms = 0;
uint32_t last_command_ms = 0;

void apply_config(const Command& c) {
  if (c.has_geo) {
    geo = c.geo;
    enc_l.set_geometry(geo);
    enc_r.set_geometry(geo);
    planner.set_geometry(geo);
  }
  if (c.has_gains) {
    gains = c.gains;
    pid_l.set_gains(gains);
    pid_r.set_gains(gains);
  }
  telem.emit_event("config_applied");
}

void handle(const Command& c, uint32_t now) {
  switch (c.type) {
    case CmdType::kDrive:
      wd.feed_ping(now);
      if (wd.faulted()) wd.clear_fault(now);
      anim.cancel();
      planner.set_twist(c.linear, c.angular, c.duration_ms, now);
      last_command_ms = now;
      break;
    case CmdType::kStop:
      wd.feed_ping(now);
      wd.clear_fault(now);           // stop is the operator's fault-reset
      anim.cancel();
      planner.stop(now);
      last_command_ms = now;
      break;
    case CmdType::kPlay:
      wd.feed_ping(now);
      if (wd.faulted()) wd.clear_fault(now);
      if (!anim.play(c.name, c.loops, now)) telem.emit_log("warn", "unknown animation");
      last_command_ms = now;
      break;
    case CmdType::kSetIdle:
      wd.feed_ping(now);
      reflex.set_intensity(c.level);
      last_command_ms = now;
      break;
    case CmdType::kPing:
      wd.feed_ping(now);             // heartbeat: liveness only, not activity
      telem.emit_pong(now);
      break;
    case CmdType::kConfig:
      wd.feed_ping(now);
      apply_config(c);
      break;
    case CmdType::kFace:
      // Face is NOT a motion command: it proves the link is live (feed_ping) but
      // must NOT set last_command_ms (body keeps idle-breathing while the eyes
      // emote), must NOT cancel animations, and must NOT clear a latched fault.
      wd.feed_ping(now);
      // intensity OMITTED => keep current (pass the -1.0f sentinel that
      // FaceState::set_emotion treats as "don't retarget intensity"), matching the
      // "omitted = keep current" contract that emotion/look already honor. A bare
      // look() (which sends no intensity key) must NOT snap the held expression
      // back to full strength.
      eyes.set_emotion(c.has_emotion ? c.emotion : nullptr,
                       c.has_intensity ? c.intensity : -1.0f, c.hold_ms, now);
      if (c.has_look) eyes.look(c.look_x, c.look_y);
      if (c.blink)    eyes.blink(now);
      break;
    case CmdType::kUnknown:
      telem.emit_log("warn", "unknown command type");
      break;
    case CmdType::kNone:
      break;
  }
}

Mode current_mode(bool idle) {
  if (wd.faulted()) return Mode::kFault;
  return idle ? Mode::kIdle : Mode::kActive;
}

}  // namespace

void setup() {
  Serial.begin(cfg::LINK_BAUD);        // USB (bench)
  PI_SERIAL.begin(cfg::LINK_BAUD);     // Serial1 (Pi link)
  randomSeed(micros());

  motor_l.begin(cfg::PIN_ENA, cfg::PIN_IN1, cfg::PIN_IN2);
  motor_r.begin(cfg::PIN_ENB, cfg::PIN_IN3, cfg::PIN_IN4);
  enc_l.begin(geo);
  enc_r.begin(geo);
  pid_l.begin(gains, 1.0f);
  pid_r.begin(gains, 1.0f);
  planner.begin(geo);
  telem.begin(&Serial, &PI_SERIAL);

  const uint32_t now = millis();
  wd.begin(now);
  last_command_ms = now;
  control_dt_us = 0;

  // Dual-OLED eyes (Plan §6). begin() is non-hanging (bounded per-panel probe);
  // a headless rig (no panels) degrades to a no-op so the body still runs.
  if (!eyes.begin(now)) telem.emit_log("warn", "no OLED eyes detected");

  pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
  const uint32_t now = millis();

  // --- Inbound commands (both links) ---------------------------------------
  Command cmd;
  if (parser_usb.poll(Serial, cmd)) handle(cmd, now);
  if (parser_pi.poll(PI_SERIAL, cmd)) handle(cmd, now);

  // --- Control @ 1 kHz ------------------------------------------------------
  if (control_dt_us >= (1000000UL / cfg::CONTROL_HZ)) {
    const float dt = control_dt_us * 1e-6f;
    control_dt_us = 0;

    enc_l.update(dt);
    enc_r.update(dt);

    const bool link_ok = wd.link_alive(now);

    anim.update(planner, now);
    planner.update(dt, now);

    const float sp_l = planner.left_setpoint();
    const float sp_r = planner.right_setpoint();

    if (!link_ok || wd.faulted()) {
      // Safe state: cut motors, hold integrators at zero.
      pid_l.reset();
      pid_r.reset();
      motor_l.stop();
      motor_r.stop();
    } else {
      const float ff_l = sp_l / geo.max_wheel_speed;  // velocity feed-forward
      const float ff_r = sp_r / geo.max_wheel_speed;
      const float eff_l = constrain(ff_l + pid_l.update(sp_l, enc_l.velocity_mps(), dt), -1.0f, 1.0f);
      const float eff_r = constrain(ff_r + pid_r.update(sp_r, enc_r.velocity_mps(), dt), -1.0f, 1.0f);
      motor_l.set(eff_l);
      motor_r.set(eff_r);
    }
  }

  // --- Face / eyes ----------------------------------------------------------
  // Runs OUTSIDE the 1 kHz control gate. update() splits its work across separate
  // loop() passes: a ~30 Hz render-in-RAM pass (tick + raster + dirty diff) and
  // flush passes that each ship at most cfg::face::FLUSH_BYTES_PER_LOOP bytes to
  // ONE panel (ping-ponging L/R). The two costs never stack in one iteration, so
  // it can never starve a control tick (<300 us budget).
  {
    const bool face_idle = wd.link_alive(now) && !anim.active() && !wd.faulted() &&
                           (now - last_command_ms > cfg::IDLE_TIMEOUT_MS);
    eyes.set_mode(current_mode(face_idle), now);  // kFault => x_x; kIdle => idle breathe
    eyes.update(now);                             // render-in-RAM OR ONE bounded I2C slice
  }

  // --- Reflex @ 10 Hz -------------------------------------------------------
  if (now - last_reflex_ms >= (1000UL / cfg::REFLEX_HZ)) {
    last_reflex_ms = now;
    const bool link_ok = wd.link_alive(now);
    const bool idle = link_ok && !anim.active() && !wd.faulted() &&
                      (now - last_command_ms > cfg::IDLE_TIMEOUT_MS);
    reflex.update(idle, planner, now);
  }

  // --- Watchdog @ 100 Hz ----------------------------------------------------
  if (now - last_watchdog_ms >= (1000UL / cfg::WATCHDOG_HZ)) {
    last_watchdog_ms = now;
    const bool was_faulted = wd.faulted();
    wd.update(now, planner.left_setpoint(), planner.right_setpoint(),
              enc_l.velocity_mps(), enc_r.velocity_mps());
    if (!was_faulted && wd.faulted()) {
      planner.stop(now);
      telem.emit_event("fault_stall");
    }
  }

  // --- Telemetry @ 50 Hz ----------------------------------------------------
  if (now - last_telemetry_ms >= (1000UL / cfg::TELEMETRY_HZ)) {
    last_telemetry_ms = now;
    const bool link_ok = wd.link_alive(now);
    const bool idle = link_ok && !anim.active() && !wd.faulted() &&
                      (now - last_command_ms > cfg::IDLE_TIMEOUT_MS);
    telem.emit_state(enc_l.count(), enc_r.count(),
                     enc_l.velocity_mps(), enc_r.velocity_mps(),
                     motor_l.duty_frac(), motor_r.duty_frac(),
                     wd.link_age_ms(now), current_mode(idle), eyes.emotion_name());
    digitalWriteFast(LED_BUILTIN, link_ok ? HIGH : ((now / 250) % 2));  // heartbeat LED
  }
}
