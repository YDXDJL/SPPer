#pragma once

#include <stddef.h>
#include <stdint.h>

namespace spp {

constexpr char PROVISION_MAGIC[] = "SPP1";
constexpr uint8_t PROVISION_VERSION = 2;

struct __attribute__((packed)) ProvisionPacket {
  char magic[4];
  uint8_t version;
  uint32_t sequence;
  uint8_t ssidLength;
  uint8_t passwordLength;
  char ssid[32];
  char password[63];
  uint8_t serverIpv4[4];
  uint16_t serverPort;
  uint32_t crc32;
};

static_assert(sizeof(ProvisionPacket) == 116,
              "SPP1 v2 packet must be exactly 116 bytes");
static_assert(offsetof(ProvisionPacket, sequence) == 5,
              "SPP1 v2 sequence offset changed");
static_assert(offsetof(ProvisionPacket, ssidLength) == 9,
              "SPP1 v2 SSID length offset changed");
static_assert(offsetof(ProvisionPacket, passwordLength) == 10,
              "SPP1 v2 password length offset changed");
static_assert(offsetof(ProvisionPacket, ssid) == 11,
              "SPP1 v2 SSID offset changed");
static_assert(offsetof(ProvisionPacket, password) == 43,
              "SPP1 v2 password offset changed");
static_assert(offsetof(ProvisionPacket, serverIpv4) == 106,
              "SPP1 v2 server IPv4 offset changed");
static_assert(offsetof(ProvisionPacket, serverPort) == 110,
              "SPP1 v2 server port offset changed");
static_assert(offsetof(ProvisionPacket, crc32) == 112,
              "SPP1 v2 CRC offset changed");

inline uint32_t calculateCrc32(const uint8_t *data, size_t length) {
  uint32_t crc = 0xFFFFFFFF;
  for (size_t index = 0; index < length; ++index) {
    crc ^= data[index];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
    }
  }
  return crc ^ 0xFFFFFFFF;
}

inline bool isValidServerIpv4(const uint8_t ipv4[4]) {
  return ipv4 != nullptr && ipv4[0] != 0 && ipv4[0] != 127 &&
         ipv4[0] < 224 && ipv4[3] != 255;
}

inline bool isValidServerPort(uint32_t port) {
  return port >= 1 && port <= 65535;
}

enum class EndpointFieldPresence {
  Missing,
  Complete,
  Partial,
};

inline EndpointFieldPresence classifyEndpointFields(
    bool hasIpv4, bool hasPort) {
  if (!hasIpv4 && !hasPort) {
    return EndpointFieldPresence::Missing;
  }
  if (hasIpv4 && hasPort) {
    return EndpointFieldPresence::Complete;
  }
  return EndpointFieldPresence::Partial;
}

}  // namespace spp
