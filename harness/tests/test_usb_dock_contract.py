from __future__ import annotations

import unittest

from harness.engine.usb_dock_contract import (
    validate_acquisition_source,
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
                    "id": "usbport:host:1",
                    "owner_kind": "host",
                    "dock_id": "",
                    "system_port_number": 2,
                    "connector_type": "usb3",
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
        const host = "电脑本机 USB";
        const nativeSerial = "主机原生串口";
        const ethernet = "拓展坞网口";
        status.textContent = "端口存在，未检测到兼容设备";
        const USB_SENSOR_ROLES = new Set(["thermocouple", "thermal_uvc", "pressure"]);
        const interfaceTransportFamily = () => "usb";
        item.selection_origin = item.selection_origin || "auto";
        item.selection_origin = "manual";
        """
        self.assertEqual(validate_frontend_source(source), ())

        issues = validate_frontend_source("const physical_interface_id = '';")
        self.assertGreaterEqual(len(issues), 4)

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

        self.assertIn("confirmation must be observed_current or observed_history", issues)

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
