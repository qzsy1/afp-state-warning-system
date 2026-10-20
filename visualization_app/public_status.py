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
    {"role": "esr1001_strain", "label": "ESR1001-A四通道应变", "driver": "", "protocol": "USB TTL / RS485"},
)


_ESR1001_CHECKED_PROTOCOLS = {
    "esr1001_usb_ttl": ({"esr1001_usb_ttl"}, "esr1001_usb_ttl"),
    "modbus_rtu_esr1001": ({"modbus_rtu", "modbus_rtu_esr1001"}, "modbus_rtu"),
}


_PUBLIC_STATE_MESSAGES = {
    "data_ready": "接口数据就绪，请核对采集单位及标定状态",
    "usb_no_valid_frame": "USB未取得有效数据帧",
    "port_access_denied": "串口访问被拒绝，请关闭占用程序或检查权限",
    "port_busy": "串口被其他采集会话占用",
    "usb_overflow": "USB缓冲或保存过载",
    "usb_parameters_invalid": "USB参数应答无效",
    "calibration_missing": "原始通信可用，标定待确认",
    "calibration_mismatch": "标定与设备条件不匹配",
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
    if acquisition.get("running"):
        for item in acquisition.get("interfaces") or []:
            if isinstance(item, dict) and item.get("role"):
                source_by_role[str(item["role"])] = item
    for definition in PUBLIC_INTERFACE_DEFINITIONS:
        source = source_by_role.get(definition["role"], {})
        state = str(source.get("state") or "unchecked").strip().lower()
        driver = definition["driver"]
        protocol = definition["protocol"]
        if definition["role"] == "esr1001_strain":
            checked_driver = str(source.get("driver") or "").strip().lower()
            protocol_contract = _ESR1001_CHECKED_PROTOCOLS.get(checked_driver)
            if protocol_contract is not None:
                allowed_protocols, default_protocol = protocol_contract
                checked_protocol = str(source.get("protocol") or "").strip().lower()
                driver = checked_driver
                protocol = (
                    checked_protocol
                    if checked_protocol in allowed_protocols
                    else default_protocol
                )
        message = _PUBLIC_STATE_MESSAGES.get(
            state,
            f"接口检查失败（状态码：{state}）",
        )
        stages = source.get("stages") if isinstance(source.get("stages"), dict) else {}
        interfaces.append(
            {
                **definition,
                "driver": driver,
                "protocol": protocol,
                "state": state,
                "ok": bool(source.get("ok", False)) if source else False,
                "message_code": state,
                "message": message,
                "stages": [
                    {"name": str(name), "state": str(stage.get("state") or "unknown")}
                    for name, stage in stages.items()
                    if isinstance(stage, dict)
                ],
                "checked_at": checked_at,
                "age_seconds": age,
                "data_mode": str((source.get("usb_evidence") or {}).get("data_mode") or ""),
                "unit": str((source.get("usb_evidence") or {}).get("unit") or ""),
                "calibration_state": str((source.get("usb_evidence") or {}).get("calibration_state") or ""),
            }
        )

    return {
        "source": "cached_hardware_check",
        "checked_at": checked_at,
        "age_seconds": age,
        "real_acquisition_running": bool(acquisition.get("running", False)),
        "interfaces": interfaces,
    }
