#pragma once

#include <stdint.h>

namespace swimmer {

constexpr uint16_t kServoMinimumPulseUs = 600;
constexpr uint16_t kServoMaximumPulseUs = 2600;
constexpr uint16_t kServoPeriodUs = 20000;
constexpr uint8_t kServoRestAngle = 180;
constexpr uint8_t kServoRescueLowAngle = 0;
constexpr uint8_t kServoRescueHighAngle = 90;
constexpr uint8_t kServoRescueCycles = 5;
constexpr uint32_t kServoBootHoldMs = 1000;
constexpr uint32_t kServoRescueStepMs = 500;

inline uint16_t servoPulseForAngle(uint16_t angle) {
  if (angle > 180) {
    angle = 180;
  }
  const uint32_t pulseRange =
      kServoMaximumPulseUs - kServoMinimumPulseUs;
  return static_cast<uint16_t>(
      kServoMinimumPulseUs + (pulseRange * angle + 90U) / 180U);
}

enum class ServoSequence : uint8_t {
  Idle,
  BootSelfTest,
  Rescue,
};

class ServoMotionController {
 public:
  void beginBootSelfTest(uint32_t now) {
    sequence_ = ServoSequence::BootSelfTest;
    stepStartedAtMs_ = now;
    setAngle(kServoRescueHighAngle);
  }

  void beginRescue(uint32_t now) {
    sequence_ = ServoSequence::Rescue;
    rescueStep_ = 0;
    stepStartedAtMs_ = now;
    setAngle(kServoRescueHighAngle);
  }

  void resetToRest() {
    sequence_ = ServoSequence::Idle;
    rescueStep_ = 0;
    setAngle(kServoRestAngle);
  }

  void update(uint32_t now) {
    if (sequence_ == ServoSequence::BootSelfTest) {
      if (now - stepStartedAtMs_ >= kServoBootHoldMs) {
        sequence_ = ServoSequence::Idle;
        setAngle(kServoRestAngle);
      }
      return;
    }

    if (sequence_ != ServoSequence::Rescue ||
        now - stepStartedAtMs_ < kServoRescueStepMs) {
      return;
    }

    stepStartedAtMs_ = now;
    ++rescueStep_;
    setAngle((rescueStep_ & 1U) == 0U ? kServoRescueHighAngle
                                      : kServoRescueLowAngle);
    if (rescueStep_ >= kServoRescueCycles * 2U) {
      // Five 90 -> 0 cycles are complete, then return to 90 degrees.
      sequence_ = ServoSequence::Idle;
    }
  }

  bool takePendingAngle(uint8_t &angle) {
    if (!anglePending_) {
      return false;
    }
    anglePending_ = false;
    angle = currentAngle_;
    return true;
  }

  ServoSequence sequence() const { return sequence_; }
  uint8_t currentAngle() const { return currentAngle_; }

 private:
  void setAngle(uint8_t angle) {
    currentAngle_ = angle;
    anglePending_ = true;
  }

  ServoSequence sequence_ = ServoSequence::Idle;
  uint8_t currentAngle_ = kServoRestAngle;
  uint8_t rescueStep_ = 0;
  uint32_t stepStartedAtMs_ = 0;
  bool anglePending_ = false;
};

}  // namespace swimmer
