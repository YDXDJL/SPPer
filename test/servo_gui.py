import tkinter as tk
from tkinter import messagebox, ttk

import serial
from serial.tools import list_ports


BAUD_RATE = 115200


class ServoController(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ESP32 180°舵机角度控制")
        self.resizable(False, False)
        self.serial_port: serial.Serial | None = None

        self.port_var = tk.StringVar(value="")
        self.angle_var = tk.IntVar(value=90)
        self.minimum_pulse_var = tk.IntVar(value=600)
        self.maximum_pulse_var = tk.IntVar(value=2600)
        self.status_var = tk.StringVar(value="未连接")

        self._build_ui()
        self.refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self.close_window)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.grid()

        ttk.Label(frame, text="串口").grid(row=0, column=0, sticky="w")
        self.port_box = ttk.Combobox(
            frame, textvariable=self.port_var, width=14, state="readonly"
        )
        self.port_box.grid(row=0, column=1, padx=8)
        ttk.Button(frame, text="刷新", command=self.refresh_ports).grid(row=0, column=2)
        self.connect_button = ttk.Button(
            frame, text="连接", command=self.toggle_connection
        )
        self.connect_button.grid(row=0, column=3, padx=(8, 0))

        ttk.Label(frame, text="目标角度").grid(
            row=1, column=0, pady=(20, 0), sticky="w"
        )
        self.angle_scale = ttk.Scale(
            frame,
            from_=0,
            to=180,
            variable=self.angle_var,
            command=self.angle_changed,
            length=230,
        )
        self.angle_scale.grid(row=1, column=1, columnspan=2, pady=(20, 0))
        self.angle_scale.bind("<ButtonRelease-1>", lambda _event: self.move_servo())
        self.angle_label = ttk.Label(frame, text="90°", width=5)
        self.angle_label.grid(row=1, column=3, pady=(20, 0))

        shortcut_frame = ttk.Frame(frame)
        shortcut_frame.grid(row=2, column=0, columnspan=4, pady=12, sticky="ew")
        for column, angle in enumerate((0, 45, 90, 135, 180)):
            ttk.Button(
                shortcut_frame,
                text=f"{angle}°",
                command=lambda value=angle: self.set_and_move(value),
            ).grid(row=0, column=column, padx=3)

        ttk.Label(frame, text="脉宽校准").grid(row=3, column=0, sticky="w")
        ttk.Label(frame, text="最小").grid(row=3, column=1, sticky="e")
        ttk.Spinbox(
            frame,
            from_=400,
            to=1400,
            increment=10,
            textvariable=self.minimum_pulse_var,
            width=7,
        ).grid(row=3, column=2, sticky="w")
        ttk.Label(frame, text="μs").grid(row=3, column=3, sticky="w")

        ttk.Label(frame, text="最大").grid(row=4, column=1, sticky="e")
        ttk.Spinbox(
            frame,
            from_=1600,
            to=2600,
            increment=10,
            textvariable=self.maximum_pulse_var,
            width=7,
        ).grid(row=4, column=2, sticky="w")
        ttk.Label(frame, text="μs").grid(row=4, column=3, sticky="w")

        ttk.Button(frame, text="转到目标角度", command=self.move_servo).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(16, 0), padx=(0, 4)
        )
        ttk.Button(frame, text="释放舵机", command=self.release_servo).grid(
            row=5, column=2, columnspan=2, sticky="ew", pady=(16, 0), padx=(4, 0)
        )

        ttk.Separator(frame).grid(
            row=6, column=0, columnspan=4, sticky="ew", pady=14
        )
        ttk.Label(frame, textvariable=self.status_var).grid(
            row=7, column=0, columnspan=4
        )

    def refresh_ports(self) -> None:
        detected = list(list_ports.comports())
        ports = [port.device for port in detected]
        self.port_box["values"] = ports
        if self.port_var.get() not in ports:
            preferred = next(
                (
                    port.device
                    for port in detected
                    if port.vid == 0x1A86 and port.pid == 0x7523
                ),
                ports[0] if ports else "",
            )
            self.port_var.set(preferred)

    def toggle_connection(self) -> None:
        if self.serial_port and self.serial_port.is_open:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        port = self.port_var.get()
        if not port:
            messagebox.showerror("连接失败", "未找到可用串口")
            return
        try:
            self.serial_port = serial.Serial(
                port, BAUD_RATE, timeout=0.1, write_timeout=1
            )
            self.serial_port.dtr = False
            self.serial_port.rts = False
            self.connect_button.config(text="断开")
            self.status_var.set(f"已连接 {port}；请选择角度")
        except serial.SerialException as error:
            self.serial_port = None
            messagebox.showerror("连接失败", str(error))

    def disconnect(self) -> None:
        if self.serial_port and self.serial_port.is_open:
            self.release_servo(show_error=False)
            self.serial_port.close()
        self.serial_port = None
        self.connect_button.config(text="连接")
        self.status_var.set("未连接")

    def send_command(self, command: str, show_error: bool = True) -> bool:
        if not self.serial_port or not self.serial_port.is_open:
            if show_error:
                messagebox.showwarning("尚未连接", "请先连接 ESP32 串口")
            return False
        try:
            self.serial_port.write((command + "\n").encode("ascii"))
            self.serial_port.flush()
            return True
        except serial.SerialException as error:
            self.status_var.set(f"串口错误：{error}")
            return False

    def pulse_range(self) -> tuple[int, int] | None:
        try:
            minimum = int(self.minimum_pulse_var.get())
            maximum = int(self.maximum_pulse_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("校准错误", "脉宽必须是整数")
            return None
        if not (400 <= minimum < 1500 < maximum <= 2600):
            messagebox.showerror(
                "校准错误", "需要满足 400 ≤ 最小值 < 1500 < 最大值 ≤ 2600"
            )
            return None
        return minimum, maximum

    def move_servo(self) -> None:
        pulse_range = self.pulse_range()
        if pulse_range is None:
            return
        angle = max(0, min(180, int(self.angle_var.get())))
        minimum, maximum = pulse_range
        if self.send_command(f"angle {angle} {minimum} {maximum}"):
            self.status_var.set(
                f"目标 {angle}°，脉宽范围 {minimum}–{maximum} μs"
            )

    def release_servo(self, show_error: bool = True) -> None:
        if self.send_command("release", show_error=show_error):
            self.status_var.set("PWM已关闭，舵机已释放")

    def set_and_move(self, angle: int) -> None:
        self.angle_var.set(angle)
        self.angle_changed(str(angle))
        self.move_servo()

    def angle_changed(self, _value: str) -> None:
        self.angle_label.config(text=f"{int(self.angle_var.get())}°")

    def close_window(self) -> None:
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    ServoController().mainloop()
