// Vector-eye rasterizer: draws one procedural rounded-rect eye into a 1024-byte
// SSD1306 page-major framebuffer. Plan §6 (face subsystem).
//
// The raster math here is plain integer/float C++ with no Arduino / U8g2 / Wire
// dependency — FaceController owns the panel objects and the I2C transport; this
// header only fills a caller-owned RAM buffer. The one hardware-ish include is
// guarded behind #if defined(ARDUINO) purely for parity with the rest of the
// tree; nothing in this file needs it. (This header is NOT host-compiled by the
// emotion unit test — only EmotionLibrary.h is — but it is intentionally kept
// host-clean so it can be exercised on the bench / in a future raster test.)
//
// Framebuffer layout (SSD1306 128x64, "F" full-buffer, vertical-byte / page mode):
//   - 8 pages (y in [0,63] -> page = y >> 3), 128 columns each.
//   - byte index = page*128 + x; within that byte, bit (y & 7) is pixel (x,y).
//   - bit set (1) = pixel lit.
// This matches U8g2's U8G2_*_F_HW_I2C buffer (getBufferPtr()), so FaceController
// can memcpy this raster straight into the U8g2 buffer.
#pragma once

#if defined(ARDUINO)
#include <Arduino.h>  // parity only; the raster below uses no Arduino symbols
#endif

#include <stdint.h>
#include <stddef.h>
#include <math.h>

#include "EmotionLibrary.h"

namespace face {

// ---- framebuffer dimensions -------------------------------------------------
static constexpr int kPanelW = 128;
static constexpr int kPanelH = 64;
static constexpr size_t kFbBytes = 1024;  // 128 * 64 / 8

namespace detail {

// Set one pixel in the page-major buffer. Bounds-checked; out-of-range is dropped.
inline void put_px(uint8_t* fb, int x, int y) {
  if ((unsigned)x >= (unsigned)kPanelW || (unsigned)y >= (unsigned)kPanelH) return;
  fb[(y >> 3) * kPanelW + x] |= (uint8_t)(1u << (y & 7));
}

// Fill the vertical span [y0,y1] in a single column, clamped to the panel.
inline void fill_col_span(uint8_t* fb, int x, int y0, int y1) {
  if ((unsigned)x >= (unsigned)kPanelW) return;
  if (y0 > y1) { const int t = y0; y0 = y1; y1 = t; }
  if (y0 < 0) y0 = 0;
  if (y1 > kPanelH - 1) y1 = kPanelH - 1;
  for (int y = y0; y <= y1; ++y)
    fb[(y >> 3) * kPanelW + x] |= (uint8_t)(1u << (y & 7));
}

// Clear the whole 1024-byte buffer.
inline void clear(uint8_t* fb) {
  for (size_t i = 0; i < kFbBytes; ++i) fb[i] = 0;
}

// Horizontal half-extent of a rounded-rect at vertical distance dy from the
// row's vertical center, given that row's half-height hh and a corner radius r.
// Inside the straight middle band (|dy| <= hh-r) the half-width is the full
// half-width hw; within the rounded cap it follows the circular arc. Returns the
// half-width to fill for this scanline.
inline float rrect_half_width(float dy, float hh, float hw, float r) {
  const float ady = fabsf(dy);
  if (ady > hh) return -1.0f;                 // outside the eye vertically
  r = clampf(r, 0.0f, (hw < hh ? hw : hh));   // radius can't exceed the box
  const float flat = hh - r;                  // start of the rounded cap
  if (ady <= flat) return hw;                 // straight side
  const float into = ady - flat;              // [0, r] into the corner
  const float k = r * r - into * into;
  if (k <= 0.0f) return hw - r;               // tip of the corner
  return (hw - r) + sqrtf(k);                 // arc: pull the side in
}

}  // namespace detail

// Draw ONE rounded-rect vector eye into fb (1024 bytes, cleared first).
//
//  - e.width/e.height are the FULL extents; the eye spans [cx-w/2, cx+w/2] x
//    [cy-h/2, cy+h/2] (height is openness; a near-zero height collapses to a slit).
//  - radius_top / radius_bot round the upper / lower corners independently.
//  - slant is in DEGREES, + = inner-corner HIGH. The top edge is skewed by a
//    horizontal-position-proportional vertical offset. mirror=true flips the
//    slant sign so the RIGHT eye slopes symmetrically to the LEFT eye (both lean
//    inward for angry / outward for sad).
//  - gaze_x/gaze_y shift a bright pupil/iris dot and the specular highlight
//    within the eye; highlight (0..1) scales the highlight dot size.
//
// The eye body is filled bright (lit pixels on a dark panel). The pupil is drawn
// as a small dark disc (cleared pixels) and the highlight as a small lit disc
// inside it, giving the Vector/Cozmo "glossy" read on a 1-bit display.
inline void draw_eye(uint8_t* fb, const EyeParams& e, bool mirror) {
  detail::clear(fb);

  const float hw = e.width  * 0.5f;
  const float hh = e.height * 0.5f;
  if (hw < 0.5f || hh < 0.25f) {
    // Collapsed eye (e.g. wink-shut / blink-shut): draw a thin slit so the panel
    // is never fully blank.
    const int y = (int)(e.cy + 0.5f);
    const int x0 = (int)(e.cx - hw + 0.5f);
    const int x1 = (int)(e.cx + hw + 0.5f);
    for (int x = x0; x <= x1; ++x) detail::put_px(fb, x, y);
    return;
  }

  // Slant -> top-edge vertical skew. Convert degrees to a px slope across the
  // half-width: tan(slant) * (x-offset). Mirror flips the sign for the right eye.
  const float slant_deg = mirror ? -e.slant : e.slant;
  const float slope = tanf(slant_deg * 0.01745329252f);  // px(top) per px(x)

  const float rt = clampf(e.radius_top, 0.0f, (hw < hh ? hw : hh));
  const float rb = clampf(e.radius_bot, 0.0f, (hw < hh ? hw : hh));

  const int x_lo = (int)(e.cx - hw - 1.0f);
  const int x_hi = (int)(e.cx + hw + 1.0f);

  // --- fill the eye body column by column -----------------------------------
  for (int x = x_lo; x <= x_hi; ++x) {
    const float dx = (float)x - e.cx;
    if (fabsf(dx) > hw + 0.5f) continue;

    // Per-column top/bottom half-widths governed by their own corner radius. We
    // sample the boundary by solving for the vertical extent at this x.
    // For a rounded rect, at horizontal distance |dx| the vertical half-extent
    // is hh in the straight band (|dx| <= hw-r) and follows the arc near the cap.
    auto vextent = [&](float r) -> float {
      const float adx = fabsf(dx);
      if (adx > hw) return -1.0f;
      r = clampf(r, 0.0f, (hw < hh ? hw : hh));
      const float flat = hw - r;
      if (adx <= flat) return hh;
      const float into = adx - flat;
      const float k = r * r - into * into;
      if (k <= 0.0f) return hh - r;
      return (hh - r) + sqrtf(k);
    };

    const float top_ext = vextent(rt);   // upward half-extent at this column
    const float bot_ext = vextent(rb);   // downward half-extent at this column
    if (top_ext < 0.0f || bot_ext < 0.0f) continue;

    // Slant shifts the TOP edge up/down proportionally to dx.
    const float top_skew = slope * dx;   // +inner-high handled via sign above
    const int y_top = (int)(e.cy - top_ext + top_skew + 0.5f);
    const int y_bot = (int)(e.cy + bot_ext + 0.5f);
    detail::fill_col_span(fb, x, y_top, y_bot);
  }

  // --- pupil + highlight ----------------------------------------------------
  // Pupil center = eye center + gaze offset, clamped to stay inside the body.
  const float gx = clampf(e.gaze_x, -(hw * 0.6f), (hw * 0.6f));
  const float gy = clampf(e.gaze_y, -(hh * 0.6f), (hh * 0.6f));
  const float pcx = e.cx + gx;
  const float pcy = e.cy + gy;

  // Pupil radius scales with eye size but stays modest; skip if the eye is a slit.
  const float pupil_r = clampf((hw < hh ? hw : hh) * 0.35f, 1.5f, 9.0f);
  if (hh >= 4.0f && hw >= 4.0f) {
    const int pr = (int)(pupil_r + 0.5f);
    for (int yy = -pr; yy <= pr; ++yy) {
      for (int xx = -pr; xx <= pr; ++xx) {
        if (xx * xx + yy * yy > pr * pr) continue;
        const int px = (int)(pcx + 0.5f) + xx;
        const int py = (int)(pcy + 0.5f) + yy;
        // Carve the pupil out (dark) only where the body is lit.
        if ((unsigned)px < (unsigned)kPanelW && (unsigned)py < (unsigned)kPanelH)
          fb[(py >> 3) * kPanelW + px] &= (uint8_t)~(1u << (py & 7));
      }
    }

    // Specular highlight: a small lit disc up-and-left of the pupil center,
    // size scaled by e.highlight. highlight==0 => no glint.
    const float hl = clampf(e.highlight, 0.0f, 1.0f);
    if (hl > 0.05f) {
      const int hr = (int)(clampf(pupil_r * 0.45f * hl, 0.0f, 4.0f) + 0.5f);
      const float hcx = pcx - pupil_r * 0.4f;
      const float hcy = pcy - pupil_r * 0.4f;
      for (int yy = -hr; yy <= hr; ++yy) {
        for (int xx = -hr; xx <= hr; ++xx) {
          if (xx * xx + yy * yy > hr * hr) continue;
          detail::put_px(fb, (int)(hcx + 0.5f) + xx, (int)(hcy + 0.5f) + yy);
        }
      }
    }
  }
}

}  // namespace face
