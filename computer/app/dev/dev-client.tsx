"use client";

import {
  DragEvent,
  FormEvent,
  PointerEvent,
  useEffect,
  useMemo,
  useState,
} from "react";
import SiteHeader from "../site-header";
import {
  Swimmer,
  api,
  isLocalhost,
  statusLabel,
  useSystemState,
} from "../system";

export default function DevClient() {
  const { snapshot, connected } = useSystemState();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [collisionTarget, setCollisionTarget] = useState("");
  const [error, setError] = useState("");
  const [wifiSsid, setWifiSsid] = useState("");
  const [wifiPassword, setWifiPassword] = useState("");
  const [hardwareNotice, setHardwareNotice] = useState("");
  const [pairingOpen, setPairingOpen] = useState(false);
  const [draftBindings, setDraftBindings] = useState<Record<string, string | null>>(
    {},
  );
  const [draftRescueEnabled, setDraftRescueEnabled] = useState<
    Record<string, boolean>
  >({});
  const [selectedPairSwimmer, setSelectedPairSwimmer] = useState<string | null>(
    null,
  );
  const [savingPairings, setSavingPairings] = useState(false);
  const adminAllowed = isLocalhost();

  const selected =
    snapshot.swimmers.find((item) => item.id === selectedId) ?? null;
  const onlineSwimmers = snapshot.swimmers.filter((item) => item.online);
  const collisionCandidates = useMemo(
    () => onlineSwimmers.filter((item) => item.id !== selectedId),
    [onlineSwimmers, selectedId],
  );
  const selectableGatewayPorts = useMemo(
    () =>
      Array.from(
        new Set(
          [
            ...snapshot.rgb_gateway.available_ports,
            snapshot.rgb_gateway.preferred_port,
            snapshot.rgb_gateway.port,
          ].filter((port): port is string => Boolean(port)),
        ),
      ),
    [
      snapshot.rgb_gateway.available_ports,
      snapshot.rgb_gateway.port,
      snapshot.rgb_gateway.preferred_port,
    ],
  );
  const lifeguardStateLabel = {
    safe: "安全",
    lane1_drowning: "泳道 1 溺水危险",
    lane2_drowning: "泳道 2 溺水危险",
    both_drowning: "两道溺水危险",
  }[snapshot.lifeguard_state.display_state];
  const lifeguardDevices = snapshot.devices.filter(
    (device) => device.device_type === "lifeguard_band",
  );
  const swimmerDevices = snapshot.devices.filter(
    (device) => device.device_type === "swimmer",
  );
  const connectedSwimmerDevices = swimmerDevices.filter(
    (device) => device.connected,
  );
  const selectedSwimmerDevice = selected
    ? swimmerDevices.find((device) => device.bound_swimmer_id === selected.id) ??
      null
    : null;
  const selectedSignalLossRequested =
    selectedSwimmerDevice?.simulation_requested ?? false;

  useEffect(() => {
    if (!pairingOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPairingOpen(false);
    };
    document.body.classList.add("modal-open");
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.classList.remove("modal-open");
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [pairingOpen]);

  const communicationStateLabel = (
    state:
      | "normal"
      | "suspected_drowning"
      | "rescue_triggered"
      | "recovering"
      | null,
  ) => {
    if (!state) return "等待首次心跳";
    return (
      {
        normal: "通信正常",
        recovering: "通信恢复确认中",
        suspected_drowning: "疑似溺水",
        rescue_triggered: "救生已触发",
      }[state] ?? state
    );
  };

  const localAlertLabel = {
    normal: "本机正常",
    suspected_drowning: "本机高频振动",
    rescue_triggered: "本机救生锁存",
  } as const;

  const formatLoss = (milliseconds: number | null) => {
    if (milliseconds === null || milliseconds === undefined) return "—";
    return `${(milliseconds / 1000).toFixed(1)} 秒`;
  };

  const run = async (action: () => Promise<unknown>) => {
    setError("");
    try {
      await action();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    }
  };

  const addSwimmer = (event: FormEvent) => {
    event.preventDefault();
    run(async () => {
      const created = await api<Swimmer>("/api/dev/swimmers", {
        method: "POST",
        body: JSON.stringify({
          display_name: name.trim() || null,
          position: { x: 0.5, y: 0.5 },
        }),
      });
      setName("");
      setSelectedId(created.id);
    });
  };

  const updateSelected = (payload: object) => {
    if (!adminAllowed) {
      setError("修改操作只能在服务器本机执行");
      return;
    }
    if (!selected || selected.source !== "virtual") return;
    run(() =>
      api(`/api/dev/swimmers/${selected.id}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    );
  };

  const setSignalLossSimulation = (paused: boolean) => {
    if (!selected || selected.source !== "virtual") return;
    run(async () => {
      await api(`/api/dev/swimmers/${selected.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: paused ? "drowning" : "normal" }),
      });
      setHardwareNotice(
        paused
          ? "已让配对泳者端停止业务通信；系统将在心跳超时后自然判定异常"
          : "已恢复配对泳者端通信；连续 3 次有效心跳后解除异常",
      );
    });
  };

  const placeSelected = (event: PointerEvent<HTMLDivElement>) => {
    if (!adminAllowed) return;
    if (!selected || selected.source !== "virtual") return;
    const box = event.currentTarget.getBoundingClientRect();
    updateSelected({
      position: {
        x: Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)),
        y: Math.max(0, Math.min(1, (event.clientY - box.top) / box.height)),
      },
    });
  };

  const reconnectGateway = () => {
    run(async () => {
      await api("/api/gateway/reconnect", { method: "POST" });
      setHardwareNotice("正在重新连接 RGB 网关");
    });
  };

  const changeGatewayPort = (port: string) => {
    run(async () => {
      await api("/api/gateway/port", {
        method: "PUT",
        body: JSON.stringify({ port: port === "auto" ? null : port }),
      });
      setHardwareNotice(
        port === "auto" ? "已切换为自动识别串口" : `已选择串口 ${port}`,
      );
    });
  };

  const configureWifi = (event: FormEvent) => {
    event.preventDefault();
    run(async () => {
      const response = await api<{ request_id: string }>(
        "/api/gateway/wifi",
        {
          method: "POST",
          body: JSON.stringify({
            ssid: wifiSsid,
            password: wifiPassword,
          }),
        },
      );
      setWifiPassword("");
      setHardwareNotice(`配网指令已发送：${response.request_id}`);
    });
  };

  const openPairing = () => {
    setDraftBindings(
      Object.fromEntries(
        swimmerDevices.map((device) => [
          device.device_id,
          device.bound_swimmer_id,
        ]),
      ),
    );
    setDraftRescueEnabled(
      Object.fromEntries(
        swimmerDevices.map((device) => [
          device.device_id,
          device.rescue_enabled,
        ]),
      ),
    );
    setSelectedPairSwimmer(null);
    setPairingOpen(true);
  };

  const pairDraft = (deviceId: string, swimmerId: string) => {
    setDraftBindings((current) => {
      const next = { ...current };
      for (const [otherDeviceId, boundSwimmerId] of Object.entries(next)) {
        if (boundSwimmerId === swimmerId && otherDeviceId !== deviceId) {
          next[otherDeviceId] = null;
        }
      }
      next[deviceId] = swimmerId;
      return next;
    });
    setSelectedPairSwimmer(null);
  };

  const unpairDraft = (deviceId: string) => {
    setDraftBindings((current) => ({ ...current, [deviceId]: null }));
  };

  const startPairDrag = (
    event: DragEvent<HTMLElement>,
    kind: "swimmer" | "device",
    id: string,
  ) => {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData(
      "application/x-spp-pairing",
      JSON.stringify({ kind, id }),
    );
  };

  const readPairDrag = (event: DragEvent<HTMLElement>) => {
    try {
      return JSON.parse(
        event.dataTransfer.getData("application/x-spp-pairing"),
      ) as { kind: "swimmer" | "device"; id: string };
    } catch {
      return null;
    }
  };

  const dropOnDevice = (event: DragEvent<HTMLElement>, deviceId: string) => {
    event.preventDefault();
    const source = readPairDrag(event);
    if (source?.kind === "swimmer") pairDraft(deviceId, source.id);
  };

  const dropOnSwimmer = (event: DragEvent<HTMLElement>, swimmerId: string) => {
    event.preventDefault();
    const source = readPairDrag(event);
    if (source?.kind === "device") pairDraft(source.id, swimmerId);
  };

  const savePairings = async () => {
    setError("");
    setSavingPairings(true);
    try {
      const currentDevices = snapshot.devices.filter(
        (device) => device.device_type === "swimmer",
      );

      // 先释放变化过的旧绑定，保证交换设备时不会触发一对一约束冲突。
      for (const device of currentDevices) {
        const target = draftBindings[device.device_id] ?? null;
        if (
          device.bound_swimmer_id &&
          device.bound_swimmer_id !== target
        ) {
          await api(
            `/api/devices/${encodeURIComponent(device.device_id)}/binding`,
            { method: "DELETE" },
          );
        }
      }

      for (const device of currentDevices) {
        const target = draftBindings[device.device_id] ?? null;
        if (
          target &&
          (target !== device.bound_swimmer_id ||
            draftRescueEnabled[device.device_id] !== device.rescue_enabled)
        ) {
          await api(
            `/api/devices/${encodeURIComponent(device.device_id)}/binding`,
            {
              method: "PUT",
              body: JSON.stringify({
                swimmer_id: target,
                rescue_enabled: draftRescueEnabled[device.device_id] ?? true,
              }),
            },
          );
        }
      }
      setHardwareNotice("泳者设备配对已保存");
      setPairingOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存配对失败");
    } finally {
      setSavingPairings(false);
    }
  };

  const triggerRescue = (deviceId: string) => {
    if (
      !window.confirm(
        `确认让 ${deviceId} 立即进入救生状态？该操作用于测试硬件。`,
      )
    ) {
      return;
    }
    run(async () => {
      await api(`/api/devices/${encodeURIComponent(deviceId)}/rescue-trigger`, {
        method: "POST",
      });
      setHardwareNotice(`已向 ${deviceId} 发送一键救生指令`);
    });
  };

  const resetRescue = (deviceId: string) => {
    run(async () => {
      await api(`/api/devices/${encodeURIComponent(deviceId)}/rescue-reset`, {
        method: "POST",
      });
      setHardwareNotice(`已向 ${deviceId} 发送救生复位命令，等待设备确认`);
    });
  };

  return (
    <main className="app-shell">
      <SiteHeader active="dev" connected={connected} />
      <section className="page-heading">
        <div>
          <p className="eyebrow">独立沙盒</p>
          <h1>开发测试界面</h1>
        </div>
        <span className="test-badge">不依赖视觉模块</span>
      </section>

      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      {!adminAllowed && (
        <div className="error-banner" role="status">
          当前通过局域网访问：位置、状态、ROI 和配网修改仅允许在服务器本机执行。
        </div>
      )}

      <section className="dev-grid">
        <aside className="dev-sidebar">
          <article className="tool-panel">
            <div className="panel-heading compact">
              <div>
                <h2>在线泳者</h2>
                <p>{onlineSwimmers.length} 人正在测试场景中</p>
              </div>
            </div>
            <form className="add-form" onSubmit={addSwimmer}>
              <label>
                <span>泳者名称</span>
                <input
                  maxLength={40}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="例如：泳者 A"
                  value={name}
                />
              </label>
              <button
                className="button primary"
                disabled={!adminAllowed}
                type="submit"
              >
                增加虚拟泳者
              </button>
            </form>
            <div className="dev-swimmer-list">
              {onlineSwimmers.length === 0 ? (
                <div className="empty-state">增加一名虚拟泳者开始测试</div>
              ) : (
                onlineSwimmers.map((swimmer) => (
                  <button
                    className={`dev-swimmer-item ${
                      selectedId === swimmer.id ? "selected" : ""
                    }`}
                    key={swimmer.id}
                    onClick={() => {
                      setSelectedId(swimmer.id);
                      setCollisionTarget("");
                    }}
                    type="button"
                  >
                    <span className={`avatar ${swimmer.status}`}>
                      {swimmer.display_name.slice(-2)}
                    </span>
                    <span>
                      <strong>{swimmer.display_name}</strong>
                      <small>{statusLabel[swimmer.status]}</small>
                    </span>
                  </button>
                ))
              )}
            </div>
          </article>
        </aside>

        <article className="pool-editor-panel">
          <div className="panel-heading">
            <div>
              <h2>位置模拟</h2>
              <p>选择虚拟泳者后，在泳池任意位置点击即可移动</p>
            </div>
            {selected && <span>当前：{selected.display_name}</span>}
          </div>
          <div
            className={`pool-editor ${selected ? "can-place" : ""}`}
            onPointerDown={placeSelected}
            role="application"
            aria-label="泳者位置模拟泳池"
          >
            {Array.from({ length: 2 }, (_, index) => (
              <div className="lane-label" key={index}>
                泳道 {index + 1}
              </div>
            ))}
            {onlineSwimmers.map((swimmer) => (
              <button
                aria-label={`选择 ${swimmer.display_name}`}
                className={`pool-marker ${swimmer.status} ${
                  selectedId === swimmer.id ? "selected" : ""
                }`}
                key={swimmer.id}
                onPointerDown={(event) => {
                  event.stopPropagation();
                  setSelectedId(swimmer.id);
                }}
                style={{
                  left: `${swimmer.position.x * 100}%`,
                  top: `${swimmer.position.y * 100}%`,
                }}
                type="button"
              >
                <span>{swimmer.display_name}</span>
              </button>
            ))}
          </div>
        </article>

        <aside className="control-panel">
          <div className="panel-heading compact">
            <div>
              <h2>状态控制</h2>
              <p>设置位置与危险场景</p>
            </div>
          </div>
          {!selected ? (
            <div className="empty-state tall">请先选择一名泳者</div>
          ) : selected.source !== "virtual" ? (
            <div className="empty-state tall">视觉泳者仅供查看，不能手动修改</div>
          ) : (
            <div className="control-stack">
              <div className="selected-summary">
                <span className={`avatar large ${selected.status}`}>
                  {selected.display_name.slice(-2)}
                </span>
                <span>
                  <strong>{selected.display_name}</strong>
                  <small>{selected.id}</small>
                </span>
              </div>

              <fieldset>
                <legend>精确位置</legend>
                <div className="coordinate-grid">
                  <label>
                    <span>横向 X</span>
                    <input
                      max="1"
                      min="0"
                      onChange={(event) =>
                        updateSelected({
                          position: {
                            x: Number(event.target.value),
                            y: selected.position.y,
                          },
                        })
                      }
                      step="0.01"
                      type="number"
                      value={selected.position.x.toFixed(2)}
                    />
                  </label>
                  <label>
                    <span>纵向 Y</span>
                    <input
                      max="1"
                      min="0"
                      onChange={(event) =>
                        updateSelected({
                          position: {
                            x: selected.position.x,
                            y: Number(event.target.value),
                          },
                        })
                      }
                      step="0.01"
                      type="number"
                      value={selected.position.y.toFixed(2)}
                    />
                  </label>
                </div>
              </fieldset>

              <fieldset>
                <legend>单人状态</legend>
                <div className="segmented">
                  <button
                    className={
                      !selectedSignalLossRequested && selected.status === "normal"
                        ? "active"
                        : ""
                    }
                    disabled={!adminAllowed}
                    onClick={() => setSignalLossSimulation(false)}
                    type="button"
                  >
                    正常
                  </button>
                  <button
                    className={selectedSignalLossRequested ? "danger" : ""}
                    disabled={!adminAllowed || !selectedSwimmerDevice?.connected}
                    onClick={() => setSignalLossSimulation(true)}
                    type="button"
                  >
                    模拟下沉（停止通信）
                  </button>
                </div>
                <p className="field-hint">
                  {selectedSwimmerDevice?.connected
                    ? "点击后不会立即报警；10 秒未收到心跳才判为疑似溺水，20 秒后按救生开关处理。"
                    : "请先在泳者配对窗口中连接一个在线泳者设备。"}
                </p>
              </fieldset>

              <fieldset>
                <legend>两人碰撞</legend>
                <label>
                  <span>选择另一名泳者</span>
                  <select
                    onChange={(event) => setCollisionTarget(event.target.value)}
                    value={collisionTarget}
                  >
                    <option value="">请选择</option>
                    {collisionCandidates.map((swimmer) => (
                      <option key={swimmer.id} value={swimmer.id}>
                        {swimmer.display_name}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  className="button warning"
                  disabled={!adminAllowed || !collisionTarget}
                  onClick={() =>
                    run(() =>
                      api("/api/dev/collisions", {
                        method: "POST",
                        body: JSON.stringify({
                          swimmer_a: selected.id,
                          swimmer_b: collisionTarget,
                          active: true,
                        }),
                      }),
                    )
                  }
                  type="button"
                >
                  设置两者碰撞并短振提醒
                </button>
              </fieldset>

              <button
                className="button ghost danger-text"
                disabled={!adminAllowed}
                onClick={() =>
                  run(async () => {
                    await api(`/api/dev/swimmers/${selected.id}`, {
                      method: "DELETE",
                    });
                    setSelectedId(null);
                  })
                }
                type="button"
              >
                删除虚拟泳者
              </button>
            </div>
          )}
        </aside>
      </section>

      <section className="hardware-panel">
        <div className="panel-heading">
          <div>
            <h2>RGB 网关与无线配网</h2>
            <p>自动识别或手动选择串口 · 115200 · 两道各 70 灯</p>
          </div>
          <span
            className={`gateway-badge ${
              snapshot.rgb_gateway.protocol_ready ? "online" : ""
            }`}
          >
            {snapshot.rgb_gateway.protocol_ready
              ? "网关已就绪"
              : snapshot.rgb_gateway.serial_connected
                ? "等待握手"
                : "串口未连接"}
          </span>
        </div>
        <div className="hardware-grid">
          <div className="gateway-details">
            <label className="gateway-port-select">
              <span>串口模式</span>
              <select
                disabled={!adminAllowed}
                onChange={(event) => changeGatewayPort(event.target.value)}
                value={
                  snapshot.rgb_gateway.port_mode === "auto"
                    ? "auto"
                    : snapshot.rgb_gateway.preferred_port ??
                      snapshot.rgb_gateway.port ??
                      "auto"
                }
              >
                <option value="auto">自动识别（推荐）</option>
                {selectableGatewayPorts.map((port) => (
                  <option key={port} value={port}>
                    {port}
                  </option>
                ))}
              </select>
              {snapshot.rgb_gateway.scanning_port && (
                <small>正在测试 {snapshot.rgb_gateway.scanning_port}</small>
              )}
            </label>
            <div>
              <span>当前串口</span>
              <strong>
                {snapshot.rgb_gateway.port ?? "尚未识别"} /{" "}
                {snapshot.rgb_gateway.baud_rate}
              </strong>
            </div>
            <div>
              <span>输出状态</span>
              <strong>{snapshot.rgb_gateway.output_status}</strong>
            </div>
            <div>
              <span>帧确认</span>
              <strong>
                {snapshot.rgb_gateway.last_ack_seq} /{" "}
                {snapshot.rgb_gateway.last_frame_seq}
              </strong>
            </div>
            <div>
              <span>设备版本</span>
              <strong>
                {snapshot.rgb_gateway.firmware_version ?? "尚未上报"}
              </strong>
            </div>
            <div>
              <span>手环服务端点</span>
              <strong>
                {snapshot.rgb_gateway.server_ipv4
                  ? `${snapshot.rgb_gateway.server_ipv4}:${snapshot.rgb_gateway.server_port}`
                  : "等待网关上报局域网 IP"}
              </strong>
            </div>
            <button
              className="button ghost"
              disabled={!adminAllowed}
              onClick={reconnectGateway}
              type="button"
            >
              重新扫描 / 连接
            </button>
            {snapshot.rgb_gateway.last_error && (
              <p className="hardware-error">
                {snapshot.rgb_gateway.last_error}
              </p>
            )}
          </div>

          <form className="wifi-form" onSubmit={configureWifi}>
            <label>
              <span>Wi-Fi 名称（SSID）</span>
              <input
                autoComplete="off"
                disabled={!adminAllowed}
                onChange={(event) => setWifiSsid(event.target.value)}
                placeholder="输入泳池现场 Wi-Fi"
                required
                value={wifiSsid}
              />
            </label>
            <label>
              <span>Wi-Fi 密码</span>
              <input
                autoComplete="new-password"
                disabled={!adminAllowed}
                onChange={(event) => setWifiPassword(event.target.value)}
                placeholder="开放网络可留空"
                type="password"
                value={wifiPassword}
              />
            </label>
            <button
              className="button primary"
              disabled={
                !adminAllowed ||
                !snapshot.rgb_gateway.protocol_ready ||
                snapshot.rgb_gateway.wifi.status === "configuring"
              }
              type="submit"
            >
              {snapshot.rgb_gateway.wifi.status === "configuring"
                ? "正在配网"
                : "发送配网信息"}
            </button>
            {hardwareNotice && <small>{hardwareNotice}</small>}
          </form>

          <div className="wifi-status-card">
            <span>当前 Wi-Fi 状态</span>
            <strong>{snapshot.rgb_gateway.wifi.status}</strong>
            <dl>
              <div>
                <dt>SSID</dt>
                <dd>{snapshot.rgb_gateway.wifi.ssid ?? "—"}</dd>
              </div>
              <div>
                <dt>IP</dt>
                <dd>{snapshot.rgb_gateway.wifi.ip ?? "—"}</dd>
              </div>
              <div>
                <dt>信号</dt>
                <dd>
                  {snapshot.rgb_gateway.wifi.rssi === null
                    ? "—"
                    : `${snapshot.rgb_gateway.wifi.rssi} dBm`}
                </dd>
              </div>
              <div>
                <dt>信道</dt>
                <dd>{snapshot.rgb_gateway.wifi.channel ?? "—"}</dd>
              </div>
            </dl>
            {snapshot.rgb_gateway.wifi.error_code && (
              <p className="hardware-error">
                {snapshot.rgb_gateway.wifi.error_code}
                {snapshot.rgb_gateway.wifi.message
                  ? `：${snapshot.rgb_gateway.wifi.message}`
                  : ""}
              </p>
            )}
            <div className="broadcast-status">
              广播序号{" "}
              {snapshot.rgb_gateway.provision_broadcast.sequence ?? "—"} · 成功{" "}
              {snapshot.rgb_gateway.provision_broadcast.successes ?? "—"} · 失败{" "}
              {snapshot.rgb_gateway.provision_broadcast.failures ?? "—"}
            </div>
          </div>
        </div>
      </section>

      <section className="hardware-panel device-panel">
        <div className="panel-heading">
          <div>
            <h2>可穿戴设备</h2>
            <p>
              /ws/devices · 泳者端 1 秒心跳 · 手环 5 秒心跳 · 状态版本{" "}
              {snapshot.lifeguard_state.state_revision}
            </p>
          </div>
          <div className="panel-heading-actions">
            <button
              className="button compact primary"
              disabled={!adminAllowed}
              onClick={openPairing}
              type="button"
            >
              泳者设备配对
            </button>
            <span
              className={`gateway-badge ${
                snapshot.device_server.connected_count > 0 ? "online" : ""
              }`}
            >
              在线 {snapshot.device_server.connected_count} 台
            </span>
          </div>
        </div>
        <div className="device-summary-grid">
          <article className="lifeguard-state-card">
            <span>救生员手环当前下发状态</span>
            <strong>{lifeguardStateLabel}</strong>
            <small>
              1 道 {snapshot.lifeguard_state.drowning_counts.lane1} 人 · 2 道{" "}
              {snapshot.lifeguard_state.drowning_counts.lane2} 人
              {snapshot.lifeguard_state.unlocated_count > 0
                ? ` · 未定位 ${snapshot.lifeguard_state.unlocated_count} 人`
                : ""}
            </small>
            <small>
              振动：{snapshot.lifeguard_state.vibration.pattern}
            </small>
          </article>
          <div className="device-groups">
            <section className="device-group">
              <div className="device-group-heading">
                <strong>救生员手环</strong>
                <span>{lifeguardDevices.length} 台已注册</span>
              </div>
              <div className="device-list">
                {lifeguardDevices.length === 0 ? (
                  <p>尚无救生员手环注册。</p>
                ) : (
                  lifeguardDevices.map((device) => (
                    <article key={device.device_id}>
                      <div>
                        <strong>{device.device_id}</strong>
                        <span>{device.firmware_version}</span>
                      </div>
                      <div>
                        <span className={device.connected ? "online-text" : ""}>
                          {device.connected ? "已连接" : "已断开"}
                        </span>
                        <span>
                          电量 {device.battery_percent ?? "—"}% · 版本{" "}
                          {device.last_applied_revision ?? "—"}
                        </span>
                      </div>
                      {device.last_error && (
                        <small className="hardware-error">{device.last_error}</small>
                      )}
                    </article>
                  ))
                )}
              </div>
            </section>

            <section className="device-group">
              <div className="device-group-heading">
                <strong>泳者设备</strong>
                <span>{swimmerDevices.length} 台已注册</span>
              </div>
              <div className="swimmer-device-list">
                {swimmerDevices.length === 0 ? (
                  <p>尚无泳者设备注册。设备入网后可通过配对窗口关联泳者。</p>
                ) : (
                  swimmerDevices.map((device) => {
                    const boundSwimmer = snapshot.swimmers.find(
                      (swimmer) => swimmer.id === device.bound_swimmer_id,
                    );
                    return (
                      <article className="swimmer-device-card" key={device.device_id}>
                        <div className="swimmer-device-heading">
                          <div>
                            <strong>{device.device_id}</strong>
                            <span>固件 {device.firmware_version}</span>
                          </div>
                          <span
                            className={`device-state ${
                              device.communication_loss_ms !== null &&
                              device.communication_loss_ms >= 10000
                                ? "danger"
                                : device.connected
                                  ? "online"
                                  : ""
                            }`}
                          >
                            {communicationStateLabel(device.communication_state)}
                          </span>
                        </div>
                        <dl className="swimmer-device-details">
                          <div>
                            <dt>连接</dt>
                            <dd>{device.connected ? "已连接" : "已断开"}</dd>
                          </div>
                          <div>
                            <dt>失联时长</dt>
                            <dd>{formatLoss(device.communication_loss_ms)}</dd>
                          </div>
                          <div>
                            <dt>恢复心跳</dt>
                            <dd>{device.recovery_heartbeat_count}/3</dd>
                          </div>
                          <div>
                            <dt>设备状态</dt>
                            <dd>
                              {device.local_alert_stage
                                ? localAlertLabel[device.local_alert_stage]
                                : "尚未上报"}
                            </dd>
                          </div>
                          <div>
                            <dt>电脑推定救生</dt>
                            <dd>{device.rescue_expected ? "已触发" : "未触发"}</dd>
                          </div>
                          <div>
                            <dt>设备确认救生</dt>
                            <dd>{device.rescue_confirmed ? "已锁存" : "未锁存"}</dd>
                          </div>
                          <div>
                            <dt>配对泳者</dt>
                            <dd>
                              {boundSwimmer?.display_name ??
                                device.bound_swimmer_id ??
                                "未配对"}
                            </dd>
                          </div>
                          <div>
                            <dt>救生功能</dt>
                            <dd>{device.rescue_enabled ? "允许" : "仅振动"}</dd>
                          </div>
                          <div>
                            <dt>模拟通信</dt>
                            <dd>
                              {device.simulation_paused
                                ? "已暂停"
                                : device.simulation_requested
                                  ? "等待暂停"
                                  : "正常"}
                            </dd>
                          </div>
                        </dl>
                        {device.last_error && (
                          <small className="hardware-error">{device.last_error}</small>
                        )}
                      </article>
                    );
                  })
                )}
              </div>
            </section>
          </div>
        </div>
      </section>

      {pairingOpen && (
        <div
          aria-labelledby="pairing-title"
          aria-modal="true"
          className="pairing-modal-backdrop"
          onMouseDown={(event) => {
            if (event.currentTarget === event.target && !savingPairings) {
              setPairingOpen(false);
            }
          }}
          role="dialog"
        >
          <section className="pairing-modal">
            <header className="pairing-modal-header">
              <div>
                <p className="eyebrow">一对一关联</p>
                <h2 id="pairing-title">泳者设备配对</h2>
                <p>
                  将任一侧卡片拖到另一侧完成配对；手机上可先点泳者，再点设备。
                </p>
              </div>
              <button
                aria-label="关闭配对窗口"
                className="modal-close"
                disabled={savingPairings}
                onClick={() => setPairingOpen(false)}
                type="button"
              >
                ×
              </button>
            </header>

            <div className="pairing-board">
              <section className="pairing-column">
                <div className="pairing-column-heading">
                  <strong>在线泳者</strong>
                  <span>{onlineSwimmers.length} 人</span>
                </div>
                <div className="pairing-card-list">
                  {onlineSwimmers.length === 0 ? (
                    <p className="pairing-empty">当前没有在线泳者</p>
                  ) : (
                    onlineSwimmers.map((swimmer) => {
                      const pairedDeviceId = Object.entries(
                        draftBindings,
                      ).find(([, swimmerId]) => swimmerId === swimmer.id)?.[0];
                      const pairedDevice = swimmerDevices.find(
                        (device) => device.device_id === pairedDeviceId,
                      );
                      return (
                        <article
                          className={`pairing-card swimmer ${
                            pairedDevice ? "paired" : ""
                          } ${
                            selectedPairSwimmer === swimmer.id ? "selected" : ""
                          }`}
                          draggable
                          key={swimmer.id}
                          onDragOver={(event) => event.preventDefault()}
                          onDragStart={(event) =>
                            startPairDrag(event, "swimmer", swimmer.id)
                          }
                          onDrop={(event) => dropOnSwimmer(event, swimmer.id)}
                        >
                          <div className="pairing-card-main">
                            <span className={`avatar ${swimmer.status}`}>
                              {swimmer.display_name.slice(-2)}
                            </span>
                            <span>
                              <strong>{swimmer.display_name}</strong>
                              <small>
                                {swimmer.source} · 泳道 {swimmer.lane ?? "—"}
                              </small>
                            </span>
                          </div>
                          <div className="pairing-card-status">
                            <span>
                              {pairedDevice
                                ? `已配对 ${pairedDevice.device_id}${
                                    pairedDevice.connected ? "" : "（已断开）"
                                  }`
                                : "等待设备"}
                            </span>
                            <button
                              className="button compact ghost"
                              onClick={() =>
                                setSelectedPairSwimmer((current) =>
                                  current === swimmer.id ? null : swimmer.id,
                                )
                              }
                              type="button"
                            >
                              {selectedPairSwimmer === swimmer.id
                                ? "已选择"
                                : "选择此泳者"}
                            </button>
                          </div>
                        </article>
                      );
                    })
                  )}
                </div>
              </section>

              <div className="pairing-divider" aria-hidden="true">
                ⇄
              </div>

              <section className="pairing-column">
                <div className="pairing-column-heading">
                  <strong>已连接泳者设备</strong>
                  <span>{connectedSwimmerDevices.length} 台</span>
                </div>
                <div className="pairing-card-list">
                  {connectedSwimmerDevices.length === 0 ? (
                    <p className="pairing-empty">当前没有已连接的泳者设备</p>
                  ) : (
                    connectedSwimmerDevices.map((device) => {
                      const swimmerId = draftBindings[device.device_id] ?? null;
                      const pairedSwimmer = snapshot.swimmers.find(
                        (swimmer) => swimmer.id === swimmerId,
                      );
                      const canReset =
                        device.communication_state === "normal" &&
                        device.recovery_heartbeat_count >= 3 &&
                        device.rescue_confirmed;
                      return (
                        <article
                          className={`pairing-card device ${
                            swimmerId ? "paired" : ""
                          }`}
                          draggable
                          key={device.device_id}
                          onDragOver={(event) => event.preventDefault()}
                          onDragStart={(event) =>
                            startPairDrag(event, "device", device.device_id)
                          }
                          onDrop={(event) =>
                            dropOnDevice(event, device.device_id)
                          }
                        >
                          <div className="pairing-card-main">
                            <span className="device-pair-icon">S</span>
                            <span>
                              <strong>{device.device_id}</strong>
                              <small>
                                {device.firmware_version} · 电量{" "}
                                {device.battery_percent ?? "—"}%
                              </small>
                            </span>
                          </div>
                          <div className="pairing-card-status">
                            <span>
                              {swimmerId
                                ? `已配对 ${
                                    pairedSwimmer?.display_name ?? swimmerId
                                  }`
                                : "等待泳者"}
                            </span>
                            {selectedPairSwimmer && (
                              <button
                                className="button compact primary"
                                onClick={() =>
                                  pairDraft(
                                    device.device_id,
                                    selectedPairSwimmer,
                                  )
                                }
                                type="button"
                              >
                                与所选泳者配对
                              </button>
                            )}
                          </div>

                          {swimmerId && (
                            <div className="pairing-settings">
                              <label className="rescue-toggle">
                                <input
                                  checked={
                                    draftRescueEnabled[device.device_id] ?? true
                                  }
                                  onChange={(event) =>
                                    setDraftRescueEnabled((current) => ({
                                      ...current,
                                      [device.device_id]: event.target.checked,
                                    }))
                                  }
                                  type="checkbox"
                                />
                                <span aria-hidden="true" />
                                <strong>允许进入救生状态</strong>
                                <small>
                                  关闭后疑似溺水只振动，不触发救生锁存
                                </small>
                              </label>
                              <div className="pairing-actions">
                                <button
                                  className="button compact danger-button"
                                  disabled={
                                    device.pending_command !== null ||
                                    device.bound_swimmer_id !== swimmerId
                                  }
                                  onClick={() => triggerRescue(device.device_id)}
                                  title={
                                    device.bound_swimmer_id === swimmerId
                                      ? "立即发送救生测试指令；即使自动救生已关闭也可手动触发"
                                      : "请先保存配对，再触发救生测试"
                                  }
                                  type="button"
                                >
                                  一键触发救生
                                </button>
                                <button
                                  className="button compact warning"
                                  disabled={
                                    !canReset || device.pending_command !== null
                                  }
                                  onClick={() => resetRescue(device.device_id)}
                                  title={
                                    canReset
                                      ? "发送可靠救生复位命令"
                                      : "连续三次恢复心跳并确认救生锁存后才能复位"
                                  }
                                  type="button"
                                >
                                  复位救生锁存
                                </button>
                                <button
                                  className="button compact ghost danger-text"
                                  onClick={() => unpairDraft(device.device_id)}
                                  type="button"
                                >
                                  解除配对
                                </button>
                              </div>
                            </div>
                          )}
                        </article>
                      );
                    })
                  )}
                </div>
              </section>
            </div>

            <footer className="pairing-modal-footer">
              <span>
                已配对{" "}
                {
                  connectedSwimmerDevices.filter(
                    (device) => draftBindings[device.device_id],
                  ).length
                }{" "}
                组；多余泳者或设备可保持未配对。
              </span>
              <div>
                <button
                  className="button ghost"
                  disabled={savingPairings}
                  onClick={() => setPairingOpen(false)}
                  type="button"
                >
                  取消
                </button>
                <button
                  className="button primary"
                  disabled={!adminAllowed || savingPairings}
                  onClick={savePairings}
                  type="button"
                >
                  {savingPairings ? "正在保存…" : "保存配对"}
                </button>
              </div>
            </footer>
          </section>
        </div>
      )}
    </main>
  );
}
