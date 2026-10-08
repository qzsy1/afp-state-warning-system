from __future__ import annotations

import base64
import csv
import ctypes
import hashlib
import http.client
import ipaddress
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
from dataclasses import InitVar, asdict, dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any

import pandas as pd

from mysql_storage import MySQLCaptureStore, MySQLSettings, validate_database_name
from smrf_hid import SmrfHidDriver, enumerate_smrf_hid_devices
from simulation_replay import MonotonicReplayScheduler
from real_acquisition import (
    AcquisitionQualityMetrics,
    ChannelQuality,
    ChannelSample,
    ChannelSampleCache,
    DriverWorker,
    FrameAssembler,
    READINESS_STAGE_NAMES,
    ReadinessReport,
    ReadinessStage,
    UnifiedFrame,
    VirtualClock,
    capture_m3232_raw_serial,
    readiness_config_fingerprint,
    recent_complete_window,
    validate_industrial_network_path,
)
try:
    from windows_usb_topology import discover_windows_usb_topology
except ImportError:  # Compatibility with older modular runtimes.
    discover_windows_usb_topology = None
try:
    from interface_transport_catalog import enrich_interface_transports
except ImportError:  # Compatibility with older modular runtimes.
    enrich_interface_transports = None


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

# Device profiles are versioned evidence contracts.  Site-specific addresses,
# tolerances and reference stimuli may override them, but a capture always
# records the selected profile/version instead of relying on hidden defaults.
PLC_DEVICE_PROFILES: dict[str, dict[str, Any]] = {
    "panasonic_afp_v1": {
        "profile_id": "panasonic_afp_v1",
        "profile_version": 1,
        "unit_id": DEFAULT_PLC_SLAVE_ID,
        "function_code": 3,
        "register_map": dict(PLC_DEFAULT_REGISTER_MAP),
        "word_order": "low_high",
        "byte_order": "big",
        "scales": {"温度": 1.0, "压力": 1.0, "张力": 1.0},
        "units": {"温度": "°C", "压力": "N", "张力": "N"},
        "valid_ranges": {
            "温度": [-80.0, 800.0],
            "压力": [-100.0, 100000.0],
            "张力": [-100.0, 100000.0],
        },
        "reference_tolerances": {"温度": 5.0, "压力": 20.0, "张力": 20.0},
    }
}
DEFAULT_PLC_PROFILE_ID = "panasonic_afp_v1"

ABB_DEVICE_PROFILES: dict[str, dict[str, Any]] = {
    "abb_rws_base_v1": {
        "profile_id": "abb_rws_base_v1",
        "profile_version": 1,
        "mechanical_unit": "ROB_1",
        "coordinate_system": "Base",
        "robtarget_path": ABB_ROBTARGET_PATH,
        "rapid_symbol_base_path": ABB_RAPID_SYMBOL_BASE_PATH,
        "task": ABB_PROCESS_TASK,
        "module": ABB_PROCESS_MODULE,
        "speed_variable": ABB_SPEED_VARIABLE,
        "units": {"ABB_X": "mm", "ABB_Y": "mm", "ABB_Z": "mm", "线速度": "mm/s"},
        "valid_ranges": {
            "ABB_X": [-10000.0, 10000.0],
            "ABB_Y": [-10000.0, 10000.0],
            "ABB_Z": [-10000.0, 10000.0],
            "线速度": [0.0, 10000.0],
        },
    }
}
DEFAULT_ABB_PROFILE_ID = "abb_rws_base_v1"

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
    if not candidate.is_absolute():
        return {
            "ok": False,
            "code": "relative",
            "path": raw,
            "message": "保存文件夹必须使用实际写入电脑上的绝对路径，当前采集不保存数据",
        }
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
        "expected_update_hz": 5.0,
        "freshness_seconds": 0.45,
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
        "expected_update_hz": 10.0,
        "freshness_seconds": 0.25,
        "processing": "Modbus TCP FC03；float32低字在前",
    },
    "robot": {
        "label": "ABB机器人",
        "driver": "abb_robot",
        "protocol": "abb_rws",
        "physical_kind": "ethernet",
        "endpoint": DEFAULT_ABB_IP,
        "channels": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
        "expected_update_hz": 10.0,
        "freshness_seconds": 0.30,
        "processing": "RWS读取robtarget；相邻XYZ欧氏距离除以时间差得到线速度",
    },
    "thermal_uvc": {
        "label": "BSV UVC测温热像仪",
        "driver": "uvc_thermal",
        "protocol": "uvc",
        "physical_kind": "usb_uvc",
        "endpoint": "BSV UVC (WinUSB)",
        "channels": ["ROI平均温度"],
        "expected_update_hz": 9.0,
        "freshness_seconds": 0.35,
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
        "expected_update_hz": 10.0,
        "freshness_seconds": 0.25,
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


def _associate_usb_topology(
    topology: dict[str, Any], physical_interfaces: list[dict[str, Any]]
) -> dict[str, Any]:
    """Add physical transport metadata while preserving legacy endpoints."""

    if enrich_interface_transports is None:
        return {
            "schema_version": 1,
            "usb_ports": [],
            "unlocated_usb_interface_ids": [],
            "native_serial_interface_ids": [
                str(item.get("id") or "")
                for item in physical_interfaces
                if str(item.get("kind") or "") in {"serial", "com"}
            ],
            "ethernet_interface_ids": [
                str(item.get("id") or "")
                for item in physical_interfaces
                if str(item.get("kind") or "") in {"ethernet", "ethernet_adapter"}
            ],
        }
    return enrich_interface_transports(topology, physical_interfaces)


def _physical_interface_binding_issues(
    interfaces: list[dict[str, Any]], acquisition_mode: str
) -> list[dict[str, Any]]:
    """Collect role/protocol and physical endpoint ownership issues.

    PLC and ABB intentionally share the same Ethernet adapter.  Every other
    enabled logical interface must own a distinct physical adapter/device.

    Simulation files are a single logical source.  The UI keeps the same
    sensor/interface cards for routing and display, so several cards may
    intentionally carry the sentinel ``simulation_source`` binding.  That is
    not a physical adapter collision and must not be checked as one.
    """
    if acquisition_mode != "real":
        return []
    issues: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for item in interfaces:
        if not item.get("enabled", True):
            continue
        role = _canonical_sensor_type(item.get("role"), item.get("driver"))
        profile = SENSOR_INTERFACE_PROFILES.get(role, SENSOR_INTERFACE_PROFILES["custom"])
        expected_driver = str(profile.get("driver") or "")
        actual_driver = str(item.get("driver") or "")
        if not profile.get("editable_driver") and actual_driver != expected_driver:
            message = (
                f"接口“{item.get('id', role)}”的协议/驱动与传感器类型不匹配："
                f"{actual_driver or '未填写'}，应为{expected_driver}"
            )
            issues.append({
                "code": "driver_mismatch", "message": message,
                "interface_ids": [str(item.get("id") or role)],
            })
        expected_kind = str(profile.get("physical_kind") or "")
        actual_kind = str(item.get("physical_interface_kind") or "")
        fallback_binding = bool(item.get("physical_fallback", False))
        if actual_kind and expected_kind and actual_kind != expected_kind and not fallback_binding:
            message = (
                f"接口“{item.get('id', role)}”的物理接口类型与协议不匹配："
                f"{actual_kind}，应为{expected_kind}"
            )
            issues.append({
                "code": "physical_kind_mismatch", "message": message,
                "interface_ids": [str(item.get("id") or role)],
            })
        physical_id = _physical_interface_key(item.get("physical_interface_id"))
        physical_port_id = _physical_interface_key(item.get("physical_port_id"))
        if acquisition_mode == "real" and not physical_id and not physical_port_id:
            message = f"真实接口“{item.get('id', role)}”尚未绑定实际物理接口"
            issues.append({
                "code": "missing_physical_binding", "message": message,
                "interface_ids": [str(item.get("id") or role)],
            })
        resource_id = physical_port_id or physical_id
        if not resource_id:
            continue
        previous = seen.get(resource_id)
        if previous is None:
            seen[resource_id] = item
            continue
        shared_roles = {str(previous.get("role") or ""), role}
        shared_kinds = {
            str(previous.get("physical_interface_kind") or expected_kind),
            actual_kind or expected_kind,
        }
        if shared_roles == {"plc", "robot"} and shared_kinds == {"ethernet"}:
            continue
        message = (
            f"物理接口“{item.get('physical_port_id') or item.get('physical_interface_id')}”重复绑定："
            f"{previous.get('id', '前一接口')}与{item.get('id', role)}；"
            "仅允许PLC与ABB共享同一网卡"
        )
        issues.append({
            "code": "duplicate_physical_binding", "message": message,
            "resource_id": str(resource_id),
            "interface_ids": [
                str(previous.get("id") or ""), str(item.get("id") or role),
            ],
        })
    return issues


def _validate_physical_interface_bindings(
    interfaces: list[dict[str, Any]], acquisition_mode: str
) -> None:
    """Strict start-time validation; diagnostics use the collector directly."""

    issues = _physical_interface_binding_issues(interfaces, acquisition_mode)
    if issues:
        raise ValueError(str(issues[0]["message"]))


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


def _is_bluetooth_virtual_serial(item: dict[str, Any]) -> bool:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("description", "manufacturer", "hwid", "product", "label")
    ).casefold()
    return "bluetooth" in text or "蓝牙" in text


def annotate_discovery_evidence(
    interfaces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach non-interchangeable discovery evidence to physical candidates."""

    annotated: list[dict[str, Any]] = []
    for source in interfaces:
        item = dict(source)
        kind = str(item.get("kind") or "").lower()
        protocol = str(item.get("protocol") or "").lower()
        endpoint_present = bool(item.get("detected", False))
        driver_available = bool(item.get("driver_available", endpoint_present))
        bluetooth_virtual = kind in {"serial", "com"} and _is_bluetooth_virtual_serial(item)
        network_text = " ".join(
            str(item.get(key) or "")
            for key in ("id", "name", "description", "label")
        ).casefold()
        virtual_network = kind in {"ethernet", "ethernet_adapter"} and any(
            token in network_text
            for token in (
                "flclash", "clash", "tun", "tap", "wintun", "vpn",
                "wireguard", "openvpn", "loopback", "hyper-v", "vmware", "virtualbox",
            )
        )
        for address in item.get("addresses") or []:
            try:
                parsed_address = ipaddress.ip_address(str(address))
            except ValueError:
                continue
            virtual_network = virtual_network or bool(
                parsed_address.is_loopback
                or parsed_address.is_link_local
                or parsed_address in ipaddress.ip_network("198.18.0.0/15")
            )
        industrial_network_candidate = kind not in {"ethernet", "ethernet_adapter"} or any(
            str(address).startswith("192.168.125.")
            for address in item.get("addresses") or []
        )
        if "identity_verified" in item:
            identity_verified = bool(item.get("identity_verified"))
        elif protocol == "smrf_hid":
            identity_verified = endpoint_present and bool(
                (item.get("vid") is not None and item.get("pid") is not None)
                or item.get("serial") or item.get("product")
            )
        elif kind in {"ethernet", "ethernet_adapter"}:
            identity_verified = endpoint_present and not virtual_network and bool(
                item.get("name") or item.get("description") or item.get("id")
            )
        elif kind in {"serial", "com", "usb_uvc"}:
            identity_verified = endpoint_present and not bluetooth_virtual and bool(
                item.get("vid") is not None
                and item.get("pid") is not None
                and (item.get("serial") or item.get("product"))
            )
        else:
            identity_verified = False
        protocol_ready = bool(item.get("protocol_ready", False))
        auto_bind_eligible = bool(
            endpoint_present and identity_verified and industrial_network_candidate
        )
        item.update({
            "driver_available": driver_available,
            "endpoint_present": endpoint_present,
            "identity_verified": identity_verified,
            "protocol_ready": protocol_ready,
            "bluetooth_virtual": bluetooth_virtual,
            "virtual_network": virtual_network,
            "auto_bind_eligible": auto_bind_eligible,
            "auto_assignable": bool(item.get("auto_assignable", False) and auto_bind_eligible),
            "discovery_evidence": {
                "driver_available": driver_available,
                "endpoint_present": endpoint_present,
                "identity_verified": identity_verified,
                "protocol_ready": protocol_ready,
            },
        })
        annotated.append(item)
    return annotated


def resolve_serial_identity(
    saved_identity: dict[str, Any], ports: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve a saved serial identity without silently accepting COM drift."""

    saved = dict(saved_identity or {})
    saved_endpoint = str(saved.get("endpoint") or "").upper()
    matches: list[dict[str, Any]] = []
    for source in ports or []:
        item = dict(source)
        if _is_bluetooth_virtual_serial(item):
            continue
        stable_fields = ("vid", "pid", "serial")
        if not all(saved.get(field) not in (None, "") for field in stable_fields):
            continue
        if all(str(item.get(field) or "") == str(saved.get(field) or "") for field in stable_fields):
            matches.append(item)
    if not matches:
        return {
            "state": "identity_not_found", "resolved_endpoint": "", "confirmed": False,
            "device_profile_id": str(saved.get("device_profile_id") or ""),
        }
    match = matches[0]
    endpoint = str(match.get("endpoint") or match.get("id") or "")
    moved = endpoint.upper() != saved_endpoint
    return {
        "state": "moved_requires_confirmation" if moved else "identity_confirmed",
        "resolved_endpoint": endpoint,
        "confirmed": not moved,
        "device_profile_id": str(saved.get("device_profile_id") or ""),
        "identity": {
            key: match.get(key)
            for key in ("vid", "pid", "serial", "location", "endpoint")
        },
    }


def build_network_discovery_summary(
    target_host: str,
    target_port: int,
    tcp_probe_reachable: bool,
    physical_interfaces: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep TCP surface reachability separate from industrial-path evidence."""

    valid_path: dict[str, Any] | None = None
    attempted: list[dict[str, Any]] = []
    octets = str(target_host or "").split(".")
    device_subnet = ".".join(octets[:3]) + ".0/24" if len(octets) == 4 else ""
    for item in physical_interfaces or []:
        if str(item.get("kind") or "") not in {"ethernet", "ethernet_adapter"}:
            continue
        for address in item.get("addresses") or []:
            candidate = {
                "physical_interface_id": str(item.get("id") or ""),
                "network_interface_name": str(
                    item.get("name") or item.get("description") or item.get("label") or ""
                ),
                "source_address": str(address or ""),
                "device_subnet": str(item.get("device_subnet") or device_subnet),
            }
            report = validate_industrial_network_path(
                target_host,
                candidate,
                target_port=int(target_port),
                actual_source_address=str(address or ""),
                route_interface_id=str(item.get("id") or ""),
            )
            attempted.append(report)
            if report.get("ok"):
                valid_path = report
                break
        if valid_path:
            break
    network_path_valid = bool(valid_path)
    protocol_ready = False
    if tcp_probe_reachable and not network_path_valid:
        state = "network_path_invalid"
    elif not tcp_probe_reachable:
        state = "endpoint_unreachable"
    else:
        state = "hardware_protocol_unverified"
    return {
        "target_host": str(target_host),
        "target_port": int(target_port),
        "tcp_probe_reachable": bool(tcp_probe_reachable),
        "network_path_valid": network_path_valid,
        "protocol_ready": protocol_ready,
        "device_reachable": bool(network_path_valid and protocol_ready),
        "state": state,
        "network_path": valid_path,
        "attempted_paths": attempted,
    }


@dataclass
class AcquisitionConfig:
    # InitVar keeps this server-controlled switch out of serialized configs and
    # out of the helper payload allow-list.  It is used only for diagnostics;
    # capture start remains strict.
    diagnostic_validation: InitVar[bool] = False
    config_version: int = 2
    capture_policy: str = ""
    degraded_confirmed: bool = False
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
    execution_host: str = "server"
    execution_device_id: str = ""
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
    # Opaque, non-secret identifier for a server-side target profile.  The
    # server uses it to resolve an already preflighted target without sending
    # credentials to the browser or local helper.
    mysql_target_config_id: str = ""
    mysql_local_enabled: bool = False
    mysql_local_host: str = "127.0.0.1"
    mysql_local_port: int = 3306
    mysql_local_user: str = "root"
    mysql_local_password: str = ""
    mysql_local_database: str = "afp_state_warning"
    mysql_charset: str = "utf8mb4"
    mysql_connect_timeout: int = 5

    def __post_init__(self, diagnostic_validation: bool = False) -> None:
        self.configuration_issues: list[dict[str, Any]] = []
        self.config_version = max(2, int(self.config_version or 1))
        requested_mode = str(self.acquisition_mode or "").lower()
        if not requested_mode:
            requested_mode = "simulation" if self.driver == "simulator" else "real"
        self.acquisition_mode = requested_mode
        if self.acquisition_mode not in {"real", "simulation"}:
            raise ValueError("acquisition_mode must be real or simulation")
        self.execution_host = str(self.execution_host or "server").strip().lower()
        self.execution_device_id = str(self.execution_device_id or "").strip()
        requested_policy = str(self.capture_policy or "").strip().lower()
        if not requested_policy:
            requested_policy = "formal" if self.acquisition_mode == "real" else "simulation"
        allowed_policies = {"formal", "degraded_engineering", "unsaved_engineering"}
        if self.acquisition_mode == "simulation":
            requested_policy = "simulation"
        elif requested_policy not in allowed_policies:
            raise ValueError(
                "capture_policy must be formal, degraded_engineering or unsaved_engineering"
            )
        self.capture_policy = requested_policy
        self.degraded_confirmed = bool(self.degraded_confirmed)
        if self.capture_policy == "degraded_engineering":
            # A degraded session is evidence collection only.  This is set at
            # configuration normalization so every caller sees the same gate.
            self.processing_mode = "capture_only"
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
        self.mysql_target_config_id = str(self.mysql_target_config_id or "").strip()
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
                    message = (
                        f"接口“{interface['id']}”的协议/驱动与传感器类型不匹配："
                        f"{requested_driver}，应为{expected_driver}"
                    )
                    if diagnostic_validation:
                        self.configuration_issues.append({
                            "code": "driver_mismatch", "message": message,
                            "interface_ids": [interface["id"]],
                        })
                    else:
                        raise ValueError(message)
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
            if interface["role"] == "plc":
                profile_id = str(interface.get("profile_id") or DEFAULT_PLC_PROFILE_ID)
                if profile_id not in PLC_DEVICE_PROFILES:
                    raise ValueError(f"未知PLC设备档：{profile_id}")
                device_profile = deepcopy(PLC_DEVICE_PROFILES[profile_id])
                interface["profile_id"] = profile_id
                interface["profile_version"] = int(device_profile["profile_version"])
                interface["slave_id"] = int(interface.get("slave_id") or device_profile["unit_id"])
                interface["function_code"] = int(interface.get("function_code") or device_profile["function_code"])
                interface["register_map"] = interface.get("register_map") or dict(device_profile["register_map"])
                interface["word_order"] = str(interface.get("word_order") or device_profile["word_order"])
                interface["byte_order"] = str(interface.get("byte_order") or device_profile["byte_order"])
                interface["scales"] = dict(interface.get("scales") or device_profile["scales"])
                interface["units"] = dict(interface.get("units") or device_profile["units"])
                interface["valid_ranges"] = dict(interface.get("valid_ranges") or device_profile["valid_ranges"])
                interface["reference_tolerances"] = dict(interface.get("reference_tolerances") or device_profile["reference_tolerances"])
                interface["reference_values"] = dict(interface.get("reference_values") or {})
            elif interface["role"] == "robot":
                profile_id = str(interface.get("profile_id") or DEFAULT_ABB_PROFILE_ID)
                if profile_id not in ABB_DEVICE_PROFILES:
                    raise ValueError(f"未知ABB设备档：{profile_id}")
                device_profile = deepcopy(ABB_DEVICE_PROFILES[profile_id])
                interface["profile_id"] = profile_id
                interface["profile_version"] = int(device_profile["profile_version"])
                for key in (
                    "mechanical_unit", "coordinate_system", "robtarget_path",
                    "rapid_symbol_base_path", "task", "module", "speed_variable",
                ):
                    interface[key] = str(interface.get(key) or device_profile[key])
                interface["units"] = dict(interface.get("units") or device_profile["units"])
                interface["valid_ranges"] = dict(interface.get("valid_ranges") or device_profile["valid_ranges"])
            elif interface["role"] == "thermocouple":
                interface["channel_types"] = list(interface.get("channel_types") or ["K"] * 8)
                interface["channel_types_confirmed"] = bool(interface.get("channel_types_confirmed", False))
                interface["cold_junction_compensation"] = bool(interface.get("cold_junction_compensation", True))
            elif interface["role"] == "thermal_uvc":
                interface["frame_width"] = int(interface.get("frame_width") or BSV_UVC_WIDTH)
                interface["frame_height"] = int(interface.get("frame_height") or BSV_UVC_HEIGHT)
                interface["temperature_range_code"] = int(interface.get("temperature_range_code") or BSV_UVC_TEMP_RANGE_CODE)
                interface["temperature_scale"] = float(interface.get("temperature_scale") or BSV_UVC_TEMP_FORMULA_SCALE)
                interface["temperature_offset"] = float(interface.get("temperature_offset") if interface.get("temperature_offset") is not None else BSV_UVC_TEMP_FORMULA_OFFSET)
                interface["calibration_reference_C"] = interface.get("calibration_reference_C")
                interface["calibration_tolerance_C"] = float(interface.get("calibration_tolerance_C") or 2.0)
            interface["expected_update_hz"] = max(
                0.01,
                float(
                    interface.get("expected_update_hz")
                    or profile_hint.get("expected_update_hz")
                    or self.sample_rate_hz
                ),
            )
            interface["freshness_seconds"] = max(
                0.0,
                float(
                    interface.get("freshness_seconds")
                    or profile_hint.get("freshness_seconds")
                    or (2.5 / interface["expected_update_hz"])
                ),
            )
            interface["physical_interface_id"] = str(
                interface.get("physical_interface_id") or ""
            ).strip()
            interface["physical_port_id"] = str(
                interface.get("physical_port_id") or ""
            ).strip()
            interface["physical_interface_kind"] = str(
                interface.get("physical_interface_kind")
                or SENSOR_INTERFACE_PROFILES.get(interface["role"], SENSOR_INTERFACE_PROFILES["custom"]).get("physical_kind")
                or ""
            ).strip().lower()
            interface["physical_verified"] = bool(interface.get("physical_verified", False))
            interface["physical_fallback"] = bool(interface.get("physical_fallback", False))
            interface["physical_vid"] = interface.get("physical_vid")
            interface["physical_pid"] = interface.get("physical_pid")
            interface["physical_serial"] = str(interface.get("physical_serial") or "").strip()
            interface["physical_location"] = str(interface.get("physical_location") or "").strip()
            interface["device_profile_id"] = str(
                interface.get("device_profile_id") or interface.get("profile_id") or ""
            ).strip()
            interface["identity_confirmed"] = bool(interface.get("identity_confirmed", False))
            interface["source_address"] = str(interface.get("source_address") or "").strip()
            interface["device_subnet"] = str(interface.get("device_subnet") or "").strip()
            interface["network_interface_name"] = str(interface.get("network_interface_name") or "").strip()
            interface["route_interface_id"] = str(interface.get("route_interface_id") or "").strip()
            normalized_interfaces.append(interface)
        if not normalized_interfaces:
            raise ValueError("至少配置一个采集接口")
        self.interfaces = normalized_interfaces
        binding_issues = _physical_interface_binding_issues(
            normalized_interfaces, self.acquisition_mode
        )
        self.configuration_issues.extend(binding_issues)
        if binding_issues and not diagnostic_validation:
            raise ValueError(str(binding_issues[0]["message"]))
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
                message = "真实接口采集模式不能使用本地模拟驱动"
                if diagnostic_validation:
                    self.configuration_issues.append({
                        "code": "simulation_driver_in_real_mode",
                        "message": message,
                        "interface_ids": [
                            str(item.get("id") or "") for item in normalized_interfaces
                            if item.get("enabled", True) and item.get("driver") == "simulator"
                        ],
                    })
                else:
                    raise ValueError(message)
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
            if not requested_assignments and (
                len(enabled_ids) == 1
                and next(
                    item for item in normalized_interfaces
                    if item.get("enabled", True)
                ).get("driver") == "simulator"
            ):
                # Backward-compatible single-file replay: without an explicit
                # map, the one simulator supplies every channel the operator
                # selected.  The browser sends an explicit map whenever more
                # than one logical interface is configured.
                interface_id = next(iter(enabled_ids))
                self.interface_channel_assignments = {
                    interface_id: list(self.selected_sensors)
                }
            else:
                self.interface_channel_assignments = _resolve_interface_channel_assignments(
                    normalized_interfaces,
                    requested_assignments,
                    list(self.selected_sensors),
                )
            routed = {
                str(channel)
                for interface_id, channels in self.interface_channel_assignments.items()
                if str(interface_id) in enabled_ids
                for channel in channels
                if str(channel) in allowed_sensors
            }
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


ACQUISITION_TRANSPORT_METADATA_FIELDS = frozenset(
    {
        "runtime_revision",
        "simulation_execution_choice",
        "simulation_source_id",
        "simulation_source_ready",
        "simulation_source_transfer",
    }
)


def acquisition_config_from_payload(payload: dict[str, Any] | None) -> AcquisitionConfig:
    """Build a strict acquisition config after removing known route metadata.

    Unknown acquisition fields deliberately remain in ``values`` so the
    dataclass raises instead of silently accepting a misspelled configuration.
    """
    values = dict(payload or {})
    for field_name in ACQUISITION_TRANSPORT_METADATA_FIELDS:
        values.pop(field_name, None)
    return AcquisitionConfig(**values)


def acquisition_diagnostic_config_from_payload(
    payload: dict[str, Any] | None,
) -> AcquisitionConfig:
    """Build a diagnostic config that reports binding faults without aborting.

    The relaxed switch is supplied internally and is never accepted as a
    request field, so it cannot weaken capture-start validation.
    """

    values = dict(payload or {})
    for field_name in ACQUISITION_TRANSPORT_METADATA_FIELDS:
        values.pop(field_name, None)
    values.pop("diagnostic_validation", None)
    return AcquisitionConfig(diagnostic_validation=True, **values)


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
            mustexist=True,
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
        self.index = 0
        self._files: list[Path] = []
        self._file_index = 0
        self._handle = None
        self._reader: csv.DictReader | None = None

    def _source_files(self) -> list[Path]:
        if not self.path.exists():
            raise FileNotFoundError(f"模拟数据文件不存在：{self.path}")
        if not self.path.is_file():
            raise FileNotFoundError(f"模拟数据文件不存在：{self.path}")
        return [self.path]

    @staticmethod
    def _open_reader(path: Path):
        last_error: UnicodeDecodeError | None = None
        for encoding in ("utf-8-sig", "gb18030"):
            handle = path.open("r", encoding=encoding, newline="")
            try:
                reader = csv.DictReader(handle)
                _ = reader.fieldnames
                return handle, reader
            except UnicodeDecodeError as exc:
                last_error = exc
                handle.close()
        if last_error is not None:
            raise last_error
        raise ValueError(f"无法读取模拟 CSV：{path}")

    @classmethod
    def _header(cls, path: Path) -> list[str]:
        handle, reader = cls._open_reader(path)
        try:
            return [str(name or "").strip() for name in (reader.fieldnames or [])]
        finally:
            handle.close()

    def _open_current(self) -> None:
        self.close()
        self._handle, self._reader = self._open_reader(self._files[self._file_index])

    def open(self) -> None:
        self._files = self._source_files()
        available = set().union(*(set(self._header(path)) for path in self._files))
        missing = [name for name in self.sensor_columns if name not in available]
        if missing:
            raise ValueError(f"模拟数据缺少传感器列：{missing}")
        self.index = 0
        self._file_index = 0
        self._open_current()

    def read_sample(self) -> dict[str, float] | None:
        if not self._files or self._reader is None:
            return None
        for _ in range(len(self._files) + 1):
            try:
                record = next(self._reader)
            except StopIteration:
                self._file_index = (self._file_index + 1) % len(self._files)
                self._open_current()
                continue
            self.index += 1
            return normalize_sample(
                {name: record.get(name) for name in self.sensor_columns}
            )
        return None

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        self._reader = None
        if handle is not None:
            handle.close()


class FolderCsvSimulatorDriver(SimulatorDriver):
    """Replay every CSV in a capture folder in lexical file order."""

    def _source_files(self) -> list[Path]:
        if not self.path.exists() or not self.path.is_dir():
            raise FileNotFoundError(f"模拟采集文件夹不存在：{self.path}")
        files = sorted(self.path.rglob("*.csv"))
        if not files:
            raise FileNotFoundError(f"模拟采集文件夹不包含 CSV：{self.path}")
        return files


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
                 slave_id: int = DEFAULT_PLC_SLAVE_ID, timeout: float = 1.0,
                 *, function_code: int = 3, word_order: str = "low_high",
                 byte_order: str = "big", scales: dict[str, float] | None = None,
                 source_address: str = "", profile_id: str = DEFAULT_PLC_PROFILE_ID,
                 profile_version: int = 1) -> None:
        self.endpoint = endpoint or f"{DEFAULT_PLC_IP}:{DEFAULT_PLC_PORT}"
        self.register_map = dict(register_map or PLC_DEFAULT_REGISTER_MAP)
        self.slave_id = int(slave_id)
        self.timeout = max(0.1, float(timeout))
        self.function_code = int(function_code)
        if self.function_code != 3:
            raise ValueError("当前PLC设备档仅支持Modbus FC03")
        self.word_order = str(word_order or "low_high").lower()
        self.byte_order = str(byte_order or "big").lower()
        if self.word_order not in {"low_high", "high_low"}:
            raise ValueError("PLC word_order必须是low_high或high_low")
        if self.byte_order not in {"big", "little"}:
            raise ValueError("PLC byte_order必须是big或little")
        self.scales = {str(key): float(value) for key, value in (scales or {}).items()}
        self.source_address = str(source_address or "").strip()
        self.profile_id = str(profile_id)
        self.profile_version = int(profile_version)
        self.quality_metadata = {
            "device_profile": self.profile_id,
            "profile_version": self.profile_version,
            "unit_id": self.slave_id,
            "function_code": self.function_code,
            "word_order": self.word_order,
            "byte_order": self.byte_order,
            "source_address": self.source_address,
        }
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
        self.sock = socket.create_connection(
            (host, int(port_text)),
            timeout=2.0,
            source_address=((self.source_address, 0) if self.source_address else None),
        )
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
            self.slave_id, self.function_code, int(start_address), int(quantity),
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
        if function_code != self.function_code:
            raise IOError(
                f"Modbus功能码不匹配：期望={self.function_code:#x}，实际={function_code:#x}"
            )
        byte_count = pdu[1]
        register_data = pdu[2:2 + byte_count]
        return [
            struct.unpack(">H", register_data[index:index + 2])[0]
            for index in range(0, len(register_data), 2)
            if len(register_data[index:index + 2]) == 2
        ]

    def _registers_to_float(self, first_word: int, second_word: int) -> float:
        low_word, high_word = (
            (first_word, second_word)
            if self.word_order == "low_high"
            else (second_word, first_word)
        )
        raw = struct.pack(">HH", high_word, low_word)
        if self.byte_order == "little":
            raw = raw[1::-1] + raw[3:1:-1]
        value = struct.unpack(">f", raw)[0]
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
                result[name] = round(
                    self._registers_to_float(low, high) * self.scales.get(name, 1.0),
                    6,
                )
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
                 timeout: float = 1.5, *, source_address: str = "",
                 robtarget_path: str = ABB_ROBTARGET_PATH,
                 rapid_symbol_base_path: str = ABB_RAPID_SYMBOL_BASE_PATH,
                 mechanical_unit: str = "ROB_1", coordinate_system: str = "Base",
                 task: str = ABB_PROCESS_TASK, module: str = ABB_PROCESS_MODULE,
                 speed_variable: str = ABB_SPEED_VARIABLE,
                 profile_id: str = DEFAULT_ABB_PROFILE_ID,
                 profile_version: int = 1) -> None:
        if str(endpoint).startswith("http"):
            self.base_url = str(endpoint).rstrip("/")
        else:
            self.base_url = f"http://{str(endpoint).split(':')[0]}"
        self.username = username
        self.password = password
        self.timeout = max(0.1, float(timeout))
        self.source_address = str(source_address or "").strip()
        self.robtarget_path = str(robtarget_path or ABB_ROBTARGET_PATH)
        self.rapid_symbol_base_path = str(rapid_symbol_base_path or ABB_RAPID_SYMBOL_BASE_PATH)
        self.mechanical_unit = str(mechanical_unit)
        self.coordinate_system = str(coordinate_system)
        self.task = str(task)
        self.module = str(module)
        self.speed_variable = str(speed_variable)
        self.profile_id = str(profile_id)
        self.profile_version = int(profile_version)
        self._last_time: float | None = None
        self._last_x: float | None = None
        self._last_y: float | None = None
        self._last_z: float | None = None
        self.quality_metadata = {
            "device_profile": self.profile_id,
            "profile_version": self.profile_version,
            "mechanical_unit": self.mechanical_unit,
            "coordinate_system": self.coordinate_system,
            "robtarget_path": self.robtarget_path,
            "speed_source_time_verified": False,
            "source_address": self.source_address,
        }

    def open(self) -> None:
        return

    def _fetch_json(self, path: str) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(self.base_url)
        connection = http.client.HTTPConnection(
            parsed.hostname,
            parsed.port or 80,
            timeout=self.timeout,
            source_address=((self.source_address, 0) if self.source_address else None),
        )
        credentials = base64.b64encode(
            f"{self.username}:{self.password}".encode("utf-8")
        ).decode("ascii")
        try:
            connection.request(
                "GET", path,
                headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
            )
            response = connection.getresponse()
            if response.status in {401, 403}:
                raise PermissionError(f"ABB RWS授权失败：HTTP {response.status}")
            if response.status >= 400:
                raise IOError(f"ABB RWS资源不可用：HTTP {response.status} {path}")
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("ABB RWS响应不是JSON对象")
            return payload
        finally:
            connection.close()

    def _fetch_position(self) -> tuple[float, float, float]:
        payload = self._fetch_json(self.robtarget_path)
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
        path = "/".join(
            urllib.parse.quote(str(part), safe="")
            for part in (task, module, variable)
        )
        payload = self._fetch_json(f"{self.rapid_symbol_base_path}/{path}?json=1")
        value = self._find_json_value(payload)
        if value is None:
            raise ValueError(f"ABB变量{variable}响应中没有value字段")
        return str(value)

    def read_sample(self) -> dict[str, float] | None:
        try:
            x, y, z = self._fetch_position()
        except Exception as exc:
            self.quality_metadata["protocol_error"] = str(exc)
            return None
        self.quality_metadata.pop("protocol_error", None)
        now = time.time()
        speed: float | None = None
        if self._last_time is not None and self._last_x is not None:
            delta_time = now - self._last_time
            if delta_time > 0:
                speed = round(math.sqrt(
                    (x - self._last_x) ** 2
                    + (y - self._last_y) ** 2
                    + (z - self._last_z) ** 2
                ) / delta_time, 2)
                self.quality_metadata["speed_source_time_verified"] = True
        self._last_time, self._last_x, self._last_y, self._last_z = now, x, y, z
        result = {"ABB_X": x, "ABB_Y": y, "ABB_Z": z}
        if speed is not None:
            result["线速度"] = speed
        return result


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


class VendorDriverError(IOError):
    """Structured vendor failure that keeps authoritative native return codes."""

    def __init__(
        self, message: str, *, stage: str, return_code: int, last_error_code: int,
    ) -> None:
        super().__init__(message)
        self.stage = str(stage)
        self.return_code = int(return_code)
        self.last_error_code = int(last_error_code)

    def evidence(self) -> dict[str, Any]:
        return {
            "vendor_stage": self.stage,
            "vendor_return_code": self.return_code,
            "vendor_last_error_code": self.last_error_code,
        }


class UvcThermalDriver(SampleDriver):
    """BSV UVC native DLL reader; produces a calibrated ROI temperature."""

    def __init__(self, dll_path: str = "", roi: Any = None, poll_retries: int = 3,
                 *, frame_width: int = BSV_UVC_WIDTH,
                 frame_height: int = BSV_UVC_HEIGHT,
                 temperature_range_code: int = BSV_UVC_TEMP_RANGE_CODE,
                 temperature_scale: float = BSV_UVC_TEMP_FORMULA_SCALE,
                 temperature_offset: float = BSV_UVC_TEMP_FORMULA_OFFSET) -> None:
        self.dll_path = str(dll_path or "").strip()
        self.roi = _parse_roi(roi)
        self.poll_retries = max(1, int(poll_retries))
        self.frame_width = int(frame_width)
        self.frame_height = int(frame_height)
        if (self.frame_width, self.frame_height) != (BSV_UVC_WIDTH, BSV_UVC_HEIGHT):
            raise ValueError("UVC设备档帧尺寸必须与本机DLL合同256×192一致")
        self.temperature_range_code = int(temperature_range_code)
        self.temperature_scale = float(temperature_scale)
        self.temperature_offset = float(temperature_offset)
        self.dll = None
        self.yuv_buffer = (ctypes.c_ubyte * BSV_UVC_YUV_BYTES)()
        self.temp_buffer = (ctypes.c_ushort * BSV_UVC_PIXELS)()
        self.frame_count = 0
        self.quality_metadata = {
            "dll_found": False,
            "device_open": False,
            "continuous_frames": 0,
            "frame_size": [self.frame_width, self.frame_height],
            "temperature_range_code": self.temperature_range_code,
            "temperature_formula": f"raw/{self.temperature_scale}+{self.temperature_offset}",
            "roi": list(self.roi) if self.roi else [0, 0, self.frame_width, self.frame_height],
        }

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
        dll_path = self._find_dll()
        self.quality_metadata["dll_found"] = True
        self.quality_metadata["dll_path"] = str(dll_path)
        self.dll = ctypes.CDLL(str(dll_path))
        self._bind_api()
        result = self.dll.BsvSetTempRangeCode(self.temperature_range_code)
        if result < 0:
            error = self.dll.BsvGetLastError()
            self.quality_metadata.update({
                "vendor_stage": "temperature_range",
                "vendor_return_code": int(result),
                "vendor_last_error_code": int(error),
            })
            raise VendorDriverError(
                f"BSV UVC 设置温度量程失败：code={result}, lastError={error}",
                stage="temperature_range", return_code=result, last_error_code=error,
            )
        result = self.dll.BsvOpen()
        if result <= 0:
            error = self.dll.BsvGetLastError()
            self.quality_metadata.update({
                "vendor_stage": "device_open",
                "vendor_return_code": int(result),
                "vendor_last_error_code": int(error),
            })
            self.dll = None
            raise VendorDriverError(
                f"BSV UVC 打开设备失败：code={result}, lastError={error}",
                stage="device_open", return_code=result, last_error_code=error,
            )
        result = self.dll.BsvStart()
        if result <= 0:
            error = self.dll.BsvGetLastError()
            self.quality_metadata.update({
                "vendor_stage": "stream_start",
                "vendor_return_code": int(result),
                "vendor_last_error_code": int(error),
            })
            self.close()
            raise VendorDriverError(
                f"BSV UVC 启动视频流失败：code={result}, lastError={error}",
                stage="stream_start", return_code=result, last_error_code=error,
            )
        self.quality_metadata["device_open"] = True

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
                total += self.temp_buffer[base + column] / self.temperature_scale + self.temperature_offset
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
                self.frame_count += 1
                self.quality_metadata["continuous_frames"] = self.frame_count
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
        self.quality_metadata["device_open"] = False


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
        start_command: bytes | None = None,
        stop_command: bytes | None = None,
        protocol_verified: bool = False,
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
        # Commands are deliberately opt-in.  The vendor baud rate is known, but
        # the binary start/stop protocol is not verified by physical evidence.
        self.start_command = start_command
        self.stop_command = stop_command
        self.protocol_verified = bool(protocol_verified)
        self.quality_metadata: dict[str, Any] = {
            "hardware_protocol_verified": self.protocol_verified,
            "pressure_peak": 0.0,
            "contact_area": 0,
            "valid_pixel_fraction": 0.0,
        }

    def open(self) -> None:
        import serial
        self.serial = serial.Serial(
            self.endpoint, self.baudrate, bytesize=8,
            parity="N", stopbits=1, timeout=0,
        )
        self.rx_buffer = ""
        self.last_data_time = None
        if self.start_command:
            self.serial.write(self.start_command)

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
            self.quality_metadata = {
                "hardware_protocol_verified": self.protocol_verified,
                "matrix_rows": self.matrix_shape[0],
                "matrix_cols": self.matrix_shape[1],
                "pressure_peak": round(float(self.latest_pressure_peak), 2),
                "contact_area": int(self.latest_contact_area),
                "valid_pixel_fraction": round(float(self.latest_valid_fraction), 6),
            }
        if self.last_data_time is None or time.time() - self.last_data_time > 2.0:
            return None
        return {
            "薄膜压力": round(float(self.latest_pressure), 2),
        }

    def close(self) -> None:
        if self.serial is None:
            return
        try:
            if self.stop_command:
                self.serial.write(self.stop_command)
        except Exception:
            pass
        try:
            self.serial.close()
        finally:
            self.serial = None


class MultiInterfaceDriver(SampleDriver):
    """Run physical interfaces independently and expose their latest samples."""
    def __init__(self, configs: list[dict[str, Any]], selected_sensors: list[str], source_file: str = "", assignments: dict[str, list[str]] | None = None) -> None:
        self.configs = [item for item in configs if item.get("enabled", True)]
        self.selected_sensors = selected_sensors
        self.source_file = source_file
        self.assignments = assignments or {}
        self.drivers: list[SampleDriver] = []
        self.cache = ChannelSampleCache(max_events=max(128, len(selected_sensors) * 64))
        self.workers: list[DriverWorker] = []

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
                        function_code=int(item.get("function_code") or 3),
                        word_order=str(item.get("word_order") or "low_high"),
                        byte_order=str(item.get("byte_order") or "big"),
                        scales=(item.get("scales") if isinstance(item.get("scales"), dict) else None),
                        source_address=str(item.get("source_address") or ""),
                        profile_id=str(item.get("profile_id") or DEFAULT_PLC_PROFILE_ID),
                        profile_version=int(item.get("profile_version") or 1),
                    )
                elif driver_name == "abb_robot":
                    driver = AbbRobotDriver(
                        str(item.get("endpoint") or DEFAULT_ABB_IP),
                        str(item.get("username") or DEFAULT_ABB_USER),
                        str(item.get("password") or DEFAULT_ABB_PASSWORD),
                        float(item.get("timeout") or 1.5),
                        source_address=str(item.get("source_address") or ""),
                        robtarget_path=str(item.get("robtarget_path") or ABB_ROBTARGET_PATH),
                        rapid_symbol_base_path=str(item.get("rapid_symbol_base_path") or ABB_RAPID_SYMBOL_BASE_PATH),
                        mechanical_unit=str(item.get("mechanical_unit") or "ROB_1"),
                        coordinate_system=str(item.get("coordinate_system") or "Base"),
                        task=str(item.get("task") or ABB_PROCESS_TASK),
                        module=str(item.get("module") or ABB_PROCESS_MODULE),
                        speed_variable=str(item.get("speed_variable") or ABB_SPEED_VARIABLE),
                        profile_id=str(item.get("profile_id") or DEFAULT_ABB_PROFILE_ID),
                        profile_version=int(item.get("profile_version") or 1),
                    )
                elif driver_name == "uvc_thermal":
                    driver = UvcThermalDriver(
                        str(item.get("dll_path") or ""), item.get("roi"),
                        frame_width=int(item.get("frame_width") or BSV_UVC_WIDTH),
                        frame_height=int(item.get("frame_height") or BSV_UVC_HEIGHT),
                        temperature_range_code=int(item.get("temperature_range_code") or BSV_UVC_TEMP_RANGE_CODE),
                        temperature_scale=float(item.get("temperature_scale") or BSV_UVC_TEMP_FORMULA_SCALE),
                        temperature_offset=float(item.get("temperature_offset") if item.get("temperature_offset") is not None else BSV_UVC_TEMP_FORMULA_OFFSET),
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
                    start_command = item.get("start_command")
                    stop_command = item.get("stop_command")
                    driver = M3232PressureDriver(
                        str(item.get("endpoint") or "COM8"),
                        int(item.get("baudrate") or M3232_BAUDRATE),
                        int(item.get("matrix_rows") or 0),
                        int(item.get("matrix_cols") or 0),
                        (
                            str(start_command).encode("ascii")
                            if start_command not in (None, "")
                            else None
                        ),
                        (
                            str(stop_command).encode("ascii")
                            if stop_command not in (None, "")
                            else None
                        ),
                        bool(item.get("protocol_verified", False)),
                    )
                elif driver_name == "simulator":
                    driver = SimulatorDriver(Path(self.source_file or DEFAULT_SIMULATOR_FILE), self.selected_sensors)
                else:
                    raise ValueError(f"unsupported interface driver: {driver_name}")
                self.drivers.append(driver)
                interface_id = str(item.get("id") or f"interface_{len(self.drivers)}")
                allowed = self.assignments.get(interface_id)
                if allowed is None:
                    allowed = list(self.selected_sensors)
                worker = DriverWorker(
                    interface_id,
                    driver,
                    allowed,
                    self.cache,
                    poll_interval_seconds=float(item.get("poll_interval_seconds") or 0.002),
                )
                self.workers.append(worker)
                worker.start()
        except Exception:
            self.close()
            raise

    def read_sample(self) -> dict[str, float] | None:
        merged = {
            name: sample.value
            for name, sample in self.cache.snapshot().items()
            if sample.value is not None and name in self.selected_sensors
        }
        return merged or None

    def worker_status(self) -> list[dict[str, Any]]:
        return [worker.status() for worker in self.workers]

    def close(self) -> None:
        for worker in self.workers:
            worker.stop(timeout_seconds=0.5)
        self.workers = []
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


def _freshness_by_channel(config: AcquisitionConfig) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in config.interfaces or []:
        if not item.get("enabled", True):
            continue
        interface_id = str(item.get("id") or "")
        freshness = max(
            0.0,
            float(item.get("freshness_seconds") or (2.5 / config.sample_rate_hz)),
        )
        for channel in config.interface_channel_assignments.get(interface_id, []):
            if channel in (config.selected_sensors or []):
                result[channel] = freshness
    return result


def _readiness_report_for_result(
    result: dict[str, Any], config_fingerprint: str, checked_at: float
) -> ReadinessReport:
    """Convert probe facts into the single layered readiness contract."""

    disabled = not result.get("enabled", True)
    simulation = result.get("driver") == "simulator"
    expected = tuple(str(value) for value in result.get("expected_channels", []))
    detected = tuple(str(value) for value in result.get("detected_channels", []))
    missing = tuple(str(value) for value in result.get("missing_channels", []))
    invalid = tuple(str(value) for value in result.get("invalid_channels", []))
    stale = tuple(str(value) for value in result.get("stale_channels", []))
    state = str(result.get("state") or "no_valid_frame")
    physical_ok = bool(result.get("physical_verified")) and not result.get("physical_fallback")
    endpoint_ok = (
        physical_ok
        and bool(result.get("endpoint"))
        and result.get("state") != "network_path_invalid"
        and (
            not isinstance(result.get("network_path"), dict)
            or bool(result["network_path"].get("ok"))
        )
    )
    driver_open = bool(result.get("driver_opened"))
    protocol_ok = bool(result.get("protocol_identified"))
    channels_ok = not missing and bool(detected or not expected)
    values_ok = channels_ok and not invalid and not result.get("semantic_errors")
    stable_ok = values_ok and not stale and bool(result.get("freshness_stable"))
    if disabled or simulation:
        physical_state = endpoint_state = "not_applicable"
    else:
        physical_state = "passed" if physical_ok else "failed"
        endpoint_state = "passed" if endpoint_ok else "failed"
    stage_values = (
        ("port_present", physical_state, {
            "physical_interface_id": result.get("physical_interface_id", ""),
            "physical_port_id": result.get("physical_port_id", ""),
            "verified": physical_ok,
        }, "重新识别并选择与设备匹配的物理端口"),
        ("endpoint_compatible", endpoint_state, {
            "endpoint": result.get("endpoint", ""),
            "protocol": result.get("protocol", ""),
            "network_path": result.get("network_path"),
        }, "核对设备端点、协议以及工业网卡绑定"),
        ("driver_open", "not_applicable" if disabled or simulation else ("passed" if driver_open else "failed"), {
            "opened": driver_open,
            "errors": result.get("errors", []),
        }, "检查驱动、权限、DLL和设备占用状态"),
        ("protocol_identified", "not_applicable" if disabled or simulation else ("passed" if protocol_ok else "failed"), {
            "identified": protocol_ok,
            "protocol_evidence": result.get("protocol_evidence", {}),
        }, "核对设备身份、协议版本、字序、资源路径或通道类型"),
        ("channels_complete", "not_applicable" if disabled else ("passed" if channels_ok else "failed"), {
            "expected": list(expected), "detected": list(detected), "missing": list(missing),
        }, "连接缺失通道并在完整检查窗口内重新检查"),
        ("values_valid", "not_applicable" if disabled else ("passed" if values_ok else "failed"), {
            "invalid": list(invalid), "semantic_errors": result.get("semantic_errors", []),
        }, "核对单位、量程、字序、倍率、断偶和参考刺激"),
        ("freshness_stable", "not_applicable" if disabled else ("passed" if stable_ok else "failed"), {
            "stale": list(stale), "sample_counts": result.get("sample_counts", {}),
            "required_samples": result.get("required_samples", 2),
        }, "保持设备连续输出并消除超时或陈旧通道"),
    )
    stages = [
        ReadinessStage(
            name=name,
            state=stage_state,
            evidence=evidence,
            started_at=float(result.get("probe_started_at") or checked_at),
            finished_at=checked_at,
            remediation="" if stage_state in {"passed", "not_applicable"} else remediation,
        )
        for name, stage_state, evidence, remediation in stage_values
    ]
    ready = state == "ready" and all(
        stage.state in {"passed", "not_applicable"} for stage in stages
    )
    stages.append(ReadinessStage(
        name="ready",
        state="passed" if ready else ("not_applicable" if disabled else "failed"),
        evidence={"interface_state": state},
        started_at=float(result.get("probe_started_at") or checked_at),
        finished_at=checked_at,
        remediation="" if ready or disabled else str(result.get("message") or "修复失败层后重新检查"),
    ))
    return ReadinessReport(
        interface_id=str(result.get("id") or ""),
        state=("disabled" if disabled else state),
        stages=tuple(stages),
        expected_channels=expected,
        detected_channels=detected,
        missing_channels=missing,
        invalid_channels=invalid,
        stale_channels=stale,
        message=str(result.get("message") or ""),
        config_fingerprint=config_fingerprint,
    )


def _validate_device_semantics(
    item: dict[str, Any], values: dict[str, float], protocol_evidence: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    ranges = item.get("valid_ranges") if isinstance(item.get("valid_ranges"), dict) else {}
    for name, bounds in ranges.items():
        if name not in values or not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            continue
        minimum, maximum = float(bounds[0]), float(bounds[1])
        if not minimum <= float(values[name]) <= maximum:
            errors.append(f"{name}={values[name]:g}超出设备档范围[{minimum:g}, {maximum:g}]")
    references = item.get("reference_values") if isinstance(item.get("reference_values"), dict) else {}
    tolerances = item.get("reference_tolerances") if isinstance(item.get("reference_tolerances"), dict) else {}
    for name, reference in references.items():
        if name not in values:
            continue
        tolerance = float(tolerances.get(name, 0.0))
        if abs(float(values[name]) - float(reference)) > tolerance:
            errors.append(
                f"{name}与参考刺激偏差超限；核对寄存器、字序、字节序和倍率"
            )
    role = str(item.get("role") or "")
    if role == "plc":
        if int(item.get("function_code") or 0) != 3:
            errors.append("PLC功能码不是设备档批准的FC03")
        protocol_evidence.update({
            "profile_id": item.get("profile_id"),
            "profile_version": item.get("profile_version"),
            "unit_id": item.get("slave_id"),
            "function_code": item.get("function_code"),
            "register_map": item.get("register_map"),
            "word_order": item.get("word_order"),
            "byte_order": item.get("byte_order"),
            "scales": item.get("scales"),
            "units": item.get("units"),
            "valid_ranges": ranges,
            "reference_values": references,
        })
    elif role == "robot":
        path = str(item.get("robtarget_path") or "")
        mechanical_unit = str(item.get("mechanical_unit") or "")
        coordinate = str(item.get("coordinate_system") or "")
        if mechanical_unit and mechanical_unit not in path:
            errors.append("ABB RWS目标路径与机械单元不匹配")
        if coordinate and f"coordinate={coordinate}".casefold() not in path.casefold():
            errors.append("ABB RWS目标路径与坐标系不匹配")
        protocol_evidence.update({
            "profile_id": item.get("profile_id"),
            "profile_version": item.get("profile_version"),
            "mechanical_unit": mechanical_unit,
            "coordinate_system": coordinate,
            "robtarget_path": path,
            "task": item.get("task"),
            "module": item.get("module"),
            "speed_variable": item.get("speed_variable"),
            "units": item.get("units"),
        })
    elif role == "thermocouple":
        configured = list(item.get("channel_types") or [])[:8]
        reported = list(protocol_evidence.get("reported_channel_types") or [])[:8]
        if reported and configured != reported and not item.get("channel_types_confirmed"):
            errors.append(
                f"SMRF设备报告类型{reported}与配置{configured}不一致，必须确认或采用设备配置"
            )
        if len(configured) != 8:
            errors.append("SMRF必须配置八路通道类型")
        if not item.get("cold_junction_compensation", True):
            errors.append("SMRF冷端补偿未启用")
    elif role == "thermal_uvc":
        roi = _parse_roi(item.get("roi"))
        if item.get("roi") not in (None, "", []) and roi is None:
            errors.append("UVC ROI格式无效")
        if int(item.get("frame_width") or 0) != BSV_UVC_WIDTH or int(item.get("frame_height") or 0) != BSV_UVC_HEIGHT:
            errors.append("UVC帧尺寸与设备档不一致")
        reference = _finite(item.get("calibration_reference_C"))
        measured = values.get("ROI平均温度")
        if reference is not None and measured is not None:
            tolerance = float(item.get("calibration_tolerance_C") or 2.0)
            protocol_evidence["calibration"] = {
                "reference_C": reference,
                "measured_C": measured,
                "error_C": measured - reference,
                "tolerance_C": tolerance,
            }
            if abs(measured - reference) > tolerance:
                errors.append("UVC参考温度偏差超出批准公差")
    return errors


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


def readiness_cache_identity(
    config: AcquisitionConfig, config_fingerprint: str | None = None,
) -> dict[str, str]:
    return {
        "acquisition_mode": str(config.acquisition_mode),
        "execution_host": str(config.execution_host or "server"),
        "execution_device_id": str(config.execution_device_id or ""),
        "runtime_revision": str(os.environ.get("AFP_RUNTIME_REVISION") or ""),
        "config_fingerprint": str(
            config_fingerprint or readiness_config_fingerprint(config)
        ),
    }


class AcquisitionManager:
    def __init__(self, capture_root: Path = DEFAULT_CAPTURE_ROOT) -> None:
        self.capture_root = capture_root
        self.capture_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._stream_activity = threading.Condition(self.lock)
        self.lifecycle_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.driver: SampleDriver | None = None
        self.config: AcquisitionConfig | None = None
        self.rows: deque[dict[str, Any]] = deque(maxlen=200000)
        self.timestamps: deque[float] = deque(maxlen=200000)
        self.frame_quality: deque[dict[str, str]] = deque(maxlen=200000)
        self.quality_metrics: AcquisitionQualityMetrics | None = None
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
            "acquisition_mode": "real",
            "checked_at": None,
            "interfaces": [],
            "sensors": [],
        }
        self._latest_simulation_check_result: dict[str, Any] = {
            "acquisition_mode": "simulation",
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
        if discover_windows_usb_topology is None:
            usb_topology = {
                "schema_version": 1, "state": "unavailable",
                "provider": "module_unavailable",
                "errors": ["USB physical port topology module is unavailable"],
                "docks": [], "usb_ports": [], "devices": [],
            }
        else:
            try:
                usb_topology = discover_windows_usb_topology()
            except Exception as exc:
                usb_topology = {
                    "schema_version": 1, "state": "unavailable",
                    "provider": "windows_usb_hub_ioctl",
                    "errors": [f"USB topology discovery failed: {exc}"],
                    "docks": [], "usb_ports": [], "devices": [],
                }
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
                    "serial": str(item.serial_number or ""),
                    "hwid": str(getattr(item, "hwid", "") or ""),
                    "location": str(getattr(item, "location", "") or ""),
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
                    "serial": str(item.serial_number or ""),
                    "hwid": str(getattr(item, "hwid", "") or ""),
                    "location": str(getattr(item, "location", "") or ""),
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
                physical_interfaces.append({
                    "id": f"ethernet:{name}", "kind": "ethernet",
                    "protocol": "ethernet", "endpoint": name,
                    "label": (
                        f"网卡 {name}（{', '.join(ipv4)}）"
                        if ipv4 else f"网卡 {name}（当前无 IPv4 地址）"
                    ),
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
        physical_interfaces = annotate_discovery_evidence(physical_interfaces)
        plc_discovery = build_network_discovery_summary(
            DEFAULT_PLC_IP, DEFAULT_PLC_PORT, plc_reachable, physical_interfaces
        )
        abb_discovery = build_network_discovery_summary(
            DEFAULT_ABB_IP, 80, abb_reachable, physical_interfaces
        )
        interface_transport_catalog = _associate_usb_topology(usb_topology, physical_interfaces)
        return {
            "ports": ports,
            "physical_interfaces": physical_interfaces,
            "usb_topology": usb_topology,
            "interface_transport_catalog": interface_transport_catalog,
            "hid_devices": smrf_devices,
            "defaults": default_capture_interfaces(),
            "sensor_type_profiles": sensor_interface_profiles(),
            "channel_metadata": SENSOR_CHANNEL_METADATA,
            "error": error,
            "network_discovery": {"plc": plc_discovery, "abb": abb_discovery},
            "plc_tcp_probe_reachable": plc_reachable,
            "abb_tcp_probe_reachable": abb_reachable,
            # Compatibility keys now mean verified device reachability, not a
            # generic TCP accept that may have been intercepted by a TUN.
            "plc_reachable": plc_discovery["device_reachable"],
            "abb_reachable": abb_discovery["device_reachable"],
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
        source_open = False
        sample_seen = False
        if config.acquisition_mode == "simulation":
            driver = build_driver(config)
            try:
                driver.open()
                source_open = True
                probe_started = time.time()
                while time.time() - probe_started < max(0.5, float(timeout_seconds)):
                    sample = driver.read_sample()
                    if sample:
                        sample_seen = True
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
            checked_at = time.time()
            config_fingerprint = readiness_config_fingerprint(config)
            missing = sorted(name for name in selected if received.get(name, 0) <= 0)
            invalid = sorted(name for name in selected if invalid_received.get(name, 0) > 0)
            schema_compatible = bool(sample_seen or not selected) and source_open
            channels_complete = source_open and not missing
            values_valid = channels_complete and not invalid
            replay_ready = bool(source_open and schema_compatible and channels_complete and values_valid)
            if source_open and missing:
                errors.append(f"模拟数据源缺少已选通道：{'、'.join(missing)}")
            if invalid:
                errors.append(f"模拟数据源包含无效数值通道：{'、'.join(invalid)}")
            stage_values = (
                ("source_open", source_open, "模拟数据源无法打开"),
                ("schema_compatible", schema_compatible, "模拟数据源结构无法解析为当前方案"),
                ("channels_complete", channels_complete, "模拟数据源缺少已选通道"),
                ("values_valid", values_valid, "模拟数据源包含非有限或无效数值"),
                ("replay_ready", replay_ready, "修复模拟数据源后重新检查"),
            )
            simulation_stages = [
                {
                    "name": name,
                    "state": "passed" if passed else "failed",
                    "evidence": {
                        "selected_channels": sorted(selected),
                        "missing_channels": missing,
                        "invalid_channels": invalid,
                    },
                    "started_at": check_started,
                    "finished_at": checked_at,
                    "remediation": "" if passed else remediation,
                }
                for name, passed, remediation in stage_values
            ]
            sensors = []
            for name in config.schema_sensors:
                is_selected = name in selected
                if not is_selected:
                    state = "not_selected"
                elif received[name] > 0:
                    state = "ready"
                elif invalid_received[name] > 0:
                    state = "invalid_data"
                else:
                    state = "source_channel_missing"
                sensors.append({
                    "name": name,
                    "selected": is_selected,
                    "received_samples": int(received[name]),
                    "invalid_samples": int(invalid_received[name]),
                    "state": state,
                    "message": {
                        "not_selected": "未选择采集",
                        "ready": "模拟数据通道已匹配",
                        "invalid_data": "模拟数据通道没有有效数值",
                        "source_channel_missing": "模拟数据源未包含该通道",
                    }[state],
                    "blocking": is_selected,
                    "ok": not is_selected or state == "ready",
                })
            channel_groups = []
            for item in config.interfaces or []:
                interface_id = str(item.get("id") or "")
                expected = [
                    name for name in config.interface_channel_assignments.get(interface_id, [])
                    if name in selected
                ]
                matched = [name for name in expected if received.get(name, 0) > 0]
                channel_groups.append({
                    "id": interface_id,
                    "role": str(item.get("role") or "custom"),
                    "label": "模拟通道覆盖",
                    "expected_channels": expected,
                    "matched_channels": matched,
                    "missing_channels": [name for name in expected if name not in matched],
                    "ok": all(name in matched for name in expected),
                })
            result = {
                "ok": replay_ready,
                "acquisition_mode": "simulation",
                "readiness_namespace": "simulation_source",
                "driver": config.driver,
                "endpoint": "模拟数据源",
                "elapsed_seconds": checked_at - check_started,
                "errors": errors,
                "sensors": sensors,
                "interfaces": [],
                "channel_groups": channel_groups,
                "readiness_reports": [],
                "simulation_readiness": {
                    "state": "replay_ready" if replay_ready else "source_not_ready",
                    "replay_ready": replay_ready,
                    "stages": simulation_stages,
                    "missing_channels": missing,
                    "invalid_channels": invalid,
                    "message": (
                        "模拟数据源已就绪"
                        if replay_ready else (errors[-1] if errors else "模拟数据源尚未就绪")
                    ),
                },
                "config_fingerprint": config_fingerprint,
                "cache_identity": readiness_cache_identity(config, config_fingerprint),
                "checked_at": checked_at,
            }
            with self.lock:
                self._latest_simulation_check_result = deepcopy(result)
            return result
        interface_results: list[dict[str, Any]] = []
        for item in config.interfaces or []:
            interface_id = str(item.get("id") or "")
            endpoint = str(item.get("endpoint") or interface_id or "未填写地址")
            role = str(item.get("role") or "custom")
            profile = SENSOR_INTERFACE_PROFILES.get(role, SENSOR_INTERFACE_PROFILES["custom"])
            physical_id = str(item.get("physical_interface_id") or "")
            physical_port_id = str(item.get("physical_port_id") or "")
            physical_kind = str(item.get("physical_interface_kind") or profile.get("physical_kind") or "")
            protocol = str(profile.get("protocol") or item.get("driver") or "")
            physical_fallback = bool(item.get("physical_fallback", False))
            physical_verified = item.get("physical_verified") is not False
            physical_warning = (
                "当前未识别到匹配协议，已临时分配串口，仅用于测试"
                if physical_fallback
                else (
                    "物理端口尚未与实时设备关联；接口检查仍会读取原驱动，"
                    "但检查结果不能解除真实采集门禁"
                    if not physical_verified else ""
                )
            )
            expected = [
                name for name in config.interface_channel_assignments.get(interface_id, [])
                if name in selected
            ]
            if not item.get("enabled", True):
                interface_results.append({
                    "id": interface_id, "role": item.get("role", "custom"),
                    "driver": item.get("driver", ""), "endpoint": endpoint,
                    "physical_interface_id": physical_id, "physical_port_id": physical_port_id,
                    "physical_interface_kind": physical_kind,
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
                    "physical_interface_id": physical_id, "physical_port_id": physical_port_id,
                    "physical_interface_kind": physical_kind,
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
            network_path = None
            if role in {"plc", "robot"}:
                target_text = endpoint.removeprefix("http://").removeprefix("https://").split("/", 1)[0]
                if ":" in target_text:
                    target_host, target_port_text = target_text.rsplit(":", 1)
                    try:
                        target_port = int(target_port_text)
                    except ValueError:
                        target_port = DEFAULT_PLC_PORT if role == "plc" else 80
                else:
                    target_host = target_text
                    target_port = DEFAULT_PLC_PORT if role == "plc" else 80
                network_path = validate_industrial_network_path(
                    target_host,
                    item,
                    target_port=target_port,
                    actual_source_address=(str(item.get("actual_source_address") or "") or None),
                    route_interface_id=(str(item.get("route_interface_id") or "") or None),
                )
                if not network_path["ok"]:
                    state = "network_path_invalid"
                    message = "；".join(network_path["errors"])
                    errors.append(f"{endpoint}：{message}")
                    interface_results.append({
                        "id": interface_id, "role": role,
                        "driver": item.get("driver", ""), "endpoint": endpoint,
                        "physical_interface_id": physical_id, "physical_port_id": physical_port_id,
                        "physical_interface_kind": physical_kind,
                        "protocol": protocol, "physical_fallback": physical_fallback,
                        "physical_verified": physical_verified,
                        "physical_warning": physical_warning,
                        "enabled": True, "expected_channels": expected,
                        "detected_channels": [], "missing_channels": expected,
                        "invalid_channels": [], "stale_channels": [],
                        "sample_counts": {}, "invalid_sample_counts": {},
                        "errors": list(network_path["errors"]),
                        "network_path": network_path, "driver_opened": False,
                        "protocol_identified": False, "freshness_stable": False,
                        "state": state, "message": message, "ok": False,
                    })
                    continue
            # A serial fallback is deliberately a test-only binding.  The
            # selected protocol driver (for example UVC or SMRF HID) may call
            # a vendor DLL/API that blocks when the real device is absent.
            # Skip only that incompatible fallback.  A valid configured
            # driver must still be probed when its physical-port association
            # is not verified so the operator keeps the original interface
            # and sensor-data check.  Port verification remains a separate
            # real-capture gate below.
            if physical_fallback:
                state = "not_connected"
                message = (
                    "未识别到匹配协议，已临时分配串口，仅用于测试；"
                    "跳过真实协议探测，请连接设备后重新检查"
                )
                errors.append(f"{endpoint}：{message}")
                interface_results.append({
                    "id": interface_id, "role": item.get("role", "custom"),
                    "driver": item.get("driver", ""), "endpoint": endpoint,
                    "physical_interface_id": physical_id, "physical_port_id": physical_port_id,
                    "physical_interface_kind": physical_kind,
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
            latest_values: dict[str, float] = {}
            last_source_sequences: dict[str, int | None] = {}
            probe_errors: list[str] = []
            interface_driver = None
            probe_started = time.time()
            required_samples = max(2, int(item.get("readiness_min_samples") or 2))
            driver_opened = False
            protocol_evidence: dict[str, Any] = {}
            try:
                interface_driver = MultiInterfaceDriver(
                    [item], list(config.schema_sensors), config.source_file,
                    {interface_id: expected},
                )
                interface_driver.open()
                driver_opened = True
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
                                value = float(sample[name])
                                source_sequence = None
                                if hasattr(interface_driver, "cache"):
                                    cached = interface_driver.cache.latest(name)
                                    source_sequence = cached.source_sequence if cached is not None else None
                                if source_sequence is None or last_source_sequences.get(name) != source_sequence:
                                    detected[name] = detected.get(name, 0) + 1
                                    received[name] += 1
                                    last_source_sequences[name] = source_sequence
                                latest_values[name] = value
                    if expected and all(detected.get(name, 0) >= required_samples for name in expected):
                        break
                    time.sleep(0.01)
                if hasattr(interface_driver, "worker_status"):
                    statuses = interface_driver.worker_status()
                    driver_opened = bool(statuses) and all(status.get("opened") for status in statuses)
                    for status in statuses:
                        if status.get("last_error"):
                            probe_errors.append(str(status["last_error"]))
                        if isinstance(status.get("error_details"), dict):
                            protocol_evidence.update(status["error_details"])
                if hasattr(interface_driver, "cache"):
                    invalid_events = [
                        event for event in interface_driver.cache.events()
                        if event.interface_id == interface_id
                        and event.channel_name in expected
                        and event.quality == ChannelQuality.INVALID
                    ]
                    for event in invalid_events:
                        invalid[event.channel_name] = invalid.get(event.channel_name, 0) + 1
                        invalid_received[event.channel_name] += 1
                drivers = getattr(interface_driver, "drivers", [])
                if drivers:
                    metadata = getattr(drivers[0], "quality_metadata", {})
                    if isinstance(metadata, dict):
                        protocol_evidence.update(deepcopy(metadata))
            except Exception as exc:
                driver_opened = False
                probe_errors.append(str(exc))
                if isinstance(exc, VendorDriverError):
                    protocol_evidence.update(exc.evidence())
            finally:
                if interface_driver is not None:
                    try:
                        interface_driver.close()
                    except Exception:
                        pass
            missing = [name for name in expected if detected.get(name, 0) <= 0]
            stale_channels = [
                name for name in expected
                if 0 < detected.get(name, 0) < required_samples
            ]
            invalid_channels = [name for name in expected if invalid.get(name, 0) > 0]
            semantic_errors = _validate_device_semantics(item, latest_values, protocol_evidence)
            protocol_verified = bool(item.get("protocol_verified", False))
            requires_physical_protocol = str(item.get("driver") or "") == "m3232_pressure"
            protocol_identified = bool(driver_opened and detected and not semantic_errors)
            if role == "thermocouple":
                protocol_identified = protocol_identified and bool(
                    protocol_evidence.get("device_identity")
                    and protocol_evidence.get("command_response")
                    and protocol_evidence.get("reported_channel_types")
                )
            if role == "thermal_uvc":
                protocol_identified = protocol_identified and bool(
                    protocol_evidence.get("device_open")
                    and int(protocol_evidence.get("continuous_frames") or 0) >= required_samples
                )
            if requires_physical_protocol:
                protocol_identified = protocol_identified and protocol_verified
            if detected and missing:
                state = "partial"
                message = (
                    f"接口仅收到部分通道：{'、'.join(sorted(detected))}；"
                    f"缺失：{'、'.join(missing)}"
                )
            elif detected and requires_physical_protocol and not protocol_verified:
                state = "hardware_protocol_unverified"
                message = (
                    "M3232收到JSON/探测数据，但实物帧协议尚未验证；"
                    "该结果不能解除正式采集门禁"
                )
            elif semantic_errors:
                state = "invalid_data"
                message = "设备语义检查失败：" + "；".join(semantic_errors)
            elif protocol_evidence.get("protocol_error"):
                state = "invalid_protocol"
                message = f"协议资源或授权检查失败：{protocol_evidence['protocol_error']}"
            elif stale_channels:
                state = "stale"
                message = f"通道未在检查窗口内连续稳定：{'、'.join(stale_channels)}"
            elif detected and not protocol_identified:
                state = "hardware_protocol_unverified"
                message = "驱动已收到数据，但设备身份或协议语义证据不完整"
            elif detected:
                state = "ready" if physical_verified else "physical_unverified"
                message = f"接口已收到有效数据：{'、'.join(sorted(detected))}"
                if not physical_verified:
                    message += "；物理端口尚未与该实时设备关联，不能启动真实采集"
            elif probe_errors and not driver_opened:
                state, message = "not_connected", f"接口无法打开或读取：{'；'.join(probe_errors)}"
            elif invalid_channels:
                state, message = "invalid_data", f"收到非数值数据：{'、'.join(invalid_channels)}"
            else:
                state, message = "no_valid_frame", f"驱动已打开但未检测到有效帧：{'、'.join(missing)}"
            if state != "ready":
                errors.append(f"{endpoint}：{message}")
            interface_results.append({
                "id": interface_id, "role": item.get("role", "custom"),
                "driver": item.get("driver", ""), "endpoint": endpoint,
                "physical_interface_id": physical_id, "physical_port_id": physical_port_id,
                "physical_interface_kind": physical_kind,
                "protocol": protocol, "physical_fallback": physical_fallback,
                "physical_verified": physical_verified,
                "physical_warning": physical_warning,
                "enabled": True, "expected_channels": expected,
                "detected_channels": sorted(detected), "missing_channels": missing,
                "invalid_channels": invalid_channels, "stale_channels": stale_channels,
                "sample_counts": detected, "required_samples": required_samples,
                "invalid_sample_counts": invalid, "errors": probe_errors,
                "semantic_errors": semantic_errors,
                "network_path": network_path,
                "driver_opened": driver_opened,
                "protocol_identified": protocol_identified,
                "protocol_evidence": protocol_evidence,
                "freshness_stable": not missing and not invalid_channels and not stale_channels,
                "probe_started_at": probe_started,
                "auxiliary_only": auxiliary_only, "state": state,
                "message": message, "ok": state == "ready",
            })
        configuration_issues = list(
            getattr(config, "configuration_issues", []) or []
        )
        issues_by_interface: dict[str, list[dict[str, Any]]] = {}
        for issue in configuration_issues:
            message = str(issue.get("message") or "采集配置无效")
            if message and message not in errors:
                errors.append(message)
            for interface_id in issue.get("interface_ids") or []:
                issues_by_interface.setdefault(str(interface_id), []).append(issue)
        for interface_result in interface_results:
            interface_issues = issues_by_interface.get(
                str(interface_result.get("id") or ""), []
            )
            if not interface_issues:
                continue
            # Keep the actual probe evidence while making the configuration
            # failure explicit.  This is what lets diagnosis report both the
            # wiring/config fault and the observed device state in one run.
            interface_result["probe_state"] = interface_result.get("state")
            interface_result["probe_ok"] = bool(interface_result.get("ok"))
            interface_result["probe_message"] = str(
                interface_result.get("message") or ""
            )
            interface_result["configuration_issues"] = interface_issues
            interface_result["state"] = "configuration_invalid"
            interface_result["ok"] = False
            issue_text = "；".join(
                str(issue.get("message") or "采集配置无效")
                for issue in interface_issues
            )
            probe_text = str(interface_result.get("probe_message") or "")
            interface_result["message"] = (
                f"配置问题：{issue_text}"
                + (f"；探测结果：{probe_text}" if probe_text else "")
            )

        sensors = []
        for name in config.schema_sensors:
            is_selected = name in selected
            if not is_selected:
                state = "not_selected"
            elif received[name] > 0:
                state = "ready"
            elif invalid_received[name] > 0:
                state = "invalid_data"
            else:
                state = "no_data"
            sensors.append({
                "name": name, "selected": is_selected,
                "received_samples": int(received[name]),
                "invalid_samples": int(invalid_received[name]), "state": state,
                "message": {"not_selected": "未选择采集", "ready": "收到有效数据",
                             "invalid_data": "收到非数值数据", "no_data": "未检测到数据"}[state],
                "blocking": config.acquisition_mode == "simulation" and is_selected,
                "ok": not is_selected or state == "ready",
            })
        checked_at = time.time()
        config_fingerprint = readiness_config_fingerprint(config)
        readiness_reports = []
        for interface_result in interface_results:
            report = _readiness_report_for_result(
                interface_result, config_fingerprint, checked_at
            ).to_dict()
            interface_result["readiness"] = report
            interface_result["stages"] = report["stages"]
            readiness_reports.append(report)
        interface_ok = config.acquisition_mode == "simulation" or all(
            item["readiness"]["ready"]
            for item in interface_results
            if item.get("enabled", True) and not item.get("auxiliary_only")
        )
        ok = bool(selected) and not errors and interface_ok
        if config.acquisition_mode == "simulation":
            ok = ok and all(item["ok"] for item in sensors)
        result = {
            "ok": ok, "acquisition_mode": "real",
            "readiness_namespace": "physical_hardware",
            "driver": config.driver, "endpoint": config.endpoint,
            "elapsed_seconds": time.time() - check_started, "errors": errors,
            "sensors": sensors, "interfaces": interface_results,
            "configuration_issues": configuration_issues,
            "readiness_reports": readiness_reports,
            "config_fingerprint": config_fingerprint,
            "cache_identity": readiness_cache_identity(config, config_fingerprint),
        }
        result["checked_at"] = checked_at
        with self.lock:
            self._latest_check_result = deepcopy(result)
        return result

    def latest_check_result(self, acquisition_mode: str = "real") -> dict[str, Any]:
        """Return one mode's cached check without allowing cross-mode overwrite."""

        with self.lock:
            if str(acquisition_mode or "real").lower() == "simulation":
                return deepcopy(self._latest_simulation_check_result)
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
        summary = {
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
        if self.quality_metrics is not None:
            unified = self.quality_metrics.summary()
            summary["unified_frame_metrics"] = unified
            summary["target_frame_rate_hz"] = unified["target_frame_rate_hz"]
            summary["actual_frame_rate_hz"] = unified["actual_frame_rate_hz"]
            summary["deadline_misses"] = unified["deadline_misses"]
            summary["channel_metrics"] = unified["channels"]
        return summary

    def start(self, config: AcquisitionConfig) -> dict:
        """Start one capture while safely replacing an active simulation.

        A simulation is disposable and may be left running when a browser is
        refreshed or switches modes.  Finalize it before the next start so it
        cannot permanently block local, LAN, or public operation.  A real
        hardware capture is never taken over implicitly.
        """
        if config.acquisition_mode == "real":
            latest = self.latest_check_result("real")
            expected_fingerprint = readiness_config_fingerprint(config)
            if not latest or latest.get("config_fingerprint") != expected_fingerprint:
                raise RuntimeError("真实采集配置尚未用当前参数完成分层接口检查，请重新执行立即检查")
            expected_identity = readiness_cache_identity(config, expected_fingerprint)
            if latest.get("cache_identity") != expected_identity:
                raise RuntimeError(
                    "真实采集检查缓存因采集模式、执行端、运行修订或配置变化而过期，"
                    "请在当前执行端重新执行立即检查"
                )
            enabled_ids = {
                str(item.get("id") or "")
                for item in config.interfaces or []
                if item.get("enabled", True)
            }
            reports = {
                str(report.get("interface_id") or ""): report
                for report in latest.get("readiness_reports", [])
            }
            failed = [
                reports.get(interface_id, {
                    "interface_id": interface_id,
                    "state": "no_readiness_report",
                    "message": "没有就绪报告",
                    "ready": False,
                })
                for interface_id in enabled_ids
                if not reports.get(interface_id, {}).get("ready")
            ]
            if config.capture_policy == "formal" and failed:
                details = "；".join(
                    f"{item['interface_id']}={item.get('state')}（{item.get('message') or '检查未通过'}）"
                    for item in failed
                )
                raise RuntimeError(f"正式真实采集失败关闭：{details}")
            if config.capture_policy == "degraded_engineering":
                disabled = [
                    str(item.get("id") or "")
                    for item in config.interfaces or []
                    if not item.get("enabled", True)
                ]
                if not config.degraded_confirmed:
                    raise RuntimeError("降级工程采集必须显式确认影响")
                if not disabled:
                    raise RuntimeError("降级工程采集必须显式停用具体未就绪接口")
                if failed:
                    details = "、".join(item["interface_id"] for item in failed)
                    raise RuntimeError(f"降级工程采集的其余启用接口仍未就绪：{details}")
            for item in config.interfaces or []:
                if not item.get("enabled", True):
                    continue
                if item.get("physical_port_id") and not item.get("physical_interface_id"):
                    raise RuntimeError(
                        f"接口“{item.get('id', item.get('role', 'unknown'))}”："
                        "端口存在，未检测到兼容设备；连接设备后重新识别接口"
                    )
                if not item.get("physical_verified", False):
                    raise RuntimeError(
                        f"接口“{item.get('id', item.get('role', 'unknown'))}”尚未通过物理设备验证，"
                        "请先执行接口检查"
                    )
        with self.lifecycle_lock:
            with self.lock:
                active = self.thread is not None and self.thread.is_alive()
                active_mode = (
                    self.config.acquisition_mode if self.config is not None else ""
                )
            replaced_simulation = bool(active and active_mode == "simulation")
            if active and not replaced_simulation:
                raise RuntimeError("真实采集已经在运行，请先停止当前采集")
            if replaced_simulation:
                stopped = self.stop()
                if stopped.get("running"):
                    raise RuntimeError("上一模拟采集未能停止，请稍后重试")
            result = self._start_new(config)
            if replaced_simulation:
                result["replaced_active_simulation"] = True
            return result

    def _start_new(self, config: AcquisitionConfig) -> dict:
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
            self.frame_quality.clear()
            self.quality_metrics = AcquisitionQualityMetrics(
                config.selected_sensors or [], config.sample_rate_hz
            )
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
                        "frame_sequence",
                        "timestamp_iso",
                        "timestamp_unix",
                        "target_monotonic",
                        "deadline_missed",
                        "channel_quality_json",
                        "channel_source_time_json",
                        "channel_age_seconds_json",
                    ],
                )
                writer.writeheader()
                timestamp_writer.writeheader()
                replay_scheduler = (
                    MonotonicReplayScheduler(
                        config.sample_rate_hz,
                        clock=time.perf_counter,
                        wait=self.stop_event.wait,
                    )
                    if config.driver == "simulator" or config.acquisition_mode == "simulation"
                    else None
                )
                frame_assembler = (
                    FrameAssembler(
                        config.selected_sensors or [],
                        config.sample_rate_hz,
                        _freshness_by_channel(config),
                        clock=time.monotonic,
                        wall_clock=time.time,
                        wait=self.stop_event.wait,
                    )
                    if config.acquisition_mode == "real"
                    else None
                )
                while not self.stop_event.is_set():
                    frame: UnifiedFrame | None = None
                    quality: dict[str, str] = {}
                    source_times: dict[str, float | None] = {}
                    ages: dict[str, float | None] = {}
                    if frame_assembler is not None:
                        deadline = frame_assembler.wait_next_deadline(self.stop_event)
                        if self.stop_event.is_set():
                            break
                        if not isinstance(self.driver, MultiInterfaceDriver):
                            raise RuntimeError("真实采集必须使用独立DriverWorker内核")
                        frame = frame_assembler.assemble(self.driver.cache, deadline)
                        sample = {
                            name: channel.value
                            for name, channel in frame.channels.items()
                            if channel.value is not None
                            and channel.quality
                            not in {ChannelQuality.MISSING, ChannelQuality.INVALID}
                        }
                        quality = frame.quality()
                        source_times = {
                            name: channel.received_wall_time
                            if channel.interface_id
                            else None
                            for name, channel in frame.channels.items()
                        }
                        ages = {
                            name: _finite(channel.metadata.get("age_seconds"))
                            for name, channel in frame.channels.items()
                        }
                        now = frame.target_wall_time
                        if self.quality_metrics is not None:
                            self.quality_metrics.observe(frame)
                        with self.lock:
                            for name, channel in frame.channels.items():
                                if channel.quality == ChannelQuality.MEASURED_NEW:
                                    self.channel_observed[name] += 1
                                    self.channel_last_observed[name] = channel.received_wall_time
                    else:
                        try:
                            sample = self.driver.read_sample()
                        except Exception as exc:
                            self.last_error = str(exc)
                            break
                        now = time.time()
                        if sample:
                            with self.lock:
                                for name, value in sample.items():
                                    if name in self.channel_observed and _finite(value) is not None:
                                        self.channel_observed[name] += 1
                                        self.channel_last_observed[name] = now
                        quality = {
                            name: (
                                ChannelQuality.MEASURED_NEW.value
                                if _finite(sample.get(name)) is not None
                                else ChannelQuality.MISSING.value
                            )
                            for name in selected
                        }
                    valid_sample = bool(sample) and any(
                        _finite(sample.get(name)) is not None for name in selected
                    )
                    # A real unified frame is persisted even when every channel
                    # is missing; dropping it would hide timing and continuity
                    # failures.  Simulation retains the legacy end-of-source
                    # behavior.
                    if valid_sample or frame is not None:
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
                                "frame_sequence": (
                                    frame.capture_sequence if frame is not None else row_index
                                ),
                                "timestamp_iso": datetime.fromtimestamp(now).isoformat(
                                    timespec="milliseconds"
                                ),
                                "timestamp_unix": f"{now:.6f}",
                                "target_monotonic": (
                                    f"{frame.target_monotonic:.9f}" if frame is not None else ""
                                ),
                                "deadline_missed": bool(
                                    frame.deadline_missed if frame is not None else False
                                ),
                                "channel_quality_json": json.dumps(
                                    quality, ensure_ascii=False, separators=(",", ":")
                                ),
                                "channel_source_time_json": json.dumps(
                                    source_times, ensure_ascii=False, separators=(",", ":")
                                ),
                                "channel_age_seconds_json": json.dumps(
                                    ages, ensure_ascii=False, separators=(",", ":")
                                ),
                            }
                        )
                        raw_file.flush()
                        time_file.flush()
                        with self.lock:
                            self.total_sample_count += 1
                            self.rows.append(row)
                            self.timestamps.append(now)
                            self.frame_quality.append(quality)
                            for name in selected:
                                if _finite(row.get(name)) is not None:
                                    self.sensor_received[name] += 1
                                    self.sensor_last_time[name] = now
                            self._stream_activity.notify_all()
                    if replay_scheduler is not None:
                        replay_scheduler.wait_next()
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
        with self.lifecycle_lock:
            return self._stop_active()

    def _stop_active(self) -> dict:
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
                        if healthy and not missing and not stale:
                            state = "ready"
                            message = f"接口正在采集：{'、'.join(healthy)}"
                        elif healthy:
                            state = "partial"
                            unavailable = [*missing, *stale]
                            message = (
                                f"接口仅部分通道可用：{'、'.join(healthy)}；"
                                f"缺失或陈旧：{'、'.join(dict.fromkeys(unavailable))}"
                            )
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
                        "ok": state in {"disabled", "video_only", "no_channels", "ready"},
                    })
            model_inputs = (
                self.config.model_input_sensors
                if self.config is not None
                else []
            )
            model_window = recent_complete_window(
                list(self.rows),
                model_inputs,
                24,
                quality_rows=list(self.frame_quality),
            )
            degraded_engineering = bool(
                self.config is not None
                and self.config.capture_policy == "degraded_engineering"
            )
            model_ready = bool(model_inputs) and model_window["ready"] and not degraded_engineering
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
                "data_quality": (
                    self.last_data_quality
                    or (
                        self.quality_metrics.summary()
                        if self.quality_metrics is not None
                        else None
                    )
                ),
                "file_integrity": self.last_file_integrity,
                "timestamp_file": (
                    str(self.timestamp_path) if self.timestamp_path else None
                ),
                "model_ready": model_ready,
                "capture_policy": self.config.capture_policy if self.config else None,
                "release_eligible": bool(
                    self.config is not None
                    and self.config.capture_policy == "formal"
                    and not degraded_engineering
                ),
                "production_conclusion_enabled": not degraded_engineering,
                "model_window": model_window,
                "recent_frame_quality": list(self.frame_quality)[-48:],
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
            self._latest_check_result = {
                "acquisition_mode": "real",
                "checked_at": None,
                "interfaces": [],
                "sensors": [],
            }
            return self.status()

    def numeric_matrix(self) -> tuple[list[dict[str, Any]], list[float]]:
        with self.lock:
            return list(self.rows), list(self.timestamps)

    def stream_counts(self) -> dict[str, int]:
        """Return O(1) counters for helper transport without copying samples."""

        with self.lock:
            buffered = len(self.rows)
            total = int(self.total_sample_count)
            return {
                "total_count": total,
                "buffered_count": buffered,
                "buffer_base": max(0, total - buffered),
            }

    def wait_for_stream_activity(self, timeout: float = 0.2) -> bool:
        """Wait until samples or capture state may have changed."""

        with self._stream_activity:
            return bool(self._stream_activity.wait(timeout=max(0.0, float(timeout))))

    def stream_rows_since(self, cursor: int, limit: int) -> dict[str, Any]:
        """Copy only the bounded absolute row range requested by the helper."""

        with self.lock:
            rows = self.rows
            timestamps = self.timestamps
            total = int(self.total_sample_count)
            buffered = len(rows)
            base = max(0, total - buffered)
            requested = max(0, int(cursor))
            truncated = requested < base
            start = max(requested, base)
            end = min(total, start + max(1, min(int(limit), 200)))
            relative_start = start - base
            relative_end = end - base
            row_values = list(islice(rows, relative_start, relative_end))
            timestamp_values = list(islice(timestamps, relative_start, relative_end))
            return {
                "rows": row_values,
                "timestamps": timestamp_values,
                "next_cursor": end,
                "total_count": total,
                "buffer_base": base,
                "truncated": truncated,
            }
