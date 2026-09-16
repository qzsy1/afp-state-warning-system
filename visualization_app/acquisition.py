from __future__ import annotations

import base64
import csv
import ctypes
import hashlib
import io
import json
import math
import os
import re
import socket
import struct
import threading
import time
import tempfile
import sys
import urllib.request
import urllib.parse
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from mysql_storage import MySQLCaptureStore, MySQLSettings, validate_database_name
from smrf_hid import SmrfHidDriver, enumerate_smrf_hid_devices


APP_DIR = Path(os.environ.get("AFP_LEGACY_APP_DIR") or Path(__file__).resolve().parent).resolve()
WORKSPACE_DIR = APP_DIR.parents[1]
DEFAULT_CAPTURE_ROOT = Path(WORKSPACE_DIR.anchor) / "AFP_Capture"
_DATA_DIR = Path(os.environ.get("AFP_DATA_DIR") or APP_DIR / "data").resolve()
_NATIVE_DLL_DIR = Path(os.environ.get("AFP_NATIVE_DLL_DIR") or APP_DIR).resolve()
_PACKAGED_SIMULATOR_FILE = _DATA_DIR / "原文件.csv"
DEFAULT_SIMULATOR_FILE = (
    _PACKAGED_SIMULATOR_FILE
    if _PACKAGED_SIMULATOR_FILE.exists()
    else WORKSPACE_DIR / "原文件.csv"
)

LEGACY_SENSOR_COLUMNS = [
    "转速",
    "位移",
    "温度1",
    "温度2",
    "温度3",
    "温度4",
    "温度5",
    "温度6",
    "温度7",
    "温度8",
    "压力",
    "振动",
]
SENSOR_COLUMNS = LEGACY_SENSOR_COLUMNS
NEW_CORE_SENSOR_COLUMNS = [
    "温度",
    "压力",
    "薄膜压力",
    "ROI平均温度",
    "张力",
    "线速度",
    "ABB_X",
    "ABB_Y",
    "ABB_Z",
]
NEW_OPTIONAL_SENSOR_COLUMNS = [
    *[f"温度{index}" for index in range(1, 9)],
]
# These legacy channels are intentionally excluded from the new collection
# plan.  New-schema checkpoints must not request them: missing physical sensor
# inputs are never replaced with a synthetic baseline during live inference.
NEW_EXCLUDED_SENSOR_COLUMNS = {"转速", "位移", "振动"}
NEW_COLLECTION_SENSOR_COLUMNS = list(
    dict.fromkeys([*NEW_CORE_SENSOR_COLUMNS, *NEW_OPTIONAL_SENSOR_COLUMNS])
)
ALL_SENSOR_COLUMNS = list(
    dict.fromkeys([*LEGACY_SENSOR_COLUMNS, *NEW_COLLECTION_SENSOR_COLUMNS])
)
PROCESS_PARAMETER_COLUMNS = [
    "initial_compaction_force_N",
    "placement_speed_mm_s",
    "pid_angle_deg",
    "temperature_setpoint_C",
]
ORIGINAL_COLUMNS = [
    "振动",
    "转速",
    "位移",
    "温度1",
    "温度2",
    "温度3",
    "温度4",
    "温度5",
    "温度6",
    "温度7",
    "温度8",
    "压力",
    "cycle",
    "file",
    "root",
    "p",
    "v",
    "pr",
    "l",
    "试件",
]
NEW_COLLECTION_COLUMNS = [
    "时间",
    *NEW_COLLECTION_SENSOR_COLUMNS,
    *PROCESS_PARAMETER_COLUMNS,
    "run_id",
    "specimen_id",
    "condition_id",
    "replicate",
    "layer_id",
]
ACQUISITION_SCHEMAS = {
    "legacy_original": {
        "label": "旧数据兼容方案（原12传感器）",
        "sensors": LEGACY_SENSOR_COLUMNS,
        "raw_columns": ORIGINAL_COLUMNS,
    },
    "new_collection_v11_3": {
        "label": "新数据集采集方案（17采集通道＋4工艺参数；含PLC压力和薄膜压力）",
        "sensors": NEW_COLLECTION_SENSOR_COLUMNS,
        "raw_columns": NEW_COLLECTION_COLUMNS,
    },
}

ALIASES = {
    "rotation": "转速",
    "rotation_speed": "转速",
    "speed_sensor": "转速",
    "displacement": "位移",
    "pressure": "压力",
    "compaction": "压力",
    "film_pressure": "薄膜压力",
    "thin_film_pressure": "薄膜压力",
    "m3232_pressure": "薄膜压力",
    "vibration": "振动",
    "temperature": "温度",
    "roi_temperature": "ROI平均温度",
    "tension": "张力",
    "line_speed": "线速度",
    "abb_x": "ABB_X",
    "abb_y": "ABB_Y",
    "abb_z": "ABB_Z",
    **{f"temperature_{index}": f"温度{index}" for index in range(1, 9)},
    **{f"temp{index}": f"温度{index}" for index in range(1, 9)},
}


DEFAULT_PLC_IP = "192.168.125.5"
DEFAULT_PLC_PORT = 502
DEFAULT_PLC_SLAVE_ID = 255
PLC_DEFAULT_REGISTER_MAP = {
    "温度": 28,
    "压力": 37,
    "张力": 23,
}
PLC_REGISTER_DECODERS: dict[str, tuple[str, float]] = {}
PLC_COMPUTED_CHANNELS: dict[str, list[str]] = {}
DEFAULT_ABB_IP = "192.168.125.1"
DEFAULT_ABB_USER = "Default User"
DEFAULT_ABB_PASSWORD = "robotics"
ABB_ROBTARGET_PATH = (
    "/rw/motionsystem/mechunits/ROB_1/robtarget?coordinate=Base&json=1"
)
ABB_RAPID_SYMBOL_BASE_PATH = "/rw/rapid/symbol/data/RAPID"
ABB_PROCESS_TASK = "T_ROB1"
ABB_PROCESS_MODULE = "MainModule"
ABB_SPEED_VARIABLE = "zMovespeed"
PLC_PID_ANGLE_REGISTER = 20

PROCESS_PARAMETER_DEFINITIONS = (
    ("initial_compaction_force_N", "初始压实力", "N"),
    ("placement_speed_mm_s", "铺放速度", "mm/s"),
    ("pid_angle_deg", "PID角度", "°"),
    ("temperature_setpoint_C", "设定温度", "°C"),
)


def resolve_default_simulation_source(
    payload: dict[str, Any], default_source: str | Path
) -> dict[str, Any]:
    """Replace a display-only default CSV name with its canonical path.

    Public/desktop bootstrap may display only the default file name.  The
    authorized acquisition endpoint still needs the server-side absolute path;
    arbitrary user-selected paths are deliberately left unchanged.
    """

    values = dict(payload or {})
    if str(values.get("simulation_source_type") or "single_csv").lower() != "single_csv":
        return values
    configured = Path(str(default_source or "").strip())
    if not configured.is_file():
        return values
    canonical = str(configured.resolve())
    for key in ("simulation_source_path", "source_file"):
        submitted = str(values.get(key) or "").strip()
        if submitted and not Path(submitted).is_absolute() and Path(submitted).name == configured.name:
            values[key] = canonical
    return values


def check_capture_save_root(path: str | Path | None) -> dict[str, Any]:
    """Check whether a requested capture directory may be used for saving.

    The check is intentionally non-creating: an empty path, a missing path, or
    a path without write permission disables file saving for that run.  A
    short-lived probe file catches Windows ACL/share failures that
    ``os.access`` alone can miss.
    """
    raw = str(path or "").strip()
    if not raw:
        return {"ok": False, "code": "empty", "path": "", "message": "保存位置为空，当前采集不保存数据"}
    candidate = Path(raw).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError as exc:
        return {"ok": False, "code": "invalid", "path": raw, "message": f"保存文件夹路径无效，当前采集不保存数据：{exc}"}
    if not candidate.exists():
        return {"ok": False, "code": "missing", "path": str(candidate), "message": "没有保存文件权限（保存文件夹不存在），当前采集不保存数据"}
    if not candidate.is_dir():
        return {"ok": False, "code": "not_directory", "path": str(candidate), "message": "没有保存文件权限（保存位置不是文件夹），当前采集不保存数据"}
    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".afp_write_check_", suffix=".tmp",
            dir=candidate, delete=False,
        ) as probe:
            probe.write(b"afp-save-check")
            probe.flush()
            os.fsync(probe.fileno())
            probe_path = Path(probe.name)
        return {"ok": True, "code": "ok", "path": str(candidate), "message": "当前可保存数据到该文件夹下"}
    except (OSError, PermissionError) as exc:
        return {"ok": False, "code": "not_writable", "path": str(candidate), "message": f"没有保存文件权限，当前采集不保存数据：{exc}"}
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass
BSV_UVC_WIDTH = 256
BSV_UVC_HEIGHT = 192
BSV_UVC_PIXELS = BSV_UVC_WIDTH * BSV_UVC_HEIGHT
BSV_UVC_YUV_BYTES = BSV_UVC_PIXELS * 2
BSV_UVC_TEMP_RANGE_CODE = 4
BSV_UVC_TEMP_FORMULA_SCALE = 64.0
BSV_UVC_TEMP_FORMULA_OFFSET = -50.0
BSV_UVC_DLL_NAME = "BsvUvcNative.dll"
DEFAULT_RTSP_URL = "rtsp://192.168.125.2:554/"
# The vendor operation manual specifies 115200 baud.  The vendor packet
# framing is not documented, so the driver accepts JSON matrix frames for the
# simulator/probe path and still needs one raw hardware capture for final
# protocol validation.
M3232_BAUDRATE = 115200

# The 16-channel collection plan remains authoritative.  This metadata only
# restores the verified physical source, unit and decoder contract used by the
# 2026-08-18 interface-mapped build.
SENSOR_CHANNEL_METADATA: dict[str, dict[str, str]] = {
    "温度": {"unit": "°C", "dtype": "float32", "source": "松下PLC DT28/DT29"},
    "压力": {"unit": "N", "dtype": "float32", "source": "松下PLC DT37/DT38（过程压力）"},
    "薄膜压力": {"unit": "N", "dtype": "float32", "source": "M3232薄膜压力传感器（独立接口）"},
    "ROI平均温度": {"unit": "°C", "dtype": "float32", "source": "BSV UVC温度矩阵ROI均值"},
    "张力": {"unit": "N", "dtype": "float32", "source": "松下PLC DT23/DT24"},
    "线速度": {"unit": "mm/s", "dtype": "float32", "source": "ABB相邻位置/时间差"},
    "ABB_X": {"unit": "mm", "dtype": "float32", "source": "ABB RWS robtarget.x"},
    "ABB_Y": {"unit": "mm", "dtype": "float32", "source": "ABB RWS robtarget.y"},
    "ABB_Z": {"unit": "mm", "dtype": "float32", "source": "ABB RWS robtarget.z"},
    **{
        f"温度{index}": {
            "unit": "°C", "dtype": "float32", "source": f"八通道热电偶 CH{index}"
        }
        for index in range(1, 9)
    },
}

# Selecting a sensor type is the routing authority: the type fixes its driver,
# default endpoint, decoding rule and compatible canonical channels.  Only the
# custom JSON profile permits manual driver/channel-map editing.
SENSOR_INTERFACE_PROFILES: dict[str, dict[str, Any]] = {
    "thermocouple": {
        "label": "八通道热电偶",
        "driver": "smrf_hid",
        "protocol": "smrf_hid",
        "physical_kind": "usb_hid",
        "endpoint": "SMRFCT08B",
        "channels": [f"温度{index}" for index in range(1, 9)],
        "channel_types": ["K"] * 8,
        "processing": (
            "USB HID；SMRF型号/序列号识别；SSPEED+HIDREAD；异或校验；"
            "CH1～CH8按K型热电偶和冷端温度换算"
        ),
    },
    "plc": {
        "label": "松下PLC过程传感器",
        "driver": "modbus_tcp",
        "protocol": "modbus_tcp",
        "physical_kind": "ethernet",
        "endpoint": f"{DEFAULT_PLC_IP}:{DEFAULT_PLC_PORT}",
        "channels": ["温度", "压力", "张力"],
        "processing": "Modbus TCP FC03；float32低字在前",
    },
    "robot": {
        "label": "ABB机器人",
        "driver": "abb_robot",
        "protocol": "abb_rws",
        "physical_kind": "ethernet",
        "endpoint": DEFAULT_ABB_IP,
        "channels": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
        "processing": "RWS读取robtarget；相邻XYZ欧氏距离除以时间差得到线速度",
    },
    "thermal_uvc": {
        "label": "BSV UVC测温热像仪",
        "driver": "uvc_thermal",
        "protocol": "uvc",
        "physical_kind": "usb_uvc",
        "endpoint": "BSV UVC (WinUSB)",
        "channels": ["ROI平均温度"],
        "processing": "256×192温度矩阵按raw/64-50换算后计算ROI均值",
    },
    "thermal_rtsp": {
        "label": "IP热像仪RTSP视频",
        "driver": "rtsp_thermal",
        "protocol": "rtsp",
        "physical_kind": "ethernet",
        "endpoint": DEFAULT_RTSP_URL,
        "channels": [],
        "processing": "仅预览/录像；无辐射测温标定时不生成数值温度通道",
    },
    "pressure": {
        "label": "M3232薄膜压力",
        "driver": "m3232_pressure",
        "protocol": "m3232_serial",
        "physical_kind": "serial",
        "endpoint": "COM8",
        "baudrate": M3232_BAUDRATE,
        "channels": ["薄膜压力"],
        "processing": "串口矩阵（行列自动识别）→有效像素合计→中值滤波（N）；坐标/零点按设备校准",
    },
    "custom": {
        "label": "自定义JSON传感器",
        "driver": "serial_json",
        "protocol": "custom",
        "physical_kind": "serial",
        "endpoint": "COM4",
        "baudrate": 115200,
        "channels": [],
        "processing": "按显式JSON键名映射；未映射通道不会进入采集数据",
        "editable_driver": True,
    },
}


def default_capture_interfaces() -> list[dict[str, Any]]:
    """Restore the five interfaces used by the 16-channel plan.

    Interface 5 is the dedicated M3232 thin-film pressure sensor.  Keeping it
    in the defaults makes the physical sensor visible in the UI and prevents
    pressure from silently falling back to the PLC interface.
    """
    return [
        {
            "id": "thermocouple_8ch", "enabled": True,
            "role": "thermocouple", "driver": "smrf_hid",
            "endpoint": "SMRFCT08B", "channel_types": ["K"] * 8,
            "channel_map": {},
        },
        {
            "id": "plc_process", "enabled": True,
            "role": "plc", "driver": "modbus_tcp",
            "endpoint": f"{DEFAULT_PLC_IP}:{DEFAULT_PLC_PORT}",
            "slave_id": DEFAULT_PLC_SLAVE_ID,
            "register_map": dict(PLC_DEFAULT_REGISTER_MAP),
            "channel_map": {},
        },
        {
            "id": "uvc_temperature", "enabled": True,
            "role": "thermal_uvc", "driver": "uvc_thermal",
            "endpoint": "BSV UVC (WinUSB)", "roi": "", "channel_map": {},
        },
        {
            "id": "abb_motion", "enabled": True,
            "role": "robot", "driver": "abb_robot",
            "endpoint": DEFAULT_ABB_IP, "channel_map": {},
        },
        {
            "id": "m3232_pressure", "enabled": True,
            "role": "pressure", "driver": "m3232_pressure",
            "endpoint": "COM8", "baudrate": M3232_BAUDRATE,
            "matrix_rows": 0, "matrix_cols": 0,
            "channels": ["薄膜压力"], "channel_map": {},
        },
    ]


def _canonical_sensor_type(role: Any, driver: Any = "") -> str:
    role_text = str(role or "custom").strip().lower()
    driver_text = str(driver or "").strip().lower()
    if role_text == "thermal":
        return "thermal_rtsp" if driver_text == "rtsp_thermal" else "thermal_uvc"
    if role_text in {"other", "new_sensor"}:
        return "custom"
    return role_text if role_text in SENSOR_INTERFACE_PROFILES else "custom"


def sensor_interface_profiles() -> list[dict[str, Any]]:
    return [
        {
            "id": profile_id,
            **{
                key: (list(value) if isinstance(value, list) else value)
                for key, value in profile.items()
            },
        }
        for profile_id, profile in SENSOR_INTERFACE_PROFILES.items()
    ]


def _apply_sensor_interface_profile(interface: dict[str, Any]) -> dict[str, Any]:
    sensor_type = _canonical_sensor_type(interface.get("role"), interface.get("driver"))
    profile = SENSOR_INTERFACE_PROFILES[sensor_type]
    interface["role"] = sensor_type
    if not profile.get("editable_driver"):
        interface["driver"] = str(profile["driver"])
    else:
        interface["driver"] = str(interface.get("driver") or profile["driver"])
    endpoint_text = str(interface.get("endpoint") or "").strip()
    if (
        not endpoint_text
        or (
            sensor_type == "thermocouple"
            and re.fullmatch(r"COM\d+", endpoint_text, re.IGNORECASE)
        )
    ):
        interface["endpoint"] = str(profile.get("endpoint") or "")
    if profile.get("baudrate"):
        interface["baudrate"] = int(profile["baudrate"])
    if profile.get("channel_types") and not interface.get("channel_types"):
        interface["channel_types"] = list(profile["channel_types"])
    if sensor_type == "plc":
        if not interface.get("register_map"):
            interface["register_map"] = dict(PLC_DEFAULT_REGISTER_MAP)
        interface["slave_id"] = int(
            interface.get("slave_id") or DEFAULT_PLC_SLAVE_ID
        )
    return interface


def _physical_interface_key(value: Any) -> str:
    """Normalize a physical-interface identifier for duplicate checks."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _validate_physical_interface_bindings(
    interfaces: list[dict[str, Any]], acquisition_mode: str
) -> None:
    """Validate role/protocol and physical endpoint ownership.

    PLC and ABB intentionally share the same Ethernet adapter.  Every other
    enabled logical interface must own a distinct physical adapter/device.

    Simulation files are a single logical source.  The UI keeps the same
    sensor/interface cards for routing and display, so several cards may
    intentionally carry the sentinel ``simulation_source`` binding.  That is
    not a physical adapter collision and must not be checked as one.
    """
    if acquisition_mode != "real":
        return
    seen: dict[str, dict[str, Any]] = {}
    for item in interfaces:
        if not item.get("enabled", True):
            continue
        role = _canonical_sensor_type(item.get("role"), item.get("driver"))
        profile = SENSOR_INTERFACE_PROFILES.get(role, SENSOR_INTERFACE_PROFILES["custom"])
        expected_driver = str(profile.get("driver") or "")
        actual_driver = str(item.get("driver") or "")
        if not profile.get("editable_driver") and actual_driver != expected_driver:
            raise ValueError(
                f"接口“{item.get('id', role)}”的协议/驱动与传感器类型不匹配："
                f"{actual_driver or '未填写'}，应为{expected_driver}"
            )
        expected_kind = str(profile.get("physical_kind") or "")
        actual_kind = str(item.get("physical_interface_kind") or "")
        fallback_binding = bool(item.get("physical_fallback", False))
        if actual_kind and expected_kind and actual_kind != expected_kind and not fallback_binding:
            raise ValueError(
                f"接口“{item.get('id', role)}”的物理接口类型与协议不匹配："
                f"{actual_kind}，应为{expected_kind}"
            )
        physical_id = _physical_interface_key(item.get("physical_interface_id"))
        if acquisition_mode == "real" and not physical_id:
            raise ValueError(
                f"真实接口“{item.get('id', role)}”尚未绑定实际物理接口"
            )
        if not physical_id:
            continue
        previous = seen.get(physical_id)
        if previous is None:
            seen[physical_id] = item
            continue
        shared_roles = {str(previous.get("role") or ""), role}
        shared_kinds = {
            str(previous.get("physical_interface_kind") or expected_kind),
            actual_kind or expected_kind,
        }
        if shared_roles == {"plc", "robot"} and shared_kinds == {"ethernet"}:
            continue
        raise ValueError(
            f"物理接口“{item.get('physical_interface_id')}”重复绑定："
            f"{previous.get('id', '前一接口')}与{item.get('id', role)}；"
            "仅允许PLC与ABB共享同一网卡"
        )


def _resolve_interface_channel_assignments(
    interfaces: list[dict[str, Any]],
    requested: dict[str, list[str]] | None,
    capture_sensors: list[str],
) -> dict[str, list[str]]:
    requested = requested or {}
    capture_set = set(capture_sensors)
    resolved: dict[str, list[str]] = {}
    enabled = [item for item in interfaces if item.get("enabled", True)]
    for item in interfaces:
        interface_id = str(item.get("id") or "")
        if not item.get("enabled", True):
            resolved[interface_id] = []
            continue
        role = str(item.get("role") or "custom")
        profile = SENSOR_INTERFACE_PROFILES.get(
            role, SENSOR_INTERFACE_PROFILES["custom"]
        )
        allowed = set(profile.get("channels") or []) & capture_set
        if role == "custom":
            channel_map = item.get("channel_map") or {}
            if isinstance(channel_map, dict):
                allowed.update(
                    str(value) for value in channel_map.values()
                    if str(value) in capture_set
                )
            if interface_id in requested:
                allowed.update(
                    str(name) for name in requested[interface_id]
                    if str(name) in capture_set
                )
        chosen = allowed
        if interface_id in requested:
            chosen &= {str(name) for name in requested[interface_id]}
        resolved[interface_id] = [
            name for name in capture_sensors if name in chosen
        ]
    owners: dict[str, str] = {}
    for interface_id, channels in resolved.items():
        for channel in channels:
            previous = owners.get(channel)
            if previous and previous != interface_id:
                raise ValueError(
                    f"数据通道“{channel}”同时分配给{previous}和{interface_id}；"
                    "每个实际数据通道只能有一个传感器来源"
                )
            owners[channel] = interface_id
    return resolved


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_sample(payload: dict[str, Any]) -> dict[str, float]:
    """Normalize one interface payload into the canonical AFP channel names.

    Hardware gateways are allowed to send either a flat JSON object or a
    ``channels``/``values`` object.  A thermocouple interface may also send an
    array; its values are mapped to 温度1..温度8 in order.  ``channel_map`` is
    intentionally explicit so a user can rename vendor channels without
    changing the saved/displayed schema.
    """
    return _normalize_interface_sample(payload)


def _normalize_interface_sample(
    payload: dict[str, Any],
    role: str = "other",
    channel_map: dict[str, str] | None = None,
) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    channel_map = channel_map or {}
    flattened: dict[str, Any] = dict(payload)
    for nested_key in ("channels", "values", "data"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            flattened.update(nested)
        elif isinstance(nested, (list, tuple)):
            flattened.pop(nested_key, None)
            if role == "thermocouple":
                for index, value in enumerate(nested[:8], start=1):
                    flattened[f"temperature_{index}"] = value
    # Common vendor spelling for a temperature array.
    for array_key in ("thermocouples", "thermocouple", "temperatures", "tc"):
        values = payload.get(array_key)
        if isinstance(values, (list, tuple)):
            for index, value in enumerate(values[:8], start=1):
                flattened[f"temperature_{index}"] = value
    normalized: dict[str, float] = {}
    for key, value in flattened.items():
        if isinstance(value, (dict, list, tuple)):
            continue
        source = str(key)
        target = channel_map.get(source) or channel_map.get(source.lower())
        if not target:
            target = ALIASES.get(source, source)
        # Generic CH1/CH2 names on a thermocouple adapter are interpreted in
        # channel order.  Other interfaces must use an explicit map or a
        # canonical/aliased name to prevent accidental column mislabelling.
        if role == "thermocouple" and target == source:
            match = re.fullmatch(r"(?:ch|channel|tc|t)[_ -]?(\d+)", source, re.I)
            if match:
                target = f"温度{int(match.group(1))}"
        if target in ALL_SENSOR_COLUMNS:
            number = _finite(value)
            if number is not None:
                normalized[target] = number
    return normalized


@dataclass
class AcquisitionConfig:
    processing_mode: str = "prediction_warning"
    dataset_schema: str = "legacy_original"
    use_best_prediction_override: bool = False
    driver: str = "simulator"
    endpoint: str = ""
    baudrate: int = 115200
    # Multiple physical interfaces can be used in one acquisition.
    interfaces: list[dict[str, Any]] | None = None
    interface_channel_assignments: dict[str, list[str]] | None = None
    sample_rate_hz: float = 10.0
    selected_sensors: list[str] | None = None
    prediction_sensors: list[str] | None = None
    model_input_sensors: list[str] | None = None
    model_output_sensors: list[str] | None = None
    prediction_model_file: str = ""
    prediction_model_type: str = "i_T_G"
    health_indicator: str = "TC-HI"
    run_id: str = "LIVE_RUN"
    specimen_id: str = "LIVE_SPECIMEN"
    # Generated by the acquisition manager and persisted across all layers of
    # one physical specimen.  It is intentionally independent of the visible
    # folder/file names so operators can keep the concise naming convention
    # without risking two specimens sharing the same database identity.
    capture_uuid: str = ""
    condition_id: str = "LIVE"
    layer: int = 0
    cycle: int = 1
    p: float = 600.0
    v: float = 100.0
    pr: float = 600.0
    root: str = "LIVE"
    source_file: str = ""
    save_root: str = ""
    initial_compaction_force_N: float = 400.0
    placement_speed_mm_s: float = 80.0
    pid_angle_deg: float = 5.0
    temperature_setpoint_C: float = 360.0
    replicate: int = 1
    # Explicitly separate real interfaces from local simulation sources.
    # Empty keeps backward compatibility with older callers: a simulator
    # driver implies simulation, while serial/TCP implies real interfaces.
    acquisition_mode: str = ""
    simulation_source_type: str = "single_csv"
    simulation_source_path: str = ""
    simulation_mysql_query: str = ""
    simulation_mysql_host: str = "127.0.0.1"
    simulation_mysql_port: int = 3306
    simulation_mysql_user: str = "root"
    simulation_mysql_password: str = ""
    simulation_mysql_database: str = "afp_state_warning"
    # MySQL is deliberately opt-in.  Local CSV/JSON files remain the primary
    # raw-data archive and MySQL receives a transaction after each saved layer.
    # ``mysql_enabled`` remains the target/remote destination switch for API
    # compatibility.  A second, independent local destination can be enabled
    # at the same time; both receive the same finalized layer transaction.
    mysql_enabled: bool = False
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "afp_state_warning"
    mysql_local_enabled: bool = False
    mysql_local_host: str = "127.0.0.1"
    mysql_local_port: int = 3306
    mysql_local_user: str = "root"
    mysql_local_password: str = ""
    mysql_local_database: str = "afp_state_warning"
    mysql_charset: str = "utf8mb4"
    mysql_connect_timeout: int = 5

    def __post_init__(self) -> None:
        requested_mode = str(self.acquisition_mode or "").lower()
        if not requested_mode:
            requested_mode = "simulation" if self.driver == "simulator" else "real"
        self.acquisition_mode = requested_mode
        if self.acquisition_mode not in {"real", "simulation"}:
            raise ValueError("acquisition_mode must be real or simulation")
        self.simulation_source_type = str(self.simulation_source_type or "single_csv").lower()
        if self.simulation_source_type not in {"single_csv", "folder_csv", "mysql"}:
            raise ValueError("simulation_source_type must be single_csv, folder_csv or mysql")
        self.simulation_source_path = str(self.simulation_source_path or self.source_file or "").strip()
        self.simulation_mysql_query = str(self.simulation_mysql_query or "").strip()
        self.simulation_mysql_host = str(self.simulation_mysql_host or "127.0.0.1").strip()
        self.simulation_mysql_port = max(1, min(int(self.simulation_mysql_port or 3306), 65535))
        self.simulation_mysql_user = str(self.simulation_mysql_user or "root").strip()
        self.simulation_mysql_password = str(self.simulation_mysql_password or "")
        self.simulation_mysql_database = validate_database_name(self.simulation_mysql_database or "afp_state_warning")
        if self.processing_mode not in {"capture_only", "prediction_warning"}:
            raise ValueError(
                "processing_mode必须是capture_only或prediction_warning"
            )
        if self.dataset_schema not in ACQUISITION_SCHEMAS:
            raise ValueError(f"未知数据采集方案：{self.dataset_schema}")
        if self.interfaces is None:
            self.interfaces = [{
                "id": "interface_1", "enabled": True,
                "driver": self.driver, "endpoint": self.endpoint,
                "baudrate": self.baudrate, "role": "other",
                "channel_map": {},
            }]
        normalized_interfaces: list[dict[str, Any]] = []
        for index, item in enumerate(self.interfaces):
            if not isinstance(item, dict):
                continue
            interface = dict(item)
            interface["id"] = str(interface.get("id") or f"interface_{index + 1}")
            interface["enabled"] = bool(interface.get("enabled", True))
            role_hint = _canonical_sensor_type(interface.get("role"), interface.get("driver"))
            profile_hint = SENSOR_INTERFACE_PROFILES.get(role_hint, SENSOR_INTERFACE_PROFILES["custom"])
            requested_driver = str(interface.get("driver") or "").strip()
            if requested_driver and not profile_hint.get("editable_driver"):
                expected_driver = str(profile_hint.get("driver") or "")
                if requested_driver != expected_driver:
                    raise ValueError(
                        f"接口“{interface['id']}”的协议/驱动与传感器类型不匹配："
                        f"{requested_driver}，应为{expected_driver}"
                    )
            interface["driver"] = requested_driver or (
                str(profile_hint.get("driver") or self.driver)
                if role_hint != "custom" else self.driver
            )
            interface["endpoint"] = str(interface.get("endpoint") or "").strip()
            interface["baudrate"] = int(interface.get("baudrate") or self.baudrate)
            interface["timeout"] = max(0.01, min(float(interface.get("timeout") or 0.05), 2.0))
            interface["role"] = _canonical_sensor_type(
                interface.get("role"), interface.get("driver")
            )
            channel_map = interface.get("channel_map") or {}
            if isinstance(channel_map, str):
                try:
                    channel_map = json.loads(channel_map)
                except json.JSONDecodeError:
                    channel_map = {}
            interface["channel_map"] = (
                {str(key): str(value) for key, value in channel_map.items()}
                if isinstance(channel_map, dict) else {}
            )
            register_map = interface.get("register_map") or {}
            interface["register_map"] = (
                {str(key): int(value) for key, value in register_map.items()}
                if isinstance(register_map, dict) else {}
            )
            interface["slave_id"] = int(
                interface.get("slave_id") or DEFAULT_PLC_SLAVE_ID
            )
            _apply_sensor_interface_profile(interface)
            interface["physical_interface_id"] = str(
                interface.get("physical_interface_id") or ""
            ).strip()
            interface["physical_interface_kind"] = str(
                interface.get("physical_interface_kind")
                or SENSOR_INTERFACE_PROFILES.get(interface["role"], SENSOR_INTERFACE_PROFILES["custom"]).get("physical_kind")
                or ""
            ).strip().lower()
            interface["physical_verified"] = bool(interface.get("physical_verified", False))
            interface["physical_fallback"] = bool(interface.get("physical_fallback", False))
            normalized_interfaces.append(interface)
        if not normalized_interfaces:
            raise ValueError("至少配置一个采集接口")
        self.interfaces = normalized_interfaces
        _validate_physical_interface_bindings(normalized_interfaces, self.acquisition_mode)
        raw_assignments = self.interface_channel_assignments or {}
        requested_assignments = {
            str(key): [str(name) for name in (value or [])]
            for key, value in raw_assignments.items()
            if isinstance(value, (list, tuple, set))
        }
        self.interface_channel_assignments = requested_assignments
        enabled = [item for item in normalized_interfaces if item["enabled"]]
        if enabled:
            self.driver = str(enabled[0]["driver"])
            self.endpoint = str(enabled[0]["endpoint"])
            self.baudrate = int(enabled[0]["baudrate"])
        if self.acquisition_mode == "real":
            if not enabled:
                raise ValueError("真实接口采集模式至少要启用一个传感器接口")
            if any(item.get("driver") == "simulator" for item in normalized_interfaces if item.get("enabled", True)):
                raise ValueError("真实接口采集模式不能使用本地模拟驱动")
            self.source_file = ""
            self.simulation_source_path = ""
        else:
            self.source_file = self.simulation_source_path
        allowed_sensors = list(
            ACQUISITION_SCHEMAS[self.dataset_schema]["sensors"]
        )
        if self.selected_sensors is None:
            self.selected_sensors = allowed_sensors.copy()
        self.selected_sensors = [
            name for name in self.selected_sensors if name in allowed_sensors
        ]
        if self.acquisition_mode == "real":
            self.interface_channel_assignments = _resolve_interface_channel_assignments(
                normalized_interfaces, requested_assignments, allowed_sensors
            )
            enabled_ids = {
                str(item.get("id"))
                for item in normalized_interfaces
                if item.get("enabled", True)
            }
            routed = {
                channel
                for interface_id, channels in self.interface_channel_assignments.items()
                if interface_id in enabled_ids
                for channel in channels
            }
            self.selected_sensors = [
                name for name in self.selected_sensors if name in routed
            ]
            if not self.selected_sensors:
                raise ValueError("已启用的传感器类型没有对应的采集数据通道")
        if not self.selected_sensors:
            raise ValueError("至少选择一个采集传感器")
        if self.acquisition_mode == "simulation":
            # Simulation follows the same routing contract as a physical
            # capture: only channels assigned to enabled interfaces are read,
            # saved and passed to the model.  The browser normally sends the
            # explicit channel mapping; older callers without that mapping use
            # the role-based defaults for backward compatibility.
            enabled_ids = {
                str(item.get("id"))
                for item in normalized_interfaces
                if item.get("enabled", True)
            }
            explicit = {
                str(channel)
                for interface_id, channels in self.interface_channel_assignments.items()
                if str(interface_id) in enabled_ids
                for channel in channels
                if str(channel) in allowed_sensors
            }
            if self.interface_channel_assignments:
                routed = explicit
            elif (
                len(enabled_ids) == 1
                and normalized_interfaces[0].get("driver") == "simulator"
            ):
                # Backward-compatible single-file replay: without an explicit
                # map, the one simulator supplies every channel the operator
                # selected.  The browser sends an explicit map whenever more
                # than one logical interface is configured.
                routed = set(self.selected_sensors)
            else:
                routed: set[str] = set()
                for item in normalized_interfaces:
                    if not item.get("enabled", True):
                        continue
                    role = str(item.get("role") or "other").lower()
                    if role == "thermocouple":
                        routed.update(
                            name for name in allowed_sensors
                            if re.fullmatch(r"温度[1-8]", name)
                        )
                    elif role == "other":
                        routed.update(
                            name for name in allowed_sensors
                            if not re.fullmatch(r"温度[1-8]", name)
                        )
                    else:
                        routed.update(allowed_sensors)
            self.selected_sensors = [
                name for name in self.selected_sensors if name in routed
            ]
            if not self.selected_sensors:
                raise ValueError(
                    "模拟采集没有可用通道：请在通道映射中把至少一个通道分配给已启用接口"
                )
        # The acquisition checklist is the source of truth for the channels
        # that are actually collected.  Drop stale interface assignments for
        # unchecked channels so real and simulated capture use the same
        # effective mapping and cannot save/route an unselected sensor.
        selected_set = set(self.selected_sensors)
        self.interface_channel_assignments = {
            interface_id: [
                name for name in channels if name in selected_set
            ]
            for interface_id, channels in self.interface_channel_assignments.items()
            if any(name in selected_set for name in channels)
        }
        if self.model_input_sensors is None:
            self.model_input_sensors = self.selected_sensors.copy()
        self.model_input_sensors = [
            name
            for name in self.model_input_sensors
            if name in allowed_sensors and name in self.selected_sensors
        ]
        if self.prediction_sensors is None:
            self.prediction_sensors = self.selected_sensors.copy()
        self.prediction_sensors = [
            name
            for name in self.prediction_sensors
            if name in allowed_sensors and name in self.selected_sensors
        ]
        if self.model_output_sensors is None:
            self.model_output_sensors = self.prediction_sensors.copy()
        self.model_output_sensors = [
            name
            for name in self.model_output_sensors
            if name in allowed_sensors and name in self.model_input_sensors
        ]
        if self.processing_mode == "capture_only":
            self.prediction_sensors = []
            self.model_input_sensors = []
            self.model_output_sensors = []
            self.prediction_model_file = ""
        # Keep the legacy field as the display/output selection used by /api/live.
        self.prediction_sensors = self.model_output_sensors.copy()
        self.sample_rate_hz = max(0.1, min(float(self.sample_rate_hz), 1000.0))
        self.baudrate = int(self.baudrate)
        self.layer = max(0, int(self.layer))
        self.cycle = int(self.cycle)
        self.replicate = max(1, int(self.replicate))
        self.mysql_enabled = bool(self.mysql_enabled)
        self.mysql_host = str(self.mysql_host or "127.0.0.1").strip()
        self.mysql_port = max(1, min(int(self.mysql_port), 65535))
        self.mysql_user = str(self.mysql_user or "root")
        self.mysql_password = str(self.mysql_password or "")
        self.mysql_database = validate_database_name(
            self.mysql_database or "afp_state_warning"
        )
        self.mysql_local_enabled = bool(self.mysql_local_enabled)
        self.mysql_local_host = str(self.mysql_local_host or "127.0.0.1").strip()
        self.mysql_local_port = max(1, min(int(self.mysql_local_port), 65535))
        self.mysql_local_user = str(self.mysql_local_user or "root")
        self.mysql_local_password = str(self.mysql_local_password or "")
        self.mysql_local_database = validate_database_name(
            self.mysql_local_database or "afp_state_warning"
        )
        self.mysql_charset = str(self.mysql_charset or "utf8mb4")
        self.mysql_connect_timeout = max(
            1, min(int(self.mysql_connect_timeout), 60)
        )
        self.use_best_prediction_override = bool(
            self.use_best_prediction_override
        )
        self.initial_compaction_force_N = float(
            self.initial_compaction_force_N
        )
        self.placement_speed_mm_s = float(self.placement_speed_mm_s)
        self.pid_angle_deg = float(self.pid_angle_deg)
        self.temperature_setpoint_C = float(self.temperature_setpoint_C)
        self.capture_uuid = str(self.capture_uuid or "").strip()

    @property
    def schema_sensors(self) -> list[str]:
        return list(ACQUISITION_SCHEMAS[self.dataset_schema]["sensors"])

    @property
    def raw_columns(self) -> list[str]:
        """Return only selected sensor columns plus identity/process fields."""
        definition = ACQUISITION_SCHEMAS[self.dataset_schema]
        selected = set(self.selected_sensors or [])
        sensor_names = set(definition["sensors"])
        return [
            name
            for name in definition["raw_columns"]
            if name not in sensor_names or name in selected
        ]

    @property
    def process_columns(self) -> list[str]:
        return list(PROCESS_PARAMETER_COLUMNS)


def _safe_component(value: Any, max_length: int = 40) -> str:
    """Create a readable Windows-safe path component with a bounded length."""
    text = str(value).strip()
    invalid = '<>:"/\\|?*'
    text = "".join("_" if char in invalid or ord(char) < 32 else char for char in text)
    text = text.rstrip(" .") or "未命名"
    if len(text) <= max_length:
        return text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{text[: max_length - 9]}_{digest}"


def _parameter_token(config: AcquisitionConfig) -> str:
    # Keep the physical condition and independent replicate visible in the
    # folder name.  The layer files and complete-specimen snapshot inherit
    # this prefix, so files from different conditions/replicates cannot be
    # mixed accidentally.
    condition = _safe_component(config.condition_id or "LIVE", max_length=24)
    replicate = max(1, int(config.replicate or 1))
    if config.dataset_schema == "new_collection_v11_3":
        return (
            f"C{condition}_R{replicate}_"
            f"F{config.initial_compaction_force_N:g}_"
            f"V{config.placement_speed_mm_s:g}_"
            f"A{config.pid_angle_deg:g}_"
            f"T{config.temperature_setpoint_C:g}"
        )
    prefix = f"R{replicate}" if condition == "LIVE" else f"C{condition}_R{replicate}"
    return f"{prefix}_p{config.p:g}_v{config.v:g}_pr{config.pr:g}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one JSON record without exposing a half-written final file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _file_integrity(path: Path | None) -> dict[str, Any] | None:
    """Return size and SHA-256 for a completed local acquisition artifact."""
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": str(path),
        "bytes": int(path.stat().st_size),
        "sha256": digest.hexdigest(),
    }


def select_capture_folder(initial_path: str = "") -> str:
    """Open the native folder chooser used by the local web/desktop interface."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            parent=root,
            title="选择AFP采集数据保存位置",
            initialdir=initial_path or str(DEFAULT_CAPTURE_ROOT),
            mustexist=False,
        )
        return str(Path(selected).resolve()) if selected else ""
    finally:
        root.destroy()


def select_simulation_source(source_type: str, initial_path: str = "") -> str:
    """Select one CSV or a capture-folder source for simulation replay."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if source_type == "folder_csv":
            selected = filedialog.askdirectory(
                parent=root, title="选择模拟采集数据文件夹",
                initialdir=initial_path or str(DEFAULT_CAPTURE_ROOT), mustexist=True,
            )
        else:
            selected = filedialog.askopenfilename(
                parent=root, title="选择模拟采集 CSV 文件",
                initialdir=initial_path or str(DEFAULT_CAPTURE_ROOT),
                filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            )
        return str(Path(selected).resolve()) if selected else ""
    finally:
        root.destroy()


def integrate_capture_sources(
    source_type: str,
    source_path: str = "",
    output_file: str = "",
    mysql_settings: dict[str, Any] | None = None,
    query: str = "",
) -> dict[str, Any]:
    """Merge complete-specimen capture data from a folder or MySQL into CSV.

    Folder integration deliberately selects only the current complete file
    for each specimen folder.  Historical snapshots and layer-only files are
    ignored, preventing duplicate rows when old captures are imported.
    """
    kind = str(source_type or "folder_csv").strip().lower()
    frames: list[pd.DataFrame] = []
    source_count = 0
    specimen_keys: set[str] = set()
    if kind in {"folder", "folder_csv", "csv_folder"}:
        root = Path(source_path).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"Capture folder does not exist: {root}")
        candidates: list[Path] = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() != ".csv":
                continue
            if any(part in {"\u5386\u53f2\u7248\u672c", "\u5206\u5c42\u6570\u636e", "\u5b8c\u6574\u8bd5\u6837\u5feb\u7167"} for part in path.parts):
                continue
            if "\u5b8c\u6574\u8bd5\u6837" in path.name:
                candidates.append(path)
        # Select the newest complete snapshot when an old folder contains
        # several _已采N层 files.
        selected: dict[str, Path] = {}
        for path in candidates:
            key = str(path.parent.resolve())
            previous = selected.get(key)
            if previous is None or path.stat().st_mtime > previous.stat().st_mtime:
                selected[key] = path
        if not selected:
            raise ValueError("No complete-specimen CSV files were found in the selected folder")
        for path in sorted(selected.values()):
            try:
                frame = pd.read_csv(path, encoding="utf-8-sig")
            except UnicodeDecodeError:
                frame = pd.read_csv(path, encoding="gb18030")
            if frame.empty:
                continue
            frame["source_file"] = str(path)
            frames.append(frame)
            specimen_keys.add(str(path.parent.resolve()))
        source_count = len(selected)
        default_output = root / "\u6574\u5408\u6570\u636e.csv"
    elif kind == "mysql":
        settings = MySQLSettings.from_mapping(mysql_settings or {})
        store = MySQLCaptureStore(settings)
        driver, connection = store._connect(settings.database)
        try:
            sql = str(query or "").strip() or (
                "SELECT * FROM afp_flat_all "
                "ORDER BY specimen_key, layer_no, sample_index"
            )
            cursor = connection.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            names = [item[0] for item in cursor.description or []]
            records = [dict(zip(names, row)) for row in rows]
        finally:
            connection.close()
        if not records:
            raise ValueError("The MySQL query returned no rows")
        expanded: list[dict[str, Any]] = []
        for row in records:
            item = dict(row)
            for json_name in ("sensor_json", "process_json"):
                raw = item.pop(json_name, None)
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        parsed = {}
                    if isinstance(parsed, dict):
                        item.update(parsed)
            item["source_file"] = f"mysql:{settings.database}"
            expanded.append(item)
            specimen_keys.add(str(row.get("specimen_key") or row.get("specimen_id") or "unknown"))
        frames.append(pd.DataFrame(expanded))
        source_count = len(specimen_keys)
        default_output = APP_DIR / "\u6574\u5408\u6570\u636e.csv"
    else:
        raise ValueError("source_type must be folder_csv or mysql")
    merged = pd.concat(frames, ignore_index=True, sort=False)
    requested_output = str(output_file or "").strip()
    if requested_output:
        destination = Path(requested_output).expanduser().resolve()
        # Users often choose a folder in the save-location field.  Treat an
        # existing directory (or a path ending with a separator) as a folder
        # and place the canonical CSV inside it instead of opening the folder
        # as if it were a file.
        if destination.exists() and destination.is_dir():
            destination = destination / "\u6574\u5408\u6570\u636e.csv"
        elif requested_output.endswith(("\\", "/")):
            destination = destination / "\u6574\u5408\u6570\u636e.csv"
    else:
        destination = default_output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the destination first and replace atomically.  This avoids
    # partial CSV files and handles an existing file that is being refreshed.
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            suffix=".tmp",
            prefix=".afp_integrate_",
            dir=str(destination.parent),
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            merged.to_csv(temporary, index=False)
        os.replace(temporary_path, destination)
    except PermissionError as exc:
        try:
            if "temporary_path" in locals() and temporary_path.exists():
                temporary_path.unlink()
        except OSError:
            pass
        raise PermissionError(
            f"无法写入整合文件：{destination}。请关闭同名 CSV，"
            "或在“整合文件保存位置”中选择一个可写的新文件名。"
        ) from exc
    return {
        "ok": True,
        "source_type": kind,
        "output_file": str(destination),
        "rows": int(len(merged)),
        "columns": [str(name) for name in merged.columns],
        "source_files": int(source_count),
        "specimens": int(len(specimen_keys)),
    }


class SampleDriver:
    def open(self) -> None:
        raise NotImplementedError

    def read_sample(self) -> dict[str, float] | None:
        raise NotImplementedError

    def close(self) -> None:
        return


class SimulatorDriver(SampleDriver):
    def __init__(self, path: Path, sensor_columns: list[str]) -> None:
        self.path = path
        self.sensor_columns = sensor_columns
        self.records: list[dict[str, Any]] = []
        self.index = 0

    def open(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"模拟数据文件不存在：{self.path}")
        try:
            frame = pd.read_csv(self.path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            frame = pd.read_csv(self.path, encoding="gb18030")
        missing = [
            name for name in self.sensor_columns if name not in frame.columns
        ]
        if missing:
            raise ValueError(f"模拟数据缺少传感器列：{missing}")
        self.records = frame[self.sensor_columns].to_dict(orient="records")
        self.index = 0

    def read_sample(self) -> dict[str, float] | None:
        if not self.records:
            return None
        record = self.records[self.index % len(self.records)]
        self.index += 1
        return normalize_sample(record)


class FolderCsvSimulatorDriver(SimulatorDriver):
    """Replay every CSV in a capture folder in lexical file order."""
    def open(self) -> None:
        if not self.path.exists() or not self.path.is_dir():
            raise FileNotFoundError(f"模拟采集文件夹不存在：{self.path}")
        files = sorted(self.path.rglob("*.csv"))
        if not files:
            raise FileNotFoundError(f"模拟采集文件夹不包含 CSV：{self.path}")
        frames = []
        for file in files:
            try:
                frames.append(pd.read_csv(file, encoding="utf-8-sig"))
            except UnicodeDecodeError:
                frames.append(pd.read_csv(file, encoding="gb18030"))
        frame = pd.concat(frames, ignore_index=True, sort=False)
        missing = [name for name in self.sensor_columns if name not in frame.columns]
        if missing:
            raise ValueError(f"模拟采集文件夹缺少传感器列：{missing}")
        self.records = frame[self.sensor_columns].to_dict(orient="records")
        self.index = 0


class MySQLSimulatorDriver(SampleDriver):
    """Replay rows from the same flat MySQL view used by acquisition storage."""
    def __init__(self, settings: dict[str, Any], sensor_columns: list[str]) -> None:
        self.settings = settings
        self.sensor_columns = sensor_columns
        self.records: list[dict[str, Any]] = []
        self.index = 0
        self.connection = None
        self.driver_name = ""

    def open(self) -> None:
        """Load the replay rows with either supported MySQL Python driver.

        The native bundle includes ``mysql-connector-python`` because it is
        also used by the normal MySQL capture store.  Older builds required
        PyMySQL only here, which made the refresh/connection check succeed but
        the actual MySQL simulation fail at start-up.  Prefer PyMySQL when it
        is installed and fall back to mysql.connector so both paths use the
        same available dependency.
        """
        pymysql = None
        try:
            import pymysql
            self.driver_name = "pymysql"
        except ImportError:
            try:
                import mysql.connector as mysql_connector
                self.driver_name = "mysql.connector"
            except ImportError as exc:
                raise RuntimeError(
                    "未找到可用的 MySQL 驱动，请安装 mysql-connector-python 或 PyMySQL"
                ) from exc
        query = str(self.settings.get("query") or "").strip() or (
            "SELECT * FROM afp_flat_all ORDER BY specimen_key, layer_no, sample_index"
        )
        connection_args = {
            "host": str(self.settings.get("host") or "127.0.0.1"),
            "port": int(self.settings.get("port") or 3306),
            "user": str(self.settings.get("user") or "root"),
            "password": str(self.settings.get("password") or ""),
            "database": str(self.settings.get("database") or "afp_state_warning"),
        }
        if self.driver_name == "pymysql":
            connection_args.update(
                {"charset": "utf8mb4", "connect_timeout": 5,
                 "cursorclass": pymysql.cursors.DictCursor}
            )
            self.connection = pymysql.connect(**connection_args)
            with self.connection.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
        else:
            import mysql.connector as mysql_connector
            connection_args["connection_timeout"] = 5
            self.connection = mysql_connector.connect(**connection_args)
            cursor = self.connection.cursor(dictionary=True)
            try:
                cursor.execute(query)
                rows = cursor.fetchall()
            finally:
                cursor.close()
        self.records = []
        for row in rows:
            payload: dict[str, Any] = {}
            sensor_json = row.get("sensor_json") if isinstance(row, dict) else None
            if isinstance(sensor_json, str):
                try:
                    parsed = json.loads(sensor_json)
                    if isinstance(parsed, dict): payload.update(parsed)
                except json.JSONDecodeError:
                    pass
            if isinstance(row, dict):
                payload.update({name: row[name] for name in self.sensor_columns if name in row})
            normalized = normalize_sample(payload)
            if normalized: self.records.append(normalized)
        self.index = 0
        if not self.records:
            raise ValueError("MySQL 查询没有包含可用传感器数据")

    def read_sample(self) -> dict[str, float] | None:
        if not self.records: return None
        record = self.records[self.index % len(self.records)]
        self.index += 1
        return record

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class SerialJsonDriver(SampleDriver):
    def __init__(self, endpoint: str, baudrate: int, role: str = "other", channel_map: dict[str, str] | None = None, timeout: float = 0.5) -> None:
        self.endpoint = endpoint
        self.baudrate = baudrate
        self.role = role
        self.channel_map = channel_map or {}
        self.timeout = max(0.01, float(timeout))
        self.serial = None

    def open(self) -> None:
        if not self.endpoint:
            raise ValueError("串口模式必须填写端口，例如 COM3")
        import serial

        self.serial = serial.Serial(
            self.endpoint,
            self.baudrate,
            timeout=self.timeout,
        )

    def read_sample(self) -> dict[str, float] | None:
        raw = self.serial.readline()
        if not raw:
            return None
        payload = json.loads(raw.decode("utf-8-sig").strip())
        if not isinstance(payload, dict):
            raise ValueError("串口每行必须是JSON对象")
        return _normalize_interface_sample(payload, self.role, self.channel_map)

    def close(self) -> None:
        if self.serial is not None:
            self.serial.close()
            self.serial = None


class TcpJsonDriver(SampleDriver):
    def __init__(self, endpoint: str, role: str = "other", channel_map: dict[str, str] | None = None, timeout: float = 0.5) -> None:
        self.endpoint = endpoint
        self.role = role
        self.channel_map = channel_map or {}
        self.timeout = max(0.01, float(timeout))
        self.sock: socket.socket | None = None
        self.file = None

    def open(self) -> None:
        if ":" not in self.endpoint:
            raise ValueError("TCP地址格式必须是 host:port")
        host, port_text = self.endpoint.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port_text)), timeout=2.0)
        self.sock.settimeout(self.timeout)
        self.file = self.sock.makefile("rb")

    def read_sample(self) -> dict[str, float] | None:
        try:
            raw = self.file.readline()
        except (socket.timeout, TimeoutError):
            return None
        if not raw:
            return None
        payload = json.loads(raw.decode("utf-8-sig").strip())
        if not isinstance(payload, dict):
            raise ValueError("TCP每行必须是JSON对象")
        return _normalize_interface_sample(payload, self.role, self.channel_map)

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
        if self.sock is not None:
            self.sock.close()
            self.sock = None


class ModbusTcpDriver(SampleDriver):
    """Panasonic PLC Modbus/TCP FC03 reader from the mapped field build."""

    def __init__(self, endpoint: str, register_map: dict[str, int] | None = None,
                 slave_id: int = DEFAULT_PLC_SLAVE_ID, timeout: float = 1.0) -> None:
        self.endpoint = endpoint or f"{DEFAULT_PLC_IP}:{DEFAULT_PLC_PORT}"
        self.register_map = dict(register_map or PLC_DEFAULT_REGISTER_MAP)
        self.slave_id = int(slave_id)
        self.timeout = max(0.1, float(timeout))
        self.sock: socket.socket | None = None
        self._transaction_id = 0

    def open(self) -> None:
        self._connect()

    def _connect(self) -> None:
        host, port_text = (
            self.endpoint.rsplit(":", 1)
            if ":" in self.endpoint
            else (self.endpoint, str(DEFAULT_PLC_PORT))
        )
        self.sock = socket.create_connection((host, int(port_text)), timeout=2.0)
        self.sock.settimeout(self.timeout)

    def _reconnect(self) -> None:
        self.close()
        try:
            self._connect()
        except OSError:
            return

    def _recv_exact(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def _read_holding_registers(self, start_address: int, quantity: int) -> list[int]:
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        request = struct.pack(
            ">HHHBBHH", self._transaction_id, 0, 6,
            self.slave_id, 3, int(start_address), int(quantity),
        )
        self.sock.sendall(request)
        header = self._recv_exact(7)
        if len(header) < 7:
            raise IOError("Modbus TCP 响应头不完整")
        _, _, length, _unit = struct.unpack(">HHHB", header)
        pdu = self._recv_exact(length)
        if len(pdu) < 2:
            raise IOError("Modbus TCP 响应PDU不完整")
        function_code = pdu[0]
        if function_code & 0x80:
            exception_code = pdu[1] if len(pdu) > 1 else 0
            raise IOError(
                f"Modbus 异常响应: 功能码={function_code:#x}, "
                f"异常码={exception_code:#x}"
            )
        byte_count = pdu[1]
        register_data = pdu[2:2 + byte_count]
        return [
            struct.unpack(">H", register_data[index:index + 2])[0]
            for index in range(0, len(register_data), 2)
            if len(register_data[index:index + 2]) == 2
        ]

    @staticmethod
    def _registers_to_float(low_word: int, high_word: int) -> float:
        value = struct.unpack("<f", struct.pack("<HH", low_word, high_word))[0]
        return 0.0 if not math.isfinite(value) else round(float(value), 2)

    def _read_registers(self) -> dict[int, int]:
        addresses = sorted({
            offset
            for address in self.register_map.values()
            for offset in (address, address + 1)
        })
        registers: dict[int, int] = {}
        cursor = 0
        while cursor < len(addresses):
            group_start = addresses[cursor]
            group_end = group_start
            next_cursor = cursor
            while (
                next_cursor < len(addresses)
                and addresses[next_cursor] - group_start < 120
            ):
                group_end = addresses[next_cursor]
                next_cursor += 1
            values = self._read_holding_registers(
                group_start, group_end - group_start + 1
            )
            registers.update({
                group_start + offset: value
                for offset, value in enumerate(values)
            })
            cursor = next_cursor
        return registers

    def read_sample(self) -> dict[str, float] | None:
        if self.sock is None:
            self._reconnect()
        if self.sock is None:
            return None
        try:
            registers = self._read_registers()
        except (OSError, IOError):
            self._reconnect()
            if self.sock is None:
                return None
            try:
                registers = self._read_registers()
            except (OSError, IOError):
                return None
        result: dict[str, float] = {}
        for name, address in self.register_map.items():
            low, high = registers.get(address), registers.get(address + 1)
            if low is not None and high is not None:
                result[name] = self._registers_to_float(low, high)
        return result or None

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class AbbRobotDriver(SampleDriver):
    def __init__(self, endpoint: str = DEFAULT_ABB_IP,
                 username: str = DEFAULT_ABB_USER,
                 password: str = DEFAULT_ABB_PASSWORD,
                 timeout: float = 1.5) -> None:
        if str(endpoint).startswith("http"):
            self.base_url = str(endpoint).rstrip("/")
        else:
            self.base_url = f"http://{str(endpoint).split(':')[0]}"
        self.username = username
        self.password = password
        self.timeout = max(0.1, float(timeout))
        self._last_time: float | None = None
        self._last_x: float | None = None
        self._last_y: float | None = None
        self._last_z: float | None = None

    def open(self) -> None:
        return

    def _fetch_position(self) -> tuple[float, float, float]:
        credentials = base64.b64encode(
            f"{self.username}:{self.password}".encode("utf-8")
        ).decode("ascii")
        request = urllib.request.Request(
            self.base_url + ABB_ROBTARGET_PATH,
            headers={
                "Authorization": f"Basic {credentials}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        state = payload["_embedded"]["_state"][0]
        return float(state["x"]), float(state["y"]), float(state["z"])

    @staticmethod
    def _find_json_value(value: Any) -> Any:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in {"value", "_value"}:
                    return item
                found = AbbRobotDriver._find_json_value(item)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = AbbRobotDriver._find_json_value(item)
                if found is not None:
                    return found
        return None

    def read_rapid_symbol(
        self,
        task: str,
        module: str,
        variable: str,
    ) -> str:
        credentials = base64.b64encode(
            f"{self.username}:{self.password}".encode("utf-8")
        ).decode("ascii")
        path = "/".join(
            urllib.parse.quote(str(part), safe="")
            for part in (task, module, variable)
        )
        request = urllib.request.Request(
            f"{self.base_url}{ABB_RAPID_SYMBOL_BASE_PATH}/{path}?json=1",
            headers={
                "Authorization": f"Basic {credentials}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw)
        value = self._find_json_value(payload)
        if value is None:
            raise ValueError(f"ABB变量{variable}响应中没有value字段")
        return str(value)

    def read_sample(self) -> dict[str, float] | None:
        try:
            x, y, z = self._fetch_position()
        except Exception:
            return None
        now = time.time()
        speed = 0.0
        if self._last_time is not None and self._last_x is not None:
            delta_time = now - self._last_time
            if delta_time > 0:
                speed = round(math.sqrt(
                    (x - self._last_x) ** 2
                    + (y - self._last_y) ** 2
                    + (z - self._last_z) ** 2
                ) / delta_time, 2)
        self._last_time, self._last_x, self._last_y, self._last_z = now, x, y, z
        return {"ABB_X": x, "ABB_Y": y, "ABB_Z": z, "线速度": speed}


def _parse_roi(value: Any) -> tuple[int, int, int, int] | None:
    if value in (None, "", []):
        return None
    pieces = (
        [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, str) else list(value)
        if isinstance(value, (list, tuple)) else []
    )
    if len(pieces) != 4:
        return None
    try:
        return tuple(int(float(part)) for part in pieces)
    except (TypeError, ValueError):
        return None


class UvcThermalDriver(SampleDriver):
    """BSV UVC native DLL reader; produces a calibrated ROI temperature."""

    def __init__(self, dll_path: str = "", roi: Any = None, poll_retries: int = 3) -> None:
        self.dll_path = str(dll_path or "").strip()
        self.roi = _parse_roi(roi)
        self.poll_retries = max(1, int(poll_retries))
        self.dll = None
        self.yuv_buffer = (ctypes.c_ubyte * BSV_UVC_YUV_BYTES)()
        self.temp_buffer = (ctypes.c_ushort * BSV_UVC_PIXELS)()

    def _find_dll(self) -> Path:
        candidates = []
        if self.dll_path:
            candidates.append(Path(self.dll_path))
        candidates.extend([
            _NATIVE_DLL_DIR / BSV_UVC_DLL_NAME,
            APP_DIR / BSV_UVC_DLL_NAME,
            APP_DIR.parent / BSV_UVC_DLL_NAME,
            Path.cwd() / BSV_UVC_DLL_NAME,
            Path(BSV_UVC_DLL_NAME),
        ])
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate.resolve()
            except OSError:
                continue
        raise FileNotFoundError(
            "未找到 BsvUvcNative.dll；请将该 DLL 放到程序目录或通过接口配置 dll_path 指定路径"
        )

    def _bind_api(self) -> None:
        if self.dll is None:
            raise RuntimeError("BSV UVC DLL尚未加载")
        self.dll.BsvSetTempRangeCode.argtypes = [ctypes.c_int]
        self.dll.BsvSetTempRangeCode.restype = ctypes.c_int
        self.dll.BsvOpen.argtypes = []
        self.dll.BsvOpen.restype = ctypes.c_int
        self.dll.BsvStart.argtypes = []
        self.dll.BsvStart.restype = ctypes.c_int
        self.dll.BsvHasFrame.argtypes = []
        self.dll.BsvHasFrame.restype = ctypes.c_int
        self.dll.BsvGetFrame.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,
            ctypes.POINTER(ctypes.c_ushort), ctypes.c_int,
        ]
        self.dll.BsvGetFrame.restype = ctypes.c_int
        self.dll.BsvGetLastError.argtypes = []
        self.dll.BsvGetLastError.restype = ctypes.c_int
        self.dll.BsvStop.argtypes = []
        self.dll.BsvStop.restype = None
        self.dll.BsvClose.argtypes = []
        self.dll.BsvClose.restype = None

    def open(self) -> None:
        self.dll = ctypes.CDLL(str(self._find_dll()))
        self._bind_api()
        if self.dll.BsvSetTempRangeCode(BSV_UVC_TEMP_RANGE_CODE) < 0:
            raise IOError("BSV UVC 设置温度量程失败")
        result = self.dll.BsvOpen()
        if result <= 0:
            error = self.dll.BsvGetLastError()
            self.dll = None
            raise IOError(f"BSV UVC 打开设备失败：code={result}, lastError={error}")
        result = self.dll.BsvStart()
        if result <= 0:
            error = self.dll.BsvGetLastError()
            self.close()
            raise IOError(f"BSV UVC 启动视频流失败：code={result}, lastError={error}")

    def _roi_average_temperature(self) -> float:
        x1, y1, x2, y2 = self.roi or (0, 0, BSV_UVC_WIDTH, BSV_UVC_HEIGHT)
        x1 = max(0, min(int(x1), BSV_UVC_WIDTH - 1))
        x2 = max(x1 + 1, min(int(x2), BSV_UVC_WIDTH))
        y1 = max(0, min(int(y1), BSV_UVC_HEIGHT - 1))
        y2 = max(y1 + 1, min(int(y2), BSV_UVC_HEIGHT))
        total = 0.0
        count = 0
        for row in range(y1, y2):
            base = row * BSV_UVC_WIDTH
            for column in range(x1, x2):
                total += self.temp_buffer[base + column] / BSV_UVC_TEMP_FORMULA_SCALE + BSV_UVC_TEMP_FORMULA_OFFSET
                count += 1
        return round(total / count, 2) if count else 0.0

    def read_sample(self) -> dict[str, float] | None:
        if self.dll is None:
            return None
        for _ in range(self.poll_retries):
            if self.dll.BsvHasFrame() <= 0:
                return None
            result = self.dll.BsvGetFrame(
                self.yuv_buffer, BSV_UVC_YUV_BYTES,
                self.temp_buffer, BSV_UVC_PIXELS,
            )
            if result > 0:
                return {"ROI平均温度": self._roi_average_temperature()}
        return None

    def close(self) -> None:
        if self.dll is None:
            return
        try:
            self.dll.BsvStop()
        except Exception:
            pass
        try:
            self.dll.BsvClose()
        except Exception:
            pass
        self.dll = None


class RtspThermalDriver(SampleDriver):
    def __init__(self, endpoint: str = DEFAULT_RTSP_URL, roi: Any = None,
                 temp_scale: float | None = None, temp_offset: float = 0.0,
                 record_dir: str = "", record_fps: float = 15.0) -> None:
        self.endpoint = str(endpoint or DEFAULT_RTSP_URL)
        self.roi = _parse_roi(roi)
        self.temp_scale = float(temp_scale) if temp_scale not in (None, "", 0) else None
        self.temp_offset = float(temp_offset or 0.0)
        self.record_dir = str(record_dir or "").strip()
        self.record_fps = max(1.0, float(record_fps or 15.0))
        self.capture = None
        self.writer = None

    def open(self) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("RTSP热像仪需要opencv-python") from exc
        self.capture = cv2.VideoCapture(self.endpoint)
        if not self.capture.isOpened():
            self.capture.release()
            self.capture = None
            raise IOError(f"无法打开 RTSP 视频流：{self.endpoint}")

    def read_sample(self) -> dict[str, float] | None:
        if self.capture is None:
            return None
        import cv2
        ok, frame = self.capture.read()
        if not ok or frame is None or self.temp_scale is None:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape[:2]
        x1, y1, x2, y2 = self.roi or (0, 0, width, height)
        x1, x2 = max(0, min(int(x1), width - 1)), max(1, min(int(x2), width))
        y1, y2 = max(0, min(int(y1), height - 1)), max(1, min(int(y2), height))
        return {"ROI平均温度": round(self.temp_offset + float(gray[y1:y2, x1:x2].mean()) * self.temp_scale, 2)}

    def close(self) -> None:
        if self.writer is not None:
            try:
                self.writer.release()
            except Exception:
                pass
            self.writer = None
        if self.capture is not None:
            try:
                self.capture.release()
            except Exception:
                pass
            self.capture = None


class M3232PressureDriver(SampleDriver):
    """Serial pressure reader with variable-size JSON matrix compatibility.

    The supplied vendor manual confirms 115200 baud and automatic X/Y/zero
    calibration, but does not publish the packet framing.  JSON arrays and
    ``{"matrix": ...}`` frames are therefore supported as a documented
    simulator/probe interchange format; hardware framing remains configurable
    after a raw serial capture.
    """

    def __init__(
        self,
        endpoint: str = "COM8",
        baudrate: int = M3232_BAUDRATE,
        matrix_rows: int = 0,
        matrix_cols: int = 0,
    ) -> None:
        self.endpoint = str(endpoint or "COM8").strip()
        self.baudrate = int(baudrate or M3232_BAUDRATE)
        self.matrix_rows = max(0, int(matrix_rows or 0))
        self.matrix_cols = max(0, int(matrix_cols or 0))
        self.matrix_shape: tuple[int, int] = (0, 0)
        self.serial = None
        self.rx_buffer = ""
        self.last_data_time: float | None = None
        self.latest_pressure = 0.0
        self.latest_pressure_peak = 0.0
        self.latest_contact_area = 0
        self.latest_valid_fraction = 0.0
        self.median_window: list[float] = []

    def open(self) -> None:
        import serial
        self.serial = serial.Serial(
            self.endpoint, self.baudrate, bytesize=8,
            parity="N", stopbits=1, timeout=0,
        )
        self.rx_buffer = ""
        self.last_data_time = None
        self.serial.write(b"begin\n")

    def _extract_frames(self) -> list[list[list[float]]]:
        """Extract complete rectangular JSON matrices from a fragmented stream."""
        frames: list[list[list[float]]] = []
        decoder = json.JSONDecoder()
        while True:
            starts = [index for index in (self.rx_buffer.find("["), self.rx_buffer.find("{")) if index >= 0]
            start = min(starts) if starts else -1
            if start < 0:
                self.rx_buffer = self.rx_buffer[-1024:]
                break
            try:
                payload, end = decoder.raw_decode(self.rx_buffer[start:])
            except json.JSONDecodeError:
                self.rx_buffer = self.rx_buffer[start:]
                break
            self.rx_buffer = self.rx_buffer[start + end:]
            if isinstance(payload, dict):
                for key in ("matrix", "data", "values"):
                    if key in payload:
                        payload = payload[key]
                        break
            if not isinstance(payload, list) or not payload or not all(isinstance(row, list) for row in payload):
                continue
            width = len(payload[0])
            if width <= 0 or not all(len(row) == width for row in payload):
                continue
            if self.matrix_rows and len(payload) != self.matrix_rows:
                continue
            if self.matrix_cols and width != self.matrix_cols:
                continue
            try:
                matrix = [[float(value) for value in row] for row in payload]
            except (TypeError, ValueError):
                continue
            self.matrix_shape = (len(matrix), width)
            frames.append(matrix)
        return frames

    @staticmethod
    def _frame_metrics(matrix: list[list[float]]) -> dict[str, float | int]:
        values = [float(value) for row in matrix for value in row]
        finite = [value for value in values if math.isfinite(value)]
        # Zero is the device's no-signal/background value.  Keep it in the
        # matrix for shape/peak calculations but exclude it from the quality
        # ratio so a disconnected matrix is distinguishable from valid data.
        valid = [value for value in finite if value > 0.0]
        active = sorted(value for value in finite if value > 5.0)
        trim_count = max(1, int(len(active) * 0.9)) if active else 0
        return {
            "pressure_total": round(sum(active[:trim_count]), 2) if active else 0.0,
            "pressure_peak": round(max(finite), 2) if finite else 0.0,
            "contact_area": int(len(active)),
            "valid_fraction": round(len(valid) / len(values), 6) if values else 0.0,
        }

    @staticmethod
    def _frame_pressure(matrix: list[list[float]]) -> float:
        """Backward-compatible scalar pressure helper."""
        return float(M3232PressureDriver._frame_metrics(matrix)["pressure_total"])

    def _update_matrix_metrics(self, matrix: list[list[float]]) -> float:
        metrics = self._frame_metrics(matrix)
        value = float(metrics["pressure_total"])
        self.latest_pressure_peak = float(metrics["pressure_peak"])
        self.latest_contact_area = int(metrics["contact_area"])
        self.latest_valid_fraction = float(metrics["valid_fraction"])
        return value

    def read_sample(self) -> dict[str, float] | None:
        if self.serial is None:
            return None
        pending = int(getattr(self.serial, "in_waiting", 0) or 0)
        if pending > 0:
            chunk = self.serial.read(min(pending, 16384))
            if chunk:
                self.last_data_time = time.time()
                self.rx_buffer += chunk.decode("utf-8", errors="ignore")
        for matrix in self._extract_frames():
            value = self._update_matrix_metrics(matrix)
            self.median_window.append(value)
            self.median_window = self.median_window[-9:]
            ordered = sorted(self.median_window)
            self.latest_pressure = ordered[len(ordered) // 2]
        if self.last_data_time is None or time.time() - self.last_data_time > 2.0:
            return None
        return {
            "压力": round(float(self.latest_pressure), 2),
            "压力峰值": round(float(self.latest_pressure_peak), 2),
            "压力接触面积": int(self.latest_contact_area),
            "压力有效像素比例": round(float(self.latest_valid_fraction), 6),
        }

    def close(self) -> None:
        if self.serial is None:
            return
        try:
            self.serial.write(b"end\n")
        except Exception:
            pass
        try:
            self.serial.close()
        finally:
            self.serial = None


class MultiInterfaceDriver(SampleDriver):
    """Read several physical interfaces and merge one time slice."""
    def __init__(self, configs: list[dict[str, Any]], selected_sensors: list[str], source_file: str = "", assignments: dict[str, list[str]] | None = None) -> None:
        self.configs = [item for item in configs if item.get("enabled", True)]
        self.selected_sensors = selected_sensors
        self.source_file = source_file
        self.assignments = assignments or {}
        self.drivers: list[SampleDriver] = []

    def open(self) -> None:
        self.drivers = []
        try:
            for item in self.configs:
                driver_name = str(item.get("driver") or "serial_json")
                role = str(item.get("role") or "other")
                channel_map = item.get("channel_map") if isinstance(item.get("channel_map"), dict) else {}
                if driver_name == "serial_json":
                    driver = SerialJsonDriver(str(item.get("endpoint") or ""), int(item.get("baudrate") or 115200), role, channel_map, float(item.get("timeout") or 0.05))
                elif driver_name == "smrf_hid":
                    channel_types = item.get("channel_types")
                    driver = SmrfHidDriver(
                        str(item.get("endpoint") or "SMRFCT08B"),
                        channel_types if isinstance(channel_types, (list, tuple)) else None,
                    )
                elif driver_name == "tcp_json":
                    driver = TcpJsonDriver(str(item.get("endpoint") or ""), role, channel_map, float(item.get("timeout") or 0.05))
                elif driver_name == "modbus_tcp":
                    register_map = item.get("register_map") if isinstance(item.get("register_map"), dict) else None
                    driver = ModbusTcpDriver(
                        str(item.get("endpoint") or f"{DEFAULT_PLC_IP}:{DEFAULT_PLC_PORT}"),
                        register_map=register_map,
                        slave_id=int(item.get("slave_id") or DEFAULT_PLC_SLAVE_ID),
                        timeout=float(item.get("timeout") or 1.0),
                    )
                elif driver_name == "abb_robot":
                    driver = AbbRobotDriver(
                        str(item.get("endpoint") or DEFAULT_ABB_IP),
                        str(item.get("username") or DEFAULT_ABB_USER),
                        str(item.get("password") or DEFAULT_ABB_PASSWORD),
                        float(item.get("timeout") or 1.5),
                    )
                elif driver_name == "uvc_thermal":
                    driver = UvcThermalDriver(
                        str(item.get("dll_path") or ""), item.get("roi"),
                    )
                elif driver_name == "rtsp_thermal":
                    scale = item.get("temp_scale")
                    driver = RtspThermalDriver(
                        str(item.get("endpoint") or DEFAULT_RTSP_URL),
                        item.get("roi"),
                        float(scale) if scale not in (None, "") else None,
                        float(item.get("temp_offset") or 0.0),
                        str(item.get("record_dir") or ""),
                    )
                elif driver_name == "m3232_pressure":
                    driver = M3232PressureDriver(
                        str(item.get("endpoint") or "COM8"),
                        int(item.get("baudrate") or M3232_BAUDRATE),
                        int(item.get("matrix_rows") or 0),
                        int(item.get("matrix_cols") or 0),
                    )
                elif driver_name == "simulator":
                    driver = SimulatorDriver(Path(self.source_file or DEFAULT_SIMULATOR_FILE), self.selected_sensors)
                else:
                    raise ValueError(f"unsupported interface driver: {driver_name}")
                driver.open()
                self.drivers.append(driver)
        except Exception:
            self.close()
            raise

    def read_sample(self) -> dict[str, float] | None:
        merged: dict[str, float] = {}
        for index, driver in enumerate(self.drivers):
            sample = driver.read_sample()
            if sample:
                interface_id = self.configs[index].get("id") if index < len(self.configs) else None
                allowed = self.assignments.get(str(interface_id))
                if allowed is not None:
                    sample = {name: value for name, value in sample.items() if name in allowed}
                merged.update(sample)
        return merged or None

    def close(self) -> None:
        for driver in self.drivers:
            try:
                driver.close()
            except Exception:
                pass
        self.drivers = []


def build_driver(config: AcquisitionConfig) -> SampleDriver:
    if config.acquisition_mode == "simulation":
        if config.simulation_source_type == "mysql":
            return MySQLSimulatorDriver(
                {
                    "host": config.simulation_mysql_host,
                    "port": config.simulation_mysql_port,
                    "user": config.simulation_mysql_user,
                    "password": config.simulation_mysql_password,
                    "database": config.simulation_mysql_database,
                    "query": config.simulation_mysql_query,
                },
                list(config.selected_sensors or []),
            )
        path = Path(config.simulation_source_path or config.source_file)
        if config.simulation_source_type == "folder_csv":
            return FolderCsvSimulatorDriver(path, list(config.selected_sensors or []))
        return SimulatorDriver(path, list(config.selected_sensors or []))
    enabled_interfaces = [item for item in (config.interfaces or []) if item.get("enabled", True)]
    if enabled_interfaces:
        return MultiInterfaceDriver(
            enabled_interfaces,
            list(config.selected_sensors or []),
            config.source_file,
            config.interface_channel_assignments,
        )
    if config.driver == "simulator":
        path = (
            Path(config.source_file)
            if config.source_file
            else DEFAULT_SIMULATOR_FILE
        )
        return SimulatorDriver(path, list(config.selected_sensors or []))
    if config.driver == "serial_json":
        return SerialJsonDriver(config.endpoint, config.baudrate)
    if config.driver == "smrf_hid":
        return SmrfHidDriver(config.endpoint or "SMRFCT08B")
    if config.driver == "tcp_json":
        return TcpJsonDriver(config.endpoint)
    if config.driver == "modbus_tcp":
        return ModbusTcpDriver(config.endpoint or f"{DEFAULT_PLC_IP}:{DEFAULT_PLC_PORT}")
    if config.driver == "abb_robot":
        return AbbRobotDriver(config.endpoint or DEFAULT_ABB_IP)
    if config.driver == "uvc_thermal":
        return UvcThermalDriver()
    if config.driver == "rtsp_thermal":
        return RtspThermalDriver(config.endpoint or DEFAULT_RTSP_URL)
    if config.driver == "m3232_pressure":
        return M3232PressureDriver(
            config.endpoint or "COM8",
            config.baudrate or M3232_BAUDRATE,
            int(getattr(config, "matrix_rows", 0) or 0),
            int(getattr(config, "matrix_cols", 0) or 0),
        )
    raise ValueError(f"不支持的采集驱动：{config.driver}")


def _parameter_result_template() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": label,
            "unit": unit,
            "ok": False,
            "value": None,
            "source": "",
            "message": "尚未读取",
        }
        for key, label, unit in PROCESS_PARAMETER_DEFINITIONS
    ]


def _process_parameter_payload(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    values = {
        str(item["key"]): float(item["value"])
        for item in parameters
        if item.get("ok") and item.get("value") is not None
    }
    return {
        "ok": bool(values),
        "complete": len(values) == len(PROCESS_PARAMETER_DEFINITIONS),
        "values": values,
        "parameters": parameters,
    }


def _read_simulation_process_parameters(config: AcquisitionConfig) -> dict[str, Any]:
    parameters = _parameter_result_template()
    if config.simulation_source_type == "mysql":
        for item in parameters:
            item["message"] = "MySQL模拟源暂不支持自动读取工艺参数"
        return _process_parameter_payload(parameters)
    source = Path(config.simulation_source_path or config.source_file)
    if config.simulation_source_type == "folder_csv":
        files = sorted(source.rglob("*.csv")) if source.is_dir() else []
        if not files:
            raise FileNotFoundError(f"模拟采集文件夹不包含 CSV：{source}")
    else:
        if not source.is_file():
            raise FileNotFoundError(f"模拟数据文件不存在：{source}")
        files = [source]
    frames = []
    for file in files:
        try:
            frames.append(pd.read_csv(file, encoding="utf-8-sig"))
        except UnicodeDecodeError:
            frames.append(pd.read_csv(file, encoding="gb18030"))
    frame = pd.concat(frames, ignore_index=True, sort=False)
    for item in parameters:
        key = str(item["key"])
        if key not in frame.columns:
            item["message"] = f"模拟数据缺少列 {key}"
            continue
        numeric = pd.to_numeric(frame[key], errors="coerce")
        finite = numeric[numeric.map(lambda value: math.isfinite(float(value)) if pd.notna(value) else False)]
        if finite.empty:
            item["message"] = f"模拟数据列 {key} 没有有效数值"
            continue
        item.update(
            ok=True,
            value=float(finite.iloc[0]),
            source="模拟数据文件",
            message=f"已从 {source.name or '模拟数据'} 读取",
        )
    return _process_parameter_payload(parameters)


def _default_read_plc_registers(
    interface: dict[str, Any], address: int, quantity: int
) -> list[int]:
    driver = ModbusTcpDriver(
        str(interface.get("endpoint") or f"{DEFAULT_PLC_IP}:{DEFAULT_PLC_PORT}"),
        register_map={},
        slave_id=int(interface.get("slave_id") or DEFAULT_PLC_SLAVE_ID),
        timeout=float(interface.get("timeout") or 1.0),
    )
    try:
        driver.open()
        return driver._read_holding_registers(int(address), int(quantity))
    finally:
        driver.close()


def _default_read_abb_symbol(
    interface: dict[str, Any], task: str, module: str, variable: str
) -> str:
    driver = AbbRobotDriver(
        str(interface.get("endpoint") or DEFAULT_ABB_IP),
        str(interface.get("username") or DEFAULT_ABB_USER),
        str(interface.get("password") or DEFAULT_ABB_PASSWORD),
        timeout=float(interface.get("timeout") or 1.5),
    )
    return driver.read_rapid_symbol(task, module, variable)


def _parse_abb_speeddata(raw: Any) -> float:
    match = re.search(r"\[\s*([-+]?\d+(?:\.\d+)?)", str(raw or ""))
    if not match:
        raise ValueError(f"ABB铺放速度返回格式无效：{str(raw or '')[:120]}")
    value = float(match.group(1))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("ABB铺放速度不是有效正数")
    return value


def read_real_process_parameters(
    interfaces: list[dict[str, Any]],
    *,
    read_plc_registers=_default_read_plc_registers,
    read_abb_symbol=_default_read_abb_symbol,
) -> dict[str, Any]:
    parameters = _parameter_result_template()
    by_key = {str(item["key"]): item for item in parameters}
    enabled = [dict(item) for item in (interfaces or []) if item.get("enabled", True)]
    plc = next(
        (item for item in enabled if item.get("role") == "plc" or item.get("driver") == "modbus_tcp"),
        None,
    )
    robot = next(
        (item for item in enabled if item.get("role") == "robot" or item.get("driver") == "abb_robot"),
        None,
    )
    pressure = by_key["initial_compaction_force_N"]
    pressure["message"] = "未配置压实力设定值读取地址；已保留原输入值"
    temperature = by_key["temperature_setpoint_C"]
    temperature["message"] = "未配置温度设定值读取地址；已保留原输入值"

    angle = by_key["pid_angle_deg"]
    if plc is None:
        angle["message"] = "未找到已启用的PLC接口；已保留原输入值"
    else:
        try:
            registers = read_plc_registers(plc, PLC_PID_ANGLE_REGISTER, 1)
            if not registers:
                raise ValueError("PLC未返回DT20寄存器")
            angle.update(
                ok=True,
                value=float(registers[0]) / 10.0,
                source="PLC DT20",
                message="已读取PLC PID角度设定值",
            )
        except Exception as exc:
            angle["message"] = f"PLC PID角度读取失败：{exc}；已保留原输入值"

    speed = by_key["placement_speed_mm_s"]
    if robot is None:
        speed["message"] = "未找到已启用的ABB接口；已保留原输入值"
    else:
        try:
            raw = read_abb_symbol(
                robot, ABB_PROCESS_TASK, ABB_PROCESS_MODULE, ABB_SPEED_VARIABLE
            )
            speed.update(
                ok=True,
                value=_parse_abb_speeddata(raw),
                source="ABB RAPID zMovespeed",
                message="已读取ABB铺放速度设定值",
            )
        except Exception as exc:
            speed["message"] = f"ABB铺放速度读取失败：{exc}；已保留原输入值"
    return _process_parameter_payload(parameters)


class AcquisitionManager:
    def __init__(self, capture_root: Path = DEFAULT_CAPTURE_ROOT) -> None:
        self.capture_root = capture_root
        self.capture_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.driver: SampleDriver | None = None
        self.config: AcquisitionConfig | None = None
        self.rows: deque[dict[str, Any]] = deque(maxlen=200000)
        self.timestamps: deque[float] = deque(maxlen=200000)
        self.total_sample_count = 0
        self.sensor_received = {name: 0 for name in ALL_SENSOR_COLUMNS}
        self.sensor_last_time = {name: None for name in ALL_SENSOR_COLUMNS}
        self.channel_observed = {name: 0 for name in ALL_SENSOR_COLUMNS}
        self.channel_last_observed = {name: None for name in ALL_SENSOR_COLUMNS}
        self.last_error = ""
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.session_dir: Path | None = None
        self.raw_path: Path | None = None
        self.raw_work_path: Path | None = None
        self.timestamp_path: Path | None = None
        self.timestamp_work_path: Path | None = None
        self.capture_record_dir: Path | None = None
        self.full_specimen_path: Path | None = None
        self.completed_layers: list[int] = []
        self.session_stamp = ""
        self.capture_uuid = ""
        self.operator_specimen_id = ""
        self.session_metadata_path: Path | None = None
        self.archived_previous_session: str | None = None
        self.replaced_layer_archive: str | None = None
        self.failed_capture_archive: str | None = None
        self.pending_previous_archive = False
        self.previous_session_metadata: dict[str, Any] = {}
        self.pending_session_metadata: dict[str, Any] = {}
        self.specimen_folder_name = ""
        self.capture_saved = False
        self.save_enabled = False
        self.save_status: dict[str, Any] = check_capture_save_root(self.capture_root)
        self.finalization_complete = False
        self.last_data_quality: dict[str, Any] | None = None
        self.last_file_integrity: dict[str, Any] | None = None
        self.mysql_status: dict[str, Any] = {
            "enabled": False,
            "ok": False,
            "saved_rows": 0,
        }
        self._latest_check_result: dict[str, Any] = {
            "checked_at": None,
            "interfaces": [],
            "sensors": [],
        }

    @staticmethod
    def _public_config(config: AcquisitionConfig) -> dict[str, Any]:
        payload = asdict(config)
        # Never expose or write the database password to a browser or manifest.
        payload["mysql_password"] = "***" if config.mysql_password else ""
        payload["mysql_local_password"] = (
            "***" if config.mysql_local_password else ""
        )
        payload["simulation_mysql_password"] = (
            "***" if config.simulation_mysql_password else ""
        )
        return payload

    @staticmethod
    def available_drivers() -> list[dict[str, str]]:
        return [
            {"id": "simulator", "label": "模拟采集（CSV数据源）"},
            {"id": "serial_json", "label": "串口 JSON Lines"},
            {"id": "smrf_hid", "label": "SMRF八通道温度巡检仪（USB HID）"},
            {"id": "tcp_json", "label": "TCP JSON Lines"},
            {"id": "modbus_tcp", "label": "Modbus TCP（松下PLC）"},
            {"id": "abb_robot", "label": "ABB机器人 RWS"},
            {"id": "uvc_thermal", "label": "BSV UVC热像仪（ROI温度）"},
            {"id": "rtsp_thermal", "label": "IP热像仪 RTSP"},
            {"id": "m3232_pressure", "label": "M3232薄膜压力（115200，矩阵自动识别）"},
        ]

    def read_process_parameters(self, config: AcquisitionConfig) -> dict[str, Any]:
        if config.acquisition_mode == "simulation":
            return _read_simulation_process_parameters(config)
        return read_real_process_parameters(list(config.interfaces or []))

    @staticmethod
    def discover_interfaces() -> dict[str, Any]:
        """Discover local serial interfaces and provide two editable defaults.

        Interface discovery must not depend on a sensor actively streaming
        data.  ``pyserial`` is the preferred provider, but the portable build
        also falls back to the Windows serial-device registry when the driver
        package is unavailable.  This lets the UI distinguish an existing
        COM interface from a connected sensor (which is checked separately by
        :meth:`test_connection`).
        """
        ports: list[dict[str, Any]] = []
        physical_interfaces: list[dict[str, Any]] = []
        error = ""
        try:
            from serial.tools import list_ports
            for item in list_ports.comports():
                ports.append({
                    "id": str(item.device),
                    "endpoint": str(item.device),
                    "description": str(item.description or ""),
                    "manufacturer": str(item.manufacturer or ""),
                    "vid": item.vid,
                    "pid": item.pid,
                })
                physical_interfaces.append({
                    "id": f"serial:{str(item.device).upper()}",
                    "kind": "serial",
                    "protocol": "serial",
                    "endpoint": str(item.device),
                    "label": f"串口 {item.device}",
                    "description": str(item.description or ""),
                    "manufacturer": str(item.manufacturer or ""),
                    "vid": item.vid,
                    "pid": item.pid,
                    "detected": True,
                })
        except Exception as exc:
            error = str(exc)
            # Keep discovery useful in a minimal/frozen Windows runtime even
            # if pyserial was not bundled.  The registry reports COM devices
            # without opening them and therefore does not require a sensor.
            if sys.platform.startswith("win"):
                try:
                    import winreg

                    key_path = r"HARDWARE\\DEVICEMAP\\SERIALCOMM"
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                        index = 0
                        while True:
                            try:
                                value_name, endpoint, _ = winreg.EnumValue(key, index)
                            except OSError:
                                break
                            endpoint = str(endpoint)
                            ports.append(
                                {
                                    "id": endpoint,
                                    "endpoint": endpoint,
                                    "description": str(value_name or "Windows serial interface"),
                                    "manufacturer": "",
                                    "vid": None,
                                    "pid": None,
                                }
                            )
                            physical_interfaces.append({
                                "id": f"serial:{endpoint.upper()}",
                                "kind": "serial", "protocol": "serial",
                                "endpoint": endpoint,
                                "label": f"串口 {endpoint}",
                                "description": str(value_name or "Windows serial interface"),
                                "manufacturer": "", "vid": None, "pid": None,
                                "detected": True,
                            })
                            index += 1
                except Exception as registry_exc:
                    error = f"{error}; Windows接口枚举失败：{registry_exc}"
        smrf_devices: list[dict[str, Any]] = []
        try:
            smrf_objects = enumerate_smrf_hid_devices()
            smrf_devices = [
                {
                    "id": item.label,
                    "endpoint": item.label,
                    "product": item.product,
                    "serial": item.serial,
                    "vid": item.vendor_id,
                    "pid": item.product_id,
                    "path": item.path,
                }
                for item in smrf_objects
            ]
            physical_interfaces.extend({
                "id": f"hid:{item.path or item.serial or item.label}",
                "kind": "usb_hid", "protocol": "smrf_hid",
                "endpoint": item.label, "label": f"SMRF HID · {item.label}",
                "path": item.path, "serial": item.serial,
                "vid": item.vendor_id, "pid": item.product_id,
                "detected": True,
            } for item in smrf_objects)
        except Exception as exc:
            error = f"{error}; SMRF HID枚举失败：{exc}" if error else f"SMRF HID枚举失败：{exc}"
        plc_reachable = False
        try:
            sock = socket.create_connection((DEFAULT_PLC_IP, DEFAULT_PLC_PORT), timeout=1.0)
            sock.close()
            plc_reachable = True
        except OSError:
            pass
        abb_reachable = False
        try:
            sock = socket.create_connection((DEFAULT_ABB_IP, 80), timeout=1.0)
            sock.close()
            abb_reachable = True
        except OSError:
            pass
        uvc_dll_found = any(
            candidate.is_file()
            for candidate in (
                _NATIVE_DLL_DIR / BSV_UVC_DLL_NAME,
                APP_DIR / BSV_UVC_DLL_NAME,
                APP_DIR.parent / BSV_UVC_DLL_NAME,
                Path.cwd() / BSV_UVC_DLL_NAME,
            )
        )
        if uvc_dll_found:
            # The native DLL confirms that the vendor driver is installed; a
            # camera instance still needs protocol/data verification.
            physical_interfaces.append({
                "id": "uvc:bsv",
                "kind": "usb_uvc", "protocol": "uvc",
                "endpoint": "BSV UVC (WinUSB)",
                "label": "BSV UVC热像仪（驱动已安装，待数据验证）",
                "detected": False, "driver_available": True,
            })
        # Keep every standard logical slot assignable on startup.  These
        # placeholders never claim that a sensor is connected; they only hold
        # the protocol-specific USB/serial binding until a real data check is
        # performed.
        if not any(item.get("kind") == "usb_hid" for item in physical_interfaces):
            physical_interfaces.append({
                "id": "usb_hid:auto", "kind": "usb_hid",
                "protocol": "smrf_hid", "endpoint": "SMRFCT08B",
                "label": "USB HID（待识别，启动后检查）",
                "detected": False, "auto_assignable": True,
            })
        if not any(item.get("kind") == "usb_uvc" for item in physical_interfaces):
            physical_interfaces.append({
                "id": "usb_uvc:auto", "kind": "usb_uvc",
                "protocol": "uvc", "endpoint": "BSV UVC (WinUSB)",
                "label": "USB/UVC（待识别，启动后检查）",
                "detected": False, "auto_assignable": True,
            })
        try:
            import psutil

            for name, addresses in psutil.net_if_addrs().items():
                ipv4 = [
                    str(address.address)
                    for address in addresses
                    if getattr(address, "family", None) == socket.AF_INET
                    and not str(address.address).startswith("127.")
                ]
                if not ipv4:
                    continue
                physical_interfaces.append({
                    "id": f"ethernet:{name}", "kind": "ethernet",
                    "protocol": "ethernet", "endpoint": name,
                    "label": f"网卡 {name}（{', '.join(ipv4)}）",
                    "name": name, "addresses": ipv4, "detected": True,
                    "shared_roles": ["plc", "robot"],
                })
        except Exception as exc:
            error = f"{error}; 网卡枚举失败：{exc}" if error else f"网卡枚举失败：{exc}"
        if not any(item.get("kind") == "ethernet" for item in physical_interfaces):
            physical_interfaces.append({
                "id": "ethernet:default", "kind": "ethernet",
                "protocol": "ethernet", "endpoint": "默认工控网卡",
                "label": "默认工控网卡（需协议检查确认）",
                "detected": False, "shared_roles": ["plc", "robot"],
            })
        if not any(item.get("kind") == "serial" for item in physical_interfaces):
            physical_interfaces.append({
                "id": "serial:auto", "kind": "serial", "protocol": "serial",
                "endpoint": "COM8", "label": "串口（待识别，启动后检查）",
                "detected": False, "auto_assignable": True,
            })
        rtsp_reachable = False
        try:
            sock = socket.create_connection(("192.168.125.2", 554), timeout=1.0)
            sock.close()
            rtsp_reachable = True
        except OSError:
            pass
        return {
            "ports": ports,
            "physical_interfaces": physical_interfaces,
            "hid_devices": smrf_devices,
            "defaults": default_capture_interfaces(),
            "sensor_type_profiles": sensor_interface_profiles(),
            "channel_metadata": SENSOR_CHANNEL_METADATA,
            "error": error,
            "plc_reachable": plc_reachable,
            "abb_reachable": abb_reachable,
            "uvc_dll_found": uvc_dll_found,
            "rtsp_reachable": rtsp_reachable,
            "thermocouple_reachable": bool(smrf_devices),
        }

    @staticmethod
    def available_schemas() -> list[dict[str, Any]]:
        return [
            {
                "id": schema_id,
                "label": definition["label"],
                "sensors": list(definition["sensors"]),
                "raw_columns": list(definition["raw_columns"]),
            }
            for schema_id, definition in ACQUISITION_SCHEMAS.items()
        ]

    def test_connection(
        self, config: AcquisitionConfig, timeout_seconds: float = 2.5
    ) -> dict:
        """Check source readiness and report the exact missing endpoint/channel."""
        selected = set(config.selected_sensors or [])
        received = {name: 0 for name in ALL_SENSOR_COLUMNS}
        invalid_received = {name: 0 for name in ALL_SENSOR_COLUMNS}
        errors: list[str] = []
        check_started = time.time()
        if config.acquisition_mode == "simulation":
            driver = build_driver(config)
            try:
                driver.open()
                probe_started = time.time()
                while time.time() - probe_started < max(0.5, float(timeout_seconds)):
                    sample = driver.read_sample()
                    if sample:
                        for name in selected:
                            if name not in sample:
                                continue
                            if _finite(sample[name]) is None:
                                invalid_received[name] += 1
                            else:
                                received[name] += 1
                        if selected and all(received[name] > 0 for name in selected):
                            break
                    time.sleep(min(0.02, 1.0 / max(config.sample_rate_hz, 1.0)))
            except Exception as exc:
                errors.append(f"模拟数据源：{exc}")
            finally:
                try:
                    driver.close()
                except Exception:
                    pass
        interface_results: list[dict[str, Any]] = []
        if config.acquisition_mode == "simulation":
            # Simulation has no physical probe.  Report the logical interface
            # mapping from the loaded source so the UI does not mistake the
            # absence of hardware for a disabled interface.
            for item in config.interfaces or []:
                interface_id = str(item.get("id") or "")
                role = str(item.get("role") or "custom")
                expected = [
                    name for name in config.interface_channel_assignments.get(interface_id, [])
                    if name in selected
                ]
                detected = [name for name in expected if received.get(name, 0) > 0]
                enabled = bool(item.get("enabled", True))
                if not enabled:
                    state, message, ok = "disabled", "接口已停用", True
                elif detected or not expected:
                    state = "ok"
                    message = (
                        f"模拟数据已匹配：{'、'.join(detected)}"
                        if detected else "模拟数据源已启用，当前未分配通道"
                    )
                    ok = True
                else:
                    state, message, ok = "no_data", "模拟数据未包含该接口对应通道", False
                interface_results.append({
                    "id": interface_id,
                    "role": role,
                    "driver": "simulator",
                    "endpoint": "模拟数据源",
                    "enabled": enabled,
                    "expected_channels": expected,
                    "detected_channels": detected,
                    "missing_channels": [name for name in expected if name not in detected],
                    "invalid_channels": [],
                    "sample_counts": {name: int(received.get(name, 0)) for name in detected},
                    "invalid_sample_counts": {},
                    "errors": [],
                    "state": state,
                    "message": message,
                    "ok": ok,
                })
        for item in ([] if config.acquisition_mode == "simulation" else (config.interfaces or [])):
            interface_id = str(item.get("id") or "")
            endpoint = str(item.get("endpoint") or interface_id or "未填写地址")
            role = str(item.get("role") or "custom")
            profile = SENSOR_INTERFACE_PROFILES.get(role, SENSOR_INTERFACE_PROFILES["custom"])
            physical_id = str(item.get("physical_interface_id") or "")
            physical_kind = str(item.get("physical_interface_kind") or profile.get("physical_kind") or "")
            protocol = str(profile.get("protocol") or item.get("driver") or "")
            physical_fallback = bool(item.get("physical_fallback", False))
            physical_warning = (
                "当前未识别到匹配协议，已临时分配串口，仅用于测试"
                if physical_fallback else ""
            )
            expected = [
                name for name in config.interface_channel_assignments.get(interface_id, [])
                if name in selected
            ]
            if not item.get("enabled", True):
                interface_results.append({
                    "id": interface_id, "role": item.get("role", "custom"),
                    "driver": item.get("driver", ""), "endpoint": endpoint,
                    "physical_interface_id": physical_id, "physical_interface_kind": physical_kind,
                    "protocol": protocol, "physical_fallback": physical_fallback,
                    "physical_warning": physical_warning,
                    "enabled": False, "expected_channels": expected,
                    "detected_channels": [], "missing_channels": [],
                    "invalid_channels": [], "sample_counts": {}, "errors": [],
                    "state": "disabled", "message": "接口已停用", "ok": True,
                })
                continue
            auxiliary_only = str(item.get("role") or "") == "thermal_rtsp"
            if not expected:
                interface_results.append({
                    "id": interface_id, "role": item.get("role", "custom"),
                    "driver": item.get("driver", ""), "endpoint": endpoint,
                    "physical_interface_id": physical_id, "physical_interface_kind": physical_kind,
                    "protocol": protocol, "physical_fallback": physical_fallback,
                    "physical_warning": physical_warning,
                    "enabled": True, "expected_channels": [],
                    "detected_channels": [], "missing_channels": [],
                    "invalid_channels": [], "sample_counts": {}, "errors": [],
                    "auxiliary_only": auxiliary_only,
                    "state": "video_only" if auxiliary_only else "no_channels",
                    "message": "仅提供视频流" if auxiliary_only else "当前未分配已选通道",
                    "ok": True,
                })
                continue
            # A serial fallback is deliberately a test-only binding.  The
            # selected protocol driver (for example UVC or SMRF HID) may call
            # a vendor DLL/API that blocks when the real device is absent.
            # Do not invoke that driver on an unverified fallback; report a
            # deterministic not-connected result so the remaining interfaces
            # and the diagnostic agent can continue.
            if physical_fallback or item.get("physical_verified") is False:
                state = "not_connected"
                if physical_fallback:
                    message = (
                        "未识别到匹配协议，已临时分配串口，仅用于测试；"
                        "跳过真实协议探测，请连接设备后重新检查"
                    )
                else:
                    message = "实际物理接口尚未验证，跳过真实协议探测；请重新识别接口后检查"
                errors.append(f"{endpoint}：{message}")
                interface_results.append({
                    "id": interface_id, "role": item.get("role", "custom"),
                    "driver": item.get("driver", ""), "endpoint": endpoint,
                    "physical_interface_id": physical_id, "physical_interface_kind": physical_kind,
                    "protocol": protocol, "physical_fallback": physical_fallback,
                    "physical_warning": physical_warning,
                    "enabled": True, "expected_channels": expected,
                    "detected_channels": [], "missing_channels": expected,
                    "invalid_channels": [], "sample_counts": {}, "invalid_sample_counts": {},
                    "errors": [message], "auxiliary_only": auxiliary_only,
                    "state": state, "message": message, "ok": False,
                })
                continue
            detected: dict[str, int] = {}
            invalid: dict[str, int] = {}
            probe_errors: list[str] = []
            interface_driver = None
            probe_started = time.time()
            try:
                interface_driver = MultiInterfaceDriver(
                    [item], list(config.schema_sensors), config.source_file,
                    {interface_id: expected},
                )
                interface_driver.open()
                while time.time() - probe_started < max(0.5, min(float(timeout_seconds), 1.5)):
                    sample = interface_driver.read_sample()
                    if sample:
                        for name in expected:
                            if name not in sample:
                                continue
                            if _finite(sample[name]) is None:
                                invalid[name] = invalid.get(name, 0) + 1
                                invalid_received[name] += 1
                            else:
                                detected[name] = detected.get(name, 0) + 1
                                received[name] += 1
                    if detected:
                        break
                    time.sleep(0.01)
            except Exception as exc:
                probe_errors.append(str(exc))
            finally:
                if interface_driver is not None:
                    try:
                        interface_driver.close()
                    except Exception:
                        pass
            missing = [name for name in expected if detected.get(name, 0) <= 0]
            invalid_channels = [name for name in missing if invalid.get(name, 0) > 0]
            if detected:
                state, message = "ok", f"接口已收到有效数据：{'、'.join(sorted(detected))}"
                if missing:
                    message += f"；未收到通道不阻止采集：{'、'.join(missing)}"
            elif probe_errors:
                state, message = "not_connected", f"接口无法打开或读取：{'；'.join(probe_errors)}"
            elif invalid_channels:
                state, message = "invalid_data", f"收到非数值数据：{'、'.join(invalid_channels)}"
            else:
                state, message = "no_data", f"未检测到采集数据：{'、'.join(missing)}"
            if state != "ok":
                errors.append(f"{endpoint}：{message}")
            interface_results.append({
                "id": interface_id, "role": item.get("role", "custom"),
                "driver": item.get("driver", ""), "endpoint": endpoint,
                "physical_interface_id": physical_id, "physical_interface_kind": physical_kind,
                "protocol": protocol, "physical_fallback": physical_fallback,
                "physical_warning": physical_warning,
                "enabled": True, "expected_channels": expected,
                "detected_channels": sorted(detected), "missing_channels": missing,
                "invalid_channels": invalid_channels, "sample_counts": detected,
                "invalid_sample_counts": invalid, "errors": probe_errors,
                "auxiliary_only": auxiliary_only, "state": state,
                "message": message, "ok": state == "ok",
            })
        sensors = []
        for name in config.schema_sensors:
            is_selected = name in selected
            if not is_selected:
                state = "not_selected"
            elif received[name] > 0:
                state = "ok"
            elif invalid_received[name] > 0:
                state = "invalid_data"
            else:
                state = "no_data"
            sensors.append({
                "name": name, "selected": is_selected,
                "received_samples": int(received[name]),
                "invalid_samples": int(invalid_received[name]), "state": state,
                "message": {"not_selected": "未选择采集", "ok": "收到有效数据",
                             "invalid_data": "收到非数值数据", "no_data": "未检测到数据"}[state],
                "blocking": config.acquisition_mode == "simulation" and is_selected,
                "ok": not is_selected or state == "ok",
            })
        interface_ok = config.acquisition_mode == "simulation" or all(
            item["ok"] for item in interface_results if item.get("enabled", True)
        )
        ok = bool(selected) and not errors and interface_ok
        if config.acquisition_mode == "simulation":
            ok = ok and all(item["ok"] for item in sensors)
        result = {
            "ok": ok, "driver": config.driver, "endpoint": config.endpoint,
            "elapsed_seconds": time.time() - check_started, "errors": errors,
            "sensors": sensors, "interfaces": interface_results,
        }
        result["checked_at"] = time.time()
        with self.lock:
            self._latest_check_result = deepcopy(result)
        return result

    def latest_check_result(self) -> dict[str, Any]:
        """Return the last completed interface check without probing hardware."""

        with self.lock:
            return deepcopy(self._latest_check_result)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _layer_files(self, specimen_folder_name: str) -> list[Path]:
        if self.session_dir is None:
            return []
        pattern = re.compile(
            rf"^{re.escape(specimen_folder_name)}_(?:\u7b2c|\ufffd\ufffd)(\d+)(?:\u5c42|\ufffd\ufffd)\.CSV$",
            re.IGNORECASE,
        )
        return sorted(
            (
                path
                for path in self.session_dir.glob(f"{specimen_folder_name}_*.CSV")
                if pattern.match(path.name)
            ),
            key=lambda path: int(pattern.match(path.name).group(1)),
        )

    def _archive_previous_specimen(
        self,
        specimen_folder_name: str,
        previous_metadata: dict[str, Any],
        exclude: set[Path] | None = None,
    ) -> str | None:
        """Move the active specimen aside before a new layer-1 capture.

        A visible folder is deliberately keyed by condition and replicate.  If
        an operator reuses that combination and starts at layer 1, keeping the
        old higher layers in place would silently mix two physical specimens.
        Archiving the active specimen prevents that failure while preserving
        the user-requested concise folder name.
        """
        if self.session_dir is None:
            return None
        candidates = self._layer_files(specimen_folder_name)
        combined = self.session_dir / f"{specimen_folder_name}_完整试样.CSV"
        if combined.exists():
            candidates.append(combined)
        candidates.extend(
            path
            for path in self.session_dir.glob(f"{specimen_folder_name}_*.partial")
            if path.is_file()
        )
        if not candidates:
            return None
        old_capture = _safe_component(
            previous_metadata.get("capture_uuid") or "旧试样", max_length=32
        )
        archive_dir = (
            self.session_dir
            / "历史版本"
            / f"试样会话_{self.session_stamp}_{old_capture}"
        )
        archive_dir.mkdir(parents=True, exist_ok=True)
        excluded = {path.resolve() for path in (exclude or set())}
        for source in dict.fromkeys(candidates):
            if source.resolve() in excluded:
                continue
            target = archive_dir / source.name
            counter = 2
            while target.exists():
                target = archive_dir / f"{source.stem}_{counter}{source.suffix}"
                counter += 1
            os.replace(source, target)
        if previous_metadata:
            _atomic_write_json(archive_dir / "试样会话.json", previous_metadata)
        return str(archive_dir)

    def _archive_failed_working_files(self) -> str | None:
        archived: list[Path] = []
        for path in (self.raw_work_path, self.timestamp_work_path):
            if path is not None and path.exists():
                target = self._archive_existing(path, "无有效数据")
                if target is not None:
                    archived.append(target)
        if not archived:
            return None
        return str(archived[0].parent)

    def _infer_existing_specimen_id(
        self, specimen_folder_name: str
    ) -> str | None:
        files = self._layer_files(specimen_folder_name)
        if not files:
            return None
        try:
            with files[0].open("r", encoding="gb18030", newline="") as handle:
                row = next(csv.DictReader(handle), None) or {}
            value = str(row.get("specimen_id") or row.get("试件") or "").strip()
            return value or None
        except Exception:
            return None

    def _prepare_capture_identity(
        self, config: AcquisitionConfig, specimen_folder_name: str
    ) -> str:
        if self.capture_record_dir is None:
            raise RuntimeError("采集记录目录尚未初始化")
        self.operator_specimen_id = str(config.specimen_id or "").strip()
        self.session_metadata_path = self.capture_record_dir / "当前试样会话.json"
        previous = (
            self._read_json(self.session_metadata_path)
            if self.session_metadata_path.exists()
            else {}
        )
        self.previous_session_metadata = dict(previous)
        signature = {
            "dataset_schema": config.dataset_schema,
            "condition_id": str(config.condition_id),
            "replicate": int(config.replicate),
            "parameter_token": specimen_folder_name,
        }
        active_layers = self._layer_files(specimen_folder_name)
        combined = self.session_dir / f"{specimen_folder_name}_完整试样.CSV"
        if config.layer > 0 and active_layers:
            expected_columns = config.raw_columns
            mismatched = [
                path.name
                for path in active_layers
                if self._layer_columns(path) != expected_columns
            ]
            if mismatched:
                raise ValueError(
                    "同一试样的后续铺层必须使用与已保存铺层相同的采集通道；"
                    "请将铺层数设为1开始新的试样，或恢复原采集通道。"
                )
        self.pending_previous_archive = bool(
            config.layer == 0 and (active_layers or combined.exists())
        )
        identity_previous = {} if self.pending_previous_archive else previous

        previous_signature = identity_previous.get("identity")
        capture_uuid = ""
        if previous_signature == signature:
            capture_uuid = str(identity_previous.get("capture_uuid") or "").strip()
        if not capture_uuid and config.layer > 0 and active_layers:
            # Backward-compatible continuation of data written before this
            # identity feature existed.
            capture_uuid = (
                self._infer_existing_specimen_id(specimen_folder_name)
                or str(config.capture_uuid or "").strip()
            )
        if not capture_uuid:
            capture_uuid = (
                f"AFP-{datetime.now().strftime('%Y%m%dT%H%M%S')}-"
                f"{uuid.uuid4().hex[:8]}"
            )

        config.capture_uuid = capture_uuid
        # Preserve the operator-entered legacy value in the session manifest,
        # but use the immutable ID in raw rows and database relations.
        config.specimen_id = capture_uuid
        config.run_id = f"{capture_uuid}-L{config.layer + 1}"
        self.capture_uuid = capture_uuid
        metadata = {
            "capture_uuid": capture_uuid,
            "operator_specimen_id": self.operator_specimen_id,
            "identity": signature,
            "created_at": identity_previous.get("created_at")
            or datetime.now().isoformat(timespec="milliseconds"),
            "last_started_at": datetime.now().isoformat(timespec="milliseconds"),
            "completed_layers": identity_previous.get("completed_layers", []),
        }
        # Commit this identity only after at least one valid sample has been
        # finalized.  Until then, the previous specimen remains authoritative.
        self.pending_session_metadata = metadata
        return capture_uuid

    def _archive_orphan_partial(self, path: Path | None) -> None:
        if path is None or not path.exists():
            return
        self._archive_existing(path, "中断采集")

    def _archive_orphan_timestamp_partials(
        self, specimen_folder_name: str
    ) -> None:
        if self.capture_record_dir is None:
            return
        for path in self.capture_record_dir.glob(
            f"{specimen_folder_name}_第*层_*_时间戳.csv.partial"
        ):
            if path != self.timestamp_work_path:
                self._archive_orphan_partial(path)

    def _finalize_working_files(self) -> None:
        for working, final in (
            (self.raw_work_path, self.raw_path),
            (self.timestamp_work_path, self.timestamp_path),
        ):
            if working is None or final is None or not working.exists():
                continue
            with working.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(working, final)

    @staticmethod
    def _read_layer_rows(path: Path) -> list[dict[str, Any]]:
        for encoding in ("gb18030", "utf-8-sig", "utf-8"):
            try:
                with path.open("r", encoding=encoding, newline="") as handle:
                    return list(csv.DictReader(handle))
            except UnicodeDecodeError:
                continue
        raise ValueError(f"无法识别层文件编码：{path}")

    @staticmethod
    def _layer_columns(path: Path) -> list[str]:
        """Read only a layer header for schema-consistency checks."""
        for encoding in ("gb18030", "utf-8-sig", "utf-8"):
            try:
                with path.open("r", encoding=encoding, newline="") as handle:
                    reader = csv.reader(handle)
                    return next(reader, [])
            except UnicodeDecodeError:
                continue
        raise ValueError(f"无法识别层文件编码：{path}")

    def _data_quality_summary(self) -> dict[str, Any]:
        timestamps: list[float] = []
        if self.timestamp_path is not None and self.timestamp_path.is_file():
            try:
                with self.timestamp_path.open(
                    "r", encoding="utf-8-sig", newline=""
                ) as handle:
                    for row in csv.DictReader(handle):
                        value = _finite(row.get("timestamp_unix"))
                        if value is not None:
                            timestamps.append(value)
            except Exception:
                timestamps = list(self.timestamps)
        if not timestamps:
            timestamps = list(self.timestamps)
        intervals = [
            later - earlier
            for earlier, later in zip(timestamps, timestamps[1:])
            if later > earlier
        ]
        duration = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0.0
        effective_rate = (
            (len(timestamps) - 1) / duration if duration > 0 else 0.0
        )
        ordered = sorted(intervals)
        p95 = (
            ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
            if ordered
            else 0.0
        )
        expected_interval = (
            1.0 / self.config.sample_rate_hz
            if self.config is not None and self.config.sample_rate_hz > 0
            else 0.0
        )
        sample_count = int(self.total_sample_count)
        selected = list(self.config.selected_sensors or []) if self.config else []
        return {
            "sample_count": sample_count,
            "duration_seconds": round(max(0.0, duration), 6),
            "configured_sample_rate_hz": (
                float(self.config.sample_rate_hz) if self.config else None
            ),
            "effective_sample_rate_hz": round(effective_rate, 6),
            "mean_interval_ms": round(
                (sum(intervals) / len(intervals) * 1000.0) if intervals else 0.0,
                6,
            ),
            "p95_interval_ms": round(p95 * 1000.0, 6),
            "maximum_gap_ms": round(
                (max(intervals) * 1000.0) if intervals else 0.0, 6
            ),
            "gaps_over_twice_expected": (
                sum(value > expected_interval * 2.0 for value in intervals)
                if expected_interval > 0
                else 0
            ),
            "channel_coverage": {
                name: round(self.sensor_received.get(name, 0) / sample_count, 6)
                if sample_count > 0
                else 0.0
                for name in selected
            },
            "online_buffer_rows": len(self.rows),
            "online_buffer_truncated": sample_count > len(self.rows),
        }

    def start(self, config: AcquisitionConfig) -> dict:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("采集已经在运行")
            if (
                self.thread is not None
                and self.config is not None
                and not self.finalization_complete
            ):
                # A hardware driver may stop itself after a read error.  Commit
                # any rows already received before a new session resets state.
                self.stop()
            self.config = config
            self.mysql_status = {
                "enabled": bool(config.mysql_enabled or config.mysql_local_enabled),
                "ok": None,
                "state": (
                    "pending"
                    if config.mysql_enabled or config.mysql_local_enabled
                    else "disabled"
                ),
                "saved_rows": 0,
            }
            self.rows.clear()
            self.timestamps.clear()
            self.total_sample_count = 0
            self.sensor_received = {
                name: 0 for name in ALL_SENSOR_COLUMNS
            }
            self.sensor_last_time = {
                name: None for name in ALL_SENSOR_COLUMNS
            }
            self.channel_observed = {
                name: 0 for name in ALL_SENSOR_COLUMNS
            }
            self.channel_last_observed = {
                name: None for name in ALL_SENSOR_COLUMNS
            }
            self.last_error = ""
            self.archived_previous_session = None
            self.replaced_layer_archive = None
            self.failed_capture_archive = None
            self.pending_previous_archive = False
            self.previous_session_metadata = {}
            self.pending_session_metadata = {}
            self.specimen_folder_name = ""
            self.capture_saved = False
            self.save_enabled = False
            self.save_status = check_capture_save_root(config.save_root)
            self.finalization_complete = False
            self.full_specimen_path = None
            self.completed_layers = []
            self.last_data_quality = None
            self.last_file_integrity = None
            self.capture_uuid = ""
            self.operator_specimen_id = ""
            self.session_metadata_path = None
            self.session_dir = None
            self.capture_record_dir = None
            self.timestamp_path = None
            self.raw_work_path = None
            self.timestamp_work_path = None
            self.started_at = time.time()
            self.stopped_at = None
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session_stamp = stamp
            parameter_token = _parameter_token(config)
            # Storage names intentionally exclude specimen/run identifiers.
            specimen_folder_name = parameter_token
            self.specimen_folder_name = specimen_folder_name
            manifest_path: Path | None = None
            if self.save_status["ok"]:
                self.save_enabled = True
                selected_root = Path(str(self.save_status["path"])).resolve()
                self.session_dir = selected_root / specimen_folder_name
                self.capture_record_dir = self.session_dir / "采集记录"
                self.capture_record_dir.mkdir(parents=True, exist_ok=True)
                self.timestamp_path = self.capture_record_dir / (
                    f"{specimen_folder_name}_第{config.layer + 1}层_"
                    f"{stamp}_时间戳.csv"
                )
                manifest_path = self.capture_record_dir / (
                    f"{specimen_folder_name}_第{config.layer + 1}层_"
                    f"{stamp}_采集清单.json"
                )
                # Use stable Unicode layer names for new captures.  The fallback
                # pattern in _rebuild_whole_specimen still accepts files created
                # by earlier builds that used replacement characters.
                self.raw_path = self.session_dir / (
                    f"{specimen_folder_name}_\u7b2c{config.layer + 1}\u5c42.CSV"
                )
                if max(len(str(self.raw_path)), len(str(self.timestamp_path))) > 235:
                    raise ValueError(
                        "保存路径过长。请改选更短的保存根目录，或缩短试样名。"
                    )
            else:
                # Keep the acquisition stream and predictions usable while
                # routing all file output to memory for this run.
                self.raw_path = None
            self.driver = build_driver(config)
            try:
                self.driver.open()
                if self.save_enabled:
                    self._prepare_capture_identity(config, specimen_folder_name)
                    self.raw_work_path = self.raw_path.with_name(
                        f"{self.raw_path.name}.partial"
                    )
                    self.timestamp_work_path = self.timestamp_path.with_name(
                        f"{self.timestamp_path.name}.partial"
                    )
                    self._archive_orphan_partial(self.raw_work_path)
                    self._archive_orphan_timestamp_partials(specimen_folder_name)
                    self._archive_orphan_partial(self.timestamp_work_path)
                    _atomic_write_json(
                        manifest_path,
                        {
                            **self._public_config(config),
                            "capture_uuid": self.capture_uuid,
                            "operator_specimen_id": self.operator_specimen_id,
                            "started_at": datetime.now().isoformat(timespec="milliseconds"),
                            "raw_encoding": "gb18030",
                            "raw_columns": config.raw_columns,
                            "selected_save_root": str(selected_root),
                            "specimen_folder": str(self.session_dir),
                            "layer_file": str(self.raw_path),
                            "working_layer_file": str(self.raw_work_path),
                            "timestamp_file": str(self.timestamp_path),
                            "working_timestamp_file": str(self.timestamp_work_path),
                            "archived_previous_session": self.archived_previous_session,
                            "whole_specimen_rule": (
                                f"{specimen_folder_name}_完整试样.CSV（每层结束后原子覆盖更新）"
                            ),
                        },
                    )
            except Exception as exc:
                self.last_error = str(exc)
                try:
                    self.driver.close()
                finally:
                    self.driver = None
                    self.thread = None
                    self.finalization_complete = True
                raise
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._run,
                name="AFP-Acquisition",
                daemon=True,
            )
            self.thread.start()
        return self.status()

    def _archive_existing(self, path: Path, category: str) -> Path | None:
        if not path.exists():
            return None
        history_dir = self.session_dir / "历史版本" / category
        history_dir.mkdir(parents=True, exist_ok=True)
        archived = history_dir / (
            f"{path.stem}_{self.session_stamp}{path.suffix}"
        )
        counter = 2
        while archived.exists():
            archived = history_dir / (
                f"{path.stem}_{self.session_stamp}_{counter}{path.suffix}"
            )
            counter += 1
        path.replace(archived)
        return archived

    def _rebuild_whole_specimen(self) -> Path | None:
        """Rebuild one canonical complete specimen CSV in-place.

        The storage path is keyed by process parameters only; specimen/run
        identifiers are retained as metadata but are deliberately excluded
        from folder and file names.  Rebuilding overwrites the prior complete
        file, so a folder contains only the current complete specimen copy.
        """
        if self.config is None or self.session_dir is None:
            return None
        specimen_folder_name = _parameter_token(self.config)
        layer_pattern = re.compile(
            rf"^{re.escape(specimen_folder_name)}_(?:\u7b2c|\ufffd\ufffd)(\d+)(?:\u5c42|\ufffd\ufffd)\.CSV$",
            re.IGNORECASE,
        )
        available: list[tuple[int, Path]] = []
        for path in self.session_dir.glob(f"{specimen_folder_name}_*.CSV"):
            match = layer_pattern.match(path.name)
            if match and path.stat().st_size > 0:
                available.append((int(match.group(1)) - 1, path))
        available.sort(key=lambda item: item[0])
        self.completed_layers = [layer for layer, _ in available]
        if not available:
            return None
        combined_path = self.session_dir / (
            f"{specimen_folder_name}_\u5b8c\u6574\u8bd5\u6837.CSV"
        )
        temporary = combined_path.with_name(
            f".{combined_path.name}.{self.session_stamp}.tmp"
        )
        try:
            with temporary.open("w", encoding="gb18030", newline="") as output_file:
                writer = csv.DictWriter(output_file, fieldnames=self.config.raw_columns)
                writer.writeheader()
                for _, layer_path in available:
                    with layer_path.open("r", encoding="gb18030", newline="") as layer_file:
                        reader = csv.DictReader(layer_file)
                        if reader.fieldnames != self.config.raw_columns:
                            raise ValueError(
                                f"Layer file columns do not match the raw schema: {layer_path.name}"
                            )
                        writer.writerows(reader)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temporary, combined_path)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        self.full_specimen_path = combined_path
        return combined_path

    def _run(self) -> None:
        config = self.config
        selected = set(config.selected_sensors or [])
        raw_target = (
            self.raw_work_path
            if self.save_enabled and self.raw_work_path is not None
            else io.StringIO()
        )
        timestamp_target = (
            self.timestamp_work_path
            if self.save_enabled and self.timestamp_work_path is not None
            else io.StringIO()
        )
        try:
            raw_context = (
                raw_target.open("w", encoding="gb18030", newline="")
                if isinstance(raw_target, Path)
                else raw_target
            )
            timestamp_context = (
                timestamp_target.open("w", encoding="utf-8-sig", newline="")
                if isinstance(timestamp_target, Path)
                else timestamp_target
            )
            with raw_context as raw_file, timestamp_context as time_file:
                writer = csv.DictWriter(
                    raw_file, fieldnames=config.raw_columns
                )
                timestamp_writer = csv.DictWriter(
                    time_file,
                    fieldnames=[
                        "row_index",
                        "timestamp_iso",
                        "timestamp_unix",
                    ],
                )
                writer.writeheader()
                timestamp_writer.writeheader()
                next_deadline = time.perf_counter()
                empty_since: float | None = None
                while not self.stop_event.is_set():
                    try:
                        sample = self.driver.read_sample()
                    except Exception as exc:
                        self.last_error = str(exc)
                        break
                    observed_now = time.time()
                    if sample:
                        with self.lock:
                            for name, value in sample.items():
                                if name in self.channel_observed and _finite(value) is not None:
                                    self.channel_observed[name] += 1
                                    self.channel_last_observed[name] = observed_now
                    valid_sample = bool(sample) and any(
                        _finite(sample.get(name)) is not None for name in selected
                    )
                    if valid_sample:
                        empty_since = None
                        now = time.time()
                        row_index = int(self.total_sample_count)
                        row = {
                            name: sample.get(name, "")
                            for name in config.selected_sensors
                        }
                        if config.dataset_schema == "new_collection_v11_3":
                            row.update(
                                {
                                    "时间": datetime.fromtimestamp(now).isoformat(
                                        timespec="milliseconds"
                                    ),
                                    "initial_compaction_force_N": (
                                        config.initial_compaction_force_N
                                    ),
                                    "placement_speed_mm_s": (
                                        config.placement_speed_mm_s
                                    ),
                                    "pid_angle_deg": config.pid_angle_deg,
                                    "temperature_setpoint_C": (
                                        config.temperature_setpoint_C
                                    ),
                                    "run_id": config.run_id,
                                    "specimen_id": config.specimen_id,
                                    "condition_id": config.condition_id,
                                    "replicate": config.replicate,
                                    "layer_id": config.layer + 1,
                                }
                            )
                        else:
                            row.update(
                                {
                                    "cycle": config.cycle,
                                    "file": (
                                        self.raw_path.name
                                        if self.raw_path is not None
                                        else "未保存.CSV"
                                    ),
                                    "root": config.root,
                                    "p": config.p,
                                    "v": config.v,
                                    "pr": config.pr,
                                    "l": config.layer,
                                    "试件": config.specimen_id,
                                }
                            )
                        writer.writerow(row)
                        timestamp_writer.writerow(
                            {
                                "row_index": row_index,
                                "timestamp_iso": datetime.fromtimestamp(now).isoformat(
                                    timespec="milliseconds"
                                ),
                                "timestamp_unix": f"{now:.6f}",
                            }
                        )
                        raw_file.flush()
                        time_file.flush()
                        with self.lock:
                            self.total_sample_count += 1
                            self.rows.append(row)
                            self.timestamps.append(now)
                            for name in selected:
                                if _finite(row.get(name)) is not None:
                                    self.sensor_received[name] += 1
                                    self.sensor_last_time[name] = now
                    elif config.acquisition_mode == "real":
                        if empty_since is None:
                            empty_since = time.monotonic()
                        elif time.monotonic() - empty_since >= 3.0:
                            self.last_error = "没有连接到传感器，3秒内没有检测到有效采集数据；请检查接口、串口和数据格式"
                            break
                        time.sleep(0.005)
                    if config.driver == "simulator" or config.acquisition_mode == "simulation":
                        next_deadline += 1.0 / config.sample_rate_hz
                        self.stop_event.wait(
                            max(0.0, next_deadline - time.perf_counter())
                        )
        except Exception as exc:
            self.last_error = str(exc)
        finally:
            try:
                if self.driver is not None:
                    self.driver.close()
            finally:
                self.stopped_at = time.time()

    @staticmethod
    def _pending_targets_database(
        saved: dict[str, Any], settings: MySQLSettings
    ) -> bool:
        return (
            str(saved.get("mysql_host") or "127.0.0.1") == settings.host
            and int(saved.get("mysql_port") or 3306) == settings.port
            and str(saved.get("mysql_user") or "root") == settings.user
            and str(saved.get("mysql_database") or "afp_state_warning")
            == settings.database
        )

    def _retry_pending_mysql(
        self, settings: MySQLSettings, root: Path, limit: int = 20
    ) -> dict[str, Any]:
        """Retry previously failed layer uploads after connectivity returns."""
        result: dict[str, Any] = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "errors": [],
        }
        pending_files = sorted(
            root.rglob("*_mysql*_pending.json"),
            key=lambda path: path.stat().st_mtime,
        )
        if not pending_files:
            return result
        retry_limit = max(0, int(limit))
        if retry_limit == 0:
            return result
        selected_pending: list[tuple[Path, dict[str, Any]]] = []
        for pending_path in pending_files:
            payload = self._read_json(pending_path)
            saved_config = payload.get("config")
            if not isinstance(saved_config, dict) or not self._pending_targets_database(
                saved_config, settings
            ):
                result["skipped"] += 1
                continue
            selected_pending.append((pending_path, payload))
            if len(selected_pending) >= retry_limit:
                break
        if not selected_pending:
            return result
        store = MySQLCaptureStore(settings)
        verifier = getattr(store, "verify_connection", None)
        connection = (
            verifier(require_schema=True)
            if callable(verifier)
            else store.test_connection()
        )
        if not connection.get("ok"):
            result["connection_error"] = connection.get("error")
            return result
        allowed_fields = set(AcquisitionConfig.__dataclass_fields__)
        for pending_path, payload in selected_pending:
            saved_config = payload.get("config")
            layer_file = Path(str(payload.get("layer_file") or ""))
            if not layer_file.is_file():
                result["failed"] += 1
                result["errors"].append(
                    f"{pending_path.name}：找不到层文件 {layer_file}"
                )
                continue
            values = {
                key: value
                for key, value in saved_config.items()
                if key in allowed_fields
            }
            values.update(
                {
                    "mysql_enabled": True,
                    "mysql_host": settings.host,
                    "mysql_port": settings.port,
                    "mysql_user": settings.user,
                    "mysql_password": settings.password,
                    "mysql_database": settings.database,
                    "mysql_charset": settings.charset,
                    "mysql_connect_timeout": settings.connect_timeout,
                }
            )
            try:
                retry_config = AcquisitionConfig(**values)
                rows = self._read_layer_rows(layer_file)
                summary = payload.get("summary")
                if not isinstance(summary, dict):
                    summary = {
                        "sample_count": len(rows),
                        "completed_layers": [retry_config.layer + 1],
                        "recovered_from_pending": True,
                    }
                result["attempted"] += 1
                uploaded = store.save_layer(
                    retry_config,
                    rows=rows,
                    layer_file=str(layer_file),
                    full_specimen_file=payload.get("full_specimen_file"),
                    timestamp_file=payload.get("timestamp_file"),
                    folder_path=payload.get("folder_path")
                    or str(layer_file.parent),
                    summary=summary,
                )
                if uploaded.get("ok"):
                    synced_path = pending_path.with_name(
                        pending_path.name.replace("_pending.json", "_synced.json")
                    )
                    _atomic_write_json(
                        synced_path,
                        {
                            **payload,
                            "retry_result": uploaded,
                            "retried_at": datetime.now().isoformat(
                                timespec="milliseconds"
                            ),
                        },
                    )
                    pending_path.unlink(missing_ok=True)
                    result["succeeded"] += 1
                else:
                    result["failed"] += 1
                    result["errors"].append(
                        f"{pending_path.name}：{uploaded.get('error', '未知错误')}"
                    )
            except Exception as exc:
                result["failed"] += 1
                result["errors"].append(f"{pending_path.name}：{exc}")
        return result

    def retry_pending_mysql(
        self,
        settings: MySQLSettings | dict[str, Any] | None = None,
        root: str | Path | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Retry completed local layers that have not reached MySQL yet.

        This is the operator-facing counterpart of ``_retry_pending_mysql``.
        It deliberately processes only finalized local layer files, so a slow
        or unreachable remote database can never block the acquisition loop.
        """
        active_config = self.config
        if settings is None:
            if active_config is None:
                raise ValueError("尚未配置 MySQL 连接")
            settings = MySQLSettings(
                enabled=True,
                host=active_config.mysql_host,
                port=active_config.mysql_port,
                user=active_config.mysql_user,
                password=active_config.mysql_password,
                database=active_config.mysql_database,
                charset=active_config.mysql_charset,
                connect_timeout=active_config.mysql_connect_timeout,
            )
        elif isinstance(settings, dict):
            settings = MySQLSettings.from_mapping(settings)
        if not settings.enabled:
            settings = MySQLSettings(
                **{
                    **settings.__dict__,
                    "enabled": True,
                }
            )
        retry_root = Path(
            root
            or (active_config.save_root if active_config is not None else "")
            or self.capture_root
        ).expanduser()
        result = self._retry_pending_mysql(
            settings, retry_root, limit=max(1, min(int(limit), 1000))
        )
        with self.lock:
            self.mysql_status = {
                **self.mysql_status,
                "enabled": True,
                "database": settings.database,
                "host": settings.host,
                "pending_retry": result,
                "state": (
                    "synced"
                    if result.get("failed", 0) == 0
                    and not result.get("connection_error")
                    else "pending"
                ),
            }
        return result

    @staticmethod
    def _mysql_destinations(
        config: AcquisitionConfig,
    ) -> list[tuple[str, MySQLSettings]]:
        """Return enabled local and target stores in deterministic order."""
        destinations: list[tuple[str, MySQLSettings]] = []
        if config.mysql_local_enabled:
            destinations.append(
                (
                    "local",
                    MySQLSettings(
                        enabled=True,
                        host=config.mysql_local_host,
                        port=config.mysql_local_port,
                        user=config.mysql_local_user,
                        password=config.mysql_local_password,
                        database=config.mysql_local_database,
                        charset=config.mysql_charset,
                        connect_timeout=config.mysql_connect_timeout,
                    ),
                )
            )
        if config.mysql_enabled:
            destinations.append(
                (
                    "target",
                    MySQLSettings(
                        enabled=True,
                        host=config.mysql_host,
                        port=config.mysql_port,
                        user=config.mysql_user,
                        password=config.mysql_password,
                        database=config.mysql_database,
                        charset=config.mysql_charset,
                        connect_timeout=config.mysql_connect_timeout,
                    ),
                )
            )
        return destinations

    def stop(self) -> dict:
        self.stop_event.set()
        thread = self.thread
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive() and self.driver is not None:
                try:
                    self.driver.close()
                except Exception:
                    pass
                thread.join(timeout=2.0)
            if thread.is_alive():
                self.last_error = (
                    self.last_error
                    or "采集接口未能及时停止，工作文件已保留，尚未生成最终层文件"
                )
                return self.status()
        with self.lock:
            if self.finalization_complete:
                return self.status()
            if self.config is None:
                self.capture_saved = False
                self.finalization_complete = True
                return self.status()
            mysql_destinations = self._mysql_destinations(self.config)
            if not self.save_enabled and not mysql_destinations:
                self.capture_saved = False
                self.finalization_complete = True
                return self.status()
            # CSV output is optional.  When its directory is empty or
            # unavailable, still finalize an enabled MySQL destination from
            # the in-memory rows collected during this run.
            if not self.save_enabled and self.session_dir is None:
                rows = list(self.rows)
                destination_results: dict[str, Any] = {}
                for destination_name, settings in mysql_destinations:
                    saved = MySQLCaptureStore(settings).save_layer(
                        self.config,
                        rows=rows,
                        layer_file=None,
                        full_specimen_file=None,
                        timestamp_file=None,
                        folder_path=None,
                        summary={
                            "sample_count": len(rows),
                            "capture_saved": False,
                            "completed_layers": [],
                            "in_memory_only": True,
                        },
                    )
                    saved["destination"] = destination_name
                    saved["pending_retry"] = {"attempted": 0, "succeeded": 0, "failed": 0}
                    destination_results[destination_name] = saved
                successful = [item for item in destination_results.values() if item.get("ok")]
                failed = [item for item in destination_results.values() if not item.get("ok")]
                self.mysql_status = {
                    "enabled": True,
                    "ok": not failed,
                    "state": "synced" if not failed else "pending",
                    "saved_rows": sum(int(item.get("saved_rows", 0)) for item in successful),
                    "destination_count": len(destination_results),
                    "successful_destinations": len(successful),
                    "failed_destinations": len(failed),
                    "destinations": destination_results,
                    "error": "; ".join(
                        f"{item.get('destination')}: {item.get('error', '未知错误')}"
                        for item in failed
                    ),
                }
                self.capture_saved = False
                self.finalization_complete = True
                return self.status()
            if self.session_dir is not None and self.config is not None:
                intended_raw_path = self.raw_path
                specimen_folder_name = (
                    self.specimen_folder_name or _parameter_token(self.config)
                )
                has_valid_data = bool(
                    self.total_sample_count > 0
                    and self.raw_work_path is not None
                    and self.raw_work_path.is_file()
                )
                full_specimen_path: Path | None = None
                if has_valid_data:
                    if self.pending_previous_archive:
                        excluded = {
                            path
                            for path in (
                                self.raw_work_path,
                                self.timestamp_work_path,
                            )
                            if path is not None
                        }
                        self.archived_previous_session = (
                            self._archive_previous_specimen(
                                specimen_folder_name,
                                self.previous_session_metadata,
                                exclude=excluded,
                            )
                        )
                    elif self.raw_path is not None and self.raw_path.exists():
                        archived_layer = self._archive_existing(
                            self.raw_path, "重采铺层"
                        )
                        self.replaced_layer_archive = (
                            str(archived_layer)
                            if archived_layer is not None
                            else None
                        )
                        combined = self.session_dir / (
                            f"{specimen_folder_name}_完整试样.CSV"
                        )
                        if combined.exists():
                            self._archive_existing(
                                combined, "完整试样快照"
                            )
                    self._finalize_working_files()
                    full_specimen_path = (
                        self._rebuild_whole_specimen()
                        if self.raw_path is not None and self.raw_path.is_file()
                        else None
                    )
                    self.capture_saved = bool(
                        self.raw_path is not None and self.raw_path.is_file()
                    )
                else:
                    self.capture_saved = False
                    self.failed_capture_archive = (
                        self._archive_failed_working_files()
                    )
                    if not self.last_error:
                        self.last_error = (
                            "未采集到有效数据，本次未生成铺层文件；"
                            "上一份有效数据（如有）保持不变"
                        )
                    if not self.pending_previous_archive:
                        previous_layers = self.previous_session_metadata.get(
                            "completed_layers", []
                        )
                        self.completed_layers = [
                            int(layer) - 1
                            for layer in previous_layers
                            if str(layer).isdigit() and int(layer) > 0
                        ]
                data_quality = self._data_quality_summary()
                integrity = {
                    "layer_file": (
                        _file_integrity(self.raw_path)
                        if self.capture_saved
                        else None
                    ),
                    "timestamp_file": (
                        _file_integrity(self.timestamp_path)
                        if self.capture_saved
                        else None
                    ),
                    "whole_specimen_file": (
                        _file_integrity(full_specimen_path)
                        if self.capture_saved
                        else None
                    ),
                }
                self.last_data_quality = data_quality
                self.last_file_integrity = integrity
                summary = {
                    "capture_uuid": self.capture_uuid,
                    "operator_specimen_id": self.operator_specimen_id,
                    "stopped_at": datetime.now().isoformat(timespec="milliseconds"),
                    "sample_count": int(self.total_sample_count),
                    "capture_saved": self.capture_saved,
                    "last_error": self.last_error,
                    "layer_file": (
                        str(self.raw_path) if self.capture_saved else None
                    ),
                    "intended_layer_file": (
                        str(intended_raw_path)
                        if intended_raw_path is not None
                        else None
                    ),
                    "whole_specimen_file": (
                        str(full_specimen_path)
                        if self.capture_saved and full_specimen_path is not None
                        else None
                    ),
                    "completed_layers": [
                        layer + 1 for layer in self.completed_layers
                    ],
                    "timestamp_file": str(self.timestamp_path),
                    "sensor_received": self.sensor_received,
                    "data_quality": data_quality,
                    "file_integrity": integrity,
                    "archived_previous_session": self.archived_previous_session,
                    "replaced_layer_archive": self.replaced_layer_archive,
                    "failed_capture_archive": self.failed_capture_archive,
                }
                summary_path = None
                if self.capture_record_dir is not None:
                    summary_path = self.capture_record_dir / (
                        f"{intended_raw_path.stem if intended_raw_path is not None else '未保存'}_"
                        f"{self.session_stamp}_采集摘要.json"
                    )
                    _atomic_write_json(summary_path, summary)
                if self.capture_saved and self.session_metadata_path is not None:
                    metadata = dict(self.pending_session_metadata)
                    metadata.update(
                        {
                            "capture_uuid": self.capture_uuid,
                            "last_saved_at": summary["stopped_at"],
                            "completed_layers": summary["completed_layers"],
                            "last_layer": self.config.layer + 1,
                            "last_summary_file": str(summary_path) if summary_path else None,
                            "last_file_integrity": integrity,
                        }
                    )
                    _atomic_write_json(self.session_metadata_path, metadata)
                if mysql_destinations and self.total_sample_count > 0:
                    mysql_rows = (
                        self._read_layer_rows(self.raw_path)
                        if self.capture_saved and self.raw_path is not None and self.raw_path.is_file()
                        else list(self.rows)
                    )
                    destination_results: dict[str, Any] = {}
                    for destination_name, settings in mysql_destinations:
                        retry_root = (
                            self.session_dir.parent
                            if self.session_dir is not None
                            else self.capture_root
                        )
                        retry_status = self._retry_pending_mysql(settings, retry_root)
                        saved = MySQLCaptureStore(settings).save_layer(
                            self.config,
                            rows=mysql_rows,
                            layer_file=str(self.raw_path),
                            full_specimen_file=(
                                str(full_specimen_path)
                                if full_specimen_path is not None
                                else None
                            ),
                            timestamp_file=(
                                str(self.timestamp_path)
                                if self.timestamp_path is not None
                                else None
                            ),
                            folder_path=(
                                str(self.session_dir)
                                if self.session_dir is not None
                                else None
                            ),
                            summary=summary,
                        )
                        saved["destination"] = destination_name
                        saved["pending_retry"] = retry_status
                        destination_results[destination_name] = saved
                        if not saved.get("ok"):
                            if self.capture_record_dir is not None:
                                pending_path = self.capture_record_dir / (
                                    f"{self.raw_path.stem if self.raw_path is not None else '未保存'}_"
                                    f"{self.session_stamp}_mysql_{destination_name}_pending.json"
                                )
                                pending_config = self._public_config(self.config)
                                pending_config.update(
                                    {
                                        "mysql_enabled": True,
                                        "mysql_local_enabled": False,
                                        "mysql_host": settings.host,
                                        "mysql_port": settings.port,
                                        "mysql_user": settings.user,
                                        "mysql_password": "***" if settings.password else "",
                                        "mysql_database": settings.database,
                                    }
                                )
                                _atomic_write_json(
                                    pending_path,
                                    {
                                        "mysql": saved,
                                        "config": pending_config,
                                        "summary": summary,
                                        "layer_file": str(self.raw_path) if self.raw_path is not None else None,
                                        "full_specimen_file": str(full_specimen_path)
                                        if full_specimen_path is not None
                                        else None,
                                        "timestamp_file": str(self.timestamp_path)
                                        if self.timestamp_path is not None
                                        else None,
                                        "folder_path": str(self.session_dir) if self.session_dir is not None else None,
                                    },
                                )
                    successful = [item for item in destination_results.values() if item.get("ok")]
                    failed = [item for item in destination_results.values() if not item.get("ok")]
                    self.mysql_status = {
                        "enabled": True,
                        "ok": not failed,
                        "state": "synced" if not failed else "pending",
                        "saved_rows": sum(int(item.get("saved_rows", 0)) for item in successful),
                        "destination_count": len(destination_results),
                        "successful_destinations": len(successful),
                        "failed_destinations": len(failed),
                        "destinations": destination_results,
                        "error": "; ".join(
                            f"{item.get('destination')}: {item.get('error', '未知错误')}"
                            for item in failed
                        ),
                    }
                    summary["mysql"] = self.mysql_status
                    if summary_path is not None:
                        _atomic_write_json(summary_path, summary)
                elif mysql_destinations and not self.capture_saved:
                    self.mysql_status = {
                        "enabled": True,
                        "ok": False,
                        "state": "not_saved",
                        "saved_rows": 0,
                        "error": "未采集到有效数据，未写入MySQL",
                    }
                    summary["mysql"] = self.mysql_status
                    if summary_path is not None:
                        _atomic_write_json(summary_path, summary)
                elif not mysql_destinations:
                    self.mysql_status = {
                        "enabled": False,
                        "ok": False,
                        "saved_rows": 0,
                    }
                self.finalization_complete = True
        return self.status()

    def status(self) -> dict:
        with self.lock:
            now = time.time()
            running = self.thread is not None and self.thread.is_alive()
            selected = set(self.config.selected_sensors if self.config is not None else [])
            available_sensors = self.config.schema_sensors if self.config is not None else LEGACY_SENSOR_COLUMNS
            sensors = []
            for name in available_sensors:
                last = self.channel_last_observed[name]
                age = None if last is None else now - float(last)
                observed = int(self.channel_observed[name])
                if name not in selected:
                    sensor_state = "not_selected"
                elif observed <= 0 and running and self.started_at is not None and now - self.started_at < 2.0:
                    sensor_state = "waiting"
                elif observed <= 0:
                    sensor_state = "no_data"
                elif running and (age is None or age > 2.0):
                    sensor_state = "stale"
                else:
                    sensor_state = "ok"
                sensors.append({
                    "name": name, "selected": name in selected,
                    "observed_samples": observed,
                    "received_samples": int(self.sensor_received[name]),
                    "saved_samples": int(self.sensor_received[name]),
                    "last_sample_age_seconds": age,
                    "last_observed_age_seconds": age,
                    "state": sensor_state,
                    "message": {
                        "not_selected": "未选择采集", "waiting": "等待首个数据",
                        "no_data": "未检测到采集数据", "stale": "数据已中断",
                        "ok": "数据正常",
                    }[sensor_state],
                    "blocking": False,
                    "ok": sensor_state in {"not_selected", "ok"},
                })
            sensor_by_name = {item["name"]: item for item in sensors}
            interfaces: list[dict[str, Any]] = []
            if self.config is not None and self.config.acquisition_mode == "real":
                for item in self.config.interfaces or []:
                    interface_id = str(item.get("id") or "")
                    enabled = bool(item.get("enabled", True))
                    expected = [
                        name for name in self.config.interface_channel_assignments.get(interface_id, [])
                        if name in selected
                    ]
                    auxiliary_only = str(item.get("role") or "") == "thermal_rtsp"
                    if not enabled:
                        state, message, missing, stale = "disabled", "接口已停用", [], []
                    elif not expected:
                        state = "video_only" if auxiliary_only else "no_channels"
                        message = "仅提供视频流" if auxiliary_only else "当前未分配已选通道"
                        missing, stale = [], []
                    else:
                        healthy = [
                            name for name in expected
                            if sensor_by_name.get(name, {}).get("state") == "ok"
                        ]
                        missing = [
                            name for name in expected
                            if sensor_by_name.get(name, {}).get("state") in {"waiting", "no_data"}
                        ]
                        stale = [
                            name for name in expected
                            if sensor_by_name.get(name, {}).get("state") == "stale"
                        ]
                        if healthy:
                            state = "ok"
                            message = f"接口正在采集：{'、'.join(healthy)}"
                            unavailable = [*missing, *stale]
                            if unavailable:
                                message += "；其余通道不作为采集条件：" + "、".join(dict.fromkeys(unavailable))
                        elif missing and running and all(
                            sensor_by_name.get(name, {}).get("state") == "waiting" for name in missing
                        ) and not stale:
                            state, message = "waiting", f"等待 {len(missing)} 个通道的首个数据"
                        elif stale:
                            state, message = "stale", f"接口数据已中断：{'、'.join(stale)}"
                        else:
                            state, message = "no_data", f"未检测到采集数据：{'、'.join(missing or expected)}"
                    detected = [
                        name for name in expected
                        if sensor_by_name.get(name, {}).get("observed_samples", 0) > 0
                    ]
                    interfaces.append({
                        "id": interface_id, "role": item.get("role", "custom"),
                        "driver": item.get("driver", ""), "endpoint": item.get("endpoint", ""),
                        "enabled": enabled, "expected_channels": expected,
                        "detected_channels": detected, "missing_channels": missing,
                        "stale_channels": stale, "auxiliary_only": auxiliary_only,
                        "state": state, "message": message,
                        "ok": state in {"disabled", "video_only", "no_channels", "ok"},
                    })
            model_inputs = (
                self.config.model_input_sensors
                if self.config is not None
                else []
            )
            model_ready = bool(model_inputs) and all(
                name in selected
                and self.sensor_received[name] >= 24
                and self.sensor_last_time[name] is not None
                for name in model_inputs
            )
            return {
                "running": running,
                "sample_count": int(self.total_sample_count),
                "online_buffer_rows": len(self.rows),
                "capture_uuid": self.capture_uuid or None,
                "capture_saved": self.capture_saved,
                "save_enabled": self.save_enabled,
                "save_status": dict(self.save_status),
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "last_error": self.last_error,
                "session_dir": str(self.session_dir) if self.session_dir else None,
                "save_root": (
                    str(self.session_dir.parent)
                    if self.session_dir is not None
                    else None
                ),
                "raw_file": (
                    str(self.raw_path)
                    if self.save_enabled and (running or self.capture_saved) and self.raw_path
                    else None
                ),
                "layer_file": (
                    str(self.raw_path)
                    if self.save_enabled and (running or self.capture_saved) and self.raw_path
                    else None
                ),
                "full_specimen_file": (
                    str(self.full_specimen_path)
                    if self.capture_saved and self.full_specimen_path
                    else None
                ),
                "completed_layers": [
                    layer + 1 for layer in self.completed_layers
                ],
                "archived_previous_session": self.archived_previous_session,
                "replaced_layer_archive": self.replaced_layer_archive,
                "failed_capture_archive": self.failed_capture_archive,
                "data_quality": self.last_data_quality,
                "file_integrity": self.last_file_integrity,
                "timestamp_file": (
                    str(self.timestamp_path) if self.timestamp_path else None
                ),
                "model_ready": model_ready,
                "minimum_prediction_points": 24,
                "minimum_warning_points": 48,
                "sensors": sensors,
                "interfaces": interfaces,
                "config": (
                    self._public_config(self.config) if self.config else None
                ),
                "mysql": dict(self.mysql_status),
            }

    def export_manifest(self) -> dict[str, Any]:
        """Return a browser-safe manifest for the completed capture folder.

        The manifest deliberately exposes only paths relative to the specimen
        folder.  This lets the web client recreate the same folder hierarchy
        and filenames as the desktop capture without leaking host paths.
        """
        with self.lock:
            if self.session_dir is None or not self.session_dir.is_dir():
                return {"ready": False, "root_name": "", "files": []}
            root = self.session_dir.resolve()
            files: list[dict[str, Any]] = []
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                resolved = path.resolve()
                if root not in resolved.parents:
                    continue
                relative = resolved.relative_to(root).as_posix()
                files.append({"path": f"{root.name}/{relative}", "size": int(resolved.stat().st_size)})
            return {"ready": bool(files), "root_name": root.name, "files": files}

    def export_file(self, relative_path: str) -> tuple[bytes, str]:
        """Read one manifest path while preventing traversal outside the session."""
        with self.lock:
            if self.session_dir is None:
                raise FileNotFoundError("当前没有可导出的采集会话")
            root = self.session_dir.resolve()
            clean = str(relative_path or "").replace("\\", "/").strip("/")
            parts = clean.split("/")
            if len(parts) < 2 or parts[0] != root.name or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("导出文件路径无效")
            target = (root.joinpath(*parts[1:])).resolve()
            if root not in target.parents or not target.is_file() or target.is_symlink():
                raise FileNotFoundError("导出文件不存在")
            return target.read_bytes(), "/".join(parts)

    def reset_check_state(self) -> dict:
        """Reset interface/channel observations without deleting saved files."""
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("采集运行中不能重置检查，请先停止并保存")
            if self.driver is not None:
                try:
                    self.driver.close()
                except Exception:
                    pass
                self.driver = None
            self.channel_observed = {name: 0 for name in ALL_SENSOR_COLUMNS}
            self.channel_last_observed = {name: None for name in ALL_SENSOR_COLUMNS}
            self.last_error = ""
            return self.status()

    def numeric_matrix(self) -> tuple[list[dict[str, Any]], list[float]]:
        with self.lock:
            return list(self.rows), list(self.timestamps)
