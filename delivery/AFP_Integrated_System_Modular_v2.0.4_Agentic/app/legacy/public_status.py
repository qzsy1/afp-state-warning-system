from __future__ import annotations

import math
import time
from typing import Any


PUBLIC_INTERFACE_DEFINITIONS = (
    {
        "role": "thermocouple",
        "label": "SMRF八通道热电偶",
        "driver": "smrf_hid",
        "protocol": "USB HID",
    },
    {
        "role": "plc",
        "label": "松下PLC",
        "driver": "modbus_tcp",
        "protocol": "Modbus TCP",
    },
    {
        "role": "thermal_uvc",
        "label": "BSV UVC热像仪",
        "driver": "uvc_thermal",
        "protocol": "USB UVC",
    },
    {
        "role": "robot",
        "label": "ABB机器人",
        "driver": "abb_robot",
        "protocol": "RWS HTTP",
    },
    {
        "role": "pressure",
        "label": "M3232薄膜压力传感器",
        "driver": "m3232_pressure",
        "protocol": "串口",
    },
)


_PUBLIC_STATE_MESSAGES = {
    "ok": "接口在线并已收到有效数据",
    "not_connected": "接口无法打开或设备未连接",
    "no_data": "接口已打开但未检测到数据",
    "invalid_data": "接口收到的数据格式或数值无效",
    "disabled": "接口当前未启用",
    "video_only": "接口仅提供视频状态",
    "no_channels": "接口尚未分配采集通道",
    "waiting": "正在等待设备数据",
    "stale": "设备数据已中断",
    "unchecked": "尚未执行真实接口检查",
}


def _finite_time(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def build_public_device_status(
    discovery: dict[str, Any] | None,
    check_result: dict[str, Any] | None,
    acquisition_status: dict[str, Any] | None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Project cached hardware state without returning physical identifiers."""

    del discovery  # Enumeration details are intentionally not public.
    check = dict(check_result or {})
    acquisition = dict(acquisition_status or {})
    checked_at = _finite_time(check.get("checked_at"))
    current = time.time() if now is None else float(now)
    age = None if checked_at is None else round(max(0.0, current - checked_at), 3)
    source_by_role: dict[str, dict[str, Any]] = {}
    for item in check.get("interfaces") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role in {definition["role"] for definition in PUBLIC_INTERFACE_DEFINITIONS}:
            source_by_role[role] = item

    interfaces: list[dict[str, Any]] = []
    for definition in PUBLIC_INTERFACE_DEFINITIONS:
        source = source_by_role.get(definition["role"], {})
        state = str(source.get("state") or "unchecked").strip().lower()
        if state not in _PUBLIC_STATE_MESSAGES:
            state = "unchecked"
        interfaces.append(
            {
                **definition,
                "state": state,
                "ok": bool(source.get("ok", False)) if source else False,
                "message_code": state,
                "message": _PUBLIC_STATE_MESSAGES[state],
                "checked_at": checked_at,
                "age_seconds": age,
            }
        )

    return {
        "source": "cached_hardware_check",
        "checked_at": checked_at,
        "age_seconds": age,
        "real_acquisition_running": bool(acquisition.get("running", False)),
        "interfaces": interfaces,
    }
