from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.models import VisionFrame


ROOT = Path(__file__).resolve().parents[2]
TRACKING_OUTPUT = ROOT / "demo_assets" / "vision_replays" / "tracks"
SAMPLE_IDS = {"oneline", "twolines", "crashing", "drowning"}


def wait_for(
    predicate,
    *,
    timeout: float = 2.0,
    interval: float = 0.01,
    message: str = "condition was not reached",
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(message)


def replay_state(client: TestClient) -> dict:
    return client.get("/api/state").json()["vision_replay"]


def swimmers_by_id(client: TestClient) -> dict[str, dict]:
    return {
        swimmer["id"]: swimmer
        for swimmer in client.get("/api/state").json()["swimmers"]
    }


def command(client: TestClient, action: str, **payload) -> dict:
    response = client.post(
        "/api/vision/replay/command",
        json={"action": action, **payload},
    )
    assert response.status_code == 200, response.text
    return response.json()


def configured_frame(sample_id: str, frame_index: int) -> dict:
    path = TRACKING_OUTPUT / f"{sample_id}_configured_tracks.jsonl"
    with path.open("r", encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index == frame_index:
                return json.loads(line)
    raise AssertionError(f"missing {sample_id} frame {frame_index}")


def vision_payload(sample_id: str, frame_index: int) -> dict:
    record = configured_frame(sample_id, frame_index)
    width, height = (1920, 1080) if sample_id != "twolines" else (1920, 858)
    captured_at = datetime(2026, 8, 12, tzinfo=timezone.utc) + timedelta(
        seconds=float(record["timestamp_s"])
    )
    return {
        "type": "vision_frame",
        "schema_version": "1.0",
        "frame_id": f"test-{sample_id}-{frame_index}",
        "captured_at": captured_at.isoformat(),
        "image_width": width,
        "image_height": height,
        "tracks": [
            {
                "track_id": str(track["person_id"]),
                "class_id": 0,
                "class_name": "person",
                "confidence": track["confidence"],
                "bbox_xyxy": track["bbox_xyxy"],
                "center_xy": track["center_xy"],
                "center_normalized": [
                    track["center_xy"][0] / width,
                    track["center_xy"][1] / height,
                ],
            }
            for track in record["tracks"]
        ],
        "collision_warnings": [
            {
                "track_ids": [str(item) for item in warning["person_ids"]],
                "time_to_collision_s": warning.get("time_to_collision_s"),
                "minimum_distance_px": warning.get("minimum_distance_px"),
                "warning_distance_px": warning.get("warning_distance_px"),
            }
            for warning in record["collision_warnings"]
        ],
    }


def send_frame(websocket, payload: dict) -> None:
    websocket.send_json(payload)
    websocket.send_bytes(b"\xff\xd8test-jpeg")
    acknowledgement = websocket.receive_json()
    assert acknowledgement["type"] == "vision_ack"
    assert acknowledgement["frame_id"] == payload["frame_id"]


def swimmer_hello(device_id: str) -> dict:
    return {
        "type": "device_hello",
        "schema_version": "1.0",
        "message_id": f"hello-{device_id}",
        "device_id": device_id,
        "device_type": "swimmer",
        "firmware_version": "0.4.1",
        "boot_id": f"boot-{device_id}",
    }


def test_vision_frame_collision_warnings_are_optional_and_validated() -> None:
    without_warning = vision_payload("crashing", 0)
    without_warning.pop("collision_warnings")
    parsed = VisionFrame.model_validate(without_warning)
    # Omitted preserves compatibility with older live senders; an explicit
    # empty list is the authoritative "collision episode ended" signal.
    assert parsed.collision_warnings is None

    with_warning = VisionFrame.model_validate(vision_payload("crashing", 124))
    assert len(with_warning.collision_warnings) == 1
    warning = with_warning.collision_warnings[0]
    assert warning.track_ids == ("1", "2")
    assert warning.time_to_collision_s == 3.0


def test_replay_catalog_describes_the_four_predecoded_samples(
    tmp_path: Path,
) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    with TestClient(application) as client:
        response = client.get("/api/vision/replays")
        assert response.status_code == 200
        samples = {sample["id"]: sample for sample in response.json()["samples"]}
        assert set(samples) == SAMPLE_IDS
        assert samples["crashing"]["frame_count"] == 246
        assert samples["crashing"]["fps"] == 30.0
        assert samples["crashing"]["people_count"] == 2
        assert samples["crashing"]["collision_episodes"] == 1
        assert samples["oneline"]["people_count"] == 1
        assert samples["oneline"]["collision_episodes"] == 0
        assert samples["twolines"]["people_count"] == 2
        assert samples["twolines"]["collision_episodes"] == 0
        assert samples["drowning"]["frame_count"] == 717
        assert samples["drowning"]["fps"] == 30.0
        assert samples["drowning"]["people_count"] == 1
        assert samples["drowning"]["collision_episodes"] == 0
        assert all(sample["duration_s"] > 0 for sample in samples.values())
        assert all(sample["label"] for sample in samples.values())


def test_replay_command_is_localhost_only_and_reports_state(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    with TestClient(application) as client:
        initial = replay_state(client)
        assert initial["sample_id"] is None
        assert initial["state"] == "idle"
        assert initial["frame_index"] == 0
        assert initial["position_s"] == 0
        assert initial["duration_s"] == 0
        assert initial["error"] is None

        selected = command(client, "select", sample_id="crashing")
        assert selected["sample_id"] == "crashing"
        assert selected["state"] == "ready"
        assert selected["frame_index"] == 0
        assert selected["position_s"] == 0
        assert selected["duration_s"] > 0
        assert replay_state(client) == selected

        feedback = command(client, "set_feedback", device_feedback_enabled=False)
        assert feedback["device_feedback_enabled"] is False
        assert feedback["sample_id"] == "crashing"
        assert feedback["state"] == "ready"

        stopped = command(client, "stop")
        assert stopped["state"] == "idle"
        assert stopped["sample_id"] is None

    with TestClient(
        application, client=("192.168.1.50", 50000)
    ) as remote_client:
        response = remote_client.post(
            "/api/vision/replay/command",
            json={"action": "select", "sample_id": "oneline"},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "LOCALHOST_ONLY"
        assert remote_client.get("/api/vision/replays").status_code == 200


def test_replay_uses_sample_lane_boundaries_and_led_mapping(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    with TestClient(application) as client:
        command(client, "select", sample_id="oneline")
        state = client.get("/api/state").json()
        swimmers = {item["id"]: item for item in state["swimmers"]}
        assert state["calibration"]["mapping_source"] == "replay"
        assert state["calibration"]["lane_boundary_y"] == 0.482
        assert swimmers["vision-1"]["lane"] == 2
        assert swimmers["vision-1"]["led_index"] == 67

        command(client, "select", sample_id="twolines")
        command(client, "play")
        wait_for(
            lambda: replay_state(client)["frame_index"] >= 14,
            timeout=2.0,
            message="twolines replay did not reach its first tracked frame",
        )
        state = client.get("/api/state").json()
        swimmers = {item["id"]: item for item in state["swimmers"]}
        assert 0.44 < state["calibration"]["lane_boundary_y"] < 0.47
        assert swimmers["vision-1"]["lane"] == 1
        assert swimmers["vision-2"]["lane"] == 2

        command(client, "select", sample_id="crashing")
        state = client.get("/api/state").json()
        swimmers = {item["id"]: item for item in state["swimmers"]}
        assert state["calibration"]["lane_boundary_y"] == 0.411
        assert swimmers["vision-1"]["lane"] == 2
        assert swimmers["vision-1"]["led_index"] == 6
        assert swimmers["vision-2"]["lane"] == 2
        assert swimmers["vision-2"]["led_index"] == 64

        command(client, "select", sample_id="drowning")
        state = client.get("/api/state").json()
        swimmers = {item["id"]: item for item in state["swimmers"]}
        assert state["calibration"]["lane_boundary_y"] == 0.248
        assert state["calibration"]["lane_boundary_line"] == {
            "left_y": 0.258,
            "right_y": 0.238,
        }
        assert swimmers["vision-1"]["lane"] == 2

        command(client, "stop")
        state = client.get("/api/state").json()
        assert state["calibration"]["mapping_source"] is None
        assert state["calibration"]["valid_for_current_stream"] is False


def test_crashing_frames_raise_one_start_edge_and_clear_on_end(
    tmp_path: Path,
) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    pulse = AsyncMock(
        wraps=application.state.device_hub.pulse_collision_warning
    )
    application.state.device_hub.pulse_collision_warning = pulse

    with TestClient(application) as client:
        with client.websocket_connect("/ws/vision") as websocket:
            # The configured event log uses one-based frame 125 for START;
            # JSONL uses zero-based frame_index 124.
            send_frame(websocket, vision_payload("crashing", 123))
            send_frame(websocket, vision_payload("crashing", 124))
            send_frame(websocket, vision_payload("crashing", 125))

            swimmers = swimmers_by_id(client)
            assert swimmers["vision-1"]["status"] == "collision"
            assert swimmers["vision-1"]["collision_with"] == "vision-2"
            assert swimmers["vision-2"]["collision_with"] == "vision-1"
            assert pulse.await_count == 1
            assert pulse.await_args.args == ({"vision-1", "vision-2"},)

            # One-based event frame 166 is zero-based JSONL frame 165.
            send_frame(websocket, vision_payload("crashing", 165))
            swimmers = swimmers_by_id(client)
            assert swimmers["vision-1"]["status"] == "normal"
            assert swimmers["vision-2"]["status"] == "normal"
            assert swimmers["vision-1"]["collision_with"] is None
            assert swimmers["vision-2"]["collision_with"] is None
            assert pulse.await_count == 1


def test_pairing_targets_are_the_replayed_vision_ids(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    with TestClient(application) as client:
        selected = command(client, "select", sample_id="crashing")
        assert selected["state"] == "ready"
        assert {"vision-1", "vision-2"} <= swimmers_by_id(client).keys()

        with client.websocket_connect("/ws/devices") as first_socket:
            with client.websocket_connect("/ws/devices") as second_socket:
                first_socket.send_json(swimmer_hello("swimmer-first"))
                second_socket.send_json(swimmer_hello("swimmer-second"))
                assert first_socket.receive_json()["accepted"] is True
                assert second_socket.receive_json()["accepted"] is True

                first = client.put(
                    "/api/devices/swimmer-first/binding",
                    json={"swimmer_id": "vision-1"},
                )
                second = client.put(
                    "/api/devices/swimmer-second/binding",
                    json={"swimmer_id": "vision-2"},
                )
                assert first.status_code == 200
                assert second.status_code == 200
                assert first.json()["bound_swimmer_id"] == "vision-1"
                assert second.json()["bound_swimmer_id"] == "vision-2"


def test_replay_auto_binds_only_one_online_swimmer_device(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    with TestClient(application) as client:
        command(client, "select", sample_id="drowning")
        with client.websocket_connect("/ws/devices") as first_socket:
            first_socket.send_json(swimmer_hello("swimmer-only"))
            assert first_socket.receive_json()["accepted"] is True
            wait_for(
                lambda: next(
                    item
                    for item in client.get("/api/devices").json()["devices"]
                    if item["device_id"] == "swimmer-only"
                )["bound_swimmer_id"]
                == "vision-1",
                message="the unique swimmer device was not auto-bound",
            )
            device = next(
                item
                for item in client.get("/api/devices").json()["devices"]
                if item["device_id"] == "swimmer-only"
            )
            assert device["binding_automatic"] is True

            with client.websocket_connect("/ws/devices") as second_socket:
                second_socket.send_json(swimmer_hello("swimmer-second"))
                assert second_socket.receive_json()["accepted"] is True
                wait_for(
                    lambda: all(
                        item["bound_swimmer_id"] is None
                        for item in client.get("/api/devices").json()["devices"]
                        if item["device_type"] == "swimmer" and item["connected"]
                    ),
                    message="automatic binding was retained with multiple devices",
                )


def test_stop_and_sample_switch_clear_collisions_and_mark_old_tracks_offline(
    tmp_path: Path,
) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    with TestClient(application) as client:
        command(client, "select", sample_id="crashing")
        assert client.post(
            "/api/dev/collisions",
            json={
                "swimmer_a": "vision-1",
                "swimmer_b": "vision-2",
                "active": True,
            },
        ).status_code == 204

        command(client, "select", sample_id="oneline")
        swimmers = swimmers_by_id(client)
        assert swimmers["vision-1"]["status"] == "normal"
        assert swimmers["vision-1"]["collision_with"] is None
        assert swimmers["vision-2"]["status"] == "normal"
        assert swimmers["vision-2"]["collision_with"] is None
        assert swimmers["vision-2"]["online"] is False

        assert client.post(
            "/api/dev/collisions",
            json={
                "swimmer_a": "vision-1",
                "swimmer_b": "vision-2",
                "active": True,
            },
        ).status_code == 409

        command(client, "stop")
        swimmers = swimmers_by_id(client)
        assert all(
            swimmer["online"] is False
            for swimmer in swimmers.values()
            if swimmer["source"] == "vision"
        )
        assert all(
            swimmer["collision_with"] is None
            for swimmer in swimmers.values()
            if swimmer["source"] == "vision"
        )


def test_feedback_disabled_replay_keeps_visual_collision_but_eof_cleans_up(
    tmp_path: Path,
) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        replay_playback_rate=30.0,
    )
    pulse = AsyncMock(
        wraps=application.state.device_hub.pulse_collision_warning
    )
    application.state.device_hub.pulse_collision_warning = pulse

    with TestClient(application) as client:
        command(client, "select", sample_id="crashing")
        command(client, "set_feedback", device_feedback_enabled=False)
        command(client, "play")
        started = wait_for(
            lambda: (
                state
                if (state := replay_state(client))["state"] == "paused"
                and state["cue"]
                and state["cue"]["phase"] == "started"
                else None
            ),
            timeout=8.0,
            message="crashing replay did not reach its collision episode",
        )
        assert started["frame_index"] == 124
        assert started["pause_reason"] == "cue"
        assert pulse.await_count == 0
        swimmers = swimmers_by_id(client)
        assert swimmers["vision-1"]["status"] == "collision"
        assert swimmers["vision-2"]["status"] == "collision"

        held_frame = replay_state(client)["frame_index"]
        time.sleep(0.15)
        assert replay_state(client)["frame_index"] == held_frame

        command(client, "play")
        ended_cue = wait_for(
            lambda: (
                state
                if (state := replay_state(client))["state"] == "paused"
                and state["cue"]
                and state["cue"]["phase"] == "ended"
                else None
            ),
            timeout=5.0,
            message="crashing replay did not pause when the collision ended",
        )
        assert ended_cue["frame_index"] == 165
        swimmers = swimmers_by_id(client)
        assert swimmers["vision-1"]["status"] == "normal"
        assert swimmers["vision-2"]["status"] == "normal"

        command(client, "play")
        wait_for(
            lambda: replay_state(client)["state"] == "ended",
            timeout=5.0,
            message="crashing replay did not reach EOF",
        )
        assert pulse.await_count == 0
        state = replay_state(client)
        assert state["sample_id"] == "crashing"
        assert state["frame_index"] == 245
        assert state["position_s"] >= 8.0
        swimmers = swimmers_by_id(client)
        vision = [item for item in swimmers.values() if item["source"] == "vision"]
        assert vision
        assert all(item["online"] is False for item in vision)
        assert all(item["collision_with"] is None for item in vision)


def test_drowning_cues_pause_once_hold_outputs_and_never_auto_rescue(
    tmp_path: Path,
) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        replay_playback_rate=60.0,
    )
    trigger = AsyncMock(
        wraps=application.state.device_hub.request_rescue_trigger
    )
    application.state.device_hub.request_rescue_trigger = trigger

    with TestClient(application) as client:
        command(client, "select", sample_id="drowning")
        command(client, "play")
        suspected = wait_for(
            lambda: (
                state
                if (state := replay_state(client))["state"] == "paused"
                and state["alert_stage"] == "suspected_drowning"
                else None
            ),
            timeout=12.0,
            message="drowning replay did not pause at frame 335",
        )
        assert suspected["frame_index"] == 334
        assert suspected["cue"]["frame_number"] == 335
        assert suspected["pause_reason"] == "cue"
        state = client.get("/api/state").json()
        swimmer = next(item for item in state["swimmers"] if item["id"] == "vision-1")
        assert swimmer["status"] == "suspected_drowning"
        assert swimmer["lane"] == 2
        assert swimmer["led_index"] == 49
        assert state["calibration"]["lane_boundary_line"] == {
            "left_y": 0.198,
            "right_y": 0.256,
        }
        assert state["lifeguard_state"]["display_state"] == "lane2_drowning"
        assert trigger.await_count == 0

        held = replay_state(client)["frame_index"]
        time.sleep(0.2)
        assert replay_state(client)["frame_index"] == held
        assert swimmers_by_id(client)["vision-1"]["status"] == "suspected_drowning"

        command(client, "play")
        high_risk = wait_for(
            lambda: (
                state
                if (state := replay_state(client))["state"] == "paused"
                and state["alert_stage"] == "high_risk"
                else None
            ),
            timeout=8.0,
            message="drowning replay did not pause at frame 490",
        )
        assert high_risk["frame_index"] == 489
        assert high_risk["cue"]["frame_number"] == 490
        state = client.get("/api/state").json()
        swimmer = next(item for item in state["swimmers"] if item["id"] == "vision-1")
        assert swimmer["status"] == "drowning"
        assert swimmer["lane"] == 2
        assert swimmer["led_index"] == 50
        assert state["calibration"]["lane_boundary_line"] == {
            "left_y": 0.239,
            "right_y": 0.264,
        }
        assert trigger.await_count == 0

        command(client, "play")
        wait_for(
            lambda: replay_state(client)["state"] == "ended",
            timeout=10.0,
            message="drowning replay did not reach EOF",
        )
        ended = replay_state(client)
        assert ended["alert_stage"] == "high_risk"
        assert ended["frame_index"] == 716
        assert swimmers_by_id(client)["vision-1"]["status"] == "drowning"
        assert swimmers_by_id(client)["vision-1"]["online"] is True
        assert trigger.await_count == 0

        command(client, "stop")
        assert replay_state(client)["alert_stage"] == "normal"
        assert swimmers_by_id(client)["vision-1"]["online"] is False
