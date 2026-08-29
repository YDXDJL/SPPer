#pragma once

#include <stdint.h>

namespace swimmer {

constexpr uint32_t kCollisionPulseMs = 250;

class CollisionPulseController {
 public:
  void trigger(uint32_t now) {
    active_ = true;
    startedAtMs_ = now;
  }

  bool active(uint32_t now) {
    if (active_ && now - startedAtMs_ >= kCollisionPulseMs) {
      active_ = false;
    }
    return active_;
  }

 private:
  bool active_ = false;
  uint32_t startedAtMs_ = 0;
};

}  // namespace swimmer
