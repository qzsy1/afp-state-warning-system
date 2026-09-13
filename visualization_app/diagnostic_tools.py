"""Read-only evidence tools for AFP interface diagnosis.

This module treats operating-system interfaces as candidates only.  A target
sensor is confirmed exclusively by protocol-valid samples from the hardware
check that started the current diagnostic case.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


TARGET_INTERFACE_COUNT = 5
TARGET_CHANNEL_COUNT = 17


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
