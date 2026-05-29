// Teensy 4.1 firmware entry point. Real modules (MotorDriver, EncoderReader,
// PID, MotionPlanner, AnimationPlayer, ReflexEngine, CommandParser, Telemetry,
// Watchdog) land in M1/M2. This placeholder just blinks the onboard LED so the
// project compiles and flashes cleanly during scaffolding.

#include <Arduino.h>

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(921600);
}

void loop() {
  digitalWrite(LED_BUILTIN, HIGH);
  delay(500);
  digitalWrite(LED_BUILTIN, LOW);
  delay(500);
}
