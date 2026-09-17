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
                    "remote_server_received_at": self._last_batch_at,
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
) -> Any:
    """Select hardware rows without ever falling back for helper-backed roles."""

    if str(role or "") in {"lan_operator", "authorized"}:
        if str(requested_mode or "").lower() == "simulation":
            return local_acquisition
        return remote_registry.for_session(str(session_id or ""))
    return local_acquisition
