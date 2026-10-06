"""Session-scoped acquisition mirrors for visitor-computer edge helpers."""

from __future__ import annotations

import threading
import time
from collections import deque
from copy import deepcopy
from typing import Any


class RemoteAcquisitionMirror:
    """Expose helper sample batches through the AcquisitionManager read API."""

    def __init__(self, *, max_rows: int = 50_000) -> None:
        self.max_rows = max(24, int(max_rows))
        self._lock = threading.RLock()
        self._rows: deque[dict[str, Any]] = deque(maxlen=self.max_rows)
        self._timestamps: deque[float] = deque(maxlen=self.max_rows)
        self._capture_uuid = ""
        self._expected_sequence = 0
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

    def _reset_locked(self, capture_uuid: str) -> None:
        self._rows.clear()
        self._timestamps.clear()
        self._capture_uuid = capture_uuid
        self._expected_sequence = 0
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

    def ingest(self, batch: dict[str, Any]) -> dict[str, Any]:
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
        if not all(isinstance(row, dict) for row in rows):
            return {"ok": False, "error": "sample_row_invalid"}
        try:
            numeric_timestamps = [float(value) for value in timestamps]
        except (TypeError, ValueError):
            return {"ok": False, "error": "sample_timestamp_invalid"}

        with self._lock:
            if capture_uuid != self._capture_uuid:
                if sequence != 0:
                    return {
                        "ok": False,
                        "error": "capture_sequence_must_start_at_zero",
                        "expected_sequence": 0,
                    }
                self._reset_locked(capture_uuid)
            if sequence < self._expected_sequence:
                return {
                    "ok": True,
                    "duplicate": True,
                    "capture_uuid": capture_uuid,
                    "ack_sequence": sequence,
                    "expected_sequence": self._expected_sequence,
                }
            if sequence > self._expected_sequence:
                return {
                    "ok": False,
                    "error": "sample_sequence_gap",
                    "capture_uuid": capture_uuid,
                    "expected_sequence": self._expected_sequence,
                }
            for row, timestamp in zip(rows, numeric_timestamps):
                self._rows.append(dict(row))
                self._timestamps.append(timestamp)
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
                    "remote_received_rows": self._total_received_rows,
                    "remote_buffered_rows": len(self._rows),
                    "remote_expected_sequence": self._expected_sequence,
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


class RemoteAcquisitionRegistry:
    """Own one isolated acquisition mirror per authorized browser session."""

    def __init__(self, *, max_rows: int = 50_000) -> None:
        self.max_rows = max_rows
        self._lock = threading.RLock()
        self._mirrors: dict[str, RemoteAcquisitionMirror] = {}

    def for_session(self, session_id: str) -> RemoteAcquisitionMirror:
        key = str(session_id or "").strip()
        if not key:
            raise ValueError("远程采集会话不能为空")
        with self._lock:
            mirror = self._mirrors.get(key)
            if mirror is None:
                mirror = RemoteAcquisitionMirror(max_rows=self.max_rows)
                self._mirrors[key] = mirror
            return mirror

    def ingest(self, session_id: str, batch: dict[str, Any]) -> dict[str, Any]:
        return self.for_session(session_id).ingest(batch)


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
