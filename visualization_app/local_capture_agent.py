"""Local computer adapter used by the web capture helper.

The agent deliberately stays thin: protocol readers, channel normalization,
capture timing, and persistence remain in the existing acquisition modules.
This module only adds a stable local-helper contract and deterministic mapping
from the five logical AFP sensors to discovered physical interfaces.
"""

from __future__ import annotations

import json
from typing import Any

from acquisition import AcquisitionManager, MySQLSettings
from helper_relay import ALLOWED_HELPER_COMMANDS
from mysql_storage import MySQLCaptureStore


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
                "local_csv_save": True,
                "local_mysql_save": True,
            },
            "warnings": [discovered["error"]] if discovered.get("error") else [],
            "raw_discovery": discovered,
        }

    def mysql_preflight(
        self, settings: MySQLSettings, *, write_test: bool = False
    ) -> dict[str, Any]:
        return MySQLCaptureStore(settings).preflight(write_test=write_test)

    def start_capture(self, config: Any) -> dict[str, Any]:
        return self.manager.start(config)

    def stop_capture(self) -> dict[str, Any]:
        return self.manager.stop()

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
        if not self.server_url.startswith("wss://"):
            raise ValueError("本地辅助程序只允许使用wss://服务地址")
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise RuntimeError("缺少websocket-client依赖，无法连接公网辅助服务") from exc
        return websocket.create_connection(
            self.server_url,
            timeout=10,
            header=[f"Authorization: Bearer {self.pairing_token}"],
        )
