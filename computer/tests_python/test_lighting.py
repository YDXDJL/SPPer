from __future__ import annotations

from backend.lighting import (
    OFF,
    compose_rgb_frame,
    map_frame_position,
    map_pool_position,
)
from backend.models import PoolRoi, Position, Swimmer


def swimmer(
    swimmer_id: str,
    *,
    lane: int,
    led_index: int,
    status: str,
) -> Swimmer:
    position = Position(x=(led_index - 1) / 69, y=0.25 if lane == 1 else 0.75)
    return Swimmer(
        id=swimmer_id,
        display_name=swimmer_id,
        source="virtual",
        position=position,
        pool_position=position,
        in_pool=True,
        lane=lane,
        led_index=led_index,
        status=status,
    )


def test_pool_mapping_endpoints_and_lane_boundary() -> None:
    assert map_pool_position(Position(x=0, y=0.49)).led_index == 1
    assert map_pool_position(Position(x=1, y=0.49)).led_index == 70
    assert map_pool_position(Position(x=0.5, y=0.49)).lane == 1
    assert map_pool_position(Position(x=0.5, y=0.5)).lane == 2


def test_frame_mapping_excludes_points_outside_roi() -> None:
    roi = PoolRoi(x1=0.1, y1=0.1, x2=0.9, y2=0.9)
    assert map_frame_position(Position(x=0.05, y=0.5), roi) is None
    left = map_frame_position(Position(x=0.1, y=0.2), roi)
    right = map_frame_position(Position(x=0.9, y=0.8), roi)
    assert left and left.led_index == 1 and left.lane == 1
    assert right and right.led_index == 70 and right.lane == 2


def test_frame_mapping_uses_visual_lane_boundary() -> None:
    roi = PoolRoi(x1=0.0, y1=0.0, x2=1.0, y2=1.0)
    upper = map_frame_position(
        Position(x=0.25, y=0.34), roi, lane_boundary_y=0.35
    )
    lower = map_frame_position(
        Position(x=0.75, y=0.36), roi, lane_boundary_y=0.35
    )
    assert upper and upper.lane == 1 and upper.led_index == 18
    assert lower and lower.lane == 2 and lower.led_index == 53


def test_full_frame_length_priority_and_drowning_flash() -> None:
    swimmers = [
        swimmer("normal", lane=1, led_index=5, status="normal"),
        swimmer("collision", lane=1, led_index=5, status="collision"),
        swimmer("drowning", lane=1, led_index=5, status="drowning"),
        swimmer("lane-2", lane=2, led_index=70, status="normal"),
    ]
    visible_1, visible_2 = compose_rgb_frame(swimmers, drowning_visible=True)
    hidden_1, hidden_2 = compose_rgb_frame(swimmers, drowning_visible=False)

    assert len(visible_1) == 420
    assert len(visible_2) == 420
    assert visible_1[4 * 6 : 5 * 6] == "FF0000"
    assert hidden_1[4 * 6 : 5 * 6] == OFF
    assert visible_2[-6:] == "00FF00"
    assert hidden_2[-6:] == "00FF00"
