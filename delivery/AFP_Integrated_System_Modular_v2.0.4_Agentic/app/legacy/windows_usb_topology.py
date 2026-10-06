"""Windows USB hub topology discovery.

The rest of the application consumes only the normalized dictionaries returned
by :func:`discover_windows_usb_topology`.  Win32 handles, symbolic links and
USB IOCTL layouts stay inside this module so an unavailable or partially
working hub cannot break the existing serial/network discovery path.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import sys
import uuid
from collections import defaultdict
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
PROVIDER_NAME = "windows_usb_hub_ioctl"

_FILE_DEVICE_UNKNOWN = 0x22
_METHOD_BUFFERED = 0
_FILE_ANY_ACCESS = 0


def _ctl_code(function: int) -> int:
    return (
        (_FILE_DEVICE_UNKNOWN << 16)
        | (_FILE_ANY_ACCESS << 14)
        | (function << 2)
        | _METHOD_BUFFERED
    )


IOCTL_USB_GET_NODE_INFORMATION = _ctl_code(258)
IOCTL_USB_GET_NODE_CONNECTION_NAME = _ctl_code(261)
IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME = _ctl_code(264)
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX = _ctl_code(274)
IOCTL_USB_GET_HUB_INFORMATION_EX = _ctl_code(277)
IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES = _ctl_code(278)
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2 = _ctl_code(279)

GUID_DEVINTERFACE_USB_HUB = uuid.UUID("f18a0e88-c30c-11d0-8815-00a0c906bed8")

_DIGCF_PRESENT = 0x00000002
_DIGCF_ALLCLASSES = 0x00000004
_DIGCF_DEVICEINTERFACE = 0x00000010
_ERROR_NO_MORE_ITEMS = 259
_ERROR_INSUFFICIENT_BUFFER = 122
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080

_SPDRP_DEVICEDESC = 0
_SPDRP_HARDWAREID = 1
_SPDRP_SERVICE = 4
_SPDRP_CLASS = 7
_SPDRP_CLASSGUID = 8
_SPDRP_DRIVER = 9
_SPDRP_MFG = 11
_SPDRP_FRIENDLYNAME = 12
_SPDRP_LOCATION_INFORMATION = 13
_SPDRP_ENUMERATOR_NAME = 22
_SPDRP_LOCATION_PATHS = 35

_CR_SUCCESS = 0
_CM_LOCATE_DEVNODE_NORMAL = 0

_CONNECTION_STATUS = {
    0: "empty",
    1: "occupied",
    2: "error",
    3: "error",
    4: "error",
    5: "error",
    6: "error",
    7: "error",
    8: "enumerating",
    9: "enumerating",
}


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_uuid(cls, value: uuid.UUID) -> "_GUID":
        raw = value.bytes_le
        return cls.from_buffer_copy(raw)


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", _GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


class USB_DEVICE_DESCRIPTOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bcdUSB", wintypes.WORD),
        ("bDeviceClass", ctypes.c_ubyte),
        ("bDeviceSubClass", ctypes.c_ubyte),
        ("bDeviceProtocol", ctypes.c_ubyte),
        ("bMaxPacketSize0", ctypes.c_ubyte),
        ("idVendor", wintypes.WORD),
        ("idProduct", wintypes.WORD),
        ("bcdDevice", wintypes.WORD),
        ("iManufacturer", ctypes.c_ubyte),
        ("iProduct", ctypes.c_ubyte),
        ("iSerialNumber", ctypes.c_ubyte),
        ("bNumConfigurations", ctypes.c_ubyte),
    ]


class USB_HUB_DESCRIPTOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("bDescriptorLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bNumberOfPorts", ctypes.c_ubyte),
        ("wHubCharacteristics", wintypes.WORD),
        ("bPowerOnToPowerGood", ctypes.c_ubyte),
        ("bHubControlCurrent", ctypes.c_ubyte),
        ("bRemoveAndPowerMask", ctypes.c_ubyte * 64),
    ]


class USB_30_HUB_DESCRIPTOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bNumberOfPorts", ctypes.c_ubyte),
        ("wHubCharacteristics", wintypes.WORD),
        ("bPowerOnToPowerGood", ctypes.c_ubyte),
        ("bHubControlCurrent", ctypes.c_ubyte),
        ("bHubHdrDecLat", ctypes.c_ubyte),
        ("wHubDelay", wintypes.WORD),
        ("DeviceRemovable", wintypes.WORD),
    ]


class _USB_HUB_DESCRIPTOR_UNION(ctypes.Union):
    _pack_ = 1
    _fields_ = [
        ("UsbHubDescriptor", USB_HUB_DESCRIPTOR),
        ("Usb30HubDescriptor", USB_30_HUB_DESCRIPTOR),
    ]


class USB_HUB_INFORMATION_EX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("HubType", wintypes.DWORD),
        ("HighestPortNumber", wintypes.WORD),
        ("u", _USB_HUB_DESCRIPTOR_UNION),
    ]


class USB_NODE_CONNECTION_INFORMATION_EX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ConnectionIndex", wintypes.DWORD),
        ("DeviceDescriptor", USB_DEVICE_DESCRIPTOR),
        ("CurrentConfigurationValue", ctypes.c_ubyte),
        ("Speed", ctypes.c_ubyte),
        ("DeviceIsHub", ctypes.c_ubyte),
        ("DeviceAddress", wintypes.WORD),
        ("NumberOfOpenPipes", wintypes.DWORD),
        ("ConnectionStatus", wintypes.DWORD),
    ]


class USB_NODE_CONNECTION_INFORMATION_EX_V2(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ConnectionIndex", wintypes.DWORD),
        ("Length", wintypes.DWORD),
        ("SupportedUsbProtocols", wintypes.DWORD),
        ("Flags", wintypes.DWORD),
    ]


class USB_PORT_CONNECTOR_PROPERTIES(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ConnectionIndex", wintypes.DWORD),
        ("ActualLength", wintypes.DWORD),
        ("UsbPortProperties", wintypes.DWORD),
        ("CompanionIndex", wintypes.WORD),
        ("CompanionPortNumber", wintypes.WORD),
        ("CompanionHubSymbolicLinkName", wintypes.WCHAR * 1),
    ]


@dataclass(frozen=True)
class _DeviceRecord:
    instance_id: str
    parent_instance_id: str
    friendly_name: str
    description: str
    manufacturer: str
    driver_key: str
    service: str
    class_name: str
    class_guid: str
    hardware_ids: tuple[str, ...]
    location_paths: tuple[str, ...]
    location_info: str
    network_name: str = ""


def _empty_topology(state: str, provider: str, errors: Iterable[str] = ()) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "provider": provider,
        "errors": [str(item) for item in errors if str(item)],
        "docks": [],
        "usb_ports": [],
        "devices": [],
    }


def _stable_id(prefix: str, *parts: object) -> str:
    normalized = "|".join(str(part or "").strip().lower() for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _vid_pid(value: str) -> tuple[int | None, int | None]:
    match = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", value or "", re.I)
    if not match:
        return None, None
    return int(match.group(1), 16), int(match.group(2), 16)


def _canonical_link(value: str) -> str:
    text = str(value or "").replace("/", "\\").strip().lower()
    for prefix in ("\\\\?\\", "\\??\\", "??\\"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.rstrip("\\")


def _common_location(paths: Sequence[str]) -> str:
    if not paths:
        return ""
    parts = [str(path).split("#") for path in paths if path]
    if not parts:
        return ""
    common: list[str] = []
    for values in zip(*parts):
        if len({item.lower() for item in values}) != 1:
            break
        common.append(values[0])
    return "#".join(common) or min(paths, key=len)


class _UnionFind:
    def __init__(self, keys: Iterable[tuple[str, int]]) -> None:
        self.parent = {key: key for key in keys}

    def find(self, key: tuple[str, int]) -> tuple[str, int]:
        parent = self.parent[key]
        if parent != key:
            self.parent[key] = self.find(parent)
        return self.parent[key]

    def union(self, left: tuple[str, int], right: tuple[str, int]) -> None:
        if left not in self.parent or right not in self.parent:
            return
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _trusted_genesys_companions(raw_hubs: list[dict[str, Any]]) -> None:
    usb2 = [hub for hub in raw_hubs if (hub.get("vid"), hub.get("pid")) == (0x05E3, 0x0610)]
    usb3 = [hub for hub in raw_hubs if (hub.get("vid"), hub.get("pid")) == (0x05E3, 0x0626)]
    # Modern Windows normally supplies the companion link through
    # IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES.  This fallback is deliberately
    # limited to one unambiguous known pair; with two identical docks there is
    # no safe basis for guessing which USB2 and USB3 hubs belong together.
    if len(usb2) != 1 or len(usb3) != 1:
        return
    for slow in usb2:
        candidates = [
            fast for fast in usb3
            if fast.get("parent_instance_id") == slow.get("parent_instance_id")
            or (len(usb2) == 1 and len(usb3) == 1)
        ]
        if len(candidates) != 1:
            continue
        fast = candidates[0]
        slow_ports = {int(item["number"]): item for item in slow.get("ports", [])}
        fast_ports = {int(item["number"]): item for item in fast.get("ports", [])}
        common = sorted(set(slow_ports) & set(fast_ports))
        if len(common) < 3:
            continue
        for number in common:
            slow_port = slow_ports[number]
            fast_port = fast_ports[number]
            if slow_port.get("companion") or fast_port.get("companion"):
                continue
            slow_port["trusted_companion"] = {"hub_id": fast["id"], "port_number": number}
            fast_port["trusted_companion"] = {"hub_id": slow["id"], "port_number": number}


def normalize_usb_topology(
    raw_hubs: Sequence[Mapping[str, Any]],
    *,
    errors: Iterable[str] = (),
    known_host_port_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Normalize raw hub IOCTL records into docks, connectors and devices."""

    error_list = [str(item) for item in errors if str(item)]
    known_host_ports = {str(item) for item in known_host_port_ids if str(item)}
    hubs = [{**hub, "ports": [dict(port) for port in hub.get("ports", [])]} for hub in raw_hubs]
    root_hubs = [hub for hub in hubs if bool(hub.get("is_root"))]
    public_hubs = [hub for hub in hubs if not bool(hub.get("is_root"))]

    hub_by_id = {str(hub["id"]): hub for hub in public_hubs}
    path_to_hub = {
        _canonical_link(str(hub.get("device_path") or "")): str(hub["id"])
        for hub in public_hubs
        if hub.get("device_path")
    }
    port_keys = [
        (str(hub["id"]), int(port["number"]))
        for hub in public_hubs
        for port in hub.get("ports", [])
    ]
    union = _UnionFind(port_keys)
    for hub in public_hubs:
        hub_id = str(hub["id"])
        for port in hub.get("ports", []):
            companion = port.get("companion") or port.get("trusted_companion") or {}
            companion_hub = str(companion.get("hub_id") or "")
            if not companion_hub and companion.get("hub_path"):
                companion_hub = path_to_hub.get(_canonical_link(str(companion["hub_path"])), "")
            companion_port = int(companion.get("port_number") or 0)
            if companion_hub and companion_port:
                union.union((hub_id, int(port["number"])), (companion_hub, companion_port))

    connector_members: dict[tuple[str, int], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for hub in public_hubs:
        for port in hub.get("ports", []):
            key = (str(hub["id"]), int(port["number"]))
            connector_members[union.find(key)].append((str(hub["id"]), port))

    hub_links: dict[str, set[str]] = {str(hub["id"]): set() for hub in public_hubs}
    for members in connector_members.values():
        member_hubs = {hub_id for hub_id, _ in members}
        for hub_id in member_hubs:
            hub_links[hub_id].update(member_hubs - {hub_id})

    components: list[set[str]] = []
    remaining = set(hub_by_id)
    while remaining:
        seed = remaining.pop()
        component = {seed}
        pending = [seed]
        while pending:
            current = pending.pop()
            for neighbor in hub_links.get(current, set()):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    pending.append(neighbor)
        components.append(component)

    components.sort(key=lambda group: sorted(group))
    dock_by_hub: dict[str, str] = {}
    docks: list[dict[str, Any]] = []
    for component in components:
        component_hubs = [hub_by_id[item] for item in sorted(component)]
        locations = [path for hub in component_hubs for path in hub.get("location_paths", []) if path]
        identity = _common_location(locations) or "|".join(sorted(component))
        fingerprint = ",".join(
            f"{int(hub.get('vid') or 0):04x}:{int(hub.get('pid') or 0):04x}"
            for hub in component_hubs
        )
        dock_id = _stable_id("dock", identity, fingerprint)
        for hub_id in component:
            dock_by_hub[hub_id] = dock_id
        docks.append({
            "id": dock_id,
            "label": "",
            "state": "connected",
            "upstream_location": _stable_id("location", identity),
            "hub_refs": [_stable_id("hub", item) for item in sorted(component)],
            "hub_vid_pids": [
                {"vid": hub.get("vid"), "pid": hub.get("pid")}
                for hub in component_hubs
            ],
        })

    docks.sort(key=lambda item: item["id"])
    for index, dock in enumerate(docks, 1):
        dock["label"] = f"拓展坞 {index}"
    dock_records = {item["id"]: item for item in docks}

    ports: list[dict[str, Any]] = []
    devices: list[dict[str, Any]] = []
    device_ids: set[str] = set()
    port_id_by_member: dict[tuple[str, int], str] = {}
    for members in connector_members.values():
        member_hubs = {hub_id for hub_id, _ in members}
        dock_ids = {dock_by_hub[hub_id] for hub_id in member_hubs if hub_id in dock_by_hub}
        if len(dock_ids) != 1:
            continue
        dock_id = next(iter(dock_ids))
        active = [port for _, port in members if str(port.get("state")) == "occupied"]
        candidate = active[0] if active else members[0][1]
        protocols = sorted({
            protocol
            for _, port in members
            for protocol in port.get("supported_protocols", [])
            if protocol
        })
        states = {str(port.get("state") or "unknown") for _, port in members}
        state = (
            "occupied" if "occupied" in states
            else "enumerating" if "enumerating" in states
            else "error" if "error" in states
            else "disabled" if "disabled" in states
            else "empty" if "empty" in states
            else "unknown"
        )
        member_identity = ",".join(
            f"{int(hub_by_id[hub].get('vid') or 0):04x}:"
            f"{int(hub_by_id[hub].get('pid') or 0):04x}:{int(port['number'])}"
            for hub, port in sorted(members, key=lambda x: (x[0], int(x[1]["number"])))
        )
        port_id = _stable_id("usbport", dock_id, member_identity)
        for hub_id, member_port in members:
            port_id_by_member[(hub_id, int(member_port["number"]))] = port_id
        user_values = [port.get("user_connectable") for _, port in members if port.get("user_connectable") is not None]
        user_connectable = any(bool(value) for value in user_values) if user_values else True
        device = candidate.get("device") if isinstance(candidate.get("device"), Mapping) else None
        internal_function = ""
        component_hubs = [hub_by_id[item] for item in member_hubs]
        hub_pairs = {(hub.get("vid"), hub.get("pid")) for hub in component_hubs}
        if (
            device
            and (device.get("vid"), device.get("pid")) == (0x0B95, 0x1790)
            and (0x05E3, 0x0610) in hub_pairs
            and (0x05E3, 0x0626) in hub_pairs
        ):
            user_connectable = False
            internal_function = "ethernet"
        elif user_values and not any(bool(value) for value in user_values):
            internal_function = str(device.get("class") or "internal") if device else "internal"

        device_id: str | None = None
        if device:
            device_id = _stable_id(
                "usbdev",
                device.get("instance_id") or "",
                port_id,
                device.get("vid"),
                device.get("pid"),
                device.get("serial") or "",
            )
            if device_id not in device_ids:
                device_ids.add(device_id)
                devices.append({
                    "id": device_id,
                    "parent_port_id": port_id,
                    "class": str(device.get("class") or "usb"),
                    "friendly_name": str(device.get("friendly_name") or device.get("description") or "USB 设备"),
                    "vid": device.get("vid"),
                    "pid": device.get("pid"),
                    "serial": str(device.get("serial") or ""),
                    "live_interface_id": str(device.get("live_interface_id") or ""),
                    "network_name": str(device.get("network_name") or ""),
                    "instance_key": _stable_id("pnp", device.get("instance_id") or "") if device.get("instance_id") else "",
                    "location_keys": [
                        _stable_id("location", path)
                        for path in device.get("location_paths", [])
                        if path
                    ],
                })

        port_numbers = [int(port["number"]) for _, port in members]
        display_number = min(port_numbers)
        connector_values = [
            port.get("connector_is_type_c")
            for _, port in members
            if port.get("connector_is_type_c") is not None
        ]
        connector_form_factor = (
            "type_c" if any(value is True for value in connector_values)
            else "type_a" if connector_values
            else "unknown"
        )
        ports.append({
            "id": port_id,
            "owner_kind": "dock",
            "dock_id": dock_id,
            "system_port_number": display_number,
            "connector_type": "usb3" if "usb3" in protocols else "usb2",
            "connector_form_factor": connector_form_factor,
            "supported_protocols": protocols or ["usb2"],
            "state": state,
            "user_connectable": bool(user_connectable),
            "device_id": device_id,
            "internal_function": internal_function,
            "merge_state": "companion" if len(members) > 1 else "independent",
        })

    # Root hubs represent connectors owned by the computer.  Some firmware
    # marks unpopulated or un-routed controller ports as user-connectable, so
    # that flag alone cannot prove a chassis connector exists.  Current or
    # historical occupation is evidence; an explicit USB2/SuperSpeed companion
    # pair is also sufficient evidence for an empty USB-A chassis connector.
    root_by_id = {str(hub["id"]): hub for hub in root_hubs}
    root_path_to_id = {
        _canonical_link(str(hub.get("device_path") or "")): str(hub["id"])
        for hub in root_hubs
        if hub.get("device_path")
    }
    downstream_paths = {
        _canonical_link(str(hub.get("device_path") or ""))
        for hub in public_hubs
        if hub.get("device_path")
    }
    downstream_instance_ids = {
        str(hub.get("instance_id") or "").strip().upper()
        for hub in public_hubs
        if hub.get("instance_id")
    }
    root_keys = [
        (str(hub["id"]), int(port["number"]))
        for hub in root_hubs
        if hub.get("location_paths")
        for port in hub.get("ports", [])
    ]
    root_union = _UnionFind(root_keys)
    for hub in root_hubs:
        if not hub.get("location_paths"):
            continue
        hub_id = str(hub["id"])
        for port in hub.get("ports", []):
            companion = port.get("companion") or {}
            companion_hub = str(companion.get("hub_id") or "")
            if not companion_hub and companion.get("hub_path"):
                companion_hub = root_path_to_id.get(
                    _canonical_link(str(companion.get("hub_path") or "")), ""
                )
            companion_port = int(companion.get("port_number") or 0)
            if companion_hub and companion_port:
                root_union.union(
                    (hub_id, int(port["number"])),
                    (companion_hub, companion_port),
                )

    root_connectors: dict[tuple[str, int], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for hub in root_hubs:
        if not hub.get("location_paths"):
            continue
        for port in hub.get("ports", []):
            key = (str(hub["id"]), int(port["number"]))
            root_connectors[root_union.find(key)].append((str(hub["id"]), port))

    for members in root_connectors.values():
        if not any(port.get("user_connectable") is True for _, port in members):
            continue
        dock_upstream = any(
            _canonical_link(str(port.get("child_hub_path") or "")) in downstream_paths
            for _, port in members
            if port.get("child_hub_path")
        ) or any(
            str((port.get("device") or {}).get("instance_id") or "").strip().upper()
            in downstream_instance_ids
            for _, port in members
            if isinstance(port.get("device"), Mapping)
        )
        member_identity = ",".join(
            f"{hub_id}:{int(port['number'])}"
            for hub_id, port in sorted(members, key=lambda value: (value[0], int(value[1]["number"])))
        )
        locations = [
            path
            for hub_id, _ in members
            for path in root_by_id[hub_id].get("location_paths", [])
            if path
        ]
        port_id = _stable_id("usbport", "host", _common_location(locations), member_identity)
        currently_observed = any(
            str(port.get("state") or "") == "occupied" for _, port in members
        )
        connector_values = [
            port.get("connector_is_type_c")
            for _, port in members
            if port.get("connector_is_type_c") is not None
        ]
        connector_form_factor = (
            "type_c" if any(value is True for value in connector_values)
            else "type_a" if connector_values
            else "unknown"
        )
        companion_confirmed_usb_a = connector_form_factor == "type_a" and len(members) > 1
        historically_observed = port_id in known_host_ports
        if not currently_observed and not historically_observed and not companion_confirmed_usb_a:
            continue
        active = [port for _, port in members if str(port.get("state")) == "occupied"]
        candidate = active[0] if active else members[0][1]
        protocols = sorted({
            protocol
            for _, port in members
            for protocol in port.get("supported_protocols", [])
            if protocol
        })
        states = {str(port.get("state") or "unknown") for _, port in members}
        state = (
            "occupied" if "occupied" in states
            else "enumerating" if "enumerating" in states
            else "error" if "error" in states
            else "disabled" if "disabled" in states
            else "empty" if "empty" in states
            else "unknown"
        )
        device = candidate.get("device") if isinstance(candidate.get("device"), Mapping) else None
        device_id: str | None = None
        if device:
            device_id = _stable_id(
                "usbdev",
                device.get("instance_id") or "",
                port_id,
                device.get("vid"),
                device.get("pid"),
                device.get("serial") or "",
            )
            if device_id not in device_ids:
                device_ids.add(device_id)
                devices.append({
                    "id": device_id,
                    "parent_port_id": port_id,
                    "class": str(device.get("class") or "usb"),
                    "friendly_name": str(device.get("friendly_name") or device.get("description") or "USB 设备"),
                    "vid": device.get("vid"),
                    "pid": device.get("pid"),
                    "serial": str(device.get("serial") or ""),
                    "live_interface_id": str(device.get("live_interface_id") or ""),
                    "network_name": str(device.get("network_name") or ""),
                    "instance_key": _stable_id("pnp", device.get("instance_id") or "") if device.get("instance_id") else "",
                    "location_keys": [
                        _stable_id("location", path)
                        for path in device.get("location_paths", [])
                        if path
                    ],
                })
        numbers = [int(port["number"]) for _, port in members]
        ports.append({
            "id": port_id,
            "owner_kind": "host",
            "dock_id": "",
            "system_port_number": min(numbers),
            "connector_type": "usb3" if "usb3" in protocols else "usb2",
            "connector_form_factor": connector_form_factor,
            "supported_protocols": protocols or ["usb2"],
            "state": state,
            "user_connectable": True,
            "confirmation": (
                "observed_current" if currently_observed
                else "observed_history" if historically_observed
                else "connector_metadata"
            ),
            "device_id": device_id,
            "internal_function": "dock_upstream" if dock_upstream else "",
            "merge_state": "companion" if len(members) > 1 else "independent",
        })

    # Preserve nested-hub parentage without exposing the raw symbolic link.
    for parent_hub in public_hubs:
        parent_hub_id = str(parent_hub["id"])
        for raw_port in parent_hub.get("ports", []):
            child_path = _canonical_link(str(raw_port.get("child_hub_path") or ""))
            child_hub_id = path_to_hub.get(child_path, "")
            child_dock_id = dock_by_hub.get(child_hub_id, "")
            parent_port_id = port_id_by_member.get((parent_hub_id, int(raw_port["number"])), "")
            if child_dock_id and parent_port_id and child_dock_id != dock_by_hub.get(parent_hub_id):
                dock_records[child_dock_id]["parent_port_id"] = parent_port_id

    ports.sort(key=lambda item: (item.get("owner_kind") != "host", item["dock_id"], not item["user_connectable"], item["system_port_number"], item["id"]))
    host_counts: dict[str, int] = defaultdict(int)
    external_counts: dict[str, int] = defaultdict(int)
    for port in ports:
        if port.get("owner_kind") == "host":
            form_factor = str(port.get("connector_form_factor") or "unknown")
            host_counts[form_factor] += 1
            connector_label = (
                "Type-C" if form_factor == "type_c"
                else "USB-A" if form_factor == "type_a"
                else str(port["connector_type"]).upper()
            )
            port["label"] = (
                f"电脑本机 · {connector_label}-{host_counts[form_factor]}"
                f"（系统端口 {port['system_port_number']}）"
            )
        elif port["user_connectable"]:
            external_counts[port["dock_id"]] += 1
            port["label"] = (
                f"{dock_records[port['dock_id']]['label']} · "
                f"{str(port['connector_type']).upper()}-{external_counts[port['dock_id']]}"
                f"（系统端口 {port['system_port_number']}）"
            )
        else:
            port["label"] = (
                f"{dock_records[port['dock_id']]['label']} · 内部"
                f"{port.get('internal_function') or '功能'}"
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "state": "partial" if error_list else "complete",
        "provider": PROVIDER_NAME,
        "errors": error_list,
        "docks": docks,
        "usb_ports": ports,
        "devices": sorted(devices, key=lambda item: item["id"]),
    }


def _host_port_registry_path() -> Path | None:
    override = str(os.environ.get("AFP_USB_HOST_PORT_REGISTRY") or "").strip()
    if override:
        return Path(override).expanduser()
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        return Path(local_app_data) / "AFP_Integrated_System" / "usb_host_ports.json"
    runtime_dir = str(os.environ.get("AFP_RUNTIME_DIR") or "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "usb_host_ports.json"
    return None


def _load_known_host_port_ids(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    values = payload.get("port_ids", []) if isinstance(payload, Mapping) else []
    return {str(item) for item in values if str(item).startswith("usbport:")}


def _save_known_host_port_ids(path: Path | None, port_ids: Iterable[str]) -> None:
    if path is None:
        return
    values = sorted({str(item) for item in port_ids if str(item).startswith("usbport:")})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"schema_version": 1, "port_ids": values}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        return


class _WindowsApi:
    def __init__(self) -> None:
        if not sys.platform.startswith("win"):
            raise OSError("Windows USB topology is only available on Windows")
        self.setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        self.cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self.setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD
        ]
        self.setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
        self.setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
        self.setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
        self.setupapi.SetupDiEnumDeviceInfo.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(_SP_DEVINFO_DATA)
        ]
        self.setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
        self.setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_SP_DEVINFO_DATA),
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ]
        self.setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        self.setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_SP_DEVINFO_DATA),
        ]
        self.setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
        self.setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_SP_DEVINFO_DATA),
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
        self.setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_SP_DEVINFO_DATA),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_ubyte),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
        self.cfgmgr32.CM_Get_Parent.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.ULONG
        ]
        self.cfgmgr32.CM_Get_Parent.restype = wintypes.DWORD
        self.cfgmgr32.CM_Get_Device_IDW.argtypes = [
            wintypes.DWORD, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG
        ]
        self.cfgmgr32.CM_Get_Device_IDW.restype = wintypes.DWORD
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self.kernel32.DeviceIoControl.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def _last_error(self, operation: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{operation}: {ctypes.FormatError(code).strip()}")

    def _instance_id(self, info_set: int, devinfo: _SP_DEVINFO_DATA) -> str:
        required = wintypes.DWORD()
        self.setupapi.SetupDiGetDeviceInstanceIdW(
            info_set, ctypes.byref(devinfo), None, 0, ctypes.byref(required)
        )
        if not required.value:
            return ""
        buffer = ctypes.create_unicode_buffer(required.value + 1)
        if not self.setupapi.SetupDiGetDeviceInstanceIdW(
            info_set, ctypes.byref(devinfo), buffer, len(buffer), ctypes.byref(required)
        ):
            return ""
        return buffer.value

    def _property(self, info_set: int, devinfo: _SP_DEVINFO_DATA, prop: int) -> tuple[str, ...]:
        required = wintypes.DWORD()
        data_type = wintypes.DWORD()
        self.setupapi.SetupDiGetDeviceRegistryPropertyW(
            info_set,
            ctypes.byref(devinfo),
            prop,
            ctypes.byref(data_type),
            None,
            0,
            ctypes.byref(required),
        )
        if not required.value:
            return ()
        buffer = (ctypes.c_ubyte * required.value)()
        if not self.setupapi.SetupDiGetDeviceRegistryPropertyW(
            info_set,
            ctypes.byref(devinfo),
            prop,
            ctypes.byref(data_type),
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            return ()
        text = ctypes.wstring_at(ctypes.addressof(buffer), required.value // ctypes.sizeof(wintypes.WCHAR))
        return tuple(item for item in text.rstrip("\x00").split("\x00") if item)

    def _parent_id(self, devinst: int) -> str:
        parent = wintypes.DWORD()
        if self.cfgmgr32.CM_Get_Parent(ctypes.byref(parent), devinst, 0) != _CR_SUCCESS:
            return ""
        buffer = ctypes.create_unicode_buffer(4096)
        if self.cfgmgr32.CM_Get_Device_IDW(parent.value, buffer, len(buffer), 0) != _CR_SUCCESS:
            return ""
        return buffer.value

    def enumerate_devices(self) -> dict[str, _DeviceRecord]:
        info_set = self.setupapi.SetupDiGetClassDevsW(
            None, None, None, _DIGCF_PRESENT | _DIGCF_ALLCLASSES
        )
        if info_set == _INVALID_HANDLE_VALUE:
            raise self._last_error("SetupDiGetClassDevsW(all devices)")
        records: dict[str, _DeviceRecord] = {}
        try:
            index = 0
            while True:
                devinfo = _SP_DEVINFO_DATA()
                devinfo.cbSize = ctypes.sizeof(devinfo)
                if not self.setupapi.SetupDiEnumDeviceInfo(info_set, index, ctypes.byref(devinfo)):
                    if ctypes.get_last_error() == _ERROR_NO_MORE_ITEMS:
                        break
                    raise self._last_error("SetupDiEnumDeviceInfo")
                index += 1
                instance_id = self._instance_id(info_set, devinfo)
                if not instance_id:
                    continue
                friendly = self._property(info_set, devinfo, _SPDRP_FRIENDLYNAME)
                description = self._property(info_set, devinfo, _SPDRP_DEVICEDESC)
                records[instance_id.upper()] = _DeviceRecord(
                    instance_id=instance_id,
                    parent_instance_id=self._parent_id(devinfo.DevInst),
                    friendly_name=friendly[0] if friendly else "",
                    description=description[0] if description else "",
                    manufacturer=(self._property(info_set, devinfo, _SPDRP_MFG) or ("",))[0],
                    driver_key=(self._property(info_set, devinfo, _SPDRP_DRIVER) or ("",))[0],
                    service=(self._property(info_set, devinfo, _SPDRP_SERVICE) or ("",))[0],
                    class_name=(self._property(info_set, devinfo, _SPDRP_CLASS) or ("",))[0],
                    class_guid=(self._property(info_set, devinfo, _SPDRP_CLASSGUID) or ("",))[0],
                    hardware_ids=self._property(info_set, devinfo, _SPDRP_HARDWAREID),
                    location_paths=self._property(info_set, devinfo, _SPDRP_LOCATION_PATHS),
                    location_info=(self._property(info_set, devinfo, _SPDRP_LOCATION_INFORMATION) or ("",))[0],
                )
        finally:
            self.setupapi.SetupDiDestroyDeviceInfoList(info_set)
        return _attach_network_names(records)

    def enumerate_hub_interfaces(self) -> list[dict[str, Any]]:
        guid = _GUID.from_uuid(GUID_DEVINTERFACE_USB_HUB)
        info_set = self.setupapi.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None, _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE
        )
        if info_set == _INVALID_HANDLE_VALUE:
            raise self._last_error("SetupDiGetClassDevsW(USB hubs)")
        hubs: list[dict[str, Any]] = []
        try:
            index = 0
            while True:
                interface = _SP_DEVICE_INTERFACE_DATA()
                interface.cbSize = ctypes.sizeof(interface)
                if not self.setupapi.SetupDiEnumDeviceInterfaces(
                    info_set, None, ctypes.byref(guid), index, ctypes.byref(interface)
                ):
                    if ctypes.get_last_error() == _ERROR_NO_MORE_ITEMS:
                        break
                    raise self._last_error("SetupDiEnumDeviceInterfaces")
                index += 1
                required = wintypes.DWORD()
                devinfo = _SP_DEVINFO_DATA()
                devinfo.cbSize = ctypes.sizeof(devinfo)
                self.setupapi.SetupDiGetDeviceInterfaceDetailW(
                    info_set,
                    ctypes.byref(interface),
                    None,
                    0,
                    ctypes.byref(required),
                    ctypes.byref(devinfo),
                )
                if not required.value:
                    continue
                buffer = ctypes.create_string_buffer(required.value)
                ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                if not self.setupapi.SetupDiGetDeviceInterfaceDetailW(
                    info_set,
                    ctypes.byref(interface),
                    buffer,
                    required.value,
                    ctypes.byref(required),
                    ctypes.byref(devinfo),
                ):
                    raise self._last_error("SetupDiGetDeviceInterfaceDetailW")
                hubs.append({
                    "device_path": ctypes.wstring_at(ctypes.addressof(buffer) + 4),
                    "instance_id": self._instance_id(info_set, devinfo),
                })
        finally:
            self.setupapi.SetupDiDestroyDeviceInfoList(info_set)
        return hubs

    def open_hub(self, path: str) -> int:
        handle = self.kernel32.CreateFileW(
            path,
            0,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise self._last_error(f"CreateFileW({path})")
        return int(handle)

    def close_handle(self, handle: int) -> None:
        self.kernel32.CloseHandle(handle)

    def ioctl(self, handle: int, code: int, buffer: ctypes.Array[Any] | ctypes.Structure, size: int | None = None) -> int:
        length = int(size if size is not None else ctypes.sizeof(buffer))
        returned = wintypes.DWORD()
        if not self.kernel32.DeviceIoControl(
            handle,
            code,
            ctypes.byref(buffer),
            length,
            ctypes.byref(buffer),
            length,
            ctypes.byref(returned),
            None,
        ):
            raise self._last_error(f"DeviceIoControl(0x{code:08X})")
        return int(returned.value)


def _attach_network_names(records: Mapping[str, _DeviceRecord]) -> dict[str, _DeviceRecord]:
    if not sys.platform.startswith("win"):
        return dict(records)
    try:
        import winreg
    except ImportError:
        return dict(records)
    result = dict(records)
    network_class = "{4D36E972-E325-11CE-BFC1-08002BE10318}"
    for key, record in records.items():
        if record.class_guid.upper() != network_class or not record.driver_key:
            continue
        try:
            class_path = rf"SYSTEM\CurrentControlSet\Control\Class\{record.driver_key}"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, class_path) as device_key:
                adapter_guid = str(winreg.QueryValueEx(device_key, "NetCfgInstanceId")[0])
            connection_path = rf"SYSTEM\CurrentControlSet\Control\Network\{network_class}\{adapter_guid}\Connection"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, connection_path) as connection_key:
                network_name = str(winreg.QueryValueEx(connection_key, "Name")[0])
        except OSError:
            continue
        result[key] = _DeviceRecord(**{**record.__dict__, "network_name": network_name})
    return result


def _query_dynamic_string(
    api: _WindowsApi,
    handle: int,
    code: int,
    connection_index: int,
) -> str:
    initial = (ctypes.c_ubyte * 8)()
    ctypes.cast(initial, ctypes.POINTER(wintypes.DWORD))[0] = connection_index
    try:
        api.ioctl(handle, code, initial)
    except OSError:
        pass
    actual = int(ctypes.cast(ctypes.byref(initial, 4), ctypes.POINTER(wintypes.DWORD))[0])
    if actual <= 8 or actual > 65536:
        return ""
    buffer = (ctypes.c_ubyte * actual)()
    ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0] = connection_index
    api.ioctl(handle, code, buffer)
    return ctypes.wstring_at(ctypes.addressof(buffer) + 8).rstrip("\x00")


def _query_connector(api: _WindowsApi, handle: int, port_number: int) -> dict[str, Any]:
    base = USB_PORT_CONNECTOR_PROPERTIES()
    base.ConnectionIndex = port_number
    base.CompanionIndex = 0
    try:
        api.ioctl(handle, IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES, base)
    except OSError:
        return {"user_connectable": None, "companion": None}
    actual = int(base.ActualLength or ctypes.sizeof(base))
    if actual > ctypes.sizeof(base) and actual < 65536:
        buffer = (ctypes.c_ubyte * actual)()
        ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0] = port_number
        ctypes.cast(ctypes.byref(buffer, 12), ctypes.POINTER(wintypes.WORD))[0] = 0
        api.ioctl(handle, IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES, buffer)
        props = int(ctypes.cast(ctypes.byref(buffer, 8), ctypes.POINTER(wintypes.DWORD))[0])
        companion_port = int(ctypes.cast(ctypes.byref(buffer, 14), ctypes.POINTER(wintypes.WORD))[0])
        companion_path = ctypes.wstring_at(ctypes.addressof(buffer) + 16).rstrip("\x00") if actual > 18 else ""
    else:
        props = int(base.UsbPortProperties)
        companion_port = int(base.CompanionPortNumber)
        companion_path = str(base.CompanionHubSymbolicLinkName).rstrip("\x00")
    companion = None
    if companion_port and companion_path:
        companion = {"hub_path": companion_path, "port_number": companion_port}
    return {
        "user_connectable": bool(props & 0x1),
        "connector_is_type_c": bool(props & 0x8),
        "companion": companion,
    }


def _query_port(
    api: _WindowsApi,
    handle: int,
    port_number: int,
    device_by_driver: Mapping[str, _DeviceRecord],
) -> dict[str, Any]:
    info = USB_NODE_CONNECTION_INFORMATION_EX()
    info.ConnectionIndex = port_number
    api.ioctl(handle, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX, info)
    status = _CONNECTION_STATUS.get(int(info.ConnectionStatus), "unknown")
    v2 = USB_NODE_CONNECTION_INFORMATION_EX_V2()
    v2.ConnectionIndex = port_number
    v2.Length = ctypes.sizeof(v2)
    v2.SupportedUsbProtocols = 0x7
    try:
        api.ioctl(handle, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2, v2)
        protocol_mask = int(v2.SupportedUsbProtocols)
    except OSError:
        protocol_mask = 0x4 if int(info.Speed) >= 3 else 0x2
    protocols = [
        name for bit, name in ((0x1, "usb1"), (0x2, "usb2"), (0x4, "usb3"))
        if protocol_mask & bit
    ]
    connector = _query_connector(api, handle, port_number)
    driver_key = ""
    if status == "occupied":
        try:
            driver_key = _query_dynamic_string(
                api, handle, IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME, port_number
            )
        except OSError:
            driver_key = ""
    pnp = device_by_driver.get(driver_key.upper()) if driver_key else None
    descriptor = info.DeviceDescriptor
    device = None
    if status == "occupied":
        instance_id = pnp.instance_id if pnp else ""
        vid = int(descriptor.idVendor) or (_vid_pid(instance_id)[0] if instance_id else None)
        pid = int(descriptor.idProduct) or (_vid_pid(instance_id)[1] if instance_id else None)
        serial = ""
        if instance_id and "\\" in instance_id:
            tail = instance_id.rsplit("\\", 1)[-1]
            if "&" not in tail:
                serial = tail
        class_code = int(descriptor.bDeviceClass)
        class_name = {
            0x03: "hid",
            0x09: "hub",
            0x0E: "video",
        }.get(class_code, (pnp.class_name.lower() if pnp and pnp.class_name else "usb"))
        device = {
            "instance_id": instance_id,
            "friendly_name": (
                pnp.friendly_name or pnp.description
                if pnp else f"USB {vid or 0:04X}:{pid or 0:04X}"
            ),
            "description": pnp.description if pnp else "",
            "class": class_name,
            "vid": vid,
            "pid": pid,
            "serial": serial,
            "network_name": pnp.network_name if pnp else "",
            "driver_key": driver_key,
            "location_paths": list(pnp.location_paths) if pnp else [],
        }
    child_hub_path = ""
    if bool(info.DeviceIsHub):
        try:
            child_hub_path = _query_dynamic_string(
                api, handle, IOCTL_USB_GET_NODE_CONNECTION_NAME, port_number
            )
        except OSError:
            child_hub_path = ""
    return {
        "number": port_number,
        "state": status,
        "connection_status": int(info.ConnectionStatus),
        "speed": int(info.Speed),
        "supported_protocols": protocols,
        "device_is_hub": bool(info.DeviceIsHub),
        "child_hub_path": child_hub_path,
        "device": device,
        **connector,
    }


def _enrich_port_device_from_pnp(
    port: dict[str, Any],
    *,
    hub_instance_id: str,
    port_number: int,
    devices: Mapping[str, _DeviceRecord],
) -> None:
    """Attach the PnP function record when the hub IOCTL omits a driver key."""

    device = port.get("device")
    if not isinstance(device, dict):
        return
    wanted_vid = device.get("vid")
    wanted_pid = device.get("pid")
    candidates: list[_DeviceRecord] = []
    hub_key = hub_instance_id.upper()
    for record in devices.values():
        record_vid, record_pid = _vid_pid(
            record.instance_id + " " + " ".join(record.hardware_ids)
        )
        if (record_vid, record_pid) != (wanted_vid, wanted_pid):
            continue
        direct_child = record.parent_instance_id.upper() == hub_key
        at_port = any(
            str(path).upper().endswith(f"#USB({port_number})")
            for path in record.location_paths
        )
        if direct_child or at_port:
            candidates.append(record)
    if len(candidates) != 1:
        return
    record = candidates[0]
    device.update({
        "instance_id": record.instance_id,
        "friendly_name": record.friendly_name or record.description or device.get("friendly_name", ""),
        "description": record.description,
        "class": record.class_name.lower() if record.class_name else device.get("class", "usb"),
        "network_name": record.network_name,
        "driver_key": record.driver_key,
        "location_paths": list(record.location_paths),
    })
    tail = record.instance_id.rsplit("\\", 1)[-1]
    if "&" not in tail:
        device["serial"] = tail


def _read_hub(
    api: _WindowsApi,
    interface: Mapping[str, Any],
    devices: Mapping[str, _DeviceRecord],
) -> tuple[dict[str, Any], list[str]]:
    path = str(interface["device_path"])
    instance_id = str(interface.get("instance_id") or "")
    pnp = devices.get(instance_id.upper())
    errors: list[str] = []
    handle = api.open_hub(path)
    try:
        hub_info = USB_HUB_INFORMATION_EX()
        api.ioctl(handle, IOCTL_USB_GET_HUB_INFORMATION_EX, hub_info)
        highest_port = int(hub_info.HighestPortNumber)
        hub_type = int(hub_info.HubType)
        ports: list[dict[str, Any]] = []
        driver_map = {
            record.driver_key.upper(): record
            for record in devices.values()
            if record.driver_key
        }
        for number in range(1, highest_port + 1):
            try:
                port = _query_port(api, handle, number, driver_map)
                _enrich_port_device_from_pnp(
                    port,
                    hub_instance_id=instance_id,
                    port_number=number,
                    devices=devices,
                )
                ports.append(port)
            except OSError as exc:
                errors.append(f"hub {_stable_id('hub', instance_id)} port {number}: {exc}")
                ports.append({
                    "number": number,
                    "state": "unknown",
                    "supported_protocols": [],
                    "user_connectable": None,
                    "companion": None,
                    "device": None,
                })
    finally:
        api.close_handle(handle)
    vid, pid = _vid_pid(instance_id)
    return ({
        "id": _stable_id("rawhub", instance_id or path),
        "device_path": path,
        "instance_id": instance_id,
        "parent_instance_id": pnp.parent_instance_id if pnp else "",
        "location_paths": list(pnp.location_paths if pnp else ()),
        "friendly_name": (pnp.friendly_name or pnp.description) if pnp else "USB Hub",
        "vid": vid,
        "pid": pid,
        "hub_type": hub_type,
        "highest_port_number": highest_port,
        "is_root": instance_id.upper().startswith("USB\\ROOT_HUB"),
        "ports": ports,
    }, errors)


def discover_windows_usb_topology() -> dict[str, Any]:
    if not sys.platform.startswith("win"):
        return _empty_topology("unavailable", "unsupported_platform", ["Windows USB topology is unavailable"])
    try:
        api = _WindowsApi()
        devices = api.enumerate_devices()
        interfaces = api.enumerate_hub_interfaces()
    except Exception as exc:
        return _empty_topology("unavailable", PROVIDER_NAME, [f"USB topology initialization failed: {exc}"])

    raw_hubs: list[dict[str, Any]] = []
    errors: list[str] = []
    for interface in interfaces:
        try:
            hub, hub_errors = _read_hub(api, interface, devices)
            raw_hubs.append(hub)
            errors.extend(hub_errors)
        except Exception as exc:
            errors.append(
                f"hub {_stable_id('hub', interface.get('instance_id') or interface.get('device_path'))}: {exc}"
            )
    registry_path = _host_port_registry_path()
    known_host_port_ids = _load_known_host_port_ids(registry_path)
    try:
        topology = normalize_usb_topology(
            raw_hubs,
            errors=errors,
            known_host_port_ids=known_host_port_ids,
        )
    except Exception as exc:
        return _empty_topology("unavailable", PROVIDER_NAME, [f"USB topology normalization failed: {exc}"])
    observed_host_port_ids = {
        str(port.get("id") or "")
        for port in topology.get("usb_ports", [])
        if port.get("owner_kind") == "host"
        and port.get("confirmation") == "observed_current"
    }
    _save_known_host_port_ids(
        registry_path,
        known_host_port_ids | observed_host_port_ids,
    )
    if not raw_hubs and errors:
        topology["state"] = "unavailable"
    return topology


def find_topology_device(
    topology: Mapping[str, Any],
    *,
    vid: int | None = None,
    pid: int | None = None,
    serial: str = "",
    network_name: str = "",
) -> Mapping[str, Any] | None:
    serial_key = str(serial or "").strip().lower()
    network_key = str(network_name or "").strip().lower()
    candidates = []
    for device in topology.get("devices", []) or []:
        if vid is not None and device.get("vid") != vid:
            continue
        if pid is not None and device.get("pid") != pid:
            continue
        if serial_key and str(device.get("serial") or "").strip().lower() != serial_key:
            continue
        if network_key and str(device.get("network_name") or "").strip().lower() != network_key:
            continue
        candidates.append(device)
    return candidates[0] if len(candidates) == 1 else None


def topology_port(topology: Mapping[str, Any], port_id: str) -> Mapping[str, Any] | None:
    return next(
        (item for item in topology.get("usb_ports", []) or [] if str(item.get("id")) == str(port_id)),
        None,
    )


def topology_dock(topology: Mapping[str, Any], dock_id: str) -> Mapping[str, Any] | None:
    return next(
        (item for item in topology.get("docks", []) or [] if str(item.get("id")) == str(dock_id)),
        None,
    )
