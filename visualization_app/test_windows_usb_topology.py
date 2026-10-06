from __future__ import annotations

import copy
import ctypes
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import windows_usb_topology as topology  # noqa: E402


def _port(
    number: int,
    *,
    state: str = "empty",
    protocol: str = "usb2",
    companion: dict | None = None,
    device: dict | None = None,
    user_connectable: bool | None = True,
    child_hub_path: str = "",
) -> dict:
    return {
        "number": number,
        "state": state,
        "supported_protocols": [protocol],
        "user_connectable": user_connectable,
        "companion": companion,
        "device": device,
        "child_hub_path": child_hub_path,
    }


def _hub(
    hub_id: str,
    *,
    vid: int,
    pid: int,
    path: str,
    ports: list[dict],
    location: str = "PCIROOT(0)#USBROOT(0)#USB(1)",
) -> dict:
    return {
        "id": hub_id,
        "device_path": path,
        "instance_id": f"USB\\VID_{vid:04X}&PID_{pid:04X}\\{hub_id}",
        "parent_instance_id": "USB\\ROOT_HUB30\\ROOT",
        "location_paths": [location],
        "friendly_name": "USB Hub",
        "vid": vid,
        "pid": pid,
        "is_root": False,
        "ports": ports,
    }


def _genesys_fixture(*, mapped: bool = True, asix_pid: int = 0x1790) -> list[dict]:
    slow_path = r"\\?\usb#vid_05e3&pid_0610#slow#{hub}"
    fast_path = r"\\?\usb#vid_05e3&pid_0626#fast#{hub}"
    slow_ports: list[dict] = []
    fast_ports: list[dict] = []
    for number in range(1, 5):
        slow_companion = {"hub_path": fast_path, "port_number": number} if mapped else None
        fast_companion = {"hub_path": slow_path, "port_number": number} if mapped else None
        slow_ports.append(_port(number, companion=slow_companion))
        device = None
        state = "empty"
        if number == 4:
            state = "occupied"
            device = {
                "instance_id": r"USB\VID_0B95&PID_%04X\SERIAL" % asix_pid,
                "friendly_name": "ASIX USB Ethernet",
                "class": "net",
                "vid": 0x0B95,
                "pid": asix_pid,
                "serial": "SERIAL",
                "network_name": "以太网",
            }
        fast_ports.append(
            _port(
                number,
                state=state,
                protocol="usb3",
                companion=fast_companion,
                device=device,
            )
        )
    return [
        _hub("slow", vid=0x05E3, pid=0x0610, path=slow_path, ports=slow_ports),
        _hub("fast", vid=0x05E3, pid=0x0626, path=fast_path, ports=fast_ports),
    ]


class WindowsUsbTopologyTests(unittest.TestCase):
    def test_windows_sdk_structure_sizes_and_ioctl_values(self) -> None:
        expected_sizes = {
            topology.USB_DEVICE_DESCRIPTOR: 18,
            topology.USB_HUB_DESCRIPTOR: 71,
            topology.USB_30_HUB_DESCRIPTOR: 12,
            topology.USB_HUB_INFORMATION_EX: 77,
            topology.USB_NODE_CONNECTION_INFORMATION_EX: 35,
            topology.USB_NODE_CONNECTION_INFORMATION_EX_V2: 16,
            topology.USB_PORT_CONNECTOR_PROPERTIES: 18,
        }
        for structure, expected in expected_sizes.items():
            self.assertEqual(ctypes.sizeof(structure), expected, structure.__name__)
        self.assertEqual(topology.IOCTL_USB_GET_NODE_INFORMATION, 0x220408)
        self.assertEqual(topology.IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX, 0x220448)
        self.assertEqual(topology.IOCTL_USB_GET_HUB_INFORMATION_EX, 0x220454)
        self.assertEqual(topology.IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES, 0x220458)
        self.assertEqual(topology.IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2, 0x22045C)
        self.assertEqual(topology.USB_NODE_CONNECTION_INFORMATION_EX.ConnectionStatus.offset, 31)
        self.assertEqual(topology.USB_PORT_CONNECTOR_PROPERTIES.CompanionPortNumber.offset, 14)

    def test_companion_hubs_become_three_external_ports_and_one_dock_ethernet(self) -> None:
        result = topology.normalize_usb_topology(_genesys_fixture())

        self.assertEqual(result["state"], "complete")
        self.assertEqual(len(result["docks"]), 1)
        external = [port for port in result["usb_ports"] if port["user_connectable"]]
        internal = [port for port in result["usb_ports"] if not port["user_connectable"]]
        self.assertEqual(len(external), 3)
        self.assertEqual(len(internal), 1)
        self.assertTrue(all(port["owner_kind"] == "dock" for port in result["usb_ports"]))
        self.assertTrue(all(port["merge_state"] == "companion" for port in result["usb_ports"]))
        self.assertEqual(internal[0]["internal_function"], "ethernet")
        device = next(item for item in result["devices"] if item["id"] == internal[0]["device_id"])
        self.assertEqual((device["vid"], device["pid"]), (0x0B95, 0x1790))
        self.assertEqual(device["network_name"], "以太网")

    def test_missing_companion_mapping_does_not_guess_a_merge(self) -> None:
        result = topology.normalize_usb_topology(_genesys_fixture(mapped=False))

        self.assertEqual(len(result["docks"]), 2)
        self.assertEqual(len(result["usb_ports"]), 8)
        self.assertTrue(all(port["merge_state"] == "independent" for port in result["usb_ports"]))

    def test_partial_hardware_fingerprint_keeps_fourth_port_visible(self) -> None:
        result = topology.normalize_usb_topology(_genesys_fixture(asix_pid=0x9999))

        self.assertEqual(len([port for port in result["usb_ports"] if port["user_connectable"]]), 4)
        self.assertFalse(any(port["internal_function"] == "ethernet" for port in result["usb_ports"]))

    def test_stable_ids_survive_refresh_but_change_with_upstream_location(self) -> None:
        fixture = _genesys_fixture()
        first = topology.normalize_usb_topology(fixture)
        second = topology.normalize_usb_topology(copy.deepcopy(fixture))
        moved = copy.deepcopy(fixture)
        for hub in moved:
            hub["location_paths"] = ["PCIROOT(0)#USBROOT(1)#USB(7)"]
        third = topology.normalize_usb_topology(moved)

        self.assertEqual(first["docks"][0]["id"], second["docks"][0]["id"])
        self.assertEqual(
            [port["id"] for port in first["usb_ports"]],
            [port["id"] for port in second["usb_ports"]],
        )
        self.assertNotEqual(first["docks"][0]["id"], third["docks"][0]["id"])

    def test_nested_hub_records_parent_port_without_raw_path(self) -> None:
        child_path = r"\\?\usb#vid_1234&pid_0002#child#{hub}"
        parent = _hub(
            "parent",
            vid=0x1234,
            pid=0x0001,
            path=r"\\?\usb#vid_1234&pid_0001#parent#{hub}",
            ports=[
                _port(
                    1,
                    state="occupied",
                    child_hub_path=child_path,
                    device={"vid": 0x1234, "pid": 0x0002, "class": "hub"},
                )
            ],
        )
        child = _hub(
            "child",
            vid=0x1234,
            pid=0x0002,
            path=child_path,
            ports=[_port(1)],
            location="PCIROOT(0)#USBROOT(0)#USB(1)#USB(1)",
        )

        result = topology.normalize_usb_topology([parent, child])
        nested = next(dock for dock in result["docks"] if dock.get("parent_port_id"))

        self.assertIn(nested["parent_port_id"], {port["id"] for port in result["usb_ports"]})
        self.assertNotIn("device_path", nested)

    def test_partial_errors_and_unsupported_platform_are_nonfatal(self) -> None:
        result = topology.normalize_usb_topology(_genesys_fixture(), errors=["one hub failed"])
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["errors"], ["one hub failed"])
        with patch.object(topology.sys, "platform", "linux"):
            unavailable = topology.discover_windows_usb_topology()
        self.assertEqual(unavailable["state"], "unavailable")
        self.assertEqual(unavailable["usb_ports"], [])

    def test_host_user_connectable_ports_join_dock_ports_and_skip_upstream_link(self) -> None:
        dock_hubs = _genesys_fixture()
        root_path = r"\\?\usb#root_hub30#root#{hub}"
        root = _hub(
            "root",
            vid=0,
            pid=0,
            path=root_path,
            ports=[
                _port(1, child_hub_path=dock_hubs[0]["device_path"]),
                _port(2, protocol="usb3", user_connectable=True),
                _port(3, protocol="usb3", user_connectable=False),
            ],
            location="PCIROOT(0)#USBROOT(0)",
        )
        root["is_root"] = True
        root["instance_id"] = r"USB\ROOT_HUB30\ROOT"

        result = topology.normalize_usb_topology([root, *dock_hubs])

        host_ports = [item for item in result["usb_ports"] if item["owner_kind"] == "host"]
        dock_ports = [item for item in result["usb_ports"] if item["owner_kind"] == "dock"]
        self.assertEqual(len(host_ports), 1)
        self.assertEqual(host_ports[0]["system_port_number"], 2)
        self.assertIn("电脑本机", host_ports[0]["label"])
        self.assertEqual(len(dock_ports), 4)

    def test_locationless_root_hub_is_not_published_as_empty_host_connectors(self) -> None:
        root = _hub(
            "virtual-root",
            vid=0,
            pid=0,
            path=r"\\?\usb#root_hub30#virtual#{hub}",
            ports=[_port(1), _port(2, protocol="usb3")],
            location="",
        )
        root["is_root"] = True
        root["location_paths"] = []

        result = topology.normalize_usb_topology([root])

        self.assertEqual(result["usb_ports"], [])


if __name__ == "__main__":
    unittest.main()
