# 泳者安全终端固件

适用于 ESP32-S3 DevKitC-1。设备每次开机先搜索 `main` 网关的 SPP1 v2
ESP-NOW 广播，不读取旧 Wi-Fi 凭据；获得有效凭据与电脑端点后连接
`ws://<server>:<port>/ws/devices`。

## 接线

| 模块 | 模块引脚 | ESP32-S3 |
| --- | --- | --- |
| SSD1306 OLED | VCC / GND | 3V3 / GND |
| SSD1306 OLED | SDA / SCL | GPIO15 / GPIO16 |
| 震动模块 | VCC / GND | 按模块额定电压 / GND |
| 震动模块 | IN | GPIO17，高电平有效 |

OLED 自动探测 I²C 地址 `0x3C` 和 `0x3D`。本版本不占用舵机引脚；20 秒救生
动作只通过 OLED 和串口模拟。

## 工作流程

1. 以约 150 ms 驻留时间循环扫描信道 1～13，严格校验 SPP1 v2 的长度、字段、
   电脑端点和 CRC32。
2. 加入广播指定的 Wi-Fi，并连接电脑 `/ws/devices`，发送
   `device_type=swimmer` 的 `device_hello`。
3. 注册 ACK 成功后，每秒发送一条 `swimmer_heartbeat`。未收到 ACK 时每秒重发
   完全相同的消息；只有 `message_id` 匹配且 `accepted=true` 才刷新设备端计时。
4. 距离最后一次成功状态更新达到 10 秒时，以 200 ms 开、100 ms 关的节奏振动，
   OLED 显示“疑似溺水”；达到 20 秒时锁存“救生模块已触发”。
5. 报警后连续三个正确心跳 ACK 才停止振动。救生锁存不会因重连或恢复自动清除，
   只有通信稳定后收到电脑的 `rescue_reset_command` 才能复位。

心跳会上报 `uptime_ms`、`local_alert_stage` 和 `communication_loss_ms`；没有可信
UTC 时间时会省略 `sent_at`。固件不保存或打印 Wi-Fi 密码。

Wi-Fi 加入失败或掉线会立即丢弃凭据并重新搜索。WebSocket 按 1、2、5、最多
10 秒退避重连，连续 30 秒无法连接电脑后重新获取广播端点。网络状态机与安全
状态机相互独立，重连不会重置失联计时或救生锁存。

## 构建与测试

```powershell
cd D:\Vibecoding_Projects\SPPer\swimmer
pio test -e native
pio run -e esp32-s3-devkitc-1
pio run -e esp32-s3-devkitc-1 -t upload
pio device monitor -p COM8 -b 115200
```

`native` 测试覆盖 SPP1 v2 固定测试向量、非法包，以及 10/20 秒边界、错误 ACK、
三次恢复和救生复位规则。COM8 是当前泳者端上传口；不要上传到 RGB 网关 COM6。
