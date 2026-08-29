from __future__ import annotations

import asyncio
import json
import queue
import socket
from pathlib import Path
from unittest.mock import patch

from backend.models import DevSwimmerCreate, GatewayWifiRequest, Position
from backend.serial_gateway import SerialGatewayService, resolve_local_ipv4_for_peer
from backend.settings import SettingsStore
from backend.state import StateStore


class FakeSerial:
    def __init__(self) -> None:
        self.incoming: queue.Queue[bytes] = queue.Queue()
        self.writes: list[bytes] = []
        self.closed = False

    def readline(self) -> bytes:
        try:
            return self.incoming.get(timeout=0.03)
        except queue.Empty:
            return b""

    def write(self, data: bytes) -> int:
        if self.closed:
            raise OSError("closed")
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


async def wait_until(predicate, timeout: float = 1.5) -> None:
    started = asyncio.get_running_loop().time()
    while not predicate():
        if asyncio.get_running_loop().time() - started > timeout:
            raise AssertionError("condition timed out")
        await asyncio.sleep(0.01)


def decoded_writes(fake: FakeSerial) -> list[dict]:
    return [json.loads(item.decode("utf-8")) for item in fake.writes]


def test_endpoint_resolver_ignores_meta_benchmark_adapter() -> None:
    class RoutedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def connect(self, _endpoint) -> None:
            return None

        def getsockname(self) -> tuple[str, int]:
            return ("198.18.0.1", 12345)

    addresses = [
        (socket.AF_INET, 0, 0, "", ("198.18.0.1", 0)),
        (socket.AF_INET, 0, 0, "", ("192.168.1.3", 0)),
    ]
    with (
        patch("backend.serial_gateway.socket.socket", return_value=RoutedSocket()),
        patch("backend.serial_gateway.socket.gethostname", return_value="host"),
        patch("backend.serial_gateway.socket.getaddrinfo", return_value=addresses),
    ):
        assert resolve_local_ipv4_for_peer("192.168.10.112") == "192.168.1.3"


def test_serial_handshake_frames_wifi_and_status(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeSerial()
        local_address = ["192.168.1.3"]
        fake.incoming.put(b"legacy lamp-test firmware ready\r\n")
        fake.incoming.put(
            (
                json.dumps(
                    {
                        "type": "gateway_hello",
                        "schema_version": "1.0",
                        "device_id": "rgb-test",
                        "firmware_version": "0.2.0",
                        "lanes": 2,
                        "leds_per_lane": 70,
                    }
                )
                + "\n"
            ).encode()
        )
        fake.incoming.put(
            b'{"type":"gateway_ready_ack","schema_version":"1.0",'
            b'"accepted":true,"watchdog_ms":1500}\n'
        )
        state = StateStore(SettingsStore(tmp_path / "settings.json"))
        await state.create_virtual(
            DevSwimmerCreate(position=Position(x=0.25, y=0.25))
        )
        callbacks = 0

        async def changed() -> None:
            nonlocal callbacks
            callbacks += 1

        service = SerialGatewayService(
            state,
            changed,
            serial_factory=lambda **_: fake,
            port_provider=lambda: [
                {"device": "COM9", "description": "测试串口", "hwid": "TEST"}
            ],
            address_resolver=lambda _: local_address[0],
            endpoint_check_interval=60,
            probe_timeout=0.2,
        )
        await service.start()
        await wait_until(
            lambda: any(
                item.get("type") == "rgb_frame" for item in decoded_writes(fake)
            )
        )
        messages = decoded_writes(fake)
        probe = next(item for item in messages if item["type"] == "gateway_probe")
        ready = next(item for item in messages if item["type"] == "gateway_ready")
        frame = next(item for item in messages if item["type"] == "rgb_frame")
        assert probe["schema_version"] == "1.0"
        assert ready["frame_interval_ms"] == 200
        assert "server_ipv4" not in ready
        assert "server_port" not in ready
        assert len(frame["lane1_hex"]) == 420
        assert len(frame["lane2_hex"]) == 420

        result = await service.configure_wifi(
            GatewayWifiRequest(ssid="Pool-Safety", password="supersecret")
        )
        assert result["status"] == "configuring"
        wifi_command = next(
            item
            for item in decoded_writes(fake)
            if item.get("type") == "wifi_config"
        )
        assert "server_ipv4" not in wifi_command
        assert "server_port" not in wifi_command

        fake.incoming.put(
            b'{"type":"wifi_status","status":"connected","ssid":"Pool-Safety",'
            b'"ip":"192.168.1.5","rssi":-48,"channel":6}\n'
        )

        fake.incoming.put(
            b'{"type":"provision_broadcast","sequence":8,"sent":true,'
            b'"channel":6,"successes":2,"failures":1}\n'
        )
        await wait_until(
            lambda: (
                state._gateway["provision_broadcast"]["sequence"] == 8
            )
        )
        await wait_until(
            lambda: any(
                item.get("type") == "gateway_ready"
                and item.get("server_ipv4") == "192.168.1.3"
                and item.get("server_port") == 8000
                for item in decoded_writes(fake)
            )
        )
        snapshot = await state.snapshot()
        assert snapshot["rgb_gateway"]["port"] == "COM9"
        assert snapshot["rgb_gateway"]["port_mode"] == "auto"
        assert snapshot["rgb_gateway"]["wifi"]["status"] == "connected"
        assert snapshot["rgb_gateway"]["wifi"]["channel"] == 6
        assert snapshot["rgb_gateway"]["server_ipv4"] == "192.168.1.3"
        assert (
            snapshot["rgb_gateway"]["provision_broadcast"]["successes"] == 2
        )
        assert snapshot["rgb_gateway"]["provision_broadcast"]["sent"] is True

        await service.configure_wifi(
            GatewayWifiRequest(ssid="Pool-Safety", password="supersecret")
        )
        wifi_commands = [
            item
            for item in decoded_writes(fake)
            if item.get("type") == "wifi_config"
        ]
        assert wifi_commands[-1]["server_ipv4"] == "192.168.1.3"
        assert wifi_commands[-1]["server_port"] == 8000

        local_address[0] = "192.168.1.4"
        fake.incoming.put(
            b'{"type":"gateway_status","protocol_ready":true,'
            b'"wifi":{"status":"connected","ip":"192.168.1.5"}}\n'
        )
        await wait_until(
            lambda: any(
                item.get("type") == "gateway_ready"
                and item.get("server_ipv4") == "192.168.1.4"
                for item in decoded_writes(fake)
            )
        )
        assert "supersecret" not in str(snapshot)
        assert callbacks > 0
        await service.stop()

    asyncio.run(scenario())


def test_auto_scan_skips_non_gateway_and_manual_port_is_selectable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        other = FakeSerial()
        gateway = FakeSerial()
        gateway.incoming.put(
            b'{"type":"gateway_hello","schema_version":"1.0",'
            b'"device_id":"rgb-auto","firmware_version":"0.3.1",'
            b'"lanes":2,"leds_per_lane":70}\n'
        )
        gateway.incoming.put(
            b'{"type":"gateway_ready_ack","schema_version":"1.0",'
            b'"accepted":true}\n'
        )
        serials = {"COM4": other, "COM7": gateway}
        state = StateStore(SettingsStore(tmp_path / "settings.json"))

        async def changed() -> None:
            return None

        service = SerialGatewayService(
            state,
            changed,
            serial_factory=lambda **kwargs: serials[kwargs["port"]],
            port_provider=lambda: [
                {"device": "COM4", "description": "普通串口", "hwid": "A"},
                {"device": "COM7", "description": "RGB 网关", "hwid": "B"},
            ],
            probe_timeout=0.08,
            endpoint_check_interval=60,
        )
        await service.start()
        await wait_until(lambda: state._gateway["protocol_ready"])
        assert other.closed is True
        assert decoded_writes(other)[0]["type"] == "gateway_probe"
        assert decoded_writes(gateway)[0]["type"] == "gateway_probe"
        status = await state.gateway_status()
        assert status["port"] == "COM7"
        assert status["available_ports"] == ["COM4", "COM7"]

        selected = await service.select_port("COM7")
        assert selected["port_mode"] == "manual"
        assert selected["preferred_port"] == "COM7"
        status = await state.gateway_status()
        assert status["port_mode"] == "manual"
        await service.stop()

    asyncio.run(scenario())
