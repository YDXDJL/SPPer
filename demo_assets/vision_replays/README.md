# 预识别视频演示资产

本目录是电脑端四组演示回放的唯一运行时数据源，不包含视觉模型或推理代码。

- `videos/`：原始 H.264 MP4，由 OpenCV 在 CPU 上逐帧解码。
- `tracks/`：视觉会话已经生成的逐帧 JSONL 与摘要 JSON。
- `manifest.sha256`：迁移后用于校验大文件是否完整。

四组样例为 `oneline`、`twolines`、`crashing` 和 `drowning`。电脑端只读取既有
结果，不会重新执行 YOLO；因此展示电脑不需要独立显卡或 CUDA。
