# 冠影守望者数据接入规范

文档版本：`1.2`  
状态：电脑端视觉输入、网页订阅、开发测试、泳池区域标定、RGB 网关串口协议、
硬件设备连接和救生员手环协议已确定。

本规范是视觉识别、ESP32 泳者端、灯带网关和救生员手环后续接入电脑端的
统一依据。字段名、枚举值、坐标系和消息顺序不得由各模块自行修改。

## 1. 通用规则

- 编码：JSON 文本统一使用 UTF-8。
- 时间：使用 ISO 8601 UTC 时间，例如 `2026-07-31T08:30:15.123Z`。
- 版本：每条业务消息必须携带 `schema_version`，当前为 `"1.0"`。
- 标识符：设备和泳者 ID 为不区分格式的字符串，接收端不得假设其为数字。
- 坐标：`x` 向右增大、`y` 向下增大，左上角为 `(0, 0)`，右下角为 `(1, 1)`。
- 状态：`normal`、`collision`、`drowning`；显示优先级为溺水、碰撞、正常。
- WebSocket 断线后由客户端以 1 秒、2 秒、5 秒、最多 10 秒间隔自动重连。
- 发送方只有收到相同 `frame_id` 或 `message_id` 的 ACK，才能认为消息已被接收。
- USB 串口业务消息使用 UTF-8 NDJSON：每条 JSON 独占一行并以 `\n` 结束。
- 涉及 Wi-Fi 密码的消息不得写入电脑日志、状态快照或普通错误详情。

## 2. 视觉模块接入

### 2.1 连接

视觉进程作为 WebSocket 客户端连接：

```text
ws://<电脑IP>:8000/ws/vision
```

每一帧必须严格发送两条连续的 WebSocket 消息：

1. JSON 文本元数据；
2. 与该元数据对应的 JPEG 二进制画面。

同一连接内不得交错发送不同帧。JPEG 单帧上限为 8 MiB。建议画面使用
1280×720、JPEG 质量 70～85，发送频率为 5～15 FPS。

### 2.2 每帧 JSON

```json
{
  "type": "vision_frame",
  "schema_version": "1.0",
  "frame_id": "camera-top-00000125",
  "captured_at": "2026-07-31T08:30:15.123Z",
  "image_width": 1280,
  "image_height": 720,
  "tracks": [
    {
      "track_id": 7,
      "class_id": 0,
      "class_name": "person",
      "confidence": 0.91,
      "bbox_xyxy": [300.0, 210.0, 420.0, 510.0],
      "center_xy": [360.0, 360.0],
      "center_normalized": [0.28125, 0.5]
    }
  ]
}
```

字段约束：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `frame_id` | string | 当前视觉进程内唯一，最长 128 字符 |
| `captured_at` | datetime | 必须包含时区 |
| `image_width` / `image_height` | integer | 与随后 JPEG 的尺寸一致 |
| `track_id` | string 或 integer | 同一目标连续帧中保持稳定 |
| `confidence` | number | `0.0～1.0` |
| `bbox_xyxy` | number[4] | 原图像素坐标，顺序为左上和右下 |
| `center_xy` | number[2] | 原图像素中心点 |
| `center_normalized` | number[2] | 归一化中心点，推荐下游定位使用 |

画面可以是原始帧，网页会根据 `bbox_xyxy` 绘制识别框；视觉模块无需把框预先
画进 JPEG。

### 2.3 后台确认

成功：

```json
{
  "type": "vision_ack",
  "schema_version": "1.0",
  "frame_id": "camera-top-00000125"
}
```

失败：

```json
{
  "type": "error",
  "code": "INVALID_METADATA",
  "message": "字段校验错误详情"
}
```

错误码：

| 错误码 | 含义 |
| --- | --- |
| `EXPECTED_METADATA` | 当前应发送 JSON，但收到二进制 |
| `INVALID_METADATA` | JSON 格式或字段不符合规范 |
| `EXPECTED_JPEG` | 元数据后没有紧跟 JPEG |
| `INVALID_JPEG` | 二进制内容没有 JPEG 文件头 |

## 3. 网页实时订阅

监控页和开发测试页连接：

```text
ws://<电脑IP>:8000/ws/dashboard
```

后台向网页发送：

- `state` JSON：泳者列表、在线状态和统计数据；
- `vision_frame` JSON：最新识别参数；
- JPEG 二进制：紧跟在对应的 `vision_frame` 后。

`state` 示例：

```json
{
  "type": "state",
  "schema_version": "1.0",
  "revision": 18,
  "vision_connected": true,
  "swimmers": [
    {
      "id": "vision-7",
      "display_name": "视觉泳者 7",
      "source": "vision",
      "position": {"x": 0.28125, "y": 0.5},
      "status": "normal",
      "collision_with": null,
      "online": true,
      "last_seen_at": "2026-07-31T08:30:15.123Z",
      "confidence": 0.91,
      "bbox_xyxy": [300.0, 210.0, 420.0, 510.0]
    }
  ],
  "stats": {
    "total": 1,
    "online": 1,
    "normal": 1,
    "drowning": 0,
    "collision": 0
  }
}
```

`revision` 每次状态变化递增。订阅方收到旧版本时应丢弃旧消息。

## 4. 开发测试 REST 接口

这些接口只用于电脑端联调，不提供给正式 ESP32 设备。

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| `GET` | `/api/state` | 获取当前完整状态 |
| `POST` | `/api/dev/swimmers` | 新增虚拟泳者 |
| `PATCH` | `/api/dev/swimmers/{id}` | 修改虚拟泳者位置或状态 |
| `DELETE` | `/api/dev/swimmers/{id}` | 删除虚拟泳者 |
| `POST` | `/api/dev/collisions` | 设置或解除两名泳者的碰撞状态 |
| `POST` | `/api/dev/reset` | 清除全部虚拟泳者 |

新增泳者：

```json
{
  "display_name": "测试泳者 A",
  "position": {"x": 0.2, "y": 0.4}
}
```

修改位置或溺水状态：

```json
{
  "position": {"x": 0.65, "y": 0.3},
  "status": "drowning"
}
```

设置碰撞：

```json
{
  "swimmer_a": "virtual-a1b2c3d4",
  "swimmer_b": "virtual-e5f6g7h8",
  "active": true
}
```

## 5. 可穿戴设备 WebSocket 规范

电脑端已开放 `/ws/devices`，当前支持救生员手环和泳者设备。连接只接受回环、
私有或链路本地地址；所有业务消息的 `schema_version` 固定为 `1.0`。

设备连接地址：

```text
ws://<电脑IP>:8000/ws/devices
```

设备注册：

```json
{
  "type": "device_hello",
  "schema_version": "1.0",
  "message_id": "hello-0001",
  "device_id": "swimmer-band-a1b2",
  "device_type": "swimmer",
  "firmware_version": "0.1.0",
  "boot_id": "boot-7e91"
}
```

`device_type` 可取：

- `swimmer`：泳者端；
- `lifeguard_band`：救生员手环。

RGB 灯带网关使用第 6 节的自动探测/115200 NDJSON 串口协议，不连接此 WebSocket。

泳者心跳：

```json
{
  "type": "swimmer_heartbeat",
  "schema_version": "1.0",
  "message_id": "hb-000125",
  "device_id": "swimmer-band-a1b2",
  "uptime_ms": 125000,
  "sent_at": "2026-07-31T08:30:15.123Z",
  "local_alert_stage": "normal",
  "communication_loss_ms": 0
}
```

泳者端每 1 秒发送一次心跳。`sent_at` 仅在设备已获得可信 UTC 时间时发送，电脑
始终以消息接收时间进行超时判断。`local_alert_stage` 只能取 `normal`、
`suspected_drowning` 或 `rescue_triggered`。

电脑独立维护每台泳者设备的通信安全状态：

- 连续 10 秒未收到有效心跳：`suspected_drowning`；
- 连续 20 秒未收到有效心跳：`rescue_triggered`，并锁存电脑侧
  `rescue_expected = true`；
- 连接断开或重新注册都不得自动清除危险；
- 报警后必须收到 3 个连续且 `message_id` 各不相同的有效心跳才恢复
  `normal`。重复心跳仍逐条确认并刷新接收时间，但不增加恢复计数；
- 通信恢复只解除电脑侧的泳者状态覆盖，不自动清除泳者设备的救生锁存。

电脑向泳者设备下发可靠救生复位命令：

```json
{
  "type": "rescue_reset_command",
  "schema_version": "1.0",
  "message_id": "reset-0008",
  "target_device_id": "swimmer-band-a1b2"
}
```

同一命令按 500 ms、1 s、2 s 重试，所有重试保持相同 `message_id`。只有设备
已连接、电脑通信状态已恢复 `normal`、已累计 3 个恢复心跳、电脑已锁存救生且
设备以 `local_alert_stage = rescue_triggered` 确认锁存时才允许发送。设备使用
通用 `ack` 确认；`accepted = true` 后电脑清除本次救生锁存。

通用确认：

```json
{
  "type": "ack",
  "schema_version": "1.0",
  "message_id": "reset-0008",
  "accepted": true
}
```

电脑接受 `device_hello` 后，可在确认中增加当前会话参数：

```json
{
  "type": "ack",
  "schema_version": "1.0",
  "message_id": "hello-0001",
  "accepted": true,
  "session_id": "session-52ac",
  "server_instance_id": "server-14e8",
  "heartbeat_interval_ms": 1000
}
```

`heartbeat_interval_ms` 对 `swimmer` 为 1000，对 `lifeguard_band` 为 5000。
泳者注册后电脑不发送 `lifeguard_state`；救生员手环注册后则必须立即接收下述
完整状态快照。

### 5.1 电脑端绑定与复位 REST API

绑定只保存在当前电脑进程内，服务重启后清空；一个泳者设备只能绑定一个视觉或
虚拟泳者，同一泳者也只能绑定一台泳者设备：

```text
PUT    /api/devices/{device_id}/binding   {"swimmer_id":"virtual-a1b2","rescue_enabled":true}
DELETE /api/devices/{device_id}/binding
POST   /api/devices/{device_id}/rescue-trigger
POST   /api/devices/{device_id}/rescue-reset
GET    /api/devices
```

`rescue_enabled=false` 时，泳者端在失联 10 秒后仍振动并上报疑似溺水，但不会在
20 秒时自动锁存救生状态。`rescue-trigger` 是显式测试操作，不受该开关限制。
配对、开关、一键触发和复位接口均只允许 localhost。

电脑使用同一条可靠控制消息同步泳者端配置和联调状态：

```json
{
  "type": "swimmer_control_command",
  "schema_version": "1.0",
  "message_id": "control-0008",
  "target_device_id": "swimmer-band-a1b2",
  "rescue_enabled": false,
  "simulation_paused": true,
  "trigger_rescue": false,
  "collision_pulse": false
}
```

该消息按 500 ms、1 s、2 s 重试，重试保持相同 `message_id`。设备应用后使用通用
`ack` 确认。`simulation_paused=true` 时，设备先确认控制消息，再停止业务心跳并
忽略除后续控制消息外的业务下行；`false` 时立即恢复每秒心跳。

开发页点击“模拟下沉”时，不直接把泳者写成 `drowning`，而是先要求已配对且在线的
泳者端停止业务通信。电脑继续按正常心跳规则计时：10 秒未收到心跳后才判定疑似
溺水并驱动 RGB 与救生员手环，20 秒后再按 `rescue_enabled` 决定是否进入救生状态。
虚拟泳者恢复 `normal`、被删除、解除配对或清空虚拟泳者时，电脑请求恢复通信；
恢复后的三个不同有效心跳用于解除报警。

设置两名泳者碰撞时，电脑向与这两名泳者配对的在线泳者端发送
`collision_pulse=true`。泳者端只短振约 250 ms；同一设备 2 秒内的重复碰撞通知会
被抑制，避免持续振动。碰撞仍不触发救生员手环。

前三个修改接口仅允许 localhost。绑定设备进入通信报警时，电脑将目标泳者临时
覆盖为 `drowning`，并同时影响监控统计、救生员状态和 RGB 灯带；报警恢复后还原
目标原始状态。设备未绑定、绑定目标不存在或目标离线时，不猜测泳道，计入
`lifeguard_state.unlocated_count` 并按两条泳道均危险处理。

`GET /api/devices` 的泳者设备摘要包含：`bound_swimmer_id`、
`communication_state`、`communication_loss_ms`、`recovery_heartbeat_count`、
`local_alert_stage`、`rescue_expected`、`rescue_confirmed`、`pending_command`。

救生员手环注册成功后，电脑必须立即发送当前完整状态；不得用“没有报警消息”
表示安全：

```json
{
  "type": "lifeguard_state",
  "schema_version": "1.0",
  "message_id": "lg-server14e8-42",
  "target_device_id": "lifeguard-band-a1b2",
  "server_instance_id": "server-14e8",
  "state_revision": 42,
  "generated_at": "2026-08-01T08:30:15.123Z",
  "display_state": "lane1_drowning",
  "drowning_lanes": [1],
  "drowning_counts": {"lane1": 1, "lane2": 0},
  "unlocated_count": 0,
  "vibration": {
    "enabled": true,
    "pattern": "200ms_on_100ms_off"
  }
}
```

`display_state` 只能取：

```text
safe
lane1_drowning
lane2_drowning
both_drowning
```

- 只统计 `online = true` 且 `status = drowning` 的泳者，碰撞状态不参与手环状态。
- 同一泳道多人溺水仍只显示该泳道危险；两道均有危险时显示 `both_drowning`。
- 存在尚未映射到泳道的在线溺水者时，按失效安全原则发送
  `both_drowning`，并在 `unlocated_count` 中给出人数。
- 救生员状态使用独立递增的 `state_revision`，只在派生的溺水状态变化时递增。
- 连接建立后立即发送完整快照；后续仅在状态变化时发送。
- 状态消息等待确认，按 500 ms、1 s、2 s 重试三次；所有重试保持相同
  `message_id` 和内容。新状态产生时直接淘汰旧的待确认快照。
- 手环收到相同版本时不重复切换硬件但仍需确认；旧版本不得覆盖新版本；
  `server_instance_id` 变化时重新接受服务器的初始版本。

救生员手环每 5 秒发送通用心跳：

```json
{
  "type": "device_heartbeat",
  "schema_version": "1.0",
  "message_id": "heartbeat-125",
  "device_id": "lifeguard-band-a1b2",
  "uptime_ms": 125000,
  "sent_at": "2026-08-01T08:30:20Z",
  "last_applied_revision": 42
}
```

`sent_at` 在设备尚未获得可信时间时允许省略；电脑使用接收时间进行超时判断。
电脑逐条确认心跳，连续 15 秒没有心跳时关闭设备会话。相同 `device_id`
重新连接时，新连接替换旧连接。

泳池固定为两条泳道，所有模块的 `lane` 字段只能取整数 `1` 或 `2`。

## 6. 泳池区域标定与 RGB 灯带网关

### 6.1 泳池区域标定

监控页在最新视觉画面中选择泳池左上角和右下角。电脑保存完整画面内的归一化
ROI：

```json
{
  "top_left": {"x": 0.08, "y": 0.12},
  "bottom_right": {"x": 0.92, "y": 0.88},
  "source_frame_id": "camera-top-00000125",
  "source_image_width": 1280,
  "source_image_height": 720
}
```

对于视觉泳者的完整画面归一化中心点 `(frame_x, frame_y)`：

```text
pool_x = (frame_x - x_min) / (x_max - x_min)
pool_y = (frame_y - y_min) / (y_max - y_min)
```

- 中心点在 ROI 外时 `in_pool = false`，不参与灯光投射。
- 手动标定默认以 ROI 中线为泳道边界：`pool_y < 0.5` 为泳道 1，其他为泳道 2。
- 已识别视频回放可返回倾斜浮标绳端点 `lane_boundary_line.left_y/right_y`，
  并按泳者横坐标在线性插值后的边界判断泳道；`lane_boundary_y` 继续返回两端
  中心值以兼容旧客户端。`frame_y` 小于该横坐标处边界为泳道 1，其他为泳道 2。
- `led_index = round(clamp(pool_x, 0, 1) × 69) + 1`。
- 泳道 1 对应 GPIO26，泳道 2 对应 GPIO17。
- 两条灯带均从画面左端第 1 颗递增至右端第 70 颗。
- 虚拟泳者的 `position` 已是池内归一化坐标，可直接参与灯光测试。
- 离线泳者不参与投射。

状态快照中的泳者允许增加以下可选字段：

```json
{
  "frame_position": {"x": 0.31, "y": 0.28},
  "pool_position": {"x": 0.27, "y": 0.21},
  "in_pool": true,
  "lane": 1,
  "led_index": 20
}
```

颜色和覆盖规则：

| 状态 | RGB | 行为 |
| --- | --- | --- |
| `normal` | `00FF00` | 绿色常亮 |
| `collision` | `FFD000` | 黄色常亮 |
| `suspected_drowning` | `FF0000` | 红色逐帧亮灭，文案显示疑似溺水 |
| `drowning` | `FF0000` | 红色逐帧亮灭 |

同一灯珠对应多名泳者时，优先级为：

```text
drowning > suspected_drowning > collision > normal
```

### 6.2 RGB 网关串口

串口参数：

```text
端口：默认自动枚举并探测，也可由本机开发页手动选择
波特率：115200
数据位/校验/停止位：8/N/1
编码：UTF-8
消息边界：每条 JSON 以 LF 结束
单行上限：2048 字节
```

ESP32 上电后发送：

```json
{
  "type": "gateway_hello",
  "schema_version": "1.0",
  "device_id": "rgb-gateway-A0DD6C9B109C",
  "firmware_version": "0.2.0",
  "lanes": 2,
  "leds_per_lane": 70,
  "lane1_pin": 26,
  "lane2_pin": 25
}
```

电脑不会根据串口号识别网关。自动模式会依次打开系统当前可用串口并发送：

```json
{"type":"gateway_probe","schema_version":"1.0"}
```

只有在探测超时内返回合法 `gateway_hello`，且 `lanes=2`、`leds_per_lane=70` 的
串口才会被保留；其他串口立即关闭。手动模式只探测用户指定串口。

电脑确认硬件参数后发送：

```json
{
  "type": "gateway_ready",
  "schema_version": "1.0",
  "frame_interval_ms": 200,
  "watchdog_ms": 1500,
  "server_ipv4": "192.168.1.3",
  "server_port": 8000
}
```

首次启动且网关尚未入网时，电脑可能还无法判断用于该局域网的本机 IPv4。
此时 `server_ipv4` 与 `server_port` 可以成对省略，网关仍完成 RGB/配网串口
握手，但回复 `endpoint_valid = false`，并且不得发送 ESP-NOW 配网广播。网关
入网并上报 IP 后，电脑必须根据到该 IP 的系统路由确定本机 IPv4，再补发一条
带完整端点的 `gateway_ready`。

网关回复：

```json
{
  "type": "gateway_ready_ack",
  "schema_version": "1.0",
  "accepted": true,
  "watchdog_ms": 1500,
  "endpoint_valid": true,
  "server_ipv4": "192.168.1.3",
  "server_port": 8000
}
```

只有握手完成后才能发送灯光帧。电脑每 200 ms 生成一次完整帧；状态变化时允许
立即补发。等待发送的周期帧只保留最新一条，不得积压。

```json
{
  "type": "rgb_frame",
  "schema_version": "1.0",
  "seq": 1820,
  "leds_per_lane": 70,
  "lane1_hex": "<420个十六进制字符>",
  "lane2_hex": "<420个十六进制字符>"
}
```

- 每颗灯使用连续六个字符 `RRGGBB`。
- 每条泳道固定为 `70 × 6 = 420` 个十六进制字符。
- 每条字符串的第一个颜色对应第 1 颗灯。
- 完整帧会替换此前所有灯珠状态，包括全灭帧。

网关成功显示后回复：

```json
{
  "type": "rgb_ack",
  "schema_version": "1.0",
  "seq": 1820,
  "applied": true
}
```

实时 RGB 帧由新帧覆盖旧帧，不执行三次重试；配置和报警类消息仍按通用规则
重试。ESP32 连续 1500 ms 未收到合法 `rgb_frame` 时必须熄灭两条灯带。电脑
正常关闭时应尽力发送全灭帧。

### 6.3 电脑端标定和网关接口

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| `GET` | `/api/calibration/roi` | 获取 ROI 与有效状态 |
| `PUT` | `/api/calibration/roi` | 保存当前视觉画面的 ROI |
| `DELETE` | `/api/calibration/roi` | 清除 ROI |
| `GET` | `/api/gateway/status` | 获取串口、灯光和 Wi-Fi 状态 |
| `POST` | `/api/gateway/reconnect` | 重新连接 RGB 网关 |
| `PUT` | `/api/gateway/port` | 选择指定串口；`{"port":null}` 恢复自动探测 |
| `POST` | `/api/gateway/wifi` | 经串口配置网关 Wi-Fi |

修改标定、重连串口和提交 Wi-Fi 凭据只允许从电脑本机访问。局域网中的其他
浏览器只能读取监控状态。

## 7. 局域网凭据下发与 ESP-NOW 广播

### 7.1 网页到网关

网页向电脑后台提交：

```json
{
  "ssid": "Pool-Safety",
  "password": "example-password"
}
```

电脑通过串口发送：

```json
{
  "type": "wifi_config",
  "schema_version": "1.0",
  "request_id": "wifi-0001",
  "ssid": "Pool-Safety",
  "password": "example-password",
  "server_ipv4": "192.168.1.3",
  "server_port": 8000
}
```

字段约束：

- SSID 为 1～32 个 UTF-8 字节。
- 密码为空字符串时表示开放网络，否则为 8～63 个 UTF-8 字节。
- `server_ipv4` 和 `server_port` 必须同时提供或同时省略；提供时，IPv4 必须是
  电脑当前可供局域网设备访问的非回环地址，端口必须为 `1～65535`，当前固定
  使用 `8000`。
- 电脑不保存密码，也不得在 REST、WebSocket 或日志中回显密码。
- 网关将有效凭据和电脑端点保存到 ESP32 Preferences/NVS，重启后自动重连。
- 电脑确认局域网出口地址后，必须发送携带当前 `server_ipv4` 和
  `server_port` 的 `gateway_ready`；网关检测到端点变化时立即更新并补发
  配网广播。

网关处理中发送：

```json
{
  "type": "wifi_progress",
  "schema_version": "1.0",
  "request_id": "wifi-0001",
  "status": "connecting"
}
```

成功：

```json
{
  "type": "wifi_result",
  "schema_version": "1.0",
  "request_id": "wifi-0001",
  "success": true,
  "ssid": "Pool-Safety",
  "ip": "192.168.1.25",
  "rssi": -51
}
```

失败时 `error_code` 只能取：

```text
AUTH_FAILED
SSID_NOT_FOUND
CONNECT_TIMEOUT
INVALID_CREDENTIALS
INTERNAL_ERROR
```

### 7.2 ESP-NOW 广播包

网关成功接入目标局域网且已获得有效电脑端点后，在当前 Wi-Fi 信道按 10 秒
周期向 `FF:FF:FF:FF:FF:FF` 发送 ESP-NOW 配网广播。每个周期开始时连续
广播约 2.2 秒，间隔约 100 ms，以便正在轮询 1～13 信道的设备快速捕获。
当前科创原型按用户选择使用明文凭据，只允许在受控展示网络中使用。

二进制包使用紧凑布局，固定为 116 字节：

| 字段 | 大小 | 说明 |
| --- | ---: | --- |
| `magic` | 4 | ASCII `SPP1` |
| `version` | 1 | 当前为 `2` |
| `sequence` | 4 | 单调递增广播序号 |
| `ssid_length` | 1 | SSID 实际字节数 |
| `password_length` | 1 | 密码实际字节数 |
| `ssid` | 32 | 定长区域，未使用字节补零 |
| `password` | 63 | 定长区域，未使用字节补零 |
| `server_ipv4` | 4 | IPv4 的 A/B/C/D 四个网络顺序字节 |
| `server_port` | 2 | WebSocket 服务端口，小端序 |
| `crc32` | 4 | 前 112 字节的标准 CRC-32，小端序 |

除 IPv4 外，多字节整数统一使用小端序。CRC-32 参数固定为：

```text
多项式：0xEDB88320
初始值：0xFFFFFFFF
最终异或：0xFFFFFFFF
```

接收端必须同时校验 `magic`、版本、实际消息长度、SSID/密码长度和 CRC32。
旧版 110 字节 `version = 1` 包不含电脑端点，设备只能将其识别为不完整配置，
不得据此入网，必须继续等待 v2 包。

固定测试向量：`sequence = 0x01020304`、SSID 为 `Pool-Safety`、密码为
`example-password`、端点为 `192.168.1.3:8000` 时，端点六字节必须是
`C0 A8 01 03 40 1F`，CRC32 必须是 `0x4BB94BEA`。

救生员手环必须遵循以下启动规则；未来泳者端接入时可复用同一发现机制：

1. 救生员手环每次启动均从信道扫描开始，不使用本地旧凭据直接入网。
2. 按约 150 ms 驻留时间持续轮询 2.4 GHz Wi-Fi 信道 1～13，并等待
   ESP-NOW `SPP1 version = 2` 广播。
3. 收到广播后校验版本、长度、字段范围和 CRC32；校验失败继续等待。
4. 使用有效包中的凭据加入局域网，并连接
   `ws://<server_ipv4>:<server_port>/ws/devices`。
5. 加入失败或运行中 Wi-Fi 断开时，丢弃当前凭据并重新进入信道轮询。
6. WebSocket 断线后按 1 秒、2 秒、5 秒、最多 10 秒退避重连；连续 30 秒
   无法连接电脑时重新搜索配网广播，以获取可能已经变化的电脑地址。
7. 危险期间断线不得自动转为安全；救生员手环继续执行最后一次危险震动，
   直到重连后收到明确的 `safe` 完整快照。

## 8. 兼容性要求

## 6.4 已识别视频回放

电脑端可以把预先生成的逐帧 JSONL 与对应原始视频组合成正式视觉输入。回放帧仍按
`vision_frame JSON -> JPEG bytes` 的顺序广播，因此监控网页、状态映射和 ROI 逻辑
不需要第二套画面协议。`vision_frame` 可选增加：

```json
{
  "collision_warnings": [
    {
      "track_ids": [1, 2],
      "time_to_collision_s": 2.0,
      "minimum_distance_px": 120.0,
      "warning_distance_px": 176.2
    }
  ]
}
```

省略 `collision_warnings` 表示旧发送端没有提供碰撞信息；显式空数组表示当前没有
活动碰撞。实时视觉与开发接口仍使用一次性碰撞短振。预置回放在活动碰撞集合的
START/END 边沿分别自动暂停，START 至 END 之间由电脑等待上一条 ACK 后周期续发既有
`collision_pulse`，END 先清除黄色状态和振动再显示提示。

```text
GET  /api/vision/replays
POST /api/vision/replay/command
```

命令 `action` 支持 `select`、`play`、`pause`、`restart`、`stop` 和
`set_feedback`。修改操作仅允许 localhost。离线回放与 `/ws/vision` 实时输入互斥，
冲突时返回 `VISION_SOURCE_BUSY`。

回放清单包含 `oneline`、`twolines`、`crashing`、`drowning`。状态快照的
`vision_replay` 额外返回 `alert_stage`、`pause_reason` 和当前 `cue`。`drowning`
在人类帧号 335/490（内部索引 334/489）分别进入疑似溺水与高风险并自动暂停；
两个节点都只驱动画面、RGB、救生员手环和既有泳者振动命令，绝不自动发送
`trigger_rescue=true`。实体救生触发与复位继续使用现有设备可靠接口并由本机网页
显式确认。高风险播放到 EOF 后保持，直至停止、切换、重播或成功复位。

- 新增可选字段允许小版本升级；删除字段、改名、改变含义必须升级主版本。
- 接收端必须忽略无法识别的可选字段。
- 接收端遇到未知 `type` 时返回 `UNKNOWN_MESSAGE_TYPE`，不能中断整个服务。
- 除实时 RGB 帧外，需要可靠送达的电脑下行消息至少重试 3 次；
  `lifeguard_state` 的新快照可覆盖旧待确认快照，`rescue_reset_command` 和
  `swimmer_control_command` 的所有重试使用同一 `message_id` 作为幂等键。
- 正式接入设备认证前，`/ws/devices` 只能开放在可信局域网。
