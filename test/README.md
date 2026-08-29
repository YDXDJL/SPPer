# ESP32 180°位置舵机控制

舵机信号线接 `GPIO25`，地线必须与 ESP32 共地。固件通过串口接收目标角度，GUI 提供 `0–180°` 角度控制和脉宽校准。

```powershell
cd D:\Vibecoding_Projects\SPPer\test
python -m pip install -r requirements.txt
python servo_gui.py
```

GUI 会优先选择 CH340 对应的 ESP32 串口。连接后可拖动角度滑块，或点击 0°、45°、90°、135°、180° 快捷按钮。

默认脉宽范围为 `1000–2000 μs`。若实际转角不足，可每次将最小值降低、最大值升高 `50 μs` 后重试。舵机触及机械限位、持续嗡响或明显发热时，应立即回退脉宽并点击“释放舵机”。
