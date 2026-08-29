from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Position(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class PoolRoi(BaseModel):
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)
    x2: float = Field(ge=0.0, le=1.0)
    y2: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def ordered_and_large_enough(self) -> "PoolRoi":
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError("ROI 右下角必须位于左上角的右下方")
        if self.x2 - self.x1 < 0.01 or self.y2 - self.y1 < 0.01:
            raise ValueError("ROI 宽度和高度至少应占画面的 1%")
        return self


class RoiUpdateRequest(BaseModel):
    frame_id: str = Field(min_length=1, max_length=128)
    top_left: Position
    bottom_right: Position

    def as_roi(self) -> PoolRoi:
        return PoolRoi(
            x1=self.top_left.x,
            y1=self.top_left.y,
            x2=self.bottom_right.x,
            y2=self.bottom_right.y,
        )


class LaneBoundaryLine(BaseModel):
    left_y: float = Field(ge=0.0, le=1.0)
    right_y: float = Field(ge=0.0, le=1.0)


class CalibrationState(BaseModel):
    roi_configured: bool = False
    roi: PoolRoi | None = None
    lane_boundary_y: float | None = Field(default=None, ge=0.0, le=1.0)
    lane_boundary_line: LaneBoundaryLine | None = None
    mapping_source: Literal["manual", "replay"] | None = None
    source_frame_id: str | None = None
    source_image_width: int | None = None
    source_image_height: int | None = None
    updated_at: datetime | None = None
    valid_for_current_stream: bool = False

    @model_validator(mode="after")
    def lane_boundary_fits_roi(self) -> "CalibrationState":
        if (
            self.lane_boundary_y is not None
            and self.roi is not None
            and not self.roi.y1 < self.lane_boundary_y < self.roi.y2
        ):
            raise ValueError("lane_boundary_y must be inside the ROI")
        if self.lane_boundary_line is not None and self.roi is not None:
            if not all(
                self.roi.y1 < value < self.roi.y2
                for value in (
                    self.lane_boundary_line.left_y,
                    self.lane_boundary_line.right_y,
                )
            ):
                raise ValueError("lane_boundary_line must be inside the ROI")
        return self


class VisionTrack(BaseModel):
    track_id: str | int
    class_id: int = 0
    class_name: str = "person"
    confidence: float = Field(ge=0.0, le=1.0)
    bbox_xyxy: tuple[float, float, float, float]
    center_xy: tuple[float, float]
    center_normalized: tuple[float, float]

    @field_validator("center_normalized")
    @classmethod
    def normalized_center_is_valid(
        cls, value: tuple[float, float]
    ) -> tuple[float, float]:
        if not all(0.0 <= item <= 1.0 for item in value):
            raise ValueError("center_normalized values must be between 0 and 1")
        return value


class VisionCollisionWarning(BaseModel):
    track_ids: tuple[str | int, str | int]
    time_to_collision_s: float | None = Field(default=None, ge=0.0)
    minimum_distance_px: float | None = Field(default=None, ge=0.0)
    warning_distance_px: float | None = Field(default=None, ge=0.0)

    @field_validator("track_ids")
    @classmethod
    def track_ids_are_distinct(
        cls, value: tuple[str | int, str | int]
    ) -> tuple[str | int, str | int]:
        if str(value[0]) == str(value[1]):
            raise ValueError("collision track_ids must be different")
        return value


class VisionFrame(BaseModel):
    type: Literal["vision_frame"] = "vision_frame"
    schema_version: Literal["1.0"] = "1.0"
    frame_id: str = Field(min_length=1, max_length=128)
    captured_at: datetime
    image_width: int = Field(gt=0, le=16384)
    image_height: int = Field(gt=0, le=16384)
    tracks: list[VisionTrack] = Field(default_factory=list, max_length=500)
    # None means an older sender did not provide collision information.  An
    # explicit empty list means the collision episode has ended.
    collision_warnings: list[VisionCollisionWarning] | None = Field(
        default=None, max_length=500
    )

    @field_validator("captured_at")
    @classmethod
    def captured_at_needs_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("captured_at must include a timezone")
        return value

    @model_validator(mode="after")
    def coordinates_fit_image(self) -> "VisionFrame":
        for track in self.tracks:
            x1, y1, x2, y2 = track.bbox_xyxy
            if x1 > x2 or y1 > y2:
                raise ValueError("bbox_xyxy must be ordered as x1,y1,x2,y2")
            if not (
                0 <= x1 <= self.image_width
                and 0 <= x2 <= self.image_width
                and 0 <= y1 <= self.image_height
                and 0 <= y2 <= self.image_height
            ):
                raise ValueError("bbox_xyxy must fit inside the declared image")
        return self


class Swimmer(BaseModel):
    id: str
    display_name: str
    source: Literal["vision", "virtual", "device"]
    position: Position
    frame_position: Position | None = None
    pool_position: Position | None = None
    in_pool: bool = False
    lane: Literal[1, 2] | None = None
    led_index: int | None = Field(default=None, ge=1, le=70)
    status: Literal[
        "normal", "suspected_drowning", "drowning", "collision"
    ] = "normal"
    collision_with: str | None = None
    online: bool = True
    last_seen_at: datetime = Field(default_factory=utc_now)
    confidence: float | None = None
    bbox_xyxy: tuple[float, float, float, float] | None = None


class DevSwimmerCreate(BaseModel):
    display_name: str | None = Field(default=None, max_length=40)
    position: Position = Field(default_factory=lambda: Position(x=0.5, y=0.5))


class DevSwimmerUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=40)
    position: Position | None = None
    status: Literal["normal", "drowning"] | None = None
    online: bool | None = None


class CollisionCommand(BaseModel):
    swimmer_a: str
    swimmer_b: str
    active: bool = True

    @model_validator(mode="after")
    def swimmers_are_distinct(self) -> "CollisionCommand":
        if self.swimmer_a == self.swimmer_b:
            raise ValueError("collision swimmers must be different")
        return self


class VisionReplayCommand(BaseModel):
    action: Literal["select", "play", "pause", "restart", "stop", "set_feedback"]
    sample_id: Literal["oneline", "twolines", "crashing", "drowning"] | None = None
    device_feedback_enabled: bool | None = None

    @model_validator(mode="after")
    def required_action_argument_is_present(self) -> "VisionReplayCommand":
        if self.action == "select" and self.sample_id is None:
            raise ValueError("sample_id is required for select")
        if self.action == "set_feedback" and self.device_feedback_enabled is None:
            raise ValueError("device_feedback_enabled is required for set_feedback")
        return self


class GatewayWifiRequest(BaseModel):
    ssid: str
    password: SecretStr

    @model_validator(mode="after")
    def valid_wifi_credentials(self) -> "GatewayWifiRequest":
        ssid_length = len(self.ssid.encode("utf-8"))
        password_length = len(self.password.get_secret_value().encode("utf-8"))
        if not 1 <= ssid_length <= 32:
            raise ValueError("SSID 按 UTF-8 编码后必须为 1～32 字节")
        if password_length != 0 and not 8 <= password_length <= 63:
            raise ValueError("Wi-Fi 密码必须为空，或按 UTF-8 编码后为 8～63 字节")
        return self


class GatewayPortSelection(BaseModel):
    port: str | None = Field(default=None, min_length=1, max_length=128)


class DeviceHello(BaseModel):
    type: Literal["device_hello"] = "device_hello"
    schema_version: Literal["1.0"] = "1.0"
    message_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    device_type: Literal["lifeguard_band", "swimmer"]
    firmware_version: str = Field(min_length=1, max_length=64)
    boot_id: str | None = Field(default=None, max_length=128)


class DeviceHeartbeat(BaseModel):
    type: Literal["device_heartbeat"] = "device_heartbeat"
    schema_version: Literal["1.0"] = "1.0"
    message_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    uptime_ms: int = Field(ge=0)
    sent_at: datetime | None = None
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    last_applied_revision: int | None = Field(default=None, ge=0)

    @field_validator("sent_at")
    @classmethod
    def sent_at_needs_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("sent_at must include a timezone when provided")
        if value is not None and value.utcoffset() != timedelta(0):
            raise ValueError("sent_at must use UTC when provided")
        return value


class SwimmerHeartbeat(BaseModel):
    type: Literal["swimmer_heartbeat"] = "swimmer_heartbeat"
    schema_version: Literal["1.0"] = "1.0"
    message_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    uptime_ms: int = Field(ge=0)
    sent_at: datetime | None = None
    local_alert_stage: Literal[
        "normal", "suspected_drowning", "rescue_triggered"
    ]
    communication_loss_ms: int = Field(ge=0)

    @field_validator("sent_at")
    @classmethod
    def sent_at_needs_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("sent_at must include a timezone when provided")
        if value is not None and value.utcoffset() != timedelta(0):
            raise ValueError("sent_at must use UTC when provided")
        return value


class DeviceBindingRequest(BaseModel):
    swimmer_id: str = Field(min_length=1, max_length=128)
    rescue_enabled: bool = True


class DeviceAck(BaseModel):
    type: Literal["ack"] = "ack"
    schema_version: Literal["1.0"] = "1.0"
    message_id: str = Field(min_length=1, max_length=128)
    accepted: bool
    applied_revision: int | None = Field(default=None, ge=0)
