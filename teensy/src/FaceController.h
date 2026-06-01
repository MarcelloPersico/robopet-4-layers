// Dual-OLED expressive face: owns two SSD1306 128x64 panels and drives them from
// the procedural-eye state machine without ever stalling the 1 kHz control loop.
// Plan §6 (face subsystem).
//
// Topology (see Config.h cfg::face and the pin map):
//   LEFT  eye on Wire  (SDA=18, SCL=19, LPI2C1) @ 0x3C  -> U8g2 _HW_I2C
//   RIGHT eye on Wire1 (SDA=17, SCL=16, LPI2C3) @ 0x3C  -> U8g2 _2ND_HW_I2C
// Two hardware I2C buses => identical 0x3C addresses with no address jumper, driven
// by U8g2's stock HW-I2C (Wire) + 2nd-HW-I2C (Wire1) classes — zero custom I2C code.
// The RIGHT encoder was relocated 16/17 -> 20/21 to free Wire1 for the right eye.
//
// REAL-TIME CONTRACT (the central constraint): a full 1024-byte SSD1306 flush is
// ~9.2 ms @ 1 MHz FM+ / ~23 ms @ 400 kHz — i.e. ~9-23 control ticks. That is
// forbidden. Instead update() splits its work across DISTINCT loop() iterations so
// a single iteration is never charged for both:
//   - render iteration (at most every 1000/REFRESH_HZ ms): FaceState::tick + RAM
//     raster + 2x dirty-page diff. Cheap on the M7 FPU; ships NO I2C this pass.
//   - flush iterations (every other call): ship AT MOST cfg::face::
//     FLUSH_BYTES_PER_LOOP bytes of I2C to ONE panel, ping-ponging LEFT/RIGHT.
// At the contract default (16 B @ ~9 us/byte ≈ 144 us + the updateDisplayArea
// per-call command-byte overhead ≈ 55-90 us ≈ 234 us worst case) a flush clears
// the 300 us/tick hard rule with margin, and because the
// render pass never coincides with a flush pass, the raster+diff CPU cost can't
// stack on top of the wire time. A full repaint drains over many (cheap) loop()
// iterations between control ticks, so it is invisible and never stalls a single
// tick. update() NEVER blocks.
//
// All Wire / U8g2 / Arduino includes are confined to this header and guarded
// behind #if defined(ARDUINO); on a host build FaceController degrades to a
// pure no-op so the surrounding firmware logic still compiles for review.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#include "EmotionLibrary.h"
#include "EyeRenderer.h"
#include "Config.h"
#include "Telemetry.h"   // for enum Mode

#if defined(ARDUINO)
#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>
#endif

class FaceController {
 public:
  // Non-hanging init: bring up each bus, probe each panel (bounded by
  // cfg::face::PROBE_TIMEOUT_US), and init whichever are present. Returns true if
  // at least one eye came up; false => fully headless (every method is a safe
  // no-op). Renders the boot emotion immediately so the eyes aren't blank.
  bool begin(uint32_t now_ms) {
    fs_.begin(now_ms);
    mode_ = Mode::kActive;
    last_face_tick_ = now_ms;
    flush_turn_ = 0;
    for (Panel& p : panel_) {
      p.present = false;
      p.cur_page = 0;
      p.cur_col = 0;
      p.dirty_pages = 0;
      memset(p.fb, 0, sizeof(p.fb));
      memset(p.prev, 0xFF, sizeof(p.prev));  // force a full first paint
    }

#if defined(ARDUINO)
    // LEFT on Wire (pins 18/19), RIGHT on Wire1 (pins 17/16). Explicit setSDA/
    // setSCL guard against a future core default change (17/16 are Wire1 default).
    Wire.setSDA(cfg::face::PIN_SDA_LEFT);
    Wire.setSCL(cfg::face::PIN_SCL_LEFT);
    Wire1.setSDA(cfg::face::PIN_SDA_RIGHT);
    Wire1.setSCL(cfg::face::PIN_SCL_RIGHT);

    panel_[0].present = probe(Wire,  cfg::face::ADDR_LEFT);
    panel_[1].present = probe(Wire1, cfg::face::ADDR_RIGHT);

    if (panel_[0].present) {
      left_.setI2CAddress(cfg::face::ADDR_LEFT << 1);   // U8g2 wants the 8-bit addr
      left_.setBusClock(cfg::face::BUS_CLOCK_HZ);
      left_.initDisplay();
      left_.clearDisplay();
      left_.setPowerSave(0);   // display ON: the init seq leaves the panel off (0x0ae)
    }
    if (panel_[1].present) {
      right_.setI2CAddress(cfg::face::ADDR_RIGHT << 1);
      right_.setBusClock(cfg::face::BUS_CLOCK_HZ);
      right_.initDisplay();
      right_.clearDisplay();
      right_.setPowerSave(0);  // display ON: the init seq leaves the panel off (0x0ae)
    }
#endif

    // Render the initial frame into the shadow buffers + mark all pages dirty so
    // the first few update() calls paint the boot face.
    render_frame(now_ms, /*force=*/true);
    return present_left() || present_right();
  }

  // name nullptr/"" => keep current emotion; intensity < 0 => keep current
  // intensity; hold_ms > 0 => revert to neutral after hold_ms.
  void set_emotion(const char* name, float intensity, uint32_t hold_ms, uint32_t now_ms) {
    const face::Emotion e =
        (name && name[0]) ? face::emotion_from_name(name) : face::Emotion::kCount;
    fs_.set_emotion(e, intensity, hold_ms, now_ms);
  }

  // Gaze target in [-1,1] per axis. (look() never clears the held emotion.)
  void look(float x, float y) { fs_.set_gaze(x, y); }

  void blink(uint32_t now_ms) { fs_.trigger_blink(now_ms); }

  // Idle breathe / blink / saccade behaviors on/off.
  void set_idle(bool idle, uint32_t now_ms) { fs_.set_idle(idle, now_ms); }

  // kFault => x_x face overlay; kIdle => idle micro-behaviors over the last
  // emotion; kActive => exactly as commanded. The emotion string reported by
  // emotion_name() stays the last commanded emotion in every mode (mode already
  // carries fault; the eye visual is the fault indicator).
  void set_mode(Mode mode, uint32_t now_ms) {
    if (mode == mode_) return;
    mode_ = mode;
    fs_.set_idle(mode == Mode::kIdle, now_ms);
  }

  // Per-loop entry point. Splits the two costs onto SEPARATE loop() iterations so
  // a single iteration is never charged for BOTH the heavy raster/diff AND an I2C
  // flush (the worst-case spike the <=300 us/tick budget must bound):
  //   (1) On the iteration where the >= REFRESH_HZ cadence fires: FaceState::tick
  //       + 2x draw_eye + 2x 1024-byte dirty-page diff. NO I2C this iteration.
  //   (2) Every OTHER iteration: ship <= FLUSH_BYTES_PER_LOOP to ONE panel,
  //       alternating LEFT/RIGHT.
  // The render fires at most once per ~33 ms; loop() spins thousands of times in
  // that window, so deferring the flush by one iteration costs nothing visible and
  // guarantees render-cost and flush-cost never stack in the same tick. NEVER blocks.
  void update(uint32_t now_ms) {
    // --- (1) bounded render cadence — raster + diff ONLY, no I2C this pass ---
    const uint32_t kFramePeriodMs = (cfg::face::REFRESH_HZ > 0)
                                        ? (1000u / cfg::face::REFRESH_HZ)
                                        : 33u;
    if (now_ms - last_face_tick_ >= kFramePeriodMs) {
      render_frame(now_ms, /*force=*/false);
      last_face_tick_ = now_ms;
      return;  // do NOT also flush this iteration — keep the costs disjoint.
    }

    // --- (2) one bounded I2C slice to ONE panel, ping-ponging ---
    // Try the scheduled panel first; if it has nothing dirty, give the other a
    // turn this loop so a one-sided update still drains promptly.
    if (!flush_one(flush_turn_)) {
      flush_one(flush_turn_ ^ 1);
    }
    flush_turn_ ^= 1;
  }

  // Stable short string for telemetry — the last commanded emotion.
  const char* emotion_name() const {
    return face::emotion_name(fs_.current_emotion());
  }

  bool present_left()  const { return panel_[0].present; }
  bool present_right() const { return panel_[1].present; }

 private:
  static constexpr int kPages = 8;            // 64 rows / 8
  static constexpr int kCols  = face::kPanelW;  // 128

  struct Panel {
    uint8_t fb[face::kFbBytes];    // freshly rasterized frame (page-major)
    uint8_t prev[face::kFbBytes];  // last frame actually shipped to the glass
    uint8_t dirty_pages;           // bitmask of pages whose fb != prev (not yet shipped)
    uint8_t cur_page;              // resume cursor: page currently being flushed
    int     cur_col;               // resume cursor: next column within cur_page
    bool    present;
  };

  // Rasterize the current FaceState into both shadow framebuffers and update each
  // panel's dirty-page mask. `force` marks every page dirty (boot / mode change).
  void render_frame(uint32_t now_ms, bool force) {
    const float dt_ms = (float)(now_ms - last_face_tick_);
    fs_.tick(now_ms, dt_ms > 0.0f ? dt_ms : 1.0f);

    face::EyeParams le = fs_.out_left();
    face::EyeParams re = fs_.out_right();
    if (mode_ == Mode::kFault) { apply_fault_face(le); apply_fault_face(re); }

    face::draw_eye(panel_[0].fb, le, /*mirror=*/false);
    face::draw_eye(panel_[1].fb, re, /*mirror=*/true);

    for (Panel& p : panel_) {
      if (force) { p.dirty_pages = 0xFF; continue; }
      for (int pg = 0; pg < kPages; ++pg) {
        const uint8_t* a = p.fb   + pg * kCols;
        const uint8_t* b = p.prev + pg * kCols;
        if (memcmp(a, b, kCols) != 0) p.dirty_pages |= (uint8_t)(1u << pg);
      }
    }
  }

  // Overlay an "x_x" dead-eye look on a copy of the params: collapse to a thin
  // crossed slit by shrinking height and killing the highlight. (The renderer's
  // slit path draws the bar; this keeps the fault unmistakable without a separate
  // glyph engine.)
  static void apply_fault_face(face::EyeParams& e) {
    e.height = 2.0f;
    e.highlight = 0.0f;
    e.gaze_x = 0.0f;
    e.gaze_y = 0.0f;
  }

  // Ship up to FLUSH_BYTES_PER_LOOP bytes of the given panel's dirty pages to the
  // glass, resuming from (cur_page, cur_col). Returns true if it shipped anything.
  // Bounded, non-blocking: at most one byte-budgeted page window per call.
  bool flush_one(int idx) {
    Panel& p = panel_[idx];
    if (!p.present || p.dirty_pages == 0) return false;

    // Advance cur_page to the next dirty page if the current one is clean/done.
    if (((p.dirty_pages >> p.cur_page) & 1u) == 0) {
      int pg = -1;
      for (int i = 0; i < kPages; ++i) {
        const int cand = (p.cur_page + i) % kPages;
        if ((p.dirty_pages >> cand) & 1u) { pg = cand; break; }
      }
      if (pg < 0) return false;
      p.cur_page = (uint8_t)pg;
      p.cur_col = 0;
    }

    // Snap the byte budget to whole 8-column U8g2 tiles so updateDisplayArea()'s
    // tile-granular window (tx,ty,tw,th in 8-px tiles) maps EXACTLY to the bytes
    // we ship — no partial-tile drift. Budget 24 -> 3 tiles -> 24 columns.
    const int budget_tiles = (int)cfg::face::FLUSH_BYTES_PER_LOOP / 8;  // >=1 by config
    const int tiles = budget_tiles > 0 ? budget_tiles : 1;
    const int col0 = (p.cur_col / 8) * 8;            // tile-aligned start
    int col1 = col0 + tiles * 8;                     // exclusive end this slice
    if (col1 > kCols) col1 = kCols;
    const int count = col1 - col0;
    if (count <= 0) { p.cur_col = 0; return false; }

    const uint8_t* src = p.fb + (int)p.cur_page * kCols + col0;

#if defined(ARDUINO)
    // U8g2 _F_ buffer = our page-major layout. Copy this page's slice into the
    // U8g2 buffer and ship exactly this page+column tile window. (We keep U8g2's
    // whole buffer in sync page by page; only the windowed bytes are transmitted.)
    if (idx == 0) {
      uint8_t* dst = left_.getBufferPtr() + (int)p.cur_page * kCols + col0;
      memcpy(dst, src, (size_t)count);
      left_.updateDisplayArea(col0 / 8, p.cur_page, count / 8, 1);
    } else {
      uint8_t* dst = right_.getBufferPtr() + (int)p.cur_page * kCols + col0;
      memcpy(dst, src, (size_t)count);
      right_.updateDisplayArea(col0 / 8, p.cur_page, count / 8, 1);
    }
#endif

    // Mirror the shipped bytes into prev so future diffs see them as on-glass.
    memcpy(p.prev + (int)p.cur_page * kCols + col0, src, (size_t)count);

    p.cur_col = col1;
    if (p.cur_col >= kCols) {
      // Page fully shipped: clear its dirty bit and move on next time.
      p.dirty_pages &= (uint8_t)~(1u << p.cur_page);
      p.cur_col = 0;
      p.cur_page = (uint8_t)((p.cur_page + 1) % kPages);
    }
    return true;
  }

#if defined(ARDUINO)
  // Bounded I2C presence probe: a zero-length write to the address. Wire has no
  // hard per-transaction timeout knob across cores, so we additionally bound the
  // wall time with micros() and bail if the bus is wedged/floating.
  static bool probe(TwoWire& bus, uint8_t addr) {
    const uint32_t t0 = micros();
    bus.begin();
    bus.beginTransmission(addr);
    const uint8_t err = bus.endTransmission();  // 0 == ACK
    if ((uint32_t)(micros() - t0) > cfg::face::PROBE_TIMEOUT_US) return false;
    return err == 0;
  }
#endif

  face::FaceState fs_;
  Panel panel_[2];          // [0]=LEFT (Wire), [1]=RIGHT (Wire1)
  int flush_turn_ = 0;      // ping-pong index 0/1
  Mode mode_ = Mode::kActive;
  uint32_t last_face_tick_ = 0;

#if defined(ARDUINO)
  // Full-buffer panels via U8g2's stock bus classes (no TwoWire* ctor arg exists):
  // LEFT  -> _HW_I2C      = Wire  (pins 18/19).
  // RIGHT -> _2ND_HW_I2C  = Wire1 (pins 17/16); the R encoder moved to 20/21.
  U8G2_SSD1306_128X64_NONAME_F_HW_I2C     left_ {U8G2_R0, U8X8_PIN_NONE};
  U8G2_SSD1306_128X64_NONAME_F_2ND_HW_I2C right_{U8G2_R0, U8X8_PIN_NONE};
#endif
};
