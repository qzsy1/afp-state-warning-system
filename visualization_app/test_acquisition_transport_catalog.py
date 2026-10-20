from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acquisition import AcquisitionManager  # noqa: E402


class AcquisitionTransportCatalogTests(unittest.TestCase):
    def test_serial_candidates_include_tc64_without_identity_or_hid_reachability(self):
        serial_ports = [SimpleNamespace(device=f"COM{i+3}", description=label, manufacturer="fixture",
            vid=vid, pid=pid, serial_number="", hwid=hwid, location="")
            for i, (label, vid, pid, hwid) in enumerate([
                ("CH340",0x1A86,0x7523,"USB VID:PID=1A86:7523"),
                ("FTDI",0x0403,0x6001,"USB VID:PID=0403:6001"),
                ("Bluetooth",None,None,"BTHENUM"),
            ])]
        with patch("serial.tools.list_ports.comports",return_value=serial_ports), patch("acquisition.discover_windows_usb_topology",return_value={}), patch("acquisition.enumerate_smrf_hid_devices",return_value=[]), patch("acquisition.socket.create_connection",side_effect=OSError("offline")), patch("acquisition._network_adapter_snapshot",return_value=[]):
            result = AcquisitionManager.discover_interfaces()
        serial = [p for p in result["physical_interfaces"] if p["kind"] == "serial"]
        self.assertEqual(len(serial), 3)
        self.assertTrue(all("thermocouple64" in p["compatible_roles"] for p in serial))
        self.assertTrue(all(p["identity_confirmed"] is False for p in serial))
        self.assertFalse(result["thermocouple_reachable"])
        self.assertTrue(result["thermocouple_discovered"])
        self.assertTrue(all(p.get("transport_family") == "usb" for p in serial[:2]))

    @patch("acquisition.socket.create_connection", side_effect=OSError("offline"))
    @patch("acquisition.enumerate_smrf_hid_devices", return_value=[])
    @patch("serial.tools.list_ports.comports")
    @patch("acquisition.discover_windows_usb_topology")
    def test_discovery_exposes_usb_serial_parent_and_role_independent_catalog(
        self, topology, comports, _smrf, _connect
    ) -> None:
        topology.return_value = {
            "schema_version": 1,
            "state": "complete",
            "provider": "fixture",
            "errors": [],
            "docks": [{"id": "dock:1", "label": "拓展坞 1", "state": "connected"}],
            "usb_ports": [
                {"id": "host:1", "owner_kind": "host", "dock_id": "", "label": "电脑本机 · USB3-1", "state": "empty", "user_connectable": True, "connector_type": "usb3", "supported_protocols": ["usb3"], "device_id": None},
                {"id": "dock:1:1", "owner_kind": "dock", "dock_id": "dock:1", "label": "拓展坞 1 · USB3-1", "state": "occupied", "user_connectable": True, "connector_type": "usb3", "supported_protocols": ["usb3"], "device_id": "device:serial"},
            ],
            "devices": [
                {"id": "device:serial", "parent_port_id": "dock:1:1", "class": "ports", "friendly_name": "USB Serial", "vid": 0x1234, "pid": 0x5678, "serial": "ABC"},
            ],
        }
        comports.return_value = [SimpleNamespace(
            device="COM8", description="USB Serial", manufacturer="Vendor",
            vid=0x1234, pid=0x5678, serial_number="ABC",
            hwid="USB VID:PID=1234:5678", location="1-2",
        )]

        result = AcquisitionManager.discover_interfaces()

        serial = next(item for item in result["physical_interfaces"] if item["id"] == "serial:COM8")
        self.assertEqual(serial["transport_family"], "usb")
        self.assertEqual(serial["endpoint_kind"], "serial")
        self.assertEqual(serial["parent_port_id"], "dock:1:1")
        self.assertEqual(serial["dock_id"], "")
        self.assertTrue(serial["hub_id"].startswith("hub:"))
        self.assertEqual(result["usb_topology"]["schema_version"], 2)
        self.assertEqual(result["usb_topology"]["docks"], [])
        self.assertEqual(
            result["usb_topology"]["hubs"][0]["classification"],
            "unconfirmed_accessory",
        )
        catalog = result["interface_transport_catalog"]
        self.assertEqual([item["id"] for item in catalog["usb_ports"]], ["host:1", "dock:1:1"])
        self.assertEqual(catalog["usb_ports"][1]["endpoints"][0]["id"], "serial:COM8")


if __name__ == "__main__":
    unittest.main()
