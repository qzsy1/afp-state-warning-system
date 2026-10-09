"""Session-scoped acquisition mirrors for visitor-computer edge helpers."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import deque
from copy import deepcopy
from typing import Any

from real_acquisition import UnifiedFrame


class RemoteAcquisitionMirror:
    """Expose helper sample batches through the AcquisitionManager read API."""

    def __init__(self, *, max_rows: int = 50_000) -> None:
        self.max_rows = max(24, int(max_rows))
        self._lock = threading.RLock()
        self._rows: deque[dict[str, Any]] = deque(maxlen=self.max_rows)
        self._timestamps: deque[float] = deque(maxlen=self.max_rows)
        self._frames: deque[dict[str, Any]] = deque(maxlen=self.max_rows)
        self._capture_uuid = ""
        self._expected_sequence = 0
        self._expected_frame_sequence = 0
        self._accepted_batch_digests: dict[int, str] = {}
        self._accepted_batch_order: deque[int] = deque()
        self._status: dict[str, Any] = {
            "running": False,
            "sensors": [],
            "interfaces": [],
            "config": {},
        }
        self._last_batch_at: float | None = None
        self._total_received_rows = 0
        self._stream_version = 0
        self._latest_sample_at: float | None = None
        self._helper_batch_created_at: float | None = None
        self._helper_queue_depth = 0
        self._helper_ack_rtt_ms: float | None = None
        self._helper_route_type = "unknown"
        self._local_stopped_at: float | None = None
        self._flush_state = "running"
        self._helper_generated_rows = 0
        self._expected_channels: tuple[str, ...] = ()
        self._expected_channel_units: dict[str, str] = {}

    def configure_contract(
        self,
        channels: list[str] | tuple[str, ...],
        units: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        normalized = tuple(dict.fromkeys(str(name) for name in channels if str(name)))
        if not normalized:
            return {"ok": False, "error": "frame_channel_contract_empty"}
        normalized_units = {
            name: str((units or {}).get(name) or "") for name in normalized
        }
        with self._lock:
            self._expected_channels = normalized
            self._expected_channel_units = normalized_units
        return {"ok": True, "channels": list(normalized)}

    def _reset_locked(self, capture_uuid: str) -> None:
        self._rows.clear()
        self._timestamps.clear()
        self._frames.clear()
        self._capture_uuid = capture_uuid
        self._expected_sequence = 0
        self._expected_frame_sequence = 0
        self._accepted_batch_digests.clear()
        self._accepted_batch_order.clear()
        self._status = {
            "running": False,
            "sensors": [],
            "interfaces": [],
            "config": {},
        }
        self._last_batch_at = None
        self._total_received_rows = 0
        self._latest_sample_at = None
        self._helper_batch_created_at = None
        self._helper_queue_depth = 0
        self._helper_ack_rtt_ms = None
        self._helper_route_type = "unknown"
        self._local_stopped_at = None
        self._flush_state = "running"
        self._helper_generated_rows = 0

    def observe_helper_status(self, status: dict[str, Any]) -> bool:
        """Apply control-plane stop facts before slower sample batches drain."""

        capture_uuid = str((status or {}).get("capture_uuid") or "").strip()
        with self._lock:
            if not capture_uuid or capture_uuid != self._capture_uuid:
                return False
            stopped_at = status.get("local_stopped_at")
            flush_state = str(status.get("flush_state") or "")
            if stopped_at is None and flush_state not in {"flushing", "completed"}:
                return False
            try:
                self._local_stopped_at = float(stopped_at)
            except (TypeError, ValueError):
                self._local_stopped_at = self._local_stopped_at or time.time()
            self._flush_state = (
                flush_state if flush_state in {"flushing", "completed"} else "flushing"
            )
            try:
                self._helper_generated_rows = max(
                    self._helper_generated_rows,
                    int(status.get("generated_rows") or 0),
                )
            except (TypeError, ValueError):
                pass
            try:
                self._helper_queue_depth = max(
                    0,
                    int(status.get("queued_rows") or self._helper_queue_depth),
                )
            except (TypeError, ValueError):
                pass
            merged = deepcopy(self._status)
            merged.update(deepcopy(status))
            merged["running"] = False
            merged["local_stopped_at"] = self._local_stopped_at
            merged["flush_state"] = self._flush_state
            merged["capture_phase"] = (
                "local_stopped_flushing"
                if self._flush_state == "flushing"
                else "completed"
            )
            self._status = merged
            self._stream_version += 1
            return True

    def validate(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Validate one batch against current sequence state without mutation."""

        return self._ingest(batch, apply=False)

    def ingest(self, batch: dict[str, Any]) -> dict[str, Any]:
        return self._ingest(batch, apply=True)

    def _ingest(self, batch: dict[str, Any], *, apply: bool) -> dict[str, Any]:
        capture_uuid = str(batch.get("capture_uuid") or "").strip()
        if not capture_uuid:
            return {"ok": False, "error": "capture_uuid_required"}
        try:
            sequence = int(batch.get("sequence"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "sample_sequence_invalid"}
        if sequence < 0:
            return {"ok": False, "error": "sample_sequence_invalid"}
        rows = batch.get("rows")
        timestamps = batch.get("timestamps")
        if not isinstance(rows, list) or not isinstance(timestamps, list):
            return {"ok": False, "error": "sample_batch_invalid"}
        if len(rows) != len(timestamps):
            return {"ok": False, "error": "sample_batch_length_mismatch"}
        frames = batch.get("frames")
        required_frame_contract = str(
            batch.get("required_frame_contract") or ""
        ).strip()
        if required_frame_contract and required_frame_contract != "unified_frame_v1":
            return {"ok": False, "error": "frame_contract_unsupported"}
        if required_frame_contract == "unified_frame_v1" and frames is None:
            return {"ok": False, "error": "unified_frames_required"}
        frame_sequences: list[int] = []
        restored_frames: list[UnifiedFrame] = []
        if frames is not None:
            if not isinstance(frames, list) or len(frames) != len(rows):
                return {"ok": False, "error": "frame_batch_length_mismatch"}
            try:
                for frame in frames:
                    restored = UnifiedFrame.from_envelope(frame)
                    if str(frame.get("capture_uuid") or "") != capture_uuid:
                        raise ValueError("frame capture_uuid mismatch")
                    if restored.capture_sequence < 0:
                        raise ValueError("frame_sequence invalid")
                    frame_sequences.append(restored.capture_sequence)
                    restored_frames.append(restored)
            except (TypeError, ValueError) as exc:
                return {"ok": False, "error": "frame_contract_invalid", "detail": str(exc)}
        if not all(isinstance(row, dict) for row in rows):
            return {"ok": False, "error": "sample_row_invalid"}
        try:
            numeric_timestamps = [float(value) for value in timestamps]
        except (TypeError, ValueError):
            return {"ok": False, "error": "sample_timestamp_invalid"}
        if not all(math.isfinite(value) for value in numeric_timestamps):
            return {"ok": False, "error": "sample_timestamp_invalid"}
        if required_frame_contract == "unified_frame_v1":
            for row, timestamp, frame in zip(rows, numeric_timestamps, restored_frames):
                expected_channels = set(self._expected_channels)
                if not expected_channels:
                    return {"ok": False, "error": "frame_channel_contract_unbound"}
                if set(frame.channels) != expected_channels:
                    return {
                        "ok": False,
                        "error": "frame_channel_contract_mismatch",
                    }
                if not math.isclose(
                    timestamp, frame.target_wall_time, rel_tol=0.0, abs_tol=1e-6
                ):
                    return {"ok": False, "error": "frame_projection_mismatch"}
                for name, channel in frame.channels.items():
                    expected_unit = self._expected_channel_units.get(name, "")
                    actual_unit = str(channel.metadata.get("unit") or "")
                    if expected_unit and actual_unit != expected_unit:
                        return {"ok": False, "error": "frame_unit_contract_mismatch"}
                    row_value = row.get(name)
                    if channel.value is None:
                        if row_value not in (None, ""):
                            return {"ok": False, "error": "frame_projection_mismatch"}
                    else:
                        try:
                            matches = math.isclose(
                                float(row_value), channel.value,
                                rel_tol=1e-12, abs_tol=1e-12,
                            )
                        except (TypeError, ValueError):
                            matches = False
                        if not matches:
                            return {"ok": False, "error": "frame_projection_mismatch"}
        batch_digest = hashlib.sha256(
            json.dumps(
                {"rows": rows, "timestamps": numeric_timestamps, "frames": frames or []},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        with self._lock:
            new_capture = capture_uuid != self._capture_uuid
            expected_sequence = 0 if new_capture else self._expected_sequence
            expected_frame_sequence = (
                0 if new_capture else self._expected_frame_sequence
            )
            if new_capture:
                if sequence != 0:
                    return {
                        "ok": False,
                        "error": "capture_sequence_must_start_at_zero",
                        "expected_sequence": 0,
                    }
            if sequence < expected_sequence:
                if self._accepted_batch_digests.get(sequence) != batch_digest:
                    return {
                        "ok": False,
                        "error": "frame_conflict" if frames is not None else "sample_batch_conflict",
                        "capture_uuid": capture_uuid,
                        "ack_sequence": sequence,
                        "expected_sequence": expected_sequence,
                    }
                return {
                    "ok": True,
                    "duplicate": True,
                    "capture_uuid": capture_uuid,
                    "ack_sequence": sequence,
                    "expected_sequence": expected_sequence,
                }
            if sequence > expected_sequence:
                return {
                    "ok": False,
                    "error": "sample_sequence_gap",
                    "capture_uuid": capture_uuid,
                    "expected_sequence": expected_sequence,
                }
            if frame_sequences:
                expected_frames = list(
                    range(
                        expected_frame_sequence,
                        expected_frame_sequence + len(frame_sequences),
                    )
                )
                if frame_sequences != expected_frames:
                    return {
                        "ok": False,
                        "error": "frame_sequence_gap",
                        "capture_uuid": capture_uuid,
                        "expected_frame_sequence": expected_frame_sequence,
                        "received_frame_sequences": frame_sequences,
                    }
            if not apply:
                return {
                    "ok": True,
                    "validated": True,
                    "capture_uuid": capture_uuid,
                    "ack_sequence": sequence,
                    "expected_sequence": expected_sequence + 1,
                    "received_rows": len(rows),
                }
            if new_capture:
                self._reset_locked(capture_uuid)
            for index, (row, timestamp) in enumerate(zip(rows, numeric_timestamps)):
                canonical_row = dict(row)
                canonical_timestamp = timestamp
                if required_frame_contract == "unified_frame_v1":
                    restored = restored_frames[index]
                    canonical_row.update(restored.values())
                    canonical_timestamp = restored.target_wall_time
                self._rows.append(canonical_row)
                self._timestamps.append(canonical_timestamp)
            for frame in frames or []:
                self._frames.append(deepcopy(frame))
            self._accepted_batch_digests[sequence] = batch_digest
            self._accepted_batch_order.append(sequence)
            while len(self._accepted_batch_order) > 4096:
                expired = self._accepted_batch_order.popleft()
                self._accepted_batch_digests.pop(expired, None)
            self._expected_frame_sequence += len(frame_sequences)
            status = batch.get("status")
            if isinstance(status, dict):
                self._status = deepcopy(status)
            self._expected_sequence += 1
            received_at = time.time()
            self._last_batch_at = received_at
            self._total_received_rows += len(rows)
            self._stream_version += 1
            if numeric_timestamps:
                self._latest_sample_at = max(numeric_timestamps)
            transport = batch.get("transport")
            if isinstance(transport, dict):
                try:
                    self._helper_batch_created_at = float(
                        transport.get("helper_batch_created_at")
                    )
                except (TypeError, ValueError):
                    self._helper_batch_created_at = None
                try:
                    self._helper_queue_depth = max(
                        0, int(transport.get("helper_queue_depth") or 0)
                    )
                except (TypeError, ValueError):
                    self._helper_queue_depth = 0
                try:
                    raw_rtt = transport.get("helper_ack_rtt_ms")
                    self._helper_ack_rtt_ms = (
                        max(0.0, float(raw_rtt)) if raw_rtt is not None else None
                    )
                except (TypeError, ValueError):
                    self._helper_ack_rtt_ms = None
                route_type = str(transport.get("paired_route_type") or "unknown").lower()
                self._helper_route_type = (
                    route_type if route_type in {"loopback", "lan", "public"} else "unknown"
                )
                try:
                    self._helper_generated_rows = max(
                        self._helper_generated_rows,
                        int(transport.get("helper_generated_rows") or 0),
                    )
                except (TypeError, ValueError):
                    pass
                if transport.get("local_stopped_at") is not None:
                    try:
                        self._local_stopped_at = float(transport.get("local_stopped_at"))
                    except (TypeError, ValueError):
                        pass
                incoming_flush = str(transport.get("flush_state") or "")
                if incoming_flush in {"flushing", "completed"}:
                    self._flush_state = incoming_flush
            if self._local_stopped_at is not None:
                self._status["running"] = False
                self._status["local_stopped_at"] = self._local_stopped_at
                self._status["flush_state"] = self._flush_state
                self._status["capture_phase"] = (
                    "local_stopped_flushing"
                    if self._flush_state == "flushing"
                    else "completed"
                )
                self._status["generated_rows"] = self._helper_generated_rows
            return {
                "ok": True,
                "duplicate": False,
                "capture_uuid": capture_uuid,
                "ack_sequence": sequence,
                "expected_sequence": self._expected_sequence,
                "received_rows": len(rows),
                "server_received_at": received_at,
            }

    def stream_version(self) -> int:
        with self._lock:
            return self._stream_version

    def status(self) -> dict[str, Any]:
        with self._lock:
            value = deepcopy(self._status)
            value.update(
                {
                    "capture_uuid": self._capture_uuid,
                    "remote_source": True,
                    "fault_domain": "remote_helper_transport",
                    "remote_received_rows": self._total_received_rows,
                    "remote_buffered_rows": len(self._rows),
                    "remote_expected_sequence": self._expected_sequence,
                    "remote_expected_frame_sequence": self._expected_frame_sequence,
                    "remote_last_batch_at": self._last_batch_at,
                    "remote_stream_version": self._stream_version,
                    "remote_latest_sample_at": self._latest_sample_at,
                    "remote_helper_batch_created_at": self._helper_batch_created_at,
                    "remote_helper_queue_depth": self._helper_queue_depth,
                    "remote_helper_ack_rtt_ms": self._helper_ack_rtt_ms,
                    "remote_helper_route_type": self._helper_route_type,
                    "remote_helper_generated_rows": self._helper_generated_rows,
                    "local_stopped_at": self._local_stopped_at,
                    "flush_state": (
                        self._flush_state if self._local_stopped_at is not None else None
                    ),
                    "capture_phase": (
                        "local_stopped_flushing"
                        if self._local_stopped_at is not None and self._flush_state == "flushing"
                        else "completed"
                        if self._local_stopped_at is not None
                        else "running" if value.get("running") else "stopped"
                    ),
                    "remote_server_received_at": self._last_batch_at,
                    "remote_server_clock_delta_ms": (
                        max(
                            0.0,
                            (self._last_batch_at - self._helper_batch_created_at) * 1000.0,
                        )
                        if self._last_batch_at is not None
                        and self._helper_batch_created_at is not None
                        else None
                    ),
                    "remote_server_receive_latency_ms": (
                        max(
                            0.0,
                            (self._last_batch_at - self._helper_batch_created_at) * 1000.0,
                        )
                        if self._last_batch_at is not None
                        and self._helper_batch_created_at is not None
                        else None
                    ),
                    "first_sample_received": bool(self._rows),
                }
            )
            return value

    def numeric_matrix(self) -> tuple[list[dict[str, Any]], list[float]]:
        with self._lock:
            return list(self._rows), list(self._timestamps)

    def frame_envelopes(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(list(self._frames))


class RemoteAcquisitionRegistry:
    """Own one isolated acquisition mirror per authorized browser session."""

    def __init__(self, *, max_rows: int = 50_000) -> None:
        self.max_rows = max_rows
        self._lock = threading.RLock()
        self._mirrors: dict[str, RemoteAcquisitionMirror] = {}
        self._contracts: dict[str, tuple[tuple[str, ...], dict[str, str]]] = {}
        self._capture_contracts: dict[
            tuple[str, str], tuple[tuple[str, ...], dict[str, str]]
        ] = {}
        self._staged_contracts: dict[
            tuple[str, str], tuple[tuple[str, ...], dict[str, str]]
        ] = {}

    @staticmethod
    def _normalize_contract(
        channels: list[str] | tuple[str, ...], units: dict[str, str] | None
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        normalized = tuple(dict.fromkeys(str(name) for name in channels if str(name)))
        return normalized, {
            name: str((units or {}).get(name) or "") for name in normalized
        }

    def _apply_batch_contract(
        self, session_id: str, batch: dict[str, Any]
    ) -> dict[str, Any]:
        key = str(session_id or "").strip()
        capture_uuid = str(batch.get("capture_uuid") or "").strip()
        with self._lock:
            contract = self._capture_contracts.get((key, capture_uuid))
            if contract is None:
                contract = self._contracts.get(key)
        if contract is None:
            if str(batch.get("required_frame_contract") or "") == "unified_frame_v1":
                return {"ok": False, "error": "frame_channel_contract_unbound"}
            return {"ok": True}
        return self.for_session(key).configure_contract(contract[0], contract[1])

    def for_session(self, session_id: str) -> RemoteAcquisitionMirror:
        key = str(session_id or "").strip()
        if not key:
            raise ValueError("远程采集会话不能为空")
        with self._lock:
            mirror = self._mirrors.get(key)
            if mirror is None:
                mirror = RemoteAcquisitionMirror(max_rows=self.max_rows)
                contract = self._contracts.get(key)
                if contract is not None:
                    mirror.configure_contract(contract[0], contract[1])
                self._mirrors[key] = mirror
            return mirror

    def ingest(self, session_id: str, batch: dict[str, Any]) -> dict[str, Any]:
        configured = self._apply_batch_contract(session_id, batch)
        if not configured.get("ok"):
            return configured
        return self.for_session(session_id).ingest(batch)

    def validate(self, session_id: str, batch: dict[str, Any]) -> dict[str, Any]:
        configured = self._apply_batch_contract(session_id, batch)
        if not configured.get("ok"):
            return configured
        return self.for_session(session_id).validate(batch)

    def configure_contract(
        self,
        session_id: str,
        channels: list[str] | tuple[str, ...],
        units: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        key = str(session_id or "").strip()
        if not key:
            return {"ok": False, "error": "remote_contract_session_required"}
        normalized, normalized_units = self._normalize_contract(channels, units)
        with self._lock:
            self._contracts[key] = (normalized, normalized_units)
        return self.for_session(key).configure_contract(normalized, normalized_units)

    def configure_capture_contract(
        self,
        session_id: str,
        capture_uuid: str,
        channels: list[str] | tuple[str, ...],
        units: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        key = str(session_id or "").strip()
        capture_key = str(capture_uuid or "").strip()
        if not key or not capture_key:
            return {"ok": False, "error": "remote_capture_contract_identity_required"}
        contract = self._normalize_contract(channels, units)
        if not contract[0]:
            return {"ok": False, "error": "frame_channel_contract_empty"}
        with self._lock:
            self._capture_contracts[(key, capture_key)] = contract
        return {"ok": True, "channels": list(contract[0])}

    def stage_contract(
        self,
        session_id: str,
        request_id: str,
        channels: list[str] | tuple[str, ...],
        units: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        key = str(session_id or "").strip()
        request_key = str(request_id or "").strip()
        contract = self._normalize_contract(channels, units)
        if not key or not request_key or not contract[0]:
            return {"ok": False, "error": "remote_staged_contract_invalid"}
        with self._lock:
            self._staged_contracts[(key, request_key)] = contract
        return {"ok": True, "staged": True}

    def bind_contract(
        self, session_id: str, request_id: str, capture_uuid: str
    ) -> dict[str, Any]:
        key = str(session_id or "").strip()
        request_key = str(request_id or "").strip()
        capture_key = str(capture_uuid or "").strip()
        with self._lock:
            contract = self._staged_contracts.pop((key, request_key), None)
            if contract is None or not capture_key:
                return {"ok": False, "error": "remote_staged_contract_not_bound"}
            self._capture_contracts[(key, capture_key)] = contract
        return {"ok": True, "capture_uuid": capture_key}

    def restore(
        self, session_id: str, batches: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Atomically rebuild one bounded mirror from durable ordered batches."""

        key = str(session_id or "").strip()
        if not key:
            return {"ok": False, "error": "remote_restore_session_required"}
        candidate = RemoteAcquisitionMirror(max_rows=self.max_rows)
        capture_uuid = str((batches[0] if batches else {}).get("capture_uuid") or "")
        with self._lock:
            contract = self._capture_contracts.get((key, capture_uuid))
            if contract is None:
                contract = self._contracts.get(key)
        if contract is not None:
            candidate.configure_contract(contract[0], contract[1])
        last_ack: dict[str, Any] = {"ok": True, "expected_sequence": 0}
        for batch in batches:
            last_ack = candidate.ingest(batch)
            if not last_ack.get("ok"):
                return {
                    **last_ack,
                    "error": "remote_restore_failed",
                    "restore_error": last_ack.get("error"),
                }
        with self._lock:
            self._mirrors[key] = candidate
        return {**last_ack, "restored": True}


def select_acquisition_for_identity(
    role: str,
    session_id: str | None,
    local_acquisition: Any,
    remote_registry: RemoteAcquisitionRegistry,
    requested_mode: str = "",
    simulation_execution_host: str = "server",
) -> Any:
    """Select hardware rows without ever falling back for helper-backed roles."""

    if str(role or "") in {"lan_operator", "authorized"}:
        if str(requested_mode or "").lower() == "simulation":
            if str(simulation_execution_host or "") == "helper_local":
                return remote_registry.for_session(str(session_id or ""))
            return local_acquisition
        return remote_registry.for_session(str(session_id or ""))
    return local_acquisition
