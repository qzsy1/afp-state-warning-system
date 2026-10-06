from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from interface_transport_catalog import enrich_interface_transports  # noqa: E402


class InterfaceTransportCatalogTests(unittest.TestCase):
    def test_usb_serial_is_nested_under_port_and_native_serial_stays_separate(self) -> None:
        topology = {
            "docks": [{"id": "dock:1", "label": "拓展坞 1"}],
            "usb_ports": [
                {"id": "usb:host", "owner_kind": "host", "dock_id": "", "label": "电脑本机 · USB3-1", "state": "empty", "user_connectable": True},
                {"id": "usb:dock", "owner_kind": "dock", "dock_id": "dock:1", "label": "拓展坞 1 · USB3-1", "state": "occupied", "user_connectable": True, "device_id": "dev:serial"},
            ],
            "devices": [
                {"id": "dev:serial", "parent_port_id": "usb:dock", "class": "ports", "friendly_name": "USB Serial", "vid": 0x1234, "pid": 0x5678, "serial": "ABC"},
            ],
        }
        physical = [
            {"id": "serial:COM8", "kind": "serial", "endpoint": "COM8", "vid": 0x1234, "pid": 0x5678, "serial": "ABC", "label": "串口 COM8", "detected": True},
            {"id": "serial:COM1", "kind": "serial", "endpoint": "COM1", "label": "串口 COM1", "detected": True},
        ]

        catalog = enrich_interface_transports(topology, physical)

        self.assertEqual(physical[0]["transport_family"], "usb")
        self.assertEqual(physical[0]["parent_port_id"], "usb:dock")
        self.assertEqual(physical[0]["dock_id"], "dock:1")
        self.assertEqual(physical[1]["transport_family"], "serial_native")
        dock = next(item for item in catalog["usb_ports"] if item["id"] == "usb:dock")
        self.assertEqual([item["id"] for item in dock["endpoints"]], ["serial:COM8"])
        self.assertEqual(catalog["native_serial_interface_ids"], ["serial:COM1"])

    def test_hid_uvc_and_unlocated_usb_are_preserved_without_duplicates(self) -> None:
        topology = {
            "docks": [],
            "usb_ports": [{"id": "usb:1", "owner_kind": "host", "dock_id": "", "label": "电脑本机 · USB3-1", "state": "occupied", "user_connectable": True, "device_id": "dev:hid"}],
            "devices": [{"id": "dev:hid", "parent_port_id": "usb:1", "class": "hid", "friendly_name": "SMRF", "vid": 1, "pid": 2, "serial": "S"}],
        }
        physical = [
            {"id": "hid:one", "kind": "usb_hid", "vid": 1, "pid": 2, "serial": "S", "label": "SMRF", "detected": True},
            {"id": "uvc:bsv", "kind": "usb_uvc", "label": "BSV UVC", "detected": False, "driver_available": True},
        ]

        catalog = enrich_interface_transports(topology, physical)

        self.assertEqual(catalog["usb_ports"][0]["endpoints"][0]["id"], "hid:one")
        self.assertEqual(catalog["unlocated_usb_interface_ids"], ["uvc:bsv"])

    def test_pnp_instance_key_has_priority_over_ambiguous_vid_pid(self) -> None:
        import hashlib

        instance = r"USB\VID_1234&PID_5678\ONE"
        key = "pnp:" + hashlib.sha256(instance.lower().encode()).hexdigest()[:20]
        topology = {
            "docks": [],
            "usb_ports": [
                {"id": "usb:1", "owner_kind": "host", "dock_id": "", "label": "USB 1", "state": "occupied", "user_connectable": True},
                {"id": "usb:2", "owner_kind": "host", "dock_id": "", "label": "USB 2", "state": "occupied", "user_connectable": True},
            ],
            "devices": [
                {"id": "dev:1", "parent_port_id": "usb:1", "instance_key": key, "vid": 0x1234, "pid": 0x5678, "serial": ""},
                {"id": "dev:2", "parent_port_id": "usb:2", "vid": 0x1234, "pid": 0x5678, "serial": ""},
            ],
        }
        physical = [{"id": "serial:COM8", "kind": "serial", "pnp_instance_id": instance, "vid": 0x1234, "pid": 0x5678}]

        enrich_interface_transports(topology, physical)

        self.assertEqual(physical[0]["parent_port_id"], "usb:1")


if __name__ == "__main__":
    unittest.main()
