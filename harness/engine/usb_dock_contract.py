from __future__ import annotations

import argparse
import json
import re
import subprocess
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
FRONTEND_BEHAVIOR_TESTS = (
    "test_usb_interface_candidates.UsbInterfaceCandidateTests."
    "test_shared_ethernet_endpoint_keeps_plc_and_abb_logical_interfaces",
    "test_frontend_guest_simulation.GuestSimulationFrontendContractTests."
    "test_interface_check_allows_unverified_port_without_weakening_start_gate",
)


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
        owner_kind = str(port.get("owner_kind") or "")
        dock_id = str(port.get("dock_id") or "")
        if owner_kind not in {"host", "dock"}:
            issues.append(f"usb_topology.usb_ports[{index}].owner_kind must be host or dock")
        if owner_kind == "dock" and dock_id not in dock_ids:
            issues.append(f"usb_topology.usb_ports[{index}].dock_id must reference a dock")
        if owner_kind == "host" and dock_id:
            issues.append(f"usb_topology.usb_ports[{index}] host port must not reference a dock")
        if owner_kind == "host" and str(port.get("confirmation") or "") not in {
            "observed_current",
            "observed_history",
            "connector_metadata",
        }:
            issues.append(
                f"usb_topology.usb_ports[{index}].confirmation must be "
                "observed_current, observed_history, or connector_metadata"
            )
        if port.get("user_connectable") not in {True, False}:
            issues.append(f"usb_topology.usb_ports[{index}].user_connectable must be boolean")
        if not str(port.get("connector_type") or "").strip():
            issues.append(f"usb_topology.usb_ports[{index}].connector_type is required")
        if str(port.get("connector_form_factor") or "") not in {"type_a", "type_c", "unknown"}:
            issues.append(
                f"usb_topology.usb_ports[{index}].connector_form_factor must be "
                "type_a, type_c, or unknown"
            )
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
        kind = str(item.get("kind") or "")
        transport = str(item.get("transport_family") or "")
        if parent and parent not in port_ids:
            issues.append(f"physical_interfaces[{index}].parent_port_id must reference usb_topology")
        if dock_id and dock_id not in dock_ids:
            issues.append(f"physical_interfaces[{index}].dock_id must reference usb_topology")
        if kind in {"usb_hid", "usb_uvc"} and transport != "usb":
            issues.append(f"physical_interfaces[{index}] USB endpoint must use transport_family usb")
        if kind in {"serial", "com"}:
            expected_transport = "usb" if parent else "serial_native"
            if transport != expected_transport:
                issues.append(
                    f"physical_interfaces[{index}] serial transport_family must be {expected_transport}"
                )
        if dock_id and kind in {"ethernet", "ethernet_adapter"}:
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
        "USB sensor roles must share one physical transport pool": ("USB_SENSOR_ROLES", "interfaceTransportFamily"),
        "host USB-A and native serial groups must be explicit": ("电脑本机 USB-A", "主机原生串口"),
        "Type-C and dock upstream connectors must be excluded from USB-A candidates": (
            "connector_form_factor",
            "type_c",
            "dock_upstream",
        ),
        "default selections must track their origin": ("selection_origin", "manual", "auto"),
        "logical interfaces sharing one physical endpoint must remain distinct": (
            "uniqueLogicalInterfaceConfigs",
            "usedIds",
        ),
    }
    issues: list[str] = []
    for message, tokens in requirements.items():
        if not all(token in source for token in tokens):
            issues.append(message)
    render_start = source.find("function renderInterfacePanel")
    render_end = source.find("function mergeRememberedInterfaceSelections", render_start)
    render_source = source[render_start:render_end] if render_start >= 0 else ""
    if "usedEndpoints" in render_source:
        issues.append(
            "logical interface rendering must not deduplicate PLC and ABB by shared endpoint"
        )
    check_start = source.rfind("async function testSensorConnection")
    check_source = source[check_start:] if check_start >= 0 else ""
    if not re.search(
        r"acquisitionConfig\(\{[^}]*allowUnverifiedPhysical\s*:\s*true[^}]*\}\)",
        check_source,
    ):
        issues.append(
            "interface checks must submit unverified physical ports to the configured driver"
        )
    return tuple(issues)


def validate_acquisition_source(source: str) -> tuple[str, ...]:
    issues: list[str] = []
    obsolete_skip = 'if physical_fallback or item.get("physical_verified") is False:'
    if obsolete_skip in source:
        issues.append(
            "unverified physical-port bindings must not skip the configured sensor driver check"
        )
    requirements = {
        "interface checks must keep probing configured drivers": (
            "if physical_fallback:",
            "MultiInterfaceDriver",
            "physical_unverified",
        ),
        "interface checks must keep the real-capture port gate separate": (
            "physical_verified",
            "不能启动真实采集",
        ),
    }
    for message, tokens in requirements.items():
        if not all(token in source for token in tokens):
            issues.append(message)
    return tuple(issues)


def validate_frontend_behavior(app_dir: Path) -> tuple[str, ...]:
    """Execute production JavaScript behavior scenarios, not source-token proxies."""
    if not app_dir.is_dir():
        return (f"front-end application directory does not exist: {app_dir}",)
    command = [sys.executable, "-m", "unittest", *FRONTEND_BEHAVIOR_TESTS, "-v"]
    try:
        completed = subprocess.run(
            command,
            cwd=app_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ("production front-end behavior scenarios timed out after 180 seconds",)
    except OSError as exc:
        return (f"production front-end behavior scenarios could not start: {exc}",)
    if completed.returncode == 0:
        return ()
    detail = (completed.stderr or completed.stdout or "no test output").strip()
    if len(detail) > 4000:
        detail = detail[-4000:]
    return (f"production front-end behavior scenarios failed:\n{detail}",)


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
    acquisition_path = app_dir / "acquisition.py"
    if not acquisition_path.is_file():
        issues.append(f"acquisition source does not exist: {acquisition_path}")
    else:
        issues.extend(validate_acquisition_source(acquisition_path.read_text(encoding="utf-8")))
    issues.extend(validate_frontend_behavior(app_dir))
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
