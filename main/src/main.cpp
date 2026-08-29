#include <Arduino.h>
#include <Adafruit_NeoPixel.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_now.h>

#include <cstddef>

#include "provision_protocol.h"

namespace {

constexpr char FIRMWARE_VERSION[] = "0.3.3";
constexpr char SCHEMA_VERSION[] = "1.0";

constexpr uint8_t LANE_1_PIN = 26;
constexpr uint8_t LANE_2_PIN = 17;
constexpr uint16_t LEDS_PER_LANE = 70;
constexpr uint8_t DEFAULT_BRIGHTNESS = 32;
constexpr uint32_t BOOT_TEST_COLOR_HOLD_MS = 300;
constexpr uint32_t BOOT_TEST_PIXEL_HOLD_MS = 20;

constexpr uint32_t DEFAULT_WATCHDOG_MS = 1500;
constexpr uint32_t HELLO_INTERVAL_MS = 5000;
constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 15000;
constexpr uint32_t WIFI_RETRY_INTERVAL_MS = 10000;
constexpr uint32_t PROVISION_BROADCAST_INTERVAL_MS = 10000;
constexpr uint32_t PROVISION_BURST_DURATION_MS = 2200;
constexpr uint32_t PROVISION_PACKET_INTERVAL_MS = 100;
constexpr size_t SERIAL_LINE_MAX = 2048;

constexpr uint8_t ESPNOW_BROADCAST_ADDRESS[] = {
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

Adafruit_NeoPixel lane1(
    LEDS_PER_LANE, LANE_1_PIN, NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel lane2(
    LEDS_PER_LANE, LANE_2_PIN, NEO_GRB + NEO_KHZ800);

Preferences preferences;
String inputLine;
String deviceId;
String currentSsid;
String currentPassword;
String pendingWifiRequestId;
uint8_t serverIpv4[4] = {0, 0, 0, 0};
uint16_t serverPort = 0;

bool protocolReady = false;
bool frameReceived = false;
bool wifiConfigured = false;
bool wifiConfigInProgress = false;
bool wifiWasConnected = false;
bool espNowReady = false;
bool endpointValid = false;
bool provisionBurstActive = false;
volatile bool espNowResultPending = false;
volatile bool espNowLastSendSucceeded = false;

uint32_t watchdogMs = DEFAULT_WATCHDOG_MS;
uint32_t lastRgbFrameMs = 0;
uint32_t lastHelloMs = 0;
uint32_t wifiConnectStartedMs = 0;
uint32_t lastWifiAttemptMs = 0;
uint32_t provisionBurstStartedMs = 0;
uint32_t lastProvisionPacketMs = 0;
uint32_t provisionSequence = 0;
uint32_t provisionBroadcastSuccesses = 0;
uint32_t provisionBroadcastFailures = 0;

static_assert(sizeof(spp::ProvisionPacket) <= ESP_NOW_MAX_DATA_LEN,
              "Provision packet must fit in one ESP-NOW message");
static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__,
              "SPP1 v2 integer fields require a little-endian target");

void onEspNowSent(
    const uint8_t *macAddress, esp_now_send_status_t status);
void triggerProvisionBurst();

void sendJson(const JsonDocument &document) {
  serializeJson(document, Serial);
  Serial.write('\n');
}

void sendError(const char *code,
               const char *message,
               const char *requestId = nullptr) {
  StaticJsonDocument<320> response;
  response["type"] = "error";
  response["schema_version"] = SCHEMA_VERSION;
  response["code"] = code;
  response["message"] = message;
  if (requestId != nullptr && requestId[0] != '\0') {
    response["request_id"] = requestId;
  }
  sendJson(response);
}

String serverIpv4String() {
  if (!endpointValid) {
    return "";
  }
  IPAddress address(
      serverIpv4[0], serverIpv4[1], serverIpv4[2], serverIpv4[3]);
  return address.toString();
}

void appendEndpoint(JsonDocument &message) {
  message["endpoint_valid"] = endpointValid;
  if (endpointValid) {
    message["server_ipv4"] = serverIpv4String();
    message["server_port"] = serverPort;
  }
}

bool parseServerIpv4(const char *value, uint8_t output[4]) {
  if (value == nullptr || value[0] == '\0') {
    return false;
  }

  IPAddress address;
  if (!address.fromString(value)) {
    return false;
  }

  for (size_t index = 0; index < 4; ++index) {
    output[index] = address[index];
  }
  return spp::isValidServerIpv4(output);
}

enum class EndpointParseStatus {
  Missing,
  Valid,
  Invalid,
};

EndpointParseStatus parseEndpointFields(
    const JsonDocument &document,
    uint8_t outputIpv4[4],
    uint16_t &outputPort) {
  const bool hasIpv4 = document.containsKey("server_ipv4");
  const bool hasPort = document.containsKey("server_port");
  const spp::EndpointFieldPresence presence =
      spp::classifyEndpointFields(hasIpv4, hasPort);
  if (presence == spp::EndpointFieldPresence::Missing) {
    return EndpointParseStatus::Missing;
  }
  if (presence == spp::EndpointFieldPresence::Partial) {
    return EndpointParseStatus::Invalid;
  }

  const char *ipv4 = document["server_ipv4"] | "";
  const JsonVariantConst portValue = document["server_port"];
  if (!portValue.is<uint32_t>()) {
    return EndpointParseStatus::Invalid;
  }
  const uint32_t parsedPort = portValue.as<uint32_t>();
  if (!spp::isValidServerPort(parsedPort) ||
      !parseServerIpv4(ipv4, outputIpv4)) {
    return EndpointParseStatus::Invalid;
  }

  outputPort = static_cast<uint16_t>(parsedPort);
  return EndpointParseStatus::Valid;
}

void saveServerEndpoint() {
  preferences.begin("spp-gateway", false);
  preferences.putString("server_ip", serverIpv4String());
  preferences.putUShort("server_port", serverPort);
  preferences.end();
}

bool setServerEndpoint(const uint8_t ipv4[4], uint16_t port) {
  const bool changed =
      !endpointValid || memcmp(serverIpv4, ipv4, sizeof(serverIpv4)) != 0 ||
      serverPort != port;
  memcpy(serverIpv4, ipv4, sizeof(serverIpv4));
  serverPort = port;
  endpointValid = true;
  if (changed) {
    saveServerEndpoint();
    triggerProvisionBurst();
  }
  return changed;
}

void invalidateServerEndpointForSession() {
  endpointValid = false;
  provisionBurstActive = false;
  memset(serverIpv4, 0, sizeof(serverIpv4));
  serverPort = 0;
}

void clearAll() {
  lane1.clear();
  lane2.clear();
  lane1.show();
  lane2.show();
}

Adafruit_NeoPixel *getLane(int laneNumber) {
  if (laneNumber == 1) {
    return &lane1;
  }
  if (laneNumber == 2) {
    return &lane2;
  }
  return nullptr;
}

bool validByteValue(int value) {
  return value >= 0 && value <= 255;
}

int hexNibble(char value) {
  if (value >= '0' && value <= '9') {
    return value - '0';
  }
  if (value >= 'A' && value <= 'F') {
    return value - 'A' + 10;
  }
  if (value >= 'a' && value <= 'f') {
    return value - 'a' + 10;
  }
  return -1;
}

bool validateHexFrame(const char *hex) {
  if (hex == nullptr || strlen(hex) != LEDS_PER_LANE * 6) {
    return false;
  }
  for (size_t index = 0; index < LEDS_PER_LANE * 6; ++index) {
    if (hexNibble(hex[index]) < 0) {
      return false;
    }
  }
  return true;
}

uint8_t parseHexByte(const char *hex) {
  return static_cast<uint8_t>(
      (hexNibble(hex[0]) << 4) | hexNibble(hex[1]));
}

void applyHexFrame(Adafruit_NeoPixel &strip, const char *hex) {
  for (uint16_t pixel = 0; pixel < LEDS_PER_LANE; ++pixel) {
    const char *color = hex + pixel * 6;
    strip.setPixelColor(
        pixel,
        strip.Color(
            parseHexByte(color),
            parseHexByte(color + 2),
            parseHexByte(color + 4)));
  }
}

String makeDeviceId() {
  const uint64_t mac = ESP.getEfuseMac();
  char suffix[13];
  snprintf(
      suffix,
      sizeof(suffix),
      "%04X%08X",
      static_cast<uint16_t>(mac >> 32),
      static_cast<uint32_t>(mac));
  return "rgb-gateway-" + String(suffix);
}

void sendGatewayHello() {
  StaticJsonDocument<384> message;
  message["type"] = "gateway_hello";
  message["schema_version"] = SCHEMA_VERSION;
  message["device_id"] = deviceId;
  message["firmware_version"] = FIRMWARE_VERSION;
  message["lanes"] = 2;
  message["leds_per_lane"] = LEDS_PER_LANE;
  message["lane1_pin"] = LANE_1_PIN;
  message["lane2_pin"] = LANE_2_PIN;
  sendJson(message);
  lastHelloMs = millis();
}

void sendGatewayStatus(const char *outputStatus) {
  StaticJsonDocument<512> message;
  message["type"] = "gateway_status";
  message["schema_version"] = SCHEMA_VERSION;
  message["device_id"] = deviceId;
  message["output_status"] = outputStatus;
  message["protocol_ready"] = protocolReady;
  JsonObject wifi = message.createNestedObject("wifi");
  wifi["status"] =
      WiFi.status() == WL_CONNECTED ? "connected" : "disconnected";
  if (WiFi.status() == WL_CONNECTED) {
    wifi["ssid"] = currentSsid;
    wifi["ip"] = WiFi.localIP().toString();
    wifi["rssi"] = WiFi.RSSI();
    wifi["channel"] = WiFi.channel();
  }
  message["broadcast_successes"] = provisionBroadcastSuccesses;
  message["broadcast_failures"] = provisionBroadcastFailures;
  message["broadcast_burst_active"] = provisionBurstActive;
  appendEndpoint(message);
  sendJson(message);
}

void sendWifiProgress(const String &requestId, const char *status) {
  StaticJsonDocument<256> message;
  message["type"] = "wifi_progress";
  message["schema_version"] = SCHEMA_VERSION;
  message["request_id"] = requestId;
  message["status"] = status;
  sendJson(message);
}

void sendWifiResult(const String &requestId,
                    bool success,
                    const char *errorCode = nullptr,
                    const char *errorMessage = nullptr) {
  StaticJsonDocument<448> message;
  message["type"] = "wifi_result";
  message["schema_version"] = SCHEMA_VERSION;
  message["request_id"] = requestId;
  message["success"] = success;
  message["ssid"] = currentSsid;
  if (success) {
    message["ip"] = WiFi.localIP().toString();
    message["rssi"] = WiFi.RSSI();
  } else {
    message["error_code"] =
        errorCode == nullptr ? "INTERNAL_ERROR" : errorCode;
    message["message"] =
        errorMessage == nullptr ? "Wi-Fi connection failed" : errorMessage;
  }
  appendEndpoint(message);
  sendJson(message);
}

void sendWifiStatus(const char *status) {
  StaticJsonDocument<448> message;
  message["type"] = "wifi_status";
  message["schema_version"] = SCHEMA_VERSION;
  message["status"] = status;
  message["ssid"] = currentSsid;
  if (WiFi.status() == WL_CONNECTED) {
    message["ip"] = WiFi.localIP().toString();
    message["rssi"] = WiFi.RSSI();
    message["channel"] = WiFi.channel();
  }
  appendEndpoint(message);
  sendJson(message);
}

void setPixel(int laneNumber,
              int pixelNumber,
              int red,
              int green,
              int blue) {
  Adafruit_NeoPixel *strip = getLane(laneNumber);
  if (strip == nullptr) {
    Serial.println(F("ERR lane must be 1 or 2"));
    return;
  }
  if (pixelNumber < 1 || pixelNumber > LEDS_PER_LANE) {
    Serial.printf("ERR pixel must be 1..%u\n", LEDS_PER_LANE);
    return;
  }
  if (!validByteValue(red) || !validByteValue(green) ||
      !validByteValue(blue)) {
    Serial.println(F("ERR r, g and b must be 0..255"));
    return;
  }

  strip->setPixelColor(
      pixelNumber - 1, strip->Color(red, green, blue));
  strip->show();
  Serial.printf(
      "OK lane=%d pixel=%d rgb=(%d,%d,%d)\n",
      laneNumber,
      pixelNumber,
      red,
      green,
      blue);
}

void fillLane(int laneNumber, int red, int green, int blue) {
  Adafruit_NeoPixel *strip = getLane(laneNumber);
  if (strip == nullptr) {
    Serial.println(F("ERR lane must be 1 or 2"));
    return;
  }
  if (!validByteValue(red) || !validByteValue(green) ||
      !validByteValue(blue)) {
    Serial.println(F("ERR r, g and b must be 0..255"));
    return;
  }

  strip->fill(strip->Color(red, green, blue));
  strip->show();
  Serial.printf(
      "OK filled lane=%d rgb=(%d,%d,%d)\n",
      laneNumber,
      red,
      green,
      blue);
}

void runTest(uint16_t count) {
  count = constrain(count, 1, LEDS_PER_LANE);
  clearAll();
  Serial.printf("TEST starting: %u pixels on each lane\n", count);

  const uint32_t colors[] = {
      lane1.Color(255, 0, 0),
      lane1.Color(0, 255, 0),
      lane1.Color(0, 0, 255),
  };
  const char *colorNames[] = {"RED", "GREEN", "BLUE"};

  for (size_t colorIndex = 0; colorIndex < 3; ++colorIndex) {
    Serial.printf("TEST color=%s\n", colorNames[colorIndex]);
    for (uint16_t pixel = 0; pixel < count; ++pixel) {
      lane1.clear();
      lane2.clear();
      lane1.setPixelColor(pixel, colors[colorIndex]);
      lane2.setPixelColor(pixel, colors[colorIndex]);
      lane1.show();
      lane2.show();
      delay(35);
    }
  }

  clearAll();
  Serial.println(F("TEST complete; both lanes cleared"));
}

void runBootSelfTest() {
  clearAll();
  Serial.println(F("[SELFTEST] RGB strips: red, green, blue, then pixel sweep"));

  const uint32_t colors[] = {
      lane1.Color(255, 0, 0),
      lane1.Color(0, 255, 0),
      lane1.Color(0, 0, 255),
  };
  for (const uint32_t color : colors) {
    lane1.fill(color);
    lane2.fill(color);
    lane1.show();
    lane2.show();
    delay(BOOT_TEST_COLOR_HOLD_MS);
  }

  const uint32_t white = lane1.Color(255, 255, 255);
  for (uint16_t pixel = 0; pixel < LEDS_PER_LANE; ++pixel) {
    lane1.clear();
    lane2.clear();
    lane1.setPixelColor(pixel, white);
    lane2.setPixelColor(pixel, white);
    lane1.show();
    lane2.show();
    delay(BOOT_TEST_PIXEL_HOLD_MS);
  }

  clearAll();
  Serial.println(F("[SELFTEST] RGB strips passed visual sequence; both lanes cleared"));
}

void showHelp() {
  Serial.println();
  Serial.println(F("=== Crown Shadow RGB gateway ==="));
  Serial.println(F("Pixel number is 1-based."));
  Serial.println(F("SET <lane> <n> <r> <g> <b>"));
  Serial.println(F("OFF <lane> <n>"));
  Serial.println(F("CLEAR <lane|ALL>"));
  Serial.println(F("FILL <lane> <r> <g> <b>"));
  Serial.println(F("BRIGHTNESS <0-255>"));
  Serial.println(F("TEST [pixel_count]"));
  Serial.println(F("STATUS"));
  Serial.println(F("HELP"));
  Serial.println();
}

void beginWifiConnection() {
  if (!wifiConfigured || currentSsid.isEmpty()) {
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(currentSsid.c_str(), currentPassword.c_str());
  wifiConnectStartedMs = millis();
  lastWifiAttemptMs = wifiConnectStartedMs;
}

bool initialiseEspNow() {
  if (espNowReady) {
    return true;
  }
  if (esp_now_init() != ESP_OK) {
    sendError("ESPNOW_INIT_FAILED", "Unable to initialise ESP-NOW");
    return false;
  }
  esp_now_register_send_cb(onEspNowSent);

  esp_now_peer_info_t peer{};
  memcpy(
      peer.peer_addr,
      ESPNOW_BROADCAST_ADDRESS,
      sizeof(ESPNOW_BROADCAST_ADDRESS));
  peer.channel = 0;
  peer.encrypt = false;
  peer.ifidx = WIFI_IF_STA;

  if (!esp_now_is_peer_exist(ESPNOW_BROADCAST_ADDRESS) &&
      esp_now_add_peer(&peer) != ESP_OK) {
    esp_now_deinit();
    sendError("ESPNOW_PEER_FAILED", "Unable to add broadcast peer");
    return false;
  }

  espNowReady = true;
  return true;
}

void onEspNowSent(
    const uint8_t *macAddress, esp_now_send_status_t status) {
  (void)macAddress;
  espNowLastSendSucceeded = status == ESP_NOW_SEND_SUCCESS;
  espNowResultPending = true;
}

bool provisioningBroadcastEligible() {
  return protocolReady && espNowReady && WiFi.status() == WL_CONNECTED &&
         wifiConfigured && endpointValid;
}

void triggerProvisionBurst() {
  if (!provisioningBroadcastEligible()) {
    provisionBurstActive = false;
    return;
  }
  const uint32_t now = millis();
  provisionBurstActive = true;
  provisionBurstStartedMs = now;
  lastProvisionPacketMs = now - PROVISION_PACKET_INTERVAL_MS;
}

void broadcastProvisioningPacket() {
  if (!provisioningBroadcastEligible()) {
    return;
  }

  spp::ProvisionPacket packet{};
  memcpy(packet.magic, spp::PROVISION_MAGIC, sizeof(packet.magic));
  packet.version = spp::PROVISION_VERSION;
  packet.sequence = ++provisionSequence;
  packet.ssidLength = static_cast<uint8_t>(currentSsid.length());
  packet.passwordLength =
      static_cast<uint8_t>(currentPassword.length());
  memcpy(packet.ssid, currentSsid.c_str(), packet.ssidLength);
  memcpy(
      packet.password,
      currentPassword.c_str(),
      packet.passwordLength);
  memcpy(packet.serverIpv4, serverIpv4, sizeof(packet.serverIpv4));
  packet.serverPort = serverPort;
  packet.crc32 = spp::calculateCrc32(
      reinterpret_cast<const uint8_t *>(&packet),
      offsetof(spp::ProvisionPacket, crc32));

  const esp_err_t result = esp_now_send(
      ESPNOW_BROADCAST_ADDRESS,
      reinterpret_cast<const uint8_t *>(&packet),
      sizeof(packet));
  if (result != ESP_OK) {
    espNowLastSendSucceeded = false;
    espNowResultPending = true;
  }
  lastProvisionPacketMs = millis();
}

void handleProvisionBroadcastSchedule() {
  if (!provisioningBroadcastEligible()) {
    provisionBurstActive = false;
    return;
  }

  const uint32_t now = millis();
  if (!provisionBurstActive &&
      now - provisionBurstStartedMs >= PROVISION_BROADCAST_INTERVAL_MS) {
    triggerProvisionBurst();
  }

  if (!provisionBurstActive) {
    return;
  }
  if (now - provisionBurstStartedMs > PROVISION_BURST_DURATION_MS) {
    provisionBurstActive = false;
    return;
  }
  if (now - lastProvisionPacketMs >= PROVISION_PACKET_INTERVAL_MS) {
    broadcastProvisioningPacket();
  }
}

void handleEspNowResult() {
  if (!espNowResultPending) {
    return;
  }
  espNowResultPending = false;
  if (espNowLastSendSucceeded) {
    ++provisionBroadcastSuccesses;
  } else {
    ++provisionBroadcastFailures;
  }

  StaticJsonDocument<448> message;
  message["type"] = "provision_broadcast";
  message["schema_version"] = SCHEMA_VERSION;
  message["packet_version"] = spp::PROVISION_VERSION;
  message["packet_size"] = sizeof(spp::ProvisionPacket);
  message["sequence"] = provisionSequence;
  message["sent"] = espNowLastSendSucceeded;
  message["channel"] = WiFi.channel();
  message["successes"] = provisionBroadcastSuccesses;
  message["failures"] = provisionBroadcastFailures;
  appendEndpoint(message);
  sendJson(message);
}

void handleWifiState() {
  const bool connected = WiFi.status() == WL_CONNECTED;

  if (connected && !wifiWasConnected) {
    wifiWasConnected = true;
    initialiseEspNow();
    if (wifiConfigInProgress) {
      sendWifiResult(pendingWifiRequestId, true);
      wifiConfigInProgress = false;
      pendingWifiRequestId = "";
    }
    sendWifiStatus("connected");
    triggerProvisionBurst();
  }

  if (!connected && wifiWasConnected) {
    wifiWasConnected = false;
    sendWifiStatus("disconnected");
  }

  if (wifiConfigInProgress && !connected &&
      millis() - wifiConnectStartedMs >= WIFI_CONNECT_TIMEOUT_MS) {
    const wl_status_t status = WiFi.status();
    if (status == WL_NO_SSID_AVAIL) {
      sendWifiResult(
          pendingWifiRequestId,
          false,
          "SSID_NOT_FOUND",
          "Wi-Fi network was not found");
    } else if (status == WL_CONNECT_FAILED) {
      sendWifiResult(
          pendingWifiRequestId,
          false,
          "AUTH_FAILED",
          "Wi-Fi authentication failed");
    } else {
      sendWifiResult(
          pendingWifiRequestId,
          false,
          "CONNECT_TIMEOUT",
          "Wi-Fi connection timed out");
    }
    wifiConfigInProgress = false;
    pendingWifiRequestId = "";
  }

  if (wifiConfigured && !connected && !wifiConfigInProgress &&
      millis() - lastWifiAttemptMs >= WIFI_RETRY_INTERVAL_MS) {
    beginWifiConnection();
  }

}

void saveWifiCredentials(const String &ssid, const String &password) {
  preferences.begin("spp-gateway", false);
  preferences.putString("ssid", ssid);
  preferences.putString("password", password);
  preferences.end();
}

void loadWifiCredentials() {
  preferences.begin("spp-gateway", true);
  currentSsid = preferences.getString("ssid", "");
  currentPassword = preferences.getString("password", "");
  const String savedServerIpv4 =
      preferences.getString("server_ip", "");
  const uint16_t savedServerPort =
      preferences.getUShort("server_port", 0);
  preferences.end();
  wifiConfigured = !currentSsid.isEmpty();

  uint8_t parsedIpv4[4];
  endpointValid =
      savedServerPort > 0 &&
      parseServerIpv4(savedServerIpv4.c_str(), parsedIpv4);
  if (endpointValid) {
    memcpy(serverIpv4, parsedIpv4, sizeof(serverIpv4));
    serverPort = savedServerPort;
  }
}

void processRgbFrame(JsonDocument &document) {
  if (!protocolReady) {
    sendError(
        "GATEWAY_NOT_READY",
        "gateway_ready must be received before rgb_frame");
    return;
  }

  const int ledsPerLane = document["leds_per_lane"] | 0;
  const char *lane1Hex = document["lane1_hex"];
  const char *lane2Hex = document["lane2_hex"];
  const uint32_t sequence = document["seq"] | 0;

  if (ledsPerLane != LEDS_PER_LANE) {
    sendError(
        "INVALID_LED_COUNT", "leds_per_lane must be exactly 70");
    return;
  }
  if (!validateHexFrame(lane1Hex) || !validateHexFrame(lane2Hex)) {
    sendError(
        "INVALID_RGB_FRAME",
        "lane hex fields must contain exactly 420 hex characters");
    return;
  }

  applyHexFrame(lane1, lane1Hex);
  applyHexFrame(lane2, lane2Hex);
  lane1.show();
  lane2.show();

  lastRgbFrameMs = millis();
  frameReceived = true;

  StaticJsonDocument<192> response;
  response["type"] = "rgb_ack";
  response["schema_version"] = SCHEMA_VERSION;
  response["seq"] = sequence;
  response["applied"] = true;
  sendJson(response);
}

void processWifiConfig(JsonDocument &document) {
  const char *requestIdRaw = document["request_id"] | "";
  const char *ssidRaw = document["ssid"] | "";
  const char *passwordRaw = document["password"] | "";
  const String requestId = requestIdRaw;
  const String ssid = ssidRaw;
  const String password = passwordRaw;

  if (requestId.isEmpty()) {
    sendError(
        "INVALID_CREDENTIALS",
        "request_id is required",
        requestIdRaw);
    return;
  }
  if (wifiConfigInProgress) {
    sendError(
        "WIFI_CONFIG_BUSY",
        "another Wi-Fi configuration is already running",
        requestIdRaw);
    return;
  }
  if (ssid.length() < 1 || ssid.length() > 32 ||
      (!password.isEmpty() &&
       (password.length() < 8 || password.length() > 63))) {
    sendWifiResult(
        requestId,
        false,
        "INVALID_CREDENTIALS",
        "SSID must be 1-32 bytes and password 8-63 bytes or empty");
    return;
  }

  uint8_t parsedServerIpv4[4];
  uint16_t parsedServerPort = 0;
  const EndpointParseStatus endpointStatus = parseEndpointFields(
      document, parsedServerIpv4, parsedServerPort);
  if (endpointStatus == EndpointParseStatus::Invalid) {
    sendError(
        "INVALID_SERVER_ENDPOINT",
        "server_ipv4 and server_port must be a valid unicast IPv4 endpoint",
        requestIdRaw);
    return;
  }

  currentSsid = ssid;
  currentPassword = password;
  wifiConfigured = true;
  wifiConfigInProgress = true;
  pendingWifiRequestId = requestId;
  saveWifiCredentials(currentSsid, currentPassword);
  if (endpointStatus == EndpointParseStatus::Valid) {
    setServerEndpoint(parsedServerIpv4, parsedServerPort);
  }
  sendWifiProgress(requestId, "connecting");

  if (espNowReady) {
    esp_now_deinit();
    espNowReady = false;
    provisionBurstActive = false;
  }
  WiFi.disconnect(true, false);
  delay(100);
  beginWifiConnection();
}

void processJsonCommand(const String &line) {
  DynamicJsonDocument document(3072);
  const DeserializationError error = deserializeJson(document, line);
  if (error) {
    sendError("INVALID_JSON", error.c_str());
    return;
  }

  const char *type = document["type"] | "";
  const char *schemaVersion = document["schema_version"] | "";
  if (strcmp(schemaVersion, SCHEMA_VERSION) != 0) {
    sendError(
        "UNSUPPORTED_SCHEMA_VERSION",
        "schema_version must be 1.0");
    return;
  }

  if (strcmp(type, "gateway_ready") == 0) {
    uint8_t parsedServerIpv4[4];
    uint16_t parsedServerPort = 0;
    const EndpointParseStatus endpointStatus = parseEndpointFields(
        document, parsedServerIpv4, parsedServerPort);
    if (endpointStatus == EndpointParseStatus::Invalid) {
      sendError(
          "INVALID_SERVER_ENDPOINT",
          "server_ipv4 and server_port must be omitted or supplied as a valid pair");
      return;
    }

    const uint32_t requestedWatchdog =
        document["watchdog_ms"] | DEFAULT_WATCHDOG_MS;
    watchdogMs = constrain(requestedWatchdog, 500UL, 5000UL);
    protocolReady = true;
    if (endpointStatus == EndpointParseStatus::Valid) {
      const bool endpointChanged =
          setServerEndpoint(parsedServerIpv4, parsedServerPort);
      if (!endpointChanged) {
        triggerProvisionBurst();
      }
    } else {
      invalidateServerEndpointForSession();
    }

    StaticJsonDocument<320> response;
    response["type"] = "gateway_ready_ack";
    response["schema_version"] = SCHEMA_VERSION;
    response["accepted"] = true;
    response["watchdog_ms"] = watchdogMs;
    appendEndpoint(response);
    sendJson(response);
    // A restarted computer does not yet know the gateway's LAN address.
    // Send a complete snapshot after every handshake so it can derive and
    // return a reachable server endpoint without reconfiguring Wi-Fi.
    sendWifiStatus(
        WiFi.status() == WL_CONNECTED ? "connected" : "disconnected");
    return;
  }

  if (strcmp(type, "rgb_frame") == 0) {
    processRgbFrame(document);
    return;
  }

  if (strcmp(type, "wifi_config") == 0) {
    processWifiConfig(document);
    return;
  }

  if (strcmp(type, "gateway_probe") == 0) {
    sendGatewayHello();
    return;
  }

  sendError("UNKNOWN_MESSAGE_TYPE", "unknown serial message type");
}

void processTextCommand(String command) {
  command.trim();
  if (command.isEmpty()) {
    return;
  }

  String upper = command;
  upper.toUpperCase();

  int laneNumber;
  int pixelNumber;
  int red;
  int green;
  int blue;
  int value;

  if (sscanf(
          upper.c_str(),
          "SET %d %d %d %d %d",
          &laneNumber,
          &pixelNumber,
          &red,
          &green,
          &blue) == 5) {
    setPixel(laneNumber, pixelNumber, red, green, blue);
    return;
  }

  if (sscanf(
          upper.c_str(),
          "OFF %d %d",
          &laneNumber,
          &pixelNumber) == 2) {
    setPixel(laneNumber, pixelNumber, 0, 0, 0);
    return;
  }

  if (sscanf(
          upper.c_str(),
          "FILL %d %d %d %d",
          &laneNumber,
          &red,
          &green,
          &blue) == 4) {
    fillLane(laneNumber, red, green, blue);
    return;
  }

  if (upper == "CLEAR ALL") {
    clearAll();
    Serial.println(F("OK all lanes cleared"));
    return;
  }

  if (sscanf(upper.c_str(), "CLEAR %d", &laneNumber) == 1) {
    Adafruit_NeoPixel *strip = getLane(laneNumber);
    if (strip == nullptr) {
      Serial.println(F("ERR lane must be 1 or 2"));
      return;
    }
    strip->clear();
    strip->show();
    Serial.printf("OK lane=%d cleared\n", laneNumber);
    return;
  }

  if (sscanf(upper.c_str(), "BRIGHTNESS %d", &value) == 1) {
    if (!validByteValue(value)) {
      Serial.println(F("ERR brightness must be 0..255"));
      return;
    }
    lane1.setBrightness(value);
    lane2.setBrightness(value);
    lane1.show();
    lane2.show();
    Serial.printf("OK brightness=%d\n", value);
    return;
  }

  if (upper == "TEST") {
    runTest(LEDS_PER_LANE);
    return;
  }

  if (sscanf(upper.c_str(), "TEST %d", &value) == 1) {
    if (value < 1 || value > LEDS_PER_LANE) {
      Serial.printf(
          "ERR test pixel count must be 1..%u\n", LEDS_PER_LANE);
      return;
    }
    runTest(value);
    return;
  }

  if (upper == "STATUS") {
    Serial.printf(
        "OK lane1_pin=%u lane2_pin=%u leds=%u protocol=%s wifi=%s endpoint=%s\n",
        LANE_1_PIN,
        LANE_2_PIN,
        LEDS_PER_LANE,
        protocolReady ? "ready" : "waiting",
        WiFi.status() == WL_CONNECTED ? "connected" : "disconnected",
        endpointValid ? "valid" : "missing");
    sendGatewayStatus(frameReceived ? "streaming" : "idle");
    return;
  }

  if (upper == "HELP") {
    showHelp();
    return;
  }

  Serial.println(F("ERR unknown command; type HELP"));
}

void processLine(String line) {
  line.trim();
  if (line.isEmpty()) {
    return;
  }
  if (line[0] == '{') {
    processJsonCommand(line);
  } else {
    processTextCommand(line);
  }
}

void readSerial() {
  while (Serial.available() > 0) {
    const char character = static_cast<char>(Serial.read());
    if (character == '\r') {
      continue;
    }
    if (character == '\n') {
      processLine(inputLine);
      inputLine = "";
      continue;
    }
    if (inputLine.length() < SERIAL_LINE_MAX) {
      inputLine += character;
    } else {
      inputLine = "";
      sendError("SERIAL_LINE_TOO_LONG", "serial line exceeds 2048 bytes");
    }
  }
}

void handleRgbWatchdog() {
  if (frameReceived &&
      millis() - lastRgbFrameMs > watchdogMs) {
    clearAll();
    frameReceived = false;
    sendGatewayStatus("watchdog_clear");
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);

  deviceId = makeDeviceId();
  inputLine.reserve(SERIAL_LINE_MAX + 1);

  lane1.begin();
  lane2.begin();
  lane1.setBrightness(DEFAULT_BRIGHTNESS);
  lane2.setBrightness(DEFAULT_BRIGHTNESS);
  runBootSelfTest();

  WiFi.mode(WIFI_STA);
  loadWifiCredentials();
  if (wifiConfigured) {
    beginWifiConnection();
  }

  sendGatewayHello();
}

void loop() {
  readSerial();
  handleRgbWatchdog();
  handleWifiState();
  handleProvisionBroadcastSchedule();
  handleEspNowResult();

  if (millis() - lastHelloMs >= HELLO_INTERVAL_MS) {
    sendGatewayHello();
  }

  delay(1);
}
