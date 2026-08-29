from __future__ import annotations

from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.devices import derive_lifeguard_state
from backend.main import create_app


def hello_payload(device_id: str = "lifeguard-band-a1b2") -> dict:
    return {
        "type": "device_hello",
        "schema_version": "1.0",
        "message_id": "hello-1",
        "device_id": device_id,
        "device_type": "lifeguard_band",
        "firmware_version": "0.1.0",
        "boot_id": "boot-1",
    }


def swimmer_hello_payload(device_id: str = "swimmer-a1b2") -> dict:
    return {
        "type": "device_hello",
        "schema_version": "1.0",
        "message_id": "swimmer-hello-1",
        "device_id": device_id,
        "device_type": "swimmer",
        "firmware_version": "0.1.0",
        "boot_id": "swimmer-boot-1",
    }


def swimmer_heartbeat(
    sequence: int,
    *,
    device_id: str = "swimmer-a1b2",
    stage: str = "normal",
) -> dict:
    return {
        "type": "swimmer_heartbeat",
        "schema_version": "1.0",
        "message_id": f"swimmer-hb-{sequence}",
        "device_id": device_id,
        "uptime_ms": sequence * 1000,
        "local_alert_stage": stage,
        "communication_loss_ms": 0,
    }


def wait_for_device(client: TestClient, device_id: str, predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        devices = client.get("/api/devices").json()["devices"]
        device = next(item for item in devices if item["device_id"] == device_id)
        if predicate(device):
            return device
        time.sleep(0.01)
    raise AssertionError(f"device {device_id} did not reach expected state")


def acknowledge_state(websocket, state: dict) -> None:
    websocket.send_json(
        {
            "type": "ack",
            "schema_version": "1.0",
            "message_id": state["message_id"],
            "accepted": True,
            "applied_revision": state["state_revision"],
        }
    )


def test_device_registration_initial_state_heartbeat_and_updates(
    tmp_path: Path,
) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={"retry_delays": (0.05, 0.1, 0.2)},
    )
    with TestClient(application) as client:
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(hello_payload())
            hello_ack = websocket.receive_json()
            assert hello_ack["type"] == "ack"
            assert hello_ack["accepted"] is True
            assert hello_ack["session_id"].startswith("session-")
            assert hello_ack["server_instance_id"].startswith("server-")
            assert hello_ack["heartbeat_interval_ms"] == 5000

            initial = websocket.receive_json()
            assert initial["type"] == "lifeguard_state"
            assert initial["schema_version"] == "1.0"
            assert initial["display_state"] == "safe"
            assert initial["vibration"]["pattern"] == "off"
            acknowledge_state(websocket, initial)

            websocket.send_json(
                {
                    "type": "device_heartbeat",
                    "schema_version": "1.0",
                    "message_id": "hb-1",
                    "device_id": "lifeguard-band-a1b2",
                    "uptime_ms": 12345,
                    "battery_percent": 82,
                    "last_applied_revision": initial["state_revision"],
                }
            )
            heartbeat_ack = websocket.receive_json()
            assert heartbeat_ack["message_id"] == "hb-1"
            assert heartbeat_ack["accepted"] is True

            lane1 = client.post(
                "/api/dev/swimmers",
                json={"position": {"x": 0.2, "y": 0.2}},
            ).json()
            lane2 = client.post(
                "/api/dev/swimmers",
                json={"position": {"x": 0.8, "y": 0.8}},
            ).json()

            with client.websocket_connect("/ws/devices") as lane1_socket:
                with client.websocket_connect("/ws/devices") as lane2_socket:
                    lane1_socket.send_json(swimmer_hello_payload("swimmer-lane1"))
                    lane2_socket.send_json(swimmer_hello_payload("swimmer-lane2"))
                    lane1_socket.receive_json()
                    lane2_socket.receive_json()
                    assert client.put(
                        "/api/devices/swimmer-lane1/binding",
                        json={"swimmer_id": lane1["id"]},
                    ).status_code == 200
                    assert client.put(
                        "/api/devices/swimmer-lane2/binding",
                        json={"swimmer_id": lane2["id"]},
                    ).status_code == 200

                    assert client.post(
                        "/api/devices/swimmer-lane1/rescue-trigger"
                    ).status_code == 202
                    lane1_command = lane1_socket.receive_json()
                    lane1_socket.send_json(
                        {
                            "type": "ack",
                            "schema_version": "1.0",
                            "message_id": lane1_command["message_id"],
                            "accepted": True,
                        }
                    )
                    lane1_state = websocket.receive_json()
                    assert lane1_state["display_state"] == "lane1_drowning"
                    assert lane1_state["drowning_lanes"] == [1]
                    assert (
                        lane1_state["vibration"]["pattern"]
                        == "200ms_on_100ms_off"
                    )
                    assert (
                        lane1_state["state_revision"]
                        == initial["state_revision"] + 1
                    )
                    acknowledge_state(websocket, lane1_state)

                    assert client.post(
                        "/api/devices/swimmer-lane2/rescue-trigger"
                    ).status_code == 202
                    lane2_command = lane2_socket.receive_json()
                    lane2_socket.send_json(
                        {
                            "type": "ack",
                            "schema_version": "1.0",
                            "message_id": lane2_command["message_id"],
                            "accepted": True,
                        }
                    )
                    both_state = websocket.receive_json()
                    assert both_state["display_state"] == "both_drowning"
                    assert both_state["drowning_counts"] == {
                        "lane1": 1,
                        "lane2": 1,
                    }
                    acknowledge_state(websocket, both_state)

            status = client.get("/api/devices").json()
            assert status["connected_count"] == 1
            assert status["devices"][0]["uptime_ms"] == 12345
            assert status["devices"][0]["battery_percent"] == 82
            snapshot = client.get("/api/state").json()
            assert snapshot["schema_version"] == "1.0"
            assert snapshot["device_server"]["connected_count"] == 1
            assert snapshot["devices"][0]["device_id"] == "lifeguard-band-a1b2"


def test_collision_does_not_change_lifeguard_revision(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    with TestClient(application) as client:
        first = client.post("/api/dev/swimmers", json={}).json()
        second = client.post("/api/dev/swimmers", json={}).json()
        before = client.get("/api/devices").json()["lifeguard_state"]
        response = client.post(
            "/api/dev/collisions",
            json={
                "swimmer_a": first["id"],
                "swimmer_b": second["id"],
                "active": True,
            },
        )
        after = client.get("/api/devices").json()["lifeguard_state"]
        assert response.status_code == 204
        assert after["state_revision"] == before["state_revision"]
        assert after["display_state"] == "safe"


def test_unlocated_drowning_is_fail_safe_both() -> None:
    state = derive_lifeguard_state(
        {
            "swimmers": [
                {
                    "online": True,
                    "status": "drowning",
                    "lane": None,
                },
                {
                    "online": False,
                    "status": "drowning",
                    "lane": 1,
                },
                {
                    "online": True,
                    "status": "collision",
                    "lane": 2,
                },
            ]
        }
    )
    assert state["display_state"] == "both_drowning"
    assert state["drowning_lanes"] == [1, 2]
    assert state["unlocated_count"] == 1
    assert state["drowning_counts"] == {"lane1": 0, "lane2": 0}


def test_heartbeat_requires_uptime_but_not_sent_at(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={"retry_delays": (0.05, 0.1, 0.2)},
    )
    with TestClient(application) as client:
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(hello_payload())
            websocket.receive_json()
            state = websocket.receive_json()
            acknowledge_state(websocket, state)

            websocket.send_json(
                {
                    "type": "device_heartbeat",
                    "schema_version": "1.0",
                    "message_id": "hb-missing-uptime",
                    "device_id": "lifeguard-band-a1b2",
                }
            )
            error = websocket.receive_json()
            assert error["code"] == "INVALID_HEARTBEAT"

            websocket.send_json(
                {
                    "type": "device_heartbeat",
                    "schema_version": "1.0",
                    "message_id": "hb-with-time",
                    "device_id": "lifeguard-band-a1b2",
                    "uptime_ms": 5000,
                    "sent_at": "2026-08-01T09:00:00Z",
                }
            )
            ack = websocket.receive_json()
            assert ack["message_id"] == "hb-with-time"

            websocket.send_json(
                {
                    "type": "device_heartbeat",
                    "schema_version": "1.0",
                    "message_id": "hb-non-utc",
                    "device_id": "lifeguard-band-a1b2",
                    "uptime_ms": 6000,
                    "sent_at": "2026-08-01T17:00:00+08:00",
                }
            )
            error = websocket.receive_json()
            assert error["code"] == "INVALID_HEARTBEAT"


def test_new_snapshot_replaces_unacknowledged_snapshot(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={"retry_delays": (0.5, 0.6, 0.7)},
    )
    with TestClient(application) as client:
        with client.websocket_connect("/ws/devices") as websocket:
            with client.websocket_connect("/ws/devices") as swimmer_socket:
                websocket.send_json(hello_payload())
                websocket.receive_json()
                initial = websocket.receive_json()
                swimmer_socket.send_json(swimmer_hello_payload())
                swimmer_socket.receive_json()

                swimmer = client.post(
                    "/api/dev/swimmers",
                    json={"position": {"x": 0.2, "y": 0.2}},
                ).json()
                assert client.put(
                    "/api/devices/swimmer-a1b2/binding",
                    json={"swimmer_id": swimmer["id"]},
                ).status_code == 200
                assert client.post(
                    "/api/devices/swimmer-a1b2/rescue-trigger"
                ).status_code == 202
                command = swimmer_socket.receive_json()
                swimmer_socket.send_json(
                    {
                        "type": "ack",
                        "schema_version": "1.0",
                        "message_id": command["message_id"],
                        "accepted": True,
                    }
                )
                replacement = websocket.receive_json()
                assert replacement["message_id"] != initial["message_id"]
                assert replacement["display_state"] == "lane1_drowning"

                retry = websocket.receive_json()
                assert retry["message_id"] == replacement["message_id"]
                acknowledge_state(websocket, retry)


def test_state_retries_same_message_then_disconnects(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={
            "retry_delays": (0.01, 0.015, 0.02),
            "heartbeat_timeout": 1,
            "watchdog_interval": 0.1,
        },
    )
    with TestClient(application) as client:
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(hello_payload())
            websocket.receive_json()
            messages = [websocket.receive_json() for _ in range(4)]
            assert {item["message_id"] for item in messages} == {
                messages[0]["message_id"]
            }
            assert {item["state_revision"] for item in messages} == {
                messages[0]["state_revision"]
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 4408


def test_heartbeat_timeout_and_new_connection_replaces_old(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={
            "retry_delays": (0.05, 0.1, 0.2),
            "heartbeat_timeout": 0.06,
            "watchdog_interval": 0.01,
        },
    )
    with TestClient(application) as client:
        with client.websocket_connect("/ws/devices") as first:
            first.send_json(hello_payload())
            first_ack = first.receive_json()
            first_state = first.receive_json()
            acknowledge_state(first, first_state)

            with client.websocket_connect("/ws/devices") as second:
                second.send_json(hello_payload())
                second_ack = second.receive_json()
                second_state = second.receive_json()
                acknowledge_state(second, second_state)
                assert second_ack["session_id"] != first_ack["session_id"]
                with pytest.raises(WebSocketDisconnect) as replaced:
                    first.receive_json()
                assert replaced.value.code == 4009

                with pytest.raises(WebSocketDisconnect) as expired:
                    second.receive_json()
                assert expired.value.code == 4408


def test_device_hello_timeout(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={"hello_timeout": 0.02},
    )
    with TestClient(application) as client:
        with client.websocket_connect("/ws/devices") as websocket:
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 4408


def test_devices_websocket_rejects_public_peer(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    with TestClient(application, client=("8.8.8.8", 50000)) as client:
        with client.websocket_connect("/ws/devices") as websocket:
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 4403


def test_swimmer_binding_alarm_override_and_three_heartbeat_recovery(
    tmp_path: Path,
) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={
            "swimmer_suspected_timeout": 0.06,
            "swimmer_rescue_timeout": 0.5,
            "watchdog_interval": 0.01,
            "retry_delays": (0.05, 0.1, 0.2),
        },
    )
    with TestClient(application) as client:
        swimmer = client.post(
            "/api/dev/swimmers", json={"position": {"x": 0.2, "y": 0.2}}
        ).json()
        partner = client.post(
            "/api/dev/swimmers", json={"position": {"x": 0.7, "y": 0.8}}
        ).json()
        assert client.post(
            "/api/dev/collisions",
            json={
                "swimmer_a": swimmer["id"],
                "swimmer_b": partner["id"],
                "active": True,
            },
        ).status_code == 204
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(swimmer_hello_payload())
            hello_ack = websocket.receive_json()
            assert hello_ack["heartbeat_interval_ms"] == 1000

            response = client.put(
                "/api/devices/swimmer-a1b2/binding",
                json={"swimmer_id": swimmer["id"]},
            )
            assert response.status_code == 200

            websocket.send_json(swimmer_heartbeat(1))
            assert websocket.receive_json()["message_id"] == "swimmer-hb-1"
            alarm = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["communication_state"] == "suspected_drowning",
            )
            assert alarm["bound_swimmer_id"] == swimmer["id"]
            snapshot = client.get("/api/state").json()
            target = next(item for item in snapshot["swimmers"] if item["id"] == swimmer["id"])
            assert target["status"] == "drowning"
            assert target["device_alarm_override"] is True
            assert snapshot["lifeguard_state"]["display_state"] == "lane1_drowning"

            websocket.send_json(swimmer_heartbeat(2))
            websocket.receive_json()
            websocket.send_json(swimmer_heartbeat(2))
            websocket.receive_json()
            duplicate = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["communication_state"] == "recovering",
            )
            assert duplicate["recovery_heartbeat_count"] == 1

            for sequence in (3, 4):
                websocket.send_json(swimmer_heartbeat(sequence))
                websocket.receive_json()
            recovered = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["communication_state"] == "normal",
            )
            assert recovered["recovery_heartbeat_count"] == 3
            snapshot = client.get("/api/state").json()
            target = next(item for item in snapshot["swimmers"] if item["id"] == swimmer["id"])
            assert target["status"] == "collision"
            assert target["device_alarm_override"] is False


def test_swimmer_disconnect_keeps_unlocated_alarm(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={
            "swimmer_suspected_timeout": 0.04,
            "swimmer_rescue_timeout": 0.5,
            "watchdog_interval": 0.01,
            "retry_delays": (0.05, 0.1, 0.2),
        },
    )
    with TestClient(application) as client:
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(swimmer_hello_payload())
            websocket.receive_json()
            websocket.send_json(swimmer_heartbeat(1))
            websocket.receive_json()
            wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["communication_state"] == "suspected_drowning",
            )

        disconnected = wait_for_device(
            client,
            "swimmer-a1b2",
            lambda item: not item["connected"],
        )
        assert disconnected["communication_state"] == "suspected_drowning"
        snapshot = client.get("/api/state").json()
        assert snapshot["lifeguard_state"]["display_state"] == "both_drowning"
        assert snapshot["lifeguard_state"]["unlocated_count"] == 1


def test_binding_is_one_to_one_and_localhost_only(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    with TestClient(application) as client:
        target = client.post("/api/dev/swimmers", json={}).json()
        for device_id in ("swimmer-a1b2", "swimmer-c3d4"):
            with client.websocket_connect("/ws/devices") as websocket:
                websocket.send_json(swimmer_hello_payload(device_id))
                websocket.receive_json()
        assert client.put(
            "/api/devices/swimmer-a1b2/binding",
            json={"swimmer_id": target["id"]},
        ).status_code == 200
        conflict = client.put(
            "/api/devices/swimmer-c3d4/binding",
            json={"swimmer_id": target["id"]},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "SWIMMER_ALREADY_BOUND"

    with TestClient(application, client=("192.168.1.20", 50000)) as remote:
        response = remote.delete("/api/devices/swimmer-a1b2/binding")
        assert response.status_code == 403


def test_manual_rescue_trigger_allows_connected_unbound_device(
    tmp_path: Path,
) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={"retry_delays": (0.05, 0.1, 0.2)},
    )
    with TestClient(application) as client:
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(swimmer_hello_payload())
            websocket.receive_json()

            response = client.post("/api/devices/swimmer-a1b2/rescue-trigger")
            assert response.status_code == 202
            command = websocket.receive_json()
            assert command["type"] == "swimmer_control_command"
            assert command["target_device_id"] == "swimmer-a1b2"
            assert command["trigger_rescue"] is True
            websocket.send_json(
                {
                    "type": "ack",
                    "schema_version": "1.0",
                    "message_id": command["message_id"],
                    "accepted": True,
                }
            )
            triggered = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["pending_command"] is None,
            )
            assert triggered["bound_swimmer_id"] is None
            assert triggered["rescue_expected"] is True


def test_rescue_reset_retries_same_command_and_requires_ack(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={
            "swimmer_suspected_timeout": 0.03,
            "swimmer_rescue_timeout": 0.06,
            "watchdog_interval": 0.005,
            "retry_delays": (0.03, 0.04, 0.05),
        },
    )
    with TestClient(application) as client:
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(swimmer_hello_payload())
            websocket.receive_json()
            websocket.send_json(swimmer_heartbeat(1))
            websocket.receive_json()
            wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["communication_state"] == "rescue_triggered",
            )
            for sequence in (2, 3, 4):
                websocket.send_json(
                    swimmer_heartbeat(sequence, stage="rescue_triggered")
                )
                websocket.receive_json()
            ready = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["communication_state"] == "normal",
            )
            assert ready["rescue_expected"] is True
            assert ready["rescue_confirmed"] is True

            response = client.post("/api/devices/swimmer-a1b2/rescue-reset")
            assert response.status_code == 202
            first = websocket.receive_json()
            second = websocket.receive_json()
            assert first["type"] == "rescue_reset_command"
            assert second["message_id"] == first["message_id"]
            websocket.send_json(
                {
                    "type": "ack",
                    "schema_version": "1.0",
                    "message_id": second["message_id"],
                    "accepted": True,
                }
            )
            reset = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: not item["rescue_expected"],
            )
            assert reset["rescue_confirmed"] is False
            assert reset["pending_command"] is None
            denied = client.post("/api/devices/swimmer-a1b2/rescue-reset")
            assert denied.status_code == 409


def test_rescue_reset_after_server_restart_uses_device_latch(
    tmp_path: Path,
) -> None:
    """A fresh server trusts the swimmer's latched rescue state after recovery."""
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={"retry_delays": (0.05, 0.1, 0.2)},
    )
    with TestClient(application) as client:
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(swimmer_hello_payload())
            assert websocket.receive_json()["heartbeat_interval_ms"] == 1000

            for sequence in (1, 2, 3):
                websocket.send_json(
                    swimmer_heartbeat(sequence, stage="rescue_triggered")
                )
                assert websocket.receive_json()["accepted"] is True

            ready = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["recovery_heartbeat_count"] == 3,
            )
            assert ready["connected"] is True
            assert ready["communication_state"] == "normal"
            assert ready["rescue_confirmed"] is True
            assert ready["rescue_expected"] is False

            response = client.post("/api/devices/swimmer-a1b2/rescue-reset")
            assert response.status_code == 202
            command = websocket.receive_json()
            assert command["type"] == "rescue_reset_command"
            assert command["target_device_id"] == "swimmer-a1b2"
            websocket.send_json(
                {
                    "type": "ack",
                    "schema_version": "1.0",
                    "message_id": command["message_id"],
                    "accepted": True,
                }
            )
            reset = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: not item["rescue_confirmed"],
            )
            assert reset["pending_command"] is None


def test_pairing_controls_rescue_and_manual_trigger(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={"retry_delays": (0.05, 0.1, 0.2)},
    )
    with TestClient(application) as client:
        swimmer = client.post(
            "/api/dev/swimmers", json={"position": {"x": 0.2, "y": 0.2}}
        ).json()
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(swimmer_hello_payload())
            websocket.receive_json()
            response = client.put(
                "/api/devices/swimmer-a1b2/binding",
                json={"swimmer_id": swimmer["id"], "rescue_enabled": False},
            )
            assert response.status_code == 200
            control = websocket.receive_json()
            assert control["type"] == "swimmer_control_command"
            assert control["rescue_enabled"] is False
            assert control["simulation_paused"] is False
            websocket.send_json(
                {
                    "type": "ack",
                    "schema_version": "1.0",
                    "message_id": control["message_id"],
                    "accepted": True,
                }
            )
            configured = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["pending_command"] is None,
            )
            assert configured["rescue_enabled"] is False

            triggered = client.post("/api/devices/swimmer-a1b2/rescue-trigger")
            assert triggered.status_code == 202
            command = websocket.receive_json()
            assert command["trigger_rescue"] is True
            assert command["rescue_enabled"] is False
            websocket.send_json(
                {
                    "type": "ack",
                    "schema_version": "1.0",
                    "message_id": command["message_id"],
                    "accepted": True,
                }
            )
            websocket.send_json(swimmer_heartbeat(1, stage="rescue_triggered"))
            websocket.receive_json()
            alarmed = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["rescue_confirmed"],
            )
            assert alarmed["rescue_expected"] is True
            snapshot = client.get("/api/state").json()
            target = next(
                item for item in snapshot["swimmers"] if item["id"] == swimmer["id"]
            )
            assert target["status"] == "drowning"


def test_virtual_drowning_pauses_and_resumes_paired_device(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={
            "swimmer_suspected_timeout": 0.08,
            "swimmer_rescue_timeout": 0.16,
            "watchdog_interval": 0.005,
            "retry_delays": (0.05, 0.1, 0.2),
        },
    )
    with TestClient(application) as client:
        swimmer = client.post(
            "/api/dev/swimmers", json={"position": {"x": 0.2, "y": 0.2}}
        ).json()
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(swimmer_hello_payload())
            websocket.receive_json()
            assert client.put(
                "/api/devices/swimmer-a1b2/binding",
                json={"swimmer_id": swimmer["id"]},
            ).status_code == 200

            assert client.patch(
                f"/api/dev/swimmers/{swimmer['id']}",
                json={"status": "drowning"},
            ).status_code == 200
            immediate = client.get("/api/state").json()
            immediate_target = next(
                item
                for item in immediate["swimmers"]
                if item["id"] == swimmer["id"]
            )
            assert immediate_target["status"] == "normal"
            assert immediate_target["device_alarm_override"] is False
            assert immediate["lifeguard_state"]["display_state"] == "safe"

            pause = websocket.receive_json()
            assert pause["type"] == "swimmer_control_command"
            assert pause["simulation_paused"] is True
            websocket.send_json(
                {
                    "type": "ack",
                    "schema_version": "1.0",
                    "message_id": pause["message_id"],
                    "accepted": True,
                }
            )
            paused = wait_for_device(
                client, "swimmer-a1b2", lambda item: item["simulation_paused"]
            )
            assert paused["simulation_requested"] is True

            alarmed = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["communication_state"] == "suspected_drowning",
            )
            assert alarmed["communication_loss_ms"] >= 80
            delayed = client.get("/api/state").json()
            delayed_target = next(
                item
                for item in delayed["swimmers"]
                if item["id"] == swimmer["id"]
            )
            assert delayed_target["status"] == "drowning"
            assert delayed_target["device_alarm_override"] is True
            assert delayed["lifeguard_state"]["display_state"] == "lane1_drowning"

            assert client.patch(
                f"/api/dev/swimmers/{swimmer['id']}",
                json={"status": "normal"},
            ).status_code == 200
            resume = websocket.receive_json()
            assert resume["simulation_paused"] is False
            websocket.send_json(
                {
                    "type": "ack",
                    "schema_version": "1.0",
                    "message_id": resume["message_id"],
                    "accepted": True,
                }
            )
            resumed = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: not item["simulation_paused"],
            )
            assert resumed["simulation_requested"] is False
            for sequence in (1, 2, 3):
                websocket.send_json(swimmer_heartbeat(sequence))
                assert websocket.receive_json()["message_id"] == f"swimmer-hb-{sequence}"
            recovered = wait_for_device(
                client,
                "swimmer-a1b2",
                lambda item: item["communication_state"] == "normal",
            )
            assert recovered["recovery_heartbeat_count"] == 3


def test_signal_loss_simulation_requires_connected_pair(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json", enable_serial=False
    )
    with TestClient(application) as client:
        swimmer = client.post("/api/dev/swimmers", json={}).json()
        response = client.patch(
            f"/api/dev/swimmers/{swimmer['id']}",
            json={"status": "drowning"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == (
            "SIMULATION_REQUIRES_CONNECTED_PAIR"
        )
        snapshot = client.get("/api/state").json()
        assert snapshot["swimmers"][0]["status"] == "normal"
        assert snapshot["lifeguard_state"]["display_state"] == "safe"


def test_collision_pulses_only_paired_swimmer_devices(tmp_path: Path) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={"retry_delays": (0.05, 0.1, 0.2)},
    )
    with TestClient(application) as client:
        first = client.post("/api/dev/swimmers", json={}).json()
        second = client.post("/api/dev/swimmers", json={}).json()
        with client.websocket_connect("/ws/devices") as first_socket:
            with client.websocket_connect("/ws/devices") as second_socket:
                first_socket.send_json(swimmer_hello_payload("swimmer-first"))
                second_socket.send_json(swimmer_hello_payload("swimmer-second"))
                first_socket.receive_json()
                second_socket.receive_json()
                assert client.put(
                    "/api/devices/swimmer-first/binding",
                    json={"swimmer_id": first["id"]},
                ).status_code == 200
                assert client.put(
                    "/api/devices/swimmer-second/binding",
                    json={"swimmer_id": second["id"]},
                ).status_code == 200

                response = client.post(
                    "/api/dev/collisions",
                    json={
                        "swimmer_a": first["id"],
                        "swimmer_b": second["id"],
                        "active": True,
                    },
                )
                assert response.status_code == 204
                commands = [
                    first_socket.receive_json(),
                    second_socket.receive_json(),
                ]
                assert all(command["type"] == "swimmer_control_command" for command in commands)
                assert all(command["collision_pulse"] is True for command in commands)
                assert all(command["simulation_paused"] is False for command in commands)

                for websocket, command in zip(
                    (first_socket, second_socket), commands, strict=True
                ):
                    websocket.send_json(
                        {
                            "type": "ack",
                            "schema_version": "1.0",
                            "message_id": command["message_id"],
                            "accepted": True,
                        }
                    )
                wait_for_device(
                    client,
                    "swimmer-first",
                    lambda item: item["pending_command"] is None,
                )
                wait_for_device(
                    client,
                    "swimmer-second",
                    lambda item: item["pending_command"] is None,
                )

                # Repeated collision frames inside the cooldown must not create
                # continuous vibration.
                assert client.post(
                    "/api/dev/collisions",
                    json={
                        "swimmer_a": first["id"],
                        "swimmer_b": second["id"],
                        "active": True,
                    },
                ).status_code == 204
                time.sleep(0.02)
                devices = client.get("/api/devices").json()["devices"]
                assert all(item["pending_command"] is None for item in devices)


def test_disabled_rescue_stays_in_vibration_stage_after_twenty_seconds(
    tmp_path: Path,
) -> None:
    application = create_app(
        settings_path=tmp_path / "settings.json",
        enable_serial=False,
        device_hub_options={
            "swimmer_suspected_timeout": 0.03,
            "swimmer_rescue_timeout": 0.06,
            "watchdog_interval": 0.005,
            "retry_delays": (0.05, 0.1, 0.2),
        },
    )
    with TestClient(application) as client:
        swimmer = client.post("/api/dev/swimmers", json={}).json()
        with client.websocket_connect("/ws/devices") as websocket:
            websocket.send_json(swimmer_hello_payload())
            websocket.receive_json()
            client.put(
                "/api/devices/swimmer-a1b2/binding",
                json={"swimmer_id": swimmer["id"], "rescue_enabled": False},
            )
            control = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "ack",
                    "schema_version": "1.0",
                    "message_id": control["message_id"],
                    "accepted": True,
                }
            )
            websocket.send_json(swimmer_heartbeat(1))
            websocket.receive_json()
            time.sleep(0.1)
            device = client.get("/api/devices").json()["devices"][0]
            assert device["communication_state"] == "suspected_drowning"
            assert device["rescue_expected"] is False
