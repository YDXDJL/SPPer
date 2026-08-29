from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from fastapi import HTTPException

from .lighting import OFF, compose_rgb_frame
from .models import GatewayWifiRequest
from .state import StateStore

BAUD_RATE = 115200
FRAME_INTERVAL_SECONDS = 0.2
LINE_LIMIT = 4096
SERVER_PORT = 8000
ENDPOINT_CHECK_INTERVAL_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 1.0
BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class SerialConnection(Protocol):
    def readline(self) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


SerialFactory = Callable[..., SerialConnection]
StateCallback = Callable[[], Awaitable[None]]
AddressResolver = Callable[[str], str | None]
PortProvider = Callable[[], list[dict[str, str]]]


def default_serial_factory(**kwargs) -> SerialConnection:
    import serial

    return serial.Serial(**kwargs)


def default_port_provider() -> list[dict[str, str]]:
    from serial.tools import list_ports

    return [
        {
            "device": item.device,
            "description": item.description or item.device,
            "hwid": item.hwid or "",
        }
        for item in list_ports.comports()
    ]


def _is_usable_server_ipv4(address: ipaddress.IPv4Address) -> bool:
    return not (
        address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or address in BENCHMARK_NETWORK
    )


def _shared_prefix_bits(left: ipaddress.IPv4Address, right: ipaddress.IPv4Address) -> int:
    difference = int(left) ^ int(right)
    return 32 if difference == 0 else 32 - difference.bit_length()


def resolve_local_ipv4_for_peer(peer_ip: str) -> str | None:
    try:
        peer = ipaddress.ip_address(peer_ip)
        if not isinstance(peer, ipaddress.IPv4Address):
            return None
    except ValueError:
        return None

    raw_candidates: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect((str(peer), 9))
            raw_candidates.append(connection.getsockname()[0])
    except OSError:
        pass
    try:
        raw_candidates.extend(
            item[4][0]
            for item in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET
            )
        )
    except OSError:
        pass

    candidates: list[ipaddress.IPv4Address] = []
    for raw_candidate in raw_candidates:
        try:
            candidate = ipaddress.ip_address(raw_candidate)
        except ValueError:
            continue
        if (
            isinstance(candidate, ipaddress.IPv4Address)
            and _is_usable_server_ipv4(candidate)
            and candidate not in candidates
        ):
            candidates.append(candidate)
    if not candidates:
        return None

    # VPN/proxy adapters can hijack the UDP route (for example 198.18/15).
    # Prefer the real private adapter whose address most closely matches the
    # gateway, while retaining the routed candidate as a final tie-breaker.
    routed = raw_candidates[0] if raw_candidates else None
    best = max(
        candidates,
        key=lambda candidate: (
            int(candidate.is_private == peer.is_private),
            _shared_prefix_bits(candidate, peer),
            int(str(candidate) == routed),
        ),
    )
    return str(best)


class SerialGatewayService:
    def __init__(
        self,
        state: StateStore,
        on_state_change: StateCallback,
        *,
        serial_factory: SerialFactory = default_serial_factory,
        port_provider: PortProvider = default_port_provider,
        address_resolver: AddressResolver = resolve_local_ipv4_for_peer,
        endpoint_check_interval: float = ENDPOINT_CHECK_INTERVAL_SECONDS,
        probe_timeout: float = PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self.state = state
        self.on_state_change = on_state_change
        self.serial_factory = serial_factory
        self.port_provider = port_provider
        self.address_resolver = address_resolver
        self.endpoint_check_interval = endpoint_check_interval
        self.probe_timeout = probe_timeout
        self._running = False
        self._serial: SerialConnection | None = None
        self._protocol_ready = False
        self._sequence = 0
        self._gateway_identified = False
        self._server_ipv4: str | None = None
        self._preferred_port: str | None = None
        self._write_lock = asyncio.Lock()
        self._reconnect = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._supervisor(), name="rgb-serial-supervisor"),
            asyncio.create_task(self._frame_loop(), name="rgb-frame-loop"),
            asyncio.create_task(
                self._endpoint_loop(), name="gateway-endpoint-monitor"
            ),
        ]

    async def stop(self) -> None:
        if not self._running:
            return
        if self._protocol_ready:
            try:
                await self._send_frame(force_off=True)
            except Exception:
                pass
        self._running = False
        self._reconnect.set()
        await self._close_serial()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.state.update_gateway(
            serial_connected=False,
            protocol_ready=False,
            output_status="serial_disconnected",
        )
        await self.on_state_change()

    async def reconnect(self) -> None:
        self._reconnect.set()
        await self._close_serial()
        await self.state.update_gateway(
            serial_connected=False,
            protocol_ready=False,
            output_status="serial_disconnected",
            last_error=None,
        )
        await self.on_state_change()

    async def select_port(self, port: str | None) -> dict:
        normalized = port.strip() if isinstance(port, str) else None
        self._preferred_port = normalized or None
        await self.state.update_gateway(
            port_mode="manual" if self._preferred_port else "auto",
            preferred_port=self._preferred_port,
            port=None,
            scanning_port=None,
            last_error=None,
        )
        await self.reconnect()
        return {
            "status": "reconnecting",
            "port_mode": "manual" if self._preferred_port else "auto",
            "preferred_port": self._preferred_port,
        }

    async def configure_wifi(self, command: GatewayWifiRequest) -> dict:
        if not self._protocol_ready or self._serial is None:
            raise HTTPException(
                503,
                detail={"code": "GATEWAY_UNAVAILABLE", "message": "RGB 网关未就绪"},
            )
        status = await self.state.gateway_status()
        gateway_ip = status["wifi"].get("ip")
        if isinstance(gateway_ip, str):
            await self._refresh_server_endpoint(gateway_ip, announce=True)
        if status["wifi"]["status"] == "configuring":
            raise HTTPException(
                409,
                detail={"code": "WIFI_CONFIG_BUSY", "message": "正在进行配网"},
            )
        request_id = f"wifi-{uuid4().hex[:8]}"
        await self.state.update_gateway(
            wifi={
                "status": "configuring",
                "request_id": request_id,
                "ssid": command.ssid,
                "ip": None,
                "rssi": None,
                "channel": None,
                "error_code": None,
                "message": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self.on_state_change()
        try:
            payload = {
                "type": "wifi_config",
                "schema_version": "1.0",
                "request_id": request_id,
                "ssid": command.ssid,
                "password": command.password.get_secret_value(),
            }
            if self._server_ipv4 is not None:
                payload["server_ipv4"] = self._server_ipv4
                payload["server_port"] = SERVER_PORT
            await self._send_json(payload)
        except Exception as exc:
            await self.state.update_gateway(
                wifi={
                    "status": "failed",
                    "error_code": "SERIAL_WRITE_FAILED",
                    "message": str(exc),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self.on_state_change()
            raise HTTPException(
                503,
                detail={"code": "SERIAL_WRITE_FAILED", "message": "配网指令发送失败"},
            ) from exc
        return {"request_id": request_id, "status": "configuring"}

    async def _supervisor(self) -> None:
        retry_index = 0
        retry_delays = (1, 2, 5, 10)
        while self._running:
            self._reconnect.clear()
            try:
                serial_connection, port, hello = await self._discover_gateway()
                self._serial = serial_connection
                self._protocol_ready = False
                self._gateway_identified = True
                retry_index = 0
                await self.state.update_gateway(
                    port=port,
                    scanning_port=None,
                    serial_connected=True,
                    protocol_ready=False,
                    output_status="starting",
                    last_error=None,
                )
                await self.on_state_change()
                await self._handle_message(hello)
                await self._reader_loop(serial_connection)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.state.update_gateway(
                    port=None,
                    scanning_port=None,
                    serial_connected=False,
                    protocol_ready=False,
                    output_status="serial_disconnected",
                    last_error=str(exc),
                )
                await self.on_state_change()
            finally:
                self._protocol_ready = False
                await self._close_serial()

            if not self._running:
                break
            delay = retry_delays[min(retry_index, len(retry_delays) - 1)]
            retry_index += 1
            try:
                await asyncio.wait_for(self._reconnect.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _discover_gateway(
        self,
    ) -> tuple[SerialConnection, str, dict[str, Any]]:
        available = await asyncio.to_thread(self.port_provider)
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in available:
            device = str(item.get("device", "")).strip()
            if not device or device in seen:
                continue
            seen.add(device)
            normalized.append(
                {
                    "device": device,
                    "description": str(item.get("description") or device),
                    "hwid": str(item.get("hwid") or ""),
                }
            )
        await self.state.update_gateway(
            available_ports=[item["device"] for item in normalized]
        )
        await self.on_state_change()

        if self._preferred_port is not None:
            candidates = [self._preferred_port]
        else:
            candidates = [item["device"] for item in normalized]
        if not candidates:
            raise ConnectionError("未发现可用串口")

        last_error = "未找到冠影守望者 RGB 网关"
        for port in candidates:
            if self._reconnect.is_set() or not self._running:
                raise ConnectionError("串口扫描已取消")
            await self.state.update_gateway(
                scanning_port=port,
                output_status="probing",
                last_error=None,
            )
            await self.on_state_change()
            connection: SerialConnection | None = None
            try:
                connection = await asyncio.to_thread(
                    self.serial_factory,
                    port=port,
                    baudrate=BAUD_RATE,
                    timeout=0.2,
                    write_timeout=0.5,
                )
                await self._write_json(
                    connection,
                    {"type": "gateway_probe", "schema_version": "1.0"},
                )
                hello = await self._wait_for_gateway_hello(connection)
                if hello is not None:
                    return connection, port, hello
                last_error = f"{port} 未响应 RGB 网关探测"
            except Exception as exc:
                last_error = f"{port}: {exc}"
            if connection is not None:
                try:
                    await asyncio.to_thread(connection.close)
                except Exception:
                    pass
        raise ConnectionError(last_error)

    async def _wait_for_gateway_hello(
        self, connection: SerialConnection
    ) -> dict[str, Any] | None:
        deadline = asyncio.get_running_loop().time() + self.probe_timeout
        while asyncio.get_running_loop().time() < deadline:
            raw = await asyncio.to_thread(connection.readline)
            if not raw or len(raw) > LINE_LIMIT:
                continue
            try:
                message = json.loads(raw.decode("utf-8").strip())
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(message, dict)
                and message.get("type") == "gateway_hello"
                and message.get("schema_version") == "1.0"
                and message.get("lanes") == 2
                and message.get("leds_per_lane") == 70
            ):
                return message
        return None

    async def _reader_loop(self, connection: SerialConnection) -> None:
        while self._running and not self._reconnect.is_set():
            raw = await asyncio.to_thread(connection.readline)
            if not raw:
                continue
            if len(raw) > LINE_LIMIT:
                await self.state.update_gateway(
                    last_error="SERIAL_LINE_TOO_LONG",
                    output_status="protocol_error",
                )
                await self.on_state_change()
                continue
            try:
                message = json.loads(raw.decode("utf-8").strip())
            except (UnicodeDecodeError, json.JSONDecodeError):
                # ESP32 ROM boot text and the legacy lamp-test firmware may share
                # this serial port. They are outside the NDJSON protocol and can
                # be ignored while waiting for gateway_hello.
                continue
            if not isinstance(message, dict):
                continue
            await self._handle_message(message)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        message_type = message.get("type")
        await self.state.update_gateway(last_seen_at=now)
        if message_type == "gateway_hello":
            if message.get("lanes") != 2 or message.get("leds_per_lane") != 70:
                await self.state.update_gateway(
                    protocol_ready=False,
                    output_status="protocol_error",
                    last_error="UNSUPPORTED_LED_LAYOUT",
                )
            else:
                self._gateway_identified = True
                gateway_status = await self.state.gateway_status()
                gateway_ip = gateway_status["wifi"].get("ip")
                if isinstance(gateway_ip, str):
                    await self._refresh_server_endpoint(
                        gateway_ip, announce=False
                    )
                await self._send_gateway_ready()
                await self.state.update_gateway(
                    protocol_ready=False,
                    device_id=message.get("device_id"),
                    firmware_version=message.get("firmware_version"),
                    output_status="starting",
                    last_error=None,
                )
        elif message_type == "gateway_ready_ack":
            accepted = bool(message.get("accepted"))
            self._protocol_ready = accepted
            await self.state.update_gateway(
                protocol_ready=accepted,
                output_status=(
                    "disabled_no_positions" if accepted else "protocol_error"
                ),
                last_error=None if accepted else "GATEWAY_READY_REJECTED",
            )
        elif message_type == "rgb_ack":
            await self.state.update_gateway(
                last_ack_seq=int(message.get("seq", 0)),
                last_ack_at=now,
                frames_acked=(await self.state.gateway_status())["frames_acked"] + 1,
            )
        elif message_type == "wifi_progress":
            await self.state.update_gateway(
                wifi={
                    "status": message.get("status", "configuring"),
                    "request_id": message.get("request_id"),
                    "updated_at": now,
                }
            )
        elif message_type == "wifi_result":
            await self.state.update_gateway(
                wifi={
                    "status": "connected" if message.get("success") else "failed",
                    "request_id": message.get("request_id"),
                    "ssid": message.get("ssid"),
                    "ip": message.get("ip"),
                    "rssi": message.get("rssi"),
                    "channel": message.get("channel"),
                    "error_code": message.get("error_code"),
                    "message": message.get("message"),
                    "updated_at": now,
                }
            )
            gateway_ip = message.get("ip")
            if isinstance(gateway_ip, str):
                await self._refresh_server_endpoint(gateway_ip, announce=True)
        elif message_type == "wifi_status":
            await self.state.update_gateway(
                wifi={
                    "status": message.get("status", "unknown"),
                    "ssid": message.get("ssid"),
                    "ip": message.get("ip"),
                    "rssi": message.get("rssi"),
                    "channel": message.get("channel"),
                    "error_code": None,
                    "message": None,
                    "updated_at": now,
                }
            )
            gateway_ip = message.get("ip")
            if isinstance(gateway_ip, str):
                await self._refresh_server_endpoint(gateway_ip, announce=True)
        elif message_type == "provision_broadcast":
            await self.state.update_gateway(
                provision_broadcast={
                    "sequence": message.get("sequence"),
                    "sent": message.get("sent"),
                    "channel": message.get("channel"),
                    "successes": message.get("successes"),
                    "failures": message.get("failures"),
                    "updated_at": now,
                }
            )
        elif message_type == "gateway_status":
            wifi = message.get("wifi")
            status_changes: dict[str, Any] = {}
            if "output_status" in message:
                status_changes["output_status"] = message.get("output_status")
            if "protocol_ready" in message:
                reported_ready = bool(message.get("protocol_ready"))
                self._protocol_ready = reported_ready
                status_changes["protocol_ready"] = reported_ready
            if status_changes:
                await self.state.update_gateway(**status_changes)
            if isinstance(wifi, dict):
                allowed = {
                    key: wifi.get(key)
                    for key in (
                        "status",
                        "ssid",
                        "ip",
                        "rssi",
                        "channel",
                        "error_code",
                        "message",
                    )
                    if key in wifi
                }
                allowed["updated_at"] = now
                await self.state.update_gateway(wifi=allowed)
                gateway_ip = wifi.get("ip")
                if isinstance(gateway_ip, str):
                    await self._refresh_server_endpoint(
                        gateway_ip, announce=True
                    )
            if (
                "broadcast_successes" in message
                or "broadcast_failures" in message
            ):
                await self.state.update_gateway(
                    provision_broadcast={
                        "successes": message.get("broadcast_successes"),
                        "failures": message.get("broadcast_failures"),
                        "updated_at": now,
                    }
                )
        await self.on_state_change()

    async def _endpoint_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.endpoint_check_interval)
            try:
                status = await self.state.gateway_status()
                gateway_ip = status["wifi"].get("ip")
                if not isinstance(gateway_ip, str):
                    continue
                changed = await self._refresh_server_endpoint(
                    gateway_ip, announce=True
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._reconnect.set()
                await self._close_serial()
                await self.state.update_gateway(last_error=str(exc))
                await self.on_state_change()
                continue
            if changed:
                await self.on_state_change()

    async def _refresh_server_endpoint(
        self, gateway_ip: str, *, announce: bool
    ) -> bool:
        candidate = await asyncio.to_thread(self.address_resolver, gateway_ip)
        if candidate is None or candidate == self._server_ipv4:
            return False
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return False
        if not isinstance(address, ipaddress.IPv4Address) or not _is_usable_server_ipv4(address):
            return False
        self._server_ipv4 = str(address)
        await self.state.update_gateway(
            server_ipv4=self._server_ipv4,
            server_port=SERVER_PORT,
        )
        if announce and self._gateway_identified and self._serial is not None:
            await self._send_gateway_ready()
        return True

    async def _send_gateway_ready(self) -> None:
        payload: dict[str, Any] = {
            "type": "gateway_ready",
            "schema_version": "1.0",
            "frame_format": "rgb888_hex",
            "frame_interval_ms": 200,
            "watchdog_ms": 1500,
        }
        if self._server_ipv4 is not None:
            payload["server_ipv4"] = self._server_ipv4
            payload["server_port"] = SERVER_PORT
        await self._send_json(payload)

    async def _frame_loop(self) -> None:
        while self._running:
            started = time.monotonic()
            if self._protocol_ready and self._serial is not None:
                try:
                    await self._send_frame()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self.state.update_gateway(
                        dropped_frames=(
                            await self.state.gateway_status()
                        )["dropped_frames"]
                        + 1,
                        last_error=str(exc),
                    )
                    self._reconnect.set()
                    await self._close_serial()
                    await self.on_state_change()
            remaining = FRAME_INTERVAL_SECONDS - (time.monotonic() - started)
            await asyncio.sleep(max(0.0, remaining))

    async def _send_frame(self, *, force_off: bool = False) -> None:
        swimmers = [] if force_off else await self.state.lighting_swimmers()
        self._sequence += 1
        lane1, lane2 = compose_rgb_frame(
            swimmers,
            drowning_visible=self._sequence % 2 == 1,
        )
        await self._send_json(
            {
                "type": "rgb_frame",
                "schema_version": "1.0",
                "seq": self._sequence,
                "sent_at_ms": int(time.time() * 1000),
                "leds_per_lane": 70,
                "lane1_hex": lane1,
                "lane2_hex": lane2,
            }
        )
        status = await self.state.gateway_status()
        has_lights = lane1 != OFF * 70 or lane2 != OFF * 70
        await self.state.update_gateway(
            last_frame_seq=self._sequence,
            frames_sent=status["frames_sent"] + 1,
            output_status="streaming" if has_lights else "disabled_no_positions",
        )
        await self.on_state_change()

    async def _send_json(self, message: dict[str, Any]) -> None:
        connection = self._serial
        if connection is None:
            raise ConnectionError("串口未连接")
        await self._write_json(connection, message)

    async def _write_json(
        self, connection: SerialConnection, message: dict[str, Any]
    ) -> None:
        encoded = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > LINE_LIMIT:
            raise ValueError("NDJSON 消息超过 4096 字节")
        async with self._write_lock:
            await asyncio.to_thread(connection.write, encoded)
            await asyncio.to_thread(connection.flush)

    async def _close_serial(self) -> None:
        connection = self._serial
        self._serial = None
        if connection is not None:
            try:
                await asyncio.to_thread(connection.close)
            except Exception:
                pass
