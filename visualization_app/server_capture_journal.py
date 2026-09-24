"""Durable, bounded server-side sample journal for helper target-MySQL saves."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from acquisition import AcquisitionConfig
from mysql_storage import MySQLCaptureStore, MySQLSettings
from remote_mysql_setup import classify_mysql_error


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_secrets(item)
            for key, item in value.items()
            if not any(word in str(key).lower() for word in ("password", "secret", "token", "credential"))
        }
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


def _safe_capture_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only server-side acquisition metadata; target credentials stay elsewhere."""
    value = _without_secrets(dict(config or {}))
    return {
        key: item for key, item in value.items()
        if not str(key).lower().startswith("mysql_")
    }


class JournalRows:
    """Reiterable disk-backed layer, without materializing the full capture."""

    def __init__(self, path: Path, session_id: str, capture_uuid: str, count: int) -> None:
        self.path, self.session_id, self.capture_uuid, self.count = path, session_id, capture_uuid, count

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[dict[str, Any]]:
        with closing(sqlite3.connect(self.path)) as connection:
            cursor = connection.execute(
                "SELECT body FROM batches WHERE session_id=? AND capture_uuid=? ORDER BY sequence",
                (self.session_id, self.capture_uuid),
            )
            for (body,) in cursor:
                payload = json.loads(body)
                for row, timestamp in zip(payload["rows"], payload["timestamps"]):
                    recovered = dict(row)
                    recovered.setdefault("timestamp_unix", float(timestamp))
                    yield recovered


@dataclass(frozen=True)
class CaptureRecord:
    session_id: str
    capture_uuid: str
    target_config_id: str
    row_count: int
    status: dict[str, Any]
    config: dict[str, Any]
    target: dict[str, Any]
    rows: JournalRows


class ServerCaptureJournal:
    def __init__(self, path: Path, *, max_capture_bytes: int = 256 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_capture_bytes = max_capture_bytes
        self._lock = threading.RLock()
        self._armed: dict[str, tuple[str, dict[str, Any], dict[str, Any], str | None]] = {}
        self._pending_starts: dict[
            tuple[str, str], tuple[str, dict[str, Any], dict[str, Any]]
        ] = {}
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS captures (
                    session_id TEXT NOT NULL, capture_uuid TEXT NOT NULL,
                    config_id TEXT NOT NULL, next_sequence INTEGER NOT NULL,
                    row_count INTEGER NOT NULL, byte_count INTEGER NOT NULL,
                    final_status TEXT, save_state TEXT NOT NULL DEFAULT 'receiving',
                    save_error TEXT, save_result TEXT,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    target_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (session_id, capture_uuid)
                );
                CREATE TABLE IF NOT EXISTS batches (
                    session_id TEXT NOT NULL, capture_uuid TEXT NOT NULL,
                    sequence INTEGER NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY (session_id, capture_uuid, sequence)
                );
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def arm(
        self,
        session_id: str,
        target_config_id: str,
        *,
        config: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> None:
        if not session_id or not target_config_id:
            raise ValueError("目标数据库会话配置缺失")
        with self._lock:
            self._armed[session_id] = (
                target_config_id,
                _safe_capture_config(config),
                _without_secrets(target or {}),
                None,
            )

    def arm_start(
        self,
        session_id: str,
        request_id: str,
        target_config_id: str,
        *,
        config: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> None:
        if not session_id or not request_id or not target_config_id:
            raise ValueError("目标数据库启动意图缺失")
        with self._lock:
            self._pending_starts[(session_id, request_id)] = (
                target_config_id, _safe_capture_config(config), _without_secrets(target or {})
            )

    def bind_start_result(self, session_id: str, request_id: str, result: dict[str, Any]) -> bool:
        with self._lock:
            pending = self._pending_starts.pop((session_id, request_id), None)
            capture_uuid = str((result or {}).get("capture_uuid") or "").strip()
            if pending is None or not capture_uuid or not bool((result or {}).get("running")):
                return False
            self._armed[session_id] = (*pending, capture_uuid)
            return True

    def disarm(self, session_id: str) -> None:
        with self._lock:
            self._armed.pop(session_id, None)
            for key in [key for key in self._pending_starts if key[0] == session_id]:
                self._pending_starts.pop(key, None)

    def manages(self, session_id: str, capture_uuid: str) -> bool:
        """Whether a batch must use the durable target-MySQL ACK path."""
        with self._lock:
            if session_id in self._armed or any(key[0] == session_id for key in self._pending_starts):
                return True
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM captures WHERE session_id=? AND capture_uuid=?",
                (session_id, capture_uuid),
            ).fetchone() is not None

    def close(self) -> None:
        pass  # Connections are scoped to each transaction and iterator.

    def ingest(self, session_id: str, batch: dict[str, Any], mirrors: Any) -> dict[str, Any]:
        capture_uuid = str(batch.get("capture_uuid") or "")
        try:
            sequence = int(batch.get("sequence"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "sample_sequence_invalid"}
        rows, timestamps = batch.get("rows"), batch.get("timestamps")
        if (not capture_uuid or sequence < 0 or not isinstance(rows, list)
                or not isinstance(timestamps, list) or len(rows) != len(timestamps)
                or not all(isinstance(row, dict) for row in rows)):
            return {"ok": False, "error": "sample_batch_invalid"}
        try:
            timestamps = [float(value) for value in timestamps]
        except (TypeError, ValueError):
            return {"ok": False, "error": "sample_timestamp_invalid"}
        if any(_without_secrets(row) != row for row in rows):
            return {"ok": False, "error": "sample_row_contains_secret"}
        body = json.dumps({"rows": rows, "timestamps": timestamps}, ensure_ascii=False, allow_nan=False)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        safe_status = _without_secrets(batch.get("status") or {})
        if not isinstance(safe_status, dict):
            safe_status = {}
        byte_count = len(body.encode("utf-8"))
        with self._lock:
            try:
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute(
                        "SELECT config_id,next_sequence,row_count,byte_count,config_json,target_json FROM captures "
                        "WHERE session_id=? AND capture_uuid=?", (session_id, capture_uuid),
                    ).fetchone()
                    if current is None:
                        armed = self._armed.get(session_id)
                        if not armed:
                            return {"ok": False, "error": "target_capture_not_armed"}
                        if sequence != 0:
                            return {"ok": False, "error": "capture_sequence_must_start_at_zero", "expected_sequence": 0}
                        config_id, capture_config, target, expected_capture_uuid = armed
                        if expected_capture_uuid and capture_uuid != expected_capture_uuid:
                            return {"ok": False, "error": "target_capture_unexpected"}
                        connection.execute(
                            "INSERT INTO captures "
                            "(session_id,capture_uuid,config_id,next_sequence,row_count,byte_count,config_json,target_json) "
                            "VALUES (?,?,?,0,0,0,?,?)",
                            (session_id, capture_uuid, config_id,
                             json.dumps(capture_config, ensure_ascii=False),
                             json.dumps(target, ensure_ascii=False)),
                        )
                        current = (config_id, 0, 0, 0, "{}", "{}")
                    config_id, next_sequence, count, total_bytes, _config, _target = current
                    if sequence < next_sequence:
                        original = connection.execute(
                            "SELECT digest FROM batches WHERE session_id=? AND capture_uuid=? AND sequence=?",
                            (session_id, capture_uuid, sequence),
                        ).fetchone()
                        if not original or original[0] != digest:
                            return {"ok": False, "error": "sample_sequence_conflict"}
                        return {"ok": True, "duplicate": True, "capture_uuid": capture_uuid,
                                "ack_sequence": sequence, "expected_sequence": next_sequence}
                    if sequence > next_sequence:
                        return {"ok": False, "error": "sample_sequence_gap", "expected_sequence": next_sequence}
                    if total_bytes + byte_count > self.max_capture_bytes:
                        return {"ok": False, "error": "target_journal_quota_exceeded"}
                    connection.execute(
                        "INSERT INTO batches (session_id,capture_uuid,sequence,digest,body) VALUES (?,?,?,?,?)",
                        (session_id, capture_uuid, sequence, digest, body),
                    )
                    final = (safe_status.get("running") is False
                             and bool(safe_status.get("finalization_complete")))
                    connection.execute(
                        "UPDATE captures SET next_sequence=?,row_count=?,byte_count=?,final_status=?, "
                        "save_state=CASE WHEN ? THEN 'ready' ELSE save_state END "
                        "WHERE session_id=? AND capture_uuid=?",
                        (next_sequence + 1, count + len(rows), total_bytes + byte_count,
                         json.dumps(safe_status, ensure_ascii=False) if final else None,
                         int(final),
                         session_id, capture_uuid),
                    )
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                return {"ok": False, "error": "target_journal_write_failed", "detail": str(exc)}
            # Only after commit may the helper advance its cursor. The mirror is
            # deliberately bounded and is never the source for MySQL persistence.
            mirror_ack = mirrors.ingest(session_id, batch)
            if not mirror_ack.get("ok"):
                return {"ok": False, "error": "target_mirror_sync_failed"}
            return mirror_ack

    def ready_capture(self, session_id: str, capture_uuid: str) -> CaptureRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT config_id,row_count,final_status,config_json,target_json FROM captures "
                "WHERE session_id=? AND capture_uuid=?",
                (session_id, capture_uuid),
            ).fetchone()
        if row is None or not row[2]:
            return None
        status = json.loads(row[2])
        if int(status.get("sample_count") or 0) != row[1]:
            return None
        return CaptureRecord(
            session_id, capture_uuid, row[0], row[1], status,
            json.loads(row[3] or "{}"), json.loads(row[4] or "{}"),
            JournalRows(self.path, session_id, capture_uuid, row[1]),
        )

    def claim_save(self, session_id: str, capture_uuid: str) -> CaptureRecord | None:
        """Atomically reserve one ready/failed capture for a server write attempt."""
        with self._lock:
            record = self.ready_capture(session_id, capture_uuid)
            if record is None:
                return None
            with self._connection() as connection:
                cursor = connection.execute(
                    "UPDATE captures SET save_state='saving',save_error=NULL "
                    "WHERE session_id=? AND capture_uuid=? AND save_state IN ('ready','failed')",
                    (session_id, capture_uuid),
                )
                if cursor.rowcount != 1:
                    return None
            return record

    def mark_save_result(
        self, session_id: str, capture_uuid: str, result: dict[str, Any]
    ) -> None:
        safe_result = _without_secrets(result)
        ok = bool(safe_result.get("ok"))
        with self._connection() as connection:
            connection.execute(
                "UPDATE captures SET save_state=?,save_error=?,save_result=? "
                "WHERE session_id=? AND capture_uuid=?",
                (
                    "saved" if ok else "failed",
                    None if ok else str(safe_result.get("error") or "目标 MySQL 保存失败"),
                    json.dumps(safe_result, ensure_ascii=False), session_id, capture_uuid,
                ),
            )

    def capture_status(self, session_id: str, capture_uuid: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT row_count,save_state,save_error,save_result,target_json FROM captures "
                "WHERE session_id=? AND capture_uuid=?", (session_id, capture_uuid),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row[3] or "{}")
        target = json.loads(row[4] or "{}")
        return {
            "enabled": True, "scope": "server_target", "execution_host": "server",
            "state": row[1], "ok": row[1] == "saved", "received_rows": int(row[0]),
            "saved_rows": int(result.get("saved_rows") or 0),
            "error": row[2], "error_detail": result.get("error_detail"), "target": target,
        }


class TargetMySQLSaveCoordinator:
    """Finalize complete helper captures on the server, outside the ACK path."""

    def __init__(
        self,
        journal: ServerCaptureJournal,
        target_profiles: Any,
        store_factory: Callable[[MySQLSettings], Any] = MySQLCaptureStore,
    ) -> None:
        self.journal = journal
        self.target_profiles = target_profiles
        self.store_factory = store_factory

    def save_now(self, session_id: str, capture_uuid: str) -> dict[str, Any]:
        record = self.journal.claim_save(session_id, capture_uuid)
        if record is None:
            current = self.journal.capture_status(session_id, capture_uuid)
            if current is None:
                return {"ok": False, "error": "target_capture_not_ready"}
            return {"ok": bool(current.get("ok")), **current}
        selection = None
        try:
            selection = self.target_profiles.resolve_id(session_id, record.target_config_id)
            config_values = dict(record.config)
            config_values.update(
                {
                    "capture_uuid": record.capture_uuid,
                    # This object is metadata for persistence only.  It must
                    # not revalidate or open the visitor's physical endpoint.
                    "acquisition_mode": "simulation",
                    "mysql_enabled": False,
                    "mysql_local_enabled": False,
                }
            )
            config = AcquisitionConfig(**config_values)
            summary = {
                "capture_uuid": record.capture_uuid,
                "sample_count": record.row_count,
                "source": "remote_helper_server_journal",
                "completed_layers": [int(config.layer) + 1],
            }
            result = self.store_factory(selection.settings).save_layer(
                config,
                rows=record.rows,
                layer_file=None,
                full_specimen_file=None,
                timestamp_file=None,
                folder_path=None,
                summary=summary,
            )
            result = _without_secrets(result if isinstance(result, dict) else {"ok": False})
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            if selection is not None and selection.settings.password:
                error = error.replace(selection.settings.password, "***")
            result = {"ok": False, "error": error, "saved_rows": 0}
        if not result.get("ok"):
            result["error_detail"] = classify_mysql_error(result.get("error"))
        result.update({"scope": "server_target", "execution_host": "server"})
        self.journal.mark_save_result(session_id, capture_uuid, result)
        return result
