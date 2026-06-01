// Host (g++) unit test for the hardware-free emotion/tween math in
// teensy/src/EmotionLibrary.h. Plan §6 (face subsystem).
//
// This machine cannot flash or compile the Teensy firmware, so the renderer and
// FaceController (which need Arduino + U8g2) are NOT exercised here. What IS
// exercised is the pure interpolation/easing/keyframe layer, which by contract
// includes ONLY <stdint.h>/<stddef.h>/<math.h> and therefore builds with a plain
// host compiler. Run it via teensy/test/run_host_tests.{ps1,sh}.
//
// IMPORTANT: build WITHOUT -ffast-math. set_gaze() uses NaN as a "keep this axis"
// sentinel and -ffast-math would fold isnan() to false, breaking that path — so a
// couple of NaN-sentinel assertions below would silently regress.
//
// No GoogleTest dependency: a tiny CHECK/CHECK_NEAR harness keeps the build a
// single self-contained g++ invocation.

#include "arduino_shim.h"        // proves EmotionLibrary coexists with the shim
#include "../src/EmotionLibrary.h"

#include <cmath>
#include <cstdio>
#include <cstdint>

using namespace face;

// --- tiny test harness -------------------------------------------------------
static int g_failures = 0;
static int g_checks = 0;

#define CHECK(cond)                                                            \
  do {                                                                         \
    ++g_checks;                                                                \
    if (!(cond)) {                                                             \
      ++g_failures;                                                            \
      std::printf("  FAIL %s:%d  CHECK(%s)\n", __FILE__, __LINE__, #cond);     \
    }                                                                          \
  } while (0)

#define CHECK_NEAR(a, b, eps)                                                  \
  do {                                                                         \
    ++g_checks;                                                                \
    const double da = (a), db = (b), de = (eps);                              \
    if (!(std::fabs(da - db) <= de)) {                                         \
      ++g_failures;                                                            \
      std::printf("  FAIL %s:%d  CHECK_NEAR(%s=%.6f, %s=%.6f, eps=%.6f)\n",    \
                  __FILE__, __LINE__, #a, da, #b, db, de);                    \
    }                                                                          \
  } while (0)

static bool keyframes_equal(const Keyframe& x, const Keyframe& y, float eps) {
  const float* xl = field_cptr(x.left);
  const float* xr = field_cptr(x.right);
  const float* yl = field_cptr(y.left);
  const float* yr = field_cptr(y.right);
  for (size_t i = 0; i < kEyeFieldCount; ++i) {
    if (std::fabs(xl[i] - yl[i]) > eps) return false;
    if (std::fabs(xr[i] - yr[i]) > eps) return false;
  }
  return true;
}

// === structural invariants ===================================================
// Mirror the in-header static_assert so the flat-array tween contract is checked
// from the test side too (a layout change here is a loud, separate failure).
static_assert(sizeof(EyeParams) == 10 * sizeof(float),
              "EyeParams must be exactly 10 tightly-packed floats");

static void test_struct_layout() {
  // field_ptr must alias the first member and stride by one float per field.
  EyeParams e{};
  float* p = field_ptr(e);
  CHECK(p == &e.cx);
  // write through the flat view, read back through the named members.
  for (size_t i = 0; i < kEyeFieldCount; ++i) p[i] = (float)i;
  CHECK_NEAR(e.cx, 0.0, 1e-9);
  CHECK_NEAR(e.height, 3.0, 1e-9);     // 4th field
  CHECK_NEAR(e.highlight, 9.0, 1e-9);  // 10th field
}

// === emotion identity ========================================================
static void test_every_emotion_has_a_keyframe_and_name() {
  CHECK(kEmotionCount == 15);
  for (uint8_t i = 0; i < kEmotionCount; ++i) {
    const Emotion e = (Emotion)i;
    const char* n = emotion_name(e);
    CHECK(n != nullptr && n[0] != '\0');
    // name round-trips back to the same enum value.
    CHECK(emotion_from_name(n) == e);
    // keyframe lookup is in-range and finite for every emotion.
    const Keyframe& kf = keyframe(e);
    const float* l = field_cptr(kf.left);
    for (size_t f = 0; f < kEyeFieldCount; ++f) CHECK(std::isfinite(l[f]));
  }
}

static void test_name_table_matches_locked_list() {
  // Byte-identical to the 15-emotion list duplicated in tools.py / mcp_server /
  // persona.md / test_face.py. If this fails, the desktop literals must follow.
  const char* expected[15] = {
      "neutral", "happy", "sad", "angry", "surprised", "curious", "sleepy",
      "love", "suspicious", "dizzy", "focused", "scared", "excited", "bored",
      "wink"};
  for (uint8_t i = 0; i < 15; ++i) {
    const char* got = emotion_name((Emotion)i);
    bool eq = true;
    const char* a = got;
    const char* b = expected[i];
    while (*a && *b) { if (*a != *b) { eq = false; break; } ++a; ++b; }
    CHECK(eq && *a == '\0' && *b == '\0');
  }
}

static void test_emotion_from_name_unknown_is_neutral() {
  CHECK(emotion_from_name(nullptr) == Emotion::kNeutral);
  CHECK(emotion_from_name("") == Emotion::kNeutral);
  CHECK(emotion_from_name("not-a-real-emotion") == Emotion::kNeutral);
  CHECK(emotion_from_name("HAPPY") == Emotion::kNeutral);  // case-sensitive
}

static void test_keyframe_out_of_range_is_neutral() {
  CHECK(keyframes_equal(keyframe(Emotion::kCount),
                        keyframe(Emotion::kNeutral), 1e-6f));
  CHECK(keyframes_equal(keyframe((Emotion)200),
                        keyframe(Emotion::kNeutral), 1e-6f));
}

// === scalar helpers ==========================================================
static void test_clampf() {
  CHECK_NEAR(clampf(5.0f, -1.0f, 1.0f), 1.0f, 1e-9);
  CHECK_NEAR(clampf(-5.0f, -1.0f, 1.0f), -1.0f, 1e-9);
  CHECK_NEAR(clampf(0.3f, -1.0f, 1.0f), 0.3f, 1e-6);
}

static void test_lerpf_endpoints() {
  CHECK_NEAR(lerpf(2.0f, 10.0f, 0.0f), 2.0f, 1e-6);
  CHECK_NEAR(lerpf(2.0f, 10.0f, 1.0f), 10.0f, 1e-6);
  CHECK_NEAR(lerpf(2.0f, 10.0f, 0.5f), 6.0f, 1e-6);
}

static void test_ease_pinned_and_monotonic() {
  CHECK_NEAR(ease_in_out_cubic(0.0f), 0.0f, 1e-6);   // pinned at 0
  CHECK_NEAR(ease_in_out_cubic(1.0f), 1.0f, 1e-6);   // pinned at 1
  CHECK_NEAR(ease_in_out_cubic(0.5f), 0.5f, 1e-6);   // symmetric midpoint
  // clamps out-of-range input.
  CHECK_NEAR(ease_in_out_cubic(-0.5f), 0.0f, 1e-6);
  CHECK_NEAR(ease_in_out_cubic(1.5f), 1.0f, 1e-6);
  // monotonic non-decreasing across [0,1].
  float prev = ease_in_out_cubic(0.0f);
  for (int i = 1; i <= 100; ++i) {
    const float t = i / 100.0f;
    const float y = ease_in_out_cubic(t);
    CHECK(y >= prev - 1e-6f);
    CHECK(y >= -1e-6f && y <= 1.0f + 1e-6f);
    prev = y;
  }
}

static void test_expo_approach() {
  // dt<=0 holds; tau<=0 snaps to target.
  CHECK_NEAR(expo_approach(3.0f, 9.0f, 90.0f, 0.0f), 3.0f, 1e-6);
  CHECK_NEAR(expo_approach(3.0f, 9.0f, 0.0f, 16.0f), 9.0f, 1e-6);
  // a positive step moves toward the target but does not overshoot it.
  const float v = expo_approach(0.0f, 10.0f, 90.0f, 16.0f);
  CHECK(v > 0.0f && v < 10.0f);
  // converges to the target after many steps.
  float c = 0.0f;
  for (int i = 0; i < 200; ++i) c = expo_approach(c, 10.0f, 90.0f, 16.0f);
  CHECK_NEAR(c, 10.0f, 1e-3);
}

static void test_blink_envelope() {
  // Fully open before and long after the 180ms envelope.
  CHECK_NEAR(blink_openness_mult(-5.0f), 1.0f, 1e-6);
  CHECK_NEAR(blink_openness_mult(0.0f), 1.0f, 1e-6);
  CHECK_NEAR(blink_openness_mult(180.0f), 1.0f, 1e-6);
  CHECK_NEAR(blink_openness_mult(500.0f), 1.0f, 1e-6);
  // Fully shut during the hold window (80..100ms).
  CHECK_NEAR(blink_openness_mult(90.0f), 0.0f, 1e-6);
  // Mid-close and mid-open are strictly between 0 and 1.
  const float closing = blink_openness_mult(40.0f);
  const float opening = blink_openness_mult(140.0f);
  CHECK(closing > 0.0f && closing < 1.0f);
  CHECK(opening > 0.0f && opening < 1.0f);
  // Output always in [0,1] across the whole timeline.
  for (int ms = -20; ms <= 220; ++ms) {
    const float m = blink_openness_mult((float)ms);
    CHECK(m >= -1e-6f && m <= 1.0f + 1e-6f);
  }
}

// === intensity blend =========================================================
static void test_intensity_zero_is_exactly_neutral() {
  for (uint8_t i = 0; i < kEmotionCount; ++i) {
    const Keyframe blended = scale_to_intensity((Emotion)i, 0.0f);
    CHECK(keyframes_equal(blended, keyframe(Emotion::kNeutral), 1e-5f));
  }
}

static void test_intensity_one_is_exactly_the_emotion() {
  for (uint8_t i = 0; i < kEmotionCount; ++i) {
    const Keyframe blended = scale_to_intensity((Emotion)i, 1.0f);
    CHECK(keyframes_equal(blended, keyframe((Emotion)i), 1e-5f));
  }
}

static void test_intensity_half_is_midpoint() {
  const Keyframe half = scale_to_intensity(Emotion::kAngry, 0.5f);
  const Keyframe& tgt = keyframe(Emotion::kAngry);
  const Keyframe& neu = keyframe(Emotion::kNeutral);
  const float* h = field_cptr(half.left);
  const float* t = field_cptr(tgt.left);
  const float* n = field_cptr(neu.left);
  for (size_t f = 0; f < kEyeFieldCount; ++f) {
    CHECK_NEAR(h[f], 0.5 * (t[f] + n[f]), 1e-4);
  }
}

static void test_intensity_clamped() {
  // Out-of-range intensity is clamped, not extrapolated.
  CHECK(keyframes_equal(scale_to_intensity(Emotion::kHappy, 2.0f),
                        keyframe(Emotion::kHappy), 1e-5f));
  CHECK(keyframes_equal(scale_to_intensity(Emotion::kHappy, -1.0f),
                        keyframe(Emotion::kNeutral), 1e-5f));
}

// === FaceState tween / sentinels =============================================
static void test_facestate_starts_neutral() {
  FaceState fs;
  fs.begin(0);
  CHECK(fs.current_emotion() == Emotion::kNeutral);
  // out_* equals neutral on the first frame (cur_ == target_ at begin()).
  fs.tick(0, 0.0f);
  const EyeParams& l = fs.out_left();
  const EyeParams& nl = keyframe(Emotion::kNeutral).left;
  CHECK_NEAR(l.width, nl.width, 1e-4);
  CHECK_NEAR(l.height, nl.height, 1e-4);
}

static void test_facestate_tween_converges_to_target() {
  FaceState fs;
  fs.begin(0);
  fs.set_emotion(Emotion::kSurprised, 1.0f, 0, 0);
  // Step ~1.5s of 16ms frames; the expo tween must reach the surprised keyframe.
  uint32_t now = 0;
  for (int i = 0; i < 100; ++i) { now += 16; fs.tick(now, 16.0f); }
  const EyeParams& l = fs.out_left();
  const EyeParams& s = keyframe(Emotion::kSurprised).left;
  CHECK_NEAR(l.height, s.height, 0.5);   // openness reached (snap tol 0.25px)
  CHECK_NEAR(l.width, s.width, 0.5);
  CHECK(fs.current_emotion() == Emotion::kSurprised);
}

static void test_facestate_keep_current_emotion_sentinel() {
  FaceState fs;
  fs.begin(0);
  fs.set_emotion(Emotion::kHappy, 1.0f, 0, 0);
  // kCount means "keep current emotion" — must not revert to neutral.
  fs.set_emotion(Emotion::kCount, -1.0f, 0, 0);
  CHECK(fs.current_emotion() == Emotion::kHappy);
}

static void test_facestate_keep_current_intensity_sentinel() {
  FaceState fs;
  fs.begin(0);
  fs.set_emotion(Emotion::kAngry, 0.4f, 0, 0);
  // intensity < 0 keeps the prior intensity; target stays the 0.4-scaled angry.
  fs.set_emotion(Emotion::kAngry, -1.0f, 0, 0);
  uint32_t now = 0;
  for (int i = 0; i < 100; ++i) { now += 16; fs.tick(now, 16.0f); }
  const Keyframe expect = scale_to_intensity(Emotion::kAngry, 0.4f);
  CHECK_NEAR(fs.out_left().slant, expect.left.slant, 0.5);
}

static void test_facestate_hold_reverts_to_neutral() {
  FaceState fs;
  fs.begin(0);
  fs.set_emotion(Emotion::kHappy, 1.0f, /*hold_ms=*/200, /*now=*/0);
  CHECK(fs.current_emotion() == Emotion::kHappy);
  // After the hold window elapses, tick() reverts the emotion to neutral.
  fs.tick(250, 16.0f);
  CHECK(fs.current_emotion() == Emotion::kNeutral);
}

static void test_facestate_gaze_keep_axis_nan_sentinel() {
  // Relies on isnan() working — i.e. NOT built with -ffast-math.
  FaceState fs;
  fs.begin(0);
  fs.set_gaze(1.0f, 1.0f);
  fs.tick(16, 16.0f);
  const float gx_after_set = fs.out_left().gaze_x;
  CHECK(gx_after_set > 0.0f);  // +1 gaze -> +6px (plus tweened base 0)
  // NaN on X keeps X; set Y to 0. X must remain the prior +1 mapping.
  fs.set_gaze(NAN, 0.0f);
  fs.tick(32, 16.0f);
  CHECK(fs.out_left().gaze_x > 0.0f);   // X preserved through the NaN sentinel
  CHECK_NEAR(fs.out_left().gaze_y, 0.0, 1e-4);  // Y cleared to 0
}

static void test_facestate_blink_only_touches_height() {
  FaceState fs;
  fs.begin(0);
  fs.set_emotion(Emotion::kNeutral, 1.0f, 0, 0);
  // settle
  uint32_t now = 0;
  for (int i = 0; i < 30; ++i) { now += 16; fs.tick(now, 16.0f); }
  const float w_before = fs.out_left().width;
  fs.trigger_blink(now);
  // sample mid-blink (~40ms in: lid dropping, height < tweened openness).
  fs.tick(now + 40, 16.0f);
  CHECK(fs.out_left().height < keyframe(Emotion::kNeutral).left.height);
  CHECK_NEAR(fs.out_left().width, w_before, 1e-3);  // width untouched by a blink
}

static void test_rng_is_deterministic_and_bounded() {
  Rng a(0xC0FFEEu), b(0xC0FFEEu);
  for (int i = 0; i < 1000; ++i) {
    CHECK(a.next() == b.next());          // same seed -> same stream
  }
  Rng u(42u);
  for (int i = 0; i < 1000; ++i) {
    const float x = u.unit();
    CHECK(x >= 0.0f && x < 1.0f);
    const float r = u.range(-1.5f, 1.5f);
    CHECK(r >= -1.5f && r <= 1.5f);
  }
}

// === runner ==================================================================
int main() {
  std::printf("emotion-logic host test\n");

  test_struct_layout();
  test_every_emotion_has_a_keyframe_and_name();
  test_name_table_matches_locked_list();
  test_emotion_from_name_unknown_is_neutral();
  test_keyframe_out_of_range_is_neutral();

  test_clampf();
  test_lerpf_endpoints();
  test_ease_pinned_and_monotonic();
  test_expo_approach();
  test_blink_envelope();

  test_intensity_zero_is_exactly_neutral();
  test_intensity_one_is_exactly_the_emotion();
  test_intensity_half_is_midpoint();
  test_intensity_clamped();

  test_facestate_starts_neutral();
  test_facestate_tween_converges_to_target();
  test_facestate_keep_current_emotion_sentinel();
  test_facestate_keep_current_intensity_sentinel();
  test_facestate_hold_reverts_to_neutral();
  test_facestate_gaze_keep_axis_nan_sentinel();
  test_facestate_blink_only_touches_height();
  test_rng_is_deterministic_and_bounded();

  if (g_failures == 0) {
    std::printf("OK: all %d checks passed\n", g_checks);
    return 0;
  }
  std::printf("FAILED: %d/%d checks failed\n", g_failures, g_checks);
  return 1;
}
