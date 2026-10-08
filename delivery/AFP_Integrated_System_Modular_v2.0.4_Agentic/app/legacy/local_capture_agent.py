"""Local computer adapter used by the web capture helper.

The agent deliberately stays thin: protocol readers, channel normalization,
capture timing, and persistence remain in the existing acquisition modules.
This module only adds a stable local-helper contract and deterministic mapping
from the five logical AFP sensors to discovered physical interfaces.
"""

from __future__ import annotations

import json
import ipaddress
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import os
from pathlib import Path
from typing import Any

from acquisition import (
    AcquisitionManager,
    MySQLSettings,
    check_capture_save_root,
    select_capture_folder,
)
from helper_relay import ALLOWED_HELPER_COMMANDS
from mysql_storage import MySQLCaptureStore
from json_safety import json_safe_value
from remote_mysql_setup import classify_mysql_error
from simulation_source_transfer import SimulationSourceCache, inspect_csv_source


_ROLE_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("thermocouple_8ch", "usb_hid", ("smrf_hid",)),
    ("plc_process", "ethernet", ("ethernet",)),
    ("uvc_temperature", "usb_uvc", ("uvc",)),
    ("abb_motion", "ethernet", ("ethernet",)),
    ("m3232_pressure", "serial", ("serial",)),
)


class LocalCaptureAgent:
    """Expose local discovery, capture, and MySQL operations to a relay."""

    def __init__(
        self,
        manager: AcquisitionManager | None = None,
        *,
        simulation_cache_root: str | Path | None = None,
    ) -> None:
        self.manager = manager or AcquisitionManager()
        local_app_data = os.environ.get("LOCALAPPDATA")
        default_cache_root = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        ) / "AFP_Local_Capture_Helper" / "simulation-cache"
        self._simulation_cache = SimulationSourceCache(
            simulation_cache_root or default_cache_root
        )
        self._stream_lock = threading.RLock()
        self._capture_uuid = ""
        self._stream_cursor = 0
        self._stream_sequence = 0
        self._pending_batch: dict[str, Any] | None = None
        self._status_revision = 0
        self._pending_status_revision = -1
        self._status_dirty = False
        self._local_stopped_at: float | None = None
        self._flush_state = "idle"
        self._source_transfer_status: dict[str, Any] = {"state": "not_required"}

    @staticmethod
    def _choose_candidate(
        physical: list[dict[str, Any]],
        kind: str,
        protocols: tuple[str, ...],
        *,
        used: set[str],
        allow_used: bool = False,
    ) -> dict[str, Any] | None:
        candidates = [
            item
            for item in physical
            if str(item.get("kind") or "") == kind
            and str(item.get("protocol") or "") in protocols
            and bool(item.get("auto_bind_eligible"))
            and (
                allow_used
                or str(item.get("parent_port_id") or item.get("id") or "") not in used
            )
        ]
        candidates.sort(
            key=lambda item: (
                not bool(item.get("detected")),
                not bool(item.get("driver_available")),
                not bool(item.get("auto_assignable")),
                str(item.get("id") or ""),
            )
        )
        return candidates[0] if candidates else None

    def discover(self) -> dict[str, Any]:
        discovered = self.manager.discover_interfaces()
        physical = list(discovered.get("physical_interfaces") or [])
        bindings: list[dict[str, Any]] = []
        used: set[str] = set()
        for role, kind, protocols in _ROLE_SPECS:
            # Only PLC and ABB are allowed to share an Ethernet adapter.
            allow_used = role == "abb_motion"
            candidate = self._choose_candidate(
                physical, kind, protocols, used=used, allow_used=allow_used
            )
            physical_id = str(candidate.get("id") or "") if candidate else ""
            if candidate and not allow_used:
                # Multiple live endpoints can represent one USB parent port.
                # Reserve the physical resource, not merely the endpoint ID.
                used.add(str(candidate.get("parent_port_id") or physical_id))
            detected = bool(candidate and candidate.get("detected"))
            driver_available = bool(candidate and candidate.get("driver_available"))
            identity_verified = bool(candidate and candidate.get("identity_verified"))
            protocol_ready = bool(candidate and candidate.get("protocol_ready"))
            bindings.append(
                {
                    "role": role,
                    "physical_interface_id": physical_id or None,
                    "physical_port_id": (candidate or {}).get("parent_port_id") or None,
                    "physical_kind": kind,
                    "protocol": protocols[0],
                    "interface_detected": detected,
                    "driver_available": driver_available,
                    "endpoint_present": bool(candidate and candidate.get("endpoint_present")),
                    "identity_verified": identity_verified,
                    "protocol_ready": protocol_ready,
                    "sensor_data_state": "unknown",
                    "acquisition_state": "not_started",
                    "state": (
                        "identity_verified"
                        if detected and identity_verified
                        else "driver_available"
                        if driver_available
                        else "unavailable"
                    ),
                    "message": (
                        "接口与设备身份已识别，协议数据待采集验证"
                        if detected and identity_verified
                        else "驱动或协议已配置，传感器数据待采集验证"
                        if driver_available
                        else "未找到兼容的实际接口"
                    ),
                }
            )
        return {
            "interfaces": physical,
            "sensor_bindings": bindings,
            "usb_topology": discovered.get("usb_topology") or {
                "schema_version": 1,
                "state": "unavailable",
                "provider": "legacy_discovery",
                "errors": ["旧版发现结果未提供 USB 物理端口拓扑"],
                "docks": [],
                "usb_ports": [],
                "devices": [],
            },
            "interface_transport_catalog": discovered.get("interface_transport_catalog") or {
                "schema_version": 1,
                "usb_ports": [],
                "unlocated_usb_interface_ids": [],
                "native_serial_interface_ids": [],
                "ethernet_interface_ids": [],
            },
            "capabilities": {
                "hardware_discovery": True,
                "usb_topology_v1": bool(discovered.get("usb_topology")),
                "interface_transport_catalog_v1": bool(discovered.get("interface_transport_catalog")),
                "real_capture": True,
                "process_parameter_read": True,
                "local_csv_save": True,
                "local_mysql_save": True,
            },
            "warnings": [discovered["error"]] if discovered.get("error") else [],
            "raw_discovery": discovered,
        }

    def mysql_preflight(
        self,
        settings: MySQLSettings,
        *,
        write_test: bool = False,
        initialize_if_missing: bool = False,
    ) -> dict[str, Any]:
        store = MySQLCaptureStore(settings)
        result = store.preflight(write_test=write_test)
        schema_initialized = False
        error_text = str(result.get("error") or "").lower()
        unknown_database = (
            not result.get("ok")
            and result.get("stage") == "connect"
            and (
                "1049" in error_text
                or "unknown database" in error_text
                or "doesn't exist" in error_text
                or "does not exist" in error_text
            )
        )
        missing_schema = (
            not result.get("ok")
            and result.get("stage") == "schema"
            and bool(result.get("missing_objects"))
        )
        if initialize_if_missing and (unknown_database or missing_schema):
            # This command is only exposed by the explicit helper-local
            # "检查本机数据库" action.  A missing database may therefore be
            # created here, while ordinary reads and the server-target path
            # remain strictly non-DDL.  Authentication, network, driver and
            # permission failures do not enter this branch.
            initialized = store.initialize_schema(create_database=unknown_database)
            if initialized.get("ok"):
                result = store.preflight(write_test=write_test)
                schema_initialized = bool(result.get("ok"))
            else:
                initialization_error = initialized.get("error") or "AFP数据库表结构初始化失败"
                result = {
                    **result,
                    "error": initialization_error,
                    "error_detail": initialized.get("error_detail")
                    or classify_mysql_error(initialization_error),
                    "initialization_error": initialization_error,
                }
        result["schema_initialized"] = schema_initialized
        result["scope"] = "helper_local"
        result["execution_host"] = "visitor_local_computer"
        return result

    def mysql_relation_map(self, settings: MySQLSettings, *, limit: int = 1000) -> dict[str, Any]:
        result = MySQLCaptureStore(settings).relation_map(max(1, min(int(limit), 1000)), auto_initialize=False)
        result["scope"] = "helper_local"
        result["execution_host"] = "visitor_local_computer"
        return result

    def check_save_root(self, path: str) -> dict[str, Any]:
        return check_capture_save_root(str(path or ""))

    def select_folder(self, initial_path: str = "") -> dict[str, Any]:
        selected = select_capture_folder(str(initial_path or ""))
        return {"selected": bool(selected), "path": selected}

    def prepare_simulation_source(
        self,
        transfer: dict[str, Any],
        fetch_chunk,
        *,
        required_channels: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Materialize one verified browser source inside the helper cache."""

        manifest = transfer.get("manifest") if isinstance(transfer, dict) else None
        if not isinstance(manifest, dict):
            raise ValueError("模拟源交付清单缺失")
        cache_hit = self._simulation_cache.is_ready(transfer)
        cache_root = self._simulation_cache.materialize(transfer, fetch_chunk)
        source_type = str(manifest.get("source_type") or "")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("模拟源交付清单没有文件")
        if source_type == "single_csv":
            source_path = cache_root / str(files[0].get("relative_path") or "")
        elif source_type == "folder_csv":
            source_path = cache_root
        else:
            raise ValueError("helper 模拟回放只支持 CSV 或 CSV 文件夹")
        inspected = inspect_csv_source(
            source_type,
            source_path,
            required_channels=required_channels,
        )
        result = {
            "path": str(source_path),
            "source_type": source_type,
            "content_sha256": str(manifest.get("content_sha256") or ""),
            "files": len(files),
            "relative_paths": [str(item.get("relative_path") or "") for item in files],
            "bytes": int(manifest.get("total_bytes") or 0),
            "channels": inspected["channels"],
            "numeric_channels": inspected["numeric_channels"],
            "valid_rows": inspected["valid_rows"],
            "state": "ready",
            "cache_hit": cache_hit,
        }
        with self._stream_lock:
            self._source_transfer_status = dict(result)
        return result

    def resolve_prepared_simulation_source(
        self,
        reference: dict[str, Any],
        *,
        required_channels: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        digest = str(reference.get("content_sha256") or "").strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("helper 模拟源 ready 哈希无效")
        source_type = str(reference.get("source_type") or "").strip().lower()
        relative_paths = [
            str(item or "").replace("\\", "/").strip()
            for item in (reference.get("relative_paths") or [])
            if str(item or "").strip()
        ]
        cache_root = (self._simulation_cache.root / digest).resolve()
        marker = cache_root / ".manifest.json"
        if self._simulation_cache.root not in cache_root.parents or not marker.is_file():
            raise ValueError("helper 模拟源尚未预下载或缓存已清理，请重新下载并校验")
        if source_type == "single_csv" and len(relative_paths) == 1:
            source_path = (cache_root / relative_paths[0]).resolve()
        elif source_type == "folder_csv" and relative_paths:
            source_path = cache_root
        else:
            raise ValueError("helper 模拟源 ready 描述不完整")
        if source_path != cache_root and cache_root not in source_path.parents:
            raise ValueError("helper 模拟源 ready 路径越界")
        inspected = inspect_csv_source(
            source_type,
            source_path,
            required_channels=required_channels,
        )
        result = {
            "path": str(source_path),
            "source_type": source_type,
            "content_sha256": digest,
            "relative_paths": relative_paths,
            "files": len(relative_paths),
            "channels": inspected["channels"],
            "numeric_channels": inspected["numeric_channels"],
            "valid_rows": inspected["valid_rows"],
            "state": "ready",
            "cache_hit": True,
        }
        with self._stream_lock:
            self._source_transfer_status = dict(result)
        return result

    def start_capture(self, config: Any) -> dict[str, Any]:
        result = self.manager.start(config)
        with self._stream_lock:
            self._capture_uuid = uuid.uuid4().hex
            self._stream_cursor = 0
            self._stream_sequence = 0
            self._pending_batch = None
            self._status_revision += 1
            self._pending_status_revision = -1
            self._status_dirty = True
            self._local_stopped_at = None
            self._flush_state = "running"
            capture_uuid = self._capture_uuid
        response = dict(result) if isinstance(result, dict) else {"result": result}
        response["capture_uuid"] = capture_uuid
        response["execution_host"] = "helper_local"
        if str(getattr(config, "acquisition_mode", "")) == "simulation":
            response["source_transfer"] = dict(self._source_transfer_status)
        return response

    def next_sample_batch(self, *, limit: int = 20) -> dict[str, Any] | None:
        """Return one replayable batch without advancing before acknowledgement."""

        batch_limit = max(1, min(int(limit), 200))
        with self._stream_lock:
            if self._pending_batch is not None:
                return dict(self._pending_batch)
            if not self._capture_uuid:
                return None
            incremental = None
            reader = getattr(self.manager, "stream_rows_since", None)
            if callable(reader):
                candidate = reader(self._stream_cursor, batch_limit)
                if isinstance(candidate, dict):
                    incremental = candidate
            if incremental is not None:
                rows = list(incremental.get("rows") or [])
                timestamps = list(incremental.get("timestamps") or [])
                end_cursor = int(incremental.get("next_cursor", self._stream_cursor))
                total_count = int(incremental.get("total_count", end_cursor))
                if bool(incremental.get("truncated")):
                    raise RuntimeError("helper样本队列已超过内存边界，无法保证完整重传")
            else:
                all_rows, all_timestamps = self.manager.numeric_matrix()
                if self._stream_cursor > len(all_rows):
                    self._stream_cursor = 0
                    self._stream_sequence = 0
                end_cursor = min(len(all_rows), self._stream_cursor + batch_limit)
                rows = all_rows[self._stream_cursor:end_cursor]
                timestamps = all_timestamps[self._stream_cursor:end_cursor]
                total_count = len(all_rows)
            if end_cursor <= self._stream_cursor and not self._status_dirty:
                return None
            pending = {
                "capture_uuid": self._capture_uuid,
                "sequence": self._stream_sequence,
                "cursor_start": self._stream_cursor,
                "cursor_end": end_cursor,
                "rows": [json_safe_value(dict(row)) for row in rows],
                "timestamps": [float(value) for value in timestamps],
                "status": json_safe_value(self.status()),
                "transport": {
                    "helper_batch_created_at": time.time(),
                    "helper_queue_depth": max(0, total_count - end_cursor),
                    "helper_generated_rows": total_count,
                    "local_stopped_at": self._local_stopped_at,
                    "flush_state": self._flush_state,
                },
            }
            self._pending_batch = pending
            self._pending_status_revision = self._status_revision
            return dict(pending)

    def wait_for_stream_activity(self, timeout: float = 0.2) -> bool:
        waiter = getattr(self.manager, "wait_for_stream_activity", None)
        if callable(waiter):
            return bool(waiter(timeout))
        time.sleep(max(0.0, min(float(timeout), 0.2)))
        return False

    def ack_sample_batch(self, capture_uuid: str, sequence: int) -> bool:
        with self._stream_lock:
            pending = self._pending_batch
            if pending is None:
                return False
            if str(capture_uuid) != str(pending["capture_uuid"]):
                return False
            if int(sequence) != int(pending["sequence"]):
                return False
            self._stream_cursor = int(
                pending.get("cursor_end", self._stream_cursor + len(pending["rows"]))
            )
            self._stream_sequence += 1
            self._pending_batch = None
            pending_status_revision = self._pending_status_revision
            if self._local_stopped_at is not None and self._flush_state == "flushing":
                counts = None
                counter = getattr(self.manager, "stream_counts", None)
                if callable(counter):
                    candidate = counter()
                    if isinstance(candidate, dict):
                        counts = candidate
                if counts is None:
                    all_rows, _timestamps = self.manager.numeric_matrix()
                    total_count = len(all_rows)
                else:
                    total_count = int(counts.get("total_count", 0))
                if total_count <= self._stream_cursor:
                    self._flush_state = "completed"
                    self._status_revision += 1
                    self._status_dirty = True
            if pending_status_revision == self._status_revision:
                self._status_dirty = False
            self._pending_status_revision = -1
            return True

    def stream_metrics(self, *, status: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return safe transport counters without exposing samples or credentials."""

        with self._stream_lock:
            current_status = status if isinstance(status, dict) else self.manager.status()
            counts = None
            counter = getattr(self.manager, "stream_counts", None)
            if callable(counter):
                candidate = counter()
                if isinstance(candidate, dict):
                    counts = candidate
            if counts is None:
                matrix = self.manager.numeric_matrix()
                if isinstance(matrix, tuple) and len(matrix) == 2:
                    rows, _timestamps = matrix
                    total_count = len(rows)
                else:
                    total_count = int((current_status or {}).get("sample_count") or 0)
            else:
                total_count = int(counts.get("total_count", 0))
            config = current_status.get("config") if isinstance(current_status, dict) else {}
            try:
                sample_rate = float((config or {}).get("sample_rate") or 10.0)
            except (TypeError, ValueError):
                sample_rate = 10.0
            queued_rows = max(0, total_count - self._stream_cursor)
            return {
                "capture_uuid": self._capture_uuid,
                "generated_rows": total_count,
                "queued_rows": queued_rows,
                "queue_age_seconds": queued_rows / max(0.1, sample_rate),
                "pending_sequence": (
                    int(self._pending_batch["sequence"])
                    if isinstance(self._pending_batch, dict)
                    else None
                ),
                "next_sequence": self._stream_sequence,
                "sample_rate_hz": max(0.1, sample_rate),
                "local_stopped_at": self._local_stopped_at,
                "flush_state": self._flush_state,
            }

    def check_capture(self, config: Any) -> dict[str, Any]:
        return self.manager.test_connection(config)

    def read_process_parameters(self, config: Any) -> dict[str, Any]:
        return self.manager.read_process_parameters(config)

    def stop_capture(self) -> dict[str, Any]:
        result = self.manager.stop()
        stopped_at = time.time()
        response = dict(result) if isinstance(result, dict) else {"result": result}
        with self._stream_lock:
            self._local_stopped_at = stopped_at
            self._flush_state = "flushing"
            self._status_revision += 1
            self._status_dirty = True
            capture_uuid = self._capture_uuid
            if isinstance(self._pending_batch, dict):
                pending_status = dict(self._pending_batch.get("status") or {})
                pending_status.update(response)
                pending_status.update(
                    {
                        "running": False,
                        "capture_uuid": capture_uuid,
                        "local_stopped_at": stopped_at,
                        "flush_state": "flushing",
                        "capture_phase": "local_stopped_flushing",
                        "finalization_complete": False,
                    }
                )
                self._pending_batch["status"] = json_safe_value(pending_status)
                transport = self._pending_batch.setdefault("transport", {})
                if isinstance(transport, dict):
                    transport["local_stopped_at"] = stopped_at
                    transport["flush_state"] = "flushing"
        response["capture_uuid"] = capture_uuid
        response["execution_host"] = "helper_local"
        response["source_transfer"] = dict(self._source_transfer_status)
        metrics = self.stream_metrics(status=response)
        response.update(
            {
                "running": False,
                "local_stopped_at": stopped_at,
                "flush_state": "flushing",
                "capture_phase": "local_stopped_flushing",
                "generated_rows": metrics["generated_rows"],
                "queued_rows": metrics["queued_rows"],
                "transport": metrics,
            }
        )
        return response

    def status(self) -> dict[str, Any]:
        value = dict(self.manager.status())
        value["execution_host"] = "helper_local"
        value["source_transfer"] = dict(self._source_transfer_status)
        value["transport"] = self.stream_metrics(status=value)
        if self._local_stopped_at is not None:
            value["running"] = False
            value["local_stopped_at"] = self._local_stopped_at
            value["flush_state"] = self._flush_state
            value["capture_phase"] = (
                "local_stopped_flushing"
                if self._flush_state == "flushing"
                else "completed"
            )
            value["generated_rows"] = value["transport"]["generated_rows"]
            value["queued_rows"] = value["transport"]["queued_rows"]
            if self._flush_state == "flushing":
                value["finalization_complete"] = False
        started_at = value.get("started_at")
        sample_count = int(value.get("sample_count") or 0)
        try:
            elapsed = max(0.0, time.time() - float(started_at)) if started_at else 0.0
        except (TypeError, ValueError):
            elapsed = 0.0
        value["local_effective_rate_hz"] = (
            round(sample_count / elapsed, 6) if elapsed > 0 else 0.0
        )
        return value


class HelperTransport:
    """Small, dependency-optional JSON/WSS transport contract.

    The pairing token is intentionally kept out of JSON messages.  A real
    WSS client supplies it as an authorization header during connection; the
    message methods here remain safe to log and easy to test.
    """

    def __init__(self, server_url: str, pairing_token: str, *, device_id: str = "") -> None:
        self.server_url = str(server_url).strip()
        self.pairing_token = str(pairing_token)
        self.device_id = str(device_id or "local-helper")

    def hello(self, *, capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "type": "hello",
            "device_id": self.device_id,
            "capabilities": dict(capabilities or {}),
        }

    @staticmethod
    def decode_command(raw: str | bytes) -> dict[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("helper命令不是有效JSON") from exc
        if not isinstance(value, dict) or value.get("type") != "command":
            raise ValueError("helper命令类型无效")
        command = str(value.get("command") or "").strip().lower()
        if command not in ALLOWED_HELPER_COMMANDS:
            raise ValueError("helper命令不在允许列表")
        payload = value.get("payload")
        return {
            "type": "command",
            "request_id": str(value.get("request_id") or ""),
            "command": command,
            "payload": dict(payload) if isinstance(payload, dict) else {},
        }

    @staticmethod
    def encode_result(request_id: str, payload: dict[str, Any]) -> str:
        return json.dumps(
            {
                "type": "result",
                "request_id": str(request_id),
                "payload": dict(payload),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def connect_once(self):
        """Open one WSS connection when websocket-client is installed."""
        if not self.server_url:
            raise ValueError("辅助服务地址不能为空")
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise RuntimeError("缺少websocket-client依赖，无法连接公网辅助服务") from exc
        parsed = urllib.parse.urlsplit(self.server_url)
        if parsed.scheme not in {"ws", "wss", "http", "https"}:
            raise ValueError("本地辅助程序服务地址协议无效")
        if parsed.scheme in {"ws", "http"}:
            hostname = str(parsed.hostname or "").strip().lower()
            private_origin = hostname in {"localhost", "127.0.0.1", "::1"}
            if not private_origin:
                try:
                    private_origin = ipaddress.ip_address(hostname).is_private
                except ValueError:
                    private_origin = False
            if not private_origin:
                raise ValueError("公网辅助程序地址必须使用 HTTPS/WSS")
        path = parsed.path.rstrip("/")
        if not path or path == "/":
            path = "/api/helper/ws"
        elif not path.endswith("/api/helper/ws"):
            # The saved server value is normally an origin URL.  Ignore any
            # accidental landing-page path instead of producing a bad nested
            # endpoint such as ``/base/api/helper/ws``.
            path = "/api/helper/ws"
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key != "device_id"]
        query.append(("device_id", self.device_id))
        scheme = {"https": "wss", "http": "ws"}.get(parsed.scheme, parsed.scheme)
        url = urllib.parse.urlunsplit(
            (scheme, parsed.netloc, path, urllib.parse.urlencode(query), "")
        )
        return websocket.create_connection(
            url,
            timeout=10,
            header=[f"Authorization: Bearer {self.pairing_token}"],
        )

    def http_json(self, path: str, payload: dict[str, Any], *, authorized: bool = True) -> dict[str, Any]:
        """Send one outbound HTTPS helper request (Cloudflare-compatible)."""
        base = self.server_url.replace("wss://", "https://").replace("ws://", "http://")
        url = base.rstrip("/") + "/" + path.lstrip("/")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {self.pairing_token}"
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f"辅助服务连接失败：{exc}") from exc
        return value if isinstance(value, dict) else {"ok": False, "error": "响应格式无效"}
