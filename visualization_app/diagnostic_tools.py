"""Read-only evidence tools for AFP interface diagnosis.

This module treats operating-system interfaces as candidates only.  A target
sensor is confirmed exclusively by protocol-valid samples from the hardware
check that started the current diagnostic case.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from statistics import fmean
from typing import Any


TARGET_INTERFACE_COUNT = 5
TARGET_CHANNEL_COUNT = 17


class DiagnosticToolError(ValueError):
    """Raised when a model requests a tool or target outside the allowlist."""


@dataclass
class DiagnosticToolContext:
    """Sanitized snapshots available to read-only diagnostic tools."""

    events: tuple[dict[str, Any], ...]
    hardware_result: dict[str, Any]
    discovery: dict[str, Any]
    acquisition_status: dict[str, Any]
    evidence_sequence: int = 0

    def __post_init__(self) -> None:
        self.events = tuple(deepcopy(item) for item in self.events if isinstance(item, dict))
        self.hardware_result = deepcopy(self.hardware_result or {})
        self.discovery = deepcopy(self.discovery or {})
        self.acquisition_status = deepcopy(self.acquisition_status or {})

    def next_evidence_id(self, _tool_name: str) -> str:
        self.evidence_sequence += 1
        return f"EV-{self.evidence_sequence:03d}"

    @property
    def interface_ids(self) -> set[str]:
        return {
            str(item.get("interface_id") or item.get("id") or "")
            for item in self.events
            if str(item.get("interface_id") or item.get("id") or "")
        }

    @property
    def channel_names(self) -> set[str]:
        names: set[str] = set()
        for event in self.events:
            names.update(str(value) for value in (event.get("channels") or []) if str(value))
        return names


def normalize_no_sensor_states(hardware_result: dict[str, Any]) -> dict[str, Any]:
    """Normalize placeholders without upgrading them to connected sensors."""

    result = deepcopy(hardware_result or {})
    interfaces = result.get("interfaces") if isinstance(result.get("interfaces"), list) else []
    sensors = result.get("sensors") if isinstance(result.get("sensors"), list) else []
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        valid_count = sum(
            max(0, int(value or 0))
            for value in (interface.get("sample_counts") or {}).values()
        )
        if valid_count > 0:
            continue
        physical_id = str(interface.get("physical_interface_id") or "").lower()
        placeholder = physical_id.endswith(":auto") or bool(interface.get("physical_fallback"))
        if placeholder:
            interface["state"] = "identity_unconfirmed"
            interface["ok"] = False

    interface_ok = sum(bool(item.get("ok")) for item in interfaces if isinstance(item, dict))
    channel_ok = sum(
        str(item.get("state")) == "ok" for item in sensors if isinstance(item, dict)
    )
    result["summary"] = (
        f"检查未通过：{interface_ok}/{TARGET_INTERFACE_COUNT} 个目标传感器接口已由有效数据确认，"
        f"{channel_ok}/{TARGET_CHANNEL_COUNT} 个通道正常"
    )
    return result


def _function(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def tool_definitions() -> list[dict[str, Any]]:
    """Return the exact read-only tools exposed to an online model."""

    interface_id = {"type": "string", "description": "当前诊断任务中的接口ID"}
    channel_names = {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": TARGET_CHANNEL_COUNT,
    }
    window = {"type": "number", "minimum": 1, "maximum": 120}
    return [
        _function(
            "inspect_interface_mapping",
            "读取目标传感器、协议和候选物理接口映射；不打开硬件",
            {"interface_id": interface_id},
            ["interface_id"],
        ),
        _function(
            "recheck_interface",
            "读取本次用户主动硬件检查中指定接口的限时探测结果；不会再次打开端口",
            {
                "interface_id": interface_id,
                "duration_seconds": {"type": "number", "minimum": 0.1, "maximum": 3},
            },
            ["interface_id", "duration_seconds"],
        ),
        _function(
            "get_recent_channel_stats",
            "读取指定通道的有效、无效、缺失和近期数值统计",
            {"channel_names": channel_names, "window_seconds": window},
            ["channel_names", "window_seconds"],
        ),
        _function(
            "get_cross_interface_timeline",
            "比较多个接口在当前诊断任务中的异常状态和共同物理链路",
            {
                "interface_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": TARGET_INTERFACE_COUNT,
                },
                "window_seconds": window,
            },
            ["interface_ids", "window_seconds"],
        ),
        _function(
            "check_network_path",
            "读取已配置PLC或ABB端点、共用网卡和可达性；不扫描其他地址",
            {"interface_id": interface_id},
            ["interface_id"],
        ),
        _function(
            "inspect_protocol_frame",
            "读取当前探测已保存的有限协议帧摘要；没有帧时明确返回不可用",
            {"interface_id": interface_id},
            ["interface_id"],
        ),
        _function(
            "lookup_device_knowledge",
            "查询软件内置的设备协议边界和安全排查建议",
            {
                "device_type": {"type": "string"},
                "symptom": {"type": "string", "maxLength": 200},
            },
            ["device_type", "symptom"],
        ),
        _function(
            "compare_related_signals",
            "对当前缓存中的相关通道进行确定性同步和变化比较",
            {"channel_names": channel_names, "window_seconds": window},
            ["channel_names", "window_seconds"],
        ),
    ]


def _event(context: DiagnosticToolContext, interface_id: str) -> dict[str, Any]:
    for item in context.events:
        if str(item.get("interface_id") or item.get("id")) == interface_id:
            return item
    raise DiagnosticToolError(f"未知接口：{interface_id}")


def _hardware_interface(context: DiagnosticToolContext, interface_id: str) -> dict[str, Any]:
    for item in context.hardware_result.get("interfaces") or []:
        if isinstance(item, dict) and str(item.get("id")) == interface_id:
            return item
    return {}


def _physical_candidate(context: DiagnosticToolContext, physical_id: str) -> dict[str, Any]:
    for item in context.discovery.get("physical_interfaces") or []:
        if isinstance(item, dict) and str(item.get("id")) == physical_id:
            return item
    return {}


def _number(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _validate_window(arguments: dict[str, Any]) -> float:
    window = _number(arguments.get("window_seconds"), 30.0)
    if window < 1 or window > 120:
        raise DiagnosticToolError("诊断统计窗口必须在 1 到 120 秒之间")
    return window


def _validate_channels(context: DiagnosticToolContext, values: Any) -> list[str]:
    if not isinstance(values, list) or not values:
        raise DiagnosticToolError("至少选择一个诊断通道")
    names = [str(value) for value in values]
    unknown = [name for name in names if name not in context.channel_names]
    if unknown:
        raise DiagnosticToolError(f"未知通道：{'、'.join(unknown)}")
    return list(dict.fromkeys(names))


def _recent_rows(context: DiagnosticToolContext, window_seconds: float) -> list[dict[str, Any]]:
    rows = context.acquisition_status.get("recent_samples")
    if not isinstance(rows, list):
        rows = context.hardware_result.get("recent_samples")
    if not isinstance(rows, list):
        return []
    limit = max(1, min(len(rows), int(window_seconds * 100)))
    return [item for item in rows[-limit:] if isinstance(item, dict)]


def _inspect_interface_mapping(context: DiagnosticToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    interface_id = str(arguments["interface_id"])
    event = _event(context, interface_id)
    hardware = _hardware_interface(context, interface_id)
    physical_id = str(event.get("physical_interface_id") or hardware.get("physical_interface_id") or "")
    physical = _physical_candidate(context, physical_id)
    sample_count = sum(
        max(0, int(value or 0)) for value in (hardware.get("sample_counts") or {}).values()
    )
    return {
        "interface_id": interface_id,
        "role": event.get("role"),
        "driver": event.get("driver"),
        "protocol": event.get("protocol"),
        "configured_endpoint": event.get("endpoint"),
        "physical_interface_id": physical_id,
        "physical_kind": event.get("physical_interface_kind"),
        "candidate_detected": bool(physical.get("detected", False)),
        "placeholder_binding": bool(event.get("physical_fallback"))
        or physical_id.lower().endswith(":auto")
        or bool(physical.get("auto_assignable")),
        "identity_verified": bool(hardware.get("physical_verified")) and sample_count > 0,
        "valid_sample_count": sample_count,
        "warning": event.get("physical_warning") or hardware.get("physical_warning") or "",
    }


def _recheck_interface(context: DiagnosticToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    interface_id = str(arguments["interface_id"])
    event = _event(context, interface_id)
    hardware = _hardware_interface(context, interface_id)
    return {
        "interface_id": interface_id,
        "probe_source": "current_user_triggered_hardware_check",
        "new_hardware_scan_started": False,
        "requested_duration_seconds": arguments["duration_seconds"],
        "state": hardware.get("state") or event.get("state"),
        "message": hardware.get("message") or event.get("message"),
        "expected_channels": list(hardware.get("expected_channels") or event.get("channels") or []),
        "detected_channels": list(hardware.get("detected_channels") or []),
        "missing_channels": list(hardware.get("missing_channels") or event.get("channels") or []),
        "invalid_channels": list(hardware.get("invalid_channels") or []),
        "sample_counts": deepcopy(hardware.get("sample_counts") or {}),
        "errors": [str(value)[:500] for value in (hardware.get("errors") or [])[:5]],
    }


def _get_recent_channel_stats(context: DiagnosticToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    names = arguments["channel_names"]
    rows = _recent_rows(context, arguments["window_seconds"])
    sensor_by_name = {
        str(item.get("name")): item
        for item in (context.hardware_result.get("sensors") or [])
        if isinstance(item, dict)
    }
    stats = []
    for name in names:
        values = []
        for row in rows:
            value = _number(row.get(name), math.nan)
            if math.isfinite(value):
                values.append(value)
        sensor = sensor_by_name.get(name, {})
        stats.append(
            {
                "channel": name,
                "state": sensor.get("state", "unknown"),
                "received_samples": int(sensor.get("received_samples") or len(values)),
                "invalid_samples": int(sensor.get("invalid_samples") or 0),
                "recent_value_count": len(values),
                "minimum": min(values) if values else None,
                "maximum": max(values) if values else None,
                "mean": fmean(values) if values else None,
                "change": values[-1] - values[0] if len(values) >= 2 else None,
            }
        )
    return {
        "window_seconds": arguments["window_seconds"],
        "channels": stats,
        "insufficient_data": not any(item["recent_value_count"] for item in stats),
    }


def _get_cross_interface_timeline(context: DiagnosticToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    events = [_event(context, interface_id) for interface_id in arguments["interface_ids"]]
    physical_ids = [str(item.get("physical_interface_id") or "") for item in events]
    shared = physical_ids[0] if physical_ids and all(value == physical_ids[0] for value in physical_ids) else ""
    abnormal = [
        item
        for item in events
        if str(item.get("state")) not in {"ok", "healthy", "disabled", "video_only"}
    ]
    return {
        "window_seconds": arguments["window_seconds"],
        "interfaces": [
            {
                "interface_id": item.get("interface_id"),
                "state": item.get("state"),
                "message": item.get("message"),
            }
            for item in events
        ],
        "same_case_abnormal": len(abnormal) == len(events),
        "shared_physical_interface_id": shared or None,
        "temporal_order_available": False,
        "boundary": "同一检查任务不等同于已证明在同一瞬间失效",
    }


def _check_network_path(context: DiagnosticToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    interface_id = str(arguments["interface_id"])
    event = _event(context, interface_id)
    physical_id = str(event.get("physical_interface_id") or "")
    physical = _physical_candidate(context, physical_id)
    reachable_key = "plc_reachable" if interface_id == "plc_process" else "abb_reachable"
    shared = [
        str(item.get("interface_id"))
        for item in context.events
        if str(item.get("physical_interface_id") or "") == physical_id
        and str(item.get("interface_id")) != interface_id
    ]
    return {
        "interface_id": interface_id,
        "configured_endpoint": event.get("endpoint"),
        "physical_interface_id": physical_id,
        "adapter_detected": bool(physical.get("detected", False)),
        "adapter_addresses": list(physical.get("addresses") or []),
        "endpoint_reachable": bool(context.discovery.get(reachable_key, False)),
        "shared_with_interfaces": shared,
        "arbitrary_scan_performed": False,
    }


def _bounded_protocol_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_bounded_protocol_value(item) for item in value[:16]]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _bounded_protocol_value(item)
            for key, item in list(value.items())[:16]
        }
    return str(value)[:256]


def _inspect_protocol_frame(context: DiagnosticToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    interface_id = str(arguments["interface_id"])
    _event(context, interface_id)
    hardware = _hardware_interface(context, interface_id)
    source = hardware.get("protocol_evidence") or hardware.get("raw_frame_summary")
    return {
        "interface_id": interface_id,
        "evidence_available": isinstance(source, dict) and bool(source),
        "frame_summary": _bounded_protocol_value(source) if isinstance(source, dict) else {},
        "boundary": (
            "仅返回当前检查已保存的协议摘要"
            if isinstance(source, dict) and source
            else "当前检查未捕获可供分析的协议帧"
        ),
    }


_DEVICE_KNOWLEDGE: dict[str, dict[str, Any]] = {
    "thermocouple_8ch": {
        "facts": ["目标设备应通过 SMRF HID 身份和有效通道帧确认"],
        "checks": ["核对SMRF设备供电和USB连接", "核对设备身份后再判断温度通道"],
    },
    "plc_process": {
        "facts": ["PLC与ABB可以共用同一电脑网卡，但使用不同目标协议端点"],
        "checks": ["核对工控网卡地址和子网", "检查192.168.125.5:502的Modbus响应"],
    },
    "uvc_temperature": {
        "facts": ["发现厂商DLL不等于已发现热像设备或有效温度帧"],
        "checks": ["核对BSV设备身份", "检查温度帧尺寸与ROI换算"],
    },
    "abb_motion": {
        "facts": ["ABB RWS与PLC共享物理网卡但使用独立端点"],
        "checks": ["核对工控网卡地址和子网", "检查192.168.125.1的RWS响应"],
    },
    "m3232_pressure": {
        "facts": ["普通COM口存在不等于M3232身份或协议已确认"],
        "checks": ["核对实际串口和通信参数", "捕获真实M3232帧后核对矩阵包装与尺寸"],
    },
}


def _lookup_device_knowledge(context: DiagnosticToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    device_type = str(arguments["device_type"])
    _event(context, device_type)
    entry = _DEVICE_KNOWLEDGE.get(device_type, {"facts": [], "checks": []})
    return {
        "device_type": device_type,
        "symptom": str(arguments.get("symptom") or "")[:200],
        "confirmed_protocol_facts": list(entry["facts"]),
        "safe_checks": list(entry["checks"]),
        "source": "software_bundled_device_knowledge",
    }


def _compare_related_signals(context: DiagnosticToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    names = arguments["channel_names"]
    rows = _recent_rows(context, arguments["window_seconds"])
    series: dict[str, list[float]] = {name: [] for name in names}
    for row in rows:
        for name in names:
            value = _number(row.get(name), math.nan)
            if math.isfinite(value):
                series[name].append(value)
    summaries = {
        name: {
            "valid_values": len(values),
            "change": values[-1] - values[0] if len(values) >= 2 else None,
        }
        for name, values in series.items()
    }
    return {
        "window_seconds": arguments["window_seconds"],
        "signals": summaries,
        "insufficient_data": any(len(values) < 2 for values in series.values()),
        "boundary": "仅比较缓存数据，不推断因果关系",
    }


_ALLOWED_TOOLS = {
    "inspect_interface_mapping": _inspect_interface_mapping,
    "recheck_interface": _recheck_interface,
    "get_recent_channel_stats": _get_recent_channel_stats,
    "get_cross_interface_timeline": _get_cross_interface_timeline,
    "check_network_path": _check_network_path,
    "inspect_protocol_frame": _inspect_protocol_frame,
    "lookup_device_knowledge": _lookup_device_knowledge,
    "compare_related_signals": _compare_related_signals,
}


def _validate_arguments(
    context: DiagnosticToolContext, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise DiagnosticToolError("诊断工具参数必须是JSON对象")
    clean = deepcopy(arguments)
    if "interface_id" in clean:
        interface_id = str(clean["interface_id"])
        _event(context, interface_id)
        clean["interface_id"] = interface_id
    if name == "recheck_interface":
        duration = _number(clean.get("duration_seconds"), 1.0)
        if duration < 0.1 or duration > 3:
            raise DiagnosticToolError("单次接口复查不能超过 3 秒")
        clean["duration_seconds"] = duration
    if name in {
        "get_recent_channel_stats",
        "get_cross_interface_timeline",
        "compare_related_signals",
    }:
        clean["window_seconds"] = _validate_window(clean)
    if name in {"get_recent_channel_stats", "compare_related_signals"}:
        clean["channel_names"] = _validate_channels(context, clean.get("channel_names"))
    if name == "get_cross_interface_timeline":
        interface_ids = clean.get("interface_ids")
        if not isinstance(interface_ids, list) or len(interface_ids) < 2:
            raise DiagnosticToolError("跨接口检查至少需要两个接口")
        clean["interface_ids"] = [str(value) for value in interface_ids]
        for interface_id in clean["interface_ids"]:
            _event(context, interface_id)
    if name == "check_network_path":
        event = _event(context, clean["interface_id"])
        if str(event.get("physical_interface_kind")) != "ethernet" or clean["interface_id"] not in {
            "plc_process",
            "abb_motion",
        }:
            raise DiagnosticToolError("该工具只允许检查已配置的PLC或ABB网络接口")
    if name == "lookup_device_knowledge":
        device_type = str(clean.get("device_type") or "")
        if device_type not in context.interface_ids:
            raise DiagnosticToolError(f"未知接口：{device_type}")
        clean["device_type"] = device_type
        clean["symptom"] = str(clean.get("symptom") or "")[:200]
    return clean


def execute_tool(
    context: DiagnosticToolContext, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Validate and execute one model-selected read-only tool."""

    if name not in _ALLOWED_TOOLS:
        raise DiagnosticToolError(f"不允许的诊断工具：{name}")
    clean_arguments = _validate_arguments(context, name, arguments)
    payload = _ALLOWED_TOOLS[name](context, clean_arguments)
    return {
        "evidence_id": context.next_evidence_id(name),
        "tool": name,
        **payload,
    }
