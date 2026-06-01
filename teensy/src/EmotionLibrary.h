// Procedural-eye emotion math: hardware-free parameter/keyframe/tween layer for
// the dual-OLED expressive face. Plan §6 (face subsystem).
//
// This header is the single source of truth for the 15 core emotions and the
// per-frame state machine (FaceState) that the renderer drives. It is
// deliberately free of any Arduino / U8g2 / display dependency: it includes
// ONLY <stdint.h>, <stddef.h>, <math.h>, so the interpolation/easing math can be
// unit-tested on the host (see teensy/test/test_emotion_logic.cpp) on a machine
// without the embedded toolchain.
//
// IMPORTANT: do NOT compile this header with -ffast-math. The gaze "keep this
// axis" sentinel is NaN, and -ffast-math assumes no NaNs (isnan() would be
// folded to false), breaking set_gaze()'s "omitted = keep current" contract.
//
// Vector-eye model (Anki Vector / Cozmo style): each eye is a rounded rectangle
// described by EXACTLY 10 floats (EyeParams). Emotions are keyframes in this
// space; expressivity comes from per-eye asymmetry (wink/suspicious/curious),
// top-edge slant (angry/sad), openness == height (surprised/sleepy), gaze offset
// + micro-saccades, blinks, and idle "breathing". Tweening between keyframes is a
// per-field exponential approach; a blink is a post-tween multiplier on height so
// it never disturbs the tween target.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <math.h>

namespace face {

// ---------------------------------------------------------------------------
// Scalar helpers
// ---------------------------------------------------------------------------

inline float clampf(float v, float lo, float hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

// Linear interpolation. t is unclamped here; callers pass 0..1.
inline float lerpf(float a, float b, float t) { return a + (b - a) * t; }

// Smooth ease, pinned: ease(0)==0, ease(1)==1, monotonic non-decreasing on
// [0,1]. Standard cubic in/out. t is clamped to [0,1] for safety.
inline float ease_in_out_cubic(float t) {
  t = clampf(t, 0.0f, 1.0f);
  if (t < 0.5f) return 4.0f * t * t * t;
  const float u = -2.0f * t + 2.0f;
  return 1.0f - (u * u * u) * 0.5f;
}

// First-order exponential approach toward target over a time constant tau_ms,
// integrated for dt_ms: current + (target-current)*(1 - exp(-dt/tau)). Frame-rate
// independent. Guards tau<=0 (snap) and dt<=0 (hold).
inline float expo_approach(float current, float target, float tau_ms, float dt_ms) {
  if (tau_ms <= 0.0f) return target;
  if (dt_ms <= 0.0f) return current;
  const float k = 1.0f - expf(-dt_ms / tau_ms);
  return current + (target - current) * k;
}

// One-shot blink openness multiplier in [0,1] as a function of ms since the
// blink began: close over kCloseMs (1->0), hold shut kHoldMs, open over kOpenMs
// (0->1). Past the full envelope it returns 1.0 (eye fully open). The ramps use
// ease_in_out_cubic so the lid motion looks organic. Total = 80+20+80 = 180 ms.
inline float blink_openness_mult(float elapsed_ms) {
  constexpr float kCloseMs = 80.0f;
  constexpr float kHoldMs  = 20.0f;
  constexpr float kOpenMs  = 80.0f;
  constexpr float kTotalMs = kCloseMs + kHoldMs + kOpenMs;  // 180
  if (elapsed_ms <= 0.0f) return 1.0f;
  if (elapsed_ms >= kTotalMs) return 1.0f;
  if (elapsed_ms < kCloseMs) {
    // 1 -> 0 as the lid drops.
    return 1.0f - ease_in_out_cubic(elapsed_ms / kCloseMs);
  }
  if (elapsed_ms < kCloseMs + kHoldMs) {
    return 0.0f;  // fully shut
  }
  // 0 -> 1 as the lid lifts.
  const float u = (elapsed_ms - kCloseMs - kHoldMs) / kOpenMs;
  return ease_in_out_cubic(u);
}

// ---------------------------------------------------------------------------
// Deterministic RNG (xorshift32). Injected so the math layer never reaches for
// Arduino's random(); idle saccade/blink timing is reproducible in tests.
// ---------------------------------------------------------------------------

struct Rng {
  uint32_t s;
  explicit Rng(uint32_t seed = 0x1234567u) : s(seed ? seed : 0x1234567u) {}

  uint32_t next() {
    // xorshift32 (Marsaglia). Never lands on 0 given a non-zero seed.
    s ^= s << 13;
    s ^= s >> 17;
    s ^= s << 5;
    return s;
  }

  // [0,1)
  float unit() {
    // 24 bits of mantissa precision is plenty for saccade jitter.
    return (next() >> 8) * (1.0f / 16777216.0f);
  }

  float range(float lo, float hi) { return lo + (hi - lo) * unit(); }
};

// ---------------------------------------------------------------------------
// EyeParams — the per-eye parameter block. EXACTLY 10 contiguous floats so the
// tween can treat it as a flat array (field_ptr). Order is fixed and mirrored by
// the keyframe table below and by the host unit test's static_assert.
// ---------------------------------------------------------------------------

struct EyeParams {
  float cx;          // eye-center X in panel px (0..127), nominal 64
  float cy;          // eye-center Y in panel px (0..63),  nominal 32
  float width;       // eye full width px
  float height;      // eye full height px (== openness; 0 = shut)
  float radius_top;  // top corner radius px
  float radius_bot;  // bottom corner radius px
  float slant;       // signed degrees on top edge: + = inner-corner-HIGH (angry)
  float gaze_x;      // pupil/iris offset X px (gaze)
  float gaze_y;      // pupil/iris offset Y px (gaze)
  float highlight;   // 0..1 specular highlight strength
};

static constexpr size_t kEyeFieldCount = 10;
static_assert(sizeof(EyeParams) == kEyeFieldCount * sizeof(float),
              "EyeParams must be 10 tightly-packed floats (flat-array tween relies on it)");

inline       float* field_ptr (EyeParams& e)       { return &e.cx; }
inline const float* field_cptr(const EyeParams& e) { return &e.cx; }

// ---------------------------------------------------------------------------
// Emotion identity. Enum order == keyframe table index. kCount is the count
// sentinel AND the "keep current emotion" token for FaceState::set_emotion.
// ---------------------------------------------------------------------------

enum class Emotion : uint8_t {
  kNeutral = 0, kHappy, kSad, kAngry, kSurprised, kCurious, kSleepy, kLove,
  kSuspicious, kDizzy, kFocused, kScared, kExcited, kBored, kWink, kCount
};

static constexpr uint8_t kEmotionCount = (uint8_t)Emotion::kCount;  // 15

struct Keyframe { EyeParams left; EyeParams right; };

// The literal emotion table. Order MUST match the Emotion enum (index = (uint8_t)e).
// Values are first-pass aesthetic estimates for a 128x64 panel, centered at
// (64,32); L and R are identical except for the asymmetric emotions
// (curious / suspicious / wink). M2 bench-tunes these. Fields, in order:
//   cx, cy, width, height, radius_top, radius_bot, slant, gaze_x, gaze_y, highlight
inline const Keyframe* keyframe_table() {
  static const Keyframe kTable[kEmotionCount] = {
    // kNeutral: calm, symmetric, half-open.
    {{64, 32, 44, 30, 10, 10,   0,  0,  0, 0.5f},
     {64, 32, 44, 30, 10, 10,   0,  0,  0, 0.5f}},
    // kHappy: bottom-flat "happy arch", eyes squint up.
    {{64, 32, 46, 22, 12,  2,   0,  0,  0, 0.7f},
     {64, 32, 46, 22, 12,  2,   0,  0,  0, 0.7f}},
    // kSad: inner-corner LOW (slant -), gaze down.
    {{64, 33, 42, 24,  8, 12, -14,  0, -3, 0.3f},
     {64, 33, 42, 24,  8, 12, -14,  0, -3, 0.3f}},
    // kAngry: inner-corner HIGH (slant +), narrowed.
    {{64, 31, 44, 22,  2,  8,  16,  0,  0, 0.2f},
     {64, 31, 44, 22,  2,  8,  16,  0,  0, 0.2f}},
    // kSurprised: max openness, round.
    {{64, 32, 50, 50, 16, 16,   0,  0,  0, 0.9f},
     {64, 32, 50, 50, 16, 16,   0,  0,  0, 0.9f}},
    // kCurious: ASYM — L taller + slight slant (head-tilt read), gaze up-right.
    {{64, 32, 44, 34, 12, 10,   4,  3,  2, 0.7f},
     {64, 32, 44, 28, 10, 10,   0,  3,  2, 0.6f}},
    // kSleepy: heavy lids, low openness, gaze down.
    {{64, 34, 44, 12,  6,  2,   0,  0, -3, 0.3f},
     {64, 34, 44, 12,  6,  2,   0,  0, -3, 0.3f}},
    // kLove: wide, max highlight (renderer may heart-ify).
    {{64, 32, 46, 34, 16, 16,   0,  0,  0, 1.0f},
     {64, 32, 46, 34, 16, 16,   0,  0,  0, 1.0f}},
    // kSuspicious: ASYM — LEFT eye narrowed, both look slightly right.
    {{64, 32, 44, 16,  4,  4,   6,  2,  0, 0.4f},
     {64, 32, 44, 30, 10, 10,   0,  2,  0, 0.5f}},
    // kDizzy: static fallback (idle layer adds a spiral gaze sweep).
    {{64, 32, 42, 30, 10, 10,   0,  0,  0, 0.5f},
     {64, 32, 42, 30, 10, 10,   0,  0,  0, 0.5f}},
    // kFocused: narrowed, leaned-in.
    {{64, 32, 40, 18,  6,  6,   4,  0,  0, 0.6f},
     {64, 32, 40, 18,  6,  6,   4,  0,  0, 0.6f}},
    // kScared: wide like surprised + slight sad slant (idle adds tremble), gaze up.
    {{64, 31, 48, 46, 14, 14,  -6,  0,  2, 0.8f},
     {64, 31, 48, 46, 14, 14,  -6,  0,  2, 0.8f}},
    // kExcited: big + happy bottom-flat, gaze up.
    {{64, 32, 48, 40, 14,  6,   0,  0,  2, 0.9f},
     {64, 32, 48, 40, 14,  6,   0,  0,  2, 0.9f}},
    // kBored: half-lidded, gaze off to the side.
    {{64, 34, 44, 18,  6,  8,  -4, -3, -2, 0.3f},
     {64, 34, 44, 18,  6,  8,  -4, -3, -2, 0.3f}},
    // kWink: ASYM — LEFT shut (h~2), RIGHT happy.
    {{64, 32, 46,  2,  2,  2,   0,  0,  0, 0.0f},
     {64, 32, 46, 22, 12,  2,   0,  0,  0, 0.7f}},
  };
  return kTable;
}

// Table lookup; returns kNeutral on an out-of-range index.
inline const Keyframe& keyframe(Emotion e) {
  const uint8_t i = (uint8_t)e;
  return keyframe_table()[i < kEmotionCount ? i : 0];
}

// Stable short name "neutral".."wink"; "neutral" on a bad index.
inline const char* emotion_name(Emotion e) {
  static const char* const kNames[kEmotionCount] = {
    "neutral", "happy", "sad", "angry", "surprised", "curious", "sleepy",
    "love", "suspicious", "dizzy", "focused", "scared", "excited", "bored",
    "wink"
  };
  const uint8_t i = (uint8_t)e;
  return kNames[i < kEmotionCount ? i : 0];
}

// Inverse of emotion_name(); unknown/null/"" => kNeutral. Plain strcmp without
// <string.h> so the host build needs nothing beyond what we already include.
inline Emotion emotion_from_name(const char* name) {
  if (!name) return Emotion::kNeutral;
  for (uint8_t i = 0; i < kEmotionCount; ++i) {
    const char* a = emotion_name((Emotion)i);
    const char* b = name;
    bool equal = true;
    while (*a && *b) {
      if (*a != *b) { equal = false; break; }
      ++a; ++b;
    }
    if (equal && *a == '\0' && *b == '\0') return (Emotion)i;
  }
  return Emotion::kNeutral;
}

// Blend keyframe(e) toward keyframe(kNeutral) per field, per eye, by (1-intensity).
// intensity is clamped to [0,1]; intensity==1 -> keyframe(e) exactly,
// intensity==0 -> neutral exactly.
inline Keyframe scale_to_intensity(Emotion e, float intensity) {
  intensity = clampf(intensity, 0.0f, 1.0f);
  const Keyframe& tgt = keyframe(e);
  const Keyframe& neu = keyframe(Emotion::kNeutral);
  Keyframe out;
  const float* tl = field_cptr(tgt.left);
  const float* tr = field_cptr(tgt.right);
  const float* nl = field_cptr(neu.left);
  const float* nr = field_cptr(neu.right);
  float* ol = field_ptr(out.left);
  float* orr = field_ptr(out.right);
  for (size_t i = 0; i < kEyeFieldCount; ++i) {
    ol[i]  = lerpf(nl[i], tl[i], intensity);
    orr[i] = lerpf(nr[i], tr[i], intensity);
  }
  return out;
}

// ---------------------------------------------------------------------------
// FaceState — the per-frame state machine the renderer drives. Holds the tween
// target and the smoothed current keyframe, blink phase, idle micro-behavior
// timers, the RNG, and the gaze target. tick() advances everything and bakes the
// blink openness into out_left()/out_right().
// ---------------------------------------------------------------------------

class FaceState {
 public:
  void begin(uint32_t now_ms, uint32_t rng_seed = 0x1234567u) {
    rng_ = Rng(rng_seed);
    emotion_ = Emotion::kNeutral;
    intensity_ = 1.0f;
    target_ = scale_to_intensity(emotion_, intensity_);
    cur_ = target_;
    gaze_x_ = 0.0f;
    gaze_y_ = 0.0f;
    blinking_ = false;
    blink_start_ = 0;
    idle_ = false;
    hold_until_ = 0;
    has_hold_ = false;
    saccade_until_ = 0;
    next_saccade_ = now_ms + 1200;
    next_idle_blink_ = now_ms + 3000;
    out_left_ = cur_.left;
    out_right_ = cur_.right;
    begun_ = true;
  }

  // emotion == kCount keeps the current emotion. intensity < 0 keeps the current
  // intensity. hold_ms > 0 schedules an automatic revert to kNeutral.
  void set_emotion(Emotion e, float intensity, uint32_t hold_ms, uint32_t now_ms) {
    if (e != Emotion::kCount) emotion_ = e;
    if (intensity >= 0.0f) intensity_ = clampf(intensity, 0.0f, 1.0f);
    target_ = scale_to_intensity(emotion_, intensity_);
    if (hold_ms > 0) { hold_until_ = now_ms + hold_ms; has_hold_ = true; }
    else             { has_hold_ = false; }
  }

  // Gaze target in [-1,1] per axis. NaN on an axis keeps that axis (the wire
  // "omitted = keep current" path). Clamped after the keep-check.
  void set_gaze(float gx, float gy) {
    if (!isnan(gx)) gaze_x_ = clampf(gx, -1.0f, 1.0f);
    if (!isnan(gy)) gaze_y_ = clampf(gy, -1.0f, 1.0f);
  }

  void trigger_blink(uint32_t now_ms) {
    blinking_ = true;
    blink_start_ = now_ms;
  }

  void set_idle(bool on, uint32_t now_ms) {
    if (on && !idle_) {
      next_saccade_ = now_ms + (uint32_t)rng_.range(600.0f, 1800.0f);
      next_idle_blink_ = now_ms + (uint32_t)rng_.range(2000.0f, 5000.0f);
    }
    idle_ = on;
    if (!on) { saccade_until_ = 0; saccade_x_ = 0.0f; saccade_y_ = 0.0f; }
  }

  // Advance one frame: expo tween (tau ~90 ms), idle micro-behaviors, hold-revert,
  // snap-to-target when very close, then bake the blink openness multiplier and
  // gaze/saccade offsets into out_*.
  void tick(uint32_t now_ms, float dt_ms) {
    if (!begun_) begin(now_ms);

    // --- hold-revert: a held expression reverts to neutral after hold_until_ ---
    if (has_hold_ && (int32_t)(now_ms - hold_until_) >= 0) {
      has_hold_ = false;
      emotion_ = Emotion::kNeutral;
      intensity_ = 1.0f;
      target_ = scale_to_intensity(emotion_, intensity_);
    }

    // --- idle micro-behaviors: breathe (deterministic sin), saccade + blink ----
    float breathe_h = 0.0f;
    float breathe_cy = 0.0f;
    if (idle_) {
      // Breathing is a pure sine of wall-clock time (no RNG) so it is smooth and
      // continuous: a slow ~0.25 Hz wobble on openness and a tiny vertical drift.
      const float phase = (float)now_ms * (6.2831853f * 0.00025f);  // ~0.25 Hz
      const float s = sinf(phase);
      breathe_h = 1.2f * s;    // +/-1.2 px openness wobble
      breathe_cy = 0.6f * s;   // +/-0.6 px vertical drift

      // Micro-saccades: small gaze darts that decay back.
      if (saccade_until_ != 0 && (int32_t)(now_ms - saccade_until_) >= 0) {
        saccade_until_ = 0;
        saccade_x_ = 0.0f;
        saccade_y_ = 0.0f;
      }
      if (saccade_until_ == 0 && (int32_t)(now_ms - next_saccade_) >= 0) {
        saccade_x_ = rng_.range(-1.5f, 1.5f);
        saccade_y_ = rng_.range(-1.0f, 1.0f);
        saccade_until_ = now_ms + (uint32_t)rng_.range(120.0f, 320.0f);
        next_saccade_ = now_ms + (uint32_t)rng_.range(700.0f, 2200.0f);
      }

      // Spontaneous idle blinks.
      if (!blinking_ && (int32_t)(now_ms - next_idle_blink_) >= 0) {
        trigger_blink(now_ms);
        next_idle_blink_ = now_ms + (uint32_t)rng_.range(2500.0f, 6000.0f);
      }
    } else {
      saccade_x_ = 0.0f;
      saccade_y_ = 0.0f;
    }

    // --- per-field exponential tween of cur_ toward target_ --------------------
    constexpr float kTauMs = 90.0f;
    constexpr float kSnapPx = 0.25f;
    float* cl = field_ptr(cur_.left);
    float* cr = field_ptr(cur_.right);
    const float* tl = field_cptr(target_.left);
    const float* tr = field_cptr(target_.right);
    for (size_t i = 0; i < kEyeFieldCount; ++i) {
      cl[i] = expo_approach(cl[i], tl[i], kTauMs, dt_ms);
      cr[i] = expo_approach(cr[i], tr[i], kTauMs, dt_ms);
      if (fabsf(cl[i] - tl[i]) < kSnapPx) cl[i] = tl[i];
      if (fabsf(cr[i] - tr[i]) < kSnapPx) cr[i] = tr[i];
    }

    // --- compose output: copy tween, add gaze (px), idle breathe, blink mult ----
    out_left_ = cur_.left;
    out_right_ = cur_.right;

    // Gaze normalized [-1,1] -> px (+/-6 X, +/-4 Y); saccades stack on top.
    const float gx_px = clampf(gaze_x_, -1.0f, 1.0f) * 6.0f + saccade_x_;
    const float gy_px = clampf(gaze_y_, -1.0f, 1.0f) * 4.0f + saccade_y_;
    out_left_.gaze_x  += gx_px;
    out_left_.gaze_y  += gy_px;
    out_right_.gaze_x += gx_px;
    out_right_.gaze_y += gy_px;

    if (idle_) {
      out_left_.height  += breathe_h;
      out_right_.height += breathe_h;
      out_left_.cy  += breathe_cy;
      out_right_.cy += breathe_cy;
    }

    // Blink: a post-tween multiplier on height ONLY (never moves the target).
    if (blinking_) {
      const float elapsed = (float)(now_ms - blink_start_);
      const float m = blink_openness_mult(elapsed);
      out_left_.height  *= m;
      out_right_.height *= m;
      if (elapsed >= 180.0f) blinking_ = false;
    }
  }

  const EyeParams& out_left()  const { return out_left_; }
  const EyeParams& out_right() const { return out_right_; }
  Emotion          current_emotion() const { return emotion_; }

 private:
  // Tween endpoints.
  Keyframe target_{};
  Keyframe cur_{};
  EyeParams out_left_{};
  EyeParams out_right_{};

  Emotion emotion_ = Emotion::kNeutral;
  float intensity_ = 1.0f;

  // Gaze target in [-1,1].
  float gaze_x_ = 0.0f;
  float gaze_y_ = 0.0f;

  // Blink phase.
  bool blinking_ = false;
  uint32_t blink_start_ = 0;

  // Idle micro-behaviors.
  bool idle_ = false;
  uint32_t next_saccade_ = 0;
  uint32_t saccade_until_ = 0;
  float saccade_x_ = 0.0f;
  float saccade_y_ = 0.0f;
  uint32_t next_idle_blink_ = 0;

  // Hold-revert.
  bool has_hold_ = false;
  uint32_t hold_until_ = 0;

  Rng rng_{0x1234567u};
  bool begun_ = false;
};

}  // namespace face
