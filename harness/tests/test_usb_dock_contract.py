from __future__ import annotations

import unittest

from harness.engine.usb_dock_contract import (
    validate_discovery_payload,
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
                    "id": "usbport:a:1",
                    "dock_id": "dock:a",
                    "system_port_number": 1,
                    "connector_type": "usb3",
                    "supported_protocols": ["usb2", "usb3"],
                    "state": "empty",
                    "user_connectable": True,
                    "device_id": None,
                },
                {
                    "id": "usbport:a:2",
                    "dock_id": "dock:a",
                    "system_port_number": 2,
                    "connector_type": "usb3",
                    "supported_protocols": ["usb2", "usb3"],
                    "state": "occupied",
                    "user_connectable": True,
                    "device_id": "device:smrf",
                },
                {
                    "id": "usbport:a:4",
                    "dock_id": "dock:a",
                    "system_port_number": 4,
                    "connector_type": "internal",
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
                "parent_port_id": "usbport:a:2",
                "dock_id": "dock:a",
                "topology_label": "拓展坞 1 · USB3-2 · SMRFCT08B",
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
    def test_accepts_versioned_topology_and_dock_network_projection(self) -> None:
        self.assertEqual(validate_discovery_payload(compliant_discovery()), ())

    def test_rejects_duplicate_ports_and_dangling_device_parent(self) -> None:
        payload = compliant_discovery()
        payload["usb_topology"]["usb_ports"][1]["id"] = "usbport:a:1"
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
        const ethernet = "拓展坞网口";
        status.textContent = "端口存在，未检测到兼容设备";
        """
        self.assertEqual(validate_frontend_source(source), ())

        issues = validate_frontend_source("const physical_interface_id = '';")
        self.assertGreaterEqual(len(issues), 4)


if __name__ == "__main__":
    unittest.main()
