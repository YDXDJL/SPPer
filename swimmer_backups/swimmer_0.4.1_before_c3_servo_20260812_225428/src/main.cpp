#include <Arduino.h>
#include <ArduinoJson.h>
#include <U8g2lib.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <time.h>

#include "collision_pulse.h"
#include "provision_protocol.h"
#include "serial_test_control.h"
#include "swimmer_safety.h"

namespace {

constexpr char kFirmwareVersion[] = "0.4.1";
constexpr char kSchemaVersion[] = "1.0";

constexpr uint8_t kSdaPin = 15;
constexpr uint8_t kSclPin = 16;
constexpr uint8_t kVibrationPin = 17;
constexpr uint8_t kVibrationActiveLevel = HIGH;
constexpr uint8_t kVibrationInactiveLevel = LOW;

constexpr uint32_t kChannelDwellMs = 150;
constexpr uint32_t kWifiConnectTimeoutMs = 15000;
constexpr uint32_t kRegistrationTimeoutMs = 5000;
constexpr uint32_t kHeartbeatIntervalMs = 1000;
constexpr uint32_t kServerSilenceReconnectMs = 15000;
constexpr uint32_t kServerSearchResetMs = 30000;
constexpr uint32_t kVibrationOnMs = 200;
constexpr uint32_t kVibrationOffMs = 100;
constexpr uint32_t kDisplayRefreshMs = 200;
constexpr uint32_t kSerialCommandIdleMs = 1000;
constexpr uint32_t kReconnectDelaysMs[] = {1000, 2000, 5000, 10000};

enum class CommunicationState : uint8_t {
  Searching,
  WifiConnecting,
  ServerConnecting,
  Registered,
};

U8G2_SSD1306_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);
WebSocketsClient webSocket;
swimmer::SafetyController safety;
swimmer::CollisionPulseController collisionPulse;

portMUX_TYPE provisionMux = portMUX_INITIALIZER_UNLOCKED;
volatile bool provisionPending = false;
spp::ProvisionPacket pendingProvision{};

CommunicationState communicationState = CommunicationState::Searching;
swimmer::AlertStage lastReportedAlertStage = swimmer::AlertStage::Normal;

bool oledFound = false;
bool espNowReady = false;
bool webSocketStarted = false;
bool webSocketConnected = false;
bool helloAccepted = false;
bool vibrationOutputOn = false;
bool heartbeatSyncPaused = false;
bool remoteSimulationPaused = false;
bool serialCommandOverflow = false;

uint8_t scanChannel = 1;
uint8_t reconnectIndex = 0;
uint32_t lastChannelChangeMs = 0;
uint32_t wifiConnectStartedMs = 0;
uint32_t serverConnectStartedMs = 0;
uint32_t websocketConnectedAtMs = 0;
uint32_t lastServerMessageMs = 0;
uint32_t lastHeartbeatSentMs = 0;
uint32_t lastVibrationChangeMs = 0;
uint32_t lastDisplayRefreshMs = 0;
uint32_t lastSerialCommandByteMs = 0;
uint32_t messageSequence = 0;

String deviceId;
String bootId;
String currentSsid;
String currentPassword;
String serverHost;
uint16_t serverPort = 0;
String helloMessageId;
String pendingHeartbeatId;
String pendingHeartbeatPayload;
String lastAcceptedResetId;
String lastAcceptedControlId;

char serialCommandBuffer[24]{};
size_t serialCommandLength = 0;

bool heartbeatPaused() {
  return heartbeatSyncPaused || remoteSimulationPaused;
}

void applySerialTestCommand(swimmer::SerialTestCommand command,
                            uint32_t now) {
  if (command == swimmer::SerialTestCommand::Stop) {
    if (heartbeatSyncPaused) {
      Serial.println(F("[TEST] Heartbeat sync is already stopped"));
      return;
    }
    heartbeatSyncPaused = true;
    pendingHeartbeatId = "";
    pendingHeartbeatPayload = "";
    Serial.println(F("[TEST] STOP accepted: application heartbeat sync paused"));
    Serial.println(F("[TEST] Expected: vibration at 10 s, rescue latch at 20 s"));
    return;
  }

  if (command == swimmer::SerialTestCommand::Continue) {
    if (!heartbeatSyncPaused) {
      Serial.println(F("[TEST] Heartbeat sync is already running"));
      return;
    }
    heartbeatSyncPaused = false;
    lastHeartbeatSentMs = now - kHeartbeatIntervalMs;
    lastServerMessageMs = now;
    Serial.println(F("[TEST] CONTINUE accepted: heartbeat sync resumed"));
    Serial.println(F("[TEST] Alarm clears after 3 matching heartbeat ACKs"));
    return;
  }

  Serial.println(F("[TEST] Unknown command; use stop or continue"));
}

void finishSerialCommand(uint32_t now) {
  if (serialCommandLength == 0 && !serialCommandOverflow) {
    return;
  }
  if (serialCommandOverflow) {
    Serial.println(F("[TEST] Command too long; use stop or continue"));
  } else {
    applySerialTestCommand(swimmer::parseSerialTestCommand(
                               serialCommandBuffer, serialCommandLength),
                           now);
  }
  serialCommandLength = 0;
  serialCommandOverflow = false;
}

void updateSerialTestControl() {
  while (Serial.available() > 0) {
    const char value = static_cast<char>(Serial.read());
    lastSerialCommandByteMs = millis();
    if (value == '\r' || value == '\n') {
      finishSerialCommand(lastSerialCommandByteMs);
    } else if (serialCommandLength < sizeof(serialCommandBuffer)) {
      serialCommandBuffer[serialCommandLength++] = value;
    } else {
      serialCommandOverflow = true;
    }
  }

  const uint32_t current = millis();
  if ((serialCommandLength > 0 || serialCommandOverflow) &&
      current - lastSerialCommandByteMs >= kSerialCommandIdleMs) {
    finishSerialCommand(current);
  }
}

void setVibrationOutput(bool enabled) {
  vibrationOutputOn = enabled;
  digitalWrite(kVibrationPin,
               enabled ? kVibrationActiveLevel : kVibrationInactiveLevel);
}

void stopVibration() {
  setVibrationOutput(false);
  lastVibrationChangeMs = millis();
}

void updateVibration(uint32_t now) {
  if (collisionPulse.active(now)) {
    if (!vibrationOutputOn) {
      setVibrationOutput(true);
    }
    return;
  }

  if (!safety.vibrationRequired()) {
    if (vibrationOutputOn) {
      stopVibration();
    }
    return;
  }

  const uint32_t interval =
      vibrationOutputOn ? kVibrationOnMs : kVibrationOffMs;
  if (now - lastVibrationChangeMs >= interval) {
    setVibrationOutput(!vibrationOutputOn);
    lastVibrationChangeMs = now;
  }
}

void drawLine(uint8_t y, const __FlashStringHelper *text) {
  oled.setCursor(0, y);
  oled.print(text);
}

void renderDisplay(uint32_t now) {
  if (!oledFound) {
    return;
  }

  oled.clearBuffer();
  oled.setFont(u8g2_font_wqy12_t_gb2312);
  oled.setFontMode(1);
  oled.enableUTF8Print();

  drawLine(12, F("泳者安全终端"));
  oled.drawHLine(0, 16, 128);

  if (safety.rescueLatched()) {
    drawLine(31, F("救生模块已触发"));
    drawLine(47, safety.vibrationRequired() ? F("通信仍异常，持续振动")
                                             : F("通信恢复，等待复位"));
    oled.setCursor(0, 63);
    oled.print(F("恢复确认: "));
    oled.print(safety.recoveryAckCount());
    oled.print(F("/3"));
    oled.sendBuffer();
    return;
  }

  if (safety.vibrationRequired()) {
    drawLine(31, F("疑似溺水"));
    oled.setCursor(0, 47);
    oled.print(F("失联: "));
    oled.print(safety.communicationLossMs(now) / 1000U);
    oled.print(F(" 秒"));
    oled.setCursor(0, 63);
    oled.print(F("恢复确认: "));
    oled.print(safety.recoveryAckCount());
    oled.print(F("/3"));
    oled.sendBuffer();
    return;
  }

  if (heartbeatPaused()) {
    drawLine(32, remoteSimulationPaused ? F("模拟：通信已停止")
                                        : F("测试：同步已停止"));
    oled.setCursor(0, 49);
    oled.print(F("失联计时: "));
    oled.print(safety.communicationLossMs(now) / 1000U);
    oled.print(F(" 秒"));
    drawLine(63, F("串口 continue 恢复"));
    oled.sendBuffer();
    return;
  }

  switch (communicationState) {
    case CommunicationState::Searching:
      drawLine(32, F("正在搜索主模块信号"));
      oled.setCursor(0, 51);
      oled.print(F("ESP-NOW 信道: "));
      oled.print(scanChannel);
      break;
    case CommunicationState::WifiConnecting:
      drawLine(34, F("正在连接局域网"));
      drawLine(54, F("凭据仅保存在内存"));
      break;
    case CommunicationState::ServerConnecting:
      drawLine(32, F("网络已连接"));
      drawLine(52, F("正在连接电脑"));
      break;
    case CommunicationState::Registered:
      drawLine(32, F("状态更新正常"));
      oled.setCursor(0, 52);
      oled.print(F("最近确认: "));
      oled.print(safety.communicationLossMs(now) / 1000U);
      oled.print(F(" 秒前"));
      break;
  }
  oled.sendBuffer();
}

void updateDisplay(uint32_t now) {
  if (now - lastDisplayRefreshMs >= kDisplayRefreshMs) {
    lastDisplayRefreshMs = now;
    renderDisplay(now);
  }
}

String nextMessageId(const char *prefix) {
  return String(prefix) + "-" + String(++messageSequence);
}

void sendJson(JsonDocument &document) {
  String payload;
  serializeJson(document, payload);
  webSocket.sendTXT(payload);
}

void sendAck(const char *messageId, bool accepted) {
  StaticJsonDocument<256> response;
  response["type"] = "ack";
  response["schema_version"] = kSchemaVersion;
  response["message_id"] = messageId;
  response["accepted"] = accepted;
  sendJson(response);
}

void sendDeviceHello() {
  helloMessageId = nextMessageId("hello");
  StaticJsonDocument<384> message;
  message["type"] = "device_hello";
  message["schema_version"] = kSchemaVersion;
  message["message_id"] = helloMessageId;
  message["device_id"] = deviceId;
  message["device_type"] = "swimmer";
  message["firmware_version"] = kFirmwareVersion;
  message["boot_id"] = bootId;
  sendJson(message);
}

bool formatUtcTime(char *buffer, size_t size) {
  const time_t now = time(nullptr);
  if (now < 1700000000) {
    return false;
  }
  struct tm utc {};
  gmtime_r(&now, &utc);
  return strftime(buffer, size, "%Y-%m-%dT%H:%M:%SZ", &utc) > 0;
}

void createHeartbeat(uint32_t now) {
  pendingHeartbeatId = nextMessageId("hb");
  StaticJsonDocument<512> message;
  message["type"] = "swimmer_heartbeat";
  message["schema_version"] = kSchemaVersion;
  message["message_id"] = pendingHeartbeatId;
  message["device_id"] = deviceId;
  message["uptime_ms"] = now;
  message["local_alert_stage"] =
      swimmer::alertStageName(safety.alertStage());
  message["communication_loss_ms"] = safety.communicationLossMs(now);

  char timestamp[32];
  if (formatUtcTime(timestamp, sizeof(timestamp))) {
    message["sent_at"] = timestamp;
  }
  pendingHeartbeatPayload = "";
  serializeJson(message, pendingHeartbeatPayload);
}

void sendPendingHeartbeat(uint32_t now) {
  if (pendingHeartbeatId.isEmpty()) {
    createHeartbeat(now);
  }
  webSocket.sendTXT(pendingHeartbeatPayload);
  lastHeartbeatSentMs = now;
}

void handleHeartbeatAck(const char *messageId, bool accepted,
                        uint32_t now) {
  const bool matches = !pendingHeartbeatId.isEmpty() &&
                       pendingHeartbeatId == messageId;
  safety.onHeartbeatAck(matches, accepted, now);
  if (matches) {
    pendingHeartbeatId = "";
    pendingHeartbeatPayload = "";
  }
}

void handleRescueReset(JsonDocument &document, uint32_t now) {
  const char *messageId = document["message_id"] | "";
  const char *targetDeviceId = document["target_device_id"] | "";
  if (messageId[0] == '\0' || targetDeviceId[0] == '\0' ||
      deviceId != targetDeviceId) {
    if (messageId[0] != '\0') {
      sendAck(messageId, false);
    }
    return;
  }

  if (lastAcceptedResetId == messageId) {
    sendAck(messageId, true);
    return;
  }

  const bool accepted = communicationState == CommunicationState::Registered &&
                        safety.resetRescueLatch(now);
  if (accepted) {
    lastAcceptedResetId = messageId;
    Serial.println(F("[SAFETY] Rescue latch reset by computer"));
  }
  sendAck(messageId, accepted);
}

void handleSwimmerControl(JsonDocument &document, uint32_t now) {
  const char *messageId = document["message_id"] | "";
  const char *targetDeviceId = document["target_device_id"] | "";
  if (messageId[0] == '\0' || targetDeviceId[0] == '\0' ||
      deviceId != targetDeviceId ||
      !document.containsKey("rescue_enabled") ||
      !document.containsKey("simulation_paused")) {
    if (messageId[0] != '\0') {
      sendAck(messageId, false);
    }
    return;
  }
  if (lastAcceptedControlId == messageId) {
    sendAck(messageId, true);
    return;
  }

  safety.setRescueEnabled(document["rescue_enabled"].as<bool>());
  if (document["trigger_rescue"] | false) {
    safety.forceRescue();
    Serial.println(F("[SAFETY] Rescue manually triggered by computer"));
  }
  if (document["collision_pulse"] | false) {
    collisionPulse.trigger(now);
    setVibrationOutput(true);
    lastVibrationChangeMs = now;
    Serial.println(F("[SAFETY] Collision warning pulse"));
  }

  const bool pauseRequested = document["simulation_paused"].as<bool>();
  remoteSimulationPaused = pauseRequested;
  pendingHeartbeatId = "";
  pendingHeartbeatPayload = "";
  if (!pauseRequested) {
    lastHeartbeatSentMs = now - kHeartbeatIntervalMs;
    lastServerMessageMs = now;
  }
  lastAcceptedControlId = messageId;
  sendAck(messageId, true);
  Serial.printf("[TEST] Remote communication simulation=%s rescue=%s\n",
                remoteSimulationPaused ? "paused" : "running",
                safety.rescueEnabled() ? "enabled" : "disabled");
}

void handleWebSocketText(uint8_t *payload, size_t length) {
  DynamicJsonDocument document(1536);
  if (deserializeJson(document, payload, length)) {
    return;
  }

  const uint32_t now = millis();
  lastServerMessageMs = now;
  const char *type = document["type"] | "";
  if (strcmp(type, "swimmer_control_command") == 0) {
    handleSwimmerControl(document, now);
    return;
  }
  if (strcmp(type, "ack") == 0) {
    const char *messageId = document["message_id"] | "";
    const bool accepted = document["accepted"] | false;
    if (!helloAccepted && helloMessageId == messageId) {
      if (!accepted) {
        Serial.println(F("[WS] Device registration rejected"));
        webSocket.disconnect();
        return;
      }
      helloAccepted = true;
      communicationState = CommunicationState::Registered;
      pendingHeartbeatId = "";
      pendingHeartbeatPayload = "";
      lastHeartbeatSentMs = now - kHeartbeatIntervalMs;
      if (!safety.monitoring()) {
        safety.beginMonitoring(now);
      }
      Serial.println(F("[WS] Swimmer registered; safety watchdog active"));
      return;
    }

    if (helloAccepted && !pendingHeartbeatId.isEmpty()) {
      handleHeartbeatAck(messageId, accepted, now);
    }
    return;
  }

  if (remoteSimulationPaused) {
    return;
  }

  if (strcmp(type, "rescue_reset_command") == 0) {
    handleRescueReset(document, now);
  }
}

void onWebSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      webSocketConnected = true;
      helloAccepted = false;
      reconnectIndex = 0;
      serverConnectStartedMs = 0;
      websocketConnectedAtMs = millis();
      lastServerMessageMs = millis();
      webSocket.setReconnectInterval(kReconnectDelaysMs[0]);
      communicationState = CommunicationState::ServerConnecting;
      sendDeviceHello();
      break;

    case WStype_DISCONNECTED:
      webSocketConnected = false;
      helloAccepted = false;
      pendingHeartbeatId = "";
      pendingHeartbeatPayload = "";
      if (WiFi.status() == WL_CONNECTED) {
        communicationState = CommunicationState::ServerConnecting;
        if (serverConnectStartedMs == 0) {
          serverConnectStartedMs = millis();
        }
        webSocket.setReconnectInterval(kReconnectDelaysMs[reconnectIndex]);
        if (reconnectIndex + 1 <
            sizeof(kReconnectDelaysMs) / sizeof(kReconnectDelaysMs[0])) {
          ++reconnectIndex;
        }
      }
      break;

    case WStype_TEXT:
      handleWebSocketText(payload, length);
      break;

    default:
      break;
  }
}

void onEspNowReceive(const uint8_t *, const uint8_t *data, int length) {
  if (length != static_cast<int>(sizeof(spp::ProvisionPacket))) {
    return;
  }

  spp::ProvisionPacket packet{};
  memcpy(&packet, data, sizeof(packet));
  if (!spp::validProvisionPacket(packet, static_cast<size_t>(length))) {
    return;
  }

  portENTER_CRITICAL(&provisionMux);
  pendingProvision = packet;
  provisionPending = true;
  portEXIT_CRITICAL(&provisionMux);
}

void stopEspNow() {
  if (espNowReady) {
    esp_now_deinit();
    espNowReady = false;
  }
}

bool setWifiChannel(uint8_t channel) {
  esp_wifi_set_promiscuous(true);
  const esp_err_t result =
      esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);
  return result == ESP_OK;
}

void startSearching() {
  if (webSocketStarted) {
    webSocket.disconnect();
    webSocketStarted = false;
  }
  webSocketConnected = false;
  helloAccepted = false;
  pendingHeartbeatId = "";
  pendingHeartbeatPayload = "";
  stopEspNow();

  WiFi.persistent(false);
  WiFi.disconnect(true, true);
  delay(50);
  WiFi.mode(WIFI_STA);
  delay(50);

  if (esp_now_init() == ESP_OK) {
    esp_now_register_recv_cb(onEspNowReceive);
    espNowReady = true;
  } else {
    Serial.println(F("[PROVISION] ESP-NOW initialization failed"));
  }

  currentSsid = "";
  currentPassword = "";
  serverHost = "";
  serverPort = 0;
  scanChannel = 1;
  setWifiChannel(scanChannel);
  lastChannelChangeMs = millis();
  serverConnectStartedMs = 0;
  communicationState = CommunicationState::Searching;
  Serial.println(F("[PROVISION] Searching SPP1 v2 on channels 1-13"));
}

bool takeProvisionPacket(spp::ProvisionPacket &packet) {
  bool ready = false;
  portENTER_CRITICAL(&provisionMux);
  if (provisionPending) {
    packet = pendingProvision;
    provisionPending = false;
    ready = true;
  }
  portEXIT_CRITICAL(&provisionMux);
  return ready;
}

void startWifiConnection(const spp::ProvisionPacket &packet) {
  stopEspNow();
  currentSsid = String(packet.ssid, packet.ssidLength);
  currentPassword = String(packet.password, packet.passwordLength);
  serverHost = String(packet.serverIpv4[0]) + "." +
               String(packet.serverIpv4[1]) + "." +
               String(packet.serverIpv4[2]) + "." +
               String(packet.serverIpv4[3]);
  serverPort = packet.serverPort;

  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.begin(currentSsid.c_str(), currentPassword.c_str());
  currentPassword = "";
  wifiConnectStartedMs = millis();
  communicationState = CommunicationState::WifiConnecting;
  Serial.printf("[WIFI] Connecting; server=%s:%u\n", serverHost.c_str(),
                serverPort);
}

void startServerConnection() {
  webSocket.disconnect();
  webSocket.begin(serverHost.c_str(), serverPort, "/ws/devices");
  webSocket.onEvent(onWebSocketEvent);
  webSocket.setReconnectInterval(kReconnectDelaysMs[0]);
  webSocket.enableHeartbeat(5000, 3000, 2);
  webSocketStarted = true;
  webSocketConnected = false;
  helloAccepted = false;
  reconnectIndex = 0;
  serverConnectStartedMs = millis();
  communicationState = CommunicationState::ServerConnecting;
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  Serial.printf("[WS] Connecting to ws://%s:%u/ws/devices\n",
                serverHost.c_str(), serverPort);
}

void updateSearching(uint32_t now) {
  spp::ProvisionPacket packet{};
  if (takeProvisionPacket(packet)) {
    Serial.printf("[PROVISION] Valid packet received on channel %u\n",
                  scanChannel);
    startWifiConnection(packet);
    return;
  }

  if (now - lastChannelChangeMs >= kChannelDwellMs) {
    scanChannel = scanChannel >= 13 ? 1 : scanChannel + 1;
    setWifiChannel(scanChannel);
    lastChannelChangeMs = now;
  }
}

void updateWifiAndServer(uint32_t now) {
  if (communicationState == CommunicationState::WifiConnecting) {
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("[WIFI] Connected; IP=%s RSSI=%d\n",
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
      startServerConnection();
    } else if (now - wifiConnectStartedMs >= kWifiConnectTimeoutMs) {
      Serial.println(F("[WIFI] Timeout; returning to signal search"));
      startSearching();
    }
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(F("[WIFI] Lost; discarding credentials and searching"));
    startSearching();
    return;
  }

  if (webSocketStarted) {
    webSocket.loop();
  }

  const uint32_t websocketNow = millis();
  if (webSocketConnected && !helloAccepted &&
      websocketNow - websocketConnectedAtMs >= kRegistrationTimeoutMs) {
    Serial.println(F("[WS] Registration timeout"));
    webSocket.disconnect();
    return;
  }

  if (!heartbeatPaused() && webSocketConnected && helloAccepted &&
      websocketNow - lastHeartbeatSentMs >= kHeartbeatIntervalMs) {
    sendPendingHeartbeat(websocketNow);
  }

  if (!heartbeatPaused() && webSocketConnected &&
      websocketNow - lastServerMessageMs >= kServerSilenceReconnectMs) {
    Serial.println(F("[WS] Server silent; reconnecting"));
    webSocket.disconnect();
    return;
  }

  if (!webSocketConnected && serverConnectStartedMs != 0 &&
      websocketNow - serverConnectStartedMs >= kServerSearchResetMs) {
    Serial.println(F("[WS] Computer unavailable; refreshing provision data"));
    startSearching();
  }
}

uint8_t findOledAddress() {
  const uint8_t candidates[] = {0x3C, 0x3D};
  for (uint8_t address : candidates) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      return address;
    }
  }
  return 0;
}

void initialiseIdentity() {
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  mac.toLowerCase();
  deviceId = "swimmer-band-" + mac;
  bootId = "boot-" + String(esp_random(), HEX);
}

void reportAlertTransition() {
  const swimmer::AlertStage stage = safety.alertStage();
  if (stage == lastReportedAlertStage) {
    return;
  }
  lastReportedAlertStage = stage;
  Serial.printf("[SAFETY] stage=%s loss_ms=%lu recovery=%u/3\n",
                swimmer::alertStageName(stage),
                static_cast<unsigned long>(
                    safety.communicationLossMs(millis())),
                safety.recoveryAckCount());
  if (stage == swimmer::AlertStage::RescueTriggered) {
    Serial.println(F("[RESCUE] Servo action simulated on OLED only"));
  }
}

}  // namespace

void setup() {
  pinMode(kVibrationPin, OUTPUT);
  stopVibration();

  Serial.begin(115200);
  const uint32_t serialWaitStartedAt = millis();
  while (!Serial && millis() - serialWaitStartedAt < 1500) {
    delay(10);
  }

  Wire.begin(kSdaPin, kSclPin);
  Wire.setClock(400000);
  const uint8_t oledAddress = findOledAddress();
  if (oledAddress != 0) {
    oled.setI2CAddress(oledAddress << 1);
    oledFound = oled.begin();
  }

  WiFi.mode(WIFI_STA);
  initialiseIdentity();
  Serial.printf("\n=== Swimmer safety terminal %s ===\n", kFirmwareVersion);
  Serial.printf("[DEVICE] id=%s boot=%s\n", deviceId.c_str(),
                bootId.c_str());
  Serial.printf("[HARDWARE] OLED=%s SDA=%u SCL=%u vibration=GPIO%u\n",
                oledFound ? "ready" : "missing", kSdaPin, kSclPin,
                kVibrationPin);
  Serial.println(F("[TEST] Serial commands: stop | continue"));
  startSearching();
  renderDisplay(millis());
}

void loop() {
  const uint32_t now = millis();
  updateSerialTestControl();
  if (communicationState == CommunicationState::Searching) {
    updateSearching(now);
  } else {
    updateWifiAndServer(now);
  }

  const uint32_t current = millis();
  safety.update(current);
  reportAlertTransition();
  updateVibration(current);
  updateDisplay(current);
  delay(5);
}
