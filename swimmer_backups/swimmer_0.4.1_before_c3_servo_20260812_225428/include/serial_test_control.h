#pragma once

#include <stddef.h>
#include <stdint.h>

namespace swimmer {

enum class SerialTestCommand : uint8_t {
  None,
  Stop,
  Continue,
};

inline char asciiLower(char value) {
  return value >= 'A' && value <= 'Z' ? value + ('a' - 'A') : value;
}

inline bool commandEquals(const char *input, size_t length,
                          const char *expected, size_t expectedLength) {
  if (length != expectedLength) {
    return false;
  }
  for (size_t index = 0; index < length; ++index) {
    if (asciiLower(input[index]) != expected[index]) {
      return false;
    }
  }
  return true;
}

inline SerialTestCommand parseSerialTestCommand(const char *input,
                                                size_t length) {
  if (input == nullptr) {
    return SerialTestCommand::None;
  }
  size_t begin = 0;
  while (begin < length &&
         (input[begin] == ' ' || input[begin] == '\t' ||
          input[begin] == '\r' || input[begin] == '\n')) {
    ++begin;
  }
  while (length > begin &&
         (input[length - 1] == ' ' || input[length - 1] == '\t' ||
          input[length - 1] == '\r' || input[length - 1] == '\n')) {
    --length;
  }

  const size_t commandLength = length - begin;
  const char *command = input + begin;
  if (commandEquals(command, commandLength, "stop", 4)) {
    return SerialTestCommand::Stop;
  }
  if (commandEquals(command, commandLength, "continue", 8)) {
    return SerialTestCommand::Continue;
  }
  return SerialTestCommand::None;
}

}  // namespace swimmer
