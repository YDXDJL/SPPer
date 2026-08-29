# ESP32 双泳道 RGB 硬件网关

本固件驱动两条各 70 颗 WS2812B 灯带，通过 USB 串口接收电脑生成的完整 RGB 帧，并通过 ESP-NOW 向手环等设备广播局域网与电脑服务端点。

## 硬件

| 泳道 | 数据引脚 | 灯珠编号方向 |
| --- | --- | --- |
| 泳道 1 | GPIO26 | 画面左侧 1 → 右侧 70 |
| 泳道 2 | GPIO17 | 画面左侧 1 → 右侧 70 |

ESP32、两条灯带和外部 5V 灯带电源必须共地。灯带应使用独立 5V 电源，不要由 ESP32 开发板直接为整条灯带供电。默认亮度为 32/255。

每次上电或重启时，两条灯带会先执行约 2.3 秒的视觉自检：整条依次显示红、绿、蓝，
随后白色光点从第 1 颗扫到第 70 颗，最后全部熄灭。自检结束后才进入 Wi-Fi、
ESP-NOW 和电脑串口控制流程。

## 串口接口

- 端口：电脑逐个探测可用串口并通过 `gateway_probe` 自动识别，也可在开发页手动选择
- 波特率：`115200`
- 格式：UTF-8 NDJSON，每条 JSON 以换行结束

`gateway_ready` 和 `wifi_config` 中的 `server_ipv4`、`server_port` 均为成对可选字段。端点未知时，电脑可先发送不含端点的 `gateway_ready`，网关仍接受握手并允许灯光帧与配网；ACK 返回 `endpoint_valid=false`，本次会话不会使用 NVS 中的旧端点，也不进行 ESP-NOW 广播。网关入网并上报自身 IP 后，电脑再次发送带端点的 `gateway_ready`，网关保存端点并立即开始广播。IPv4 必须为可用的单播地址，端口范围为 1～65535；只给出其中一个字段会被拒绝。

```json
{"type":"gateway_ready","schema_version":"1.0","watchdog_ms":1500,"server_ipv4":"192.168.1.3","server_port":8000}
```

```json
{"type":"wifi_config","schema_version":"1.0","request_id":"wifi-0001","ssid":"Pool-Safety","password":"example-password","server_ipv4":"192.168.1.3","server_port":8000}
```

端点保存到 NVS；每次 `gateway_ready_ack`、`wifi_status`、`wifi_result` 和 `gateway_status` 都会回传 `endpoint_valid`，有效时同时回传 `server_ipv4`、`server_port`。固件不会在日志或状态消息中回显 Wi-Fi 密码。

电脑端完成握手后每 200 ms 发送一条 `rgb_frame`。若 ESP32 连续 1500 ms 没有收到合法灯光帧，会自动熄灭两条灯带。

## SPP1 v2 ESP-NOW 广播

只有 Wi-Fi 已连接、电脑端点有效且 ESP-NOW 已初始化时才会广播。连接成功或端点变化会立即启动一次突发；此后每 10 秒启动一次。每次突发持续 2.2 秒，每 100 ms 发送一个包。

广播包固定为 116 字节：

| 偏移 | 长度 | 字段 |
| ---: | ---: | --- |
| 0 | 4 | `magic = "SPP1"` |
| 4 | 1 | `version = 2` |
| 5 | 4 | `sequence`，小端 |
| 9 | 1 | `ssid_length` |
| 10 | 1 | `password_length` |
| 11 | 32 | `ssid`，未用字节补零 |
| 43 | 63 | `password`，未用字节补零 |
| 106 | 4 | `server_ipv4`，网络字节序（A、B、C、D） |
| 110 | 2 | `server_port`，小端 |
| 112 | 4 | `crc32`，小端 |

CRC32 使用多项式 `0xEDB88320`，初值和末尾异或均为 `0xFFFFFFFF`，覆盖前 112 字节。

## 人工诊断命令

```text
SET <泳道> <灯珠编号> <R> <G> <B>
OFF <泳道> <灯珠编号>
CLEAR <泳道|ALL>
FILL <泳道> <R> <G> <B>
BRIGHTNESS <0-255>
TEST [测试灯珠数量]
STATUS
HELP
```

## 构建与协议验证

以下命令不会访问串口，也不会烧录设备：

```powershell
platformio run -e esp32dev
platformio test -e native
```

本机测试固定检查 116 字节布局、端点字节序、CRC 黄金向量和负向篡改检测。只有显式运行 `platformio run -e esp32dev -t upload` 才会烧录设备。PlatformIO 默认自动选择端口，也可追加 `--upload-port COMx` 指定端口。
