"use client";

import { useEffect, useRef, useState } from "react";

export type SwimmerStatus =
  | "normal"
  | "suspected_drowning"
  | "drowning"
  | "collision";

export interface Position {
  x: number;
  y: number;
}

export interface Swimmer {
  id: string;
  display_name: string;
  source: "vision" | "virtual" | "device";
  position: Position;
  frame_position: Position | null;
  pool_position: Position | null;
  in_pool: boolean;
  lane: 1 | 2 | null;
  led_index: number | null;
  status: SwimmerStatus;
  collision_with: string | null;
  online: boolean;
  last_seen_at: string;
  confidence: number | null;
  bbox_xyxy: [number, number, number, number] | null;
  device_alarm_override: boolean;
}

export interface DeviceSummary {
  device_id: string;
  device_type: "lifeguard_band" | "swimmer";
  firmware_version: string;
  boot_id: string | null;
  connected: boolean;
  session_id: string | null;
  peer_ip: string | null;
  connected_at: string;
  last_seen_at: string;
  last_heartbeat_at: string | null;
  uptime_ms: number | null;
  battery_percent: number | null;
  last_applied_revision: number | null;
  pending_revision: number | null;
  retry_count: number;
  last_error: string | null;
  bound_swimmer_id: string | null;
  binding_automatic: boolean;
  communication_state:
    | "normal"
    | "suspected_drowning"
    | "rescue_triggered"
    | "recovering"
    | null;
  communication_loss_ms: number | null;
  recovery_heartbeat_count: number;
  local_alert_stage:
    | "normal"
    | "suspected_drowning"
    | "rescue_triggered"
    | null;
  rescue_expected: boolean;
  rescue_confirmed: boolean;
  rescue_enabled: boolean;
  simulation_paused: boolean;
  simulation_requested: boolean;
  pending_command: string | null;
}

export interface LifeguardState {
  type: "lifeguard_state";
  schema_version: "1.0";
  server_instance_id: string;
  state_revision: number;
  generated_at: string;
  display_state:
    | "safe"
    | "lane1_drowning"
    | "lane2_drowning"
    | "both_drowning";
  drowning_lanes: Array<1 | 2>;
  drowning_counts: { lane1: number; lane2: number };
  unlocated_count: number;
  vibration: {
    enabled: boolean;
    pattern: "off" | "200ms_on_100ms_off";
  };
}

export interface Snapshot {
  type: "state";
  schema_version: "1.0";
  revision: number;
  vision_connected: boolean;
  vision_replay: {
    sample_id: "oneline" | "twolines" | "crashing" | "drowning" | null;
    state: "idle" | "ready" | "playing" | "paused" | "ended" | "error";
    frame_index: number;
    position_s: number;
    duration_s: number;
    device_feedback_enabled: boolean;
    alert_stage: "normal" | "suspected_drowning" | "high_risk";
    pause_reason: "manual" | "cue" | null;
    cue: {
      id: string;
      kind: "drowning" | "collision";
      phase: "suspected_drowning" | "high_risk" | "started" | "ended";
      frame_index: number;
      frame_number: number;
      title: string;
      message: string;
      severity: "warning" | "danger" | "safe";
      target_track_ids: number[];
    } | null;
    error: string | null;
  };
  device_server: {
    server_instance_id: string;
    heartbeat_interval_ms: number;
    heartbeat_timeout_ms: number;
    swimmer_heartbeat_interval_ms: number;
    swimmer_suspected_timeout_ms: number;
    swimmer_rescue_timeout_ms: number;
    connected_count: number;
    connected_by_type: { lifeguard_band: number; swimmer: number };
  };
  lifeguard_state: LifeguardState;
  devices: DeviceSummary[];
  calibration: {
    roi_configured: boolean;
    roi: {
      x1: number;
      y1: number;
      x2: number;
      y2: number;
    } | null;
    lane_boundary_y: number | null;
    lane_boundary_line: { left_y: number; right_y: number } | null;
    mapping_source: "manual" | "replay" | null;
    source_frame_id: string | null;
    source_image_width: number | null;
    source_image_height: number | null;
    updated_at: string | null;
    valid_for_current_stream: boolean;
  };
  rgb_gateway: {
    port: string | null;
    port_mode: "auto" | "manual";
    preferred_port: string | null;
    available_ports: string[];
    scanning_port: string | null;
    baud_rate: number;
    serial_connected: boolean;
    protocol_ready: boolean;
    device_id: string | null;
    firmware_version: string | null;
    server_ipv4: string | null;
    server_port: number;
    last_seen_at: string | null;
    last_frame_seq: number;
    last_ack_seq: number;
    last_ack_at: string | null;
    output_status: string;
    frames_sent: number;
    frames_acked: number;
    dropped_frames: number;
    last_error: string | null;
    wifi: {
      status: string;
      request_id: string | null;
      ssid: string | null;
      ip: string | null;
      rssi: number | null;
      channel: number | null;
      error_code: string | null;
      message: string | null;
      updated_at: string | null;
    };
    provision_broadcast: {
      sequence: number | null;
      sent: boolean | null;
      channel: number | null;
      successes: number | null;
      failures: number | null;
      updated_at: string | null;
    };
  };
  swimmers: Swimmer[];
  stats: {
    total: number;
    online: number;
    normal: number;
    drowning: number;
    suspected_drowning: number;
    collision: number;
  };
}

export interface VisionTrack {
  track_id: string | number;
  confidence: number;
  bbox_xyxy: [number, number, number, number];
  center_normalized: [number, number];
}

export interface VisionCollisionWarning {
  track_ids: [string | number, string | number];
  time_to_collision_s?: number | null;
  minimum_distance_px?: number | null;
  warning_distance_px?: number | null;
}

export interface VisionFrame {
  type: "vision_frame";
  schema_version: "1.0";
  frame_id: string;
  captured_at: string;
  image_width: number;
  image_height: number;
  tracks: VisionTrack[];
  collision_warnings?: VisionCollisionWarning[];
}

const emptySnapshot: Snapshot = {
  type: "state",
  schema_version: "1.0",
  revision: 0,
  vision_connected: false,
  vision_replay: {
    sample_id: null,
    state: "idle",
    frame_index: 0,
    position_s: 0,
    duration_s: 0,
    device_feedback_enabled: true,
    alert_stage: "normal",
    pause_reason: null,
    cue: null,
    error: null,
  },
  device_server: {
    server_instance_id: "",
    heartbeat_interval_ms: 5000,
    heartbeat_timeout_ms: 15000,
    swimmer_heartbeat_interval_ms: 1000,
    swimmer_suspected_timeout_ms: 10000,
    swimmer_rescue_timeout_ms: 20000,
    connected_count: 0,
    connected_by_type: { lifeguard_band: 0, swimmer: 0 },
  },
  lifeguard_state: {
    type: "lifeguard_state",
    schema_version: "1.0",
    server_instance_id: "",
    state_revision: 1,
    generated_at: "",
    display_state: "safe",
    drowning_lanes: [],
    drowning_counts: { lane1: 0, lane2: 0 },
    unlocated_count: 0,
    vibration: { enabled: false, pattern: "off" },
  },
  devices: [],
  calibration: {
    roi_configured: false,
    roi: null,
    lane_boundary_y: null,
    lane_boundary_line: null,
    mapping_source: null,
    source_frame_id: null,
    source_image_width: null,
    source_image_height: null,
    updated_at: null,
    valid_for_current_stream: false,
  },
  rgb_gateway: {
    port: null,
    port_mode: "auto",
    preferred_port: null,
    available_ports: [],
    scanning_port: null,
    baud_rate: 115200,
    serial_connected: false,
    protocol_ready: false,
    device_id: null,
    firmware_version: null,
    server_ipv4: null,
    server_port: 8000,
    last_seen_at: null,
    last_frame_seq: 0,
    last_ack_seq: 0,
    last_ack_at: null,
    output_status: "starting",
    frames_sent: 0,
    frames_acked: 0,
    dropped_frames: 0,
    last_error: null,
    wifi: {
      status: "unknown",
      request_id: null,
      ssid: null,
      ip: null,
      rssi: null,
      channel: null,
      error_code: null,
      message: null,
      updated_at: null,
    },
    provision_broadcast: {
      sequence: null,
      sent: null,
      channel: null,
      successes: null,
      failures: null,
      updated_at: null,
    },
  },
  swimmers: [],
  stats: {
    total: 0,
    online: 0,
    normal: 0,
    drowning: 0,
    suspected_drowning: 0,
    collision: 0,
  },
};

export function websocketUrl(path: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}`;
}

export function isLocalhost(): boolean {
  if (typeof window === "undefined") return false;
  return ["127.0.0.1", "localhost", "::1", "[::1]"].includes(
    window.location.hostname,
  );
}

export function useSystemState(
  onFrame?: (frame: Blob, metadata: VisionFrame) => void,
) {
  const [snapshot, setSnapshot] = useState<Snapshot>(emptySnapshot);
  const [connected, setConnected] = useState(false);
  const pendingMetadata = useRef<VisionFrame | null>(null);
  const frameHandler = useRef(onFrame);

  useEffect(() => {
    frameHandler.current = onFrame;
  }, [onFrame]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;

    const connect = () => {
      socket = new WebSocket(websocketUrl("/ws/dashboard"));
      socket.binaryType = "blob";
      socket.onopen = () => setConnected(true);
      socket.onclose = () => {
        setConnected(false);
        if (!stopped) reconnectTimer = setTimeout(connect, 1500);
      };
      socket.onerror = () => socket?.close();
      socket.onmessage = (event) => {
        if (typeof event.data === "string") {
          const message = JSON.parse(event.data);
          if (message.type === "state") setSnapshot(message);
          if (message.type === "vision_frame") pendingMetadata.current = message;
          return;
        }
        const metadata = pendingMetadata.current;
        if (metadata && event.data instanceof Blob) {
          frameHandler.current?.(event.data, metadata);
        }
      };
    };

    connect();
    return () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, []);

  return { snapshot, connected };
}

export async function api<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail || `请求失败：${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const statusLabel: Record<SwimmerStatus, string> = {
  normal: "正常",
  suspected_drowning: "疑似溺水",
  drowning: "高风险",
  collision: "碰撞风险",
};
