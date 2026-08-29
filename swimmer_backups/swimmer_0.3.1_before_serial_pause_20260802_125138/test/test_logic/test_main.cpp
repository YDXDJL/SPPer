#include <unity.h>

#include <cstring>

#include "provision_protocol.h"
#include "swimmer_safety.h"

void setUp() {}
void tearDown() {}

namespace {

spp::ProvisionPacket makeGoldenPacket() {
  spp::ProvisionPacket packet{};
  memcpy(packet.magic, spp::kProvisionMagic, sizeof(packet.magic));
  packet.version = spp::kProvisionVersion;
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
  packet.crc32 = spp::calculateCrc32(
      reinterpret_cast<const uint8_t *>(&packet),
      offsetof(spp::ProvisionPacket, crc32));
  return packet;
}

void testProvisionGoldenVector() {
  const spp::ProvisionPacket packet = makeGoldenPacket();
  const auto *wire = reinterpret_cast<const uint8_t *>(&packet);
  TEST_ASSERT_EQUAL_UINT32(116, sizeof(packet));
  TEST_ASSERT_EQUAL_HEX8(0xC0, wire[106]);
  TEST_ASSERT_EQUAL_HEX8(0xA8, wire[107]);
  TEST_ASSERT_EQUAL_HEX8(0x01, wire[108]);
  TEST_ASSERT_EQUAL_HEX8(0x03, wire[109]);
  TEST_ASSERT_EQUAL_HEX8(0x40, wire[110]);
  TEST_ASSERT_EQUAL_HEX8(0x1F, wire[111]);
  TEST_ASSERT_EQUAL_HEX32(0x4BB94BEAU, packet.crc32);
  TEST_ASSERT_TRUE(spp::validProvisionPacket(packet, sizeof(packet)));
}

void testProvisionRejectsInvalidInputs() {
  spp::ProvisionPacket packet = makeGoldenPacket();
  TEST_ASSERT_FALSE(spp::validProvisionPacket(packet, 110));
  packet.version = 1;
  TEST_ASSERT_FALSE(spp::validProvisionPacket(packet, sizeof(packet)));

  packet = makeGoldenPacket();
  packet.serverIpv4[0] = 127;
  packet.crc32 = spp::calculateCrc32(
      reinterpret_cast<const uint8_t *>(&packet),
      offsetof(spp::ProvisionPacket, crc32));
  TEST_ASSERT_FALSE(spp::validProvisionPacket(packet, sizeof(packet)));

  packet = makeGoldenPacket();
  packet.passwordLength = 7;
  packet.crc32 = spp::calculateCrc32(
      reinterpret_cast<const uint8_t *>(&packet),
      offsetof(spp::ProvisionPacket, crc32));
  TEST_ASSERT_FALSE(spp::validProvisionPacket(packet, sizeof(packet)));

  packet = makeGoldenPacket();
  packet.ssid[0] ^= 1;
  TEST_ASSERT_FALSE(spp::validProvisionPacket(packet, sizeof(packet)));
}

void testSafetyThresholdBoundaries() {
  swimmer::SafetyController safety;
  safety.beginMonitoring(1000);
  safety.update(10999);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(swimmer::AlertStage::Normal),
                        static_cast<int>(safety.alertStage()));
  TEST_ASSERT_FALSE(safety.vibrationRequired());

  safety.update(11000);
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(swimmer::AlertStage::SuspectedDrowning),
      static_cast<int>(safety.alertStage()));
  TEST_ASSERT_TRUE(safety.vibrationRequired());

  safety.update(20999);
  TEST_ASSERT_FALSE(safety.rescueLatched());
  safety.update(21000);
  TEST_ASSERT_TRUE(safety.rescueLatched());
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(swimmer::AlertStage::RescueTriggered),
      static_cast<int>(safety.alertStage()));
}

void testOnlyMatchingAcceptedAckRefreshesWatchdog() {
  swimmer::SafetyController safety;
  safety.beginMonitoring(0);
  safety.update(10000);

  safety.onHeartbeatAck(true, true, 10001);
  TEST_ASSERT_EQUAL_UINT8(1, safety.recoveryAckCount());
  TEST_ASSERT_EQUAL_UINT32(0, safety.communicationLossMs(10001));

  safety.onHeartbeatAck(false, true, 10002);
  TEST_ASSERT_EQUAL_UINT8(0, safety.recoveryAckCount());
  TEST_ASSERT_EQUAL_UINT32(1, safety.communicationLossMs(10002));

  safety.onHeartbeatAck(true, false, 10003);
  TEST_ASSERT_EQUAL_UINT8(0, safety.recoveryAckCount());
  TEST_ASSERT_EQUAL_UINT32(2, safety.communicationLossMs(10003));
  TEST_ASSERT_TRUE(safety.vibrationRequired());
}

void testThreeAcksRecoverButRescueStaysLatchedUntilReset() {
  swimmer::SafetyController safety;
  safety.beginMonitoring(0);
  safety.update(20000);
  TEST_ASSERT_TRUE(safety.rescueLatched());
  TEST_ASSERT_FALSE(safety.resetRescueLatch(20000));

  safety.onHeartbeatAck(true, true, 20001);
  safety.onHeartbeatAck(true, true, 21001);
  TEST_ASSERT_TRUE(safety.vibrationRequired());
  safety.onHeartbeatAck(true, true, 22001);
  TEST_ASSERT_FALSE(safety.vibrationRequired());
  TEST_ASSERT_TRUE(safety.rescueLatched());
  TEST_ASSERT_TRUE(safety.resetRescueLatch(22002));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(swimmer::AlertStage::Normal),
                        static_cast<int>(safety.alertStage()));
}

void testMillisWraparoundUsesUnsignedElapsedTime() {
  swimmer::SafetyController safety;
  safety.beginMonitoring(0xFFFFFF00U);
  safety.update(0x0000260FU);  // 9,999 ms after start across wrap.
  TEST_ASSERT_FALSE(safety.vibrationRequired());
  safety.update(0x00002610U);  // 10,000 ms after start.
  TEST_ASSERT_TRUE(safety.vibrationRequired());
}

}  // namespace

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(testProvisionGoldenVector);
  RUN_TEST(testProvisionRejectsInvalidInputs);
  RUN_TEST(testSafetyThresholdBoundaries);
  RUN_TEST(testOnlyMatchingAcceptedAckRefreshesWatchdog);
  RUN_TEST(testThreeAcksRecoverButRescueStaysLatchedUntilReset);
  RUN_TEST(testMillisWraparoundUsesUnsignedElapsedTime);
  return UNITY_END();
}
