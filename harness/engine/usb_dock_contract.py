from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


TOPOLOGY_STATES = {"complete", "partial", "unavailable"}
PORT_STATES = {
    "empty",
    "occupied",
    "enumerating",
    "disabled",
    "disconnected",
    "error",
    "unknown",
}


def _records(value: object, field: str, issues: list[str]) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        issues.append(f"usb_topology.{field} must be a list")
        return []
    records: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            issues.append(f"usb_topology.{field}[{index}] must be an object")
            continue
        records.append(item)
    return records


def _unique_ids(records: Sequence[Mapping[str, Any]], field: str, issues: list[str]) -> set[str]:
    identifiers: set[str] = set()
    for index, item in enumerate(records):
        identifier = str(item.get("id") or "").strip()
        if not identifier:
            issues.append(f"usb_topology.{field}[{index}].id is required")
        elif identifier in identifiers:
            issues.append(f"usb_topology.{field} contains duplicate id {identifier}")
        else:
            identifiers.add(identifier)
    return identifiers


def validate_discovery_payload(payload: object) -> tuple[str, ...]:
    issues: list[str] = []
    if not isinstance(payload, Mapping):
        return ("interface discovery result must be an object",)

    topology = payload.get("usb_topology")
    if not isinstance(topology, Mapping):
        return ("interface discovery must expose usb_topology",)

    if topology.get("schema_version") != 1:
        issues.append("usb_topology.schema_version must equal 1")
    if str(topology.get("state") or "") not in TOPOLOGY_STATES:
        issues.append("usb_topology.state must be complete, partial, or unavailable")
    if not str(topology.get("provider") or "").strip():
        issues.append("usb_topology.provider is required")
    if not isinstance(topology.get("errors"), list):
        issues.append("usb_topology.errors must be a list")

    docks = _records(topology.get("docks"), "docks", issues)
    ports = _records(topology.get("usb_ports"), "usb_ports", issues)
    devices = _records(topology.get("devices"), "devices", issues)
    dock_ids = _unique_ids(docks, "docks", issues)
    port_ids = _unique_ids(ports, "usb_ports", issues)
    _unique_ids(devices, "devices", issues)

    for index, dock in enumerate(docks):
        if not str(dock.get("label") or "").strip():
            issues.append(f"usb_topology.docks[{index}].label is required")

    for index, port in enumerate(ports):
        dock_id = str(port.get("dock_id") or "")
        if dock_id not in dock_ids:
            issues.append(f"usb_topology.usb_ports[{index}].dock_id must reference a dock")
        if port.get("user_connectable") not in {True, False}:
            issues.append(f"usb_topology.usb_ports[{index}].user_connectable must be boolean")
        if not str(port.get("connector_type") or "").strip():
            issues.append(f"usb_topology.usb_ports[{index}].connector_type is required")
        protocols = port.get("supported_protocols")
        if not isinstance(protocols, list) or not all(isinstance(item, str) and item for item in protocols):
            issues.append(f"usb_topology.usb_ports[{index}].supported_protocols must be a string list")
        if str(port.get("state") or "") not in PORT_STATES:
            issues.append(f"usb_topology.usb_ports[{index}].state is invalid")

    for index, device in enumerate(devices):
        parent = str(device.get("parent_port_id") or "")
        if parent not in port_ids:
            issues.append(f"usb_topology.devices[{index}].parent_port_id must reference a port")

    physical = payload.get("physical_interfaces")
    if not isinstance(physical, list):
        issues.append("physical_interfaces must remain a list")
        physical = []
    for index, item in enumerate(physical):
        if not isinstance(item, Mapping):
            continue
        parent = str(item.get("parent_port_id") or "")
        dock_id = str(item.get("dock_id") or "")
        if parent and parent not in port_ids:
            issues.append(f"physical_interfaces[{index}].parent_port_id must reference usb_topology")
        if dock_id and dock_id not in dock_ids:
            issues.append(f"physical_interfaces[{index}].dock_id must reference usb_topology")
        if dock_id and str(item.get("kind") or "") in {"ethernet", "ethernet_adapter"}:
            if "拓展坞网口" not in str(item.get("topology_label") or item.get("label") or ""):
                issues.append(f"physical_interfaces[{index}] dock ethernet label must contain 拓展坞网口")

    return tuple(issues)


def validate_frontend_source(source: str) -> tuple[str, ...]:
    requirements = {
        "front-end state must consume usb_topology": ("usb_topology", "usbTopology"),
        "USB port selection must submit physical_port_id": ("physical_port_id",),
        "interface choices must use optgroup grouping": ("optgroup",),
        "dock network adapters must be labelled as 拓展坞网口": ("拓展坞网口",),
        "empty selected ports must explain that no compatible device is present": ("端口存在", "未检测到兼容设备"),
    }
    issues: list[str] = []
    for message, tokens in requirements.items():
        if not all(token in source for token in tokens):
            issues.append(message)
    return tuple(issues)


def run_repo_contract(repo_root: Path) -> tuple[str, ...]:
    root = repo_root.resolve()
    app_dir = root / "visualization_app"
    if not app_dir.is_dir():
        return (f"visualization_app directory does not exist: {app_dir}",)

    sys.path.insert(0, str(app_dir))
    try:
        from acquisition import AcquisitionManager

        payload = AcquisitionManager.discover_interfaces()
    except Exception as exc:
        return (f"interface discovery could not run: {type(exc).__name__}: {exc}",)
    finally:
        if sys.path and sys.path[0] == str(app_dir):
            sys.path.pop(0)

    issues = list(validate_discovery_payload(payload))
    frontend_path = app_dir / "static" / "app.js"
    if not frontend_path.is_file():
        issues.append(f"front-end source does not exist: {frontend_path}")
    else:
        issues.extend(validate_frontend_source(frontend_path.read_text(encoding="utf-8")))
    return tuple(issues)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the USB dock topology discovery and interface display contract."
    )
    parser.add_argument("--repo", default=".", help="Repository root")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON result for machine inspection",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    issues = run_repo_contract(Path(args.repo))
    if args.json:
        print(json.dumps({"ok": not issues, "issues": list(issues)}, ensure_ascii=False))
    elif issues:
        print("USB dock topology contract failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
    else:
        print("USB dock topology and interface display contracts passed.")
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
