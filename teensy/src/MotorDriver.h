// L298N H-bridge driver for one brushed DC motor. Plan §6.
//
// effort is a signed fraction in [-1, 1]; sign sets direction, magnitude sets
// PWM duty. A global ceiling (cfg::PWM_CEILING_FRAC) caps the duty for thermal
// / over-current headroom.
#pragma once

#include <Arduino.h>
#include "Config.h"

class MotorDriver {
 public:
  void begin(uint8_t pin_en, uint8_t pin_in_a, uint8_t pin_in_b) {
    pin_en_ = pin_en;
    pin_in_a_ = pin_in_a;
    pin_in_b_ = pin_in_b;
    pinMode(pin_in_a_, OUTPUT);
    pinMode(pin_in_b_, OUTPUT);
    pinMode(pin_en_, OUTPUT);
    analogWriteResolution(cfg::PWM_RESOLUTION_BITS);
    analogWriteFrequency(pin_en_, cfg::PWM_FREQUENCY_HZ);
    stop();
  }

  void set(float effort) {
    effort = constrain(effort, -1.0f, 1.0f);
    const float ceiling = cfg::PWM_CEILING_FRAC;
    const uint16_t duty =
        static_cast<uint16_t>(fabsf(effort) * ceiling * cfg::PWM_MAX);

    if (effort > 0.0f) {
      digitalWriteFast(pin_in_a_, HIGH);
      digitalWriteFast(pin_in_b_, LOW);
    } else if (effort < 0.0f) {
      digitalWriteFast(pin_in_a_, LOW);
      digitalWriteFast(pin_in_b_, HIGH);
    } else {
      // Active brake (both low = coast on L298N; both inputs equal = brake).
      digitalWriteFast(pin_in_a_, LOW);
      digitalWriteFast(pin_in_b_, LOW);
    }
    analogWrite(pin_en_, duty);
    last_duty_ = duty;
  }

  void stop() { set(0.0f); }

  // PWM duty as a 0..1 fraction — used as the motor-current proxy in telemetry.
  float duty_frac() const {
    return static_cast<float>(last_duty_) / static_cast<float>(cfg::PWM_MAX);
  }

 private:
  uint8_t pin_en_ = 0, pin_in_a_ = 0, pin_in_b_ = 0;
  uint16_t last_duty_ = 0;
};
