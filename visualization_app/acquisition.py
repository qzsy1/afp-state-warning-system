from __future__ import annotations

import csv
import ctypes
import hashlib
import io
import ipaddress
import json
import math
import os
import queue
import re
import socket
import struct
import subprocess
import threading
import time
import tempfile
import sys
import urllib.request
import urllib.parse
import urllib.error
import requests
import urllib3.connectionpool
from requests.adapters import HTTPAdapter
from requests.auth import HTTPDigestAuth
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
from hp_thermocouple64 import HpThermocouple64Driver, normalize_read_profile
from esr1001_usb import (
    USB_DRIVER, RAW_CHANNELS, Esr1001UsbDriver, RawCaptureWriter,
    load_calibration, UsbProtocolError, probe_usb, read_vendor_candidate, confirm_vendor_candidate, replay_source_capture,
)
from m3232_serial import (M3232PressureDriver, MatrixRecorder, normalize_config as normalize_m3232,
    matrix_csv_path,
    BAUDRATE as M3232_BAUDRATE, RAW_CHANNEL as M3232_RAW_CHANNEL)
from simulation_replay import MonotonicReplayScheduler
from real_acquisition import (
    AcquisitionQualityMetrics,
    ChannelQuality,
    ChannelSample,
    ChannelSampleCache,
    DriverWorker,
    FrameAssembler,
    ReadinessSnapshot,
    UnifiedFrame,
    readiness_config_fingerprint,
)
from local_direct_acquisition import LocalDirectAdapter, minimum_poll_interval
from release_contract import assert_config_contract, load_release_contract
from thermocouple64 import (
    THERMOCOUPLE64_CHANNELS,
    THERMOCOUPLE64_DRIVER,
    THERMOCOUPLE64_INTERFACE_ID,
    THERMOCOUPLE64_MODE_ID,
    THERMOCOUPLE64_ROLE,
    THERMOCOUPLE64_DERIVED_CHANNELS,
    Thermocouple64Snapshot,
    project_specimen_layers,
    raw_catalog,
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
RELEASE_CONTRACT = load_release_contract(start=__file__)
WORKSPACE_DIR = APP_DIR.parents[1]
DEFAULT_CAPTURE_ROOT = Path("D:/AFP_Capture") if os.name == "nt" else Path(WORKSPACE_DIR.anchor) / "AFP_Capture"
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
STRAIN_SENSOR_COLUMNS = [f"应变{index}" for index in range(1, 5)]
# These legacy channels are intentionally excluded from the new collection
# plan.  New-schema checkpoints must not request them: missing physical sensor
# inputs are never replaced with a synthetic baseline during live inference.
NEW_EXCLUDED_SENSOR_COLUMNS = {"转速", "位移", "振动"}
NEW_COLLECTION_SENSOR_COLUMNS = list(
    dict.fromkeys([*NEW_CORE_SENSOR_COLUMNS, *NEW_OPTIONAL_SENSOR_COLUMNS])
)
LATEST_COLLECTION_SENSOR_COLUMNS = [
    *NEW_COLLECTION_SENSOR_COLUMNS,
    *STRAIN_SENSOR_COLUMNS,
]
CURRENT_COLLECTION_SENSOR_COLUMNS = [
    *NEW_CORE_SENSOR_COLUMNS,
    *THERMOCOUPLE64_CHANNELS,
    *STRAIN_SENSOR_COLUMNS,
]
ALL_SENSOR_COLUMNS = list(
    dict.fromkeys(
        [*LEGACY_SENSOR_COLUMNS, *LATEST_COLLECTION_SENSOR_COLUMNS, *CURRENT_COLLECTION_SENSOR_COLUMNS, *RAW_CHANNELS, M3232_RAW_CHANNEL]
    )
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
LATEST_COLLECTION_COLUMNS = [
    "时间",
    *LATEST_COLLECTION_SENSOR_COLUMNS,
    *PROCESS_PARAMETER_COLUMNS,
    "run_id",
    "specimen_id",
    "condition_id",
    "replicate",
    "layer_id",
]
CURRENT_COLLECTION_COLUMNS = [
    "时间",
    *CURRENT_COLLECTION_SENSOR_COLUMNS,
    *PROCESS_PARAMETER_COLUMNS,
    "run_id",
    "specimen_id",
    "condition_id",
    "replicate",
    "layer_id",
]
ACQUISITION_SCHEMAS = {
    "esr1001_raw_adc": {
        "label": "ESR1001-A 原始 ADC 诊断（adc_counts，标定未确认）",
        "sensors": list(RAW_CHANNELS),
        "raw_columns": [*RAW_CHANNELS, "cycle", "file", "root", "run_id", "specimen_id", "condition_id", "replicate", "layer_id"],
    },
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
    "new_collection_v12_0": {
        "label": "完整采集方案（21采集通道＋4工艺参数；含四通道微应变）",
        "sensors": LATEST_COLLECTION_SENSOR_COLUMNS,
        "raw_columns": LATEST_COLLECTION_COLUMNS,
    },
    THERMOCOUPLE64_MODE_ID: {
        "label": "64路热电偶完整采集方案（77采集通道＋4工艺参数）",
        "sensors": CURRENT_COLLECTION_SENSOR_COLUMNS,
        "raw_columns": CURRENT_COLLECTION_COLUMNS,
    },
}
MODERN_COLLECTION_SCHEMAS = frozenset(
    {"new_collection_v11_3", "new_collection_v12_0", THERMOCOUPLE64_MODE_ID}
)

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
    **{f"strain_{index}": f"应变{index}" for index in range(1, 5)},
    **{f"strain{index}": f"应变{index}" for index in range(1, 5)},
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


def _network_interface_type(name: str, description: str = "") -> str:
    text = f"{name} {description}".casefold()
    if any(token in text for token in ("wlan", "wi-fi", "wifi", "wireless", "无线")):
        return "wireless"
    if any(
        token in text
        for token in (
            "flclash", "tunnel", "tun", " tap", "vpn", "loopback",
            "hyper-v", "vethernet", "wsl", "virtual", "虚拟",
        )
    ):
        return "virtual"
    return "physical"


def network_interface_route_facts(
    name: str,
    description: str,
    ipv4_bindings: list[dict[str, str]],
    targets: dict[str, str],
    route_sources: dict[str, str],
) -> dict[str, Any]:
    bindings = [
        {
            "address": str(item.get("address") or ""),
            "netmask": str(item.get("netmask") or ""),
        }
        for item in ipv4_bindings
        if str(item.get("address") or "")
    ]
    addresses = [item["address"] for item in bindings]
    parsed_addresses = []
    for item in bindings:
        try:
            parsed_addresses.append(
                ipaddress.IPv4Interface(
                    f"{item['address']}/{item['netmask'] or '255.255.255.255'}"
                )
            )
        except ValueError:
            continue
    interface_type = _network_interface_type(name, description)
    apipa_only = bool(parsed_addresses) and all(
        address.ip.is_link_local for address in parsed_addresses
    )
    target_routes: dict[str, dict[str, Any]] = {}
    for target_id, target_value in targets.items():
        target = ipaddress.IPv4Address(str(target_value))
        source_address = str(route_sources.get(target_id) or "")
        target_subnet_match = any(
            target in address.network for address in parsed_addresses
        )
        best_route = bool(source_address and source_address in addresses)
        target_routes[str(target_id)] = {
            "target_address": str(target),
            "source_address": source_address,
            "target_subnet_match": target_subnet_match,
            "best_route": best_route,
            "auto_eligible": bool(
                interface_type == "physical"
                and not apipa_only
                and target_subnet_match
                and best_route
            ),
        }
    return {
        "interface_type": interface_type,
        "ipv4_bindings": bindings,
        "apipa_only": apipa_only,
        "target_routes": target_routes,
    }


def _route_source_address(host: str, port: int) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((str(host), int(port)))
        return str(sock.getsockname()[0])
    except OSError:
        return ""
    finally:
        sock.close()


def _network_adapter_snapshot() -> list[dict[str, Any]]:
    try:
        import psutil
    except ImportError:
        return _windows_network_adapter_snapshot()
    if psutil is None:
        return _windows_network_adapter_snapshot()
    result = []
    for name, addresses in psutil.net_if_addrs().items():
        bindings = [
            {
                "address": str(address.address),
                "netmask": str(address.netmask or ""),
            }
            for address in addresses
            if getattr(address, "family", None) == socket.AF_INET
            and not str(address.address).startswith("127.")
        ]
        result.append({
            "id": f"ethernet:{name}",
            "name": str(name),
            "description": "",
            "ipv4_bindings": bindings,
        })
    return result


_WINDOWS_ADAPTER_LOCK = threading.RLock()
_WINDOWS_ADAPTER_CACHE = None


def _windows_network_signature():
    """Read the current IPv4/index bindings without starting PowerShell."""
    if not sys.platform.startswith("win"):
        return None
    try:
        size = ctypes.c_ulong(0)
        query = ctypes.windll.iphlpapi.GetIpAddrTable
        if query(None, ctypes.byref(size), False) != 122:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if query(buffer, ctypes.byref(size), False):
            return None
        count = struct.unpack_from("<I", buffer.raw)[0]
        rows = tuple(sorted(struct.unpack_from("<5I2H", buffer.raw, 4 + 24 * index) for index in range(count)))
        return rows, tuple(socket.if_nameindex())
    except (AttributeError, OSError, ValueError, struct.error):
        return None


def _windows_network_adapter_snapshot() -> list[dict[str, Any]]:
    global _WINDOWS_ADAPTER_CACHE
    signature = _windows_network_signature()
    with _WINDOWS_ADAPTER_LOCK:
        if signature is not None and _WINDOWS_ADAPTER_CACHE is not None and _WINDOWS_ADAPTER_CACHE[0] == signature:
            return deepcopy(_WINDOWS_ADAPTER_CACHE[1])
        rows = _query_windows_network_adapters()
        _WINDOWS_ADAPTER_CACHE = (signature, deepcopy(rows)) if signature is not None and rows else None
        return rows


def _query_windows_network_adapters() -> list[dict[str, Any]]:
    """Read Windows IPv4 adapter identities without requiring psutil."""
    if not sys.platform.startswith("win"):
        return []
    script = (
        "$utf8=[System.Text.UTF8Encoding]::new($false);"
        "[Console]::OutputEncoding=$utf8;$OutputEncoding=$utf8;"
        "$items=@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | "
        "Where-Object {$_.IPAddress -notlike '127.*'} | ForEach-Object {"
        "$adapter=Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue;"
        "[pscustomobject]@{Name=$_.InterfaceAlias;Description=$adapter.InterfaceDescription;"
        "Address=$_.IPAddress;PrefixLength=$_.PrefixLength}});"
        "$items | ConvertTo-Json -Compress"
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=8.0,
        creationflags=creation_flags,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    decoded = json.loads(completed.stdout)
    rows = decoded if isinstance(decoded, list) else [decoded]
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "").strip()
        address = str(row.get("Address") or "").strip()
        if not name or not address:
            continue
        try:
            prefix_length = int(row.get("PrefixLength"))
            netmask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix_length}").netmask)
        except (TypeError, ValueError):
            netmask = ""
        item = grouped.setdefault(
            name,
            {
                "id": f"ethernet:{name}",
                "name": name,
                "description": str(row.get("Description") or ""),
                "ipv4_bindings": [],
            },
        )
        item["ipv4_bindings"].append({"address": address, "netmask": netmask})
    return list(grouped.values())


def _usb_vid_pid(instance_id: str) -> tuple[int | None, int | None]:
    match = re.search(
        r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})",
        str(instance_id or ""),
        re.IGNORECASE,
    )
    if not match:
        return None, None
    return int(match.group(1), 16), int(match.group(2), 16)


def _windows_camera_pnp_facts() -> list[dict[str, Any]]:
    """Return read-only Windows camera identity and driver facts."""
    if not sys.platform.startswith("win"):
        return []
    script = (
        "$utf8=[System.Text.UTF8Encoding]::new($false);"
        "[Console]::OutputEncoding=$utf8;$OutputEncoding=$utf8;"
        "$items=@(Get-PnpDevice -PresentOnly -Class Camera -ErrorAction Stop | "
        "ForEach-Object {"
        "$id=$_.InstanceId;"
        "$props=@(Get-PnpDeviceProperty -InstanceId $id -ErrorAction SilentlyContinue);"
        "$service=($props|Where-Object KeyName -eq 'DEVPKEY_Device_Service').Data;"
        "$provider=($props|Where-Object KeyName -eq 'DEVPKEY_Device_DriverProvider').Data;"
        "$version=($props|Where-Object KeyName -eq 'DEVPKEY_Device_DriverVersion').Data;"
        "$locations=($props|Where-Object KeyName -eq 'DEVPKEY_Device_LocationPaths').Data;"
        "$container=($props|Where-Object KeyName -eq 'DEVPKEY_Device_ContainerId').Data;"
        "[pscustomobject]@{InstanceId=$id;FriendlyName=$_.FriendlyName;"
        "Status=$_.Status;Service=$service;DriverProvider=$provider;"
        "DriverVersion=$version;LocationPaths=@($locations);ContainerId=\"$container\"}"
        "});$items | ConvertTo-Json -Depth 4 -Compress"
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            creationflags=creation_flags,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        decoded = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return []
    rows = decoded if isinstance(decoded, list) else [decoded]
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        instance_id = str(row.get("InstanceId") or "")
        vid, pid = _usb_vid_pid(instance_id)
        locations = row.get("LocationPaths") or []
        if not isinstance(locations, list):
            locations = [locations]
        result.append({
            "instance_id": instance_id,
            "friendly_name": str(row.get("FriendlyName") or ""),
            "pnp_status": str(row.get("Status") or ""),
            "service": str(row.get("Service") or ""),
            "driver_provider": str(row.get("DriverProvider") or ""),
            "driver_version": str(row.get("DriverVersion") or ""),
            "location_paths": [str(item) for item in locations if str(item)],
            "container_id": str(row.get("ContainerId") or ""),
            "vid": vid,
            "pid": pid,
        })
    return result


def _identity_key(prefix: str, value: str) -> str:
    digest = hashlib.sha256(
        str(value or "").strip().lower().encode("utf-8", errors="replace")
    ).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _merge_camera_pnp_with_usb_topology(
    topology: dict[str, Any], camera_devices: list[dict[str, Any]]
) -> None:
    """Attach PnP instance/container/location facts to unique USB devices."""
    topology_devices = [
        item for item in topology.get("devices", []) if isinstance(item, dict)
    ]
    for camera in camera_devices:
        matches = [
            item for item in topology_devices
            if item.get("vid") == camera.get("vid")
            and item.get("pid") == camera.get("pid")
        ]
        if len(matches) != 1:
            continue
        device = matches[0]
        instance_id = str(camera.get("instance_id") or "")
        container_id = str(camera.get("container_id") or "")
        location_paths = list(camera.get("location_paths") or [])
        device.update({
            "instance_id": instance_id,
            "instance_key": _identity_key("pnp", instance_id) if instance_id else "",
            "container_id": container_id,
            "container_key": _identity_key("container", container_id) if container_id else "",
            "location_paths": location_paths,
            "location_keys": [
                _identity_key("location", value) for value in location_paths
            ],
            "friendly_name": str(camera.get("friendly_name") or device.get("friendly_name") or ""),
            "service": str(camera.get("service") or ""),
            "driver_provider": str(camera.get("driver_provider") or ""),
            "driver_version": str(camera.get("driver_version") or ""),
            "pnp_status": str(camera.get("pnp_status") or ""),
        })
        camera["parent_port_id"] = str(device.get("parent_port_id") or "")
        camera["usb_device_id"] = str(device.get("id") or "")


def _network_endpoint(endpoint: str, role: str) -> tuple[str, int]:
    value = str(endpoint or "").strip()
    if "://" in value:
        parsed = urllib.parse.urlparse(value)
        return str(parsed.hostname or ""), int(parsed.port or 80)
    if ":" in value:
        host, port_text = value.rsplit(":", 1)
        if port_text.isdigit():
            return host, int(port_text)
    return value, DEFAULT_PLC_PORT if role == "plc" else 80


def validate_network_path(
    interface: dict[str, Any],
    *,
    adapter_snapshot: list[dict[str, Any]] | None = None,
    route_source: str | None = None,
) -> dict[str, Any]:
    role = str(interface.get("role") or "")
    host, port = _network_endpoint(str(interface.get("endpoint") or ""), role)
    selected_id = str(interface.get("physical_interface_id") or "")
    adapters = list(adapter_snapshot) if adapter_snapshot is not None else _network_adapter_snapshot()
    tcp_probe_error = ""
    if route_source is not None:
        actual_source = str(route_source)
    else:
        try:
            with socket.create_connection((host, port), timeout=2.0) as connection:
                actual_source = str(connection.getsockname()[0])
        except OSError as exc:
            actual_source = ""
            tcp_probe_error = str(exc)
    selected = next(
        (
            item for item in adapters
            if str(item.get("id") or f"ethernet:{item.get('name', '')}") == selected_id
        ),
        None,
    )
    actual = next(
        (
            item for item in adapters
            if actual_source in {
                str(binding.get("address") or "")
                for binding in item.get("ipv4_bindings", [])
                if isinstance(binding, dict)
            }
        ),
        None,
    )
    facts = network_interface_route_facts(
        str((selected or {}).get("name") or selected_id.removeprefix("ethernet:")),
        str((selected or {}).get("description") or ""),
        list((selected or {}).get("ipv4_bindings") or []),
        {role or "target": host},
        {role or "target": actual_source},
    )
    target_facts = facts["target_routes"][role or "target"]
    actual_id = str(
        (actual or {}).get("id")
        or (f"ethernet:{(actual or {}).get('name')}" if actual else "")
    )
    ok = bool(
        selected
        and selected_id
        and actual_id == selected_id
        and target_facts["auto_eligible"]
    )
    return {
        "ok": ok,
        "status": "network_path_ready" if ok else "network_path_invalid",
        "target_address": host,
        "target_port": port,
        "selected_interface_id": selected_id,
        "actual_interface_id": actual_id,
        "actual_source_address": actual_source,
        "source_probe": "injected" if route_source is not None else "tcp_connect",
        "tcp_probe_error": tcp_probe_error,
        "interface_type": facts["interface_type"],
        "apipa_only": facts["apipa_only"],
        "target_subnet_match": target_facts["target_subnet_match"],
        "best_route": target_facts["best_route"],
    }


def require_network_path(interface: dict[str, Any]) -> dict[str, Any]:
    global _WINDOWS_ADAPTER_CACHE
    result = validate_network_path(interface)
    if not result["ok"] and _WINDOWS_ADAPTER_CACHE is not None:
        with _WINDOWS_ADAPTER_LOCK:
            _WINDOWS_ADAPTER_CACHE = None
        result = validate_network_path(interface)
    if not result["ok"]:
        raise RuntimeError(
            "network_path_invalid: "
            f"target={result['target_address']} "
            f"selected={result['selected_interface_id']} "
            f"actual={result['actual_interface_id']} "
            f"source={result['actual_source_address']}"
        )
    return result


def reusable_network_path(
    interface: dict[str, Any], readiness_interfaces: list[dict[str, Any]] | tuple
) -> dict[str, Any] | None:
    interface_id = str(interface.get("id") or "")
    physical_id = str(interface.get("physical_interface_id") or "")
    role = str(interface.get("role") or "")
    driver = str(interface.get("driver") or "")
    target_host, target_port = _network_endpoint(
        str(interface.get("endpoint") or ""), role
    )
    evidence = next(
        (
            item for item in readiness_interfaces
            if isinstance(item, dict)
            and str(item.get("id") or "") == interface_id
            and str(item.get("physical_interface_id") or "") == physical_id
            and str(item.get("role") or "") == role
            and str(item.get("driver") or "") == driver
            and item.get("ok") is True
            and str(item.get("state") or "").lower()
            in {"ok", "ready", "passed", "data_ready"}
        ),
        None,
    )
    path = (evidence or {}).get("network_path") or {}
    if not isinstance(path, dict):
        return None
    configured_path = interface.get("network_path") or {}
    if not isinstance(configured_path, dict):
        configured_path = {}
    try:
        observed_target_port = int(path.get("target_port") or 0)
    except (TypeError, ValueError):
        return None
    expected_source = str(
        interface.get("source_address")
        or configured_path.get("actual_source_address")
        or ""
    )
    actual_source = str(path.get("actual_source_address") or "")
    if not (
        path.get("ok") is True
        and path.get("status") == "network_path_ready"
        and str(path.get("target_address") or "") == target_host
        and observed_target_port == target_port
        and str(path.get("selected_interface_id") or "") == physical_id
        and str(path.get("actual_interface_id") or "") == physical_id
        and actual_source
        and (not expected_source or actual_source == expected_source)
    ):
        return None
    return deepcopy(path)

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


def _pending_capture_uuid(payload: dict[str, Any]) -> str:
    config = payload.get("config")
    summary = payload.get("summary")
    if isinstance(config, dict):
        capture_uuid = str(config.get("capture_uuid") or "").strip()
        if capture_uuid:
            return capture_uuid
    if isinstance(summary, dict):
        return str(summary.get("capture_uuid") or "").strip()
    return ""


def _path_has_reparse_point(path: Path, root: Path) -> bool:
    """Return whether a candidate crosses a link or Windows reparse point."""

    current = path
    root = root.resolve()
    while current != root:
        try:
            stat_result = current.lstat()
        except OSError:
            return True
        if current.is_symlink() or bool(
            int(getattr(stat_result, "st_file_attributes", 0)) & 0x400
        ):
            return True
        parent = current.parent
        if parent == current:
            return True
        current = parent
    return False


def _recovery_file_kind(path: Path) -> str:
    name = path.name
    if name.endswith(".partial"):
        return "partial"
    if name.endswith("_pending.json") and "_mysql" in name:
        return "mysql_pending"
    return ""


def _recovery_candidates(
    root: str | Path,
) -> tuple[Path, list[tuple[Path, dict[str, Any]]]]:
    capture_root = Path(root).expanduser().resolve()
    paths = {
        path
        for pattern in ("*_mysql*_pending.json", "*.partial")
        for path in capture_root.rglob(pattern)
    }
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(paths, key=lambda item: item.relative_to(capture_root).as_posix()):
        relative = path.relative_to(capture_root).as_posix()
        kind = _recovery_file_kind(path)
        eligible = bool(kind)
        reason = ""
        try:
            stat_result = path.lstat()
        except OSError:
            stat_result = None
            eligible = False
            reason = "unreadable"
        if eligible and _path_has_reparse_point(path, capture_root):
            eligible = False
            reason = "reparse_point"
        if eligible:
            try:
                path.resolve(strict=True).relative_to(capture_root)
            except (OSError, ValueError):
                eligible = False
                reason = "outside_capture_root"
        if eligible and (stat_result is None or not path.is_file()):
            eligible = False
            reason = "not_regular_file"
        size = int(stat_result.st_size) if stat_result is not None else -1
        modified_ns = int(stat_result.st_mtime_ns) if stat_result is not None else -1
        identity = hashlib.sha256(
            f"{kind}\0{relative}\0{size}\0{modified_ns}".encode("utf-8")
        ).hexdigest()
        candidates.append(
            (
                path,
                {
                    "id": identity,
                    "kind": kind or "unsupported",
                    "name": path.name,
                    "path_sha256": hashlib.sha256(relative.encode("utf-8")).hexdigest(),
                    "eligible": eligible,
                    "reason": reason,
                    "size_bytes": size,
                },
            )
        )
    return capture_root, candidates


def _resolve_pending_layer_file(
    pending_path: Path,
    payload: dict[str, Any],
    capture_root: str | Path,
) -> Path | None:
    """Resolve a retry source after a capture root was moved or archived.

    Pending records intentionally retain the original absolute path for audit
    evidence.  A later specimen can archive that CSV, or the entire capture
    root can be copied to another drive.  The immutable capture identity in
    the archive directory lets us recover the exact source without selecting
    a newer same-named layer.
    """

    saved = Path(str(payload.get("layer_file") or "")).expanduser()
    if saved.is_file():
        return saved
    if not saved.name:
        return None

    root = Path(capture_root).expanduser().resolve()
    capture_uuid = _pending_capture_uuid(payload)
    if capture_uuid:
        specimen_root = pending_path.parent.parent
        search_roots = [specimen_root]
        if specimen_root.resolve() != root:
            search_roots.append(root)
        archive_suffix = f"_{capture_uuid}"
        matches: list[Path] = []
        for search_root in search_roots:
            if not search_root.is_dir():
                continue
            for candidate in search_root.rglob(saved.name):
                if not candidate.is_file():
                    continue
                if any(parent.name.endswith(archive_suffix) for parent in candidate.parents):
                    matches.append(candidate)
            if matches:
                break
        unique = list(dict.fromkeys(path.resolve() for path in matches))
        if len(unique) == 1:
            return unique[0]

    # Relative paths from older records were relative to either the record
    # directory or the selected capture root.
    if not saved.is_absolute():
        for candidate in (pending_path.parent / saved, root / saved):
            if candidate.is_file():
                return candidate.resolve()
    return None


def scan_persistence_recovery(root: str | Path) -> dict[str, Any]:
    """Discover durable work that survived a prior process.

    Only file names, counts and capture identities are returned so readiness
    evidence does not disclose the machine's private directory layout.
    """

    capture_root, candidates = _recovery_candidates(root)
    pending_files = [path for path, item in candidates if item["kind"] == "mysql_pending"]
    incomplete_files = [path for path, item in candidates if item["kind"] == "partial"]
    capture_uuids: set[str] = set()
    unreadable_pending = 0
    missing_retry_sources = 0
    relocated_retry_sources = 0
    candidate_by_path = {path: item for path, item in candidates}
    for path in pending_files:
        if not candidate_by_path[path]["eligible"]:
            unreadable_pending += 1
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            unreadable_pending += 1
            continue
        if not isinstance(payload, dict):
            unreadable_pending += 1
            continue
        capture_uuid = _pending_capture_uuid(payload)
        if capture_uuid:
            capture_uuids.add(capture_uuid)
        resolved_layer = _resolve_pending_layer_file(path, payload, capture_root)
        if resolved_layer is None:
            missing_retry_sources += 1
        else:
            saved_layer = Path(str(payload.get("layer_file") or "")).expanduser()
            if not saved_layer.is_file():
                relocated_retry_sources += 1
    file_evidence = [dict(item) for _path, item in candidates]
    scan_fingerprint = hashlib.sha256(
        json.dumps(file_evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "blocked": bool(pending_files or incomplete_files),
        "database_pending_retry": len(pending_files),
        "incomplete_files": len(incomplete_files),
        "unreadable_pending": unreadable_pending,
        "missing_retry_sources": missing_retry_sources,
        "relocated_retry_sources": relocated_retry_sources,
        "unsafe_files": sum(not item["eligible"] for item in file_evidence),
        "capture_uuids": sorted(capture_uuids),
        "files": file_evidence,
        "scan_fingerprint": scan_fingerprint,
    }


class BoundedCsvWriter:
    """Single-owner CSV writer with bounded handoff from the sampling loop."""

    _STOP = object()

    def __init__(
        self,
        row_writer: Any,
        timestamp_writer: Any,
        *,
        max_queue_rows: int = 1024,
        submit_timeout_seconds: float = 0.05,
    ) -> None:
        self.row_writer = row_writer
        self.timestamp_writer = timestamp_writer
        self.submit_timeout_seconds = max(0.0, float(submit_timeout_seconds))
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, int(max_queue_rows)))
        self._error: BaseException | None = None
        self._closed = False
        self.high_water_rows = 0
        self._thread = threading.Thread(
            target=self._run,
            name="AFP-PersistenceWriter",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                if self._error is None:
                    row, timestamp = item
                    self.row_writer.writerow(row)
                    self.timestamp_writer.writerow(timestamp)
            except BaseException as exc:
                self._error = exc
            finally:
                self._queue.task_done()

    def _raise_error(self) -> None:
        if self._error is not None:
            raise RuntimeError(f"persistence_write_failed: {self._error}") from self._error

    def submit(self, row: dict[str, Any], timestamp: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("persistence_writer_closed")
        self._raise_error()
        try:
            self._queue.put(
                (dict(row), dict(timestamp)),
                timeout=self.submit_timeout_seconds,
            )
            self.high_water_rows = max(self.high_water_rows, self._queue.qsize())
        except queue.Full as exc:
            raise RuntimeError("persistence_queue_overflow") from exc
        self._raise_error()

    @property
    def pending_rows(self) -> int:
        return self._queue.qsize()

    def close(self, *, timeout_seconds: float = 5.0) -> None:
        if not self._closed:
            self._closed = True
            deadline = time.monotonic() + max(0.0, float(timeout_seconds))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("persistence_writer_stop_timeout")
                try:
                    self._queue.put(self._STOP, timeout=min(0.05, remaining))
                    break
                except queue.Full:
                    continue
            self._thread.join(max(0.0, deadline - time.monotonic()))
            if self._thread.is_alive():
                raise RuntimeError("persistence_writer_stop_timeout")
        self._raise_error()


class _DiscardTextIO(io.TextIOBase):
    """Writable zero-retention sink used by bounded engineering previews."""

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        return len(value)


BSV_UVC_WIDTH = 256
BSV_UVC_HEIGHT = 192
BSV_UVC_PIXELS = BSV_UVC_WIDTH * BSV_UVC_HEIGHT
BSV_UVC_YUV_BYTES = BSV_UVC_PIXELS * 2
BSV_UVC_TEMP_RANGE_CODE = 4
BSV_UVC_TEMP_FORMULA_SCALE = 64.0
BSV_UVC_TEMP_FORMULA_OFFSET = -50.0
BSV_UVC_DLL_NAME = "BsvUvcNative.dll"
BSV_UVC_VENDOR_ID = 0x2BDF
BSV_UVC_PRODUCT_ID = 0x0102
DEFAULT_RTSP_URL = "rtsp://192.168.125.2:554/"
ESR1001_BAUDRATE = 9600
ESR1001_BYTESIZE = 8
ESR1001_PARITY = "N"
ESR1001_STOPBITS = 1
ESR1001_DEFAULT_SLAVE_ID = 0x01
ESR1001_READ_FUNCTION = 0x04
ESR1001_FLOAT_REGISTER = 0x0030
ESR1001_FLOAT_REGISTER_COUNT = 8
ESR1001_CHANNEL_COUNT_REGISTER = 0x0015
ESR1001_ACQUISITION_COIL = 0x0009
ESR1001_CHANNELS = tuple(STRAIN_SENSOR_COLUMNS)
ESR1001_DEVICE_ORDER = ("应变4", "应变1", "应变2", "应变3")
ESR1001_RESPONSE_TIMEOUT_SECONDS = 0.25
ESR1001_FRESHNESS_SECONDS = 0.25

# Canonical acquisition metadata is shared by the API, storage and interfaces.
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
            "unit": "°C",
            "dtype": "float32",
            "source": f"慧谱TCP64 Modbus RTU CH{index}（摄氏度）",
            "alias": f"temperature_{index}",
            "display_group": f"TC-G{((index - 1) // 8) + 1:02d}",
        }
        for index in range(1, 65)
    },
    **{
        f"应变{index}": {
            "unit": "με",
            "dtype": "float32",
            "source": f"ESR1001-A CH{index}（所选USB/TTL或RS485接口）",
            "alias": f"strain_{index}",
        }
        for index in range(1, 5)
    },
}

SENSOR_CHANNEL_METADATA[M3232_RAW_CHANNEL] = {"unit": "raw", "dtype": "float64", "source": "M3232完整矩阵直接合计（未标定）"}

SENSOR_CHANNEL_METADATA.update({name: {"unit": "adc_counts", "dtype": "int24", "source": "ESR1001-A原生USB原始数据", "alias": name} for name in RAW_CHANNELS})

# 角色限定可用协议和通道；应变角色显式支持USB及RS485。
SENSOR_INTERFACE_PROFILES: dict[str, dict[str, Any]] = {
    "thermocouple": {
        "label": "历史八通道热电偶（只读兼容）",
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
        "historical_only": True,
    },
    THERMOCOUPLE64_ROLE: {
        "label": "慧谱TCP64热电偶（64路 Modbus RTU）",
        "driver": THERMOCOUPLE64_DRIVER,
        "protocol": "modbus_rtu",
        "physical_kind": "serial",
        "endpoint": "",
        "baudrate": 115200,
        "slave_id": 1,
        "channels": list(THERMOCOUPLE64_CHANNELS),
        "processing": "仪表选Modbus模式，115200-8-N-1；FC03读取64路20寄存器记录，温度低字在前；K/F转换为°C；现场验证独立记录",
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
        "protocol": "m3232_uart_text_v1",
        "physical_kind": "serial",
        "endpoint": "",
        "baudrate": M3232_BAUDRATE,
        "channels": [M3232_RAW_CHANNEL], "data_mode": "raw_matrix", "protocol_profile": "m3232_uart_text_v1",
        "processing": "230400/8N1；32×32明文矩阵及LF；原始合计raw（未标定），N需要匹配标定档",
    },
    "esr1001_strain": {
        "label": "ESR1001-A四通道应变",
        "driver": "modbus_rtu_esr1001",
        "allowed_drivers": ["modbus_rtu_esr1001", USB_DRIVER],
        "protocol": "modbus_rtu",
        "physical_kind": "serial",
        "endpoint": "",
        "baudrate": ESR1001_BAUDRATE,
        "slave_id": ESR1001_DEFAULT_SLAVE_ID,
        "channels": list(ESR1001_CHANNELS),
        "processing": "RS485：Modbus RTU FC04读取0x0030的8个寄存器；USB/TTL：115200-8-N-1连续上传24位ADC；微应变需要有效逐通道K/B",
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

SHARED_DEVICE_PROFILES: dict[str, dict[str, Any]] = {
    "plc": {
        "id": "panasonic-plc-process-v1",
        "version": 1,
        "approval_status": "approved",
        "unit_id": DEFAULT_PLC_SLAVE_ID,
        "register_map": dict(PLC_DEFAULT_REGISTER_MAP),
        "byte_order": "little_endian_bytes",
        "word_order": "low_word_first",
        "freshness_seconds": 0.5,
    },
    "robot": {
        "id": "abb-rws-rob1-base-v1",
        "version": 1,
        "approval_status": "approved",
        "mechanical_unit": "ROB_1",
        "coordinate_system": "Base",
        "resource": ABB_ROBTARGET_PATH,
        "authentication": "digest_challenge",
        "freshness_seconds": 0.5,
    },
    "thermal_uvc": {
        "id": "bsv-uvc-2bdf-0102-v1",
        "version": 1,
        "approval_status": "vendor_confirmation_required",
        "observed_identity": {"vid": 0x2BDF, "pid": 0x0102},
        "required_vendor_facts": {
            "windows_binding": None,
            "interface_number": None,
            "endpoints": None,
            "temperature_range": None,
            "frame_format": None,
            "conversion": None,
            "freshness_seconds": None,
        },
    },
}


def _credential_identifier(username: str, password: str) -> str:
    canonical = json.dumps(
        {"username": str(username), "password": str(password)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def device_profile_readiness(interface: dict[str, Any]) -> dict[str, Any]:
    profile = interface.get("device_profile")
    if not isinstance(profile, dict):
        return {
            "ready": False,
            "status": "device_profile_incomplete",
            "profile_id": "",
            "profile_version": 0,
            "missing_device_facts": ["device_profile"],
        }
    required_vendor_facts = profile.get("required_vendor_facts")
    missing = sorted(
        str(name)
        for name, value in (
            required_vendor_facts.items()
            if isinstance(required_vendor_facts, dict)
            else []
        )
        if value in (None, "", [], {})
    )
    ready = profile.get("approval_status") == "approved" and not missing
    return {
        "ready": ready,
        "status": "approved" if ready else "device_profile_incomplete",
        "profile_id": str(profile.get("id") or ""),
        "profile_version": int(profile.get("version") or 0),
        "missing_device_facts": missing,
    }


def default_capture_interfaces() -> list[dict[str, Any]]:
    """Return the six logical interfaces in the complete acquisition plan."""
    return [
        {
            "id": THERMOCOUPLE64_INTERFACE_ID,
            "enabled": True,
            "role": THERMOCOUPLE64_ROLE,
            "driver": THERMOCOUPLE64_DRIVER,
            "endpoint": "",
            "baudrate": 115200,
            "slave_id": 1,
            "timeout": 0.5,
            "read_profile": "channel_temperature",
            "channel_settings": {},
            "reconnect_window_seconds": 60.0,
            "freshness_seconds": 1.0,
            "channels": list(THERMOCOUPLE64_CHANNELS),
            "channel_map": {},
            "hardware_facts": {},
        },
        {
            "id": "plc_process", "enabled": True,
            "role": "plc", "driver": "modbus_tcp",
            "endpoint": f"{DEFAULT_PLC_IP}:{DEFAULT_PLC_PORT}",
            "slave_id": DEFAULT_PLC_SLAVE_ID,
            "register_map": dict(PLC_DEFAULT_REGISTER_MAP),
            "device_profile": deepcopy(SHARED_DEVICE_PROFILES["plc"]),
            "channel_map": {},
        },
        {
            "id": "uvc_temperature", "enabled": True,
            "role": "thermal_uvc", "driver": "uvc_thermal",
            "endpoint": "BSV UVC (WinUSB)", "roi": "",
            "device_profile": deepcopy(SHARED_DEVICE_PROFILES["thermal_uvc"]),
            "channel_map": {},
        },
        {
            "id": "abb_motion", "enabled": True,
            "role": "robot", "driver": "abb_robot",
            "endpoint": DEFAULT_ABB_IP,
            "credential_identifier": _credential_identifier(
                DEFAULT_ABB_USER, DEFAULT_ABB_PASSWORD
            ),
            "device_profile": deepcopy(SHARED_DEVICE_PROFILES["robot"]),
            "channel_map": {},
        },
        {
            "id": "m3232_pressure", "enabled": True,
            "role": "pressure", "driver": "m3232_pressure",
            "endpoint": "", "baudrate": M3232_BAUDRATE,
            "matrix_rows": 32, "matrix_cols": 32,
            "channels": [M3232_RAW_CHANNEL], "data_mode": "raw_matrix", "protocol_profile": "m3232_uart_text_v1", "channel_map": {},
        },
        {
            "id": "esr1001_strain", "enabled": True,
            "role": "esr1001_strain", "driver": "modbus_rtu_esr1001",
            "endpoint": "", "baudrate": ESR1001_BAUDRATE,
            "slave_id": ESR1001_DEFAULT_SLAVE_ID,
            "freshness_seconds": ESR1001_FRESHNESS_SECONDS,
            "channels": list(ESR1001_CHANNELS), "channel_map": {},
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
        if not profile.get("historical_only")
    ]


def _apply_sensor_interface_profile(interface: dict[str, Any]) -> dict[str, Any]:
    sensor_type = _canonical_sensor_type(interface.get("role"), interface.get("driver"))
    profile = SENSOR_INTERFACE_PROFILES[sensor_type]
    interface["role"] = sensor_type
    usb_strain = sensor_type == "esr1001_strain" and interface.get("driver") == USB_DRIVER
    if usb_strain:
        interface["channel_settings"] = deepcopy(interface.get("channel_settings") or {})
        if not isinstance(interface["channel_settings"], dict) or not isinstance(interface["channel_settings"].get("esr_context", {}), dict):
            raise ValueError("ESR通道设置及现场信息必须为对象")
        interface["protocol"] = USB_DRIVER
        interface["baudrate"] = 115200
        interface["data_mode"] = str(interface.get("data_mode") or "raw_adc")
        if interface["data_mode"] not in {"raw_adc", "microstrain"}:
            raise ValueError("USB数据模式必须是raw_adc或microstrain")
        interface["channel_count"] = 4
        calibration = interface.get("calibration_profile")
        if calibration:
            profile_data = json.loads(Path(calibration).read_text(encoding="utf-8")) if isinstance(calibration, str) else calibration
            interface["calibration_digest"] = hashlib.sha256(json.dumps(profile_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        else:
            interface["calibration_digest"] = ""
    elif not profile.get("editable_driver"):
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
    if profile.get("baudrate") and sensor_type not in {THERMOCOUPLE64_ROLE, "esr1001_strain", "pressure"}:
        interface["baudrate"] = int(profile["baudrate"])
    if profile.get("channel_types") and not interface.get("channel_types"):
        interface["channel_types"] = list(profile["channel_types"])
    if sensor_type == "plc":
        if not interface.get("register_map"):
            interface["register_map"] = dict(PLC_DEFAULT_REGISTER_MAP)
        interface["slave_id"] = int(
            interface.get("slave_id") or DEFAULT_PLC_SLAVE_ID
        )
    elif sensor_type == THERMOCOUPLE64_ROLE:
        slave_id = int(interface.get("slave_id", 1))
        if not 1 <= slave_id <= 247:
            raise ValueError("慧谱仪表地址必须在1到247之间")
        interface["slave_id"] = slave_id
        interface["read_profile"] = normalize_read_profile(interface.get("read_profile"))
        interface["channel_settings"] = deepcopy(interface.get("channel_settings") or {})
        if not isinstance(interface["channel_settings"], dict):
            raise ValueError("慧谱通道设置必须为对象")
        interface["reconnect_window_seconds"] = max(0.0, min(float(interface.get("reconnect_window_seconds", 60.0)), 300.0))
        interface["freshness_seconds"] = float(interface.get("freshness_seconds") or 1.0)
    elif sensor_type == "pressure":
        if interface.get("enabled", True):
            normalize_m3232(interface)
    elif sensor_type == "esr1001_strain":
        if not usb_strain:
            interface["baudrate"] = int(interface.get("baudrate") or ESR1001_BAUDRATE)
        interface["slave_id"] = int(
            interface.get("slave_id") or ESR1001_DEFAULT_SLAVE_ID
        )
        interface["freshness_seconds"] = float(
            interface.get("freshness_seconds") or ESR1001_FRESHNESS_SECONDS
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
            "schema_version": 2,
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
    """Validate role/protocol and physical endpoint ownership.

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
    serial_endpoints: dict[str, dict[str, Any]] = {}
    for item in interfaces:
        if not item.get("enabled", True):
            continue
        role = _canonical_sensor_type(item.get("role"), item.get("driver"))
        profile = SENSOR_INTERFACE_PROFILES.get(role, SENSOR_INTERFACE_PROFILES["custom"])
        expected_driver = str(profile.get("driver") or "")
        actual_driver = str(item.get("driver") or "")
        if not profile.get("editable_driver") and actual_driver not in profile.get("allowed_drivers", [expected_driver]):
            message = (
                f"接口“{item.get('id', role)}”的协议/驱动与传感器类型不匹配："
                f"{actual_driver or '未填写'}，应为{expected_driver}"
            )
            issues.append({"code": "driver_mismatch", "message": message, "interface_ids": [str(item.get("id") or role)]})
        expected_kind = str(profile.get("physical_kind") or "")
        actual_kind = str(item.get("physical_interface_kind") or "")
        fallback_binding = bool(item.get("physical_fallback", False))
        if actual_kind and expected_kind and actual_kind != expected_kind and not fallback_binding:
            message = (
                f"接口“{item.get('id', role)}”的物理接口类型与协议不匹配："
                f"{actual_kind}，应为{expected_kind}"
            )
            issues.append({"code": "physical_kind_mismatch", "message": message, "interface_ids": [str(item.get("id") or role)]})
        if expected_kind == "serial":
            serial_endpoint = _physical_interface_key(item.get("endpoint"))
            if serial_endpoint:
                previous_serial = serial_endpoints.get(serial_endpoint)
                if previous_serial is not None:
                    message = (
                        f"独占串口“{item.get('endpoint')}”重复绑定："
                        f"{previous_serial.get('id', '前一接口')}与{item.get('id', role)}"
                    )
                    issues.append(
                        {
                            "code": "duplicate_serial_endpoint",
                            "message": message,
                            "resource_id": serial_endpoint,
                            "interface_ids": [
                                str(previous_serial.get("id") or ""),
                                str(item.get("id") or role),
                            ],
                        }
                    )
                else:
                    serial_endpoints[serial_endpoint] = item
        physical_id = _physical_interface_key(item.get("physical_interface_id"))
        physical_port_id = _physical_interface_key(item.get("physical_port_id"))
        if acquisition_mode == "real" and not physical_id and not physical_port_id:
            message = f"真实接口“{item.get('id', role)}”尚未绑定实际物理接口"
            issues.append({"code": "missing_physical_binding", "message": message, "interface_ids": [str(item.get("id") or role)]})
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
            "interface_ids": [str(previous.get("id") or ""), str(item.get("id") or role)],
        })
    return issues


def _validate_physical_interface_bindings(
    interfaces: list[dict[str, Any]], acquisition_mode: str
) -> None:
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
        raw_usb = item.get("driver") == USB_DRIVER and item.get("data_mode") == "raw_adc"
        allowed = set(RAW_CHANNELS if raw_usb else item.get("channels") if item.get("driver") == "m3232_pressure" else profile.get("channels") or []) & capture_set
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
    ``channels``/``values`` object.  The historical thermocouple role accepts
    exactly its eight positions; the current 64-channel role accepts exactly
    64 positions.  Other roles cannot claim 64-channel thermocouple names.
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
                if len(nested) != 8:
                    raise ValueError("historical thermocouple array must contain exactly 8 values")
                for index, value in enumerate(nested, start=1):
                    flattened[f"temperature_{index}"] = value
            elif role == THERMOCOUPLE64_ROLE:
                if len(nested) != 64:
                    raise ValueError("thermocouple64 array must contain exactly 64 values")
                for index, value in enumerate(nested, start=1):
                    flattened[f"temperature_{index}"] = value
    # Common vendor spelling for a temperature array.
    for array_key in ("thermocouples", "thermocouple", "temperatures", "tc"):
        values = payload.get(array_key)
        if isinstance(values, (list, tuple)):
            expected = 64 if role == THERMOCOUPLE64_ROLE else 8 if role == "thermocouple" else 0
            if not expected:
                continue
            if len(values) != expected:
                raise ValueError(f"thermocouple array must contain exactly {expected} values")
            for index, value in enumerate(values, start=1):
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
        if role in {"thermocouple", THERMOCOUPLE64_ROLE} and target == source:
            match = re.fullmatch(r"(?:ch|channel|tc|t)[_ -]?(\d+)", source, re.I)
            if match:
                target = f"温度{int(match.group(1))}"
        if role == THERMOCOUPLE64_ROLE:
            match = re.fullmatch(r"(?:temperature|temp)[_ -]?(\d+)", source, re.I)
            if match:
                target = f"温度{int(match.group(1))}"
        if target in THERMOCOUPLE64_CHANNELS:
            channel_number = int(target.removeprefix("温度"))
            if role == THERMOCOUPLE64_ROLE:
                pass
            elif role == "thermocouple" and channel_number <= 8:
                pass
            elif source in channel_map or source.lower() in channel_map:
                pass
            else:
                continue
        if target in ALL_SENSOR_COLUMNS:
            number = _finite(value)
            if number is not None:
                normalized[target] = number
    return normalized


def publish_thermocouple64_snapshot(
    cache: ChannelSampleCache,
    snapshot: Thermocouple64Snapshot,
    *,
    interface_id: str = THERMOCOUPLE64_INTERFACE_ID,
    received_wall_time: float | None = None,
    received_monotonic: float | None = None,
) -> None:
    """Publish all 64 adapter results into the per-channel frame cache."""

    wall_time = float(received_wall_time if received_wall_time is not None else time.time())
    monotonic = float(
        received_monotonic if received_monotonic is not None else time.monotonic()
    )
    quality_map = {
        "measured_new": ChannelQuality.MEASURED_NEW,
        "missing": ChannelQuality.MISSING,
        "stale": ChannelQuality.STALE,
        "invalid": ChannelQuality.INVALID,
    }
    for channel in THERMOCOUPLE64_CHANNELS:
        raw_quality = str(snapshot.quality.get(channel) or "invalid")
        cache.publish(
            ChannelSample(
                interface_id=interface_id,
                channel_name=channel,
                value=snapshot.values.get(channel),
                quality=quality_map.get(raw_quality, ChannelQuality.INVALID),
                device_timestamp=snapshot.source_timestamp,
                received_wall_time=wall_time,
                received_monotonic=monotonic,
                source_sequence=snapshot.source_sequence,
                protocol_ok=True,
                error=str(snapshot.errors.get(channel) or ""),
                metadata={"device_identity": snapshot.device_identity},
            )
        )


@dataclass
class AcquisitionConfig:
    diagnostic_validation: InitVar[bool] = False
    application_version: str = ""
    release_revision: str = ""
    configuration_schema_version: int = 0
    processing_mode: str = "prediction_warning"
    dataset_schema: str = "legacy_original"
    use_best_prediction_override: bool = False
    driver: str = "simulator"
    endpoint: str = ""
    baudrate: int = 0
    matrix_rows: int = 32
    matrix_cols: int = 32
    protocol_profile: str = "m3232_uart_text_v1"
    data_mode: str = "raw_matrix"
    device_identity: dict[str, Any] | None = None
    calibration_profile: Any = None
    slave_id: int = 1
    read_profile: str = "channel_temperature"
    timeout: float = 0.5
    channel_settings: dict[str, Any] | None = None
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
    # Real acquisition must either prove a durable destination or explicitly
    # opt into a bounded, non-production engineering preview.
    persistence_policy: str = "required"
    writer_queue_rows: int = 1024
    writer_submit_timeout_seconds: float = 0.05
    writer_stop_timeout_seconds: float = 5.0
    stop_sampling_timeout_seconds: float = 5.0
    stop_force_timeout_seconds: float = 2.0
    first_sample_timeout_seconds: float = 3.0
    async_start: bool = False
    start_request_id: str = ""
    capture_generation: str = ""
    initial_compaction_force_N: float = 400.0
    placement_speed_mm_s: float = 80.0
    pid_angle_deg: float = 5.0
    temperature_setpoint_C: float = 360.0
    replicate: int = 1
    # Explicitly separate real interfaces from local simulation sources.
    # Empty keeps backward compatibility with older callers: a simulator
    # driver implies simulation, while serial/TCP implies real interfaces.
    acquisition_mode: str = ""
    # Orthogonal to acquisition_mode: real captures explicitly choose where
    # the physical drivers live.  The dataclass default preserves direct
    # constructor compatibility; external payload builders enforce presence.
    real_acquisition_mode: str = "local_direct"
    execution_host: str = ""
    helper_session_id: str = ""
    helper_capabilities: dict[str, Any] | None = None
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
        self.application_version = str(
            self.application_version or RELEASE_CONTRACT.application_version
        ).strip()
        self.release_revision = str(
            self.release_revision or RELEASE_CONTRACT.release_revision
        ).strip()
        self.configuration_schema_version = int(
            self.configuration_schema_version
            or RELEASE_CONTRACT.configuration_schema_version
        )
        requested_mode = str(self.acquisition_mode or "").lower()
        if not requested_mode:
            requested_mode = "simulation" if self.driver == "simulator" else "real"
        self.acquisition_mode = requested_mode
        if self.acquisition_mode not in {"real", "simulation"}:
            raise ValueError("acquisition_mode must be real or simulation")
        self.real_acquisition_mode = str(self.real_acquisition_mode or "").strip().lower()
        self.execution_host = str(self.execution_host or "").strip().lower()
        self.helper_session_id = str(self.helper_session_id or "").strip()
        self.helper_capabilities = dict(self.helper_capabilities or {})
        self.persistence_policy = str(self.persistence_policy or "required").strip().lower()
        self.writer_queue_rows = max(1, min(int(self.writer_queue_rows or 1024), 200000))
        self.writer_submit_timeout_seconds = max(
            0.0, min(float(self.writer_submit_timeout_seconds or 0.0), 5.0)
        )
        self.writer_stop_timeout_seconds = max(
            0.001, min(float(self.writer_stop_timeout_seconds or 5.0), 60.0)
        )
        self.stop_sampling_timeout_seconds = max(
            0.001, min(float(self.stop_sampling_timeout_seconds or 5.0), 60.0)
        )
        self.stop_force_timeout_seconds = max(
            0.001, min(float(self.stop_force_timeout_seconds or 2.0), 30.0)
        )
        self.first_sample_timeout_seconds = max(
            0.01, min(float(self.first_sample_timeout_seconds or 3.0), 30.0)
        )
        if self.acquisition_mode == "simulation":
            self.real_acquisition_mode = ""
            if self.persistence_policy == "required":
                self.persistence_policy = "optional"
        else:
            if self.persistence_policy not in {"required", "engineering_preview"}:
                raise ValueError(
                    "persistence_policy must be required or engineering_preview for real capture"
                )
            if self.real_acquisition_mode not in {"local_direct", "remote_helper"}:
                raise ValueError(
                    "real_acquisition_mode must be local_direct or remote_helper"
                )
            remote_fields_present = bool(
                self.helper_session_id
                or self.helper_capabilities
                or self.execution_host == "helper_local"
            )
            if self.real_acquisition_mode == "local_direct" and remote_fields_present:
                raise ValueError(
                    "mode_field_conflict: local_direct cannot use Helper session, "
                    "capability, or helper_local execution fields"
                )
            if (
                self.real_acquisition_mode == "remote_helper"
                and self.execution_host != "helper_local"
            ):
                raise ValueError(
                    "mode_field_conflict: remote_helper requires execution_host=helper_local"
                )
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
        if self.dataset_schema == "esr1001_raw_adc":
            if self.processing_mode != "capture_only":
                raise ValueError("原始ADC诊断只允许capture_only，不允许模型输入")
            if self.mysql_enabled or self.mysql_local_enabled:
                raise ValueError("原始ADC诊断保存为独立CSV，不写入规范微应变MySQL表")
        if self.interfaces is None:
            self.interfaces = [{
                "id": "interface_1", "enabled": True,
                "driver": self.driver, "endpoint": self.endpoint,
                "baudrate": self.baudrate,
                "role": THERMOCOUPLE64_ROLE if self.driver == THERMOCOUPLE64_DRIVER else "pressure" if self.driver == "m3232_pressure" else "other",
                **({"matrix_rows": self.matrix_rows, "matrix_cols": self.matrix_cols, "protocol_profile": self.protocol_profile,
                    "data_mode": self.data_mode, "device_identity": self.device_identity or {}, "calibration_profile": self.calibration_profile}
                   if self.driver == "m3232_pressure" else {}),
                **({"read_profile": self.read_profile, "timeout": self.timeout,
                    "slave_id": self.slave_id, "channel_settings": deepcopy(self.channel_settings or {})}
                   if self.driver == THERMOCOUPLE64_DRIVER else {}),
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
            if role_hint == THERMOCOUPLE64_ROLE and requested_driver == "thermocouple64_protocol_pending":
                requested_driver = THERMOCOUPLE64_DRIVER
            if requested_driver and not profile_hint.get("editable_driver"):
                expected_driver = str(profile_hint.get("driver") or "")
                if requested_driver != expected_driver and not (role_hint == "esr1001_strain" and requested_driver == USB_DRIVER):
                    message = (
                        f"接口“{interface['id']}”的协议/驱动与传感器类型不匹配："
                        f"{requested_driver}，应为{expected_driver}"
                    )
                    if diagnostic_validation:
                        self.configuration_issues.append({"code": "driver_mismatch", "message": message, "interface_ids": [interface["id"]]})
                    else:
                        raise ValueError(message)
            interface["driver"] = requested_driver or (
                str(profile_hint.get("driver") or self.driver)
                if role_hint != "custom" else self.driver
            )
            interface["endpoint"] = str(interface.get("endpoint") or "").strip()
            interface["baudrate"] = int(interface.get("baudrate") or (ESR1001_BAUDRATE if role_hint == "esr1001_strain" and interface["driver"] != USB_DRIVER else M3232_BAUDRATE if role_hint == "pressure" else self.baudrate or 115200))
            interface["timeout"] = max(0.01, min(float(interface.get("timeout") or (0.5 if role_hint == THERMOCOUPLE64_ROLE else 0.05)), 2.0))
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
                interface.get("slave_id") if interface.get("slave_id") is not None else (
                    ESR1001_DEFAULT_SLAVE_ID
                    if role_hint == "esr1001_strain"
                    else 1 if role_hint == THERMOCOUPLE64_ROLE else DEFAULT_PLC_SLAVE_ID
                )
            )
            _apply_sensor_interface_profile(interface)
            if self.acquisition_mode == "simulation" and interface.get("driver") == "m3232_pressure":
                interface["channels"] = ["薄膜压力"]
            authoritative_device_profile = SHARED_DEVICE_PROFILES.get(interface["role"])
            if authoritative_device_profile is not None:
                interface["device_profile"] = deepcopy(authoritative_device_profile)
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
            default_transport_family = {
                "ethernet": "ethernet",
                "usb_uvc": "usb",
                "usb_hid": "usb",
                "serial": "serial_native",
            }.get(interface["physical_interface_kind"], "")
            interface["physical_transport_family"] = str(
                interface.get("physical_transport_family")
                or default_transport_family
            ).strip().lower()
            raw_device_identity = interface.get("device_identity")
            interface["device_identity"] = (
                deepcopy(raw_device_identity)
                if isinstance(raw_device_identity, dict)
                else {}
            )
            interface["physical_verified"] = bool(interface.get("physical_verified", False))
            interface["physical_fallback"] = bool(interface.get("physical_fallback", False))
            normalized_interfaces.append(interface)
        if not normalized_interfaces:
            raise ValueError("至少配置一个采集接口")
        self.interfaces = normalized_interfaces
        for item in normalized_interfaces:
            if not item.get("enabled", True):
                continue
            raw = item.get("driver") == USB_DRIVER and item.get("data_mode") == "raw_adc"
            if self.dataset_schema == "esr1001_raw_adc" and not raw:
                raise ValueError("独立ADC诊断方案只允许USB原始接口")
            if raw and self.dataset_schema != "esr1001_raw_adc" and self.dataset_schema not in MODERN_COLLECTION_SCHEMAS:
                raise ValueError("原始ADC联合采集需要正常数据方案")
            if raw and self.processing_mode != "capture_only":
                raise ValueError("原始ADC只允许capture_only，不允许模型输入")
        binding_issues = _physical_interface_binding_issues(normalized_interfaces, self.acquisition_mode)
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
                        "code": "simulation_driver_in_real_mode", "message": message,
                        "interface_ids": [str(item.get("id") or "") for item in normalized_interfaces if item.get("enabled", True) and item.get("driver") == "simulator"],
                    })
                else:
                    raise ValueError(message)
            historical_thermocouples = [
                item for item in enabled if item.get("role") == "thermocouple"
            ]
            if historical_thermocouples:
                message = "旧8路热电偶仅支持历史文件只读回放，不能用于新的真实采集"
                if diagnostic_validation:
                    self.configuration_issues.append({
                        "code": "historical_thermocouple_read_only",
                        "message": message,
                        "interface_ids": [str(item.get("id") or "") for item in historical_thermocouples],
                    })
                else:
                    raise ValueError(message)
            self.source_file = ""
            self.simulation_source_path = ""
        else:
            self.source_file = self.simulation_source_path
        if self.acquisition_mode == "real" and "薄膜压力" in (self.selected_sensors or []) and any(
            i.get("enabled", True) and i.get("driver") == "m3232_pressure" and i.get("data_mode") == "raw_matrix" for i in self.interfaces):
            raise ValueError("M3232原始矩阵未标定，N通道不可用；请选择薄膜压力原始响应/raw或提供有效标定档")
        allowed_sensors = self.schema_sensors
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
            and not (self.acquisition_mode == "real" and name in {M3232_RAW_CHANNEL, "薄膜压力"})
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
        return self._effective_adc_columns(ACQUISITION_SCHEMAS[self.dataset_schema]["sensors"])

    def _effective_adc_columns(self, columns: list[str]) -> list[str]:
        """按有效接口用途生成通道，原始计数不占用规范微应变列。"""
        if self.acquisition_mode == "real" and any(
            i.get("enabled", True) and i.get("driver") == "m3232_pressure" and i.get("data_mode") == "raw_matrix"
            for i in self.interfaces or []):
            columns = [M3232_RAW_CHANNEL if n == "薄膜压力" else n for n in columns]
            if M3232_RAW_CHANNEL not in columns:
                columns = [*columns, M3232_RAW_CHANNEL]
        raw_enabled = any(
            item.get("enabled", True) and item.get("driver") == USB_DRIVER
            and item.get("data_mode") == "raw_adc"
            for item in (self.interfaces or [])
        )
        if not raw_enabled or self.dataset_schema not in MODERN_COLLECTION_SCHEMAS:
            return list(columns)
        replacements = dict(zip(STRAIN_SENSOR_COLUMNS, RAW_CHANNELS))
        result = [replacements.get(name, name) for name in columns]
        if not any(name in result for name in RAW_CHANNELS):
            result = [*RAW_CHANNELS, *result]
        return result

    @property
    def raw_columns(self) -> list[str]:
        """Return only selected sensor columns plus identity/process fields."""
        definition = ACQUISITION_SCHEMAS[self.dataset_schema]
        selected = set(self.selected_sensors or [])
        sensor_names = set(self.schema_sensors)
        columns = [
            name
            for name in self._effective_adc_columns(definition["raw_columns"])
            if name not in sensor_names or name in selected
        ]
        missing = selected.difference(columns)
        if missing:
            raise ValueError("selected_channels_missing_from_csv: " + ", ".join(sorted(missing)))
        return columns

    @property
    def process_columns(self) -> list[str]:
        return list(PROCESS_PARAMETER_COLUMNS)


ACQUISITION_TRANSPORT_METADATA_FIELDS = frozenset(
    {
        "simulation_execution_choice",
        "simulation_source_id",
        "simulation_source_ready",
        "simulation_source_transfer",
        "force_check",
    }
)


def acquisition_config_from_payload(payload: dict[str, Any] | None) -> AcquisitionConfig:
    """Build a strict acquisition config after removing known route metadata.

    Unknown acquisition fields deliberately remain in ``values`` so the
    dataclass raises instead of silently accepting a misspelled configuration.
    """
    values = dict(payload or {})
    requested_mode = str(values.get("acquisition_mode") or "").strip().lower()
    requested_driver = str(values.get("driver") or "simulator").strip().lower()
    if (requested_mode == "real" or (not requested_mode and requested_driver != "simulator")) and not str(
        values.get("real_acquisition_mode") or ""
    ).strip():
        raise ValueError(
            "real_acquisition_mode is required for external real acquisition configuration"
        )
    for field_name in ACQUISITION_TRANSPORT_METADATA_FIELDS:
        values.pop(field_name, None)
    return AcquisitionConfig(**values)


def acquisition_diagnostic_config_from_payload(payload: dict[str, Any] | None) -> AcquisitionConfig:
    values = dict(payload or {})
    requested_mode = str(values.get("acquisition_mode") or "").strip().lower()
    requested_driver = str(values.get("driver") or "simulator").strip().lower()
    if (requested_mode == "real" or (not requested_mode and requested_driver != "simulator")) and not str(
        values.get("real_acquisition_mode") or ""
    ).strip():
        raise ValueError(
            "real_acquisition_mode is required for external real acquisition configuration"
        )
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
    if config.dataset_schema == "esr1001_raw_adc":
        return "ESR1001_ADC_" + _safe_component(config.specimen_id, max_length=48)
    # Keep the physical condition and independent replicate visible in the
    # folder name.  The layer files and complete-specimen snapshot inherit
    # this prefix, so files from different conditions/replicates cannot be
    # mixed accidentally.
    condition = _safe_component(config.condition_id or "LIVE", max_length=24)
    replicate = max(1, int(config.replicate or 1))
    if config.dataset_schema in MODERN_COLLECTION_SCHEMAS:
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


class Esr1001ProtocolError(IOError):
    """A classified ESR1001-A Modbus RTU protocol failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        function_code: int | None = None,
        exception_code: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = str(code)
        self.function_code = function_code
        self.exception_code = exception_code


class Esr1001FirmwareIncompatibleError(Esr1001ProtocolError):
    """The device firmware does not expose the required float registers."""


def classify_esr1001_probe_error(error: Any) -> str:
    """Map a probe failure to the stable hardware-check state catalog."""

    lowered = str(error or "").lower()
    classifications = (
        ("firmware_incompatible", "firmware_incompatible"),
        ("wrong_slave", "wrong_slave"),
        ("wrong_function", "wrong_function"),
        ("byte_count", "length_error"),
        ("length", "length_error"),
        ("crc", "crc_error"),
        ("modbus_exception", "modbus_exception"),
        ("timeout", "no_response"),
        ("access is denied", "port_occupied"),
        ("permission", "port_occupied"),
        ("cannot find", "port_missing"),
        ("file not found", "port_missing"),
        ("filenotfound", "port_missing"),
        ("no such file", "port_missing"),
    )
    return next(
        (value for token, value in classifications if token in lowered),
        "serial_parameter_error",
    )


def crc16_modbus(payload: bytes) -> int:
    """Return the Modbus CRC integer whose low byte is transmitted first."""

    crc = 0xFFFF
    for value in bytes(payload):
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def esr1001_append_crc(payload: bytes) -> bytes:
    crc = crc16_modbus(payload)
    return bytes(payload) + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def esr1001_crc_valid(frame: bytes) -> bool:
    raw = bytes(frame)
    if len(raw) < 4:
        return False
    expected = crc16_modbus(raw[:-2])
    return raw[-2:] == bytes((expected & 0xFF, (expected >> 8) & 0xFF))


def esr1001_read_request(slave_id: int = ESR1001_DEFAULT_SLAVE_ID) -> bytes:
    payload = struct.pack(
        ">BBHH",
        int(slave_id) & 0xFF,
        ESR1001_READ_FUNCTION,
        ESR1001_FLOAT_REGISTER,
        ESR1001_FLOAT_REGISTER_COUNT,
    )
    return esr1001_append_crc(payload)


def _esr1001_write_register_request(
    slave_id: int, register: int, value: int
) -> bytes:
    return esr1001_append_crc(
        struct.pack(">BBHH", int(slave_id) & 0xFF, 0x06, register, value)
    )


def _esr1001_write_coil_request(
    slave_id: int, register: int, enabled: bool
) -> bytes:
    return esr1001_append_crc(
        struct.pack(
            ">BBHH",
            int(slave_id) & 0xFF,
            0x05,
            register,
            0xFF00 if enabled else 0x0000,
        )
    )


def _raise_esr1001_exception(frame: bytes) -> None:
    function_code = frame[1]
    exception_code = frame[2] if len(frame) >= 3 else None
    if function_code == (ESR1001_READ_FUNCTION | 0x80) and exception_code == 0x02:
        raise Esr1001FirmwareIncompatibleError(
            "firmware_incompatible",
            "设备固件不支持0x0030实数输入寄存器",
            function_code=function_code,
            exception_code=exception_code,
        )
    raise Esr1001ProtocolError(
        "modbus_exception",
        f"功能码0x{function_code:02X}，异常码0x{int(exception_code or 0):02X}",
        function_code=function_code,
        exception_code=exception_code,
    )


def _validate_esr1001_frame(
    frame: bytes,
    *,
    slave_id: int,
    expected_function: int,
) -> bytes:
    raw = bytes(frame)
    if len(raw) < 5:
        raise Esr1001ProtocolError("length", f"响应长度不足：{len(raw)}")
    if not esr1001_crc_valid(raw):
        raise Esr1001ProtocolError("crc", "响应CRC16/MODBUS校验失败")
    if raw[0] != (int(slave_id) & 0xFF):
        raise Esr1001ProtocolError(
            "wrong_slave", f"期望从站{slave_id}，收到{raw[0]}"
        )
    if raw[1] & 0x80:
        _raise_esr1001_exception(raw)
    if raw[1] != expected_function:
        raise Esr1001ProtocolError(
            "wrong_function",
            f"期望功能码0x{expected_function:02X}，收到0x{raw[1]:02X}",
            function_code=raw[1],
        )
    return raw


def parse_esr1001_read_response(
    frame: bytes, *, slave_id: int = ESR1001_DEFAULT_SLAVE_ID
) -> dict[str, float]:
    raw = _validate_esr1001_frame(
        frame, slave_id=slave_id, expected_function=ESR1001_READ_FUNCTION
    )
    if raw[2] != 16:
        raise Esr1001ProtocolError(
            "byte_count", f"期望16个数据字节，收到{raw[2]}"
        )
    if len(raw) != 21:
        raise Esr1001ProtocolError(
            "length", f"期望21字节响应，收到{len(raw)}"
        )
    device_values = [
        struct.unpack(">f", raw[offset : offset + 4])[0]
        for offset in range(3, 19, 4)
    ]
    if not all(math.isfinite(value) for value in device_values):
        raise Esr1001ProtocolError("non_finite", "响应包含非有限float32")
    by_name = dict(zip(ESR1001_DEVICE_ORDER, device_values))
    return {name: float(by_name[name]) for name in ESR1001_CHANNELS}


class Esr1001StrainDriver(SampleDriver):
    """Strict single-device Modbus RTU driver for ESR1001-A."""

    def __init__(
        self,
        endpoint: str,
        baudrate: int = ESR1001_BAUDRATE,
        slave_id: int = ESR1001_DEFAULT_SLAVE_ID,
        timeout: float = ESR1001_RESPONSE_TIMEOUT_SECONDS,
        *,
        serial_factory: Any = None,
    ) -> None:
        self.endpoint = str(endpoint or "").strip()
        self.baudrate = int(baudrate or ESR1001_BAUDRATE)
        self.slave_id = int(slave_id or ESR1001_DEFAULT_SLAVE_ID)
        if not 1 <= self.slave_id <= 247:
            raise ValueError("ESR1001-A从站地址必须在1到247之间")
        self.timeout = max(0.01, min(float(timeout), 2.0))
        self.serial_factory = serial_factory
        self.serial = None
        self._rx_buffer = bytearray()
        self._reset_before_request = True
        self.protocol_verified = False
        self.error_counts: dict[str, int] = {}
        self.last_error = ""
        self.last_valid_time: float | None = None
        self.observation_count = 0
        self.quality_metadata: dict[str, Any] = self._quality_metadata()

    def _quality_metadata(self) -> dict[str, Any]:
        return {
            "protocol": "modbus_rtu",
            "driver": "modbus_rtu_esr1001",
            "slave_id": self.slave_id,
            "observation_count": self.observation_count,
            "last_valid_time": self.last_valid_time,
            "protocol_error_counts": dict(self.error_counts),
            "last_error": self.last_error,
        }

    def _record_error(self, error: Exception) -> None:
        code = getattr(error, "code", error.__class__.__name__)
        self.error_counts[str(code)] = self.error_counts.get(str(code), 0) + 1
        self.last_error = str(error)
        self.quality_metadata = self._quality_metadata()

    @staticmethod
    def _default_serial_factory(**kwargs: Any):
        import serial

        return serial.Serial(**kwargs)

    def _expected_frame_length(self, buffer: bytearray) -> int | None:
        if len(buffer) < 2:
            return None
        function_code = buffer[1]
        if function_code & 0x80:
            return 5
        if function_code in {0x05, 0x06}:
            return 8
        if function_code in {0x01, 0x02, 0x03, 0x04}:
            if len(buffer) < 3:
                return None
            byte_count = int(buffer[2])
            if byte_count > 250:
                raise Esr1001ProtocolError(
                    "byte_count", f"响应字节数无效：{byte_count}"
                )
            return 5 + byte_count
        raise Esr1001ProtocolError(
            "wrong_function", f"无法识别功能码0x{function_code:02X}"
        )

    def _read_rtu_frame(
        self, *, expected_function: int, deadline_seconds: float
    ) -> bytes:
        if self.serial is None:
            raise Esr1001ProtocolError("port_closed", "串口尚未打开")
        deadline = time.monotonic() + max(0.001, float(deadline_seconds))
        last_error: Esr1001ProtocolError | None = None
        while time.monotonic() < deadline:
            slave_byte = self.slave_id & 0xFF
            while self._rx_buffer and self._rx_buffer[0] != slave_byte:
                del self._rx_buffer[0]
            try:
                frame_length = self._expected_frame_length(self._rx_buffer)
            except Esr1001ProtocolError as exc:
                last_error = exc
                if self._rx_buffer:
                    del self._rx_buffer[0]
                continue
            if frame_length is not None and len(self._rx_buffer) >= frame_length:
                candidate = bytes(self._rx_buffer[:frame_length])
                del self._rx_buffer[:frame_length]
                try:
                    _validate_esr1001_frame(
                        candidate,
                        slave_id=self.slave_id,
                        expected_function=expected_function,
                    )
                    return candidate
                except Esr1001ProtocolError as exc:
                    last_error = exc
                    if exc.code not in {"crc", "wrong_function"}:
                        raise
                    continue
            chunk = self.serial.read(256)
            if chunk:
                self._rx_buffer.extend(chunk)
                continue
            time.sleep(0.001)
        if last_error is not None:
            raise last_error
        raise Esr1001ProtocolError("timeout", "在响应期限内未收到完整RTU帧")

    def _transact(
        self,
        request: bytes,
        *,
        expected_function: int,
        deadline_seconds: float | None = None,
    ) -> bytes:
        if self.serial is None:
            raise Esr1001ProtocolError("port_closed", "串口尚未打开")
        if self._reset_before_request:
            reset = getattr(self.serial, "reset_input_buffer", None)
            if callable(reset):
                reset()
            self._rx_buffer.clear()
        written = self.serial.write(bytes(request))
        if written != len(request):
            raise Esr1001ProtocolError(
                "write_incomplete", f"请求写入{written}/{len(request)}字节"
            )
        flush = getattr(self.serial, "flush", None)
        if callable(flush):
            flush()
        return self._read_rtu_frame(
            expected_function=expected_function,
            deadline_seconds=(
                self.timeout if deadline_seconds is None else deadline_seconds
            ),
        )

    def _validate_write_echo(self, response: bytes, request: bytes) -> None:
        raw = _validate_esr1001_frame(
            response, slave_id=self.slave_id, expected_function=request[1]
        )
        if len(raw) != 8 or raw[:-2] != request[:-2]:
            raise Esr1001ProtocolError("write_echo", "写操作应答与请求不一致")

    def open(self) -> None:
        if not self.endpoint:
            raise ValueError("ESR1001-A必须明确选择串口，不能硬编码现场COM号")
        factory = self.serial_factory or self._default_serial_factory
        self.serial = factory(
            port=self.endpoint,
            baudrate=self.baudrate,
            bytesize=ESR1001_BYTESIZE,
            parity=ESR1001_PARITY,
            stopbits=ESR1001_STOPBITS,
            timeout=min(0.02, self.timeout),
            write_timeout=self.timeout,
        )
        try:
            probe = self._transact(
                esr1001_read_request(self.slave_id),
                expected_function=ESR1001_READ_FUNCTION,
            )
            parse_esr1001_read_response(probe, slave_id=self.slave_id)
            enable = _esr1001_write_register_request(
                self.slave_id, ESR1001_CHANNEL_COUNT_REGISTER, 4
            )
            self._validate_write_echo(
                self._transact(enable, expected_function=0x06), enable
            )
            start = _esr1001_write_coil_request(
                self.slave_id, ESR1001_ACQUISITION_COIL, True
            )
            self._validate_write_echo(
                self._transact(start, expected_function=0x05), start
            )
            self.protocol_verified = True
            self.last_error = ""
            self.quality_metadata = self._quality_metadata()
        except Exception as exc:
            self._record_error(exc)
            self.close()
            raise

    def read_sample(self) -> dict[str, float] | None:
        try:
            frame = self._transact(
                esr1001_read_request(self.slave_id),
                expected_function=ESR1001_READ_FUNCTION,
            )
            values = parse_esr1001_read_response(frame, slave_id=self.slave_id)
        except Exception as exc:
            self._record_error(exc)
            raise
        self.observation_count += 1
        self.last_valid_time = time.time()
        self.last_error = ""
        self.protocol_verified = True
        self.quality_metadata = self._quality_metadata()
        return values

    def close(self) -> None:
        serial_port = self.serial
        if serial_port is None:
            return
        try:
            stop = _esr1001_write_coil_request(
                self.slave_id, ESR1001_ACQUISITION_COIL, False
            )
            response = self._transact(
                stop, expected_function=0x05, deadline_seconds=self.timeout
            )
            self._validate_write_echo(response, stop)
        except Exception as exc:
            self._record_error(exc)
        finally:
            try:
                serial_port.close()
            finally:
                self.serial = None
                self._rx_buffer.clear()


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
        self._esr_replay = None
        if set(RAW_CHANNELS).issubset(available) and "raw_hex" in available:
            metadata = json.loads(self._files[0].with_suffix(".json").read_text(encoding="utf-8"))
            profile = metadata.get("calibration_snapshot")
            if profile:
                available.update(f"应变{i}" for i in range(1, 5))
            missing = set(self.sensor_columns) - available
            if missing:
                raise ValueError(f"历史源帧缺少适用档案或通道：{sorted(missing)}")
            self._esr_replay = iter(replay_source_capture(self._files[0]))
            return
        missing = [name for name in self.sensor_columns if name not in available]
        if missing:
            raise ValueError(f"模拟数据缺少传感器列：{missing}")
        self.index = 0
        self._file_index = 0
        self._open_current()

    def read_sample(self) -> dict[str, float] | None:
        if getattr(self, "_esr_replay", None) is not None:
            try:
                record = next(self._esr_replay)
            except StopIteration:
                self._esr_replay = iter(replay_source_capture(self._files[0]))
                record = next(self._esr_replay)
            values = dict(record, **record["calibrated_values"])
            return {name: values[name] for name in self.sensor_columns}
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
            return _normalize_interface_sample(
                {name: record.get(name) for name in self.sensor_columns},
                role=THERMOCOUPLE64_ROLE,
            )
        return None

    def close(self) -> None:
        replay = getattr(self, "_esr_replay", None)
        self._esr_replay = None
        if replay is not None:
            replay.close()
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
                 source_address: str = "") -> None:
        self.endpoint = endpoint or f"{DEFAULT_PLC_IP}:{DEFAULT_PLC_PORT}"
        self.register_map = dict(register_map or PLC_DEFAULT_REGISTER_MAP)
        self.slave_id = int(slave_id)
        self.timeout = max(0.1, float(timeout))
        self.source_address = str(source_address)
        self.actual_source_address = ""
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
        options = {"source_address": (self.source_address, 0)} if self.source_address else {}
        connection = socket.create_connection((host, int(port_text)), timeout=2.0, **options)
        if self.source_address:
            self.actual_source_address = str(connection.getsockname()[0])
            if self.actual_source_address != self.source_address:
                connection.close()
                raise RuntimeError("network_path_invalid: PLC connection source changed")
        self.sock = connection
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
        if len(header) != 7:
            raise IOError("Modbus TCP 响应头不完整")
        transaction_id, protocol_id, length, unit_id = struct.unpack(">HHHB", header)
        if transaction_id != self._transaction_id:
            raise IOError(
                f"Modbus TCP 事务ID不匹配：期望{self._transaction_id}，收到{transaction_id}"
            )
        if protocol_id != 0:
            raise IOError(f"Modbus TCP 协议ID无效：{protocol_id}")
        if unit_id != self.slave_id:
            raise IOError(
                f"Modbus TCP Unit ID不匹配：期望{self.slave_id}，收到{unit_id}"
            )
        if length < 2:
            raise IOError(f"Modbus TCP MBAP长度无效：{length}")
        pdu_length = length - 1
        pdu = self._recv_exact(pdu_length)
        if len(pdu) != pdu_length:
            raise IOError(
                f"Modbus TCP 响应PDU不完整：期望{pdu_length}字节，收到{len(pdu)}字节"
            )
        if len(pdu) < 2:
            raise IOError("Modbus TCP 响应PDU不完整")
        function_code = pdu[0]
        if function_code & 0x80:
            exception_code = pdu[1] if len(pdu) > 1 else 0
            raise IOError(
                f"Modbus 异常响应: 功能码={function_code:#x}, "
                f"异常码={exception_code:#x}"
            )
        if function_code != 3:
            raise IOError(
                f"Modbus TCP 功能码不匹配：期望0x03，收到{function_code:#x}"
            )
        byte_count = pdu[1]
        if byte_count % 2:
            raise IOError(f"Modbus TCP 字节数必须为偶数：{byte_count}")
        expected_byte_count = int(quantity) * 2
        if byte_count != expected_byte_count:
            raise IOError(
                f"Modbus TCP 寄存器数量不匹配：期望{quantity}个，收到{byte_count // 2}个"
            )
        if len(pdu) != 2 + byte_count:
            raise IOError(
                f"Modbus TCP PDU长度不匹配：声明{byte_count}数据字节，"
                f"实际{max(0, len(pdu) - 2)}字节"
            )
        register_data = pdu[2:]
        registers = [
            struct.unpack(">H", register_data[index:index + 2])[0]
            for index in range(0, len(register_data), 2)
        ]
        if len(registers) != int(quantity):
            raise IOError(
                f"Modbus TCP 寄存器数量不匹配：期望{quantity}个，收到{len(registers)}个"
            )
        return registers

    @staticmethod
    def _registers_to_float(low_word: int, high_word: int) -> float:
        value = struct.unpack("<f", struct.pack("<HH", low_word, high_word))[0]
        if not math.isfinite(value):
            raise ValueError("PLC寄存器解析得到非有限浮点值")
        return round(float(value), 2)

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


class AbbRwsError(RuntimeError):
    def __init__(self, code: str, stage: str, detail: str = "") -> None:
        self.code = str(code)
        self.stage = str(stage)
        self.detail = str(detail)
        message = f"{self.code}: ABB RWS {self.stage}"
        if self.detail:
            message += f" ({self.detail})"
        super().__init__(message)


def classify_interface_probe_error(role: str, error_text: str) -> str:
    text = str(error_text or "")
    if "network_path_invalid" in text:
        return "network_path_invalid"
    if str(role or "") == "robot":
        for code in (
            "authentication_failed",
            "resource_not_found",
            "resource_invalid",
            "invalid_json",
            "numeric_invalid",
            "transport_timeout",
            "transport_error",
            "session_limit_exceeded",
            "session_close_failed",
            "http_error",
        ):
            if code in text:
                return code
    if str(role or "") == "thermal_uvc":
        for code in (
            "driver_incompatible",
            "device_open_failed",
            "no_valid_frame",
            "invalid_temperature_matrix",
            "native_call_timeout",
        ):
            if code in text:
                return code
    return "not_connected"


READINESS_STAGE_NAMES = (
    "physical_binding",
    "device_identity",
    "driver",
    "network_path",
    "usb_binding",
    "authentication",
    "protocol",
    "channels",
    "freshness",
    "stability",
)


def normalize_interface_readiness(
    interface_result: dict[str, Any],
) -> dict[str, Any]:
    """Return one explicit readiness schema without erasing raw evidence."""
    result = deepcopy(interface_result)
    if result.get("driver") == USB_DRIVER and result.get("readiness_report"):
        return result
    role = str(result.get("role") or "")
    final_state = str(result.get("state") or "not_connected")
    existing = result.get("stages") if isinstance(result.get("stages"), dict) else {}

    def stage(name: str, state: str, **values: Any) -> dict[str, Any]:
        prior = dict(existing.get(name) or {})
        prior.update(values)
        prior["state"] = state
        return prior

    has_binding = bool(
        result.get("physical_interface_id") or result.get("physical_port_id")
    )
    physical_state = (
        "not_applicable" if final_state == "disabled"
        else "passed" if has_binding and result.get("physical_verified") is not False
        else "failed"
    )
    network_applicable = role in {"plc", "robot"}
    usb_applicable = role == "thermal_uvc"
    network_path = result.get("network_path")
    network_state = (
        "not_applicable" if not network_applicable
        else "failed" if final_state == "network_path_invalid"
        else "passed" if isinstance(network_path, dict) and network_path.get("ok")
        else "not_reached"
    )
    usb_state = (
        "not_applicable" if not usb_applicable
        else "passed" if has_binding and result.get("physical_verified") is not False
        else "failed"
    )
    identity_state = (
        "failed" if final_state in {"device_profile_incomplete", "device_identity_mismatch"}
        else "not_reached" if final_state in {"network_path_invalid", "configuration_invalid"}
        else "passed" if final_state not in {"disabled", "no_channels"}
        else "not_applicable"
    )
    driver_state = (
        "failed" if final_state in {"driver_incompatible", "device_open_failed", "native_call_timeout"}
        else "not_reached" if final_state in {"device_profile_incomplete", "network_path_invalid", "configuration_invalid"}
        else "not_applicable" if final_state in {"disabled", "no_channels"}
        else "passed"
    )
    authentication_state = (
        "not_applicable" if role != "robot"
        else "failed" if final_state == "authentication_failed"
        else "not_reached" if final_state in {
            "network_path_invalid", "device_profile_incomplete",
            "driver_incompatible", "device_open_failed", "configuration_invalid",
        }
        else "passed"
    )
    if final_state in {"data_ready", "partial_channels"}:
        protocol_state = "passed"
    elif final_state in {"authentication_failed", "network_path_invalid", "driver_incompatible", "device_open_failed", "device_profile_incomplete", "configuration_invalid"}:
        protocol_state = "not_reached"
    elif final_state in {"disabled", "no_channels"}:
        protocol_state = "not_applicable"
    else:
        protocol_state = "failed"
    if final_state == "data_ready":
        channel_state = freshness_state = "passed"
    elif final_state in {"partial_channels", "invalid_data", "no_data", "no_valid_frame", "invalid_temperature_matrix", "stale"}:
        channel_state = "failed"
        freshness_state = "failed" if final_state in {"stale", "no_data", "no_valid_frame"} else "not_verified"
    elif final_state in {"disabled", "no_channels"}:
        channel_state = freshness_state = "not_applicable"
    else:
        channel_state = freshness_state = "not_reached"

    if result.get("driver") == THERMOCOUPLE64_DRIVER and final_state not in {"disabled", "no_channels", "configuration_invalid"}:
        protocol_state = "passed" if result.get("protocol_verified") else "failed"
        channel_state = "passed" if result.get("temperature_valid") else "failed"
        freshness_state = "passed" if result.get("temperature_valid") else "not_verified"

    result["stages"] = {
        "physical_binding": stage("physical_binding", physical_state),
        "device_identity": stage("device_identity", identity_state),
        "driver": stage("driver", driver_state),
        "network_path": stage(
            "network_path", network_state, evidence=deepcopy(network_path)
        ),
        "usb_binding": stage(
            "usb_binding",
            usb_state,
            physical_port_id=str(result.get("physical_port_id") or ""),
        ),
        "authentication": stage("authentication", authentication_state),
        "protocol": stage("protocol", protocol_state),
        "channels": stage("channels", channel_state),
        "freshness": stage("freshness", freshness_state),
        "stability": stage(
            "stability",
            "not_applicable" if final_state in {"disabled", "no_channels"} else "not_verified",
        ),
    }
    result["final_status"] = final_state
    return result


class _IndustrialHTTPAdapter(HTTPAdapter):
    def __init__(self, owner: Any) -> None:
        self.owner = owner
        super().__init__(pool_connections=1, pool_maxsize=1, max_retries=0)

    def init_poolmanager(self, connections, maxsize, block=False, **options):
        owner = self.owner
        if owner.source_address:
            options["source_address"] = (owner.source_address, 0)
        super().init_poolmanager(connections, maxsize, block=block, **options)
        pools = {}
        for scheme, pool_type in (
            ("http", urllib3.connectionpool.HTTPConnectionPool),
            ("https", urllib3.connectionpool.HTTPSConnectionPool),
        ):
            base_connection = pool_type.ConnectionCls

            class BoundConnection(base_connection):
                def connect(self) -> None:
                    self.source_address = (owner.source_address, 0) if owner.source_address else None
                    super().connect()
                    owner.actual_source_address = str(self.sock.getsockname()[0])
                    if owner.source_address and owner.actual_source_address != owner.source_address:
                        self.close()
                        raise RuntimeError("network_path_invalid: ABB connection source changed")

            pools[scheme] = type("IndustrialConnectionPool", (pool_type,), {"ConnectionCls": BoundConnection})
        self.poolmanager.pool_classes_by_scheme = pools


class AbbRobotDriver(SampleDriver):
    def __init__(self, endpoint: str = DEFAULT_ABB_IP,
                 username: str = DEFAULT_ABB_USER,
                 password: str = DEFAULT_ABB_PASSWORD,
                 timeout: float = 1.5,
                 clock: Any = time.time,
                 source_address: str = "") -> None:
        if str(endpoint).startswith("http"):
            self.base_url = str(endpoint).rstrip("/")
        else:
            self.base_url = f"http://{str(endpoint).split(':')[0]}"
        self.username = username
        self.password = password
        self.timeout = max(0.1, float(timeout))
        self.clock = clock
        self.source_address = str(source_address)
        self.actual_source_address = ""
        self._last_time: float | None = None
        self._last_x: float | None = None
        self._last_y: float | None = None
        self._last_z: float | None = None
        self.source_timestamp: float | None = None
        self.quality_metadata: dict[str, Any] = {
            "mechanical_unit": "ROB_1",
            "coordinate_system": "Base",
            "speed_quality": "unverified_first_sample",
        }
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.auth = HTTPDigestAuth(self.username, self.password)
        self._session.mount("http://", _IndustrialHTTPAdapter(self))
        self._session.mount("https://", _IndustrialHTTPAdapter(self))
        self._cookies = self._session.cookies

    def open(self) -> None:
        self._last_time = self._last_x = self._last_y = self._last_z = None
        self.source_timestamp = None
        self.quality_metadata["speed_quality"] = "unverified_first_sample"

    def close(self) -> None:
        try:
            if any(cookie.name == "ABBCX" for cookie in self._cookies):
                with self._session.get(f"{self.base_url}/logout", timeout=self.timeout) as response:
                    if not 200 <= response.status_code < 300:
                        raise AbbRwsError("session_close_failed", "session", f"status={response.status_code}")
        except requests.RequestException:
            raise AbbRwsError("session_close_failed", "session") from None
        finally:
            self._cookies.clear()
            self._session.close()

    def _request_json(self, path: str) -> dict[str, Any]:
        try:
            with self._session.get(f"{self.base_url}{path}", headers={"Accept": "application/json"}, timeout=self.timeout) as response:
                status = response.status_code
                raw = response.content.decode("utf-8")
            if status in {401, 403}:
                raise AbbRwsError("authentication_failed", "authentication") from None
            if status == 404:
                raise AbbRwsError("resource_not_found", "resource") from None
            if status == 503 and "Too many sessions" in raw:
                raise AbbRwsError("session_limit_exceeded", "session", "status=503") from None
            if not 200 <= status < 300:
                raise AbbRwsError("http_error", "http", f"status={status}") from None
        except requests.Timeout:
            raise AbbRwsError("transport_timeout", "transport") from None
        except requests.RequestException:
            raise AbbRwsError("transport_error", "transport") from None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise AbbRwsError("invalid_json", "parsing") from None
        if not isinstance(payload, dict):
            raise AbbRwsError("invalid_json", "parsing")
        return payload

    def _fetch_position(self) -> tuple[float, float, float]:
        payload = self._request_json(ABB_ROBTARGET_PATH)
        try:
            state = payload["_embedded"]["_state"][0]
            values = (float(state["x"]), float(state["y"]), float(state["z"]))
        except (KeyError, IndexError, TypeError, ValueError):
            raise AbbRwsError("resource_invalid", "parsing") from None
        if not all(math.isfinite(value) for value in values):
            raise AbbRwsError("numeric_invalid", "value")
        return values

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
        payload = self._request_json(
            f"{ABB_RAPID_SYMBOL_BASE_PATH}/{path}?json=1"
        )
        value = self._find_json_value(payload)
        if value is None:
            raise ValueError(f"ABB变量{variable}响应中没有value字段")
        return str(value)

    def read_sample(self) -> dict[str, float] | None:
        x, y, z = self._fetch_position()
        if not all(math.isfinite(value) for value in (x, y, z)):
            raise AbbRwsError("numeric_invalid", "value")
        now = float(self.clock())
        self.source_timestamp = now
        result = {"ABB_X": x, "ABB_Y": y, "ABB_Z": z}
        if self._last_time is not None and self._last_x is not None:
            delta_time = now - self._last_time
            if delta_time > 0:
                speed = round(math.sqrt(
                    (x - self._last_x) ** 2
                    + (y - self._last_y) ** 2
                    + (z - self._last_z) ** 2
                ) / delta_time, 2)
                result["线速度"] = speed
                self.quality_metadata["speed_quality"] = "measured_new"
            else:
                self.quality_metadata["speed_quality"] = "unverified_zero_delta"
        else:
            self.quality_metadata["speed_quality"] = "unverified_first_sample"
        self._last_time, self._last_x, self._last_y, self._last_z = now, x, y, z
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


def uvc_source_observation_readiness(
    observations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    distinct: dict[int, float] = {}
    for item in observations:
        if not isinstance(item, dict):
            continue
        try:
            sequence = int(item["source_sequence"])
            observed = float(item["received_monotonic"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(observed):
            distinct.setdefault(sequence, observed)
    times = sorted(distinct.values())
    span = max(0.0, times[-1] - times[0]) if len(times) > 1 else 0.0
    return {
        "ready": len(distinct) >= 3 and span >= 1.0,
        "distinct_source_frames": len(distinct),
        "observation_span_seconds": span,
    }


class UvcDriverError(RuntimeError):
    def __init__(
        self,
        code: str,
        stage: str,
        *,
        return_code: int | None = None,
        last_error: int | None = None,
    ) -> None:
        self.code = str(code)
        self.stage = str(stage)
        self.return_code = return_code
        self.last_error = last_error
        details = [f"stage={self.stage}"]
        if return_code is not None:
            details.append(f"code={return_code}")
        if last_error is not None:
            details.append(f"lastError={last_error}")
        super().__init__(f"{self.code}: " + ", ".join(details))


class UvcThermalDriver(SampleDriver):
    """BSV UVC native DLL reader; produces a calibrated ROI temperature."""

    def __init__(
        self,
        dll_path: str = "",
        roi: Any = None,
        poll_retries: int = 3,
        call_timeout_seconds: float = 1.0,
    ) -> None:
        self.dll_path = str(dll_path or "").strip()
        self.roi = _parse_roi(roi)
        self.poll_retries = max(1, int(poll_retries))
        self.call_timeout_seconds = max(0.01, float(call_timeout_seconds))
        self.dll = None
        self._started = False
        self._native_lock = threading.Lock()
        self._native_timeout_stage = ""
        self._connection_generation = 0
        self._last_frame_counter: int | None = None
        self.source_timestamp: float | None = None
        self.quality_metadata: dict[str, Any] = {
            "driver": "uvc_thermal",
            "frame_kind": "temperature_matrix",
        }
        self.yuv_buffer = (ctypes.c_ubyte * BSV_UVC_YUV_BYTES)()
        self.temp_buffer = (ctypes.c_ushort * BSV_UVC_PIXELS)()

    @property
    def native_call_pending(self) -> bool:
        return bool(self._native_timeout_stage)

    def _native_call(self, stage: str, function: Any, *args: Any) -> Any:
        if self._native_timeout_stage:
            raise UvcDriverError("native_call_timeout", self._native_timeout_stage)
        if not self._native_lock.acquire(timeout=self.call_timeout_seconds):
            self._native_timeout_stage = stage
            raise UvcDriverError("native_call_timeout", stage)
        completed = threading.Event()
        outcome: dict[str, Any] = {}

        def invoke() -> None:
            try:
                outcome["value"] = function(*args)
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                self._native_lock.release()
                completed.set()

        thread = threading.Thread(
            target=invoke,
            name=f"AFP-BSV-UVC-{stage}",
            daemon=True,
        )
        thread.start()
        if not completed.wait(self.call_timeout_seconds):
            self._native_timeout_stage = stage
            raise UvcDriverError("native_call_timeout", stage)
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value")

    def _safe_last_error(self) -> int | None:
        if self.dll is None:
            return None
        try:
            return int(self._native_call("last_error", self.dll.BsvGetLastError))
        except Exception:
            return None

    def _find_dll(self) -> Path:
        candidates = []
        if self.dll_path:
            candidates.append(Path(self.dll_path))
        candidates.extend([
            _NATIVE_DLL_DIR / BSV_UVC_DLL_NAME,
            RELEASE_CONTRACT.source_path.parent / "native_dll" / BSV_UVC_DLL_NAME,
            APP_DIR / "hardware_dlls" / BSV_UVC_DLL_NAME,
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
        self.dll.BsvGetFrameCounter.argtypes = []
        self.dll.BsvGetFrameCounter.restype = ctypes.c_uint32
        self.dll.BsvGetLastError.argtypes = []
        self.dll.BsvGetLastError.restype = ctypes.c_int
        self.dll.BsvStop.argtypes = []
        self.dll.BsvStop.restype = None
        self.dll.BsvClose.argtypes = []
        self.dll.BsvClose.restype = None

    def open(self) -> None:
        try:
            dll_path = self._find_dll()
            self.dll = self._native_call("load", ctypes.CDLL, str(dll_path))
            self._bind_api()
        except UvcDriverError:
            raise
        except Exception:
            self.dll = None
            raise UvcDriverError("driver_incompatible", "load") from None
        try:
            range_result = int(self._native_call(
                "set_range",
                self.dll.BsvSetTempRangeCode,
                BSV_UVC_TEMP_RANGE_CODE,
            ))
            if range_result < 0:
                raise UvcDriverError(
                    "driver_incompatible",
                    "set_range",
                    return_code=range_result,
                    last_error=self._safe_last_error(),
                )
            result = int(self._native_call("open", self.dll.BsvOpen))
            if result < 0:
                raise UvcDriverError(
                    "device_open_failed",
                    "open",
                    return_code=result,
                    last_error=self._safe_last_error(),
                )
            result = int(self._native_call("start", self.dll.BsvStart))
            if result < 0:
                raise UvcDriverError(
                    "device_open_failed",
                    "start",
                    return_code=result,
                    last_error=self._safe_last_error(),
                )
            self._started = True
            self._connection_generation += 1
            self._last_frame_counter = None
            self.quality_metadata = {
                "driver": "uvc_thermal",
                "frame_kind": "temperature_matrix",
                "connection_generation": self._connection_generation,
            }
        except Exception:
            self._release_native(raise_errors=False)
            raise

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
        last_result: int | None = None
        counter_copy_retries = 0
        for _ in range(self.poll_retries):
            has_frame = int(self._native_call("has_frame", self.dll.BsvHasFrame))
            if has_frame <= 0:
                last_result = has_frame
                continue
            frame_counter_before = int(self._native_call(
                "frame_counter", self.dll.BsvGetFrameCounter
            ))
            if (
                self._last_frame_counter is not None
                and frame_counter_before == self._last_frame_counter
            ):
                self.quality_metadata.update({
                    "source_frame_counter": frame_counter_before,
                    "repeated_frame": True,
                    "counter_copy_retries": counter_copy_retries,
                })
                return None
            result = int(self._native_call(
                "get_frame",
                self.dll.BsvGetFrame,
                self.yuv_buffer,
                BSV_UVC_YUV_BYTES,
                self.temp_buffer,
                BSV_UVC_PIXELS,
            ))
            last_result = result
            if result > 0:
                frame_counter_after = int(self._native_call(
                    "frame_counter", self.dll.BsvGetFrameCounter
                ))
                if frame_counter_after != frame_counter_before:
                    counter_copy_retries += 1
                    continue
                temperature = self._roi_average_temperature()
                if not math.isfinite(temperature):
                    raise UvcDriverError(
                        "invalid_temperature_matrix", "validate_frame"
                    )
                self._last_frame_counter = frame_counter_after
                self.source_timestamp = time.time()
                self.quality_metadata = {
                    "driver": "uvc_thermal",
                    "frame_kind": "temperature_matrix",
                    "width": BSV_UVC_WIDTH,
                    "height": BSV_UVC_HEIGHT,
                    "source_frame_counter": frame_counter_after,
                    "connection_generation": self._connection_generation,
                    "repeated_frame": False,
                    "counter_copy_retries": counter_copy_retries,
                }
                return {"ROI平均温度": temperature}
        if counter_copy_retries:
            raise UvcDriverError(
                "frame_counter_unstable",
                "get_frame",
                return_code=last_result,
                last_error=self._safe_last_error(),
            )
        raise UvcDriverError(
            "no_valid_frame",
            "get_frame" if last_result not in (None, 0) else "has_frame",
            return_code=last_result,
            last_error=self._safe_last_error(),
        )

    def _release_native(self, *, raise_errors: bool) -> None:
        dll = self.dll
        was_started = self._started
        self.dll = None
        self._started = False
        if dll is None:
            return
        errors: list[Exception] = []
        if was_started:
            try:
                self._native_call("stop", dll.BsvStop)
            except Exception as exc:
                errors.append(exc)
        try:
            self._native_call("close", dll.BsvClose)
        except Exception as exc:
            errors.append(exc)
        if raise_errors and errors:
            raise errors[0]

    def close(self) -> None:
        self._release_native(raise_errors=True)


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


class MultiInterfaceDriver(SampleDriver):
    """Run physical interfaces independently and expose their latest samples."""
    def __init__(self, configs: list[dict[str, Any]], selected_sensors: list[str], source_file: str = "", assignments: dict[str, list[str]] | None = None, usb_startup_profiles: dict | None = None) -> None:
        self.configs = [item for item in configs if item.get("enabled", True)]
        self.selected_sensors = selected_sensors
        self.source_file = source_file
        self.assignments = assignments or {}
        self.usb_startup_profiles = dict(usb_startup_profiles or {})
        self.drivers: list[SampleDriver] = []
        self.cache = ChannelSampleCache(max_events=max(128, len(selected_sensors) * 64))
        self.workers: list[LocalDirectAdapter] = []
        self.final_worker_status: list[dict[str, Any]] = []
        self.interface_quality_metadata: dict[str, dict[str, Any]] = {}
        self._opening_cancel = threading.Event()
        self._workers_lock = threading.RLock()

    def cancel_open(self) -> None:
        self._opening_cancel.set()
        for worker in self.workers:
            worker.stop_event.set()
            cancel = getattr(worker.driver, "cancel_open", None)
            if callable(cancel):
                cancel()

    def open(self) -> None:
        self.drivers = []
        self.final_worker_status = []
        self.interface_quality_metadata = {}
        opening_started = time.monotonic()
        try:
            for item in self.configs:
                if self._opening_cancel.is_set():
                    raise RuntimeError("capture_start_cancelled")
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
                        str(item.get("dll_path") or ""),
                        item.get("roi"),
                        int(item.get("poll_retries") or 3),
                        float(item.get("native_call_timeout_seconds") or 1.0),
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
                        str(item.get("endpoint") or ""), int(item.get("baudrate") or M3232_BAUDRATE),
                        **{k: item[k] for k in ("matrix_rows", "matrix_cols", "protocol_profile", "data_mode", "device_identity",
                            "calibration_profile", "first_frame_timeout_seconds", "freshness_seconds", "frame_timeout_seconds", "max_buffer_bytes") if k in item},
                    )
                    if item.get("_raw_capture_root"):
                        driver.recorder = MatrixRecorder(item["_raw_capture_root"], {
                            "interface_id": item.get("id"), "configuration": {k:v for k,v in item.items() if not k.startswith("_")},
                            **dict(item.get("_matrix_csv_context") or {}),
                        }, csv_path=item.get("_matrix_csv_work_path"))
                elif driver_name == THERMOCOUPLE64_DRIVER:
                    driver = HpThermocouple64Driver(
                        str(item.get("endpoint") or ""),
                        int(item.get("baudrate") or 115200),
                        int(item.get("slave_id", 1)),
                        float(item.get("timeout") or 0.5),
                        read_profile=item.get("read_profile", "channel_temperature"),
                        channel_settings=item.get("channel_settings"),
                        device_identity=item.get("device_identity"),
                        scan_timeout_seconds=item.get("scan_timeout_seconds"),
                    )
                elif driver_name == USB_DRIVER:
                    recorder = None
                    if item.get("_raw_capture_root"):
                        recorder = RawCaptureWriter(item["_raw_capture_root"], {
                            "interface_id": item.get("id"), "endpoint": item.get("endpoint"),
                            "device_binding": item.get("device_identity") or {},
                            "data_mode": item.get("data_mode"), "bridge": item.get("bridge"),
                            "calibration_digest": item.get("calibration_digest"),
                            "measurement_context": deepcopy((item.get("channel_settings") or {}).get("esr_context", {})),
                        })
                    driver = Esr1001UsbDriver(
                        str(item.get("endpoint") or ""), data_mode=item.get("data_mode", "raw_adc"),
                        calibration=item.get("calibration_profile"), binding=item.get("device_identity"),
                        bridge=str(item.get("bridge") or ""), recorder=recorder,
                        measurement_context=(item.get("channel_settings") or {}).get("esr_context"),
                        startup_policy=self.usb_startup_profiles.get(str(item.get("id") or ""), "fixed"),
                    )
                elif driver_name == "modbus_rtu_esr1001":
                    driver = Esr1001StrainDriver(
                        str(item.get("endpoint") or ""),
                        int(item.get("baudrate") or ESR1001_BAUDRATE),
                        int(item.get("slave_id") or ESR1001_DEFAULT_SLAVE_ID),
                        float(item.get("timeout") or ESR1001_RESPONSE_TIMEOUT_SECONDS),
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
                requested_poll = float(item.get("poll_interval_seconds") or 0.002)
                if driver_name == USB_DRIVER:
                    requested_poll = min(requested_poll, 0.002)
                before_open = None
                if driver_name in {"modbus_tcp", "abb_robot"}:
                    network_interface = deepcopy(item)
                    def before_open(value=network_interface, target=driver):
                        path = require_network_path(value)
                        target.source_address = path["actual_source_address"]
                worker = LocalDirectAdapter(
                    interface_id,
                    driver,
                    allowed,
                    self.cache,
                    poll_interval_seconds=requested_poll,
                    minimum_poll_seconds=minimum_poll_interval(driver_name),
                    reconnect_backoff_seconds=float(
                        item.get("reconnect_backoff_seconds") or 0.05
                    ),
                    max_reconnect_backoff_seconds=float(
                        item.get("max_reconnect_backoff_seconds") or 1.0
                    ),
                    max_reconnect_attempts=int(item.get("max_reconnect_attempts") or 5),
                    reconnect_window_seconds=item.get("reconnect_window_seconds"),
                    before_open=before_open,
                )
                if driver_name == THERMOCOUPLE64_DRIVER:
                    driver.poll_interval_seconds = worker.actual_poll_interval_seconds
                with self._workers_lock:
                    if self._opening_cancel.is_set():
                        recorder = getattr(driver, "recorder", None)
                        if recorder is not None:
                            recorder.close(driver.quality_metadata)
                        raise RuntimeError("capture_start_cancelled")
                    self.workers.append(worker)
                    worker.start()
            open_timeout = max((max(float(getattr(worker.driver, "open_timeout_seconds", 2.5)),
                                    10.0 if worker.before_open is not None and sys.platform.startswith("win") else 0.0)
                                for worker in self.workers), default=2.5)
            open_deadline = opening_started + open_timeout
            for worker in self.workers:
                while (
                    not worker.opened.is_set()
                    and worker.thread is not None
                    and worker.thread.is_alive()
                    and time.monotonic() < open_deadline
                ):
                    if self._opening_cancel.is_set():
                        raise RuntimeError("capture_start_cancelled")
                    worker.opened.wait(0.02)
                if not worker.opened.is_set():
                    if worker.last_error:
                        raise RuntimeError(
                            f"接口{worker.interface_id}打开失败：{worker.last_error}"
                        )
                    raise TimeoutError(f"接口{worker.interface_id}在打开期限内未完成打开")
                if worker.state == "failed" or (
                    worker.thread is not None and not worker.thread.is_alive()
                ):
                    raise RuntimeError(
                        f"接口{worker.interface_id}打开失败：{worker.last_error}"
                    )
        except Exception:
            self.close()
            raise

    def read_sample(self) -> dict[str, float] | None:
        merged = {
            name: sample.value
            for name, sample in self.cache.snapshot().items()
            if name in self.selected_sensors
        }
        return merged or None

    def worker_status(self) -> list[dict[str, Any]]:
        return [worker.status() for worker in self.workers]

    def close(self, timeout_seconds: float = 0.5) -> None:
        pending: list[LocalDirectAdapter] = []
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        self._opening_cancel.set()
        with self._workers_lock:
            workers = list(self.workers)
        if not workers:
            return
        for worker in workers:
            worker.stop(timeout_seconds=0)
        for worker in workers:
            if not worker.stop(timeout_seconds=max(0.0, deadline-time.monotonic())):
                pending.append(worker)
        statuses = {row["interface_id"]: row for row in self.final_worker_status}
        statuses.update({worker.interface_id: worker.status() for worker in workers})
        self.final_worker_status = list(statuses.values())
        self.interface_quality_metadata.update({
            worker.interface_id: dict(metadata)
            for worker in workers
            for metadata in [getattr(worker.driver, "quality_metadata", {})]
            if isinstance(metadata, dict)
        })
        for worker in workers:
            recorder = getattr(worker.driver, "recorder", None)
            if recorder is not None and worker not in pending:
                recorder.close(worker.driver.quality_metadata)
                self.interface_quality_metadata.setdefault(worker.interface_id, {}).update({
                    "raw_file": str(recorder.path), "raw_metadata_file": str(recorder.metadata_path),
                    "saved_source_rows": recorder.rows, "writer_error": recorder.error,
                    **({"matrix_csv_file": str(recorder.csv_path).removesuffix(".partial"), "saved_matrix_csv_rows": recorder.csv_rows}
                       if isinstance(recorder, MatrixRecorder) and recorder.csv_path else {}),
                })
        self.workers = pending
        if not pending:
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
            usb_startup_profiles=getattr(config, "_session_usb_profiles", {}),
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
            config.endpoint,
            config.baudrate or M3232_BAUDRATE,
            config.matrix_rows,
            config.matrix_cols,
            data_mode=config.data_mode, device_identity=config.device_identity, calibration_profile=config.calibration_profile,
        )
    if config.driver == THERMOCOUPLE64_DRIVER:
        return HpThermocouple64Driver(
            config.endpoint,
            config.baudrate or 115200,
            config.slave_id,
            getattr(config, "timeout", 0.5),
            read_profile=getattr(config, "read_profile", "channel_temperature"),
            channel_settings=getattr(config, "channel_settings", None),
        )
    if config.driver == "modbus_rtu_esr1001":
        return Esr1001StrainDriver(
            config.endpoint,
            config.baudrate or ESR1001_BAUDRATE,
            ESR1001_DEFAULT_SLAVE_ID,
        )
    if config.driver == USB_DRIVER:
        return Esr1001UsbDriver(config.endpoint)
    raise ValueError(f"不支持的采集驱动：{config.driver}")


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


from acquisition_start import AcquisitionStartMixin


class AcquisitionManager(AcquisitionStartMixin):
    def __init__(
        self,
        capture_root: Path = DEFAULT_CAPTURE_ROOT,
        *,
        recovery_scan: bool | None = None,
    ) -> None:
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
        self.frame_sources: deque[dict[str, Any]] = deque(maxlen=2400)
        self.frame_envelopes: deque[dict[str, Any]] = deque(maxlen=200000)
        self.frame_catalog: dict[str, Any] | None = None
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
        self.quality_sidecar_path: Path | None = None
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
        self.capture_phase = "idle"
        self.persistence_failed = False
        self.persistence_queue_depth = 0
        self.persistence_queue_high_water_rows = 0
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
        self._readiness_snapshot: ReadinessSnapshot | None = None
        self._init_start_lifecycle()
        self._last_start_preflight: dict[str, Any] = {
            "clock_domain": "server_monotonic",
            "readiness_session_id": self.readiness_session_id,
            "readiness_session_reused": False,
            "complete": False,
            "elapsed_ms": 0.0,
            "events": [],
        }
        self._hardware_check_runs_lock = threading.Lock()
        self._hardware_check_runs: dict[tuple[str, float], dict[str, Any]] = {}
        if recovery_scan is None:
            recovery_scan = Path(capture_root).resolve() != DEFAULT_CAPTURE_ROOT.resolve()
        self.recovery_state = (
            scan_persistence_recovery(self.capture_root)
            if recovery_scan
            else {
                "blocked": False,
                "database_pending_retry": 0,
                "incomplete_files": 0,
                "unreadable_pending": 0,
                "missing_retry_sources": 0,
                "relocated_retry_sources": 0,
                "unsafe_files": 0,
                "capture_uuids": [],
                "files": [],
                "scan_fingerprint": hashlib.sha256(b"[]").hexdigest(),
                "scan_deferred": True,
            }
        )
        if self.recovery_state["blocked"]:
            self.finalization_complete = False
            if self.recovery_state["database_pending_retry"]:
                self.capture_phase = "database_pending_retry"
                self.mysql_status = {
                    "enabled": True,
                    "ok": False,
                    "state": "pending",
                    "saved_rows": 0,
                    "recovery": dict(self.recovery_state),
                }
            else:
                self.capture_phase = "finalization_failed"
                self.last_error = "restart_recovery_incomplete_files"

    def _apply_recovery_state_locked(self, recovery: dict[str, Any]) -> None:
        self.recovery_state = dict(recovery)
        if recovery.get("blocked"):
            self.finalization_complete = False
            if int(recovery.get("database_pending_retry") or 0) > 0:
                self.capture_phase = "database_pending_retry"
                self.mysql_status = {
                    **self.mysql_status,
                    "enabled": True,
                    "ok": False,
                    "state": "pending",
                    "recovery": dict(recovery),
                }
            else:
                self.capture_phase = "finalization_failed"
                self.last_error = "restart_recovery_incomplete_files"
            return
        if self.capture_phase in {"database_pending_retry", "finalization_failed"}:
            self.finalization_complete = True
            self.capture_phase = "completed" if self.capture_uuid else "idle"
            if self.last_error == "restart_recovery_incomplete_files":
                self.last_error = ""

    def refresh_persistence_recovery(self) -> dict[str, Any]:
        """Rescan durable blockers and publish the result without a restart."""

        with self.lifecycle_lock:
            recovery = scan_persistence_recovery(self.capture_root)
            with self.lock:
                self._apply_recovery_state_locked(recovery)
                return dict(self.recovery_state)

    def discard_persistence_recovery(
        self, request: dict[str, Any], *, actor: str
    ) -> dict[str, Any]:
        """Move explicitly confirmed blockers into an audited recovery area."""

        if str(actor) != "local_admin":
            raise PermissionError("local_admin_required")
        if request.get("confirm_discard") is not True:
            raise ValueError("confirm_discard_required")
        if str(request.get("confirmation") or "") != "重置并丢弃":
            raise ValueError("confirmation_phrase_mismatch")
        requested_ids = request.get("item_ids")
        if (
            not isinstance(requested_ids, list)
            or not requested_ids
            or any(not isinstance(value, str) or not value for value in requested_ids)
            or len(set(requested_ids)) != len(requested_ids)
            or len(requested_ids) > 1000
        ):
            raise ValueError("exact_item_ids_required")
        expected_fingerprint = str(request.get("scan_fingerprint") or "")
        if not expected_fingerprint:
            raise ValueError("scan_fingerprint_required")

        with self.lifecycle_lock:
            with self.lock:
                active = bool(self.thread is not None and self.thread.is_alive())
                active = active or self.capture_phase in {
                    "starting",
                    "running",
                    "stopping_sampling",
                    "finalizing",
                }
            if active:
                raise RuntimeError("active_capture_recovery_forbidden")

            capture_root, candidates = _recovery_candidates(self.capture_root)
            current = scan_persistence_recovery(capture_root)
            if current["scan_fingerprint"] != expected_fingerprint:
                raise ValueError("recovery_scan_changed")
            by_id = {item["id"]: (path, item) for path, item in candidates}
            selected: list[tuple[Path, dict[str, Any]]] = []
            for item_id in requested_ids:
                candidate = by_id.get(item_id)
                if candidate is None:
                    raise ValueError("recovery_item_not_found")
                path, item = candidate
                if not item["eligible"]:
                    raise ValueError(f"recovery_item_unsafe:{item['reason']}")
                selected.append((path, item))

            operation_id = uuid.uuid4().hex
            discard_root = capture_root / ".afp-recovery-discarded" / operation_id
            moved: list[tuple[Path, Path]] = []
            audit = {
                "schema_version": 1,
                "operation_id": operation_id,
                "action": "discard_recovery_blockers",
                "actor": "local_admin",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "scan_fingerprint": expected_fingerprint,
                "items": [
                    {
                        key: item[key]
                        for key in ("id", "kind", "name", "path_sha256", "size_bytes")
                    }
                    for _path, item in selected
                ],
            }
            try:
                for source, _item in selected:
                    relative = source.relative_to(capture_root)
                    destination = discard_root / relative
                    destination = destination.with_name(destination.name + ".discarded")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, destination)
                    moved.append((source, destination))
                _atomic_write_json(
                    capture_root / ".afp-recovery-audit" / f"{operation_id}.json",
                    audit,
                )
            except Exception:
                for source, destination in reversed(moved):
                    if destination.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(destination, source)
                raise
            recovery = scan_persistence_recovery(capture_root)
            with self.lock:
                self._apply_recovery_state_locked(recovery)
                if any(item["kind"] == "mysql_pending" for _path, item in selected):
                    self.mysql_status = {
                        **self.mysql_status,
                        "enabled": True,
                        "ok": False,
                        "state": "discarded",
                    }
            return {
                "ok": True,
                "operation_id": operation_id,
                "processed_count": len(selected),
                "items": audit["items"],
                "recovery": recovery,
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
        for item in payload.get("interfaces") or []:
            for key in list(item):
                if key.startswith("_"):
                    item.pop(key, None)
            if item.get("role") == "robot":
                item["credential_identifier"] = _credential_identifier(
                    str(item.get("username") or DEFAULT_ABB_USER),
                    str(item.get("password") or DEFAULT_ABB_PASSWORD),
                )
            item.pop("username", None)
            item.pop("password", None)
        return payload

    @staticmethod
    def available_drivers() -> list[dict[str, str]]:
        return [
            {"id": "simulator", "label": "模拟采集（CSV数据源）"},
            {"id": "serial_json", "label": "串口 JSON Lines"},
            {"id": "smrf_hid", "label": "SMRF八通道温度巡检仪（USB HID）"},
            {"id": THERMOCOUPLE64_DRIVER, "label": "慧谱TCP64热电偶（64路 Modbus RTU）"},
            {"id": "tcp_json", "label": "TCP JSON Lines"},
            {"id": "modbus_tcp", "label": "Modbus TCP（松下PLC）"},
            {"id": "abb_robot", "label": "ABB机器人 RWS"},
            {"id": "uvc_thermal", "label": "BSV UVC热像仪（ROI温度）"},
            {"id": "rtsp_thermal", "label": "IP热像仪 RTSP"},
            {"id": "m3232_pressure", "label": "M3232薄膜压力（230400/8N1，32×32，原始未标定）"},
            {"id": "modbus_rtu_esr1001", "label": "ESR1001-A应变（Modbus RTU 9600-8-N-1）"},
            {"id": USB_DRIVER, "label": "ESR1001-A原生USB/TTL（115200-8-N-1）"},
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
                "schema_version": 2, "state": "unavailable",
                "provider": "module_unavailable",
                "errors": ["USB physical port topology module is unavailable"],
                "docks": [], "hubs": [], "usb_ports": [], "devices": [],
            }
        else:
            try:
                usb_topology = discover_windows_usb_topology()
            except Exception as exc:
                usb_topology = {
                    "schema_version": 2, "state": "unavailable",
                    "provider": "windows_usb_hub_ioctl",
                    "errors": [f"USB topology discovery failed: {exc}"],
                    "docks": [], "hubs": [], "usb_ports": [], "devices": [],
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
                    "esr1001_usb_candidate": item.vid == 0x1A86 and item.pid in {0x7523, 0x5523},
                    "identity_confirmed": False,
                    "device_identity": {"vid": item.vid, "pid": item.pid, "serial_number": item.serial_number, "location": getattr(item, "location", None), "hwid": getattr(item, "hwid", None)},
                    "serial": str(item.serial_number or ""),
                    "hwid": str(getattr(item, "hwid", "") or ""),
                    "location": str(getattr(item, "location", "") or ""),
                    "detected": True,
                    "identity_confirmed": False,
                    "compatible_roles": ["thermocouple64", "pressure", "esr1001_strain", "custom"],
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
                                "detected": True, "identity_confirmed": False,
                                "compatible_roles": ["thermocouple64", "pressure", "esr1001_strain", "custom"],
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
                RELEASE_CONTRACT.source_path.parent / "native_dll" / BSV_UVC_DLL_NAME,
                APP_DIR / "hardware_dlls" / BSV_UVC_DLL_NAME,
                APP_DIR / BSV_UVC_DLL_NAME,
                APP_DIR.parent / BSV_UVC_DLL_NAME,
                Path.cwd() / BSV_UVC_DLL_NAME,
            )
        )
        camera_devices = _windows_camera_pnp_facts()
        _merge_camera_pnp_with_usb_topology(usb_topology, camera_devices)
        bsv_pnp = next(
            (
                item for item in camera_devices
                if item.get("vid") == BSV_UVC_VENDOR_ID
                and item.get("pid") == BSV_UVC_PRODUCT_ID
            ),
            {},
        )
        bsv_devices = [
            item for item in usb_topology.get("devices", [])
            if isinstance(item, dict)
            and item.get("vid") == BSV_UVC_VENDOR_ID
            and item.get("pid") == BSV_UVC_PRODUCT_ID
        ]
        if not bsv_devices and bsv_pnp:
            bsv_devices = [{
                "id": str(bsv_pnp.get("usb_device_id") or "usbdev:bsv-unlocated"),
                "parent_port_id": str(bsv_pnp.get("parent_port_id") or ""),
                "vid": BSV_UVC_VENDOR_ID,
                "pid": BSV_UVC_PRODUCT_ID,
            }]
        for device in bsv_devices:
            parent_port_id = str(device.get("parent_port_id") or "")
            pnp = bsv_pnp if bsv_pnp else device
            physical_interfaces.append({
                "id": f"uvc:bsv:{str(device.get('id') or parent_port_id or 'device')}",
                "kind": "usb_uvc", "protocol": "uvc",
                "endpoint": str(pnp.get("instance_id") or device.get("id") or "BSV UVC"),
                "label": "BSV UVC热像仪（设备已发现，待温度数据验证）",
                "detected": True,
                "device_detected": True,
                "topology_bound": bool(parent_port_id),
                "driver_available": uvc_dll_found,
                "auto_assignable": True,
                "vid": BSV_UVC_VENDOR_ID,
                "pid": BSV_UVC_PRODUCT_ID,
                "serial": str(device.get("serial") or pnp.get("serial") or ""),
                "pnp_instance_id": str(pnp.get("instance_id") or ""),
                "container_id": str(pnp.get("container_id") or ""),
                "location_paths": list(pnp.get("location_paths") or []),
                "parent_port_id": parent_port_id,
                "service": str(pnp.get("service") or ""),
                "driver_provider": str(pnp.get("driver_provider") or ""),
                "driver_version": str(pnp.get("driver_version") or ""),
                "pnp_status": str(pnp.get("pnp_status") or ""),
                "compatible_roles": ["thermal_uvc"],
            })
        if uvc_dll_found and not bsv_devices:
            # DLL presence is a driver capability fact, not device presence.
            physical_interfaces.append({
                "id": "uvc:bsv",
                "kind": "usb_uvc", "protocol": "uvc",
                "endpoint": "BSV UVC",
                "label": "BSV UVC驱动可用（未发现目标设备）",
                "detected": False,
                "device_detected": False,
                "topology_bound": False,
                "driver_available": True,
                "auto_assignable": True,
                "vid": BSV_UVC_VENDOR_ID,
                "pid": BSV_UVC_PRODUCT_ID,
                "compatible_roles": ["thermal_uvc"],
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
            network_targets = {
                "plc": DEFAULT_PLC_IP,
                "robot": DEFAULT_ABB_IP,
            }
            route_sources = {
                "plc": _route_source_address(DEFAULT_PLC_IP, DEFAULT_PLC_PORT),
                "robot": _route_source_address(DEFAULT_ABB_IP, 80),
            }
            for adapter in _network_adapter_snapshot():
                name = str(adapter.get("name") or "")
                ipv4_bindings = list(adapter.get("ipv4_bindings") or [])
                ipv4 = [item["address"] for item in ipv4_bindings]
                route_facts = network_interface_route_facts(
                    name,
                    str(adapter.get("description") or ""),
                    ipv4_bindings,
                    network_targets,
                    route_sources,
                )
                physical_interfaces.append({
                    "id": str(adapter.get("id") or f"ethernet:{name}"), "kind": "ethernet",
                    "protocol": "ethernet", "physical_transport_family": "ethernet",
                    "endpoint": name,
                    "label": (
                        f"网卡 {name}（{', '.join(ipv4)}）"
                        if ipv4 else f"网卡 {name}（当前无 IPv4 地址）"
                    ),
                    "name": name,
                    "description": str(adapter.get("description") or ""),
                    "addresses": ipv4, "detected": True,
                    "shared_roles": ["plc", "robot"],
                    **route_facts,
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
                "endpoint": "", "label": "串口（待识别，启动后检查）",
                "detected": False, "auto_assignable": True,
                "identity_confirmed": False,
                "compatible_roles": ["thermocouple64", "pressure", "esr1001_strain", "custom"],
            })
        rtsp_reachable = False
        try:
            sock = socket.create_connection(("192.168.125.2", 554), timeout=1.0)
            sock.close()
            rtsp_reachable = True
        except OSError:
            pass
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
            "plc_reachable": plc_reachable,
            "abb_reachable": abb_reachable,
            "uvc_dll_found": uvc_dll_found,
            "camera_devices": [
                {
                    **item,
                    "compatible_with_bsv_temperature": bool(
                        item.get("vid") == BSV_UVC_VENDOR_ID
                        and item.get("pid") == BSV_UVC_PRODUCT_ID
                    ),
                }
                for item in camera_devices
            ],
            "rtsp_reachable": rtsp_reachable,
            "thermocouple_reachable": False,
            "thermocouple_discovered": any(p.get("kind") == "serial" and p.get("detected") for p in physical_interfaces),
            "thermocouple_protocol_state": "not_verified",
            "thermocouple_temperature_state": "not_verified",
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
        self, config: AcquisitionConfig, timeout_seconds: float = 2.5, *, force: bool = False
    ) -> dict:
        """Run one real probe per fingerprint and serialize physical access."""
        if getattr(config, "acquisition_mode", "real") != "real":
            return self._test_connection_once(config, timeout_seconds)
        key = (readiness_config_fingerprint(config), float(timeout_seconds), bool(force))
        with self._hardware_check_runs_lock:
            run = self._hardware_check_runs.get(key)
            owner = run is None
            if owner:
                run = {"event": threading.Event(), "result": None, "error": None}
                self._hardware_check_runs[key] = run
        if not owner:
            run["event"].wait()
            if run["error"] is not None:
                raise run["error"]
            return deepcopy(run["result"])
        try:
            with self.lifecycle_lock:
                from acquisition_readiness import check_interfaces
                if self.thread is not None and self.thread.is_alive() and self.config.acquisition_mode == "real":
                    raise RuntimeError("port_busy: 已有真实采集会话，不能启动临时探针")
                if force:
                    self.invalidate_readiness()
                result = check_interfaces(self, config, timeout_seconds)
            run["result"] = deepcopy(result)
            return result
        except BaseException as error:
            run["error"] = error
            raise
        finally:
            run["event"].set()
            with self._hardware_check_runs_lock:
                if self._hardware_check_runs.get(key) is run:
                    self._hardware_check_runs.pop(key, None)

    def _test_connection_once(
        self, config: AcquisitionConfig, timeout_seconds: float = 2.5
    ) -> dict:
        """Check source readiness and report the exact missing endpoint/channel."""
        config._session_usb_profiles = {}
        with self.lock:
            if self.thread is not None and self.thread.is_alive() and self.config and self.config.acquisition_mode == "real":
                raise RuntimeError("port_busy: 已有真实采集会话，不能启动临时探针")
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
            physical_port_id = str(item.get("physical_port_id") or "")
            physical_kind = str(item.get("physical_interface_kind") or profile.get("physical_kind") or "")
            protocol = USB_DRIVER if item.get("driver") == USB_DRIVER else str(profile.get("protocol") or item.get("driver") or "")
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
            network_path: dict[str, Any] | None = None
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
            issues = [issue for issue in getattr(config, "configuration_issues", []) if interface_id in (issue.get("interface_ids") or [])]
            if issues:
                interface_results.append({**item, "state": "configuration_invalid", "ok": False, "errors": [issue["message"] for issue in issues], "expected_channels": expected, "detected_channels": [], "missing_channels": expected})
                errors.extend(issue["message"] for issue in issues)
                continue
            if item.get("driver") == USB_DRIVER:
                result = probe_usb(item)
                interface_results.append(result)
                for name in expected:
                    received[name] += int(result.get("sample_counts", {}).get(name, 0))
                if not result["ok"]:
                    errors.extend(result["errors"])
                continue
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
            if role in {"plc", "robot"}:
                network_path = validate_network_path(item)
                if not network_path["ok"]:
                    message = (
                        "工业设备网络路径与所选网卡不一致："
                        f"目标{network_path['target_address']}，"
                        f"实际源地址{network_path['actual_source_address'] or '未知'}"
                    )
                    errors.append(f"{endpoint}：{message}")
                    interface_results.append({
                        "id": interface_id,
                        "role": item.get("role", "custom"),
                        "driver": item.get("driver", ""),
                        "endpoint": endpoint,
                        "physical_interface_id": physical_id,
                        "physical_port_id": physical_port_id,
                        "physical_interface_kind": physical_kind,
                        "protocol": protocol,
                        "physical_fallback": physical_fallback,
                        "physical_verified": physical_verified,
                        "physical_warning": physical_warning,
                        "enabled": True,
                        "expected_channels": expected,
                        "detected_channels": [],
                        "missing_channels": expected,
                        "invalid_channels": [],
                        "sample_counts": {},
                        "invalid_sample_counts": {},
                        "errors": [message],
                        "state": "network_path_invalid",
                        "message": message,
                        "network_path": network_path,
                        "ok": False,
                    })
                    continue
            device_profile_status = device_profile_readiness(item)
            if role in SHARED_DEVICE_PROFILES and not device_profile_status["ready"]:
                missing_device_facts = list(
                    device_profile_status["missing_device_facts"]
                )
                message = (
                    "设备档缺少已批准的必需事实："
                    + "、".join(missing_device_facts)
                )
                errors.append(f"{endpoint}：{message}")
                interface_results.append({
                    "id": interface_id,
                    "role": item.get("role", "custom"),
                    "driver": item.get("driver", ""),
                    "endpoint": endpoint,
                    "physical_interface_id": physical_id,
                    "physical_port_id": physical_port_id,
                    "physical_interface_kind": physical_kind,
                    "protocol": protocol,
                    "physical_fallback": physical_fallback,
                    "physical_verified": physical_verified,
                    "physical_warning": physical_warning,
                    "enabled": True,
                    "expected_channels": expected,
                    "detected_channels": [],
                    "missing_channels": expected,
                    "invalid_channels": [],
                    "sample_counts": {},
                    "invalid_sample_counts": {},
                    "errors": [message],
                    "state": "device_profile_incomplete",
                    "message": message,
                    "ok": False,
                    **device_profile_status,
                })
                continue
            detected: dict[str, int] = {}
            invalid: dict[str, int] = {}
            channel_observations: dict[str, dict[str, Any]] = {}
            probe_errors: list[str] = []
            probe_diagnostics: dict[str, Any] = {}
            uvc_readiness = uvc_source_observation_readiness([])
            uvc_counted_frames = 0
            interface_driver = None
            try:
                interface_driver = MultiInterfaceDriver(
                    [item], list(config.schema_sensors), config.source_file,
                    {interface_id: expected},
                )
                interface_driver.open()
                probe_started = time.time()
                if item.get("driver") == "m3232_pressure":
                    probe_window = float(item.get("first_frame_timeout_seconds", 2.0)) + 0.2
                elif role == "thermal_uvc":
                    probe_window = max(1.2, min(float(timeout_seconds), 5.0))
                else:
                    probe_window = max(0.5, min(float(timeout_seconds), 1.5))
                while time.time() - probe_started < probe_window:
                    sample = interface_driver.read_sample()
                    if role == "thermal_uvc":
                        statuses = [
                            status for status in interface_driver.worker_status()
                            if str(status.get("interface_id") or "") == interface_id
                        ]
                        observations = (
                            list(statuses[0].get("source_observations") or [])
                            if statuses else []
                        )
                        uvc_readiness = uvc_source_observation_readiness(observations)
                        frame_count = int(uvc_readiness["distinct_source_frames"])
                        increment = max(0, frame_count - uvc_counted_frames)
                        if sample and increment:
                            sample_timestamp = time.time()
                            for name in expected:
                                value = _finite(sample.get(name)) if name in sample else None
                                if value is None:
                                    continue
                                detected[name] = frame_count
                                received[name] += increment
                                channel_observations[name] = {
                                    "value": value,
                                    "source_timestamp": sample_timestamp,
                                    "quality": "measured_new",
                                }
                            uvc_counted_frames = frame_count
                        if uvc_readiness["ready"] and expected and all(
                            detected.get(name, 0) >= 3 for name in expected
                        ):
                            break
                        time.sleep(0.01)
                        continue
                    if sample:
                        sample_timestamp = time.time()
                        for name in expected:
                            if name not in sample:
                                continue
                            if _finite(sample[name]) is None:
                                invalid[name] = invalid.get(name, 0) + 1
                                invalid_received[name] += 1
                                channel_observations[name] = {
                                    "value": None,
                                    "source_timestamp": sample_timestamp,
                                    "quality": "invalid",
                                }
                            else:
                                detected[name] = detected.get(name, 0) + 1
                                received[name] += 1
                                channel_observations[name] = {
                                    "value": _finite(sample[name]),
                                    "source_timestamp": sample_timestamp,
                                    "quality": "measured_new",
                                }
                    if expected and all(detected.get(name, 0) > 0 or invalid.get(name, 0) > 0 for name in expected):
                        break
                    time.sleep(0.01)
            except Exception as exc:
                probe_errors.append(str(exc))
            finally:
                if interface_driver is not None:
                    for source_driver in getattr(interface_driver, "drivers", []):
                        metadata = getattr(source_driver, "quality_metadata", {})
                        if isinstance(metadata, dict):
                            probe_diagnostics = deepcopy(metadata)
                    try:
                        interface_driver.close()
                    except Exception:
                        pass
            if role == "thermal_uvc":
                probe_diagnostics["source_readiness"] = deepcopy(uvc_readiness)
            if not detected and not probe_errors and interface_driver is not None:
                worker_statuses = list(
                    getattr(interface_driver, "final_worker_status", []) or []
                )
                worker_errors = [
                    str(status.get("last_error") or "")
                    for status in worker_statuses
                    if str(status.get("last_error") or "")
                ]
                if worker_errors:
                    probe_errors.extend(worker_errors)
            missing = [name for name in expected if detected.get(name, 0) <= 0]
            invalid_channels = [name for name in missing if invalid.get(name, 0) > 0]
            if detected:
                if role == "thermal_uvc" and not uvc_readiness["ready"]:
                    state = "uvc_new_frames_insufficient"
                elif role == "thermal_uvc" and physical_verified:
                    state = "data_ready"
                elif role in {"plc", "robot"} and (missing or invalid_channels):
                    state = "partial_channels"
                elif role in {"plc", "robot"} and physical_verified:
                    state = "data_ready"
                else:
                    state = "ok" if physical_verified else "physical_unverified"
                message = f"接口已收到有效数据：{'、'.join(sorted(detected))}"
                if role == "thermal_uvc" and not uvc_readiness["ready"]:
                    message += (
                        f"；仅确认 {uvc_readiness['distinct_source_frames']} 个不同源帧，"
                        f"观察跨度 {uvc_readiness['observation_span_seconds']:.3f} 秒"
                    )
                if missing:
                    if role in {"plc", "robot"}:
                        message += f"；缺少必需通道：{'、'.join(missing)}"
                    else:
                        message += f"；未收到通道不阻止采集：{'、'.join(missing)}"
                if not physical_verified:
                    message += "；物理端口尚未与该实时设备关联，不能启动真实采集"
            elif probe_errors:
                error_text = "；".join(probe_errors)
                if role == "esr1001_strain":
                    state = classify_esr1001_probe_error(error_text)
                    message = f"ESR1001-A协议探针失败（{state}）：{error_text}"
                else:
                    state = classify_interface_probe_error(role, error_text)
                    message = f"接口无法打开或读取：{error_text}"
            elif invalid_channels:
                state, message = "invalid_data", f"收到非数值数据：{'、'.join(invalid_channels)}"
            else:
                state, message = "no_data", f"未检测到采集数据：{'、'.join(missing)}"
            if state not in {"ok", "data_ready"}:
                errors.append(f"{endpoint}：{message}")
            channel_results = {}
            driver_diagnostics = probe_diagnostics or (deepcopy(getattr(interface_driver, "interface_quality_metadata", {}).get(interface_id, {})) if interface_driver else {})
            for name in expected:
                observation = dict(channel_observations.get(name) or {})
                channel_results[name] = {
                    "value": observation.get("value"),
                    "unit": str((SENSOR_CHANNEL_METADATA.get(name) or {}).get("unit") or ""),
                    "source_timestamp": observation.get("source_timestamp"),
                    "quality": str(observation.get("quality") or "missing"),
                }
            stages = {
                "network_path": {
                    "state": (
                        "passed" if network_path and network_path.get("ok")
                        else "not_applicable" if role not in {"plc", "robot"}
                        else "failed"
                    ),
                    "evidence": deepcopy(network_path),
                },
                "protocol": {
                    "state": (
                        "failed" if probe_errors
                        else "passed" if detected or invalid
                        else "not_verified"
                    ),
                    "protocol": protocol,
                },
                "channels": {
                    "state": "passed" if not missing and not invalid_channels else "failed",
                    "passed": sorted(detected),
                    "missing": list(missing),
                    "invalid": list(invalid_channels),
                },
                "freshness": {
                    "state": "passed" if (
                        detected and not missing
                        and (role != "thermal_uvc" or uvc_readiness["ready"])
                    ) else "failed",
                    "freshness_seconds": float(
                        (item.get("device_profile") or {}).get("freshness_seconds")
                        or item.get("freshness_seconds")
                        or 0.0
                    ),
                    **({"source_readiness": deepcopy(uvc_readiness)} if role == "thermal_uvc" else {}),
                },
            }
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
                "invalid_channels": invalid_channels, "sample_counts": detected,
                "invalid_sample_counts": invalid, "errors": probe_errors,
                "auxiliary_only": auxiliary_only, "state": state,
                "message": message, "ok": state in {"ok", "data_ready"},
                "network_path": deepcopy(network_path),
                "stages": stages,
                "channel_results": channel_results,
                "read_profile": item.get("read_profile"),
                "driver_diagnostics": driver_diagnostics,
                "protocol_verified": bool(driver_diagnostics.get("protocol_verified", bool(detected or invalid))),
                "temperature_valid": bool(
                    detected and (role != "thermal_uvc" or uvc_readiness["ready"])
                ),
            })
        configuration_issues = list(getattr(config, "configuration_issues", []) or [])
        issues_by_interface: dict[str, list[dict[str, Any]]] = {}
        for issue in configuration_issues:
            message = str(issue.get("message") or "采集配置无效")
            if message and message not in errors:
                errors.append(message)
            for interface_id in issue.get("interface_ids") or []:
                issues_by_interface.setdefault(str(interface_id), []).append(issue)
        for interface_result in interface_results:
            interface_issues = issues_by_interface.get(str(interface_result.get("id") or ""), [])
            if not interface_issues:
                continue
            interface_result["probe_state"] = interface_result.get("state")
            interface_result["probe_ok"] = bool(interface_result.get("ok"))
            interface_result["probe_message"] = str(interface_result.get("message") or "")
            interface_result["configuration_issues"] = interface_issues
            interface_result["state"] = "configuration_invalid"
            interface_result["ok"] = False
            issue_text = "；".join(str(issue.get("message") or "采集配置无效") for issue in interface_issues)
            probe_text = str(interface_result.get("probe_message") or "")
            interface_result["message"] = f"配置问题：{issue_text}" + (f"；探测结果：{probe_text}" if probe_text else "")

        interface_results = [
            normalize_interface_readiness(item) for item in interface_results
        ]

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
            "configuration_issues": configuration_issues,
        }
        result["checked_at"] = time.time()
        with self.lock:
            if config.acquisition_mode == "real":
                self._readiness_snapshot = ReadinessSnapshot.create(
                    config, result, capabilities=config.helper_capabilities
                )
                result["readiness_snapshot"] = self._readiness_snapshot.to_dict()
                self._session_readiness_fingerprint = readiness_config_fingerprint(config) if result["ok"] else ""
                self.readiness_version += 1
                self.readiness_invalid_reason = "" if result["ok"] else "hardware_check_failed"
                result["readiness_session_id"] = self.readiness_session_id
                result["readiness_version"] = self.readiness_version
            self._latest_check_result = deepcopy(result)
        return result

    def import_readiness_snapshot(self, payload: dict[str, Any]) -> None:
        snapshot = ReadinessSnapshot.from_dict(payload)
        with self.lock:
            self._readiness_snapshot = snapshot
            self._session_readiness_fingerprint = ""

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
        candidates.extend(matrix_csv_path(path) for path in list(candidates) if matrix_csv_path(path).exists())
        combined = self.session_dir / f"{specimen_folder_name}_完整试样.CSV"
        if combined.exists():
            candidates.append(combined)
            if matrix_csv_path(combined).exists():
                candidates.append(matrix_csv_path(combined))
        candidates.extend(
            path
            for path in self.session_dir.glob(f"{specimen_folder_name}_完整试样_派生.*")
            if path.is_file()
        )
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
        for path in (self.raw_work_path, self.timestamp_work_path, getattr(self, "matrix_work_path", None)):
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
            capture_uuid = str(config.capture_uuid or "").strip()
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
            "layer_sensor_channels": {
                **dict(identity_previous.get("layer_sensor_channels") or {}),
                str(config.layer + 1): list(config.selected_sensors or []),
            },
        }
        # Commit this identity only after at least one valid sample has been
        # finalized.  Until then, the previous specimen remains authoritative.
        self.pending_session_metadata = metadata
        return capture_uuid

    def _capture_uuid_for_start(self, config: AcquisitionConfig) -> str:
        token = _parameter_token(config)
        if config.layer > 0 and config.save_root:
            previous = self._read_json(Path(config.save_root) / token / "采集记录" / "当前试样会话.json")
            signature = {"dataset_schema": config.dataset_schema, "condition_id": str(config.condition_id),
                         "replicate": int(config.replicate), "parameter_token": token}
            if previous.get("identity") == signature and previous.get("capture_uuid"):
                return str(previous["capture_uuid"])
        return str(config.capture_uuid or uuid.uuid4().hex)

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
            (getattr(self, "matrix_work_path", None), getattr(self, "matrix_path", None)),
        ):
            if working is None or final is None or not working.exists():
                continue
            with working.open("r+b") as handle:
                os.fsync(handle.fileno())
            if final == getattr(self, "matrix_path", None) and final.exists():
                self._archive_existing(final, "中断采集")
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
            summary["unified_frame_quality"] = self.quality_metrics.summary()
        strain_channels = [
            name for name in STRAIN_SENSOR_COLUMNS if name in selected
        ]
        if strain_channels:
            missing_or_stale_intervals: dict[str, list[dict[str, int]]] = {}
            for channel in strain_channels:
                intervals_for_channel: list[dict[str, int]] = []
                interval_start: int | None = None
                for index, quality in enumerate(self.frame_quality):
                    state = str(quality.get(channel) or ChannelQuality.MISSING.value)
                    unavailable = state in {
                        ChannelQuality.MISSING.value,
                        ChannelQuality.STALE.value,
                        ChannelQuality.INVALID.value,
                    }
                    if unavailable and interval_start is None:
                        interval_start = index
                    elif not unavailable and interval_start is not None:
                        intervals_for_channel.append(
                            {"start_frame": interval_start, "end_frame": index - 1}
                        )
                        interval_start = None
                if interval_start is not None:
                    intervals_for_channel.append(
                        {
                            "start_frame": interval_start,
                            "end_frame": max(interval_start, len(self.frame_quality) - 1),
                        }
                    )
                missing_or_stale_intervals[channel] = intervals_for_channel

            interface_id = next(
                (
                    str(item.get("id") or "")
                    for item in (self.config.interfaces or [])
                    if item.get("role") == "esr1001_strain"
                ),
                "esr1001_strain",
            ) if self.config else "esr1001_strain"
            interface_metadata = {}
            if isinstance(self.driver, MultiInterfaceDriver):
                interface_metadata = dict(
                    self.driver.interface_quality_metadata.get(interface_id) or {}
                )
            protocol_error_counts = dict(
                interface_metadata.get("protocol_error_counts") or {}
            )
            summary["esr1001_strain"] = {
                "interface_id": interface_id,
                "observation_counts": {
                    name: int(self.sensor_received.get(name, 0))
                    for name in strain_channels
                },
                "last_valid_time": {
                    name: self.sensor_last_time.get(name)
                    for name in strain_channels
                },
                "missing_or_stale_intervals": missing_or_stale_intervals,
                "protocol_error_counts": protocol_error_counts,
                "last_protocol_error": str(interface_metadata.get("last_error") or ""),
            }
        if isinstance(self.driver, MultiInterfaceDriver):
            summary["esr1001_usb"] = {
                key: dict(value) for key, value in self.driver.interface_quality_metadata.items()
                if value.get("driver") == USB_DRIVER
            }
            summary["m3232"] = {key: dict(value) for key, value in self.driver.interface_quality_metadata.items() if value.get("driver") == "m3232_pressure"}
            summary["thermocouple64"] = {
                key: dict(value) for key, value in self.driver.interface_quality_metadata.items()
                if value.get("driver") == THERMOCOUPLE64_DRIVER
            }
        return summary

    def _record_start_preflight(
        self, audit: dict[str, Any], stage: str, **details: Any
    ) -> dict[str, Any]:
        event = {
            "stage": str(stage),
            "server_monotonic": time.monotonic(),
            **details,
        }
        audit["events"].append(event)
        started = float(audit["events"][0]["server_monotonic"])
        audit["elapsed_ms"] = max(
            0.0, (float(event["server_monotonic"]) - started) * 1000.0
        )
        audit["complete"] = stage in {
            "start_preflight_end",
            "start_preflight_failed",
        }
        with self.lock:
            self._last_start_preflight = deepcopy(audit)
        return event

    def start(self, config: AcquisitionConfig) -> dict:
        """Start one capture while safely replacing an active simulation.

        A simulation is disposable and may be left running when a browser is
        refreshed or switches modes.  Finalize it before the next start so it
        cannot permanently block local, LAN, or public operation.  A real
        hardware capture is never taken over implicitly.
        """
        if self._start_thread is not None and self._start_thread.is_alive() and threading.current_thread() is not self._start_thread:
            raise RuntimeError("capture_start_in_progress")
        preflight: dict[str, Any] = {
            "clock_domain": "server_monotonic",
            "start_request_id": str(getattr(config, "start_request_id", "") or ""),
            "capture_generation": str(
                getattr(config, "capture_generation", "") or ""
            ),
            "readiness_session_id": self.readiness_session_id,
            "readiness_session_reused": False,
            "complete": False,
            "elapsed_ms": 0.0,
            "events": [],
        }
        self._record_start_preflight(preflight, "start_preflight_begin")
        reused_network_paths = 0
        probed_network_paths = 0
        try:
            assert_config_contract(config, RELEASE_CONTRACT)
            if config.acquisition_mode == "real":
                requires_readiness = False
                with self.lock:
                    snapshot = self._readiness_snapshot
                same_readiness_session = bool(
                    snapshot is not None and self.session_readiness_matches(config)
                )
                readiness_session_reused = False
                if same_readiness_session:
                    try:
                        snapshot.validate(config, allow_expired=True)
                    except ValueError:
                        readiness_session_reused = False
                    else:
                        readiness_session_reused = True
                preflight["readiness_session_reused"] = readiness_session_reused
                self._record_start_preflight(
                    preflight,
                    "readiness_session_evaluated",
                    snapshot_present=snapshot is not None,
                    same_session=same_readiness_session,
                    valid_for_reuse=readiness_session_reused,
                )
                for item in config.interfaces or []:
                    if not item.get("enabled", True):
                        continue
                    role = str(item.get("role") or "")
                    interface_id = str(item.get("id") or role or "unknown")
                    if item.get("driver") == USB_DRIVER:
                        requires_readiness = True
                    if role in SHARED_DEVICE_PROFILES:
                        requires_readiness = True
                        profile_status = device_profile_readiness(item)
                        if not profile_status["ready"]:
                            raise RuntimeError(
                                "device_profile_incomplete: "
                                + interface_id
                                + ": "
                                + ", ".join(profile_status["missing_device_facts"])
                            )
                    if role in {"plc", "robot"}:
                        network_path = (
                            reusable_network_path(item, snapshot.interfaces)
                            if readiness_session_reused and snapshot is not None
                            else None
                        )
                        if network_path is not None:
                            item["network_path"] = network_path
                            reused_network_paths += 1
                            self._record_start_preflight(
                                preflight,
                                "network_path_reused",
                                interface_id=interface_id,
                                physical_interface_id=str(
                                    item.get("physical_interface_id") or ""
                                ),
                                target_address=str(
                                    network_path.get("target_address") or ""
                                ),
                                target_port=int(
                                    network_path.get("target_port") or 0
                                ),
                                source_address=str(
                                    network_path.get("actual_source_address") or ""
                                ),
                            )
                        else:
                            probe_begin = self._record_start_preflight(
                                preflight,
                                "network_path_probe_begin",
                                interface_id=interface_id,
                            )
                            probed_network_paths += 1
                            item["network_path"] = require_network_path(item)
                            self._record_start_preflight(
                                preflight,
                                "network_path_probe_end",
                                interface_id=interface_id,
                                elapsed_ms=max(
                                    0.0,
                                    (
                                        time.monotonic()
                                        - float(probe_begin["server_monotonic"])
                                    )
                                    * 1000.0,
                                ),
                                source_address=str(
                                    item["network_path"].get(
                                        "actual_source_address"
                                    )
                                    or ""
                                ),
                            )
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
                if requires_readiness:
                    if snapshot is None:
                        raise RuntimeError("readiness_required: 请先执行当前配置的接口检查")
                    try:
                        snapshot.validate(
                            config, allow_expired=same_readiness_session
                        )
                    except ValueError as exc:
                        raise RuntimeError(f"readiness_required: {exc}") from exc
        except Exception as exc:
            self._record_start_preflight(
                preflight,
                "start_preflight_failed",
                error_type=type(exc).__name__,
                error_code=str(exc).partition(":")[0][:120],
                network_paths_reused=reused_network_paths,
                network_paths_probed=probed_network_paths,
            )
            raise
        self._record_start_preflight(
            preflight,
            "start_preflight_end",
            network_paths_reused=reused_network_paths,
            network_paths_probed=probed_network_paths,
        )
        with self.lifecycle_lock:
            with self.lock:
                if self.capture_phase == "stop_timed_out" and not self.finalization_complete:
                    raise RuntimeError(
                        "previous_capture_stop_timed_out: "
                        "上一采集仍有未退出的设备，请完成停止后再开始采集"
                    )
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
            self._assert_start_not_cancelled()
            try:
                result = self._start_new(
                    config, preflight_events=tuple(preflight["events"])
                )
            except Exception:
                if not self._start_cancel.is_set():
                    self.invalidate_readiness()
                raise
            if config.acquisition_mode == "real":
                with self.lock:
                    self._session_readiness_fingerprint = readiness_config_fingerprint(config)
            if replaced_simulation:
                result["replaced_active_simulation"] = True
            return result

    def _start_new(
        self,
        config: AcquisitionConfig,
        *,
        preflight_events: tuple[dict[str, Any], ...] = (),
    ) -> dict:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("采集已经在运行")
            if (
                not self.finalization_complete
                and self.capture_phase in {"database_pending_retry", "finalization_failed"}
            ):
                raise RuntimeError(
                    f"previous_capture_{self.capture_phase}: "
                    "上一采集仍有未完成的持久化收尾，必须先处理后再开始新采集"
                )
            if (
                self.thread is not None
                and self.config is not None
                and not self.finalization_complete
            ):
                # A hardware driver may stop itself after a read error.  Commit
                # any rows already received before a new session resets state.
                self.stop()
            self._begin_capture_timeline(config)
            if preflight_events:
                with self._start_job_lock:
                    self._start_events[0:0] = deepcopy(list(preflight_events))
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
            self.frame_sources.clear()
            self.frame_envelopes.clear()
            self.frame_catalog = None
            self.quality_metrics = AcquisitionQualityMetrics(
                config.selected_sensors or [], config.sample_rate_hz
            )
            self.total_sample_count = 0
            self.quality_sidecar_path = None
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
            self.capture_phase = "starting"
            self.persistence_failed = False
            self.persistence_queue_depth = 0
            self.persistence_queue_high_water_rows = 0
        self.save_status = check_capture_save_root(config.save_root)
        self.record_start_event("save_root_checked", ok=self.save_status["ok"])
        mysql_preflight: dict[str, Any] = {}
        preflight_destinations = (
            self._mysql_destinations(config)
            if config.acquisition_mode == "real"
            else []
        )
        for destination_name, settings in preflight_destinations:
            result = MySQLCaptureStore(settings).preflight(write_test=True)
            mysql_preflight[destination_name] = result
            if not result.get("ok"):
                self.last_error = (
                    f"mysql_preflight_failed: {destination_name}: "
                    f"{result.get('error') or result.get('stage') or 'unknown error'}"
                )
                self.mysql_status = {
                    "enabled": True,
                    "ok": False,
                    "state": "preflight_failed",
                    "preflight": mysql_preflight,
                    "error": self.last_error,
                    "saved_rows": 0,
                }
                self.finalization_complete = True
                raise RuntimeError(self.last_error)
        if mysql_preflight:
            self.mysql_status["preflight"] = mysql_preflight
        if preflight_destinations and not self.save_status["ok"]:
            # A database can disappear after preflight.  Real MySQL-only
            # captures therefore stream to the manager's local durable
            # root so the complete layer remains retryable instead of
            # depending on the bounded live deque.
            requested_save_status = dict(self.save_status)
            retry_spool = check_capture_save_root(self.capture_root)
            if not retry_spool.get("ok"):
                self.last_error = (
                    "persistence_retry_spool_unavailable: "
                    + str(retry_spool.get("message") or "unknown error")
                )
                self.finalization_complete = True
                raise RuntimeError(self.last_error)
            self.save_status = {
                **retry_spool,
                "retry_spool": True,
                "requested_save_status": requested_save_status,
                "message": "MySQL正式采集使用本机耐久重试暂存目录",
            }
        durable_destination = bool(
            self.save_status["ok"]
            or config.mysql_enabled
            or config.mysql_local_enabled
        )
        if (
            config.acquisition_mode == "real"
            and config.persistence_policy == "required"
            and not durable_destination
        ):
            self.last_error = (
                "persistence_required: real capture requires a writable save root "
                "or an enabled durable MySQL destination"
            )
            self.finalization_complete = True
            raise RuntimeError(self.last_error)
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
        self.matrix_path = None
        self.matrix_work_path = None
        self.started_at = time.time()
        self.stopped_at = None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_stamp = stamp
        self.record_start_event("storage_paths_begin")
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
            probe_path = self.capture_record_dir / f".{stamp}.write-probe"
            try:
                with probe_path.open("xb") as probe:
                    probe.write(b"afp-persistence-probe")
                    probe.flush()
                    os.fsync(probe.fileno())
            finally:
                probe_path.unlink(missing_ok=True)
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
            self.capture_uuid = str(config.capture_uuid or "").strip() or (
                f"AFP-{datetime.now().strftime('%Y%m%dT%H%M%S')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            config.capture_uuid = self.capture_uuid
        if self.save_enabled:
            self._prepare_capture_identity(config, specimen_folder_name)
        for item in config.interfaces or []:
            if item.get("enabled", True) and item.get("driver") in {USB_DRIVER, "m3232_pressure"}:
                if self.capture_record_dir is None:
                    raise RuntimeError("原生USB采集需要可写原始证据目录")
                item["_raw_capture_root"] = str(self.capture_record_dir)
                if item.get("driver") == "m3232_pressure":
                    if [item.get("matrix_rows"), item.get("matrix_cols")] != [32, 32]:
                        raise ValueError("完整矩阵CSV需要32×32配置")
                    self.matrix_path = matrix_csv_path(self.raw_path)
                    self.matrix_work_path = self.matrix_path.with_name(self.matrix_path.name + ".partial")
                    self._archive_orphan_partial(self.matrix_work_path)
                    item["_matrix_csv_work_path"] = str(self.matrix_work_path)
                    item["_matrix_csv_context"] = {
                        "capture_uuid": self.capture_uuid, "capture_generation": config.capture_generation,
                        "run_id": config.run_id, "layer_id": config.layer + 1, "main_csv": self.raw_path.name,
                    }
        config._session_usb_profiles = self.usb_startup_profiles(config)
        self.driver = build_driver(config)
        self.record_start_event("storage_paths_end")
        try:
            self._assert_start_not_cancelled()
            self.record_start_event("devices_open_begin")
            self.driver.open()
            self.record_start_event("devices_open_end")
            self._assert_start_not_cancelled()
            if self.save_enabled:
                self.record_start_event("capture_identity_begin")
                self.record_start_event("capture_identity_end")
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
                        "matrix_csv_file": str(self.matrix_path) if self.matrix_path else None,
                        "channel_units": {name: (SENSOR_CHANNEL_METADATA.get(name) or {}).get("unit", "") for name in config.selected_sensors},
                        "archived_previous_session": self.archived_previous_session,
                        "whole_specimen_rule": (
                            f"{specimen_folder_name}_完整试样.CSV（每层结束后原子覆盖更新）"
                        ),
                    },
                )
        except Exception as exc:
            self.last_error = str(exc)
            self.capture_phase = "start_failed"
            try:
                if self._start_cancel.is_set() and isinstance(self.driver, MultiInterfaceDriver):
                    usb_opening = any(isinstance(worker.driver, Esr1001UsbDriver) for worker in self.driver.workers)
                    cancel_budget = config.stop_sampling_timeout_seconds if usb_opening else config.stop_force_timeout_seconds
                    self.driver.close(timeout_seconds=cancel_budget)
                else:
                    self.driver.close()
            finally:
                self.thread = None
                if getattr(self.driver, "workers", []):
                    self.finalization_complete = False
                    self.capture_phase = "stop_timed_out"
                else:
                    self.driver = None
                    self.finalization_complete = True
            raise
        self._assert_start_not_cancelled()
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="AFP-Acquisition",
            daemon=True,
        )
        self.thread.start()
        self.capture_phase = (
            "waiting_first_sample"
            if config.acquisition_mode == "real"
            else "running"
        )
        if config.acquisition_mode == "real":
            required_channels = set(config.selected_sensors or [])
            observed: set[str] = set()
            deadline = time.monotonic() + config.first_sample_timeout_seconds
            while time.monotonic() < deadline:
                self._assert_start_not_cancelled()
                with self.lock:
                    self._record_start_frame()
                    observed = {
                        name for name in required_channels
                        if len(self._source_observations.get(name, [])) >= 2
                    }
                    alive = self.thread is not None and self.thread.is_alive()
                if observed == required_channels and self._curve_start_saved:
                    self.capture_phase = "running"
                    break
                if not alive:
                    break
                self.wait_for_stream_activity(min(0.01, max(0.0, deadline - time.monotonic())))
            if required_channels and (observed != required_channels or not self._curve_start_saved):
                missing = sorted(required_channels - observed)
                self.last_error = "first_sample_timeout: " + ", ".join(missing)
                self.capture_phase = "start_failed"
                self.stop_event.set()
                if self.driver is not None:
                    self.driver.close()
                if self.thread is not None:
                    self.thread.join(timeout=config.stop_force_timeout_seconds)
                self.finalization_complete = False
                raise RuntimeError(self.last_error)
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
        if path.suffix.casefold() == ".csv":
            paired = matrix_csv_path(path)
            if paired.exists():
                paired.replace(matrix_csv_path(archived))
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
        headers = [self._layer_columns(path) for _, path in available]
        observed = {name for header in headers for name in header}
        canonical = list(ACQUISITION_SCHEMAS[self.config.dataset_schema]["raw_columns"])
        union_columns = [name for name in canonical if name in observed]
        union_columns.extend(
            name
            for header in headers
            for name in header
            if name not in union_columns
        )
        try:
            with temporary.open("w", encoding="gb18030", newline="") as output_file:
                writer = csv.DictWriter(
                    output_file, fieldnames=union_columns, extrasaction="ignore"
                )
                writer.writeheader()
                for _, layer_path in available:
                    with layer_path.open("r", encoding="gb18030", newline="") as layer_file:
                        reader = csv.DictReader(layer_file)
                        writer.writerows(reader)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temporary, combined_path)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        self.full_specimen_path = combined_path
        matrices = [matrix_csv_path(path) for _, path in available if matrix_csv_path(path).is_file()]
        if matrices:
            combined_matrix = matrix_csv_path(combined_path)
            work_matrix = combined_matrix.with_name(combined_matrix.name + ".partial")
            self._archive_orphan_partial(work_matrix)
            with work_matrix.open("x", encoding="utf-8-sig", newline="") as output:
                matrix_writer = None
                for path in matrices:
                    with path.open(encoding="utf-8-sig", newline="") as source:
                        reader = csv.DictReader(source)
                        if matrix_writer is None:
                            matrix_writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
                            matrix_writer.writeheader()
                        matrix_writer.writerows(reader)
                output.flush()
                os.fsync(output.fileno())
            os.replace(work_matrix, combined_matrix)
        if self.config.dataset_schema == THERMOCOUPLE64_MODE_ID:
            layer_payloads: list[dict[str, Any]] = []
            for layer_index, layer_path in available:
                with layer_path.open("r", encoding="gb18030", newline="") as layer_file:
                    layer_payloads.append(
                        {
                            "layer_id": layer_index + 1,
                            "completed": True,
                            "rows": list(csv.DictReader(layer_file)),
                        }
                    )
            sensor_union = [
                name
                for name in ACQUISITION_SCHEMAS[THERMOCOUPLE64_MODE_ID]["sensors"]
                if name in observed
            ]
            projection = project_specimen_layers(
                layer_payloads,
                sensor_columns=sensor_union,
                scope="completed_specimen",
            )
            derived_path = combined_path.with_name(
                f"{specimen_folder_name}_完整试样_派生.CSV"
            )
            derived_temporary = derived_path.with_name(
                f".{derived_path.name}.{self.session_stamp}.tmp"
            )
            derived_columns = [*union_columns, *THERMOCOUPLE64_DERIVED_CHANNELS]
            try:
                with derived_temporary.open(
                    "w", encoding="gb18030", newline=""
                ) as output_file:
                    writer = csv.DictWriter(
                        output_file,
                        fieldnames=derived_columns,
                        extrasaction="ignore",
                    )
                    writer.writeheader()
                    for layer in projection["derived_layers"]:
                        writer.writerows(layer["rows"])
                    output_file.flush()
                    os.fsync(output_file.fileno())
                os.replace(derived_temporary, derived_path)
            finally:
                if derived_temporary.exists():
                    derived_temporary.unlink(missing_ok=True)
            _atomic_write_json(
                derived_path.with_suffix(".manifest.json"), projection["manifest"]
            )
            self.derived_specimen_path = derived_path
        return combined_path

    def _run(self) -> None:
        config = self.config
        if not str(self.capture_uuid or "").strip():
            self.capture_uuid = str(config.capture_uuid or "").strip() or (
                f"AFP-{datetime.now().strftime('%Y%m%dT%H%M%S')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            config.capture_uuid = self.capture_uuid
        selected = set(config.selected_sensors or [])
        raw_target = (
            self.raw_work_path
            if self.save_enabled and self.raw_work_path is not None
            else _DiscardTextIO()
        )
        timestamp_target = (
            self.timestamp_work_path
            if self.save_enabled and self.timestamp_work_path is not None
            else _DiscardTextIO()
        )
        capture_writer: BoundedCsvWriter | None = None
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
                        "channel_source_sequence_json",
                        "channel_interface_json",
                        "channel_source_record_json",
                    ],
                )
                writer.writeheader()
                timestamp_writer.writeheader()
                capture_writer = BoundedCsvWriter(
                    writer,
                    timestamp_writer,
                    max_queue_rows=config.writer_queue_rows,
                    submit_timeout_seconds=config.writer_submit_timeout_seconds,
                )
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
                    if isinstance(self.driver, MultiInterfaceDriver):
                        for device in self.driver.drivers:
                            recorder = getattr(device, "recorder", None)
                            if recorder is not None and recorder.error:
                                raise RuntimeError("source_writer_failed: " + recorder.error)
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
                        if config.dataset_schema == "esr1001_raw_adc":
                            row.update({"cycle": config.cycle, "file": self.raw_path.name if self.raw_path else "", "root": config.root,
                                        "run_id": config.run_id, "specimen_id": config.specimen_id, "condition_id": config.condition_id,
                                        "replicate": config.replicate, "layer_id": config.layer + 1})
                        elif config.dataset_schema in MODERN_COLLECTION_SCHEMAS:
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
                        timestamp_row = {
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
                            "channel_source_sequence_json": json.dumps({name: ch.source_sequence for name, ch in frame.channels.items()} if frame else {}, ensure_ascii=False),
                            "channel_interface_json": json.dumps({name: ch.interface_id for name, ch in frame.channels.items()} if frame else {}, ensure_ascii=False),
                            "channel_source_record_json": json.dumps({name: (dict(ch.metadata["latest_record"], calibration_snapshot=ch.metadata.get("calibration_snapshot")) if ch.metadata.get("driver") == USB_DRIVER else {key: ch.metadata["latest_record"].get(key) for key in ("connection_generation", "receive_sequence", "sha256", "byte_offset")}) for name, ch in frame.channels.items() if ch.metadata.get("latest_record")} if frame else {}, ensure_ascii=False),
                        }
                        capture_writer.submit(row, timestamp_row)
                        self.persistence_queue_depth = capture_writer.pending_rows
                        self.persistence_queue_high_water_rows = max(
                            self.persistence_queue_high_water_rows,
                            capture_writer.high_water_rows,
                        )
                        if frame is None:
                            target_monotonic = time.monotonic()
                            frame = UnifiedFrame(
                                capture_sequence=row_index,
                                target_monotonic=target_monotonic,
                                target_wall_time=now,
                                channels={
                                    name: ChannelSample(
                                        interface_id="simulation",
                                        channel_name=name,
                                        value=row.get(name),
                                        quality=ChannelQuality(
                                            quality.get(name, ChannelQuality.MISSING.value)
                                        ),
                                        received_wall_time=now,
                                        received_monotonic=target_monotonic,
                                        source_sequence=row_index,
                                        metadata={"age_seconds": 0.0},
                                    )
                                    for name in config.selected_sensors
                                },
                                assembled_monotonic=target_monotonic,
                            )
                        channel_units = {
                            name: str(
                                (SENSOR_CHANNEL_METADATA.get(name) or {}).get("unit")
                                or ""
                            )
                            for name in config.selected_sensors
                        }
                        if config.dataset_schema == THERMOCOUPLE64_MODE_ID:
                            catalog = raw_catalog(config.selected_sensors, channel_units)
                            envelope = frame.to_envelope_v2(
                                session_id=self.capture_uuid or config.capture_uuid,
                                mode_id=config.dataset_schema,
                                catalog=catalog,
                            )
                            self.frame_catalog = catalog
                        else:
                            envelope = frame.to_envelope(
                                self.capture_uuid or config.capture_uuid,
                                channel_units,
                            )
                        with self.lock:
                            self.total_sample_count += 1
                            self._record_start_frame(frame)
                            self.rows.append(row)
                            self.timestamps.append(now)
                            self.frame_quality.append(quality)
                            self.frame_sources.append({name: {
                                "capture_generation": config.capture_generation,
                                "start_request_id": config.start_request_id,
                                "source_sequence": sample.source_sequence,
                                "received_monotonic": sample.received_monotonic,
                                "interface_id": sample.interface_id,
                                "protocol_ok": sample.protocol_ok,
                                "after_start": self._capture_start_monotonic is not None and sample.received_monotonic >= self._capture_start_monotonic,
                                "quality": sample.quality.value,
                                "stable": sample.metadata.get("current_stream_stable", True),
                            } for name, sample in frame.channels.items()})
                            envelope["capture_generation"] = config.capture_generation
                            envelope["start_request_id"] = config.start_request_id
                            self.frame_envelopes.append(envelope)
                            for name in selected:
                                if _finite(row.get(name)) is not None:
                                    self.sensor_received[name] += 1
                                    self.sensor_last_time[name] = now
                            self._stream_activity.notify_all()
                    if replay_scheduler is not None:
                        replay_scheduler.wait_next()
                capture_writer.close(timeout_seconds=config.writer_stop_timeout_seconds)
                self.persistence_queue_depth = 0
                raw_file.flush()
                time_file.flush()
                if self.save_enabled:
                    os.fsync(raw_file.fileno())
                    os.fsync(time_file.fileno())
        except Exception as exc:
            self.last_error = str(exc)
            self.persistence_failed = bool(self.save_enabled)
            if capture_writer is not None:
                try:
                    capture_writer.close(timeout_seconds=config.writer_stop_timeout_seconds)
                except Exception as close_exc:
                    if str(close_exc) not in self.last_error:
                        self.last_error = f"{self.last_error}; {close_exc}".strip("; ")
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
            "relocated_sources": 0,
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
            saved_layer_file = Path(str(payload.get("layer_file") or ""))
            layer_file = _resolve_pending_layer_file(pending_path, payload, root)
            if layer_file is None:
                result["failed"] += 1
                result["errors"].append(
                    f"{pending_path.name}：找不到与采集身份匹配的层文件 {saved_layer_file.name}"
                )
                continue
            if not saved_layer_file.is_file():
                result["relocated_sources"] += 1
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
        if root is not None:
            retry_root = Path(root).expanduser()
        elif self.save_status.get("retry_spool") and self.save_status.get("path"):
            retry_root = Path(str(self.save_status["path"])).expanduser()
        else:
            retry_root = Path(
                (active_config.save_root if active_config is not None else "")
                or self.capture_root
            ).expanduser()
        result = self._retry_pending_mysql(
            settings, retry_root, limit=max(1, min(int(limit), 1000))
        )
        recovery_after = scan_persistence_recovery(retry_root)
        current_pending = False
        if self.capture_uuid:
            for pending_path in retry_root.rglob("*_mysql*_pending.json"):
                payload = self._read_json(pending_path)
                summary = payload.get("summary")
                if (
                    isinstance(summary, dict)
                    and str(summary.get("capture_uuid") or "") == self.capture_uuid
                ):
                    current_pending = True
                    break
        current_capture_synced = bool(
            self.capture_phase == "database_pending_retry"
            and int(result.get("attempted", 0)) > 0
            and int(result.get("failed", 0)) == 0
            and not result.get("connection_error")
            and not current_pending
        )
        restart_recovery_synced = bool(
            self.capture_phase == "database_pending_retry"
            and int(result.get("attempted", 0)) > 0
            and int(result.get("failed", 0)) == 0
            and not result.get("connection_error")
            and recovery_after["database_pending_retry"] == 0
            and recovery_after["incomplete_files"] == 0
        )
        with self.lock:
            self.mysql_status = {
                **self.mysql_status,
                "enabled": True,
                "ok": True if current_capture_synced else self.mysql_status.get("ok"),
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
            if current_capture_synced or restart_recovery_synced:
                self.finalization_complete = True
                self.capture_phase = "completed"
                if "database" in self.last_error.lower() or "mysql" in self.last_error.lower():
                    self.last_error = ""
            self._apply_recovery_state_locked(recovery_after)
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
        if threading.current_thread() is not self._start_thread:
            pending = self.cancel_start()
            if pending is not None:
                return pending
        with self.lifecycle_lock:
            return self._stop_active()

    def _stop_active(self) -> dict:
        states = getattr(self.driver, "worker_status", lambda: [])()
        failed_interfaces = [str(row.get("interface_id") or "") for row in states
                             if row.get("state") == "failed" or int(row.get("reconnect_attempts") or 0) > 0]
        if failed_interfaces:
            self.invalidate_readiness(failed_interfaces)
        if (
            not self.finalization_complete
            and self.capture_phase in {"database_pending_retry", "finalization_failed"}
        ):
            return self.status()
        self.capture_phase = "stopping_sampling"
        self.stop_event.set()
        thread = self.thread
        if thread is not None:
            sampling_timeout = (
                self.config.stop_sampling_timeout_seconds if self.config else 5.0
            )
            force_timeout = (
                self.config.stop_force_timeout_seconds if self.config else 2.0
            )
            thread.join(timeout=sampling_timeout)
            if thread.is_alive() and self.driver is not None:
                try:
                    self.driver.close()
                except Exception:
                    pass
                thread.join(timeout=force_timeout)
            if thread.is_alive():
                self.last_error = (
                    self.last_error
                    or "stop_timeout: 采集接口未能及时停止，工作文件已保留，尚未生成最终层文件"
                )
                self.capture_phase = "stop_timed_out"
                return self.status()
        if isinstance(self.driver, MultiInterfaceDriver):
            usb_stop = any(isinstance(worker.driver, Esr1001UsbDriver) for worker in self.driver.workers)
            close_budget = (self.config.stop_force_timeout_seconds if self.config else 2.0) if usb_stop else 0.5
            self.driver.close(timeout_seconds=close_budget)
            pending_workers = self.driver.worker_status()
            if pending_workers:
                self.last_error = "stop_timeout: " + ", ".join(
                    str(item.get("interface_id") or "unknown")
                    for item in pending_workers
                )
                self.finalization_complete = False
                self.capture_phase = "stop_timed_out"
                return self.status()
        usb_errors = []
        if isinstance(self.driver, MultiInterfaceDriver):
            for evidence in self.driver.interface_quality_metadata.values():
                if evidence.get("driver") in {USB_DRIVER, "m3232_pressure"}:
                    if evidence.get("writer_error"):
                        usb_errors.append(str(evidence["writer_error"]))
                    if evidence.get("stop_error"):
                        usb_errors.append(str(evidence["stop_error"]))
                    if evidence.get("saved_source_rows") != evidence.get("valid_frames" if evidence.get("driver") == "m3232_pressure" else "source_frames"):
                        usb_errors.append("raw_source_row_mismatch: 源帧与原始文件行数不一致")
                    if evidence.get("driver") == "m3232_pressure" and evidence.get("saved_matrix_csv_rows") != evidence.get("valid_frames"):
                        usb_errors.append("matrix_csv_row_mismatch: 源帧与完整矩阵CSV行数不一致")
        if usb_errors:
            self.persistence_failed = True
        self.capture_phase = "finalizing"
        with self.lock:
            if self.finalization_complete:
                self.capture_phase = "completed"
                return self.status()
            if self.config is None:
                self.capture_saved = False
                self.finalization_complete = True
                self.capture_phase = "completed"
                return self.status()
            mysql_destinations = self._mysql_destinations(self.config)
            if not self.save_enabled and not mysql_destinations:
                self.capture_saved = False
                self.finalization_complete = True
                self.capture_phase = "completed"
                return self.status()
            # CSV output is optional.  When its directory is empty or
            # unavailable, still finalize an enabled MySQL destination from
            # the in-memory rows collected during this run.
            if not self.save_enabled and self.session_dir is None:
                rows = list(self.rows)
                if self.total_sample_count != len(rows):
                    self.last_error = (
                        "buffer_reconciliation_failed: in-memory row count "
                        f"{len(rows)} does not match captured sample count "
                        f"{self.total_sample_count}; database save was not attempted"
                    )
                    self.mysql_status = {
                        "enabled": True,
                        "ok": False,
                        "state": "incomplete_buffer",
                        "saved_rows": 0,
                        "error": self.last_error,
                    }
                    self.capture_saved = False
                    self.finalization_complete = False
                    self.capture_phase = "finalization_failed"
                    return self.status()
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
                database_pending_retry = bool(failed)
                self.finalization_complete = not database_pending_retry
                self.capture_phase = (
                    "database_pending_retry"
                    if database_pending_retry
                    else "completed"
                )
                return self.status()
            if self.session_dir is not None and self.config is not None:
                intended_raw_path = self.raw_path
                specimen_folder_name = (
                    self.specimen_folder_name or _parameter_token(self.config)
                )
                has_valid_data = bool(
                    self.total_sample_count > 0
                    and not self.persistence_failed
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
                                getattr(self, "matrix_work_path", None),
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
                if self.capture_saved and self.capture_record_dir is not None:
                    quality_stem = (
                        intended_raw_path.stem
                        if intended_raw_path is not None
                        else "未保存"
                    )
                    self.quality_sidecar_path = self.capture_record_dir / (
                        f"{quality_stem}_{self.session_stamp}_质量.json"
                    )
                    _atomic_write_json(
                        self.quality_sidecar_path,
                        {
                            "schema_version": 1,
                            "capture_uuid": self.capture_uuid,
                            "sample_count": int(self.total_sample_count),
                            "absolute_sequence_first": (
                                0 if self.total_sample_count else None
                            ),
                            "absolute_sequence_last": (
                                int(self.total_sample_count) - 1
                                if self.total_sample_count
                                else None
                            ),
                            "channel_units": {
                                name: str(
                                    (SENSOR_CHANNEL_METADATA.get(name) or {}).get("unit")
                                    or ""
                                )
                                for name in (self.config.selected_sensors or [])
                            },
                            "data_quality": data_quality,
                        },
                    )
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
                    "quality_sidecar": (
                        _file_integrity(self.quality_sidecar_path)
                        if self.quality_sidecar_path is not None
                        and self.quality_sidecar_path.is_file()
                        else None
                    ),
                    "matrix_csv": _file_integrity(self.matrix_path) if self.capture_saved and getattr(self, "matrix_path", None) and self.matrix_path.is_file() else None,
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
                    "quality_sidecar": (
                        str(self.quality_sidecar_path)
                        if self.quality_sidecar_path is not None
                        else None
                    ),
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
                database_pending_retry = bool(
                    mysql_destinations and not self.mysql_status.get("ok")
                )
                self.finalization_complete = not (
                    self.persistence_failed or database_pending_retry
                )
                if self.persistence_failed:
                    self.capture_phase = "finalization_failed"
                elif database_pending_retry:
                    self.capture_phase = "database_pending_retry"
                else:
                    self.capture_phase = "completed"
        if usb_errors:
            self.last_error = "; ".join(usb_errors)
            self.capture_phase = "finalization_failed"
            self.finalization_complete = False
        return self.status()

    def esr_source_evidence_since(self, cursors, limit=200):
        sources = dict(getattr(self.driver, "interface_quality_metadata", {}) or {})
        for worker in list(getattr(self.driver, "workers", [])):
            if isinstance(worker.driver, Esr1001UsbDriver) and worker.driver.recorder:
                writer = worker.driver.recorder
                sources[worker.interface_id] = dict(worker.driver.quality_metadata, raw_file=str(writer.path), saved_source_rows=writer.rows)
        output, updated, remaining = [], dict(cursors), 0
        for identity, evidence in sources.items():
            if evidence.get("driver") != USB_DRIVER or not evidence.get("raw_file"):
                continue
            start = int(cursors.get(identity, 0))
            count = int(evidence.get("saved_source_rows", 0))
            with Path(evidence["raw_file"]).open(encoding="utf-8-sig", newline="") as stream:
                for index, row in enumerate(csv.DictReader(stream)):
                    if index >= count or len(output) >= limit:
                        break
                    if index < start:
                        continue
                    output.append({"interface_id": identity, "record": row,
                                   "measurement_context": deepcopy(evidence.get("measurement_context") or {}),
                                   "calibration_snapshot": evidence.get("calibration_snapshot"),
                                   "capture_generation": evidence.get("capture_generation")})
                    updated[identity] = index + 1
            remaining += max(0, count - int(updated.get(identity, 0)))
        return {"records": output, "cursors": updated, "remaining": remaining}

    def esr_numeric_action(self, payload):
        action = payload.get("action")
        if action == "candidate":
            return {"ok": True, "candidate": read_vendor_candidate(payload["path"])}
        if action == "confirm":
            profile = confirm_vendor_candidate(payload["candidate"], payload["confirmation"])
            load_calibration(profile, binding=profile["device_binding"], parameters={"gain_code": profile["gain_code"]}, bridge=profile["bridge"])
            return {"ok": True, "profile": profile}
        with self.lifecycle_lock:
            if not self.thread or not self.thread.is_alive() or self.config.acquisition_mode != "real":
                raise ValueError("清零仅适用于当前正在采集的真实会话")
            workers = getattr(self.driver, "workers", [])
            worker = next((w for w in workers if w.interface_id == payload.get("interface_id") and isinstance(w.driver, Esr1001UsbDriver)), None)
            if worker is None or worker.state != "running":
                raise ValueError("当前ESR接口未运行")
            driver = worker.driver
            if payload.get("capture_generation") != driver.capture_generation or payload.get("connection_generation") != driver.connection_generation or payload.get("calibration_digest") != driver.calibration_digest:
                raise ValueError("清零操作的会话、连接或标定已变化，请刷新当前状态")
            if action == "zero":
                with driver._io_lock:
                    zero = driver.set_zero(expected_sequence=(driver.latest_record or {}).get("receive_sequence"),
                        connection_generation=payload["connection_generation"], capture_generation=payload["capture_generation"], calibration_digest=payload["calibration_digest"])
                return {"ok": True, "zero": zero}
            if action == "clear_zero":
                driver.clear_zero()
                return {"ok": True}
            raise ValueError("未知ESR数值操作")

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
                elif running and (age is None or age > (0.25 if name in RAW_CHANNELS or name in STRAIN_SENSOR_COLUMNS else 2.0)):
                    sensor_state = "stale"
                elif not running:
                    sensor_state = "historical"
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
                        "historical": "已停止；保留历史采集证据，不代表当前在线",
                    }[sensor_state],
                    "blocking": False,
                    "ok": sensor_state in {"not_selected", "ok"},
                })
            sensor_by_name = {item["name"]: item for item in sensors}
            interfaces: list[dict[str, Any]] = []
            worker_by_interface: dict[str, dict[str, Any]] = {}
            worker_status = getattr(self.driver, "worker_status", None)
            if callable(worker_status):
                try:
                    worker_by_interface = {
                        str(item.get("interface_id") or ""): dict(item)
                        for item in (worker_status() or [])
                        if isinstance(item, dict) and item.get("interface_id")
                    }
                except Exception:
                    worker_by_interface = {}
            interrupted = [key for key, value in worker_by_interface.items()
                           if int(value.get("reconnect_attempts") or 0) > 0 or value.get("state") == "failed"]
            if interrupted:
                self.invalidate_readiness(interrupted, reason="interface_reconnect_or_failure")
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
                        if not running:
                            state, message = "stopped", "接口已停止；以下计数与数据为历史会话证据"
                        elif healthy:
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
                    adapter_status = worker_by_interface.get(interface_id, {})
                    adapter_state = str(adapter_status.get("state") or "")
                    usb_evidence = adapter_status.get("driver_quality") or (getattr(self.driver, "interface_quality_metadata", {}) or {}).get(interface_id, {})
                    if item.get("driver") == USB_DRIVER and usb_evidence:
                        frames = int(usb_evidence.get("source_frames") or 0)
                        latest = usb_evidence.get("latest_record") or {}
                        frame_age = max(0.0, time.monotonic() - float(latest.get("host_monotonic") or 0)) if latest else None
                        message += f"；源帧 {frames}；单位 {usb_evidence.get('unit')}"
                        source_rate = usb_evidence.get("actual_source_rate_hz")
                        message += f"；实际帧率 {source_rate:.2f} Hz" if source_rate is not None else "；实际帧率待确认"
                        message += f"；帧龄 {frame_age:.3f}秒" if frame_age is not None else "；尚无新鲜帧"
                        message += f"；{usb_evidence.get('calibration_state')}"
                        if running and frame_age is not None and frame_age > 0.25:
                            state = "stale"
                        if usb_evidence.get("overflows") or usb_evidence.get("writer_error"):
                            state = "usb_overflow"
                    if item.get("driver") == "m3232_pressure" and usb_evidence:
                        message += f"；{usb_evidence.get('baudrate')}/8N1；{usb_evidence.get('matrix_shape')}；{usb_evidence.get('unit')}；有效矩阵 {usb_evidence.get('valid_frames')}；拒绝 {usb_evidence.get('rejected_frames')}"
                        if not usb_evidence.get("calibrated"): message += "；原始响应未标定"
                    if adapter_state in {"failed", "stop_timed_out"}:
                        state = adapter_state
                        message = str(adapter_status.get("last_error") or adapter_state)
                    interfaces.append({
                        "id": interface_id, "role": item.get("role", "custom"),
                        "driver": item.get("driver", ""), "endpoint": item.get("endpoint", ""),
                        "enabled": enabled, "expected_channels": expected,
                        "detected_channels": detected, "missing_channels": missing,
                        "stale_channels": stale, "auxiliary_only": auxiliary_only,
                        "state": state, "message": message,
                        "ok": state in {"disabled", "video_only", "no_channels", "ok"},
                        "fault_domain": adapter_status.get("fault_domain"),
                        "adapter_state": adapter_state or None,
                        "reconnect_attempts": int(adapter_status.get("reconnect_attempts") or 0),
                        "last_error": str(adapter_status.get("last_error") or ""),
                        "poll_interval_seconds": adapter_status.get("actual_poll_interval_seconds"),
                        "start_events": adapter_status.get("start_events", []),
                        "usb_evidence": usb_evidence if item.get("driver") == USB_DRIVER else None,
                        "m3232_evidence": usb_evidence if item.get("driver") == "m3232_pressure" else None,
                        "raw_file": adapter_status.get("raw_file") or usb_evidence.get("raw_file"),
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
                "status_monotonic": time.monotonic(),
                "start_operation": self.start_operation_status(),
                "capture_generation": self._start_job.get("capture_generation") or getattr(self.config, "capture_generation", ""),
                "start_request_id": self._start_job.get("start_request_id") or getattr(self.config, "start_request_id", ""),
                "capture_timeline": self.capture_timeline(),
                "start_preflight": deepcopy(self._last_start_preflight),
                "readiness_session_id": self.readiness_session_id,
                "readiness_version": self.readiness_version,
                "readiness_invalid_reason": self.readiness_invalid_reason,
                "readiness_session_valid": bool(self._readiness_snapshot is not None and self._session_readiness_fingerprint),
                "sample_count": int(self.total_sample_count),
                "online_buffer_rows": len(self.rows),
                "capture_uuid": self.capture_uuid or None,
                "capture_saved": self.capture_saved,
                "capture_phase": self.capture_phase,
                "finalization_complete": self.finalization_complete,
                "persistence_failed": self.persistence_failed,
                "persistence_queue_depth": self.persistence_queue_depth,
                "persistence_queue_high_water_rows": self.persistence_queue_high_water_rows,
                "database_pending_retry": bool(
                    self.mysql_status.get("enabled")
                    and not self.mysql_status.get("ok")
                    and self.mysql_status.get("state") in {
                        "pending", "not_saved", "preflight_failed"
                    }
                ),
                "save_enabled": self.save_enabled,
                "save_status": dict(self.save_status),
                "persistence_policy": (
                    self.config.persistence_policy if self.config else None
                ),
                "production_conclusion_enabled": bool(
                    self.config is not None
                    and self.config.persistence_policy != "engineering_preview"
                ),
                "memory_buffer_limit_rows": self.rows.maxlen,
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
                "matrix_csv_file": str(self.matrix_path) if getattr(self, "matrix_path", None) and self.capture_saved else None,
                "matrix_csv_work_file": str(self.matrix_work_path) if getattr(self, "matrix_work_path", None) and running else None,
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
                "quality_sidecar": (
                    str(self.quality_sidecar_path)
                    if self.quality_sidecar_path is not None
                    else None
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
                "persistence_recovery": dict(self.recovery_state),
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
        with self.lifecycle_lock:
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
                self.invalidate_readiness()
                return self.status()

    def numeric_matrix(self) -> tuple[list[dict[str, Any]], list[float]]:
        with self.lock:
            return list(self.rows), list(self.timestamps)

    def live_curve_snapshot(self, limit: int) -> tuple:
        """Copy one generation's bounded display data under the capture lock."""
        limit = max(48, min(int(limit), 2400))
        with self.lock:
            tail = lambda values: list(islice(reversed(values), limit))[::-1]
            return (self.status(), tail(self.rows), tail(self.timestamps), tail(self.frame_quality), tail(self.frame_sources),
                    deepcopy(self._curve_start_observations) if self._curve_start_saved else {})

    def _write_start_observations(self):
        if self.capture_record_dir is not None:
            path = self.capture_record_dir / f"{self.session_stamp}_{self.config.capture_generation}_start_observations.json"
            _atomic_write_json(path, {"capture_uuid": self.capture_uuid, "capture_generation": self.config.capture_generation,
                                     "start_request_id": self.config.start_request_id, "clock_domain": "server_monotonic",
                                     "channels": self._curve_start_observations})

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
            frame_values = list(
                islice(self.frame_envelopes, relative_start, relative_end)
            )
            return {
                "rows": row_values,
                "timestamps": timestamp_values,
                "frames": frame_values,
                "frame_catalog": deepcopy(self.frame_catalog),
                "next_cursor": end,
                "total_count": total,
                "buffer_base": base,
                "truncated": truncated,
            }
