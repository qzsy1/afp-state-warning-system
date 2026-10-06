"""Normalize physical connectors and live Windows communication endpoints.

USB is a physical transport. HID, UVC and COM are endpoints exposed by the
device currently attached to a USB connector.  Keeping that distinction here
prevents acquisition drivers and the browser from independently inferring the
same topology.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any


USB_ENDPOINT_KINDS = {"usb_hid", "usb_uvc"}
SERIAL_ENDPOINT_KINDS = {"serial", "com"}
ETHERNET_ENDPOINT_KINDS = {"ethernet", "ethernet_adapter"}


def _stable_key(prefix: str, value: object) -> str:
    text = str(value or "").strip().lower()
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def endpoint_kind(interface: Mapping[str, Any]) -> str:
    kind = str(interface.get("kind") or interface.get("interface_kind") or "")
    return {
        "usb_hid": "hid",
        "usb_uvc": "uvc",
        "serial": "serial",
        "com": "serial",
        "ethernet": "network",
        "ethernet_adapter": "network",
    }.get(kind, str(interface.get("endpoint_kind") or kind or "unknown"))


def interface_transport_family(interface: Mapping[str, Any]) -> str:
    """Return the physical transport without confusing USB COM with UART."""

    explicit = str(interface.get("transport_family") or "")
    if explicit:
        return explicit
    kind = str(interface.get("kind") or interface.get("interface_kind") or "")
    if kind in USB_ENDPOINT_KINDS:
        return "usb"
    if kind in ETHERNET_ENDPOINT_KINDS:
        return "ethernet"
    if kind in SERIAL_ENDPOINT_KINDS:
        if interface.get("parent_port_id"):
            return "usb"
        if interface.get("vid") is not None and interface.get("pid") is not None:
            return "usb"
        pnp_text = " ".join(
            str(interface.get(key) or "")
            for key in ("pnp_instance_id", "hwid", "location")
        ).upper()
        return "usb" if "USB" in pnp_text or "VID_" in pnp_text else "serial_native"
    return kind or "unknown"


def _match_topology_device(
    topology: Mapping[str, Any], interface: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    devices = [item for item in topology.get("devices", []) or [] if isinstance(item, Mapping)]
    parent_id = str(interface.get("parent_port_id") or "")
    if parent_id:
        parent_matches = [item for item in devices if str(item.get("parent_port_id") or "") == parent_id]
        if len(parent_matches) == 1:
            return parent_matches[0]

    instance_id = str(interface.get("pnp_instance_id") or "")
    if instance_id:
        instance_key = _stable_key("pnp", instance_id)
        matches = [item for item in devices if str(item.get("instance_key") or "") == instance_key]
        if len(matches) == 1:
            return matches[0]

    location = str(interface.get("location_path") or interface.get("location") or "")
    if location:
        location_key = _stable_key("location", location)
        matches = [
            item for item in devices
            if location_key in {str(value) for value in item.get("location_keys", []) or []}
        ]
        if len(matches) == 1:
            return matches[0]

    network_name = str(interface.get("name") or interface.get("network_name") or "").strip().casefold()
    if network_name:
        matches = [
            item for item in devices
            if str(item.get("network_name") or "").strip().casefold() == network_name
        ]
        if len(matches) == 1:
            return matches[0]

    vid = interface.get("vid")
    pid = interface.get("pid")
    if vid is None or pid is None:
        return None
    serial = str(interface.get("serial") or "").strip().casefold()
    matches = [
        item for item in devices
        if item.get("vid") == int(vid)
        and item.get("pid") == int(pid)
        and (
            not serial
            or str(item.get("serial") or "").strip().casefold() == serial
        )
    ]
    return matches[0] if len(matches) == 1 else None


def enrich_interface_transports(
    topology: MutableMapping[str, Any],
    physical_interfaces: Sequence[MutableMapping[str, Any]],
) -> dict[str, Any]:
    """Enrich live endpoints and return a role-independent transport catalog."""

    ports = {
        str(item.get("id") or ""): item
        for item in topology.get("usb_ports", []) or []
        if isinstance(item, MutableMapping) and item.get("id")
    }
    docks = {
        str(item.get("id") or ""): item
        for item in topology.get("docks", []) or []
        if isinstance(item, Mapping) and item.get("id")
    }
    device_by_id = {
        str(item.get("id") or ""): item
        for item in topology.get("devices", []) or []
        if isinstance(item, MutableMapping) and item.get("id")
    }
    endpoints_by_port: dict[str, list[dict[str, Any]]] = {port_id: [] for port_id in ports}
    unlocated_usb: list[str] = []
    native_serial: list[str] = []
    ethernet: list[str] = []

    for interface in physical_interfaces:
        kind = str(interface.get("kind") or interface.get("interface_kind") or "")
        interface_id = str(interface.get("id") or "")
        match = _match_topology_device(topology, interface)
        if isinstance(match, Mapping):
            parent_id = str(match.get("parent_port_id") or "")
            port = ports.get(parent_id)
            if port is not None:
                interface["parent_port_id"] = parent_id
                interface["dock_id"] = str(port.get("dock_id") or "")
                interface["owner_kind"] = str(port.get("owner_kind") or ("dock" if port.get("dock_id") else "host"))
                topology_label = str(port.get("label") or "")
                if kind in ETHERNET_ENDPOINT_KINDS or port.get("internal_function") == "ethernet":
                    dock = docks.get(str(port.get("dock_id") or ""), {})
                    topology_label = f"{dock.get('label', '拓展坞')} · 拓展坞网口"
                interface["topology_label"] = topology_label
                base_label = str(interface.get("base_label") or interface.get("label") or interface_id)
                interface["base_label"] = base_label
                if topology_label and not base_label.startswith(topology_label):
                    interface["label"] = f"{topology_label} · {base_label}"
                live_ids = match.setdefault("live_interface_ids", []) if isinstance(match, MutableMapping) else []
                if interface_id and interface_id not in live_ids:
                    live_ids.append(interface_id)
                if isinstance(match, MutableMapping) and not match.get("live_interface_id"):
                    match["live_interface_id"] = interface_id

        transport = interface_transport_family(interface)
        interface["transport_family"] = transport
        interface["endpoint_kind"] = endpoint_kind(interface)
        parent_id = str(interface.get("parent_port_id") or "")
        endpoint_record = {
            "id": interface_id,
            "kind": kind,
            "endpoint_kind": interface["endpoint_kind"],
            "endpoint": str(interface.get("endpoint") or ""),
            "label": str(interface.get("label") or interface_id),
            "detected": interface.get("detected") is not False,
            "driver_available": bool(interface.get("driver_available")),
            "auto_assignable": bool(interface.get("auto_assignable")),
        }
        if transport == "usb":
            if parent_id in endpoints_by_port:
                endpoints_by_port[parent_id].append(endpoint_record)
            elif interface_id:
                unlocated_usb.append(interface_id)
        elif transport == "serial_native" and interface_id:
            native_serial.append(interface_id)
        elif transport == "ethernet" and interface_id:
            ethernet.append(interface_id)

    usb_ports: list[dict[str, Any]] = []
    for port in ports.values():
        if port.get("user_connectable") is False:
            continue
        usb_ports.append({
            "id": str(port.get("id") or ""),
            "owner_kind": str(port.get("owner_kind") or ("dock" if port.get("dock_id") else "host")),
            "dock_id": str(port.get("dock_id") or ""),
            "label": str(port.get("label") or "USB 端口"),
            "state": str(port.get("state") or "unknown"),
            "connector_type": str(port.get("connector_type") or "usb"),
            "endpoints": sorted(endpoints_by_port.get(str(port.get("id") or ""), []), key=lambda item: item["id"]),
        })
    usb_ports.sort(key=lambda item: (item["owner_kind"] != "host", item["dock_id"], item["label"], item["id"]))
    return {
        "schema_version": 1,
        "usb_ports": usb_ports,
        "unlocated_usb_interface_ids": sorted(set(unlocated_usb)),
        "native_serial_interface_ids": sorted(set(native_serial)),
        "ethernet_interface_ids": sorted(set(ethernet)),
    }
