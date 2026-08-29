from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .devices import DeviceHub
from .models import (
    CollisionCommand,
    DeviceBindingRequest,
    DevSwimmerCreate,
    DevSwimmerUpdate,
    GatewayWifiRequest,
    GatewayPortSelection,
    RoiUpdateRequest,
    VisionFrame,
    VisionReplayCommand,
)
from .serial_gateway import SerialFactory, SerialGatewayService, default_serial_factory
from .settings import SettingsStore
from .state import StateStore
from .vision_replay import ReplayCleanup, ReplayFrame, VisionReplayService

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "out"
DEFAULT_SETTINGS_PATH = ROOT / "data" / "settings.json"
MAX_JPEG_BYTES = 8 * 1024 * 1024
REPLAY_LANE_BOUNDARY_KEYFRAMES = {
    "oneline": tuple(
        (frame, (y, y))
        for frame, y in ((0, 0.482), (100, 0.463), (211, 0.456), (300, 0.445), (396, 0.433))
    ),
    "twolines": tuple(
        (frame, (y, y))
        for frame, y in ((0, 0.460), (32, 0.457), (100, 0.444), (221, 0.420), (400, 0.422), (524, 0.402))
    ),
    "crashing": tuple(
        (frame, (y, y))
        for frame, y in ((0, 0.411), (74, 0.401), (124, 0.394), (199, 0.427), (245, 0.484))
    ),
    "drowning": (
        (0, (0.258, 0.238)),
        (76, (0.281, 0.254)),
        (120, (0.258, 0.292)),
        (213, (0.241, 0.275)),
        (334, (0.198, 0.256)),
        (489, (0.239, 0.264)),
        (550, (0.252, 0.265)),
        (629, (0.278, 0.311)),
        (650, (0.302, 0.315)),
        (670, (0.332, 0.282)),
        (680, (0.336, 0.242)),
        (690, (0.308, 0.220)),
        (700, (0.290, 0.174)),
        (710, (0.238, 0.107)),
        (716, (0.198, 0.046)),
    ),
}


def replay_lane_boundary(sample_id: str, frame_index: int) -> tuple[float, float]:
    keyframes = REPLAY_LANE_BOUNDARY_KEYFRAMES[sample_id]
    if frame_index <= keyframes[0][0]:
        return keyframes[0][1]
    for (left_frame, left_line), (right_frame, right_line) in zip(
        keyframes, keyframes[1:]
    ):
        if frame_index <= right_frame:
            progress = (frame_index - left_frame) / (right_frame - left_frame)
            return tuple(
                left_y + (right_y - left_y) * progress
                for left_y, right_y in zip(left_line, right_line)
            )
    return keyframes[-1][1]


class DashboardHub:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.connections.add(websocket)

    async def remove(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.connections.discard(websocket)

    async def broadcast_json(self, payload: dict) -> None:
        await self._broadcast(lambda ws: ws.send_json(payload))

    async def broadcast_bytes(self, payload: bytes) -> None:
        await self._broadcast(lambda ws: ws.send_bytes(payload))

    async def _broadcast(self, sender) -> None:
        async with self._lock:
            connections = tuple(self.connections)
        failed: list[WebSocket] = []
        for websocket in connections:
            try:
                await asyncio.wait_for(sender(websocket), timeout=0.5)
            except Exception:
                failed.append(websocket)
        if failed:
            async with self._lock:
                for websocket in failed:
                    self.connections.discard(websocket)


def require_localhost(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(
            403,
            detail={
                "code": "LOCALHOST_ONLY",
                "message": "该修改操作只能在服务器本机执行",
            },
        )


def create_app(
    *,
    settings_path: Path | None = None,
    enable_serial: bool = True,
    serial_factory: SerialFactory = default_serial_factory,
    serial_port_provider=None,
    device_hub_options: dict | None = None,
    replay_project_root: Path | None = None,
    replay_playback_rate: float = 1.0,
) -> FastAPI:
    state = StateStore(SettingsStore(settings_path or DEFAULT_SETTINGS_PATH))
    hub = DashboardHub()
    reconcile_lock = asyncio.Lock()
    vision_source_lock = asyncio.Lock()
    external_vision_active = False
    replay_reset_tasks: set[asyncio.Task] = set()
    replay_resetting_devices: set[str] = set()

    async def system_snapshot(base: dict | None = None) -> dict:
        snapshot = base if base is not None else await state.snapshot()
        device_status = await devices.status()
        snapshot["schema_version"] = "1.0"
        snapshot["device_server"] = {
            key: device_status[key]
            for key in (
                "server_instance_id",
                "heartbeat_interval_ms",
                "heartbeat_timeout_ms",
                "swimmer_heartbeat_interval_ms",
                "swimmer_suspected_timeout_ms",
                "swimmer_rescue_timeout_ms",
                "connected_count",
                "connected_by_type",
            )
        }
        snapshot["lifeguard_state"] = device_status["lifeguard_state"]
        snapshot["devices"] = device_status["devices"]
        snapshot["vision_replay"] = await replay.snapshot()
        return snapshot

    async def reconcile_and_broadcast() -> None:
        async with reconcile_lock:
            snapshot = await state.snapshot()
            replay_snapshot = await replay.snapshot()
            if replay_snapshot["sample_id"] is not None:
                await devices.auto_bind_single_swimmer("vision-1", snapshot)
            targets, unlocated_count = await devices.alarm_projection(snapshot)
            await state.set_device_alarm_targets(targets)
            snapshot = await state.snapshot()
            await devices.publish_from_snapshot(
                snapshot, additional_unlocated_count=unlocated_count
            )
            await hub.broadcast_json(await system_snapshot(snapshot))

    devices = DeviceHub(
        reconcile_and_broadcast,
        **(device_hub_options or {}),
    )

    async def broadcast_state() -> None:
        await reconcile_and_broadcast()

    gateway = SerialGatewayService(
        state,
        broadcast_state,
        serial_factory=serial_factory,
        **({"port_provider": serial_port_provider} if serial_port_provider else {}),
    )

    async def accept_replay_frame(frame: ReplayFrame) -> None:
        metadata = VisionFrame.model_validate(frame.metadata)
        lane_line = replay_lane_boundary(frame.sample_id, frame.frame_index)
        await state.apply_vision_frame(
            metadata,
            frame.jpeg,
            lane_boundary_line=lane_line,
        )
        await hub.broadcast_json(metadata.model_dump(mode="json"))
        await hub.broadcast_bytes(frame.jpeg)

    async def clear_replay_stream(_cleanup: ReplayCleanup) -> None:
        await devices.set_replay_vibration(set())
        await state.clear_vision_stream()

    async def replay_state_changed(replay_snapshot: dict) -> None:
        alert_stage = replay_snapshot["alert_stage"]
        await state.set_replay_alert_stage(alert_stage)
        targets: set[str] = set()
        if (
            replay_snapshot["device_feedback_enabled"]
            and not replay_resetting_devices
        ):
            if alert_stage != "normal":
                targets.add("vision-1")
            for pair in replay_snapshot["active_collision_pairs"]:
                targets.update(f"vision-{track_id}" for track_id in pair)
        await devices.set_replay_vibration(targets)
        await broadcast_state()

    replay = VisionReplayService(
        project_root=replay_project_root,
        on_frame=accept_replay_frame,
        on_cleanup=clear_replay_stream,
        on_state_change=replay_state_changed,
        playback_rate=replay_playback_rate,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await devices.start()
        await broadcast_state()
        if enable_serial:
            await gateway.start()
        try:
            yield
        finally:
            await replay.stop()
            for task in tuple(replay_reset_tasks):
                task.cancel()
            if replay_reset_tasks:
                await asyncio.gather(*replay_reset_tasks, return_exceptions=True)
            if enable_serial:
                await gateway.stop()
            await devices.stop()

    application = FastAPI(
        title="冠影守望者电脑端",
        version="0.2.0",
        description="泳池视觉、RGB 网关、状态管理和开发测试后台",
        lifespan=lifespan,
    )
    application.state.state_store = state
    application.state.gateway_service = gateway
    application.state.device_hub = devices
    application.state.vision_replay = replay

    @application.get("/api/health")
    async def health() -> dict:
        snapshot = await system_snapshot()
        return {
            "status": "ok",
            "vision_connected": snapshot["vision_connected"],
            "gateway_connected": snapshot["rgb_gateway"]["serial_connected"],
            "gateway_ready": snapshot["rgb_gateway"]["protocol_ready"],
            "roi_configured": snapshot["calibration"]["roi_configured"],
            "devices_connected": snapshot["device_server"]["connected_count"],
            "revision": snapshot["revision"],
        }

    @application.get("/api/state")
    async def get_state() -> dict:
        return await system_snapshot()

    @application.get("/api/devices")
    async def get_devices() -> dict:
        return await devices.status()

    @application.get("/api/vision/replays")
    async def get_vision_replays() -> dict:
        return await replay.samples_payload()

    @application.post(
        "/api/vision/replay/command",
        dependencies=[Depends(require_localhost)],
    )
    async def command_vision_replay(command: VisionReplayCommand) -> dict:
        nonlocal external_vision_active
        if command.action in {"select", "play", "restart"}:
            async with vision_source_lock:
                if external_vision_active:
                    raise HTTPException(
                        409,
                        detail={
                            "code": "VISION_SOURCE_BUSY",
                            "message": "实时视觉输入正在占用单路视觉通道",
                        },
                    )
        try:
            if command.action == "select":
                result = await replay.select(command.sample_id or "")
                await devices.auto_bind_single_swimmer(
                    "vision-1", await state.snapshot()
                )
            elif command.action == "play":
                result = await replay.play()
            elif command.action == "pause":
                result = await replay.pause()
            elif command.action == "restart":
                result = await replay.restart()
            elif command.action == "stop":
                result = await replay.stop()
            else:
                result = await replay.set_feedback(
                    bool(command.device_feedback_enabled)
                )
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            raise HTTPException(
                409,
                detail={"code": "VISION_REPLAY_ERROR", "message": str(exc)},
            ) from exc
        await broadcast_state()
        return result

    @application.put(
        "/api/devices/{device_id}/binding",
        dependencies=[Depends(require_localhost)],
    )
    async def put_device_binding(
        device_id: str, command: DeviceBindingRequest
    ) -> dict:
        result = await devices.set_binding(
            device_id,
            command.swimmer_id,
            await state.snapshot(),
            rescue_enabled=command.rescue_enabled,
        )
        await broadcast_state()
        return result

    @application.delete(
        "/api/devices/{device_id}/binding",
        status_code=204,
        dependencies=[Depends(require_localhost)],
    )
    async def delete_device_binding(device_id: str) -> None:
        await devices.clear_binding(device_id)
        await broadcast_state()

    @application.post(
        "/api/devices/{device_id}/rescue-reset",
        status_code=202,
        dependencies=[Depends(require_localhost)],
    )
    async def reset_device_rescue(device_id: str) -> dict:
        replay_snapshot = await replay.snapshot()
        device_status = await devices.status()
        device = next(
            (item for item in device_status["devices"] if item["device_id"] == device_id),
            None,
        )
        clears_replay = bool(
            replay_snapshot["sample_id"] == "drowning"
            and replay_snapshot["alert_stage"] != "normal"
            and device is not None
            and device["bound_swimmer_id"] == "vision-1"
        )
        if clears_replay:
            replay_resetting_devices.add(device_id)
            await devices.set_replay_vibration(set())
            await devices.wait_for_swimmer_idle(device_id)
        try:
            result = await devices.request_rescue_reset(device_id)
        except Exception:
            replay_resetting_devices.discard(device_id)
            await replay_state_changed(await replay.snapshot())
            raise
        if clears_replay:
            async def clear_after_ack() -> None:
                try:
                    if await devices.wait_for_rescue_reset(device_id):
                        await replay.clear_alert()
                        await state.set_replay_alert_stage("normal")
                finally:
                    replay_resetting_devices.discard(device_id)
                    await replay_state_changed(await replay.snapshot())

            task = asyncio.create_task(
                clear_after_ack(), name=f"replay-rescue-reset-{device_id}"
            )
            replay_reset_tasks.add(task)
            task.add_done_callback(replay_reset_tasks.discard)
        return result

    @application.post(
        "/api/devices/{device_id}/rescue-trigger",
        status_code=202,
        dependencies=[Depends(require_localhost)],
    )
    async def trigger_device_rescue(device_id: str) -> dict:
        replay_snapshot = await replay.snapshot()
        if (
            replay_snapshot["sample_id"] == "drowning"
            and replay_snapshot["alert_stage"] == "high_risk"
        ):
            await devices.set_replay_vibration(set())
            await devices.wait_for_swimmer_idle(device_id)
        try:
            return await devices.request_rescue_trigger(device_id)
        finally:
            # Restore the desired replay target even on command rejection or a
            # later ACK timeout.  While rescue_expected is true the recurring
            # pulse loop intentionally skips the device.
            await replay_state_changed(await replay.snapshot())

    @application.get("/api/calibration/roi")
    async def get_roi() -> dict:
        return await state.get_calibration()

    @application.put(
        "/api/calibration/roi",
        dependencies=[Depends(require_localhost)],
    )
    async def put_roi(command: RoiUpdateRequest) -> dict:
        calibration = await state.set_calibration(command)
        await broadcast_state()
        return calibration

    @application.delete(
        "/api/calibration/roi",
        status_code=204,
        dependencies=[Depends(require_localhost)],
    )
    async def delete_roi() -> None:
        await state.clear_calibration()
        await broadcast_state()

    @application.get("/api/gateway/status")
    async def get_gateway_status() -> dict:
        return await state.gateway_status()

    @application.post(
        "/api/gateway/reconnect",
        status_code=202,
        dependencies=[Depends(require_localhost)],
    )
    async def reconnect_gateway() -> dict:
        await gateway.reconnect()
        status = await state.gateway_status()
        return {
            "status": "reconnecting",
            "port_mode": status["port_mode"],
            "preferred_port": status["preferred_port"],
            "baud_rate": 115200,
        }

    @application.put(
        "/api/gateway/port",
        status_code=202,
        dependencies=[Depends(require_localhost)],
    )
    async def select_gateway_port(command: GatewayPortSelection) -> dict:
        return await gateway.select_port(command.port)

    @application.post(
        "/api/gateway/wifi",
        status_code=202,
        dependencies=[Depends(require_localhost)],
    )
    async def configure_gateway_wifi(command: GatewayWifiRequest) -> dict:
        return await gateway.configure_wifi(command)

    @application.post(
        "/api/dev/swimmers",
        status_code=201,
        dependencies=[Depends(require_localhost)],
    )
    async def create_virtual_swimmer(command: DevSwimmerCreate) -> dict:
        swimmer = await state.create_virtual(command)
        await broadcast_state()
        return swimmer.model_dump(mode="json")

    @application.patch(
        "/api/dev/swimmers/{swimmer_id}",
        dependencies=[Depends(require_localhost)],
    )
    async def update_virtual_swimmer(
        swimmer_id: str, command: DevSwimmerUpdate
    ) -> dict:
        if command.status == "drowning":
            await state.ensure_virtual(swimmer_id)
            await devices.sync_virtual_simulation(
                swimmer_id, True, require_connected=True
            )
            # A simulated submersion is an input failure, not an alarm result.
            # Keep the visual state normal until the heartbeat watchdog expires.
            command = command.model_copy(update={"status": "normal"})
        elif command.status == "normal":
            await devices.sync_virtual_simulation(swimmer_id, False)
        swimmer = await state.update_virtual(swimmer_id, command)
        await broadcast_state()
        return swimmer.model_dump(mode="json")

    @application.delete(
        "/api/dev/swimmers/{swimmer_id}",
        status_code=204,
        dependencies=[Depends(require_localhost)],
    )
    async def delete_virtual_swimmer(swimmer_id: str) -> None:
        await devices.sync_virtual_simulation(swimmer_id, False)
        await state.delete_virtual(swimmer_id)
        await broadcast_state()

    @application.post(
        "/api/dev/collisions",
        status_code=204,
        dependencies=[Depends(require_localhost)],
    )
    async def set_collision(command: CollisionCommand) -> None:
        await state.set_collision(command)
        if command.active:
            await devices.pulse_collision_warning(
                {command.swimmer_a, command.swimmer_b}
            )
        await broadcast_state()

    @application.post(
        "/api/dev/reset",
        status_code=204,
        dependencies=[Depends(require_localhost)],
    )
    async def reset_virtual_swimmers() -> None:
        snapshot = await state.snapshot()
        await devices.clear_virtual_simulations(
            {
                item["id"]
                for item in snapshot["swimmers"]
                if item["source"] == "virtual"
            }
        )
        await state.reset_virtual()
        await broadcast_state()

    @application.websocket("/ws/dashboard")
    async def dashboard_socket(websocket: WebSocket) -> None:
        await hub.add(websocket)
        try:
            await websocket.send_json(await system_snapshot())
            metadata, frame = await state.latest_frame_bundle()
            if metadata and frame:
                await websocket.send_json(metadata.model_dump(mode="json"))
                await websocket.send_bytes(frame)
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            await hub.remove(websocket)

    @application.websocket("/ws/devices")
    async def device_socket(websocket: WebSocket) -> None:
        await devices.handle(websocket)

    @application.websocket("/ws/vision")
    async def vision_socket(websocket: WebSocket) -> None:
        nonlocal external_vision_active
        await websocket.accept()
        async with vision_source_lock:
            replay_snapshot = await replay.snapshot()
            if external_vision_active or replay_snapshot["state"] != "idle":
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "VISION_SOURCE_BUSY",
                        "message": "单路视觉通道正由另一输入源占用",
                    }
                )
                await websocket.close(code=1013, reason="VISION_SOURCE_BUSY")
                return
            external_vision_active = True
        await state.set_vision_connected(True)
        await broadcast_state()
        try:
            while True:
                metadata_message = await websocket.receive()
                if metadata_message["type"] == "websocket.disconnect":
                    break
                metadata_text = metadata_message.get("text")
                if metadata_text is None:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "EXPECTED_METADATA",
                            "message": "每帧必须先发送 JSON 元数据",
                        }
                    )
                    continue
                try:
                    metadata = VisionFrame.model_validate_json(metadata_text)
                except ValidationError as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "INVALID_METADATA",
                            "message": json.loads(exc.json()),
                        }
                    )
                    continue

                frame_message = await websocket.receive()
                jpeg = frame_message.get("bytes")
                if jpeg is None:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "EXPECTED_JPEG",
                            "message": "JSON 元数据之后必须紧接 JPEG 二进制帧",
                        }
                    )
                    continue
                if len(jpeg) > MAX_JPEG_BYTES:
                    await websocket.close(code=1009, reason="JPEG exceeds 8 MiB")
                    return
                if not jpeg.startswith(b"\xff\xd8"):
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "INVALID_JPEG",
                            "message": "二进制消息不是 JPEG",
                        }
                    )
                    continue

                started_pairs = await state.apply_vision_frame(metadata, jpeg)
                if started_pairs:
                    await devices.pulse_collision_warning(
                        {
                            swimmer_id
                            for pair in started_pairs
                            for swimmer_id in pair
                        }
                    )
                await hub.broadcast_json(metadata.model_dump(mode="json"))
                await hub.broadcast_bytes(jpeg)
                await broadcast_state()
                await websocket.send_json(
                    {
                        "type": "vision_ack",
                        "schema_version": "1.0",
                        "frame_id": metadata.frame_id,
                    }
                )
        except WebSocketDisconnect:
            pass
        finally:
            async with vision_source_lock:
                external_vision_active = False
            await state.set_vision_connected(False)
            await broadcast_state()

    @application.get("/api/frame.jpg")
    async def latest_frame() -> None:
        raise HTTPException(
            410, "画面通过 /ws/dashboard 实时传输，不提供静态帧地址"
        )

    if FRONTEND.exists():
        application.mount(
            "/",
            StaticFiles(directory=FRONTEND, html=True),
            name="frontend",
        )
    else:
        @application.get("/")
        async def frontend_not_built() -> dict:
            return {
                "message": "前端尚未构建，请先在 computer 目录运行 npm run build:static"
            }

    return application


app = create_app()
