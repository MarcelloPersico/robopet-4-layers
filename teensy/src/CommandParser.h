// Line-delimited JSON command parser (ArduinoJson v7). Plan §3.1, §6.
//
// Command vocabulary (downstream, desktop -> Teensy):
//   {"type":"drive","linear":0.2,"angular":0.0,"duration_ms":500}
//   {"type":"stop"}
//   {"type":"play","name":"nod","loops":1}
//   {"type":"set_idle","level":0.6}
//   {"type":"ping"}
//   {"type":"config","wheel_radius_m":0.0325,"track_width_m":0.15,
//                    "counts_per_rev":1440,"max_wheel_speed":0.6,
//                    "kp":220,"ki":900,"kd":0}
//   {"type":"face","emotion":"happy","look_x":0.0,"look_y":0.0,
//                  "intensity":1.0,"blink":false,"hold_ms":0}
//
// `face` drives the dual-OLED eyes (Plan §6). All fields except "type" are
// optional and presence-flagged so "omitted = keep current": an absent emotion
// keeps the held mood, and look() (emotion omitted) must NOT wipe expression.
//
// One CommandParser instance per input Stream (USB + Serial1). Each accumulates
// a line independently; oversized lines are discarded to resync.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include "Config.h"

enum class CmdType { kNone, kDrive, kStop, kPlay, kSetIdle, kPing, kConfig, kFace, kUnknown };

struct Command {
  CmdType type = CmdType::kNone;
  // drive
  float linear = 0.0f, angular = 0.0f;
  uint32_t duration_ms = 0;
  // play
  char name[24] = {0};
  uint16_t loops = 1;
  // set_idle
  float level = 0.0f;
  // config (only fields present are applied; presence flags below)
  cfg::Geometry geo{};
  cfg::PIDGains gains{};
  bool has_geo = false, has_gains = false;
  // face (presence-flagged: omitted = keep current; see kFace handling in main)
  char     emotion[16] = {0};            // "" => keep current
  float    look_x = 0.0f, look_y = 0.0f;
  float    intensity = 1.0f;
  bool     blink = false;
  uint32_t hold_ms = 0;
  bool     has_emotion = false, has_look = false, has_intensity = false;
};

class CommandParser {
 public:
  // Drain whatever is available on `in`; if a complete line parses, fill `out`
  // and return true. Returns at most one command per call.
  bool poll(Stream& in, Command& out) {
    while (in.available()) {
      const char c = static_cast<char>(in.read());
      if (c == '\n' || c == '\r') {
        if (len_ == 0) continue;        // skip blank lines / CRLF pairs
        buf_[len_] = '\0';
        const bool ok = parse(buf_, out);
        len_ = 0;
        if (ok) return true;            // else: malformed; keep draining
      } else if (len_ < sizeof(buf_) - 1) {
        buf_[len_++] = c;
      } else {
        len_ = 0;  // overflow: drop and resync on next newline
      }
    }
    return false;
  }

 private:
  static bool parse(const char* line, Command& out) {
    JsonDocument doc;
    if (deserializeJson(doc, line)) return false;
    const char* type = doc["type"] | "";
    out = Command{};

    if      (!strcmp(type, "drive"))    { out.type = CmdType::kDrive;
                                          out.linear = doc["linear"] | 0.0f;
                                          out.angular = doc["angular"] | 0.0f;
                                          out.duration_ms = doc["duration_ms"] | 0; }
    else if (!strcmp(type, "stop"))     { out.type = CmdType::kStop; }
    else if (!strcmp(type, "play"))     { out.type = CmdType::kPlay;
                                          strlcpy(out.name, doc["name"] | "", sizeof(out.name));
                                          out.loops = doc["loops"] | 1; }
    else if (!strcmp(type, "set_idle")) { out.type = CmdType::kSetIdle;
                                          out.level = doc["level"] | 0.0f; }
    else if (!strcmp(type, "ping"))     { out.type = CmdType::kPing; }
    else if (!strcmp(type, "config"))   { out.type = CmdType::kConfig;
                                          apply_config(doc, out); }
    else if (!strcmp(type, "face"))     { out.type = CmdType::kFace;
                                          // Presence-flagged so "omitted = keep current" is real.
                                          if (doc["emotion"].is<const char*>()) {
                                            strlcpy(out.emotion, doc["emotion"] | "", sizeof(out.emotion));
                                            out.has_emotion = (out.emotion[0] != '\0');
                                          }
                                          out.has_look = doc["look_x"].is<float>() || doc["look_y"].is<float>();
                                          if (out.has_look) { out.look_x = doc["look_x"] | 0.0f;
                                                              out.look_y = doc["look_y"] | 0.0f; }
                                          out.has_intensity = doc["intensity"].is<float>();
                                          out.intensity = doc["intensity"] | 1.0f;
                                          out.blink   = doc["blink"]   | false;
                                          out.hold_ms = doc["hold_ms"] | 0; }
    else                                { out.type = CmdType::kUnknown; }
    return true;
  }

  static void apply_config(JsonDocument& doc, Command& out) {
    if (doc["wheel_radius_m"].is<float>() || doc["track_width_m"].is<float>() ||
        doc["counts_per_rev"].is<float>() || doc["max_wheel_speed"].is<float>()) {
      out.has_geo = true;
      out.geo.wheel_radius_m  = doc["wheel_radius_m"]  | cfg::Geometry{}.wheel_radius_m;
      out.geo.track_width_m   = doc["track_width_m"]   | cfg::Geometry{}.track_width_m;
      out.geo.counts_per_rev  = doc["counts_per_rev"]  | cfg::Geometry{}.counts_per_rev;
      out.geo.max_wheel_speed = doc["max_wheel_speed"] | cfg::Geometry{}.max_wheel_speed;
    }
    if (doc["kp"].is<float>() || doc["ki"].is<float>() || doc["kd"].is<float>()) {
      out.has_gains = true;
      out.gains.kp = doc["kp"] | cfg::PIDGains{}.kp;
      out.gains.ki = doc["ki"] | cfg::PIDGains{}.ki;
      out.gains.kd = doc["kd"] | cfg::PIDGains{}.kd;
    }
  }

  char buf_[256];
  size_t len_ = 0;
};
