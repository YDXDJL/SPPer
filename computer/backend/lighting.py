from __future__ import annotations

from dataclasses import dataclass

from .models import PoolRoi, Position, Swimmer

LEDS_PER_LANE = 70
OFF = "000000"
COLORS = {
    "normal": "00FF00",
    "collision": "FFD000",
    "suspected_drowning": "FF0000",
    "drowning": "FF0000",
}
PRIORITY = {
    "normal": 1,
    "collision": 2,
    "suspected_drowning": 3,
    "drowning": 4,
}


@dataclass(frozen=True, slots=True)
class LightLocation:
    pool_position: Position
    lane: int
    led_index: int


def map_frame_position(
    position: Position,
    roi: PoolRoi,
    *,
    lane_boundary_y: float | None = None,
    lane_boundary_line: tuple[float, float] | None = None,
) -> LightLocation | None:
    if not (
        roi.x1 <= position.x <= roi.x2 and roi.y1 <= position.y <= roi.y2
    ):
        return None
    pool_x = (position.x - roi.x1) / (roi.x2 - roi.x1)
    pool_y = (position.y - roi.y1) / (roi.y2 - roi.y1)
    if lane_boundary_line is not None:
        left_y, right_y = lane_boundary_line
        boundary = left_y + (right_y - left_y) * position.x
    else:
        boundary = (
            lane_boundary_y
            if lane_boundary_y is not None
            else roi.y1 + (roi.y2 - roi.y1) / 2
        )
    lane = 1 if position.y < boundary else 2
    led_index = round(max(0.0, min(1.0, pool_x)) * (LEDS_PER_LANE - 1)) + 1
    return LightLocation(
        pool_position=Position(x=pool_x, y=pool_y),
        lane=lane,
        led_index=led_index,
    )


def map_pool_position(position: Position) -> LightLocation:
    lane = 1 if position.y < 0.5 else 2
    led_index = round(position.x * (LEDS_PER_LANE - 1)) + 1
    return LightLocation(
        pool_position=position.model_copy(),
        lane=lane,
        led_index=led_index,
    )


def compose_rgb_frame(
    swimmers: list[Swimmer],
    *,
    drowning_visible: bool,
) -> tuple[str, str]:
    selected: dict[tuple[int, int], str] = {}
    for swimmer in swimmers:
        if (
            not swimmer.online
            or not swimmer.in_pool
            or swimmer.lane not in (1, 2)
            or swimmer.led_index is None
        ):
            continue
        key = (swimmer.lane, swimmer.led_index)
        current = selected.get(key)
        if current is None or PRIORITY[swimmer.status] > PRIORITY[current]:
            selected[key] = swimmer.status

    lanes = [[OFF for _ in range(LEDS_PER_LANE)] for _ in range(2)]
    for (lane, led_index), status in selected.items():
        color = COLORS[status]
        if status in {"suspected_drowning", "drowning"} and not drowning_visible:
            color = OFF
        lanes[lane - 1][led_index - 1] = color
    return "".join(lanes[0]), "".join(lanes[1])
