// Upstream JSON emitter (Teensy -> desktop): telemetry @ 50 Hz, pong, events,
// logs. Plan §3.1, §6. Writes line-delimited JSON to every attached stream
// (USB for bench, Serial1 for the Pi link).
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

enum class Mode { kActive, kIdle, kFault };

class Telemetry {
 public:
  void begin(Stream* a, Stream* b) { out_[0] = a; out_[1] = b; }

  void emit_state(int32_t enc_l, int32_t enc_r, float vel_l, float vel_r,
                  float duty_l, float duty_r, uint32_t link_age_ms, Mode mode) {
    JsonDocument doc;
    doc["type"] = "telemetry";
    doc["enc_l"] = enc_l;
    doc["enc_r"] = enc_r;
    doc["vel_l"] = round2(vel_l);
    doc["vel_r"] = round2(vel_r);
    doc["duty_l"] = round2(duty_l);
    doc["duty_r"] = round2(duty_r);
    doc["link_age_ms"] = link_age_ms;
    doc["mode"] = mode_str(mode);
    write(doc);
  }

  void emit_pong(uint32_t now_ms) {
    JsonDocument doc;
    doc["type"] = "pong";
    doc["t"] = now_ms;
    write(doc);
  }

  void emit_event(const char* name) {
    JsonDocument doc;
    doc["type"] = "event";
    doc["name"] = name;
    write(doc);
  }

  void emit_log(const char* level, const char* msg) {
    JsonDocument doc;
    doc["type"] = "log";
    doc["level"] = level;
    doc["msg"] = msg;
    write(doc);
  }

 private:
  static const char* mode_str(Mode m) {
    switch (m) {
      case Mode::kActive: return "active";
      case Mode::kIdle:   return "idle";
      default:            return "fault";
    }
  }
  static float round2(float v) { return roundf(v * 100.0f) / 100.0f; }

  void write(JsonDocument& doc) {
    for (Stream* s : out_) {
      if (!s) continue;
      serializeJson(doc, *s);
      s->write('\n');
    }
  }

  Stream* out_[2] = {nullptr, nullptr};
};
