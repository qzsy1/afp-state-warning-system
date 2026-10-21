from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from interface_transport_catalog import (  # noqa: E402
    enrich_interface_transports,
    normalize_usb_topology_contract,
)


class InterfaceTransportCatalogTests(unittest.TestCase):
    def serial_fixture(self, path: str = "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(9)#USB(3)") -> tuple[dict, list[dict]]:
        from test_windows_usb_topology import _genesys_fixture
        from windows_usb_topology import normalize_usb_topology

        hubs = _genesys_fixture()
        hubs[0]["ports"][2].update(state="occupied", device={
            "instance_id": r"USB\VID_1A86&PID_7523\fixture-esr",
            "class": "ports", "vid": 0x1A86, "pid": 0x7523,
            "location_paths": [path],
        })
        current = normalize_usb_topology(hubs)
        interfaces = [{"id": "serial:COM91", "kind": "serial", "endpoint": "COM91",
                       "vid": 0x1A86, "pid": 0x7523, "location": "1-9.3", "detected": True}]
        return current, interfaces

    def test_windows_route_associates_serial_without_claiming_protocol(self) -> None:
        current, interfaces = self.serial_fixture()
        catalog = enrich_interface_transports(current, interfaces)
        parent = interfaces[0]["parent_port_id"]
        port = next(p for p in catalog["usb_ports"] if p["id"] == parent)
        self.assertEqual([e["endpoint"] for e in port["endpoints"]], ["COM91"])
        self.assertEqual(catalog["unlocated_usb_interface_ids"], [])
        self.assertFalse(interfaces[0].get("identity_confirmed", False))
        self.assertFalse(interfaces[0].get("physical_verified", False))

    def test_windows_host_route_and_composite_interface(self) -> None:
        from test_windows_usb_topology import _port
        from windows_usb_topology import normalize_usb_topology
        for path, location in [("PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(8)", "1-8"),
                               ("PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(8)#USBMI(2)", "1-8:x.2")]:
            with self.subTest(path=path):
                current = normalize_usb_topology([{
                    "id": "root", "is_root": True, "instance_id": r"USB\ROOT_HUB30\fixture",
                    "location_paths": ["PCIROOT(0)#PCI(1400)#USBROOT(0)"],
                    "ports": [_port(8, state="occupied", device={
                        "instance_id": r"USB\VID_1A86&PID_7523\fixture-tc64",
                        "vid": 0x1A86, "pid": 0x7523, "location_paths": [path]})],
                }])
                interfaces = [{"id": "serial:COM92", "kind": "serial", "vid": 0x1A86,
                               "pid": 0x7523, "location": location}]
                enrich_interface_transports(current, interfaces)
                self.assertTrue(interfaces[0].get("parent_port_id"))

    def test_route_ambiguity_movement_and_chip_mismatch_do_not_rebind(self) -> None:
        from copy import deepcopy
        for case in ("ambiguous", "moved", "different_chip"):
            with self.subTest(case=case):
                current, interfaces = self.serial_fixture()
                if case == "ambiguous":
                    duplicate = deepcopy(next(d for d in current["devices"] if d.get("vid") == 0x1A86))
                    duplicate.update(id="other-controller-device", parent_port_id="other-controller-port")
                    current["devices"].append(duplicate)
                elif case == "moved":
                    interfaces[0]["location"] = "1-9.2"
                else:
                    interfaces[0]["pid"] = 0x5523
                catalog = enrich_interface_transports(current, interfaces)
                self.assertNotIn("parent_port_id", interfaces[0])
                self.assertEqual(catalog["unlocated_usb_interface_ids"], ["serial:COM91"])

    def test_usb_serial_is_nested_under_port_and_native_serial_stays_separate(self) -> None:
        topology = {
            "schema_version": 2,
            "docks": [{"id": "dock:1", "label": "拓展坞 1", "classification": "confirmed_dock", "classification_evidence": ["external_container", "multifunction_container"]}],
            "hubs": [],
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

    def test_schema_v1_dock_identity_is_downgraded_to_unconfirmed_hub(self) -> None:
        legacy = {
            "schema_version": 1,
            "state": "complete",
            "provider": "old-helper",
            "errors": [],
            "docks": [{"id": "dock:legacy", "label": "拓展坞 1"}],
            "usb_ports": [{
                "id": "usb:legacy",
                "owner_kind": "dock",
                "dock_id": "dock:legacy",
                "label": "拓展坞 1 · USB3-1",
                "state": "empty",
                "user_connectable": True,
            }],
            "devices": [],
        }

        current = normalize_usb_topology_contract(legacy)

        self.assertEqual(current["schema_version"], 2)
        self.assertEqual(current["docks"], [])
        self.assertEqual(current["hubs"][0]["classification"], "unconfirmed_accessory")
        self.assertEqual(current["usb_ports"][0]["owner_kind"], "hub")
        self.assertEqual(current["usb_ports"][0]["dock_id"], "")
        self.assertNotIn("拓展坞", current["hubs"][0]["label"])
        self.assertNotIn("拓展坞", current["usb_ports"][0]["label"])

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
