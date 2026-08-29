from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import uuid4

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from .models import DeviceAck, DeviceHeartbeat, DeviceHello, SwimmerHeartbeat

LIFEGUARD_HEARTBEAT_INTERVAL_MS = 5000
SWIMMER_HEARTBEAT_INTERVAL_MS = 1000
HELLO_TIMEOUT_SECONDS = 5.0
LIFEGUARD_HEARTBEAT_TIMEOUT_SECONDS = 15.0
SWIMMER_SUSPECTED_SECONDS = 10.0
SWIMMER_RESCUE_SECONDS = 20.0
SWIMMER_RECOVERY_HEARTBEATS = 3
COLLISION_PULSE_COOLDOWN_SECONDS = 2.0
REPLAY_VIBRATION_INTERVAL_SECONDS = 0.35
STATE_RETRY_DELAYS = (0.5, 1.0, 2.0)

ChangeCallback = Callable[[], Awaitable[None]]


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_trusted_device_peer(host: str | None) -> bool:
    if host in {"localhost", "testclient"}:
        return True
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def derive_lifeguard_state(
    snapshot: dict, *, additional_unlocated_count: int = 0
) -> dict:
    lane1_count = 0
    lane2_count = 0
    unlocated_count = additional_unlocated_count
    for swimmer in snapshot.get("swimmers", []):
        if not swimmer.get("online") or swimmer.get("status") not in {
            "suspected_drowning",
            "drowning",
        }:
            continue
        lane = swimmer.get("lane")
        if lane == 1:
            lane1_count += 1
        elif lane == 2:
            lane2_count += 1
        else:
            unlocated_count += 1

    if unlocated_count or (lane1_count and lane2_count):
        display_state = "both_drowning"
        drowning_lanes = [1, 2]
    elif lane1_count:
        display_state = "lane1_drowning"
        drowning_lanes = [1]
    elif lane2_count:
        display_state = "lane2_drowning"
        drowning_lanes = [2]
    else:
        display_state = "safe"
        drowning_lanes = []

    danger = display_state != "safe"
    return {
        "display_state": display_state,
        "drowning_lanes": drowning_lanes,
        "drowning_counts": {"lane1": lane1_count, "lane2": lane2_count},
        "unlocated_count": unlocated_count,
        "vibration": {
            "enabled": danger,
            "pattern": "200ms_on_100ms_off" if danger else "off",
        },
    }


def state_fingerprint(content: dict) -> tuple:
    return (
        content["display_state"],
        content["drowning_counts"]["lane1"],
        content["drowning_counts"]["lane2"],
        content["unlocated_count"],
    )


@dataclass
class DeviceRecord:
    device_id: str
    device_type: str
    firmware_version: str
    boot_id: str | None
    connected: bool
    session_id: str | None
    peer_ip: str | None
    connected_at: str
    last_seen_at: str
    last_heartbeat_at: str | None = None
    uptime_ms: int | None = None
    battery_percent: int | None = None
    last_applied_revision: int | None = None
    pending_revision: int | None = None
    retry_count: int = 0
    last_error: str | None = None
    bound_swimmer_id: str | None = None
    binding_automatic: bool = False
    communication_state: str | None = None
    recovery_heartbeat_count: int = 0
    local_alert_stage: str | None = None
    rescue_expected: bool = False
    rescue_confirmed: bool = False
    rescue_enabled: bool = True
    simulation_requested: bool = False
    simulation_paused: bool = False
    pending_command: str | None = None
    _last_heartbeat_monotonic: float | None = None
    _last_heartbeat_message_id: str | None = None
    _communication_alarm: bool = False
    _manual_rescue_alarm: bool = False
    _last_collision_pulse_monotonic: float | None = None

    def as_dict(self, now: float) -> dict:
        loss_ms = None
        if self.device_type == "swimmer" and self._last_heartbeat_monotonic is not None:
            loss_ms = max(0, int((now - self._last_heartbeat_monotonic) * 1000))
        return {
            "device_id": self.device_id,
            "device_type": self.device_type,
            "firmware_version": self.firmware_version,
            "boot_id": self.boot_id,
            "connected": self.connected,
            "session_id": self.session_id,
            "peer_ip": self.peer_ip,
            "connected_at": self.connected_at,
            "last_seen_at": self.last_seen_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "uptime_ms": self.uptime_ms,
            "battery_percent": self.battery_percent,
            "last_applied_revision": self.last_applied_revision,
            "pending_revision": self.pending_revision,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "bound_swimmer_id": self.bound_swimmer_id,
            "binding_automatic": self.binding_automatic,
            "communication_state": self.communication_state,
            "communication_loss_ms": loss_ms,
            "recovery_heartbeat_count": self.recovery_heartbeat_count,
            "local_alert_stage": self.local_alert_stage,
            "rescue_expected": self.rescue_expected,
            "rescue_confirmed": self.rescue_confirmed,
            "rescue_enabled": self.rescue_enabled,
            "simulation_requested": self.simulation_requested,
            "simulation_paused": self.simulation_paused,
            "pending_command": self.pending_command,
        }


@dataclass
class DeviceSession:
    websocket: WebSocket
    device_id: str
    device_type: str
    session_id: str
    last_heartbeat_monotonic: float
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_task: asyncio.Task | None = None
    pending_message_id: str | None = None
    pending_revision: int | None = None
    pending_kind: str | None = None
    pending_context: dict = field(default_factory=dict)
    pending_ack: asyncio.Event | None = None
    closed: bool = False


class DeviceHub:
    def __init__(
        self,
        on_change: ChangeCallback,
        *,
        hello_timeout: float = HELLO_TIMEOUT_SECONDS,
        heartbeat_timeout: float = LIFEGUARD_HEARTBEAT_TIMEOUT_SECONDS,
        swimmer_suspected_timeout: float = SWIMMER_SUSPECTED_SECONDS,
        swimmer_rescue_timeout: float = SWIMMER_RESCUE_SECONDS,
        retry_delays: tuple[float, ...] = STATE_RETRY_DELAYS,
        watchdog_interval: float = 1.0,
    ) -> None:
        if not retry_delays:
            raise ValueError("retry_delays must not be empty")
        if swimmer_rescue_timeout <= swimmer_suspected_timeout:
            raise ValueError("rescue timeout must exceed suspected timeout")
        self.on_change = on_change
        self.hello_timeout = hello_timeout
        self.heartbeat_timeout = heartbeat_timeout
        self.swimmer_suspected_timeout = swimmer_suspected_timeout
        self.swimmer_rescue_timeout = swimmer_rescue_timeout
        self.retry_delays = retry_delays
        self.watchdog_interval = watchdog_interval
        self.server_instance_id = f"server-{uuid4().hex[:12]}"
        self._lock = asyncio.Lock()
        self._sessions: dict[str, DeviceSession] = {}
        self._records: dict[str, DeviceRecord] = {}
        self._revision = 1
        self._content = derive_lifeguard_state({"swimmers": []})
        self._fingerprint = state_fingerprint(self._content)
        self._generated_at = utc_iso()
        self._watchdog_task: asyncio.Task | None = None
        self._replay_vibration_targets: set[str] = set()
        self._replay_vibration_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(), name="device-heartbeat-watchdog"
            )

    async def stop(self) -> None:
        await self.set_replay_vibration(set())
        task = self._watchdog_task
        self._watchdog_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            for record in self._records.values():
                record.connected = False
                record.session_id = None
        for session in sessions:
            await self._close_session(session, 1001, "server shutdown")

    async def auto_bind_single_swimmer(
        self, swimmer_id: str, snapshot: dict
    ) -> str | None:
        swimmers = {item["id"]: item for item in snapshot.get("swimmers", [])}
        if swimmer_id not in swimmers:
            return None
        async with self._lock:
            connected = [
                record
                for record in self._records.values()
                if record.device_type == "swimmer" and record.connected
            ]
            if len(connected) != 1:
                for record in self._records.values():
                    if record.binding_automatic:
                        record.bound_swimmer_id = None
                        record.binding_automatic = False
                return None
            selected = connected[0]
            for record in self._records.values():
                if (
                    record.device_type == "swimmer"
                    and record.device_id != selected.device_id
                    and record.bound_swimmer_id == swimmer_id
                ):
                    record.bound_swimmer_id = None
                    record.binding_automatic = False
            selected.bound_swimmer_id = swimmer_id
            selected.binding_automatic = True
            selected.simulation_requested = False
            return selected.device_id

    async def set_replay_vibration(self, swimmer_ids: set[str]) -> None:
        normalized = set(swimmer_ids)
        task_to_cancel: asyncio.Task | None = None
        async with self._lock:
            if normalized == self._replay_vibration_targets:
                if (
                    normalized
                    and (
                        self._replay_vibration_task is None
                        or self._replay_vibration_task.done()
                    )
                ):
                    self._replay_vibration_task = asyncio.create_task(
                        self._replay_vibration_loop(),
                        name="replay-swimmer-vibration",
                    )
                return
            self._replay_vibration_targets = normalized
            if not normalized:
                task_to_cancel = self._replay_vibration_task
                self._replay_vibration_task = None
            elif (
                self._replay_vibration_task is None
                or self._replay_vibration_task.done()
            ):
                self._replay_vibration_task = asyncio.create_task(
                    self._replay_vibration_loop(),
                    name="replay-swimmer-vibration",
                )
        if task_to_cancel is not None and task_to_cancel is not asyncio.current_task():
            task_to_cancel.cancel()
            await asyncio.gather(task_to_cancel, return_exceptions=True)

    async def wait_for_swimmer_idle(
        self, device_id: str, *, timeout: float = 4.0
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            async with self._lock:
                record = self._require_swimmer_device_locked(device_id)
                if record.pending_command is None:
                    return
            await asyncio.sleep(0.05)
        raise HTTPException(
            409,
            detail={
                "code": "SWIMMER_COMMAND_BUSY",
                "message": "swimmer device did not become idle",
            },
        )

    async def wait_for_rescue_reset(
        self, device_id: str, *, timeout: float = 8.0
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            async with self._lock:
                record = self._require_swimmer_device_locked(device_id)
                if record.pending_command is None:
                    return bool(
                        not record.rescue_expected
                        and not record.rescue_confirmed
                        and record.last_error is None
                    )
            await asyncio.sleep(0.05)
        return False

    async def _replay_vibration_loop(self) -> None:
        try:
            while True:
                sent = False
                async with self._lock:
                    targets = set(self._replay_vibration_targets)
                    if not targets:
                        return
                    for record in self._records.values():
                        if (
                            record.device_type != "swimmer"
                            or not record.connected
                            or record.bound_swimmer_id not in targets
                            or record.pending_command is not None
                            or record.rescue_expected
                        ):
                            continue
                        if self._schedule_swimmer_control_locked(
                            record, collision_pulse=True
                        ) is not None:
                            sent = True
                if sent:
                    await self._notify_change()
                await asyncio.sleep(REPLAY_VIBRATION_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    async def publish_from_snapshot(
        self, snapshot: dict, *, additional_unlocated_count: int = 0
    ) -> bool:
        content = derive_lifeguard_state(
            snapshot, additional_unlocated_count=additional_unlocated_count
        )
        fingerprint = state_fingerprint(content)
        async with self._lock:
            if fingerprint == self._fingerprint:
                return False
            self._fingerprint = fingerprint
            self._content = content
            self._revision += 1
            self._generated_at = utc_iso()
            sessions = [
                session
                for session in self._sessions.values()
                if session.device_type == "lifeguard_band"
            ]
        for session in sessions:
            self._schedule_state(session)
        return True

    async def alarm_projection(self, snapshot: dict) -> tuple[set[str], int]:
        swimmers = {item["id"]: item for item in snapshot.get("swimmers", [])}
        targets: set[str] = set()
        unlocated = 0
        async with self._lock:
            alarms = [
                record
                for record in self._records.values()
                if record.device_type == "swimmer"
                and (record._communication_alarm or record._manual_rescue_alarm)
            ]
            for record in alarms:
                target = swimmers.get(record.bound_swimmer_id or "")
                if target is not None and target.get("online"):
                    targets.add(target["id"])
                else:
                    unlocated += 1
        return targets, unlocated

    async def set_binding(
        self,
        device_id: str,
        swimmer_id: str,
        snapshot: dict,
        *,
        rescue_enabled: bool = True,
    ) -> dict:
        swimmers = {item["id"]: item for item in snapshot.get("swimmers", [])}
        target = swimmers.get(swimmer_id)
        if target is None:
            raise HTTPException(404, detail={"code": "SWIMMER_NOT_FOUND"})
        async with self._lock:
            record = self._require_swimmer_device_locked(device_id)
            conflict = next(
                (
                    item.device_id
                    for item in self._records.values()
                    if item.device_type == "swimmer"
                    and item.device_id != device_id
                    and item.bound_swimmer_id == swimmer_id
                ),
                None,
            )
            if conflict:
                raise HTTPException(
                    409,
                    detail={"code": "SWIMMER_ALREADY_BOUND", "device_id": conflict},
                )
            requested_simulation = False
            control_changed = (
                record.rescue_enabled != rescue_enabled
                or record.simulation_requested != requested_simulation
            )
            record.bound_swimmer_id = swimmer_id
            record.binding_automatic = False
            record.rescue_enabled = rescue_enabled
            record.simulation_requested = requested_simulation
            if control_changed:
                self._schedule_swimmer_control_locked(record)
            result = record.as_dict(time.monotonic())
        await self._notify_change()
        return result

    async def clear_binding(self, device_id: str) -> None:
        async with self._lock:
            record = self._require_swimmer_device_locked(device_id)
            control_changed = (
                not record.rescue_enabled or record.simulation_requested
            )
            record.bound_swimmer_id = None
            record.binding_automatic = False
            record.rescue_enabled = True
            record.simulation_requested = False
            if control_changed:
                self._schedule_swimmer_control_locked(record)
        await self._notify_change()

    async def sync_virtual_simulation(
        self,
        swimmer_id: str,
        paused: bool,
        *,
        require_connected: bool = False,
    ) -> None:
        async with self._lock:
            matching = [
                record
                for record in self._records.values()
                if record.device_type == "swimmer"
                and record.bound_swimmer_id == swimmer_id
            ]
            if paused and require_connected and not any(
                record.connected for record in matching
            ):
                raise HTTPException(
                    409,
                    detail={
                        "code": "SIMULATION_REQUIRES_CONNECTED_PAIR",
                        "message": (
                            "the swimmer must be paired with a connected device "
                            "before simulating signal loss"
                        ),
                    },
                )
            for record in matching:
                if record.simulation_requested != paused:
                    record.simulation_requested = paused
                    self._schedule_swimmer_control_locked(record)
        await self._notify_change()

    async def pulse_collision_warning(self, swimmer_ids: set[str]) -> list[str]:
        now = time.monotonic()
        notified: list[str] = []
        async with self._lock:
            for record in self._records.values():
                if (
                    record.device_type != "swimmer"
                    or not record.connected
                    or record.bound_swimmer_id not in swimmer_ids
                ):
                    continue
                last_pulse = record._last_collision_pulse_monotonic
                if (
                    last_pulse is not None
                    and now - last_pulse < COLLISION_PULSE_COOLDOWN_SECONDS
                ):
                    continue
                session = self._sessions.get(record.device_id)
                if session is None or session.closed:
                    continue
                if record.pending_command == "rescue_reset_command":
                    continue
                preserve_rescue_trigger = bool(
                    session.pending_kind == "swimmer_control_command"
                    and session.pending_context.get("trigger_rescue")
                )
                message_id = self._schedule_swimmer_control_locked(
                    record,
                    trigger_rescue=preserve_rescue_trigger,
                    collision_pulse=True,
                )
                if message_id is not None:
                    notified.append(record.device_id)
        if notified:
            await self._notify_change()
        return notified

    async def clear_virtual_simulations(self, swimmer_ids: set[str]) -> None:
        async with self._lock:
            for record in self._records.values():
                if (
                    record.device_type == "swimmer"
                    and record.bound_swimmer_id in swimmer_ids
                ):
                    if record.simulation_requested:
                        record.simulation_requested = False
                        self._schedule_swimmer_control_locked(record)
        await self._notify_change()

    async def request_rescue_trigger(self, device_id: str) -> dict:
        async with self._lock:
            record = self._require_swimmer_device_locked(device_id)
            session = self._sessions.get(device_id)
            if (
                session is None
                or session.closed
                or not record.connected
                or record.pending_command is not None
            ):
                raise HTTPException(
                    409,
                    detail={
                        "code": "RESCUE_TRIGGER_NOT_ALLOWED",
                        "message": "device must be connected and idle",
                    },
                )
            record._manual_rescue_alarm = True
            record.rescue_expected = True
            record.last_error = None
            message_id = self._schedule_swimmer_control_locked(
                record, trigger_rescue=True
            )
        await self._notify_change()
        return {
            "status": "pending",
            "message_id": message_id,
            "device_id": device_id,
        }

    async def request_rescue_reset(self, device_id: str) -> dict:
        async with self._lock:
            record = self._require_swimmer_device_locked(device_id)
            session = self._sessions.get(device_id)
            eligible = (
                session is not None
                and not session.closed
                and record.connected
                and record.communication_state == "normal"
                and record.recovery_heartbeat_count >= SWIMMER_RECOVERY_HEARTBEATS
                and record.rescue_confirmed
                and record.pending_command is None
            )
            if not eligible:
                raise HTTPException(
                    409,
                    detail={
                        "code": "RESCUE_RESET_NOT_ALLOWED",
                        "message": "device must be connected, healthy, recovered and rescue-latched",
                    },
                )
            message_id = f"reset-{uuid4().hex[:16]}"
            record.pending_command = "rescue_reset_command"
            record.last_error = None
            self._schedule_reliable(
                session,
                kind="rescue_reset_command",
                message={
                    "type": "rescue_reset_command",
                    "schema_version": "1.0",
                    "message_id": message_id,
                    "target_device_id": device_id,
                },
                message_id=message_id,
                revision=None,
                close_on_failure=False,
            )
        await self._notify_change()
        return {"status": "pending", "message_id": message_id, "device_id": device_id}

    async def status(self) -> dict:
        now = time.monotonic()
        async with self._lock:
            records = [
                record.as_dict(now)
                for record in sorted(
                    self._records.values(), key=lambda item: item.device_id
                )
            ]
            lifeguard_state = self._state_payload_base()
        return {
            "server_instance_id": self.server_instance_id,
            "heartbeat_interval_ms": LIFEGUARD_HEARTBEAT_INTERVAL_MS,
            "heartbeat_timeout_ms": int(self.heartbeat_timeout * 1000),
            "swimmer_heartbeat_interval_ms": SWIMMER_HEARTBEAT_INTERVAL_MS,
            "swimmer_suspected_timeout_ms": int(self.swimmer_suspected_timeout * 1000),
            "swimmer_rescue_timeout_ms": int(self.swimmer_rescue_timeout * 1000),
            "connected_count": sum(item["connected"] for item in records),
            "connected_by_type": {
                "lifeguard_band": sum(
                    item["connected"] and item["device_type"] == "lifeguard_band"
                    for item in records
                ),
                "swimmer": sum(
                    item["connected"] and item["device_type"] == "swimmer"
                    for item in records
                ),
            },
            "lifeguard_state": lifeguard_state,
            "devices": records,
        }

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        peer_ip = websocket.client.host if websocket.client else None
        if not is_trusted_device_peer(peer_ip):
            await websocket.close(code=4403, reason="trusted LAN only")
            return
        session: DeviceSession | None = None
        try:
            try:
                hello_text = await asyncio.wait_for(
                    websocket.receive_text(), timeout=self.hello_timeout
                )
            except TimeoutError:
                await websocket.close(code=4408, reason="device_hello timeout")
                return
            try:
                hello = DeviceHello.model_validate_json(hello_text)
            except ValidationError as exc:
                await self._send_unregistered_error(
                    websocket, "INVALID_DEVICE_HELLO", json.loads(exc.json())
                )
                await websocket.close(code=4400, reason="invalid device_hello")
                return

            session, previous = await self._register(websocket, hello)
            if previous is not None:
                await self._close_session(previous, 4009, "replaced by a newer connection")
            interval = (
                SWIMMER_HEARTBEAT_INTERVAL_MS
                if hello.device_type == "swimmer"
                else LIFEGUARD_HEARTBEAT_INTERVAL_MS
            )
            await self._send(
                session,
                {
                    "type": "ack",
                    "schema_version": "1.0",
                    "message_id": hello.message_id,
                    "accepted": True,
                    "session_id": session.session_id,
                    "server_instance_id": self.server_instance_id,
                    "heartbeat_interval_ms": interval,
                },
            )
            if session.device_type == "lifeguard_band":
                self._schedule_state(session)
            else:
                async with self._lock:
                    record = self._records.get(session.device_id)
                    if record is not None and (
                        not record.rescue_enabled
                        or record.simulation_requested
                        or record.simulation_paused
                    ):
                        self._schedule_swimmer_control_locked(record)
            await self._notify_change()

            while True:
                text = await websocket.receive_text()
                await self._handle_registered_message(session, text)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            if session is not None:
                await self._remove_session(session)

    async def _register(
        self, websocket: WebSocket, hello: DeviceHello
    ) -> tuple[DeviceSession, DeviceSession | None]:
        now_iso = utc_iso()
        now = time.monotonic()
        session = DeviceSession(
            websocket=websocket,
            device_id=hello.device_id,
            device_type=hello.device_type,
            session_id=f"session-{uuid4().hex[:12]}",
            last_heartbeat_monotonic=now,
        )
        peer_ip = websocket.client.host if websocket.client else None
        async with self._lock:
            previous = self._sessions.get(hello.device_id)
            self._sessions[hello.device_id] = session
            record = self._records.get(hello.device_id)
            if record is None or record.device_type != hello.device_type:
                record = DeviceRecord(
                    device_id=hello.device_id,
                    device_type=hello.device_type,
                    firmware_version=hello.firmware_version,
                    boot_id=hello.boot_id,
                    connected=True,
                    session_id=session.session_id,
                    peer_ip=peer_ip,
                    connected_at=now_iso,
                    last_seen_at=now_iso,
                    communication_state=(
                        "normal" if hello.device_type == "swimmer" else None
                    ),
                    _last_heartbeat_monotonic=(
                        now if hello.device_type == "swimmer" else None
                    ),
                )
                self._records[hello.device_id] = record
            else:
                previous_boot_id = record.boot_id
                record.firmware_version = hello.firmware_version
                record.boot_id = hello.boot_id
                record.connected = True
                record.session_id = session.session_id
                record.peer_ip = peer_ip
                record.connected_at = now_iso
                record.last_seen_at = now_iso
                record.last_error = None
                if hello.device_type == "swimmer":
                    record.recovery_heartbeat_count = 0
                    if previous_boot_id != hello.boot_id:
                        record._last_heartbeat_message_id = None
        return session, previous

    async def _handle_registered_message(
        self, session: DeviceSession, text: str
    ) -> None:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            await self._send_error(session, "INVALID_JSON", None)
            return
        if not isinstance(raw, dict):
            await self._send_error(session, "INVALID_MESSAGE", None)
            return

        message_type = raw.get("type")
        if message_type == "ack":
            try:
                ack = DeviceAck.model_validate(raw)
            except ValidationError as exc:
                await self._send_error(
                    session, "INVALID_ACK", raw.get("message_id"), json.loads(exc.json())
                )
                return
            await self._accept_command_ack(session, ack)
            return
        if message_type == "device_heartbeat":
            if session.device_type != "lifeguard_band":
                await self._send_error(
                    session, "MESSAGE_NOT_ALLOWED_FOR_DEVICE_TYPE", raw.get("message_id")
                )
                return
            try:
                heartbeat = DeviceHeartbeat.model_validate(raw)
            except ValidationError as exc:
                await self._send_error(
                    session,
                    "INVALID_HEARTBEAT",
                    raw.get("message_id"),
                    json.loads(exc.json()),
                )
                return
            await self._accept_lifeguard_heartbeat(session, heartbeat)
            return
        if message_type == "swimmer_heartbeat":
            if session.device_type != "swimmer":
                await self._send_error(
                    session, "MESSAGE_NOT_ALLOWED_FOR_DEVICE_TYPE", raw.get("message_id")
                )
                return
            try:
                heartbeat = SwimmerHeartbeat.model_validate(raw)
            except ValidationError as exc:
                await self._break_swimmer_recovery(session)
                await self._send_error(
                    session,
                    "INVALID_SWIMMER_HEARTBEAT",
                    raw.get("message_id"),
                    json.loads(exc.json()),
                )
                return
            await self._accept_swimmer_heartbeat(session, heartbeat)
            return
        await self._send_error(session, "UNKNOWN_MESSAGE_TYPE", raw.get("message_id"))

    async def _accept_lifeguard_heartbeat(
        self, session: DeviceSession, heartbeat: DeviceHeartbeat
    ) -> None:
        if heartbeat.device_id != session.device_id:
            await self._break_swimmer_recovery(session)
            await self._send_error(session, "DEVICE_ID_MISMATCH", heartbeat.message_id)
            return
        received_at = utc_iso()
        session.last_heartbeat_monotonic = time.monotonic()
        async with self._lock:
            if self._sessions.get(session.device_id) is not session:
                return
            record = self._records[session.device_id]
            record.last_seen_at = received_at
            record.last_heartbeat_at = received_at
            record.uptime_ms = heartbeat.uptime_ms
            record.battery_percent = heartbeat.battery_percent
            if heartbeat.last_applied_revision is not None:
                record.last_applied_revision = heartbeat.last_applied_revision
        await self._send_heartbeat_ack(session, heartbeat.message_id, received_at)
        await self._notify_change()

    async def _accept_swimmer_heartbeat(
        self, session: DeviceSession, heartbeat: SwimmerHeartbeat
    ) -> None:
        if heartbeat.device_id != session.device_id:
            await self._send_error(session, "DEVICE_ID_MISMATCH", heartbeat.message_id)
            return
        received_at = utc_iso()
        now = time.monotonic()
        changed = False
        async with self._lock:
            if self._sessions.get(session.device_id) is not session:
                return
            record = self._records[session.device_id]
            unique = record._last_heartbeat_message_id != heartbeat.message_id
            session.last_heartbeat_monotonic = now
            record._last_heartbeat_monotonic = now
            record.last_seen_at = received_at
            record.last_heartbeat_at = received_at
            record.uptime_ms = heartbeat.uptime_ms
            record.local_alert_stage = heartbeat.local_alert_stage
            if heartbeat.local_alert_stage == "rescue_triggered":
                record.rescue_confirmed = True
            if unique:
                record._last_heartbeat_message_id = heartbeat.message_id
                if record._communication_alarm:
                    record.recovery_heartbeat_count += 1
                    if record.recovery_heartbeat_count >= SWIMMER_RECOVERY_HEARTBEATS:
                        record.recovery_heartbeat_count = SWIMMER_RECOVERY_HEARTBEATS
                        record._communication_alarm = False
                        record.communication_state = "normal"
                    else:
                        record.communication_state = "recovering"
                    changed = True
                elif record.recovery_heartbeat_count < SWIMMER_RECOVERY_HEARTBEATS:
                    record.recovery_heartbeat_count += 1
                    changed = True
            record.last_error = None
        await self._send_heartbeat_ack(session, heartbeat.message_id, received_at)
        if changed or heartbeat.local_alert_stage == "rescue_triggered":
            await self._notify_change()

    async def _break_swimmer_recovery(self, session: DeviceSession) -> None:
        changed = False
        async with self._lock:
            record = self._records.get(session.device_id)
            if (
                record is not None
                and record._communication_alarm
                and record.recovery_heartbeat_count != 0
            ):
                record.recovery_heartbeat_count = 0
                record.communication_state = (
                    "rescue_triggered"
                    if record.rescue_expected
                    else "suspected_drowning"
                )
                changed = True
        if changed:
            await self._notify_change()

    async def _send_heartbeat_ack(
        self, session: DeviceSession, message_id: str, received_at: str
    ) -> None:
        await self._send(
            session,
            {
                "type": "ack",
                "schema_version": "1.0",
                "message_id": message_id,
                "accepted": True,
                "received_at": received_at,
            },
        )

    async def _accept_command_ack(self, session: DeviceSession, ack: DeviceAck) -> None:
        if ack.message_id != session.pending_message_id:
            return
        kind = session.pending_kind
        context = dict(session.pending_context)
        if session.pending_ack is not None:
            session.pending_ack.set()
        now = utc_iso()
        disconnect = False
        async with self._lock:
            record = self._records.get(session.device_id)
            if record is not None:
                record.last_seen_at = now
                record.retry_count = 0
                if kind == "lifeguard_state":
                    if ack.accepted:
                        record.last_applied_revision = (
                            ack.applied_revision
                            if ack.applied_revision is not None
                            else session.pending_revision
                        )
                        record.pending_revision = None
                        record.last_error = None
                    else:
                        record.last_error = "STATE_REJECTED"
                        disconnect = True
                elif kind == "rescue_reset_command":
                    record.pending_command = None
                    if ack.accepted:
                        record.rescue_expected = False
                        record.rescue_confirmed = False
                        record._manual_rescue_alarm = False
                        record.local_alert_stage = "normal"
                        record.last_error = None
                    else:
                        record.last_error = "RESCUE_RESET_REJECTED"
                elif kind == "swimmer_control_command":
                    record.pending_command = None
                    if ack.accepted:
                        record.simulation_paused = bool(
                            context.get("simulation_paused", False)
                        )
                        if context.get("collision_pulse"):
                            record._last_collision_pulse_monotonic = time.monotonic()
                        record.last_error = None
                    else:
                        if context.get("trigger_rescue"):
                            record._manual_rescue_alarm = False
                            record.rescue_expected = False
                        record.last_error = "SWIMMER_CONTROL_REJECTED"
        await self._notify_change()
        if disconnect:
            await self._disconnect(session, 4400, "state rejected")

    def _schedule_state(self, session: DeviceSession) -> None:
        if session.closed or session.device_type != "lifeguard_band":
            return
        message_id = (
            f"lg-{self.server_instance_id.removeprefix('server-')}-"
            f"{self._revision}-{uuid4().hex[:6]}"
        )
        self._schedule_reliable(
            session,
            kind="lifeguard_state",
            message=self._state_message(session.device_id, message_id),
            message_id=message_id,
            revision=self._revision,
            close_on_failure=True,
        )

    def _schedule_swimmer_control_locked(
        self,
        record: DeviceRecord,
        *,
        trigger_rescue: bool = False,
        collision_pulse: bool = False,
    ) -> str | None:
        session = self._sessions.get(record.device_id)
        if session is None or session.closed or not record.connected:
            return None
        message_id = f"control-{uuid4().hex[:16]}"
        record.pending_command = "swimmer_control_command"
        self._schedule_reliable(
            session,
            kind="swimmer_control_command",
            message={
                "type": "swimmer_control_command",
                "schema_version": "1.0",
                "message_id": message_id,
                "target_device_id": record.device_id,
                "rescue_enabled": record.rescue_enabled,
                "simulation_paused": record.simulation_requested,
                "trigger_rescue": trigger_rescue,
                "collision_pulse": collision_pulse,
            },
            message_id=message_id,
            revision=None,
            close_on_failure=False,
            context={
                "simulation_paused": record.simulation_requested,
                "trigger_rescue": trigger_rescue,
                "collision_pulse": collision_pulse,
            },
        )
        return message_id

    def _schedule_reliable(
        self,
        session: DeviceSession,
        *,
        kind: str,
        message: dict,
        message_id: str,
        revision: int | None,
        close_on_failure: bool,
        context: dict | None = None,
    ) -> None:
        if session.pending_task is not None:
            session.pending_task.cancel()
        session.pending_message_id = message_id
        session.pending_revision = revision
        session.pending_kind = kind
        session.pending_context = dict(context or {})
        session.pending_ack = asyncio.Event()
        session.pending_task = asyncio.create_task(
            self._deliver_reliable(
                session,
                message,
                message_id,
                revision,
                kind,
                session.pending_ack,
                close_on_failure,
            ),
            name=f"{kind}-{session.device_id}",
        )

    async def _deliver_reliable(
        self,
        session: DeviceSession,
        message: dict,
        message_id: str,
        revision: int | None,
        kind: str,
        ack_event: asyncio.Event,
        close_on_failure: bool,
    ) -> None:
        try:
            attempts = len(self.retry_delays) + 1
            for attempt in range(attempts):
                if session.pending_message_id != message_id or session.closed:
                    return
                async with self._lock:
                    record = self._records.get(session.device_id)
                    if record is not None:
                        record.pending_revision = revision
                        record.retry_count = attempt
                await self._send(session, message)
                await self._notify_change()
                timeout = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                try:
                    await asyncio.wait_for(ack_event.wait(), timeout=timeout)
                    return
                except TimeoutError:
                    continue
            async with self._lock:
                record = self._records.get(session.device_id)
                if record is not None:
                    record.pending_command = None
                    record.last_error = (
                        "STATE_ACK_TIMEOUT"
                        if kind == "lifeguard_state"
                        else (
                            "RESCUE_RESET_ACK_TIMEOUT"
                            if kind == "rescue_reset_command"
                            else "SWIMMER_CONTROL_ACK_TIMEOUT"
                        )
                    )
                    if kind == "swimmer_control_command" and session.pending_context.get(
                        "trigger_rescue"
                    ):
                        record._manual_rescue_alarm = False
                        record.rescue_expected = False
            await self._notify_change()
            if close_on_failure:
                await self._disconnect(session, 4408, f"{kind} ack timeout")
        except asyncio.CancelledError:
            return
        except Exception:
            if close_on_failure:
                await self._disconnect(session, 1011, "device send failed")
        finally:
            if session.pending_message_id == message_id:
                session.pending_message_id = None
                session.pending_revision = None
                session.pending_kind = None
                session.pending_context = {}
                session.pending_ack = None
                session.pending_task = None

    def _state_payload_base(self) -> dict:
        return {
            "type": "lifeguard_state",
            "schema_version": "1.0",
            "server_instance_id": self.server_instance_id,
            "state_revision": self._revision,
            "generated_at": self._generated_at,
            **self._content,
        }

    def _state_message(self, device_id: str, message_id: str) -> dict:
        return {
            **self._state_payload_base(),
            "message_id": message_id,
            "target_device_id": device_id,
        }

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self.watchdog_interval)
            now = time.monotonic()
            changed = False
            async with self._lock:
                expired_lifeguards = [
                    session
                    for session in self._sessions.values()
                    if session.device_type == "lifeguard_band"
                    and now - session.last_heartbeat_monotonic >= self.heartbeat_timeout
                ]
                for record in self._records.values():
                    if (
                        record.device_type != "swimmer"
                        or record._last_heartbeat_monotonic is None
                    ):
                        continue
                    loss = now - record._last_heartbeat_monotonic
                    if loss >= self.swimmer_rescue_timeout and record.rescue_enabled:
                        if record.communication_state != "rescue_triggered":
                            record.communication_state = "rescue_triggered"
                            record._communication_alarm = True
                            record.rescue_expected = True
                            record.recovery_heartbeat_count = 0
                            changed = True
                    elif loss >= self.swimmer_suspected_timeout:
                        if record.communication_state not in {
                            "suspected_drowning",
                            "rescue_triggered",
                        }:
                            record.communication_state = "suspected_drowning"
                            record._communication_alarm = True
                            record.recovery_heartbeat_count = 0
                            changed = True
            for session in expired_lifeguards:
                await self._disconnect(session, 4408, "heartbeat timeout")
            if changed:
                await self._notify_change()

    async def _disconnect(self, session: DeviceSession, code: int, reason: str) -> None:
        await self._remove_session(session, error=reason)
        await self._close_session(session, code, reason)

    async def _remove_session(
        self, session: DeviceSession, *, error: str | None = None
    ) -> None:
        changed = False
        async with self._lock:
            if self._sessions.get(session.device_id) is session:
                self._sessions.pop(session.device_id, None)
                record = self._records.get(session.device_id)
                if record is not None:
                    record.connected = False
                    record.session_id = None
                    record.pending_revision = None
                    record.pending_command = None
                    record.last_error = error
                    record.last_seen_at = utc_iso()
                changed = True
        await self._cancel_pending(session)
        if changed:
            await self._notify_change()

    async def _cancel_pending(self, session: DeviceSession) -> None:
        task = session.pending_task
        session.pending_task = None
        session.pending_message_id = None
        session.pending_revision = None
        session.pending_kind = None
        session.pending_context = {}
        session.pending_ack = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _close_session(self, session: DeviceSession, code: int, reason: str) -> None:
        session.closed = True
        await self._cancel_pending(session)
        with suppress(Exception):
            await session.websocket.close(code=code, reason=reason)

    async def _send(self, session: DeviceSession, payload: dict) -> None:
        async with session.send_lock:
            await session.websocket.send_json(payload)

    async def _send_error(
        self,
        session: DeviceSession,
        code: str,
        message_id: str | None,
        details: object | None = None,
    ) -> None:
        payload = {
            "type": "error",
            "schema_version": "1.0",
            "code": code,
            "message_id": message_id,
        }
        if details is not None:
            payload["details"] = details
        await self._send(session, payload)

    async def _send_unregistered_error(
        self, websocket: WebSocket, code: str, details: object
    ) -> None:
        with suppress(Exception):
            await websocket.send_json(
                {
                    "type": "error",
                    "schema_version": "1.0",
                    "code": code,
                    "details": details,
                }
            )

    def _require_swimmer_device_locked(self, device_id: str) -> DeviceRecord:
        record = self._records.get(device_id)
        if record is None:
            raise HTTPException(404, detail={"code": "DEVICE_NOT_FOUND"})
        if record.device_type != "swimmer":
            raise HTTPException(409, detail={"code": "NOT_A_SWIMMER_DEVICE"})
        return record

    async def _notify_change(self) -> None:
        with suppress(Exception):
            await self.on_change()
