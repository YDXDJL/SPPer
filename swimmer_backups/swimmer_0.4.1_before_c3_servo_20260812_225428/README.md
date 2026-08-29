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
   OLED 显示“疑似溺水”；允许自动救生时，达到 20 秒后锁存“救生模块已触发”。
5. 报警后连续三个正确心跳 ACK 才停止振动。救生锁存不会因重连或恢复自动清除，
   只有通信稳定后收到电脑的 `rescue_reset_command` 才能复位。

电脑可通过可靠的 `swimmer_control_command` 同步四个运行参数：是否允许自动进入
救生状态、是否暂停业务通信模拟失联、是否立即触发一次救生测试，以及是否执行
一次碰撞短振提醒。暂停期间
设备保持 WebSocket 控制通道，仅处理后续控制命令，不发送业务心跳，也不处理其他
业务消息；恢复后立即重新开始心跳。关闭自动救生只禁止 20 秒锁存，不影响 10 秒
疑似溺水振动；一键触发命令始终可以显式进入救生状态。碰撞提醒只让振动电机
连续开启约 250 ms，不改变通信计时或救生状态；同一控制消息重试不会重复起振。

心跳会上报 `uptime_ms`、`local_alert_stage` 和 `communication_loss_ms`；没有可信
UTC 时间时会省略 `sent_at`。固件不保存或打印 Wi-Fi 密码。

Wi-Fi 加入失败或掉线会立即丢弃凭据并重新搜索。WebSocket 按 1、2、5、最多
10 秒退避重连，连续 30 秒无法连接电脑后重新获取广播端点。网络状态机与安全
状态机相互独立，重连不会重置失联计时或救生锁存。

## 串口失联测试

打开 COM8 的 115200 波特率串口后发送以下命令（大小写不敏感，可带或不带换行）：

- `stop`：暂停业务心跳同步，但保持 Wi-Fi 和 WebSocket 连接。约 10 秒后设备振动并
  显示“疑似溺水”，约 20 秒后锁存“救生模块已触发”；电脑端也会独立判定失联。
- `continue`：立即恢复业务心跳。设备和电脑收到连续 3 次有效心跳 ACK 后解除当前
  通信报警并停止振动；已经锁存的救生状态仍需在电脑开发页点击救生复位。

该功能仅用于联调，不会伪造安全状态；它通过真实暂停 `swimmer_heartbeat` 来测试
完整报警链路。重复发送命令是安全的，串口会返回当前测试模式提示。

## 构建与测试

```powershell
cd D:\Vibecoding_Projects\SPPer\swimmer
pio test -e native
pio run -e esp32-s3-devkitc-1
pio run -e esp32-s3-devkitc-1 -t upload
pio device monitor -p COM8 -b 115200
```

`native` 测试覆盖 SPP1 v2 固定测试向量、非法包，以及 10/20 秒边界、错误 ACK、
三次恢复和救生复位规则。COM8 是当前泳者端上传口；烧录前应确认没有选中 RGB
网关当前所在的串口。
