# 冠影守望者（SPPer）

面向双泳道泳池场景的智能安全防护原型。系统以电脑为状态主控，统一协调视觉回放、
泳者定位、碰撞预警、疑似溺水分级响应、RGB灯带、救生员手环和泳者端救生装置。

## 当前组成

| 模块 | 平台 | 目录 |
| --- | --- | --- |
| 电脑端后台与网页 | Python / FastAPI / Next.js | `computer/` |
| RGB灯带网关 `0.3.3` | ESP32 | `main/` |
| 救生员手环 `0.3.1` | HU-087 / ESP32-S3 | `band/` |
| 泳者安全终端 `0.5.4` | ESP32-C3 | `swimmer/` |
| CPU预识别演示 | MP4 + JSONL + OpenCV | `demo_assets/` |

本仓库不包含YOLO模型、训练数据或推理工程。四组展示视频使用已经生成的逐帧JSONL，
由OpenCV在CPU上同步解码，可在没有独立显卡和CUDA的笔记本上演示。

## 快速部署（Windows）

需要64位Python 3.11或3.12。展示包已包含静态网页，普通部署不要求Node.js。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_laptop.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\verify_demo.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start_demo.ps1
```

启动后访问：

- 监控页：<http://127.0.0.1:8000/>
- 开发与配对页：<http://127.0.0.1:8000/dev/>
- API文档：<http://127.0.0.1:8000/docs>

## 文档

- [项目全面介绍](./项目全面介绍.md)
- [笔记本部署与演示指南](./笔记本部署与演示指南.md)
- [数据接口规范](./DATA_INTERFACE_SPEC.md)
- [电脑端说明](./computer/README.md)
- [RGB网关说明](./main/README.md)
- [救生员手环说明](./band/README.md)
- [泳者端说明](./swimmer/README.md)

## 开发验证

```powershell
cd computer
.\.venv\Scripts\python.exe -m pytest tests_python -q
npm run lint
npm run build:static

cd ..\main
pio test -e native
pio run -e esp32dev

cd ..\swimmer
pio test -e native
pio run -e esp32-c3

cd ..\band
pio run -e hu-087
```

## 安全说明

网页中的“触发救生装置”会向在线泳者端发送真实硬件命令，必须在确认装置周围无人员、
无阻挡并具备安全复位条件后操作。视频演示事件不会自动下发实体救生触发命令。
