from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app


@pytest.fixture
def settings_path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


@pytest.fixture
def client(settings_path: Path):
    application = create_app(settings_path=settings_path, enable_serial=False)
    with TestClient(application) as test_client:
        yield test_client


def vision_metadata(
    *,
    frame_id: str = "test-frame-1",
    center: tuple[float, float] = (0.2, 0.375),
) -> dict:
    return {
        "type": "vision_frame",
        "schema_version": "1.0",
        "frame_id": frame_id,
        "captured_at": "2026-07-31T08:30:15.123Z",
        "image_width": 100,
        "image_height": 80,
        "tracks": [
            {
                "track_id": 7,
                "class_id": 0,
                "class_name": "person",
                "confidence": 0.91,
                "bbox_xyxy": [10, 10, 30, 50],
                "center_xy": [center[0] * 100, center[1] * 80],
                "center_normalized": list(center),
            }
        ],
    }


def send_vision_frame(client: TestClient, metadata: dict) -> None:
    with client.websocket_connect("/ws/vision") as websocket:
        websocket.send_json(metadata)
        websocket.send_bytes(b"\xff\xd8test-jpeg")
        acknowledgement = websocket.receive_json()
        assert acknowledgement["type"] == "vision_ack"
        assert acknowledgement["frame_id"] == metadata["frame_id"]


def test_health_and_virtual_swimmer_lifecycle(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    created = client.post(
        "/api/dev/swimmers",
        json={"display_name": "测试泳者", "position": {"x": 0.2, "y": 0.4}},
    )
    assert created.status_code == 201
    swimmer_id = created.json()["id"]
    assert created.json()["lane"] == 1
    assert created.json()["led_index"] == 15

    updated = client.patch(
        f"/api/dev/swimmers/{swimmer_id}",
        json={"position": {"x": 0.8, "y": 0.6}},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "normal"
    assert updated.json()["lane"] == 2

    snapshot = client.get("/api/state").json()
    assert snapshot["stats"]["normal"] == 1
    assert "password" not in str(snapshot).lower()

    deleted = client.delete(f"/api/dev/swimmers/{swimmer_id}")
    assert deleted.status_code == 204


def test_collision_sets_both_swimmers(client: TestClient) -> None:
    first = client.post("/api/dev/swimmers", json={}).json()
    second = client.post("/api/dev/swimmers", json={}).json()

    response = client.post(
        "/api/dev/collisions",
        json={
            "swimmer_a": first["id"],
            "swimmer_b": second["id"],
            "active": True,
        },
    )
    assert response.status_code == 204

    swimmers = {
        item["id"]: item for item in client.get("/api/state").json()["swimmers"]
    }
    assert swimmers[first["id"]]["collision_with"] == second["id"]
    assert swimmers[second["id"]]["collision_with"] == first["id"]


def test_position_must_be_normalized(client: TestClient) -> None:
    response = client.post(
        "/api/dev/swimmers",
        json={"position": {"x": 2, "y": 0.5}},
    )
    assert response.status_code == 422


def test_vision_websocket_and_roi_mapping(
    client: TestClient, settings_path: Path
) -> None:
    send_vision_frame(client, vision_metadata())

    saved = client.put(
        "/api/calibration/roi",
        json={
            "frame_id": "test-frame-1",
            "top_left": {"x": 0.1, "y": 0.1},
            "bottom_right": {"x": 0.9, "y": 0.9},
        },
    )
    assert saved.status_code == 200
    assert saved.json()["roi_configured"] is True
    assert settings_path.exists()

    swimmer = next(
        item
        for item in client.get("/api/state").json()["swimmers"]
        if item["id"] == "vision-7"
    )
    assert swimmer["in_pool"] is True
    assert swimmer["lane"] == 1
    assert swimmer["led_index"] == 10

    restarted = create_app(settings_path=settings_path, enable_serial=False)
    with TestClient(restarted) as restarted_client:
        calibration = restarted_client.get("/api/calibration/roi").json()
        assert calibration["roi_configured"] is True
        assert calibration["roi"]["x1"] == 0.1


def test_roi_rejects_missing_or_stale_frame(client: TestClient) -> None:
    payload = {
        "frame_id": "missing",
        "top_left": {"x": 0.1, "y": 0.1},
        "bottom_right": {"x": 0.9, "y": 0.9},
    }
    missing = client.put("/api/calibration/roi", json=payload)
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "NO_VISION_FRAME"

    send_vision_frame(client, vision_metadata())
    stale = client.put("/api/calibration/roi", json=payload)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "STALE_FRAME"


def test_roi_accepts_recent_frozen_frame(client: TestClient) -> None:
    send_vision_frame(client, vision_metadata(frame_id="frozen-frame"))
    send_vision_frame(client, vision_metadata(frame_id="newer-frame"))
    response = client.put(
        "/api/calibration/roi",
        json={
            "frame_id": "frozen-frame",
            "top_left": {"x": 0.1, "y": 0.1},
            "bottom_right": {"x": 0.9, "y": 0.9},
        },
    )
    assert response.status_code == 200
    assert response.json()["source_frame_id"] == "frozen-frame"


def test_mutations_are_localhost_only(settings_path: Path) -> None:
    application = create_app(settings_path=settings_path, enable_serial=False)
    with TestClient(application, client=("192.168.1.50", 50000)) as remote:
        response = remote.post("/api/dev/swimmers", json={})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "LOCALHOST_ONLY"
        assert remote.get("/api/state").status_code == 200


def test_wifi_never_echoes_password(client: TestClient) -> None:
    response = client.post(
        "/api/gateway/wifi",
        json={"ssid": "Pool-Safety", "password": "supersecret"},
    )
    assert response.status_code == 503
    assert "supersecret" not in response.text
