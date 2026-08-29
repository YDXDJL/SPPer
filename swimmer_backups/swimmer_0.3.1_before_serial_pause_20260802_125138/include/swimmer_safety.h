#pragma once

#include <stdint.h>

namespace swimmer {

constexpr uint32_t kSuspectedDrowningMs = 10000;
constexpr uint32_t kRescueTriggerMs = 20000;
constexpr uint8_t kRecoveryAckCount = 3;

enum class AlertStage : uint8_t {
  Normal,
  SuspectedDrowning,
  RescueTriggered,
};

class SafetyController {
 public:
  void beginMonitoring(uint32_t now) {
    monitoring_ = true;
    lastSuccessfulUpdateMs_ = now;
    communicationAlarm_ = false;
    recoveryAckCount_ = 0;
  }

  void update(uint32_t now) {
    if (!monitoring_) {
      return;
    }

    const uint32_t loss = communicationLossMs(now);
    if (loss >= kSuspectedDrowningMs && !communicationAlarm_) {
      communicationAlarm_ = true;
      recoveryAckCount_ = 0;
    }
    if (loss >= kRescueTriggerMs) {
      rescueLatched_ = true;
      communicationAlarm_ = true;
    }
  }

  // Call only for an ACK received while a heartbeat is pending. A mismatched
  // or rejected ACK never refreshes the watchdog and breaks a recovery streak.
  void onHeartbeatAck(bool messageIdMatches, bool accepted, uint32_t now) {
    if (!messageIdMatches || !accepted) {
      if (communicationAlarm_) {
        recoveryAckCount_ = 0;
      }
      return;
    }

    lastSuccessfulUpdateMs_ = now;
    if (!monitoring_) {
      monitoring_ = true;
    }
    if (!communicationAlarm_) {
      return;
    }

    if (recoveryAckCount_ < kRecoveryAckCount) {
      ++recoveryAckCount_;
    }
    if (recoveryAckCount_ >= kRecoveryAckCount) {
      communicationAlarm_ = false;
    }
  }

  bool resetRescueLatch(uint32_t now) {
    update(now);
    if (!rescueLatched_ || communicationAlarm_ ||
        communicationLossMs(now) >= kSuspectedDrowningMs ||
        recoveryAckCount_ < kRecoveryAckCount) {
      return false;
    }
    rescueLatched_ = false;
    return true;
  }

  bool monitoring() const { return monitoring_; }
  bool vibrationRequired() const { return communicationAlarm_; }
  bool rescueLatched() const { return rescueLatched_; }
  uint8_t recoveryAckCount() const { return recoveryAckCount_; }

  uint32_t communicationLossMs(uint32_t now) const {
    return monitoring_ ? now - lastSuccessfulUpdateMs_ : 0;
  }

  AlertStage alertStage() const {
    if (rescueLatched_) {
      return AlertStage::RescueTriggered;
    }
    return communicationAlarm_ ? AlertStage::SuspectedDrowning
                               : AlertStage::Normal;
  }

 private:
  bool monitoring_ = false;
  bool communicationAlarm_ = false;
  bool rescueLatched_ = false;
  uint8_t recoveryAckCount_ = 0;
  uint32_t lastSuccessfulUpdateMs_ = 0;
};

inline const char *alertStageName(AlertStage stage) {
  switch (stage) {
    case AlertStage::SuspectedDrowning:
      return "suspected_drowning";
    case AlertStage::RescueTriggered:
      return "rescue_triggered";
    default:
      return "normal";
  }
}

}  // namespace swimmer
