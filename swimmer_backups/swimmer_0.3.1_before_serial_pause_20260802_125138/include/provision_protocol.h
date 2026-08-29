#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace spp {

constexpr char kProvisionMagic[] = "SPP1";
constexpr uint8_t kProvisionVersion = 2;

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
static_assert(offsetof(ProvisionPacket, serverIpv4) == 106,
              "SPP1 v2 endpoint offset changed");
static_assert(offsetof(ProvisionPacket, serverPort) == 110,
              "SPP1 v2 port offset changed");
static_assert(offsetof(ProvisionPacket, crc32) == 112,
              "SPP1 v2 CRC offset changed");

inline uint32_t calculateCrc32(const uint8_t *data, size_t length) {
  uint32_t crc = 0xFFFFFFFFUL;
  for (size_t index = 0; index < length; ++index) {
    crc ^= data[index];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ ((crc & 1U) ? 0xEDB88320UL : 0U);
    }
  }
  return crc ^ 0xFFFFFFFFUL;
}

inline bool validServerAddress(const uint8_t address[4], uint16_t port) {
  if (address == nullptr || port == 0 || address[0] == 0 ||
      address[0] == 127 || address[0] >= 224 || address[3] == 255) {
    return false;
  }
  return !(address[0] == 255 && address[1] == 255 &&
           address[2] == 255 && address[3] == 255);
}

inline bool validProvisionPacket(const ProvisionPacket &packet,
                                 size_t receivedLength) {
  if (receivedLength != sizeof(ProvisionPacket) ||
      memcmp(packet.magic, kProvisionMagic, sizeof(packet.magic)) != 0 ||
      packet.version != kProvisionVersion) {
    return false;
  }
  if (packet.ssidLength == 0 || packet.ssidLength > sizeof(packet.ssid) ||
      packet.passwordLength > sizeof(packet.password) ||
      (packet.passwordLength != 0 && packet.passwordLength < 8)) {
    return false;
  }
  if (!validServerAddress(packet.serverIpv4, packet.serverPort)) {
    return false;
  }
  const uint32_t expected = calculateCrc32(
      reinterpret_cast<const uint8_t *>(&packet),
      offsetof(ProvisionPacket, crc32));
  return expected == packet.crc32;
}

}  // namespace spp
