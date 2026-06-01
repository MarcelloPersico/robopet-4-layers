// Minimal pure-C++ stand-ins for the handful of Arduino conveniences, so any
// header that *might* reach for them still compiles on the host (g++) without a
// real Arduino core or U8g2. Plan §6 (face subsystem) — host test support.
//
// EmotionLibrary.h is deliberately self-contained (it includes only
// <stdint.h>/<stddef.h>/<math.h> and uses no Arduino symbols), so this shim is
// NOT strictly required to build test_emotion_logic.cpp today. It is kept as a
// safety net: if the math layer ever grows a constrain()/min()/max()/PI use, the
// host test keeps compiling unchanged. It must stay tiny and side-effect free.
//
// Do NOT include <Arduino.h> here — the whole point is to avoid it on the host.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <math.h>

#ifndef PI
#define PI 3.1415926535897932384626433832795f
#endif

#ifndef TWO_PI
#define TWO_PI (2.0f * PI)
#endif

// Arduino's macro-style helpers, reimplemented as inline templates so they are
// type-safe and have no surprising double-evaluation. (Arduino ships these as
// macros; the templates are friendlier and equivalent for our use.)
template <typename T>
inline T arduino_min(T a, T b) { return a < b ? a : b; }

template <typename T>
inline T arduino_max(T a, T b) { return a > b ? a : b; }

template <typename T>
inline T constrain(T x, T lo, T hi) {
  return x < lo ? lo : (x > hi ? hi : x);
}

// map() over floats (Arduino's is integer); handy if a renderer-ish helper is
// ever pulled into a host test. Guards a zero input span.
inline float fmap(float x, float in_lo, float in_hi, float out_lo, float out_hi) {
  const float span = in_hi - in_lo;
  if (span == 0.0f) return out_lo;
  return (x - in_lo) * (out_hi - out_lo) / span + out_lo;
}
