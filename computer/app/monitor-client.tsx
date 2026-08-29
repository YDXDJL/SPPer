"use client";

import {
  PointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import SiteHeader from "./site-header";
import {
  Position,
  Snapshot,
  VisionFrame,
  api,
  isLocalhost,
  statusLabel,
  useSystemState,
} from "./system";

type CalibrationPhase = "idle" | "first" | "second" | "confirm" | "saving";
type ReplayAction =
  | "select"
  | "play"
  | "pause"
  | "restart"
  | "stop"
  | "set_feedback";

const replaySamples = [
  { id: "oneline", label: "单人泳道" },
  { id: "twolines", label: "双泳道" },
  { id: "crashing", label: "碰撞预警" },
  { id: "drowning", label: "溺水检测" },
] as const;

const subscribeToHost = () => () => undefined;

const replayStateLabel = {
  idle: "未选择回放",
  ready: "准备播放",
  playing: "回放中",
  paused: "已暂停",
  ended: "播放完毕",
  error: "回放异常",
} as const;

function formatPlaybackTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "00:00";
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.floor(seconds % 60);
  return `${minutes.toString().padStart(2, "0")}:${remainder
    .toString()
    .padStart(2, "0")}`;
}

interface RoiShape {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

function drawPoolPlaceholder(
  canvas: HTMLCanvasElement,
  snapshot: Snapshot,
): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  canvas.width = 1280;
  canvas.height = 720;
  const gradient = ctx.createLinearGradient(0, 0, 0, canvas.height);
  gradient.addColorStop(0, "#0b4961");
  gradient.addColorStop(1, "#062d42");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = "rgba(130, 230, 255, 0.32)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(0, canvas.height / 2);
  ctx.lineTo(canvas.width, canvas.height / 2);
  ctx.stroke();

  for (const swimmer of snapshot.swimmers.filter((item) => item.online)) {
    const position = swimmer.pool_position ?? swimmer.position;
    const x = position.x * canvas.width;
    const y = position.y * canvas.height;
    ctx.beginPath();
    ctx.fillStyle =
      swimmer.status === "drowning" || swimmer.status === "suspected_drowning"
        ? "#ff5964"
        : swimmer.status === "collision"
          ? "#ffc857"
          : "#54e8c5";
    ctx.arc(x, y, 12, 0, Math.PI * 2);
    ctx.fill();
    ctx.font = "500 18px system-ui";
    ctx.fillStyle = "#f4fbff";
    ctx.fillText(swimmer.display_name, x + 20, y + 6);
  }

  if (!snapshot.swimmers.some((item) => item.online)) {
    ctx.textAlign = "center";
    ctx.fillStyle = "rgba(235, 248, 255, 0.72)";
    ctx.font = "500 24px system-ui";
    ctx.fillText("等待视觉画面或测试数据", canvas.width / 2, canvas.height / 2);
    ctx.textAlign = "start";
  }
}

function paintVision(
  canvas: HTMLCanvasElement,
  image: ImageBitmap,
  metadata: VisionFrame,
  roi: RoiShape | null,
  laneBoundaryY: number | null,
  laneBoundaryLine: { left_y: number; right_y: number } | null,
  replayAlertStage: "normal" | "suspected_drowning" | "high_risk",
  firstPoint: Position | null,
): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  canvas.width = image.width;
  canvas.height = image.height;
  ctx.drawImage(image, 0, 0);
  const warnings = metadata.collision_warnings ?? [];
  const warningTrackIds = new Set(
    warnings.flatMap((warning) => warning.track_ids.map(String)),
  );
  for (const track of metadata.tracks) {
    const [x1, y1, x2, y2] = track.bbox_xyxy;
    const collisionRisk = warningTrackIds.has(String(track.track_id));
    const drowningRisk =
      String(track.track_id) === "1" && replayAlertStage !== "normal";
    ctx.strokeStyle = drowningRisk
      ? "#ff5964"
      : collisionRisk
        ? "#ffc857"
        : "#54e8c5";
    ctx.lineWidth = Math.max(2, image.width / 640);
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    const label = `ID ${track.track_id}${
      drowningRisk
        ? replayAlertStage === "high_risk"
          ? "  高风险"
          : "  疑似溺水"
        : collisionRisk
          ? "  碰撞预警"
          : ""
    }`;
    ctx.font = `500 ${Math.max(14, image.width / 70)}px system-ui`;
    const width = ctx.measureText(label).width + 16;
    const labelY = Math.max(0, y1 - 30);
    ctx.fillStyle = "rgba(4, 24, 36, 0.9)";
    ctx.fillRect(x1, labelY, width, 28);
    ctx.fillStyle = "#ecfeff";
    ctx.fillText(label, x1 + 8, labelY + 20);
  }

  if (warnings.length > 0) {
    const ttcValues = warnings
      .map((warning) => warning.time_to_collision_s)
      .filter((value): value is number => typeof value === "number");
    const minimumTtc = ttcValues.length > 0 ? Math.min(...ttcValues) : null;
    const warningText =
      minimumTtc === null
        ? "碰撞预警"
        : `碰撞预警 · 预计 ${minimumTtc.toFixed(1)} 秒后接近`;
    ctx.font = `600 ${Math.max(16, image.width / 58)}px system-ui`;
    const warningWidth = ctx.measureText(warningText).width + 30;
    ctx.fillStyle = "rgba(70, 46, 5, 0.9)";
    ctx.fillRect(16, 16, warningWidth, 40);
    ctx.strokeStyle = "#ffc857";
    ctx.lineWidth = 2;
    ctx.strokeRect(16, 16, warningWidth, 40);
    ctx.fillStyle = "#fff4cf";
    ctx.fillText(warningText, 31, 44);
  }

  if (roi) {
    const x = roi.x1 * canvas.width;
    const y = roi.y1 * canvas.height;
    const width = (roi.x2 - roi.x1) * canvas.width;
    const height = (roi.y2 - roi.y1) * canvas.height;
    ctx.fillStyle = "rgba(72, 216, 241, 0.08)";
    ctx.fillRect(x, y, width, height);
    ctx.strokeStyle = "#48d8f1";
    ctx.lineWidth = Math.max(2, image.width / 640);
    ctx.strokeRect(x, y, width, height);
    ctx.setLineDash([12, 9]);
    ctx.beginPath();
    const fallbackY = laneBoundaryY ?? roi.y1 + (roi.y2 - roi.y1) / 2;
    const leftBoundaryY = (
      laneBoundaryLine
        ? laneBoundaryLine.left_y +
          (laneBoundaryLine.right_y - laneBoundaryLine.left_y) * roi.x1
        : fallbackY
    ) * canvas.height;
    const rightBoundaryY = (
      laneBoundaryLine
        ? laneBoundaryLine.left_y +
          (laneBoundaryLine.right_y - laneBoundaryLine.left_y) * roi.x2
        : fallbackY
    ) * canvas.height;
    ctx.moveTo(x, leftBoundaryY);
    ctx.lineTo(x + width, rightBoundaryY);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = `500 ${Math.max(15, image.width / 65)}px system-ui`;
    ctx.fillStyle = "#ecfeff";
    ctx.fillText("泳道 1", x + 12, y + 28);
    ctx.fillText("泳道 2", x + 12, leftBoundaryY + 28);
  }

  if (firstPoint) {
    const x = firstPoint.x * canvas.width;
    const y = firstPoint.y * canvas.height;
    ctx.strokeStyle = "#ffc857";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x - 14, y);
    ctx.lineTo(x + 14, y);
    ctx.moveTo(x, y - 14);
    ctx.lineTo(x, y + 14);
    ctx.stroke();
  }
}

export default function MonitorClient() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const currentImage = useRef<ImageBitmap | null>(null);
  const currentMetadata = useRef<VisionFrame | null>(null);
  const decodeToken = useRef(0);
  const snapshotRef = useRef<Snapshot | null>(null);
  const phaseRef = useRef<CalibrationPhase>("idle");
  const draftRef = useRef<{ first: Position | null; second: Position | null }>({
    first: null,
    second: null,
  });
  const [frameTime, setFrameTime] = useState<string | null>(null);
  const [phase, setPhase] = useState<CalibrationPhase>("idle");
  const [draft, setDraft] = useState<{
    first: Position | null;
    second: Position | null;
  }>({ first: null, second: null });
  const [error, setError] = useState("");
  const [replayBusy, setReplayBusy] = useState(false);
  const [selectedRescueDeviceId, setSelectedRescueDeviceId] = useState("");
  const adminAllowed = useSyncExternalStore(
    subscribeToHost,
    isLocalhost,
    () => false,
  );

  const redraw = useCallback(() => {
    const canvas = canvasRef.current;
    const snapshot = snapshotRef.current;
    if (!canvas || !snapshot) return;
    const image = currentImage.current;
    const metadata = currentMetadata.current;
    if (!image || !metadata) {
      drawPoolPlaceholder(canvas, snapshot);
      return;
    }
    const currentDraft = draftRef.current;
    const draftRoi =
      currentDraft.first && currentDraft.second
        ? {
            x1: currentDraft.first.x,
            y1: currentDraft.first.y,
            x2: currentDraft.second.x,
            y2: currentDraft.second.y,
          }
        : null;
    paintVision(
      canvas,
      image,
      metadata,
      draftRoi ?? snapshot.calibration.roi,
      draftRoi ? null : snapshot.calibration.lane_boundary_y,
      draftRoi ? null : snapshot.calibration.lane_boundary_line,
      snapshot.vision_replay.alert_stage,
      currentDraft.first && !currentDraft.second ? currentDraft.first : null,
    );
  }, []);

  const drawVisionFrame = useCallback(
    async (blob: Blob, metadata: VisionFrame) => {
      const token = ++decodeToken.current;
      const image = await createImageBitmap(blob);
      if (token !== decodeToken.current || phaseRef.current !== "idle") {
        image.close();
        return;
      }
      currentImage.current?.close();
      currentImage.current = image;
      currentMetadata.current = metadata;
      setFrameTime(
        new Intl.DateTimeFormat("zh-CN", {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }).format(new Date(metadata.captured_at)),
      );
      redraw();
    },
    [redraw],
  );

  const { snapshot, connected } = useSystemState(drawVisionFrame);
  const connectedSwimmerDevices = snapshot.devices.filter(
    (device) => device.device_type === "swimmer" && device.connected,
  );
  const replaySwimmerDevice = snapshot.devices.find(
    (device) =>
      device.device_type === "swimmer" &&
      device.connected &&
      device.bound_swimmer_id === "vision-1",
  );
  const latchedReplaySwimmerDevice = snapshot.devices.find(
    (device) =>
      device.device_type === "swimmer" &&
      device.bound_swimmer_id === "vision-1" &&
      (device.rescue_expected || device.rescue_confirmed),
  );
  const rescueDeviceOptions = snapshot.devices.filter(
    (device) =>
      device.device_type === "swimmer" &&
      (device.connected || device.rescue_expected || device.rescue_confirmed),
  );
  const rescueTargetDevice =
    rescueDeviceOptions.find(
      (device) => device.device_id === selectedRescueDeviceId,
    ) ??
    replaySwimmerDevice ??
    connectedSwimmerDevices[0] ??
    latchedReplaySwimmerDevice;
  const connectedSwimmerDeviceCount = connectedSwimmerDevices.length;
  const rescueTriggerEnabled = Boolean(
    adminAllowed &&
      rescueTargetDevice?.connected &&
      !rescueTargetDevice.rescue_expected &&
      !rescueTargetDevice.rescue_confirmed &&
      rescueTargetDevice.pending_command === null,
  );
  const rescueResetEnabled = Boolean(
    adminAllowed &&
      rescueTargetDevice?.connected &&
      rescueTargetDevice.rescue_confirmed &&
      rescueTargetDevice.communication_state === "normal" &&
      rescueTargetDevice.recovery_heartbeat_count >= 3 &&
      rescueTargetDevice.pending_command === null,
  );

  useEffect(() => {
    snapshotRef.current = snapshot;
    redraw();
  }, [snapshot, redraw]);

  useEffect(() => {
    phaseRef.current = phase;
    draftRef.current = draft;
    redraw();
  }, [phase, draft, redraw]);

  const startCalibration = () => {
    setError("");
    if (!adminAllowed) {
      setError("泳池区域只能在服务器本机设置");
      return;
    }
    if (!currentImage.current || !currentMetadata.current) {
      setError("请等待视觉模块传来最新画面");
      return;
    }
    phaseRef.current = "first";
    setDraft({ first: null, second: null });
    setPhase("first");
  };

  const handleCanvasClick = (event: PointerEvent<HTMLCanvasElement>) => {
    if (phase !== "first" && phase !== "second") return;
    const canvas = event.currentTarget;
    const rect = canvas.getBoundingClientRect();
    const point = {
      x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
    };
    if (phase === "first") {
      setDraft({ first: point, second: null });
      setPhase("second");
      return;
    }
    if (
      !draft.first ||
      point.x <= draft.first.x ||
      point.y <= draft.first.y
    ) {
      setError("右下角必须位于左上角的右下方");
      return;
    }
    setError("");
    setDraft({ first: draft.first, second: point });
    setPhase("confirm");
  };

  const cancelCalibration = () => {
    phaseRef.current = "idle";
    setPhase("idle");
    setDraft({ first: null, second: null });
    setError("");
  };

  const saveCalibration = async () => {
    const metadata = currentMetadata.current;
    if (!metadata || !draft.first || !draft.second) return;
    setPhase("saving");
    setError("");
    try {
      await api("/api/calibration/roi", {
        method: "PUT",
        body: JSON.stringify({
          frame_id: metadata.frame_id,
          top_left: draft.first,
          bottom_right: draft.second,
        }),
      });
      phaseRef.current = "idle";
      setPhase("idle");
      setDraft({ first: null, second: null });
    } catch (reason) {
      setPhase("confirm");
      setError(reason instanceof Error ? reason.message : "保存失败");
    }
  };

  const clearCalibration = async () => {
    setError("");
    try {
      await api("/api/calibration/roi", { method: "DELETE" });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "清除失败");
    }
  };

  const sendReplayCommand = async (
    action: ReplayAction,
    options: {
      sampleId?: "oneline" | "twolines" | "crashing" | "drowning";
      deviceFeedbackEnabled?: boolean;
    } = {},
  ) => {
    if (!adminAllowed) {
      setError("视频回放只能在服务器本机控制");
      return;
    }
    setReplayBusy(true);
    setError("");
    try {
      await api("/api/vision/replay/command", {
        method: "POST",
        body: JSON.stringify({
          action,
          ...(options.sampleId ? { sample_id: options.sampleId } : {}),
          ...(options.deviceFeedbackEnabled !== undefined
            ? { device_feedback_enabled: options.deviceFeedbackEnabled }
            : {}),
        }),
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "视频回放操作失败");
    } finally {
      setReplayBusy(false);
    }
  };

  const triggerReplayRescue = async () => {
    if (!rescueTargetDevice || !rescueTriggerEnabled) return;
    if (
      !window.confirm(
        `确认真实触发 ${rescueTargetDevice.device_id} 的救生装置？\n\n` +
      "该操作会立即驱动实体舵机执行 90°～0° 五轮动作并回到 90°；视频事件本身不会执行此操作。",
      )
    ) {
      return;
    }
    setReplayBusy(true);
    setError("");
    try {
      await api(
        `/api/devices/${encodeURIComponent(rescueTargetDevice.device_id)}/rescue-trigger`,
        { method: "POST" },
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "救生装置触发失败");
    } finally {
      setReplayBusy(false);
    }
  };

  const resetReplayRescue = async () => {
    if (!rescueTargetDevice || !rescueResetEnabled) return;
    setReplayBusy(true);
    setError("");
    try {
      await api(
        `/api/devices/${encodeURIComponent(rescueTargetDevice.device_id)}/rescue-reset`,
        { method: "POST" },
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "救生装置复位失败");
    } finally {
      setReplayBusy(false);
    }
  };

  const persistentDrowningMessage =
    snapshot.vision_replay.alert_stage === "suspected_drowning"
      ? "疑似溺水状态保持中：泳者端高频振动、RGB 红灯和救生员手环预警继续。"
      : snapshot.vision_replay.alert_stage === "high_risk"
        ? "高风险状态保持中：救生条件已达到，但不会自动下发实体救生信号。"
        : null;

  const calibrationHint = {
    idle:
      snapshot.calibration.mapping_source === "replay"
        ? "已按当前视频中的浮标绳自动映射两条泳道"
        : snapshot.calibration.roi_configured
          ? "泳池区域已保存"
          : "尚未设置泳池区域",
    first: "画面已冻结：请点击泳池左上角",
    second: "请点击泳池右下角",
    confirm: "确认区域，虚线为两条泳道分界",
    saving: "正在保存泳池区域",
  }[phase];

  return (
    <main className="app-shell">
      <SiteHeader active="monitor" connected={connected} />
      <section className="page-heading">
        <div>
          <p className="eyebrow">实时态势</p>
          <h1>泳池运行监控</h1>
        </div>
        <div
          className={`vision-state ${snapshot.vision_connected ? "online" : ""}`}
        >
          <span />
          {snapshot.vision_connected ? "视觉模块在线" : "视觉模块未接入"}
        </div>
      </section>

      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      {snapshot.lifeguard_state.unlocated_count > 0 && (
        <div className="danger-banner" role="alert">
          <strong>
            未定位疑似溺水 {snapshot.lifeguard_state.unlocated_count} 人
          </strong>
          <span>设备尚未绑定到有效泳者，已按两条泳道均有危险处理。</span>
        </div>
      )}

      <section className="monitor-grid">
        <article className="video-panel">
          <div className="panel-heading calibration-heading">
            <div>
              <h2>顶部摄像头</h2>
              <p>{calibrationHint}</p>
            </div>
            <div className="calibration-actions">
              {phase === "idle" ? (
                <>
                  <button
                    className="button compact"
                    disabled={
                      !adminAllowed ||
                      snapshot.calibration.mapping_source === "replay"
                    }
                    onClick={startCalibration}
                    type="button"
                  >
                    {snapshot.calibration.mapping_source === "replay"
                      ? "视频自动映射"
                      : snapshot.calibration.roi_configured
                      ? "重新设置区域"
                      : "设置泳池区域"}
                  </button>
                  {snapshot.calibration.roi_configured &&
                    snapshot.calibration.mapping_source !== "replay" && (
                    <button
                      className="button compact ghost danger-text"
                      disabled={!adminAllowed}
                      onClick={clearCalibration}
                      type="button"
                    >
                      清除
                    </button>
                  )}
                </>
              ) : (
                <>
                  {phase === "confirm" && (
                    <button
                      className="button compact primary"
                      onClick={saveCalibration}
                      type="button"
                    >
                      确认保存
                    </button>
                  )}
                  <button
                    className="button compact ghost"
                    disabled={phase === "saving"}
                    onClick={cancelCalibration}
                    type="button"
                  >
                    取消
                  </button>
                </>
              )}
            </div>
          </div>
          <div className="replay-toolbar" aria-label="识别日志视频回放控制">
            <div className="replay-samples" role="group" aria-label="选择演示视频">
              {replaySamples.map((sample) => (
                <button
                  className={`replay-sample ${
                    snapshot.vision_replay.sample_id === sample.id ? "active" : ""
                  }`}
                  disabled={!adminAllowed || replayBusy || phase !== "idle"}
                  key={sample.id}
                  onClick={() =>
                    sendReplayCommand("select", { sampleId: sample.id })
                  }
                  type="button"
                >
                  {sample.label}
                </button>
              ))}
            </div>
            <div className="replay-controls">
              <button
                className="button compact primary"
                disabled={
                  !adminAllowed ||
                  replayBusy ||
                  phase !== "idle" ||
                  !snapshot.vision_replay.sample_id ||
                  snapshot.vision_replay.state === "playing"
                }
                onClick={() => sendReplayCommand("play")}
                type="button"
              >
                {snapshot.vision_replay.state === "paused" ? "继续播放" : "播放"}
              </button>
              <button
                className="button compact"
                disabled={
                  !adminAllowed ||
                  replayBusy ||
                  phase !== "idle" ||
                  snapshot.vision_replay.state !== "playing"
                }
                onClick={() => sendReplayCommand("pause")}
                type="button"
              >
                暂停
              </button>
              <button
                className="button compact"
                disabled={
                  !adminAllowed ||
                  replayBusy ||
                  phase !== "idle" ||
                  !snapshot.vision_replay.sample_id
                }
                onClick={() => sendReplayCommand("restart")}
                type="button"
              >
                重播
              </button>
              <button
                className="button compact ghost"
                disabled={
                  !adminAllowed ||
                  replayBusy ||
                  phase !== "idle" ||
                  snapshot.vision_replay.state === "idle"
                }
                onClick={() => sendReplayCommand("stop")}
                type="button"
              >
                停止
              </button>
            </div>
            <label className="replay-feedback-toggle">
              <input
                checked={snapshot.vision_replay.device_feedback_enabled}
                disabled={!adminAllowed || replayBusy || phase !== "idle"}
                onChange={(event) =>
                  sendReplayCommand("set_feedback", {
                    deviceFeedbackEnabled: event.target.checked,
                  })
                }
                type="checkbox"
              />
              <span aria-hidden="true" />
              <strong>泳者端振动联动</strong>
            </label>
            <div className="replay-status" aria-live="polite">
              <span>
                {formatPlaybackTime(snapshot.vision_replay.position_s)} / {" "}
                {formatPlaybackTime(snapshot.vision_replay.duration_s)}
              </span>
              <span>
                帧 {snapshot.vision_replay.sample_id
                  ? snapshot.vision_replay.frame_index + 1
                  : 0}
              </span>
              {snapshot.vision_replay.error && (
                <span className="replay-error">{snapshot.vision_replay.error}</span>
              )}
            </div>
          </div>
          {snapshot.vision_replay.sample_id && connectedSwimmerDeviceCount > 1 &&
            !replaySwimmerDevice && (
              <div className="replay-device-notice" role="status">
                检测到多台泳者设备，已暂停泳者端振动反馈。请在设备配对窗口手动将目标设备配对到视觉泳者 1。
              </div>
            )}
          {snapshot.vision_replay.sample_id && connectedSwimmerDeviceCount === 0 && (
            <div className="replay-device-notice muted" role="status">
              当前没有在线泳者设备；画面、RGB 和救生员手环演示仍会正常运行。
            </div>
          )}
          <section className="replay-rescue-controls" aria-label="实体救生装置控制">
              <div>
                <strong>实体救生装置</strong>
                <span>
                  {rescueTargetDevice
                    ? rescueTargetDevice.connected
                      ? `当前目标：${rescueTargetDevice.device_id}`
                      : `装置仍锁存：${rescueTargetDevice.device_id}（设备离线）`
                    : "当前没有可控制的泳者设备"}
                </span>
              </div>
              {rescueDeviceOptions.length > 1 && (
                <label className="replay-rescue-target">
                  <span>目标设备</span>
                  <select
                    aria-label="救生装置目标设备"
                    onChange={(event) =>
                      setSelectedRescueDeviceId(event.target.value)
                    }
                    value={rescueTargetDevice?.device_id ?? ""}
                  >
                    {rescueDeviceOptions.map((device) => (
                      <option key={device.device_id} value={device.device_id}>
                        {device.device_id}{device.connected ? "（在线）" : "（离线锁存）"}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <div className="replay-rescue-actions">
                <button
                  className="button compact danger-button"
                  disabled={!rescueTriggerEnabled || replayBusy}
                  onClick={triggerReplayRescue}
                  type="button"
                >
                  触发救生装置
                </button>
                <button
                  className="button compact"
                  disabled={!rescueResetEnabled || replayBusy}
                  onClick={resetReplayRescue}
                  type="button"
                >
                  复位救生装置
                </button>
              </div>
              <p>
                可随时手动触发在线且空闲的泳者设备；视频演示节点不会自动下发实体信号。触发前必须再次确认，停止视频也不会复位已锁存的装置。
              </p>
              {rescueTargetDevice &&
                (rescueTargetDevice.rescue_expected ||
                  rescueTargetDevice.rescue_confirmed) && (
                <p className="replay-rescue-latched" role="alert">
                  实体救生装置已触发或等待确认，必须在设备恢复正常并满足条件后手动复位。
                </p>
              )}
            </section>
          <div className="video-stage">
            <canvas
              className={phase === "first" || phase === "second" ? "selecting" : ""}
              ref={canvasRef}
              aria-label="泳池视觉识别实时画面"
              onPointerDown={handleCanvasClick}
              role="img"
            />
            <div className="live-badge">
              <span />
              {phase !== "idle"
                ? "FROZEN"
                : snapshot.vision_replay.sample_id
                  ? snapshot.vision_replay.state === "playing"
                    ? "REPLAY"
                    : snapshot.vision_replay.state === "error"
                      ? "ERROR"
                      : "PAUSED"
                  : "LIVE"}
            </div>
            <div className="frame-time">
              {snapshot.vision_replay.sample_id
                ? `${formatPlaybackTime(snapshot.vision_replay.position_s)} · ${
                    replayStateLabel[snapshot.vision_replay.state]
                  }`
                : frameTime
                  ? `最近画面 ${frameTime}`
                  : "等待实时画面"}
            </div>
            {(snapshot.vision_replay.cue || persistentDrowningMessage) && (
              <aside
                className={`replay-cue ${
                  snapshot.vision_replay.cue?.severity ?? "danger"
                }`}
                role="alert"
                aria-live="assertive"
              >
                <span>
                  {snapshot.vision_replay.cue
                    ? `第 ${snapshot.vision_replay.cue.frame_number} 帧`
                    : "状态保持中"}
                </span>
                <strong>
                  {snapshot.vision_replay.cue?.title ??
                    (snapshot.vision_replay.alert_stage === "high_risk"
                      ? "高风险状态保持中"
                      : "疑似溺水状态保持中")}
                </strong>
                <p>
                  {snapshot.vision_replay.cue?.message ?? persistentDrowningMessage}
                </p>
                {snapshot.vision_replay.state === "paused" && (
                  <button
                    className="button compact primary"
                    disabled={!adminAllowed || replayBusy || phase !== "idle"}
                    onClick={() => sendReplayCommand("play")}
                    type="button"
                  >
                    继续播放
                  </button>
                )}
              </aside>
            )}
          </div>
        </article>

        <aside className="stats-column">
          <div className="stat-grid">
            <div className="stat-card primary">
              <span>在线泳者</span>
              <strong>{snapshot.stats.online}</strong>
              <small>当前监测人数</small>
            </div>
            <div className="stat-card safe">
              <span>状态正常</span>
              <strong>{snapshot.stats.normal}</strong>
              <small>无风险提示</small>
            </div>
            <div className="stat-card warning">
              <span>碰撞风险</span>
              <strong>{snapshot.stats.collision}</strong>
              <small>黄色预警</small>
            </div>
            <div className="stat-card danger">
              <span>溺水风险</span>
              <strong>
                {snapshot.stats.drowning + snapshot.stats.suspected_drowning}
              </strong>
              <small>疑似溺水与高风险</small>
            </div>
          </div>

          <article className="swimmer-panel">
            <div className="panel-heading compact">
              <div>
                <h2>泳者状态</h2>
                <p>按风险等级优先显示</p>
              </div>
            </div>
            <div className="swimmer-list">
              {snapshot.swimmers.length === 0 ? (
                <div className="empty-state">尚未发现泳者</div>
              ) : (
                [...snapshot.swimmers]
                  .sort(
                    (a, b) =>
                      ["drowning", "suspected_drowning", "collision", "normal"].indexOf(a.status) -
                      ["drowning", "suspected_drowning", "collision", "normal"].indexOf(b.status),
                  )
                  .map((swimmer) => (
                    <div className="swimmer-row" key={swimmer.id}>
                      <span className={`avatar ${swimmer.status}`}>
                        {swimmer.display_name.slice(-2)}
                      </span>
                      <span className="swimmer-name">
                        <strong>{swimmer.display_name}</strong>
                        <small>
                          {swimmer.in_pool
                            ? `泳道 ${swimmer.lane} · 灯 ${swimmer.led_index}`
                            : "泳池区域外或未校准"}
                        </small>
                      </span>
                      <span className={`status-pill ${swimmer.status}`}>
                        {swimmer.online ? statusLabel[swimmer.status] : "离线"}
                      </span>
                    </div>
                  ))
              )}
            </div>
          </article>
        </aside>
      </section>
    </main>
  );
}
