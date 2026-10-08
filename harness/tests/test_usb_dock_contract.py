from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.engine.usb_dock_contract import (
    FRONTEND_BEHAVIOR_TESTS,
    validate_acquisition_source,
    validate_discovery_payload,
    validate_frontend_behavior,
    validate_frontend_source,
)


def compliant_discovery() -> dict:
    return {
        "usb_topology": {
            "schema_version": 1,
            "state": "complete",
            "provider": "windows_usb_hub_ioctl",
            "errors": [],
            "docks": [
                {"id": "dock:a", "label": "拓展坞 1", "state": "connected"},
            ],
            "usb_ports": [
                {
                    "id": "usbport:host:1",
                    "owner_kind": "host",
                    "dock_id": "",
                    "system_port_number": 2,
                    "connector_type": "usb3",
                    "connector_form_factor": "type_a",
                    "supported_protocols": ["usb2", "usb3"],
                    "state": "empty",
                    "user_connectable": True,
                    "confirmation": "observed_history",
                    "device_id": None,
                },
                {
                    "id": "usbport:a:1",
                    "owner_kind": "dock",
                    "dock_id": "dock:a",
                    "system_port_number": 1,
                    "connector_type": "usb3",
                    "connector_form_factor": "type_a",
                    "supported_protocols": ["usb2", "usb3"],
                    "state": "empty",
                    "user_connectable": True,
                    "device_id": None,
                },
                {
                    "id": "usbport:a:2",
                    "owner_kind": "dock",
                    "dock_id": "dock:a",
                    "system_port_number": 2,
                    "connector_type": "usb3",
                    "connector_form_factor": "type_a",
                    "supported_protocols": ["usb2", "usb3"],
                    "state": "occupied",
                    "user_connectable": True,
                    "device_id": "device:smrf",
                },
                {
                    "id": "usbport:a:4",
                    "owner_kind": "dock",
                    "dock_id": "dock:a",
                    "system_port_number": 4,
                    "connector_type": "internal",
                    "connector_form_factor": "type_a",
                    "supported_protocols": ["usb3"],
                    "state": "occupied",
                    "user_connectable": False,
                    "device_id": "device:asix",
                },
            ],
            "devices": [
                {
                    "id": "device:smrf",
                    "parent_port_id": "usbport:a:2",
                    "class": "hid",
                    "friendly_name": "SMRFCT08B",
                    "vid": 0x1234,
                    "pid": 0x5678,
                    "serial": "SMRF-01",
                    "live_interface_id": "hid:smrf-01",
                },
                {
                    "id": "device:asix",
                    "parent_port_id": "usbport:a:4",
                    "class": "ethernet",
                    "friendly_name": "ASIX AX88179B",
                    "vid": 0x0B95,
                    "pid": 0x1790,
                    "serial": "",
                    "live_interface_id": "ethernet:asix",
                },
            ],
        },
        "physical_interfaces": [
            {
                "id": "hid:smrf-01",
                "kind": "usb_hid",
                "transport_family": "usb",
                "parent_port_id": "usbport:a:2",
                "dock_id": "dock:a",
                "topology_label": "拓展坞 1 · USB3-2 · SMRFCT08B",
            },
            {
                "id": "serial:COM8",
                "kind": "serial",
                "transport_family": "usb",
                "endpoint_kind": "serial",
                "endpoint": "COM8",
                "parent_port_id": "usbport:a:1",
                "dock_id": "dock:a",
                "topology_label": "拓展坞 1 · USB3-1 · COM8",
            },
            {
                "id": "serial:COM1",
                "kind": "serial",
                "transport_family": "serial_native",
                "endpoint_kind": "serial",
                "endpoint": "COM1",
            },
            {
                "id": "ethernet:asix",
                "kind": "ethernet",
                "parent_port_id": "usbport:a:4",
                "dock_id": "dock:a",
                "topology_label": "拓展坞 1 · 拓展坞网口 · ASIX AX88179B",
            },
        ],
    }


class UsbDockContractValidatorTests(unittest.TestCase):
    def test_frontend_behavior_contract_executes_the_two_production_regressions(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch(
            "harness.engine.usb_dock_contract.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")

            self.assertEqual(validate_frontend_behavior(Path(folder)), ())

        command = run.call_args.args[0]
        self.assertTrue(all(name in command for name in FRONTEND_BEHAVIOR_TESTS))
        self.assertEqual(run.call_args.kwargs["cwd"], Path(folder))

    def test_frontend_behavior_contract_reports_runtime_failures(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch(
            "harness.engine.usb_dock_contract.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "ABB row missing")

            issues = validate_frontend_behavior(Path(folder))

        self.assertIn("production front-end behavior scenarios failed", issues[0])
        self.assertIn("ABB row missing", issues[0])

    def test_accepts_versioned_topology_and_dock_network_projection(self) -> None:
        self.assertEqual(validate_discovery_payload(compliant_discovery()), ())

    def test_rejects_duplicate_ports_and_dangling_device_parent(self) -> None:
        payload = compliant_discovery()
        payload["usb_topology"]["usb_ports"][2]["id"] = "usbport:a:1"
        payload["usb_topology"]["devices"][0]["parent_port_id"] = "usbport:missing"

        issues = "\n".join(validate_discovery_payload(payload))

        self.assertIn("duplicate id", issues)
        self.assertIn("parent_port_id must reference a port", issues)

    def test_unavailable_provider_still_requires_stable_schema(self) -> None:
        payload = {
            "usb_topology": {
                "schema_version": 1,
                "state": "unavailable",
                "provider": "unsupported_platform",
                "errors": ["Windows USB topology is unavailable"],
                "docks": [],
                "usb_ports": [],
                "devices": [],
            },
            "physical_interfaces": [],
        }
        self.assertEqual(validate_discovery_payload(payload), ())

    def test_frontend_contract_requires_grouping_port_binding_and_status_copy(self) -> None:
        source = """
        const usbTopology = payload.usb_topology;
        config.physical_port_id = selected.value;
        const group = document.createElement("optgroup");
        group.label = "拓展坞 USB 端口";
        const host = "电脑本机 USB-A";
        const nativeSerial = "主机原生串口";
        const ethernet = "拓展坞网口";
        status.textContent = "端口存在，未检测到兼容设备";
        const USB_SENSOR_ROLES = new Set(["thermocouple", "thermal_uvc", "pressure"]);
        const interfaceTransportFamily = () => "usb";
        const connector_form_factor = "type_c";
        const usbA = item.connector_form_factor !== "type_c" && item.internal_function !== "dock_upstream";
        item.selection_origin = item.selection_origin || "auto";
        item.selection_origin = "manual";
        function uniqueLogicalInterfaceConfigs(configs) {
            const usedIds = new Set();
            return configs.filter((item) => !usedIds.has(item.id) && usedIds.add(item.id));
        }
        async function testSensorConnection() {
            return acquisitionConfig({allowUnverifiedPhysical: true});
        }
        """
        self.assertEqual(validate_frontend_source(source), ())

        issues = validate_frontend_source("const physical_interface_id = '';")
        self.assertGreaterEqual(len(issues), 4)

    def test_frontend_contract_rejects_endpoint_based_logical_interface_deduplication(self) -> None:
        source = """
        function renderInterfacePanel(configs) {
            const usedEndpoints = new Set();
        }
        function mergeRememberedInterfaceSelections(defaults) { return defaults; }
        """

        issues = "\n".join(validate_frontend_source(source))

        self.assertIn("must not deduplicate PLC and ABB by shared endpoint", issues)

    def test_frontend_contract_requires_unverified_ports_to_reach_interface_check(self) -> None:
        source = """
        async function testSensorConnection() {
            return acquisitionConfig();
        }
        """

        issues = "\n".join(validate_frontend_source(source))

        self.assertIn("must submit unverified physical ports", issues)

    def test_frontend_contract_accepts_diagnostic_options_with_unverified_port_probe(self) -> None:
        source = """
        const physical_interface_id = selected.value;
        const physical_port_id = selected.port;
        const group = document.createElement("optgroup");
        group.label = "拓展坞 USB 端口";
        const host = "电脑本机 USB-A";
        const nativeSerial = "主机原生串口";
        const ethernet = "拓展坞网口";
        status.textContent = "端口存在，未检测到兼容设备";
        const USB_SENSOR_ROLES = new Set(["thermocouple"]);
        const interfaceTransportFamily = () => "usb";
        const connector_form_factor = "type_c";
        const usbA = item.connector_form_factor !== "type_c" && item.internal_function !== "dock_upstream";
        item.selection_origin = "manual";
        function uniqueLogicalInterfaceConfigs(configs) { const usedIds = new Set(); return configs; }
        async function testSensorConnection() {
            return acquisitionConfig({allowUnverifiedPhysical: true, diagnosticValidation: true});
        }
        """

        self.assertNotIn(
            "interface checks must submit unverified physical ports to the configured driver",
            validate_frontend_source(source),
        )

    def test_requires_host_owner_and_usb_serial_transport_parentage(self) -> None:
        payload = compliant_discovery()
        del payload["usb_topology"]["usb_ports"][0]["owner_kind"]
        payload["physical_interfaces"][1]["transport_family"] = "serial_native"

        issues = "\n".join(validate_discovery_payload(payload))

        self.assertIn("owner_kind must be host or dock", issues)
        self.assertIn("serial transport_family must be usb", issues)

    def test_host_ports_require_physical_observation_evidence(self) -> None:
        payload = compliant_discovery()
        del payload["usb_topology"]["usb_ports"][0]["confirmation"]

        issues = "\n".join(validate_discovery_payload(payload))

        self.assertIn("confirmation must be observed_current, observed_history, or connector_metadata", issues)

    def test_rejects_missing_connector_form_factor(self) -> None:
        payload = compliant_discovery()
        del payload["usb_topology"]["usb_ports"][0]["connector_form_factor"]

        issues = "\n".join(validate_discovery_payload(payload))

        self.assertIn("connector_form_factor must be type_a, type_c, or unknown", issues)

    def test_interface_check_keeps_driver_probe_separate_from_port_gate(self) -> None:
        compliant = '''
        physical_verified = item.get("physical_verified") is not False
        if physical_fallback:
            return
        interface_driver = MultiInterfaceDriver()
        state = "ok" if physical_verified else "physical_unverified"
        message = "不能启动真实采集"
        '''
        self.assertEqual(validate_acquisition_source(compliant), ())

        obsolete = '''
        if physical_fallback or item.get("physical_verified") is False:
            return
        '''
        issues = "\n".join(validate_acquisition_source(obsolete))
        self.assertIn("must not skip", issues)


if __name__ == "__main__":
    unittest.main()
