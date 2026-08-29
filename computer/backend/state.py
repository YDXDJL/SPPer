from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from .lighting import map_frame_position, map_pool_position
from .models import (
    CalibrationState,
    CollisionCommand,
    DevSwimmerCreate,
    DevSwimmerUpdate,
    Position,
    RoiUpdateRequest,
    Swimmer,
    VisionFrame,
)
from .settings import SettingsStore


def default_gateway_state() -> dict:
    return {
        "port": None,
        "port_mode": "auto",
        "preferred_port": None,
        "available_ports": [],
        "scanning_port": None,
        "baud_rate": 115200,
        "serial_connected": False,
        "protocol_ready": False,
        "device_id": None,
        "firmware_version": None,
        "server_ipv4": None,
        "server_port": 8000,
        "last_seen_at": None,
        "last_frame_seq": 0,
        "last_ack_seq": 0,
        "last_ack_at": None,
        "output_status": "starting",
        "frames_sent": 0,
        "frames_acked": 0,
        "dropped_frames": 0,
        "last_error": None,
        "wifi": {
            "status": "unknown",
            "request_id": None,
            "ssid": None,
            "ip": None,
            "rssi": None,
            "channel": None,
            "error_code": None,
            "message": None,
            "updated_at": None,
        },
        "provision_broadcast": {
            "sequence": None,
            "sent": None,
            "channel": None,
            "successes": None,
            "failures": None,
            "updated_at": None,
        },
    }


class StateStore:
    def __init__(self, settings: SettingsStore) -> None:
        self._lock = asyncio.Lock()
        self._settings = settings
        self._swimmers: dict[str, Swimmer] = {}
        self._revision = 0
        self._vision_connected = False
        manual_calibration = settings.load().calibration
        manual_calibration.valid_for_current_stream = False
        self._manual_calibration = manual_calibration
        self._calibration = manual_calibration.model_copy(deep=True)
        self._gateway = default_gateway_state()
        self._latest_frame: bytes | None = None
        self._latest_frame_metadata: VisionFrame | None = None
        self._recent_frames: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._device_alarm_targets: set[str] = set()
        self._vision_collision_pairs: set[tuple[str, str]] = set()
        self._replay_alert_stage: str = "normal"

    async def snapshot(self) -> dict:
        async with self._lock:
            swimmers = sorted(
                (item.model_copy(deep=True) for item in self._swimmers.values()),
                key=lambda swimmer: (not swimmer.online, swimmer.display_name),
            )
            for swimmer in swimmers:
                if swimmer.id in self._device_alarm_targets and swimmer.online:
                    swimmer.status = "drowning"
                    swimmer.collision_with = None
            online = [swimmer for swimmer in swimmers if swimmer.online]
            return {
                "type": "state",
                "schema_version": "1.0",
                "revision": self._revision,
                "vision_connected": self._vision_connected,
                "calibration": self._calibration.model_dump(mode="json"),
                "rgb_gateway": deepcopy(self._gateway),
                "swimmers": [
                    {
                        **swimmer.model_dump(mode="json"),
                        "device_alarm_override": swimmer.id
                        in self._device_alarm_targets,
                    }
                    for swimmer in swimmers
                ],
                "stats": {
                    "total": len(swimmers),
                    "online": len(online),
                    "normal": sum(item.status == "normal" for item in online),
                    "drowning": sum(item.status == "drowning" for item in online),
                    "suspected_drowning": sum(
                        item.status == "suspected_drowning" for item in online
                    ),
                    "collision": sum(item.status == "collision" for item in online),
                },
            }

    async def get_calibration(self) -> dict:
        async with self._lock:
            return self._calibration.model_dump(mode="json")

    async def set_calibration(self, command: RoiUpdateRequest) -> dict:
        roi = command.as_roi()
        async with self._lock:
            if self._latest_frame_metadata is None:
                raise HTTPException(409, detail={"code": "NO_VISION_FRAME"})
            dimensions = self._recent_frames.get(command.frame_id)
            if dimensions is None:
                raise HTTPException(409, detail={"code": "STALE_FRAME"})
            image_width, image_height = dimensions
            calibration = CalibrationState(
                roi_configured=True,
                roi=roi,
                lane_boundary_y=roi.y1 + (roi.y2 - roi.y1) / 2,
                lane_boundary_line={
                    "left_y": roi.y1 + (roi.y2 - roi.y1) / 2,
                    "right_y": roi.y1 + (roi.y2 - roi.y1) / 2,
                },
                mapping_source="manual",
                source_frame_id=command.frame_id,
                source_image_width=image_width,
                source_image_height=image_height,
                updated_at=datetime.now(timezone.utc),
                valid_for_current_stream=True,
            )
            self._settings.save_calibration(calibration)
            self._manual_calibration = calibration.model_copy(deep=True)
            self._calibration = calibration
            self._remap_all_locked()
            self._revision += 1
            return calibration.model_dump(mode="json")

    async def clear_calibration(self) -> None:
        async with self._lock:
            calibration = CalibrationState()
            self._settings.save_calibration(calibration)
            self._manual_calibration = calibration.model_copy(deep=True)
            self._calibration = calibration
            self._remap_all_locked()
            self._revision += 1

    async def latest_frame_bundle(self) -> tuple[VisionFrame | None, bytes | None]:
        async with self._lock:
            metadata = (
                self._latest_frame_metadata.model_copy(deep=True)
                if self._latest_frame_metadata
                else None
            )
            return metadata, self._latest_frame

    async def lighting_swimmers(self) -> list[Swimmer]:
        async with self._lock:
            swimmers = [item.model_copy(deep=True) for item in self._swimmers.values()]
            for swimmer in swimmers:
                if swimmer.id in self._device_alarm_targets and swimmer.online:
                    swimmer.status = "drowning"
                    swimmer.collision_with = None
            return swimmers

    async def set_device_alarm_targets(self, swimmer_ids: set[str]) -> bool:
        async with self._lock:
            normalized = set(swimmer_ids)
            if normalized == self._device_alarm_targets:
                return False
            self._device_alarm_targets = normalized
            self._revision += 1
            return True

    async def gateway_status(self) -> dict:
        async with self._lock:
            return deepcopy(self._gateway)

    async def update_gateway(self, **changes) -> None:
        async with self._lock:
            wifi = changes.pop("wifi", None)
            provision_broadcast = changes.pop("provision_broadcast", None)
            self._gateway.update(changes)
            if wifi is not None:
                self._gateway["wifi"].update(wifi)
            if provision_broadcast is not None:
                self._gateway["provision_broadcast"].update(provision_broadcast)
            self._revision += 1

    async def set_vision_connected(self, connected: bool) -> None:
        async with self._lock:
            if self._vision_connected == connected:
                return
            self._vision_connected = connected
            if not connected:
                self._clear_vision_collisions_locked()
                for swimmer in self._swimmers.values():
                    if swimmer.source == "vision":
                        swimmer.online = False
            self._revision += 1

    async def clear_vision_stream(self, *, clear_frame: bool = True) -> None:
        async with self._lock:
            self._vision_connected = False
            self._replay_alert_stage = "normal"
            self._clear_vision_collisions_locked()
            vision_ids = [
                swimmer.id
                for swimmer in self._swimmers.values()
                if swimmer.source == "vision"
            ]
            for swimmer_id in vision_ids:
                self._clear_collision_locked(swimmer_id)
            for swimmer in self._swimmers.values():
                if swimmer.source == "vision":
                    swimmer.online = False
            if clear_frame:
                self._latest_frame = None
                self._latest_frame_metadata = None
                self._recent_frames.clear()
            self._restore_manual_calibration_locked()
            self._remap_all_locked()
            self._revision += 1

    async def apply_vision_frame(
        self,
        metadata: VisionFrame,
        jpeg: bytes,
        *,
        lane_boundary_y: float | None = None,
        lane_boundary_line: tuple[float, float] | None = None,
    ) -> set[tuple[str, str]]:
        seen: set[str] = set()
        started_pairs: set[tuple[str, str]] = set()
        now = datetime.now(timezone.utc)
        async with self._lock:
            if lane_boundary_y is None and lane_boundary_line is None:
                self._update_calibration_validity_locked(metadata)
            else:
                left_y, right_y = lane_boundary_line or (
                    lane_boundary_y,
                    lane_boundary_y,
                )
                self._calibration = CalibrationState(
                    roi_configured=True,
                    roi={"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0},
                    lane_boundary_y=(left_y + right_y) / 2,
                    lane_boundary_line={"left_y": left_y, "right_y": right_y},
                    mapping_source="replay",
                    source_frame_id=metadata.frame_id,
                    source_image_width=metadata.image_width,
                    source_image_height=metadata.image_height,
                    valid_for_current_stream=True,
                )
            self._recent_frames[metadata.frame_id] = (
                metadata.image_width,
                metadata.image_height,
            )
            self._recent_frames.move_to_end(metadata.frame_id)
            while len(self._recent_frames) > 300:
                self._recent_frames.popitem(last=False)
            for track in metadata.tracks:
                swimmer_id = f"vision-{track.track_id}"
                seen.add(swimmer_id)
                current = self._swimmers.get(swimmer_id)
                frame_position = Position(
                    x=track.center_normalized[0],
                    y=track.center_normalized[1],
                )
                swimmer = Swimmer(
                    id=swimmer_id,
                    display_name=(
                        current.display_name
                        if current
                        else f"视觉泳者 {track.track_id}"
                    ),
                    source="vision",
                    position=frame_position,
                    frame_position=frame_position,
                    status=current.status if current else "normal",
                    collision_with=current.collision_with if current else None,
                    online=True,
                    last_seen_at=now,
                    confidence=track.confidence,
                    bbox_xyxy=track.bbox_xyxy,
                )
                self._map_swimmer_locked(swimmer)
                self._swimmers[swimmer_id] = swimmer
            for swimmer in self._swimmers.values():
                if swimmer.source == "vision" and swimmer.id not in seen:
                    swimmer.online = False

            if metadata.collision_warnings is not None:
                requested_pairs = {
                    tuple(
                        sorted(
                            (
                                f"vision-{warning.track_ids[0]}",
                                f"vision-{warning.track_ids[1]}",
                            )
                        )
                    )
                    for warning in metadata.collision_warnings
                }
                valid_pairs = {
                    pair
                    for pair in requested_pairs
                    if all(
                        swimmer_id in seen
                        and self._swimmers[swimmer_id].online
                        for swimmer_id in pair
                    )
                }
                started_pairs = valid_pairs - self._vision_collision_pairs
                involved_ids = {
                    swimmer_id
                    for pair in self._vision_collision_pairs | valid_pairs
                    for swimmer_id in pair
                }
                for swimmer_id in involved_ids:
                    swimmer = self._swimmers.get(swimmer_id)
                    if swimmer is not None:
                        swimmer.collision_with = None
                        if swimmer.status == "collision":
                            swimmer.status = "normal"
                for swimmer_a_id, swimmer_b_id in sorted(valid_pairs):
                    swimmer_a = self._swimmers[swimmer_a_id]
                    swimmer_b = self._swimmers[swimmer_b_id]
                    swimmer_a.status = "collision"
                    swimmer_b.status = "collision"
                    swimmer_a.collision_with = swimmer_b.id
                    swimmer_b.collision_with = swimmer_a.id
                self._vision_collision_pairs = valid_pairs

            self._apply_replay_alert_locked()

            self._latest_frame_metadata = metadata
            self._latest_frame = jpeg
            self._vision_connected = True
            self._revision += 1
        return started_pairs

    async def set_replay_alert_stage(self, stage: str) -> None:
        if stage not in {"normal", "suspected_drowning", "high_risk"}:
            raise ValueError(f"unsupported replay alert stage: {stage}")
        async with self._lock:
            if self._replay_alert_stage == stage:
                return
            self._replay_alert_stage = stage
            self._apply_replay_alert_locked()
            self._revision += 1

    async def create_virtual(self, command: DevSwimmerCreate) -> Swimmer:
        async with self._lock:
            sequence = (
                sum(item.source == "virtual" for item in self._swimmers.values()) + 1
            )
            swimmer = Swimmer(
                id=f"virtual-{uuid4().hex[:8]}",
                display_name=command.display_name or f"虚拟泳者 {sequence}",
                source="virtual",
                position=command.position,
                pool_position=command.position,
            )
            self._map_swimmer_locked(swimmer)
            self._swimmers[swimmer.id] = swimmer
            self._revision += 1
            return swimmer

    async def ensure_virtual(self, swimmer_id: str) -> None:
        async with self._lock:
            swimmer = self._require_swimmer(swimmer_id)
            if swimmer.source != "virtual":
                raise HTTPException(409, "only virtual swimmers can be simulated")

    async def update_virtual(
        self, swimmer_id: str, command: DevSwimmerUpdate
    ) -> Swimmer:
        async with self._lock:
            swimmer = self._require_swimmer(swimmer_id)
            if swimmer.source != "virtual":
                raise HTTPException(409, "开发测试页只能修改虚拟泳者")
            if command.status is not None:
                self._clear_collision_locked(swimmer_id)
                swimmer.status = command.status
            if command.display_name is not None:
                swimmer.display_name = command.display_name
            if command.position is not None:
                swimmer.position = command.position
                swimmer.pool_position = command.position
                self._map_swimmer_locked(swimmer)
            if command.online is not None:
                swimmer.online = command.online
            swimmer.last_seen_at = datetime.now(timezone.utc)
            self._revision += 1
            return swimmer

    async def delete_virtual(self, swimmer_id: str) -> None:
        async with self._lock:
            swimmer = self._require_swimmer(swimmer_id)
            if swimmer.source != "virtual":
                raise HTTPException(409, "只能删除虚拟泳者")
            self._clear_collision_locked(swimmer_id)
            del self._swimmers[swimmer_id]
            self._revision += 1

    async def set_collision(self, command: CollisionCommand) -> None:
        async with self._lock:
            swimmer_a = self._require_swimmer(command.swimmer_a)
            swimmer_b = self._require_swimmer(command.swimmer_b)
            if not swimmer_a.online or not swimmer_b.online:
                raise HTTPException(409, "只能为在线泳者设置碰撞")
            self._clear_collision_locked(swimmer_a.id)
            self._clear_collision_locked(swimmer_b.id)
            if command.active:
                swimmer_a.status = "collision"
                swimmer_b.status = "collision"
                swimmer_a.collision_with = swimmer_b.id
                swimmer_b.collision_with = swimmer_a.id
            self._revision += 1

    async def reset_virtual(self) -> None:
        async with self._lock:
            virtual_ids = {
                item.id for item in self._swimmers.values() if item.source == "virtual"
            }
            for swimmer_id in virtual_ids:
                self._clear_collision_locked(swimmer_id)
                self._swimmers.pop(swimmer_id, None)
            self._revision += 1

    def _update_calibration_validity_locked(self, metadata: VisionFrame) -> None:
        if not self._calibration.roi_configured:
            self._calibration.valid_for_current_stream = False
            return
        source_width = self._calibration.source_image_width
        source_height = self._calibration.source_image_height
        if not source_width or not source_height:
            self._calibration.valid_for_current_stream = False
            return
        source_ratio = source_width / source_height
        current_ratio = metadata.image_width / metadata.image_height
        self._calibration.valid_for_current_stream = (
            abs(current_ratio - source_ratio) / source_ratio <= 0.01
        )

    def _remap_all_locked(self) -> None:
        for swimmer in self._swimmers.values():
            self._map_swimmer_locked(swimmer)

    def _map_swimmer_locked(self, swimmer: Swimmer) -> None:
        location = None
        if swimmer.source == "vision":
            if (
                swimmer.frame_position is not None
                and self._calibration.roi is not None
                and self._calibration.valid_for_current_stream
            ):
                location = map_frame_position(
                    swimmer.frame_position,
                    self._calibration.roi,
                    lane_boundary_y=self._calibration.lane_boundary_y,
                    lane_boundary_line=(
                        (
                            self._calibration.lane_boundary_line.left_y,
                            self._calibration.lane_boundary_line.right_y,
                        )
                        if self._calibration.lane_boundary_line is not None
                        else None
                    ),
                )
        elif swimmer.pool_position is not None:
            location = map_pool_position(swimmer.pool_position)

        if location is None:
            swimmer.pool_position = None if swimmer.source == "vision" else swimmer.pool_position
            swimmer.in_pool = False
            swimmer.lane = None
            swimmer.led_index = None
            return
        swimmer.pool_position = location.pool_position
        swimmer.in_pool = True
        swimmer.lane = location.lane
        swimmer.led_index = location.led_index

    def _restore_manual_calibration_locked(self) -> None:
        self._calibration = self._manual_calibration.model_copy(deep=True)
        self._calibration.valid_for_current_stream = False

    def _apply_replay_alert_locked(self) -> None:
        swimmer = self._swimmers.get("vision-1")
        if swimmer is None:
            return
        if self._replay_alert_stage == "suspected_drowning":
            self._clear_collision_locked(swimmer.id)
            swimmer.status = "suspected_drowning"
        elif self._replay_alert_stage == "high_risk":
            self._clear_collision_locked(swimmer.id)
            swimmer.status = "drowning"
        elif swimmer.status in {"suspected_drowning", "drowning"}:
            swimmer.status = "normal"

    def _require_swimmer(self, swimmer_id: str) -> Swimmer:
        swimmer = self._swimmers.get(swimmer_id)
        if swimmer is None:
            raise HTTPException(404, "泳者不存在")
        return swimmer

    def _clear_collision_locked(self, swimmer_id: str) -> None:
        swimmer = self._swimmers.get(swimmer_id)
        if swimmer is None:
            return
        partner_id = swimmer.collision_with
        swimmer.collision_with = None
        if swimmer.status == "collision":
            swimmer.status = "normal"
        if partner_id and partner_id in self._swimmers:
            partner = self._swimmers[partner_id]
            partner.collision_with = None
            if partner.status == "collision":
                partner.status = "normal"

    def _clear_vision_collisions_locked(self) -> None:
        involved_ids = {
            swimmer_id
            for pair in self._vision_collision_pairs
            for swimmer_id in pair
        }
        for swimmer_id in involved_ids:
            swimmer = self._swimmers.get(swimmer_id)
            if swimmer is not None:
                swimmer.collision_with = None
                if swimmer.status == "collision":
                    swimmer.status = "normal"
        self._vision_collision_pairs.clear()
