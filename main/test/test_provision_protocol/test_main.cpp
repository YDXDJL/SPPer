#include <unity.h>

#include <cstring>

#include "provision_protocol.h"

void setUp() {}
void tearDown() {}

namespace {

void testWireLayout() {
  TEST_ASSERT_EQUAL_UINT32(116, sizeof(spp::ProvisionPacket));
  TEST_ASSERT_EQUAL_UINT32(106, offsetof(spp::ProvisionPacket, serverIpv4));
  TEST_ASSERT_EQUAL_UINT32(110, offsetof(spp::ProvisionPacket, serverPort));
  TEST_ASSERT_EQUAL_UINT32(112, offsetof(spp::ProvisionPacket, crc32));
}

void testGoldenPacketCrcAndEndpointEncoding() {
  spp::ProvisionPacket packet{};
  memcpy(packet.magic, spp::PROVISION_MAGIC, sizeof(packet.magic));
  packet.version = spp::PROVISION_VERSION;
  packet.sequence = 0x01020304U;

  constexpr char ssid[] = "Pool-Safety";
  constexpr char password[] = "example-password";
  packet.ssidLength = sizeof(ssid) - 1;
  packet.passwordLength = sizeof(password) - 1;
  memcpy(packet.ssid, ssid, packet.ssidLength);
  memcpy(packet.password, password, packet.passwordLength);

  packet.serverIpv4[0] = 192;
  packet.serverIpv4[1] = 168;
  packet.serverIpv4[2] = 1;
  packet.serverIpv4[3] = 3;
  packet.serverPort = 8000;

  const auto *wire = reinterpret_cast<const uint8_t *>(&packet);
  TEST_ASSERT_EQUAL_HEX8(0xC0, wire[106]);
  TEST_ASSERT_EQUAL_HEX8(0xA8, wire[107]);
  TEST_ASSERT_EQUAL_HEX8(0x01, wire[108]);
  TEST_ASSERT_EQUAL_HEX8(0x03, wire[109]);
  TEST_ASSERT_EQUAL_HEX8(0x40, wire[110]);
  TEST_ASSERT_EQUAL_HEX8(0x1F, wire[111]);

  packet.crc32 = spp::calculateCrc32(wire, 112);
  TEST_ASSERT_EQUAL_HEX32(0x4BB94BEAU, packet.crc32);
}

void testCrcDetectsPayloadChanges() {
  spp::ProvisionPacket packet{};
  memcpy(packet.magic, spp::PROVISION_MAGIC, sizeof(packet.magic));
  packet.version = spp::PROVISION_VERSION;
  packet.serverIpv4[0] = 10;
  packet.serverIpv4[3] = 20;
  packet.serverPort = 8000;

  const auto *wire = reinterpret_cast<const uint8_t *>(&packet);
  const uint32_t before = spp::calculateCrc32(wire, 112);
  packet.serverIpv4[3] = 21;
  const uint32_t after = spp::calculateCrc32(wire, 112);
  TEST_ASSERT_NOT_EQUAL(before, after);
}

void testEndpointValidationBoundaries() {
  const uint8_t validPrivate[] = {192, 168, 1, 3};
  const uint8_t unspecified[] = {0, 0, 0, 0};
  const uint8_t loopback[] = {127, 0, 0, 1};
  const uint8_t multicast[] = {224, 0, 0, 1};
  const uint8_t broadcast[] = {192, 168, 1, 255};

  TEST_ASSERT_TRUE(spp::isValidServerIpv4(validPrivate));
  TEST_ASSERT_FALSE(spp::isValidServerIpv4(unspecified));
  TEST_ASSERT_FALSE(spp::isValidServerIpv4(loopback));
  TEST_ASSERT_FALSE(spp::isValidServerIpv4(multicast));
  TEST_ASSERT_FALSE(spp::isValidServerIpv4(broadcast));
  TEST_ASSERT_FALSE(spp::isValidServerPort(0));
  TEST_ASSERT_TRUE(spp::isValidServerPort(1));
  TEST_ASSERT_TRUE(spp::isValidServerPort(65535));
  TEST_ASSERT_FALSE(spp::isValidServerPort(65536));
}

void testTwoStageGatewayReadyEndpointPolicy() {
  bool protocolReady = false;
  bool endpointValid = true;  // Simulate a stale persisted endpoint.
  bool wifiConnected = false;

  const auto firstReady = spp::classifyEndpointFields(false, false);
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(spp::EndpointFieldPresence::Missing),
      static_cast<int>(firstReady));
  protocolReady = firstReady != spp::EndpointFieldPresence::Partial;
  if (firstReady == spp::EndpointFieldPresence::Missing) {
    endpointValid = false;
  }
  TEST_ASSERT_TRUE(protocolReady);
  TEST_ASSERT_FALSE(endpointValid);
  TEST_ASSERT_FALSE(wifiConnected && endpointValid);

  wifiConnected = true;
  TEST_ASSERT_FALSE(wifiConnected && endpointValid);

  const auto secondReady = spp::classifyEndpointFields(true, true);
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(spp::EndpointFieldPresence::Complete),
      static_cast<int>(secondReady));
  endpointValid = secondReady == spp::EndpointFieldPresence::Complete;
  TEST_ASSERT_TRUE(protocolReady);
  TEST_ASSERT_TRUE(wifiConnected && endpointValid);

  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(spp::EndpointFieldPresence::Partial),
      static_cast<int>(spp::classifyEndpointFields(true, false)));
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(spp::EndpointFieldPresence::Partial),
      static_cast<int>(spp::classifyEndpointFields(false, true)));
}

}  // namespace

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(testWireLayout);
  RUN_TEST(testGoldenPacketCrcAndEndpointEncoding);
  RUN_TEST(testCrcDetectsPayloadChanges);
  RUN_TEST(testEndpointValidationBoundaries);
  RUN_TEST(testTwoStageGatewayReadyEndpointPolicy);
  return UNITY_END();
}
