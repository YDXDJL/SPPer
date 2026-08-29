#include <Arduino.h>

namespace {

constexpr uint8_t kServoPin = 25;
constexpr uint8_t kPwmChannel = 0;
constexpr uint16_t kPwmFrequencyHz = 50;
constexpr uint8_t kPwmResolutionBits = 14;
constexpr uint32_t kPwmPeriodUs = 1000000UL / kPwmFrequencyHz;
constexpr uint16_t kDefaultMinimumPulseUs = 1000;
constexpr uint16_t kDefaultMaximumPulseUs = 2000;

String command;

void writePulseUs(uint16_t pulseUs) {
  const uint32_t maxDuty = (1UL << kPwmResolutionBits) - 1;
  const uint32_t duty =
      (static_cast<uint32_t>(pulseUs) * maxDuty) / kPwmPeriodUs;
  ledcWrite(kPwmChannel, duty);
}

void moveToAngle(int angle, int minimumPulseUs, int maximumPulseUs) {
  if (angle < 0 || angle > 180 || minimumPulseUs < 400 ||
      maximumPulseUs > 2600 || minimumPulseUs >= maximumPulseUs) {
    Serial.println("ERROR expected: angle 0..180 min_us max_us");
    return;
  }

  const uint16_t pulseUs = static_cast<uint16_t>(
      map(angle, 0, 180, minimumPulseUs, maximumPulseUs));
  writePulseUs(pulseUs);
  Serial.printf("ANGLE degrees=%d pulse=%uus range=%d..%dus\n", angle,
                pulseUs, minimumPulseUs, maximumPulseUs);
}

void releaseServo() {
  ledcWrite(kPwmChannel, 0);
  Serial.println("RELEASE PWM disabled");
}

void handleCommand(String input) {
  input.trim();
  input.toLowerCase();
  if (input == "release") {
    releaseServo();
    return;
  }

  int angle = 0;
  int minimumPulseUs = 0;
  int maximumPulseUs = 0;
  if (sscanf(input.c_str(), "angle %d %d %d", &angle, &minimumPulseUs,
             &maximumPulseUs) == 3) {
    moveToAngle(angle, minimumPulseUs, maximumPulseUs);
    return;
  }
  Serial.println("Commands: angle 0..180 min_us max_us | release");
}

}  // namespace

void setup() {
  Serial.begin(115200);
  const uint32_t waitStarted = millis();
  while (!Serial && millis() - waitStarted < 1500) {
    delay(10);
  }

  ledcSetup(kPwmChannel, kPwmFrequencyHz, kPwmResolutionBits);
  ledcAttachPin(kServoPin, kPwmChannel);
  moveToAngle(90, kDefaultMinimumPulseUs, kDefaultMaximumPulseUs);
  Serial.println("ESP32 180-degree servo ready on GPIO25");
  Serial.println("Commands: angle 0..180 min_us max_us | release");
}

void loop() {
  while (Serial.available() > 0) {
    const char value = static_cast<char>(Serial.read());
    if (value == '\r' || value == '\n') {
      if (!command.isEmpty()) {
        handleCommand(command);
        command = "";
      }
    } else if (command.length() < 48) {
      command += value;
    }
  }
  delay(2);
}
