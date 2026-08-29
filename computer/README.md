# 冠影守望者电脑端

电脑端由一个 Python 主进程统一提供：

- 运行监控网页；
- 开发测试网页；
- 系统状态 REST API；
- 视觉模块接入 WebSocket；
- 网页实时状态与画面 WebSocket；
- 救生员手环与泳者设备 `/ws/devices` 注册、心跳和可靠命令；
- 自动探测或手动选择的 115200 RGB 网关串口服务；
- 两条泳道各 70 灯的 RGB 整帧输出；
- 视觉画面泳池 ROI 校准与持久化；
- ESP32 网关 Wi-Fi 配网和状态显示。

视觉识别算法不包含在本目录中。它作为独立进程，按照项目根目录的
`DATA_INTERFACE_SPEC.md` 向 `/ws/vision` 推送识别结果和 JPEG 画面。

## 首次安装

移动硬盘展示副本请优先阅读项目根目录的 `笔记本部署与演示指南.md`，并运行
`scripts/setup_laptop.ps1`。展示包已包含静态网页和离线 Python 依赖，只要求安装
64 位 Python 3.11 或 3.12，不要求 Node.js。

只有开发网页源码或缺少 `out/index.html` 时，才需要 Node.js 20.9 或更高版本：

```powershell
cd D:\Vibecoding_Projects\SPPer\computer
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm install
```

## 启动

```powershell
.\.venv\Scripts\python.exe run.py
```

首次启动会自动构建网页。之后访问：

- 本机：`http://127.0.0.1:8000`
- 局域网：`http://电脑的局域网IP:8000`
- 开发测试页：`http://127.0.0.1:8000/dev/`
- 后台接口文档：`http://127.0.0.1:8000/docs`

RGB 网关未连接时网站仍会正常启动。电脑会逐个打开当前可用串口，发送
`gateway_probe` 并只保留返回合法 `gateway_hello` 的网关连接；开发页也可以
切换为手动指定串口。网关握手成功后，
电脑每 200 ms 下发一次两道整帧。正常、碰撞和溺水状态分别使用绿色、黄色和
红色；溺水红灯逐帧明灭。

救生员手环加入局域网后连接 `ws://电脑局域网IP:8000/ws/devices`。手环必须在
5 秒内发送 `device_hello`，随后每 5 秒发送带必填 `uptime_ms` 的
`device_heartbeat`；`sent_at` 仅在设备已同步 UTC 时间时发送。电脑在注册后立即
同步完整 `lifeguard_state`，状态未确认时按 500 ms、1 s、2 s 重试，15 秒未收到
心跳则将设备标记为离线。碰撞不触发救生员手环，溺水时振动节奏固定为
`200ms_on_100ms_off`。

泳者设备也连接 `/ws/devices`，注册 ACK 指定 1 秒心跳。电脑按接收时间独立执行
10 秒疑似溺水、20 秒救生触发判定；断线不会清除危险，报警后必须收到 3 个不同
`message_id` 的连续有效心跳才恢复。开发测试页可将设备一对一绑定到视觉或虚拟
泳者；绑定仅在本次运行有效。未绑定、目标消失或目标离线的报警会按未定位危险
同时通知两条泳道。救生锁存只能在设备已连接、通信恢复且设备确认锁存后从本机
页面可靠复位。

网关上报自己的局域网 IP 后，电脑根据系统路由确定同网段本机 IPv4，并通过
`gateway_ready` 和后续 `wifi_config` 发送 `server_ipv4/server_port`。首次配网时
网关尚无 IP，消息可以暂不携带端点；收到 `wifi_result` 或 `wifi_status` 后会立即
补发带端点的 `gateway_ready`。

ROI、虚拟泳者、碰撞、串口重连和 Wi-Fi 配网等修改操作只能从本机
`127.0.0.1` 页面执行。局域网访问保持只读监控。

Windows 防火墙首次询问时，需要允许 Python 在专用网络中通信。

修改网页后删除 `out` 目录或执行 `npm run build:static`，再重启 Python
服务。修改后台时可以使用：

```powershell
.\.venv\Scripts\python.exe run.py --reload
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests_python -q
npm run build:static
```

## 当前数据策略

## 已识别视频回放

监控页左上方可以直接回放 `oneline`、`twolines`、`crashing` 和 `drowning`
四组样例。
电脑只解码 `demo_assets/vision_replays/videos` 中的原始 H.264 视频，并同步读取
`demo_assets/vision_replays/tracks` 中已经生成的逐帧 JSONL；部署与演示包不包含
YOLO 模型、推理代码或训练环境，也不会重新运行 YOLO。
回放时电脑按视频中间的浮标绳位置逐帧插值泳道边界，并把泳者横向位置映射到
每条泳道的 1～70 号灯；该自动边界仅对当前回放生效，停止后恢复手动标定。

回放碰撞 episode 会在开始与结束帧分别暂停；开始后电脑等待每次 ACK 再周期续发
现有约 250 ms 的 `collision_pulse`，直到结束帧先停止振动、清除黄灯再暂停。
“泳者端振动联动”默认开启，可在回放面板中关闭；实时视觉与开发页原有的一次性
短振语义不变。

`drowning` 在第 335/490 帧分别进入疑似溺水和高风险并自动暂停。暂停期间保持红灯、
救生员手环预警和泳者振动，高风险播放到结尾后也继续保持。演示节点不会自动触发
实体救生装置；监控页始终显示手动触发与复位按钮，真实触发可在任意回放状态下
对在线且空闲的泳者设备执行，但必须经过确认。停止视频不会暗中复位已经锁存的
实体装置。恰好一台泳者设备在线时自动配对到 `vision-1`，多台在线时等待
手动配对且不猜测硬件目标。

回放控制接口只允许 localhost：

```text
GET  /api/vision/replays
POST /api/vision/replay/command
```

首次安装或更新后需要重新执行 `pip install -r requirements.txt`，电脑端使用
`opencv-python-headless` 在后台线程完成视频解码。

- 所有泳者与报警状态暂存在内存中，服务器重启后清空。
- 泳池 ROI 保存于 `data/settings.json`，服务器重启后自动恢复。
- 位置统一使用左上角为原点的归一化坐标，范围为 `0.0～1.0`。
- 开发测试页只能修改或删除虚拟泳者，不能覆盖真实视觉泳者。
- 溺水和碰撞在当前测试模型中互斥；设置新状态时自动清除旧状态。
- Wi-Fi 密码只通过一次串口消息发送，不写入文件、不回显，也不进入状态广播。
- 可穿戴设备会话、运行期绑定、心跳和 ACK 状态暂存在内存中，断线记录与危险
  锁存保留到电脑服务重启。
- 配对窗口只列出在线泳者和已连接泳者设备，支持拖拽一对一配对。每个配对可以
  禁用自动救生锁存，仅保留失联振动；也可以可靠下发一键救生触发。
- 开发页的“模拟下沉”不会直接报警；它只让已配对的在线泳者端暂停业务收发，
  由真实的 10 秒和 20 秒心跳超时链路依次触发疑似溺水和救生状态。恢复后需三个
  有效心跳解除报警。
- 设置两名泳者碰撞时，只向与两者配对的泳者端各发送一次约 250 ms 的短振提醒；
  2 秒内重复通知会被抑制，碰撞不触发救生员手环。
