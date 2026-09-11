from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .interface_catalog import build_interface_catalog


SCENARIOS: list[dict[str, str]] = [
    {"id": "healthy", "label": "恢复正常"},
    {"id": "not_found", "label": "接口未发现"},
    {"id": "open_failed", "label": "接口无法打开"},
    {"id": "timeout", "label": "通信超时/无数据"},
    {"id": "stale", "label": "数据长时间未更新"},
    {"id": "invalid_data", "label": "收到非法数值"},
    {"id": "partial_channels", "label": "部分通道缺失"},
    {"id": "parse_error", "label": "M3232矩阵帧解析失败"},
]

_SCENARIO_TEXT: dict[str, tuple[str, str, str]] = {
    "not_found": ("not_found", "critical", "系统未发现接口"),
    "open_failed": ("open_failed", "critical", "接口存在但无法打开"),
    "timeout": ("timeout", "warning", "接口已打开，但持续未收到数据"),
    "stale": ("stale", "warning", "最近数据已超过允许时延"),
    "invalid_data": ("invalid_data", "warning", "收到非数值或越界数据"),
    "partial_channels": ("partial_channels", "warning", "接口仅返回部分已配置通道"),
    "parse_error": ("invalid_data", "warning", "M3232矩阵帧无法解析"),
}


def _healthy_state(interface: dict[str, object]) -> dict[str, object]:
    return {
        **deepcopy(interface),
        "state": "healthy",
        "severity": "normal",
        "status_label": "运行正常",
        "summary": "接口已识别，全部配置通道正在更新",
        "received_samples": 128,
        "invalid_samples": 0,
        "last_sample_age_ms": 86,
        "missing_channels": [],
        "simulated": True,
    }


def _scenario_state(
    interface: dict[str, object], scenario: str, event_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    if scenario == "parse_error" and interface["id"] != "m3232_pressure":
        raise ValueError("M3232矩阵帧解析场景只适用于薄膜压力接口")
    if scenario not in _SCENARIO_TEXT:
        raise ValueError(f"未知故障场景：{scenario}")

    state_name, severity, summary = _SCENARIO_TEXT[scenario]
    channels = list(interface["channels"])
    missing_channels: list[str] = []
    received_samples = 0
    invalid_samples = 0
    age_ms: int | None = None

    if scenario == "partial_channels":
        missing_channels = channels[-2:] if len(channels) >= 8 else channels[-1:]
        received_samples = 73
        age_ms = 118
    elif scenario == "invalid_data":
        invalid_samples = 6
        age_ms = 94
    elif scenario == "parse_error":
        invalid_samples = 3
        age_ms = 420
    elif scenario == "stale":
        received_samples = 64
        age_ms = 4280
    elif scenario == "timeout":
        age_ms = 3500

    evidence: dict[str, Any] = {
        "endpoint": interface["endpoint"],
        "driver": interface["driver"],
        "received_samples": received_samples,
        "invalid_samples": invalid_samples,
        "last_sample_age_ms": age_ms,
        "missing_channels": missing_channels,
    }
    if "baudrate" in interface:
        evidence["baudrate"] = interface["baudrate"]

    state = {
        **deepcopy(interface),
        "state": state_name,
        "severity": severity,
        "status_label": "严重异常" if severity == "critical" else "需要检查",
        "summary": summary,
        "received_samples": received_samples,
        "invalid_samples": invalid_samples,
        "last_sample_age_ms": age_ms,
        "missing_channels": missing_channels,
        "simulated": True,
    }
    event = {
        "event_id": event_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "interface_id": interface["id"],
        "sensor_name": interface["sensor_name"],
        "channels": channels,
        "state": state_name,
        "severity": severity,
        "summary": summary,
        "evidence": evidence,
        "simulated": True,
    }
    return state, event


class InterfaceMonitor:
    def __init__(self, catalog: list[dict[str, object]] | None = None) -> None:
        self.catalog = deepcopy(catalog) if catalog is not None else build_interface_catalog()
        self._by_id = {str(item["id"]): item for item in self.catalog}
        self._states = {
            interface_id: _healthy_state(item)
            for interface_id, item in self._by_id.items()
        }
        self._events: dict[str, dict[str, object]] = {}
        self._event_counter = 0

    def snapshot(self) -> dict[str, object]:
        last_event_id = next(reversed(self._events), None)
        return deepcopy(
            {
                "interfaces": list(self._states.values()),
                "events": list(self._events.values()),
                "active_event": self._events.get(last_event_id),
                "simulated": True,
            }
        )

    def get_event(self, event_id: str) -> dict[str, object] | None:
        event = self._events.get(event_id)
        return deepcopy(event) if event is not None else None

    def apply_scenario(self, interface_id: str, scenario: str) -> dict[str, object]:
        interface = self._by_id.get(interface_id)
        if interface is None:
            raise ValueError(f"未知接口：{interface_id}")
        if scenario == "healthy":
            self._states[interface_id] = _healthy_state(interface)
            self._events = {
                key: value
                for key, value in self._events.items()
                if value["interface_id"] != interface_id
            }
            return deepcopy(self._states[interface_id])
        if scenario not in {item["id"] for item in SCENARIOS}:
            raise ValueError(f"未知故障场景：{scenario}")

        self._event_counter += 1
        event_id = f"evt-{self._event_counter:04d}"
        state, event = _scenario_state(interface, scenario, event_id)
        self._states[interface_id] = state
        self._events = {
            key: value
            for key, value in self._events.items()
            if value["interface_id"] != interface_id
        }
        self._events[event_id] = event
        return deepcopy(event)

    def reset(self) -> dict[str, object]:
        self._states = {
            interface_id: _healthy_state(item)
            for interface_id, item in self._by_id.items()
        }
        self._events.clear()
        return self.snapshot()
