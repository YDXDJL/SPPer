#include <Arduino.h>
#include <ArduinoJson.h>
#include <AudioFileSourcePROGMEM.h>
#include <AudioGeneratorMP3.h>
#include <AudioOutputI2S.h>
#include <U8g2lib.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <time.h>

#include <cstddef>

extern const uint8_t alarmMp3Start[]
    asm("_binary_assets_alarm_mp3_start");
extern const uint8_t alarmMp3End[]
    asm("_binary_assets_alarm_mp3_end");
extern const uint8_t lane1Mp3Start[]
    asm("_binary_assets_lane1_mp3_start");
extern const uint8_t lane1Mp3End[]
    asm("_binary_assets_lane1_mp3_end");
extern const uint8_t lane2Mp3Start[]
    asm("_binary_assets_lane2_mp3_start");
extern const uint8_t lane2Mp3End[]
    asm("_binary_assets_lane2_mp3_end");
extern const uint8_t bothMp3Start[]
    asm("_binary_assets_both_mp3_start");
extern const uint8_t bothMp3End[]
    asm("_binary_assets_both_mp3_end");

namespace {

constexpr char kFirmwareVersion[] = "0.3.1";
constexpr char kSchemaVersion[] = "1.0";

constexpr uint8_t kSdaPin = 41;
constexpr uint8_t kSclPin = 42;
constexpr uint8_t kSpeakerDoutPin = 7;
constexpr uint8_t kSpeakerBclkPin = 15;
constexpr uint8_t kSpeakerLrckPin = 16;
constexpr uint8_t kAmpEnablePin = 17;
constexpr uint8_t kOledAddress = 0x3C;

constexpr uint32_t kChannelDwellMs = 150;
constexpr uint32_t kWifiConnectTimeoutMs = 15000;
constexpr uint32_t kRegistrationTimeoutMs = 5000;
constexpr uint32_t kHeartbeatIntervalMs = 5000;
constexpr uint32_t kServerSilenceTimeoutMs = 15000;
constexpr uint32_t kServerSearchResetMs = 30000;
constexpr uint32_t kInterClipGapMs = 80;
constexpr uint32_t kDisplayRefreshMs = 250;

constexpr uint32_t kReconnectDelaysMs[] = {1000, 2000, 5000, 10000};

struct __attribute__((packed)) ProvisionPacketV1 {
  char magic[4];
  uint8_t version;
  uint32_t sequence;
  uint8_t ssidLength;
  uint8_t passwordLength;
  char ssid[32];
  char password[63];
  uint32_t crc32;
};

struct __attribute__((packed)) ProvisionPacketV2 {
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

static_assert(sizeof(ProvisionPacketV1) == 110,
              "SPP1 v1 packet must be 110 bytes");
static_assert(sizeof(ProvisionPacketV2) == 116,
              "SPP1 v2 packet must be 116 bytes");
static_assert(offsetof(ProvisionPacketV2, crc32) == 112,
              "SPP1 v2 CRC must start at byte 112");

enum class DeviceState : uint8_t {
  Searching,
  WifiConnecting,
  ServerConnecting,
  Syncing,
  Online,
};

enum class PoolState : uint8_t {
  Safe,
  Lane1Drowning,
  Lane2Drowning,
  BothDrowning,
};

U8G2_SSD1306_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);
WebSocketsClient webSocket;
AudioGeneratorMP3 audioDecoder;
AudioOutputI2S audioOutput(0, AudioOutputI2S::EXTERNAL_I2S);
AudioFileSourcePROGMEM *audioSource = nullptr;

portMUX_TYPE provisionMux = portMUX_INITIALIZER_UNLOCKED;
volatile bool provisionPending = false;
ProvisionPacketV2 pendingProvision{};

DeviceState deviceState = DeviceState::Searching;
PoolState poolState = PoolState::Safe;

bool oledFound = false;
bool espNowReady = false;
bool webSocketStarted = false;
bool webSocketConnected = false;
bool helloAccepted = false;
bool hasAppliedState = false;
bool dangerActive = false;
bool audioReady = false;

uint8_t scanChannel = 1;
uint8_t reconnectIndex = 0;
uint32_t lastChannelChangeMs = 0;
uint32_t wifiConnectStartedMs = 0;
uint32_t serverConnectStartedMs = 0;
uint32_t websocketConnectedAtMs = 0;
uint32_t lastHeartbeatMs = 0;
uint32_t lastServerMessageMs = 0;
uint32_t lastDisplayRefreshMs = 0;
uint32_t nextAudioStartMs = 0;
uint32_t messageSequence = 0;
uint32_t lastAppliedRevision = 0;
uint8_t alertSequenceStep = 0;
PoolState audioPoolState = PoolState::Safe;

String deviceId;
String bootId;
String currentSsid;
String currentPassword;
String serverHost;
uint16_t serverPort = 0;
String helloMessageId;
String serverInstanceId;

uint32_t calculateCrc32(const uint8_t *data, size_t length) {
  uint32_t crc = 0xFFFFFFFFUL;
  for (size_t index = 0; index < length; ++index) {
    crc ^= data[index];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ ((crc & 1U) ? 0xEDB88320UL : 0U);
    }
  }
  return crc ^ 0xFFFFFFFFUL;
}

bool validServerAddress(const uint8_t address[4], uint16_t port) {
  if (port == 0 || address[0] == 0 || address[0] == 127 ||
      address[0] >= 224) {
    return false;
  }
  return !(address[0] == 255 && address[1] == 255 &&
           address[2] == 255 && address[3] == 255);
}

bool validProvisionPacket(const ProvisionPacketV2 &packet) {
  if (memcmp(packet.magic, "SPP1", 4) != 0 || packet.version != 2) {
    return false;
  }
  if (packet.ssidLength == 0 || packet.ssidLength > 32 ||
      packet.passwordLength > 63 ||
      (packet.passwordLength != 0 && packet.passwordLength < 8)) {
    return false;
  }
  if (!validServerAddress(packet.serverIpv4, packet.serverPort)) {
    return false;
  }
  const uint32_t expected = calculateCrc32(
      reinterpret_cast<const uint8_t *>(&packet),
      offsetof(ProvisionPacketV2, crc32));
  return expected == packet.crc32;
}

void onEspNowReceive(const uint8_t *, const uint8_t *data, int length) {
  if (length != static_cast<int>(sizeof(ProvisionPacketV2))) {
    return;
  }

  ProvisionPacketV2 packet{};
  memcpy(&packet, data, sizeof(packet));
  if (!validProvisionPacket(packet)) {
    return;
  }

  portENTER_CRITICAL(&provisionMux);
  pendingProvision = packet;
  provisionPending = true;
  portEXIT_CRITICAL(&provisionMux);
}

struct EmbeddedAudio {
  const uint8_t *data;
  size_t size;
  const char *name;
};

EmbeddedAudio alarmClip() {
  return {alarmMp3Start,
          static_cast<size_t>(alarmMp3End - alarmMp3Start),
          "alarm"};
}

EmbeddedAudio voiceClip(PoolState state) {
  switch (state) {
    case PoolState::Lane1Drowning:
      return {lane1Mp3Start,
              static_cast<size_t>(lane1Mp3End - lane1Mp3Start),
              "lane1"};
    case PoolState::Lane2Drowning:
      return {lane2Mp3Start,
              static_cast<size_t>(lane2Mp3End - lane2Mp3Start),
              "lane2"};
    case PoolState::BothDrowning:
    case PoolState::Safe:
      return {bothMp3Start,
              static_cast<size_t>(bothMp3End - bothMp3Start),
              "both"};
  }
  return {bothMp3Start,
          static_cast<size_t>(bothMp3End - bothMp3Start),
          "both"};
}

void releaseAudioSource() {
  if (audioDecoder.isRunning()) {
    audioDecoder.stop();
  }
  if (audioSource != nullptr) {
    delete audioSource;
    audioSource = nullptr;
  }
}

void stopAlertAudio() {
  releaseAudioSource();
  digitalWrite(kAmpEnablePin, LOW);
  alertSequenceStep = 0;
  audioPoolState = PoolState::Safe;
  nextAudioStartMs = 0;
}

void restartAlertAudio(PoolState state) {
  releaseAudioSource();
  digitalWrite(kAmpEnablePin, LOW);
  alertSequenceStep = 0;
  audioPoolState = state;
  nextAudioStartMs = millis();
}

bool startAudioClip(const EmbeddedAudio &clip) {
  releaseAudioSource();
  audioSource = new AudioFileSourcePROGMEM(clip.data, clip.size);
  if (audioSource == nullptr) {
    Serial.println("[AUDIO] Unable to allocate audio source");
    return false;
  }

  digitalWrite(kAmpEnablePin, HIGH);
  delay(20);
  if (!audioDecoder.begin(audioSource, &audioOutput)) {
    Serial.printf("[AUDIO] Failed to start %s clip\n", clip.name);
    digitalWrite(kAmpEnablePin, LOW);
    releaseAudioSource();
    return false;
  }
  Serial.printf("[AUDIO] Playing %s clip (%u bytes)\n",
                clip.name,
                static_cast<unsigned>(clip.size));
  return true;
}

void initialiseAudio() {
  pinMode(kAmpEnablePin, OUTPUT);
  digitalWrite(kAmpEnablePin, LOW);
  audioOutput.SetPinout(
      kSpeakerBclkPin, kSpeakerLrckPin, kSpeakerDoutPin);
  audioOutput.SetGain(0.85f);
  audioReady = true;
  Serial.printf("[AUDIO] I2S DOUT=%u BCLK=%u LRCK=%u AMP_EN=%u\n",
                kSpeakerDoutPin,
                kSpeakerBclkPin,
                kSpeakerLrckPin,
                kAmpEnablePin);
}

void updateAlertAudio() {
  if (!dangerActive || !audioReady) {
    if (audioDecoder.isRunning() || audioSource != nullptr ||
        digitalRead(kAmpEnablePin) == HIGH) {
      stopAlertAudio();
    }
    return;
  }

  if (audioPoolState != poolState) {
    restartAlertAudio(poolState);
  }

  if (audioDecoder.isRunning()) {
    if (audioDecoder.loop()) {
      return;
    }
    releaseAudioSource();
    alertSequenceStep = (alertSequenceStep + 1) % 4;
    nextAudioStartMs = millis() + kInterClipGapMs;
    return;
  }

  if (audioSource != nullptr) {
    releaseAudioSource();
    alertSequenceStep = (alertSequenceStep + 1) % 4;
    nextAudioStartMs = millis() + kInterClipGapMs;
    return;
  }

  if (static_cast<int32_t>(millis() - nextAudioStartMs) < 0) {
    return;
  }

  const EmbeddedAudio clip =
      alertSequenceStep < 3 ? alarmClip() : voiceClip(poolState);
  if (!startAudioClip(clip)) {
    alertSequenceStep = (alertSequenceStep + 1) % 4;
    nextAudioStartMs = millis() + 500;
  }
}

void drawLine(uint8_t y, const __FlashStringHelper *text) {
  oled.setCursor(0, y);
  oled.print(text);
}

void drawPoolState(uint8_t y) {
  oled.setCursor(0, y);
  switch (poolState) {
    case PoolState::Safe:
      oled.print(F("泳池安全"));
      break;
    case PoolState::Lane1Drowning:
      oled.print(F("1号泳道溺水危险"));
      break;
    case PoolState::Lane2Drowning:
      oled.print(F("2号泳道溺水危险"));
      break;
    case PoolState::BothDrowning:
      oled.print(F("两泳道溺水危险"));
      break;
  }
}

void renderDisplay() {
  if (!oledFound) {
    return;
  }

  oled.clearBuffer();
  oled.setFont(u8g2_font_wqy12_t_gb2312);
  oled.setFontMode(1);
  oled.enableUTF8Print();

  if (!webSocketConnected && dangerActive) {
    drawLine(12, F("连接断开"));
    oled.drawHLine(0, 16, 128);
    drawLine(31, F("上次危险仍在报警"));
    drawPoolState(47);
    drawLine(63, F("正在重新连接"));
    oled.sendBuffer();
    return;
  }

  drawLine(12, F("救生员手环"));
  oled.drawHLine(0, 16, 128);

  if (deviceState == DeviceState::Searching) {
    drawLine(30, F("正在搜索主控信号"));
    oled.setCursor(0, 45);
    oled.print(F("当前频道："));
    oled.print(scanChannel);
    drawLine(60, F("等待配网广播"));
  } else if (deviceState == DeviceState::WifiConnecting) {
    drawLine(33, F("正在连接局域网"));
    drawLine(53, F("请稍候"));
  } else if (deviceState == DeviceState::ServerConnecting) {
    drawLine(30, F("网络：已连接"));
    drawLine(45, F("正在连接电脑"));
    if (dangerActive) {
      drawLine(60, F("上次危险仍在报警"));
    }
  } else if (deviceState == DeviceState::Syncing) {
    drawLine(30, F("网络：已连接"));
    drawLine(45, F("电脑：同步状态"));
    if (dangerActive) {
      drawLine(60, F("上次危险仍在报警"));
    }
  } else {
    drawLine(30, F("网络正常  电脑在线"));
    drawPoolState(52);
  }

  oled.sendBuffer();
}

void updateDisplay() {
  const uint32_t now = millis();
  if (now - lastDisplayRefreshMs < kDisplayRefreshMs) {
    return;
  }
  lastDisplayRefreshMs = now;
  renderDisplay();
}

String nextMessageId(const char *prefix) {
  return String(prefix) + "-" + String(++messageSequence);
}

void sendJson(JsonDocument &document) {
  String payload;
  serializeJson(document, payload);
  webSocket.sendTXT(payload);
}

void sendAck(const char *messageId, bool duplicate) {
  StaticJsonDocument<256> response;
  response["type"] = "ack";
  response["schema_version"] = kSchemaVersion;
  response["message_id"] = messageId;
  response["accepted"] = true;
  response["applied_revision"] = lastAppliedRevision;
  response["duplicate"] = duplicate;
  sendJson(response);
}

void sendDeviceHello() {
  helloMessageId = nextMessageId("hello");
  StaticJsonDocument<384> message;
  message["type"] = "device_hello";
  message["schema_version"] = kSchemaVersion;
  message["message_id"] = helloMessageId;
  message["device_id"] = deviceId;
  message["device_type"] = "lifeguard_band";
  message["firmware_version"] = kFirmwareVersion;
  message["boot_id"] = bootId;
  sendJson(message);
}

bool formatUtcTime(char *buffer, size_t size) {
  const time_t now = time(nullptr);
  if (now < 1700000000) {
    return false;
  }
  struct tm utc{};
  gmtime_r(&now, &utc);
  return strftime(buffer, size, "%Y-%m-%dT%H:%M:%SZ", &utc) > 0;
}

void sendHeartbeat() {
  StaticJsonDocument<384> message;
  message["type"] = "device_heartbeat";
  message["schema_version"] = kSchemaVersion;
  message["message_id"] = nextMessageId("heartbeat");
  message["device_id"] = deviceId;
  message["uptime_ms"] = millis();
  message["last_applied_revision"] = lastAppliedRevision;

  char timestamp[32];
  if (formatUtcTime(timestamp, sizeof(timestamp))) {
    message["sent_at"] = timestamp;
  }
  sendJson(message);
  lastHeartbeatMs = millis();
}

PoolState parsePoolState(const char *displayState) {
  if (strcmp(displayState, "lane1_drowning") == 0) {
    return PoolState::Lane1Drowning;
  }
  if (strcmp(displayState, "lane2_drowning") == 0) {
    return PoolState::Lane2Drowning;
  }
  if (strcmp(displayState, "both_drowning") == 0) {
    return PoolState::BothDrowning;
  }
  return PoolState::Safe;
}

bool knownPoolState(const char *displayState) {
  return strcmp(displayState, "safe") == 0 ||
         strcmp(displayState, "lane1_drowning") == 0 ||
         strcmp(displayState, "lane2_drowning") == 0 ||
         strcmp(displayState, "both_drowning") == 0;
}

void handleLifeguardState(JsonDocument &document) {
  const char *messageId = document["message_id"] | "";
  const char *targetDeviceId = document["target_device_id"] | "";
  const char *incomingServerInstance =
      document["server_instance_id"] | "";
  const char *displayState = document["display_state"] | "";
  const uint32_t revision = document["state_revision"] | 0;

  if (messageId[0] == '\0' || incomingServerInstance[0] == '\0' ||
      !knownPoolState(displayState) ||
      !document.containsKey("state_revision") ||
      (targetDeviceId[0] != '\0' && deviceId != targetDeviceId)) {
    return;
  }

  const bool newServer = serverInstanceId != incomingServerInstance;
  if (newServer) {
    serverInstanceId = incomingServerInstance;
    lastAppliedRevision = 0;
    hasAppliedState = false;
  }

  const bool duplicate = hasAppliedState && !newServer &&
                         revision <= lastAppliedRevision;
  if (!duplicate) {
    const PoolState previousPoolState = poolState;
    const bool wasDangerActive = dangerActive;
    PoolState incomingPoolState = parsePoolState(displayState);
    const bool requestedAlert =
        document["vibration"]["enabled"] | false;

    if (requestedAlert && incomingPoolState == PoolState::Safe) {
      incomingPoolState = PoolState::BothDrowning;
    }

    poolState = incomingPoolState;
    dangerActive =
        requestedAlert || incomingPoolState != PoolState::Safe;
    lastAppliedRevision = revision;
    if (!dangerActive) {
      stopAlertAudio();
    } else if (!wasDangerActive || previousPoolState != poolState) {
      restartAlertAudio(poolState);
    }
    hasAppliedState = true;
  }

  deviceState = DeviceState::Online;
  sendAck(messageId, duplicate);
}

void handleWebSocketText(uint8_t *payload, size_t length) {
  DynamicJsonDocument document(1536);
  const DeserializationError error =
      deserializeJson(document, payload, length);
  if (error) {
    return;
  }

  lastServerMessageMs = millis();
  const char *type = document["type"] | "";
  if (strcmp(type, "ack") == 0) {
    const char *messageId = document["message_id"] | "";
    if (helloMessageId == messageId &&
        (document["accepted"] | false)) {
      helloAccepted = true;
      const char *instance = document["server_instance_id"] | "";
      if (instance[0] != '\0' && serverInstanceId != instance) {
        serverInstanceId = instance;
        lastAppliedRevision = 0;
        hasAppliedState = false;
      }
      deviceState = DeviceState::Syncing;
    }
    return;
  }

  if (strcmp(type, "lifeguard_state") == 0) {
    handleLifeguardState(document);
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
      deviceState = DeviceState::Syncing;
      sendDeviceHello();
      break;

    case WStype_DISCONNECTED:
      webSocketConnected = false;
      helloAccepted = false;
      if (WiFi.status() == WL_CONNECTED) {
        deviceState = DeviceState::ServerConnecting;
        if (serverConnectStartedMs == 0) {
          serverConnectStartedMs = millis();
        }
        const uint32_t delayMs = kReconnectDelaysMs[reconnectIndex];
        webSocket.setReconnectInterval(delayMs);
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
  stopEspNow();

  WiFi.persistent(false);
  WiFi.disconnect(true, true);
  delay(50);
  WiFi.mode(WIFI_STA);
  delay(50);

  if (esp_now_init() == ESP_OK) {
    esp_now_register_recv_cb(onEspNowReceive);
    espNowReady = true;
  }

  currentSsid = "";
  currentPassword = "";
  serverHost = "";
  serverPort = 0;
  scanChannel = 1;
  setWifiChannel(scanChannel);
  lastChannelChangeMs = millis();
  deviceState = DeviceState::Searching;
  Serial.println("[PROVISION] Searching SPP1 v2 on channels 1-13");
}

bool takeProvisionPacket(ProvisionPacketV2 &packet) {
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

void startWifiConnection(const ProvisionPacketV2 &packet) {
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
  wifiConnectStartedMs = millis();
  deviceState = DeviceState::WifiConnecting;
  Serial.printf("[WIFI] Connecting to provisioned network; server=%s:%u\n",
                serverHost.c_str(), serverPort);
}

void startServerConnection() {
  webSocket.disconnect();
  webSocket.begin(serverHost.c_str(), serverPort, "/ws/devices");
  webSocket.onEvent(onWebSocketEvent);
  webSocket.setReconnectInterval(kReconnectDelaysMs[0]);
  webSocketStarted = true;
  webSocketConnected = false;
  helloAccepted = false;
  reconnectIndex = 0;
  serverConnectStartedMs = millis();
  deviceState = DeviceState::ServerConnecting;
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  currentPassword = "";
  Serial.printf("[WS] Connecting to ws://%s:%u/ws/devices\n",
                serverHost.c_str(), serverPort);
}

void updateSearching() {
  ProvisionPacketV2 packet{};
  if (takeProvisionPacket(packet)) {
    Serial.printf("[PROVISION] Valid v2 packet received on channel %u\n",
                  scanChannel);
    startWifiConnection(packet);
    return;
  }

  const uint32_t now = millis();
  if (now - lastChannelChangeMs >= kChannelDwellMs) {
    scanChannel = scanChannel >= 13 ? 1 : scanChannel + 1;
    setWifiChannel(scanChannel);
    lastChannelChangeMs = now;
  }
}

void updateWifiAndServer() {
  const uint32_t now = millis();

  if (deviceState == DeviceState::WifiConnecting) {
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("[WIFI] Connected; IP=%s RSSI=%d\n",
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
      startServerConnection();
    } else if (now - wifiConnectStartedMs >= kWifiConnectTimeoutMs) {
      Serial.println("[WIFI] Connect timeout; returning to provision search");
      startSearching();
    }
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WIFI] Connection lost; returning to provision search");
    startSearching();
    return;
  }

  if (webSocketStarted) {
    webSocket.loop();
  }

  // webSocket.loop() can invoke the connection callback and set its timestamp
  // a few milliseconds after `now` was captured above.  Re-read millis here;
  // otherwise unsigned subtraction underflows and causes an immediate,
  // repeated registration timeout on a successful connection.
  const uint32_t websocketNow = millis();
  if (webSocketConnected && !helloAccepted &&
      websocketNow - websocketConnectedAtMs >= kRegistrationTimeoutMs) {
    Serial.println("[WS] Registration timeout");
    webSocket.disconnect();
    return;
  }

  if (webSocketConnected && helloAccepted &&
      websocketNow - lastHeartbeatMs >= kHeartbeatIntervalMs) {
    sendHeartbeat();
  }

  if (webSocketConnected &&
      websocketNow - lastServerMessageMs >= kServerSilenceTimeoutMs) {
    Serial.println("[WS] Server silence timeout");
    webSocket.disconnect();
    return;
  }

  if (!webSocketConnected && serverConnectStartedMs != 0 &&
      now - serverConnectStartedMs >= kServerSearchResetMs) {
    Serial.println("[WS] Server unavailable; refreshing provision data");
    startSearching();
  }
}

uint8_t findOledAddress() {
  Wire.beginTransmission(kOledAddress);
  return Wire.endTransmission() == 0 ? kOledAddress : 0;
}

void initialiseIdentity() {
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  mac.toLowerCase();
  deviceId = "lifeguard-band-" + mac;
  bootId = "boot-" + String(esp_random(), HEX);
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(500);

  initialiseAudio();
  Wire.begin(kSdaPin, kSclPin, 400000);
  const uint8_t oledAddress = findOledAddress();
  if (oledAddress != 0) {
    oled.setI2CAddress(oledAddress << 1);
    oled.begin();
    oledFound = true;
  }

  WiFi.mode(WIFI_STA);
  initialiseIdentity();
  Serial.printf("\n=== Lifeguard band %s ===\n", kFirmwareVersion);
  Serial.printf("[DEVICE] id=%s boot=%s\n", deviceId.c_str(),
                bootId.c_str());
  startSearching();
  renderDisplay();
}

void loop() {
  if (deviceState == DeviceState::Searching) {
    updateSearching();
  } else {
    updateWifiAndServer();
  }

  updateAlertAudio();
  updateDisplay();
  delay(1);
}
