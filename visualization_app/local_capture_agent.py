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


_ROLE_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("thermocouple_8ch", "usb_hid", ("smrf_hid",)),
    ("plc_process", "ethernet", ("ethernet",)),
    ("uvc_temperature", "usb_uvc", ("uvc",)),
    ("abb_motion", "ethernet", ("ethernet",)),
    ("m3232_pressure", "serial", ("serial",)),
)


class LocalCaptureAgent:
    """Expose local discovery, capture, and MySQL operations to a relay."""

    def __init__(self, manager: AcquisitionManager | None = None) -> None:
        self.manager = manager or AcquisitionManager()
        self._stream_lock = threading.RLock()
        self._capture_uuid = ""
        self._stream_cursor = 0
        self._stream_sequence = 0
        self._pending_batch: dict[str, Any] | None = None
        self._status_revision = 0
        self._pending_status_revision = -1
        self._status_dirty = False

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
            and (allow_used or str(item.get("id") or "") not in used)
        ]
        # A driver placeholder is a valid protocol binding, but is clearly
        # marked as not yet sensor-verified in the returned state.
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
                used.add(physical_id)
            detected = bool(candidate and candidate.get("detected"))
            driver_available = bool(candidate and candidate.get("driver_available"))
            bindings.append(
                {
                    "role": role,
                    "physical_interface_id": physical_id or None,
                    "physical_kind": kind,
                    "protocol": protocols[0],
                    "interface_detected": detected,
                    "driver_available": driver_available,
                    "sensor_data_state": "unknown",
                    "acquisition_state": "not_started",
                    "state": (
                        "interface_detected"
                        if detected
                        else "driver_available"
                        if driver_available
                        else "unavailable"
                    ),
                    "message": (
                        "接口已识别，传感器数据待采集验证"
                        if detected
                        else "驱动或协议已配置，传感器数据待采集验证"
                        if driver_available
                        else "未找到兼容的实际接口"
                    ),
                }
            )
        return {
            "interfaces": physical,
            "sensor_bindings": bindings,
            "capabilities": {
                "hardware_discovery": True,
                "real_capture": True,
                "process_parameter_read": True,
                "local_csv_save": True,
                "local_mysql_save": True,
            },
            "warnings": [discovered["error"]] if discovered.get("error") else [],
            "raw_discovery": discovered,
        }

    def mysql_preflight(
        self, settings: MySQLSettings, *, write_test: bool = False
    ) -> dict[str, Any]:
        result = MySQLCaptureStore(settings).preflight(write_test=write_test)
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
            capture_uuid = self._capture_uuid
        response = dict(result) if isinstance(result, dict) else {"result": result}
        response["capture_uuid"] = capture_uuid
        return response

    def next_sample_batch(self, *, limit: int = 20) -> dict[str, Any] | None:
        """Return one replayable batch without advancing before acknowledgement."""

        batch_limit = max(1, min(int(limit), 200))
        with self._stream_lock:
            if self._pending_batch is not None:
                return dict(self._pending_batch)
            if not self._capture_uuid:
                return None
            rows, timestamps = self.manager.numeric_matrix()
            if self._stream_cursor > len(rows):
                self._stream_cursor = 0
                self._stream_sequence = 0
            end = min(len(rows), self._stream_cursor + batch_limit)
            if end <= self._stream_cursor and not self._status_dirty:
                return None
            pending = {
                "capture_uuid": self._capture_uuid,
                "sequence": self._stream_sequence,
                "rows": [json_safe_value(dict(row)) for row in rows[self._stream_cursor:end]],
                "timestamps": [float(value) for value in timestamps[self._stream_cursor:end]],
                "status": json_safe_value(self.manager.status()),
                "transport": {
                    "helper_batch_created_at": time.time(),
                    "helper_queue_depth": max(0, len(rows) - end),
                },
            }
            self._pending_batch = pending
            self._pending_status_revision = self._status_revision
            return dict(pending)

    def ack_sample_batch(self, capture_uuid: str, sequence: int) -> bool:
        with self._stream_lock:
            pending = self._pending_batch
            if pending is None:
                return False
            if str(capture_uuid) != str(pending["capture_uuid"]):
                return False
            if int(sequence) != int(pending["sequence"]):
                return False
            self._stream_cursor += len(pending["rows"])
            self._stream_sequence += 1
            self._pending_batch = None
            if self._pending_status_revision == self._status_revision:
                self._status_dirty = False
            self._pending_status_revision = -1
            return True

    def stream_metrics(self) -> dict[str, Any]:
        """Return safe transport counters without exposing samples or credentials."""

        with self._stream_lock:
            rows, _timestamps = self.manager.numeric_matrix()
            status = self.manager.status()
            config = status.get("config") if isinstance(status, dict) else {}
            try:
                sample_rate = float((config or {}).get("sample_rate") or 10.0)
            except (TypeError, ValueError):
                sample_rate = 10.0
            return {
                "capture_uuid": self._capture_uuid,
                "queued_rows": max(0, len(rows) - self._stream_cursor),
                "pending_sequence": (
                    int(self._pending_batch["sequence"])
                    if isinstance(self._pending_batch, dict)
                    else None
                ),
                "next_sequence": self._stream_sequence,
                "sample_rate_hz": max(0.1, sample_rate),
            }

    def check_capture(self, config: Any) -> dict[str, Any]:
        return self.manager.test_connection(config)

    def read_process_parameters(self, config: Any) -> dict[str, Any]:
        return self.manager.read_process_parameters(config)

    def stop_capture(self) -> dict[str, Any]:
        result = self.manager.stop()
        with self._stream_lock:
            self._status_revision += 1
            self._status_dirty = True
            capture_uuid = self._capture_uuid
        response = dict(result) if isinstance(result, dict) else {"result": result}
        response["capture_uuid"] = capture_uuid
        return response

    def status(self) -> dict[str, Any]:
        return self.manager.status()


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
