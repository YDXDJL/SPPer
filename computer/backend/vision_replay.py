"""Replay the checked-in vision samples without running inference again.

The service deliberately has no dependency on FastAPI, ``StateStore`` or the
device hub.  Integration code injects asynchronous callbacks and decides how a
decoded frame, collision transition, or cleanup event changes the application.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import cv2


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPLAY_DIRECTORY = Path("demo_assets/vision_replays")
OUTPUT_DIRECTORY = REPLAY_DIRECTORY / "tracks"
SOURCE_DIRECTORY = REPLAY_DIRECTORY / "videos"

ReplayStatus = Literal["idle", "ready", "playing", "paused", "ended", "error"]
ReplayAlertStage = Literal["normal", "suspected_drowning", "high_risk"]
ReplayPauseReason = Literal["manual", "cue"]
CleanupReason = Literal["stop", "switch", "restart", "eof", "error"]
CollisionReason = Literal["started", "ended", "feedback_changed", "cleanup"]
CollisionPair = tuple[int, int]


@dataclass(frozen=True, slots=True)
class ReplaySample:
    sample_id: str
    label: str
    source_relative: Path
    tracks_relative: Path
    summary_relative: Path

    def describe(self, root: Path) -> dict[str, Any]:
        return {
            "id": self.sample_id,
            "label": self.label,
            "source": str(root / self.source_relative),
            "tracks": str(root / self.tracks_relative),
            "summary": str(root / self.summary_relative),
        }


def _sample(sample_id: str, label: str) -> ReplaySample:
    return ReplaySample(
        sample_id=sample_id,
        label=label,
        source_relative=SOURCE_DIRECTORY / f"{sample_id}.MP4",
        tracks_relative=OUTPUT_DIRECTORY / f"{sample_id}_configured_tracks.jsonl",
        summary_relative=OUTPUT_DIRECTORY / f"{sample_id}_configured_summary.json",
    )


REPLAY_SAMPLES: dict[str, ReplaySample] = {
    "oneline": _sample("oneline", "单人泳道"),
    "twolines": _sample("twolines", "双人正常通行"),
    "crashing": _sample("crashing", "双人碰撞预警"),
    "drowning": _sample("drowning", "单人溺水检测"),
}

REPLAY_CUES: dict[tuple[str, int], dict[str, Any]] = {
    ("drowning", 334): {
        "id": "drowning-suspected",
        "kind": "drowning",
        "phase": "suspected_drowning",
        "frame_number": 335,
        "title": "检测到疑似溺水",
        "message": (
            "连续 10 秒未检测到有效信号，判断疑似溺水；泳者端开始高频振动，"
            "救生员手环开始预警，救生装置未触发。"
        ),
        "severity": "danger",
        "target_track_ids": [1],
    },
    ("drowning", 489): {
        "id": "drowning-high-risk",
        "kind": "drowning",
        "phase": "high_risk",
        "frame_number": 490,
        "title": "检测到高风险",
        "message": (
            "连续 20 秒未检测到有效信号，判断为高风险；已达到救生触发条件，"
            "但演示未下发救生信号。"
        ),
        "severity": "danger",
        "target_track_ids": [1],
    },
    ("crashing", 124): {
        "id": "collision-start",
        "kind": "collision",
        "phase": "started",
        "frame_number": 125,
        "title": "碰撞预警开始",
        "message": "检测到两名泳者存在碰撞风险，黄灯和泳者端振动保持到预警解除。",
        "severity": "warning",
        "target_track_ids": [1, 2],
    },
    ("crashing", 165): {
        "id": "collision-end",
        "kind": "collision",
        "phase": "ended",
        "frame_number": 166,
        "title": "碰撞预警结束",
        "message": "碰撞风险已经解除，黄灯和泳者端振动已停止。",
        "severity": "safe",
        "target_track_ids": [1, 2],
    },
}


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    """A decoded source frame plus metadata accepted by ``VisionFrame``."""

    sample_id: str
    frame_index: int
    timestamp_s: float
    metadata: dict[str, Any]
    jpeg: bytes
    collision_pairs: tuple[CollisionPair, ...]
    collision_warnings: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class CollisionChange:
    sample_id: str
    pair: CollisionPair
    active: bool
    feedback_enabled: bool
    frame_index: int | None
    timestamp_s: float | None
    reason: CollisionReason


@dataclass(frozen=True, slots=True)
class ReplayCleanup:
    sample_id: str
    reason: CleanupReason
    last_frame_index: int | None
    last_timestamp_s: float
    person_ids: tuple[int, ...]
    error: str | None = None


FrameCallback = Callable[[ReplayFrame], Awaitable[None]]
CollisionCallback = Callable[[CollisionChange], Awaitable[None]]
CleanupCallback = Callable[[ReplayCleanup], Awaitable[None]]
StateCallback = Callable[[dict[str, Any]], Awaitable[None]]


async def _ignore_frame(_frame: ReplayFrame) -> None:
    return None


async def _ignore_collision(_change: CollisionChange) -> None:
    return None


async def _ignore_cleanup(_cleanup: ReplayCleanup) -> None:
    return None


async def _ignore_state(_state: dict[str, Any]) -> None:
    return None


@dataclass(frozen=True, slots=True)
class _LogRecord:
    frame_index: int
    timestamp_s: float
    tracks: tuple[dict[str, Any], ...]
    collision_pairs: tuple[CollisionPair, ...]
    collision_warnings: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _LoadedSample:
    definition: ReplaySample
    source_path: Path
    width: int
    height: int
    fps: float
    frame_count: int
    duration_s: float
    person_ids: tuple[int, ...]
    records: tuple[_LogRecord, ...]


class VisionReplayService:
    """Monotonic-clock replay controller for the fixed vision samples.

    ``on_frame`` receives an original-video JPEG and metadata compatible with
    the existing ``VisionFrame`` model. ``on_collision`` receives pair state
    transitions based solely on each JSONL record's ``collision_warnings``.
    ``on_cleanup`` is always called when an active/selected run is stopped,
    switched, restarted, reaches EOF, or fails.
    """

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        on_frame: FrameCallback | None = None,
        on_collision: CollisionCallback | None = None,
        on_cleanup: CleanupCallback | None = None,
        on_state_change: StateCallback | None = None,
        jpeg_quality: int = 82,
        playback_rate: float = 1.0,
    ) -> None:
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100")
        if playback_rate <= 0:
            raise ValueError("playback_rate must be positive")
        self._root = (project_root or PROJECT_ROOT).resolve()
        self._on_frame = on_frame or _ignore_frame
        self._on_collision = on_collision or _ignore_collision
        self._on_cleanup = on_cleanup or _ignore_cleanup
        self._on_state_change = on_state_change or _ignore_state
        self._jpeg_quality = jpeg_quality
        self._playback_rate = playback_rate

        self._operation_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._control_event = asyncio.Event()
        self._loaded: _LoadedSample | None = None
        self._task: asyncio.Task[None] | None = None
        self._status: ReplayStatus = "idle"
        self._next_frame_index = 0
        self._last_frame_index: int | None = None
        self._last_timestamp_s = 0.0
        self._active_pairs: set[CollisionPair] = set()
        self._feedback_enabled = True
        self._error: str | None = None
        self._clock_origin: float | None = None
        self._pause_started: float | None = None
        self._run_sequence = 0
        self._alert_stage: ReplayAlertStage = "normal"
        self._pause_reason: ReplayPauseReason | None = None
        self._cue: dict[str, Any] | None = None

    @property
    def sample_ids(self) -> tuple[str, ...]:
        return tuple(REPLAY_SAMPLES)

    async def snapshot(self) -> dict[str, Any]:
        async with self._state_lock:
            return self._snapshot_locked()

    async def samples_payload(self) -> dict[str, Any]:
        """Return the public, stable catalog for the three replay samples."""

        samples = await asyncio.to_thread(self._load_catalog)
        return {"samples": samples}

    async def select(self, sample_id: str) -> dict[str, Any]:
        """Select and validate a sample, stopping a different current sample."""

        definition = REPLAY_SAMPLES.get(sample_id)
        if definition is None:
            raise ValueError(
                f"unknown replay sample {sample_id!r}; expected one of "
                + ", ".join(REPLAY_SAMPLES)
            )
        async with self._operation_lock:
            async with self._state_lock:
                current_id = (
                    self._loaded.definition.sample_id if self._loaded is not None else None
                )
            if current_id == sample_id:
                return await self.snapshot()
            if current_id is not None:
                await self._halt("switch", reset_position=True)
            try:
                loaded = await asyncio.to_thread(self._load_sample, definition)
            except Exception as exc:
                message = str(exc)
                async with self._state_lock:
                    self._loaded = None
                    self._status = "error"
                    self._error = message
                await self._safe_cleanup(
                    ReplayCleanup(sample_id, "error", None, 0.0, (), message)
                )
                await self._notify_state()
                raise
            async with self._state_lock:
                self._loaded = loaded
                self._status = "ready"
                self._next_frame_index = 0
                self._last_frame_index = None
                self._last_timestamp_s = 0.0
                self._active_pairs.clear()
                self._clock_origin = None
                self._pause_started = None
                self._error = None
                self._alert_stage = "normal"
                self._pause_reason = None
                self._cue = None
                self._run_sequence += 1
            await self._emit_preview(loaded)
            await self._notify_state()
            return await self.snapshot()

    async def play(self) -> dict[str, Any]:
        """Start or resume playback; playing after EOF starts from frame zero."""

        async with self._operation_lock:
            async with self._state_lock:
                if self._loaded is None:
                    raise RuntimeError("select a replay sample before play")
                now = time.monotonic()
                if self._status == "playing":
                    return self._snapshot_locked()
                if self._status == "ended" or self._next_frame_index >= self._loaded.frame_count:
                    self._reset_position_locked()
                if self._status == "paused" and self._pause_started is not None:
                    paused_for = now - self._pause_started
                    if self._clock_origin is not None:
                        self._clock_origin += paused_for
                else:
                    self._clock_origin = (
                        now - self._last_timestamp_s / self._playback_rate
                    )
                    self._run_sequence += 1
                self._pause_started = None
                self._pause_reason = None
                self._cue = None
                self._status = "playing"
                self._error = None
                if self._task is None or self._task.done():
                    self._task = asyncio.create_task(
                        self._run(), name="vision-sample-replay"
                    )
                self._control_event.set()
            await self._notify_state()
            return await self.snapshot()

    async def pause(self) -> dict[str, Any]:
        async with self._operation_lock:
            async with self._state_lock:
                if self._status == "playing":
                    self._status = "paused"
                    self._pause_started = time.monotonic()
                    self._pause_reason = "manual"
                    self._cue = None
                    self._control_event.set()
            await self._notify_state()
            return await self.snapshot()

    async def restart(self, *, autoplay: bool = True) -> dict[str, Any]:
        """Clear the current run, seek to frame zero, and optionally play."""

        async with self._operation_lock:
            if self._loaded is None:
                raise RuntimeError("select a replay sample before restart")
            await self._halt("restart", reset_position=True)
        if autoplay:
            return await self.play()
        return await self.snapshot()

    async def stop(self) -> dict[str, Any]:
        """Stop playback, clear replay output, and unload the selected sample."""

        async with self._operation_lock:
            if self._loaded is not None:
                await self._halt("stop", reset_position=True, unload=True)
            else:
                async with self._state_lock:
                    self._status = "idle"
                    self._error = None
                await self._notify_state()
            return await self.snapshot()

    async def set_feedback(self, enabled: bool) -> dict[str, Any]:
        """Enable/disable physical feedback without hiding collision state.

        Active pairs are re-emitted with ``reason='feedback_changed'`` so the
        integrator can change device behavior while retaining the visual alert.
        """

        changes: list[CollisionChange] = []
        async with self._operation_lock:
            async with self._state_lock:
                if self._feedback_enabled == enabled:
                    return self._snapshot_locked()
                self._feedback_enabled = enabled
                if self._loaded is not None:
                    changes = [
                        CollisionChange(
                            self._loaded.definition.sample_id,
                            pair,
                            True,
                            enabled,
                            self._last_frame_index,
                            self._last_timestamp_s,
                            "feedback_changed",
                        )
                        for pair in sorted(self._active_pairs)
                    ]
            for change in changes:
                await self._on_collision(change)
            await self._notify_state()
            return await self.snapshot()

    async def clear_alert(self) -> dict[str, Any]:
        """Clear a replay-only drowning alert without moving the video."""

        async with self._operation_lock:
            async with self._state_lock:
                self._alert_stage = "normal"
                if self._cue is not None and self._cue.get("kind") == "drowning":
                    self._cue = None
                if self._status == "paused" and self._pause_reason == "cue":
                    self._pause_reason = "manual"
            await self._notify_state()
            return await self.snapshot()

    async def _run(self) -> None:
        capture: cv2.VideoCapture | None = None
        try:
            async with self._state_lock:
                loaded = self._loaded
                start_frame = self._next_frame_index
            if loaded is None:
                return
            capture = await asyncio.to_thread(cv2.VideoCapture, str(loaded.source_path))
            if not capture.isOpened():
                raise RuntimeError(f"cannot open replay video: {loaded.source_path}")
            if start_frame:
                capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

            while True:
                async with self._state_lock:
                    if self._loaded is not loaded:
                        return
                    status = self._status
                    next_index = self._next_frame_index
                    origin = self._clock_origin
                    if next_index >= loaded.frame_count:
                        break
                    record = loaded.records[next_index]

                if status == "paused":
                    self._control_event.clear()
                    await self._control_event.wait()
                    continue
                if status != "playing" or origin is None:
                    return

                delay = (
                    origin
                    + record.timestamp_s / self._playback_rate
                    - time.monotonic()
                )
                if delay > 0:
                    self._control_event.clear()
                    try:
                        await asyncio.wait_for(self._control_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    else:
                        continue

                ok, image = await asyncio.to_thread(capture.read)
                if not ok:
                    raise RuntimeError(
                        f"cannot decode {loaded.definition.sample_id} frame {next_index}"
                    )
                encoded, buffer = await asyncio.to_thread(
                    cv2.imencode,
                    ".jpg",
                    image,
                    [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality],
                )
                if not encoded:
                    raise RuntimeError(
                        f"cannot encode {loaded.definition.sample_id} frame {next_index}"
                    )
                await self._emit_record(loaded, record, buffer.tobytes())

            await self._finish_naturally(loaded)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._finish_with_error(exc)
        finally:
            if capture is not None:
                await asyncio.to_thread(capture.release)
            current = asyncio.current_task()
            async with self._state_lock:
                if self._task is current:
                    self._task = None

    async def _emit_record(
        self, loaded: _LoadedSample, record: _LogRecord, jpeg: bytes
    ) -> None:
        async with self._state_lock:
            run_sequence = self._run_sequence
            feedback_enabled = self._feedback_enabled
            previous_pairs = set(self._active_pairs)
        current_pairs = set(record.collision_pairs)
        metadata = {
            "type": "vision_frame",
            "schema_version": "1.0",
            "frame_id": (
                f"replay-{loaded.definition.sample_id}-{run_sequence}-"
                f"{record.frame_index:06d}"
            ),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "image_width": loaded.width,
            "image_height": loaded.height,
            "tracks": list(record.tracks),
            "collision_warnings": list(record.collision_warnings),
        }
        await self._on_frame(
            ReplayFrame(
                loaded.definition.sample_id,
                record.frame_index,
                record.timestamp_s,
                metadata,
                jpeg,
                record.collision_pairs,
                record.collision_warnings,
            )
        )
        for pair in sorted(previous_pairs - current_pairs):
            await self._on_collision(
                CollisionChange(
                    loaded.definition.sample_id,
                    pair,
                    False,
                    feedback_enabled,
                    record.frame_index,
                    record.timestamp_s,
                    "ended",
                )
            )
        for pair in sorted(current_pairs - previous_pairs):
            await self._on_collision(
                CollisionChange(
                    loaded.definition.sample_id,
                    pair,
                    True,
                    feedback_enabled,
                    record.frame_index,
                    record.timestamp_s,
                    "started",
                )
            )
        async with self._state_lock:
            if self._loaded is not loaded:
                return
            self._active_pairs = current_pairs
            self._last_frame_index = record.frame_index
            self._last_timestamp_s = record.timestamp_s
            self._next_frame_index = record.frame_index + 1
            cue = REPLAY_CUES.get(
                (loaded.definition.sample_id, record.frame_index)
            )
            if cue is not None:
                self._cue = {**cue, "frame_index": record.frame_index}
                phase = cue["phase"]
                if phase in {"suspected_drowning", "high_risk"}:
                    self._alert_stage = phase
                self._status = "paused"
                self._pause_reason = "cue"
                self._pause_started = time.monotonic()
        await self._notify_state()

    async def _emit_preview(self, loaded: _LoadedSample) -> None:
        """Decode and publish frame zero while retaining the ready state."""

        capture = await asyncio.to_thread(cv2.VideoCapture, str(loaded.source_path))
        if not capture.isOpened():
            raise RuntimeError(f"cannot open replay video: {loaded.source_path}")
        try:
            ok, image = await asyncio.to_thread(capture.read)
        finally:
            await asyncio.to_thread(capture.release)
        if not ok:
            raise RuntimeError(
                f"cannot decode {loaded.definition.sample_id} preview frame"
            )
        encoded, buffer = await asyncio.to_thread(
            cv2.imencode,
            ".jpg",
            image,
            [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality],
        )
        if not encoded:
            raise RuntimeError(
                f"cannot encode {loaded.definition.sample_id} preview frame"
            )
        record = loaded.records[0]
        metadata = self._frame_metadata(loaded, record)
        await self._on_frame(
            ReplayFrame(
                loaded.definition.sample_id,
                0,
                0.0,
                metadata,
                buffer.tobytes(),
                record.collision_pairs,
                record.collision_warnings,
            )
        )
        async with self._state_lock:
            if self._loaded is loaded:
                self._last_frame_index = 0
                self._last_timestamp_s = 0.0
                self._next_frame_index = 1

    def _frame_metadata(
        self, loaded: _LoadedSample, record: _LogRecord
    ) -> dict[str, Any]:
        return {
            "type": "vision_frame",
            "schema_version": "1.0",
            "frame_id": (
                f"replay-{loaded.definition.sample_id}-{self._run_sequence}-"
                f"{record.frame_index:06d}"
            ),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "image_width": loaded.width,
            "image_height": loaded.height,
            "tracks": list(record.tracks),
            "collision_warnings": list(record.collision_warnings),
        }

    async def _finish_naturally(self, loaded: _LoadedSample) -> None:
        async with self._state_lock:
            preserve_drowning = (
                loaded.definition.sample_id == "drowning"
                and self._alert_stage != "normal"
            )
        if not preserve_drowning:
            cleanup = await self._build_cleanup("eof")
            await self._clear_collision_pairs("eof")
            if cleanup is not None:
                await self._safe_cleanup(cleanup)
        async with self._state_lock:
            if self._loaded is loaded:
                self._status = "ended"
                self._clock_origin = None
                self._pause_started = None
                self._pause_reason = None
        await self._notify_state()

    async def _finish_with_error(self, exc: Exception) -> None:
        message = str(exc)
        cleanup = await self._build_cleanup("error", message)
        await self._clear_collision_pairs("error")
        if cleanup is not None:
            await self._safe_cleanup(cleanup)
        async with self._state_lock:
            self._status = "error"
            self._error = message
            self._clock_origin = None
            self._pause_started = None
            self._pause_reason = None
            self._cue = None
            self._alert_stage = "normal"
        LOGGER.exception("vision sample replay failed", exc_info=exc)
        await self._notify_state()

    async def _halt(
        self,
        reason: CleanupReason,
        *,
        reset_position: bool,
        unload: bool = False,
    ) -> None:
        async with self._state_lock:
            task = self._task
            self._control_event.set()
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        cleanup = await self._build_cleanup(reason)
        await self._clear_collision_pairs(reason)
        if cleanup is not None:
            await self._safe_cleanup(cleanup)
        async with self._state_lock:
            self._task = None
            self._clock_origin = None
            self._pause_started = None
            self._error = None
            if reset_position:
                self._reset_position_locked()
            if unload:
                self._loaded = None
            self._status = "ready" if self._loaded is not None else "idle"
        await self._notify_state()

    async def _clear_collision_pairs(self, reason: CleanupReason) -> None:
        async with self._state_lock:
            loaded = self._loaded
            pairs = tuple(sorted(self._active_pairs))
            feedback = self._feedback_enabled
            frame_index = self._last_frame_index
            timestamp_s = self._last_timestamp_s
            self._active_pairs.clear()
        if loaded is None:
            return
        for pair in pairs:
            try:
                await self._on_collision(
                    CollisionChange(
                        loaded.definition.sample_id,
                        pair,
                        False,
                        feedback,
                        frame_index,
                        timestamp_s,
                        "cleanup",
                    )
                )
            except Exception:
                LOGGER.exception(
                    "collision cleanup callback failed for %s (%s)", pair, reason
                )

    async def _build_cleanup(
        self, reason: CleanupReason, error: str | None = None
    ) -> ReplayCleanup | None:
        async with self._state_lock:
            if self._loaded is None:
                return None
            return ReplayCleanup(
                self._loaded.definition.sample_id,
                reason,
                self._last_frame_index,
                self._last_timestamp_s,
                self._loaded.person_ids,
                error,
            )

    async def _safe_cleanup(self, cleanup: ReplayCleanup) -> None:
        try:
            await self._on_cleanup(cleanup)
        except Exception:
            LOGGER.exception(
                "vision replay cleanup callback failed for %s", cleanup.sample_id
            )

    async def _notify_state(self) -> None:
        state = await self.snapshot()
        try:
            await self._on_state_change(state)
        except Exception:
            LOGGER.exception("vision replay state callback failed")

    def _reset_position_locked(self) -> None:
        self._next_frame_index = 0
        self._last_frame_index = None
        self._last_timestamp_s = 0.0
        self._active_pairs.clear()
        self._clock_origin = None
        self._pause_started = None
        self._pause_reason = None
        self._cue = None
        self._alert_stage = "normal"

    def _snapshot_locked(self) -> dict[str, Any]:
        loaded = self._loaded
        duration = loaded.duration_s if loaded is not None else 0.0
        progress = (
            min(1.0, self._next_frame_index / loaded.frame_count)
            if loaded is not None and loaded.frame_count
            else 0.0
        )
        return {
            "sample_id": loaded.definition.sample_id if loaded else None,
            "state": self._status,
            "frame_index": self._last_frame_index or 0,
            "position_s": self._last_timestamp_s,
            "duration_s": duration,
            "device_feedback_enabled": self._feedback_enabled,
            "alert_stage": self._alert_stage,
            "pause_reason": self._pause_reason,
            "cue": dict(self._cue) if self._cue is not None else None,
            "error": self._error,
            # Additional diagnostic fields are intentionally additive.
            "status": self._status,
            "selected_sample": loaded.definition.sample_id if loaded else None,
            "feedback_enabled": self._feedback_enabled,
            "next_frame_index": self._next_frame_index,
            "timestamp_s": self._last_timestamp_s,
            "fps": loaded.fps if loaded else None,
            "frame_count": loaded.frame_count if loaded else 0,
            "image_width": loaded.width if loaded else None,
            "image_height": loaded.height if loaded else None,
            "progress": progress,
            "person_ids": list(loaded.person_ids) if loaded else [],
            "active_collision_pairs": [list(pair) for pair in sorted(self._active_pairs)],
        }

    def _load_catalog(self) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for definition in REPLAY_SAMPLES.values():
            summary_path = (self._root / definition.summary_relative).resolve()
            if not summary_path.is_file():
                raise FileNotFoundError(
                    f"vision replay asset not found: {summary_path}"
                )
            summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
            frame_count = int(summary.get("frames_processed", 0))
            fps = float(summary.get("source_fps", 0.0))
            if summary.get("completed") is not True or frame_count <= 0 or fps <= 0:
                raise ValueError(f"invalid replay summary: {summary_path}")
            collision = summary.get("collision_warning") or {}
            people = summary.get("unique_person_ids") or []
            catalog.append(
                {
                    "id": definition.sample_id,
                    "label": definition.label,
                    "duration_s": (frame_count - 1) / fps,
                    "frame_count": frame_count,
                    "fps": fps,
                    "people_count": len(people),
                    "collision_episodes": int(collision.get("episodes", 0)),
                }
            )
        return catalog

    def _load_sample(self, definition: ReplaySample) -> _LoadedSample:
        source_path = (self._root / definition.source_relative).resolve()
        tracks_path = (self._root / definition.tracks_relative).resolve()
        summary_path = (self._root / definition.summary_relative).resolve()
        for path in (source_path, tracks_path, summary_path):
            if not path.is_file():
                raise FileNotFoundError(f"vision replay asset not found: {path}")

        summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
        if summary.get("completed") is not True:
            raise ValueError(f"vision replay summary is incomplete: {summary_path}")
        fps = float(summary.get("source_fps", 0.0))
        expected_frames = int(summary.get("frames_processed", 0))
        if fps <= 0 or expected_frames <= 0:
            raise ValueError(f"vision replay summary has invalid timing: {summary_path}")

        capture = cv2.VideoCapture(str(source_path))
        if not capture.isOpened():
            raise ValueError(f"cannot open vision replay video: {source_path}")
        try:
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            video_fps = float(capture.get(cv2.CAP_PROP_FPS))
            video_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        finally:
            capture.release()
        if width <= 0 or height <= 0:
            raise ValueError(f"vision replay video has invalid dimensions: {source_path}")
        if video_frames != expected_frames or abs(video_fps - fps) > 0.01:
            raise ValueError(
                "vision replay video and summary disagree: "
                f"video={video_frames}@{video_fps}, summary={expected_frames}@{fps}"
            )

        records: list[_LogRecord] = []
        person_ids: set[int] = set()
        with tracks_path.open("r", encoding="utf-8-sig") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    raise ValueError(f"blank JSONL record at {tracks_path}:{line_number}")
                raw = json.loads(line)
                frame_index = int(raw["frame_index"])
                if frame_index != len(records):
                    raise ValueError(
                        f"non-contiguous frame_index at {tracks_path}:{line_number}"
                    )
                timestamp_s = float(raw["timestamp_s"])
                expected_timestamp = frame_index / fps
                if abs(timestamp_s - expected_timestamp) > 0.002:
                    raise ValueError(
                        f"timestamp/frame mismatch at {tracks_path}:{line_number}"
                    )
                tracks: list[dict[str, Any]] = []
                for track in raw.get("tracks", []):
                    person_id = int(track["person_id"])
                    bbox = [float(value) for value in track["bbox_xyxy"]]
                    center = [float(value) for value in track["center_xy"]]
                    confidence = float(track.get("confidence", 1.0))
                    if len(bbox) != 4 or len(center) != 2:
                        raise ValueError(
                            f"invalid track geometry at {tracks_path}:{line_number}"
                        )
                    person_ids.add(person_id)
                    tracks.append(
                        {
                            "track_id": person_id,
                            "class_id": 0,
                            "class_name": "person",
                            "confidence": max(0.0, min(1.0, confidence)),
                            "bbox_xyxy": bbox,
                            "center_xy": center,
                            "center_normalized": [
                                max(0.0, min(1.0, center[0] / width)),
                                max(0.0, min(1.0, center[1] / height)),
                            ],
                        }
                    )
                collision_pairs: set[CollisionPair] = set()
                collision_warnings: list[dict[str, Any]] = []
                for warning in raw.get("collision_warnings", []):
                    ids = tuple(sorted(int(value) for value in warning["person_ids"]))
                    if len(ids) != 2 or ids[0] == ids[1]:
                        raise ValueError(
                            f"invalid collision pair at {tracks_path}:{line_number}"
                        )
                    collision_pairs.add((ids[0], ids[1]))
                    collision_warnings.append(
                        {
                            "track_ids": [ids[0], ids[1]],
                            "time_to_collision_s": warning.get(
                                "time_to_collision_s"
                            ),
                            "minimum_distance_px": warning.get(
                                "minimum_distance_px"
                            ),
                            "warning_distance_px": warning.get(
                                "warning_distance_px"
                            ),
                        }
                    )
                records.append(
                    _LogRecord(
                        frame_index,
                        timestamp_s,
                        tuple(tracks),
                        tuple(sorted(collision_pairs)),
                        tuple(collision_warnings),
                    )
                )
        if len(records) != expected_frames:
            raise ValueError(
                f"vision replay JSONL has {len(records)} frames, expected {expected_frames}"
            )
        return _LoadedSample(
            definition,
            source_path,
            width,
            height,
            fps,
            expected_frames,
            (expected_frames - 1) / fps,
            tuple(sorted(person_ids)),
            tuple(records),
        )
