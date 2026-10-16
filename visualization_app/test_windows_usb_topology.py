from __future__ import annotations

import copy
import ctypes
import json
import os
import sys
import tempfile
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
    device_is_hub: bool = False,
    connector_is_type_c: bool | None = None,
) -> dict:
    return {
        "number": number,
        "state": state,
        "supported_protocols": [protocol],
        "user_connectable": user_connectable,
        "companion": companion,
        "device": device,
        "child_hub_path": child_hub_path,
        "device_is_hub": device_is_hub,
        "connector_is_type_c": connector_is_type_c,
    }


def _hub(
    hub_id: str,
    *,
    vid: int,
    pid: int,
    path: str,
    ports: list[dict],
    location: str = "PCIROOT(0)#USBROOT(0)#USB(1)",
    container_id: str | None = None,
    removal_policy: int | None = 3,
    device_categories: list[str] | None = None,
    container_function_classes: list[str] | None = None,
) -> dict:
    return {
        "id": hub_id,
        "device_path": path,
        "instance_id": f"USB\\VID_{vid:04X}&PID_{pid:04X}\\{hub_id}",
        "parent_instance_id": "USB\\ROOT_HUB30\\ROOT",
        "location_paths": [location],
        "friendly_name": "USB Hub",
        "container_id": f"fixture-container-{hub_id}" if container_id is None else container_id,
        "removal_policy": removal_policy,
        "device_categories": ["dock"] if device_categories is None else device_categories,
        "container_function_classes": ["hub"] if container_function_classes is None else container_function_classes,
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
        _hub(
            "slow", vid=0x05E3, pid=0x0610, path=slow_path, ports=slow_ports,
            container_id="fixture-genesys-dock",
        ),
        _hub(
            "fast", vid=0x05E3, pid=0x0626, path=fast_path, ports=fast_ports,
            container_id="fixture-genesys-dock",
        ),
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
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(len(result["docks"]), 1)
        self.assertEqual(result["hubs"], [])
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

        self.assertEqual(len(result["docks"]), 1)
        self.assertEqual(len(result["usb_ports"]), 8)
        self.assertTrue(all(port["merge_state"] == "independent" for port in result["usb_ports"]))

    def test_partial_hardware_fingerprint_keeps_fourth_port_visible(self) -> None:
        result = topology.normalize_usb_topology(_genesys_fixture(asix_pid=0x9999))

        self.assertEqual(len([port for port in result["usb_ports"] if port["user_connectable"]]), 4)
        self.assertFalse(any(port["internal_function"] == "ethernet" for port in result["usb_ports"]))

    def test_stable_ids_survive_refresh_and_host_location_changes(self) -> None:
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
        self.assertEqual(first["docks"][0]["id"], third["docks"][0]["id"])

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
            container_id="fixture-child-dock",
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

    def test_host_ports_require_observation_and_keep_dock_upstream_connector(self) -> None:
        dock_hubs = _genesys_fixture()
        root_path = r"\\?\usb#root_hub30#root#{hub}"
        root = _hub(
            "root",
            vid=0,
            pid=0,
            path=root_path,
            ports=[
                _port(
                    1,
                    state="occupied",
                    child_hub_path=dock_hubs[0]["device_path"],
                    device_is_hub=True,
                    device={
                        "instance_id": dock_hubs[0]["instance_id"],
                        "friendly_name": "USB Hub",
                        "class": "hub",
                        "vid": 0x05E3,
                        "pid": 0x0610,
                    },
                ),
                _port(
                    2,
                    state="occupied",
                    protocol="usb3",
                    user_connectable=True,
                    device={
                        "instance_id": r"USB\VID_1234&PID_5678\EXTERNAL",
                        "friendly_name": "External USB device",
                        "class": "hid",
                        "vid": 0x1234,
                        "pid": 0x5678,
                    },
                ),
                _port(3, protocol="usb3", user_connectable=True),
                _port(4, protocol="usb3", user_connectable=False),
            ],
            location="PCIROOT(0)#USBROOT(0)",
            container_id="fixture-system",
        )
        root["is_root"] = True
        root["instance_id"] = r"USB\ROOT_HUB30\ROOT"
        root["container_id"] = "fixture-system"

        result = topology.normalize_usb_topology([root, *dock_hubs])

        host_ports = [item for item in result["usb_ports"] if item["owner_kind"] == "host"]
        dock_ports = [item for item in result["usb_ports"] if item["owner_kind"] == "dock"]
        self.assertEqual(
            [port["system_port_number"] for port in host_ports],
            [1, 2],
        )
        self.assertTrue(all("电脑本机" in port["label"] for port in host_ports))
        self.assertTrue(all(port["confirmation"] == "observed_current" for port in host_ports))
        self.assertEqual(host_ports[0]["internal_function"], "dock_upstream")
        self.assertEqual(len(dock_ports), 4)

    def test_type_c_is_separate_and_companion_metadata_confirms_two_empty_usb_a_ports(self) -> None:
        root_path = r"\\?\usb#root_hub30#root#{hub}"
        root = _hub(
            "root",
            vid=0,
            pid=0,
            path=root_path,
            ports=[
                _port(
                    1,
                    state="occupied",
                    protocol="usb2",
                    connector_is_type_c=True,
                    device={
                        "instance_id": r"USB\VID_05E3&PID_0610\DOCK",
                        "friendly_name": "USB Hub",
                        "class": "hub",
                        "vid": 0x05E3,
                        "pid": 0x0610,
                    },
                ),
                _port(8, connector_is_type_c=False, companion={"hub_path": root_path, "port_number": 13}),
                _port(9, connector_is_type_c=False, companion={"hub_path": root_path, "port_number": 14}),
                _port(11, connector_is_type_c=False),
                _port(12, connector_is_type_c=False),
                _port(13, protocol="usb3", connector_is_type_c=False, companion={"hub_path": root_path, "port_number": 8}),
                _port(14, protocol="usb3", connector_is_type_c=False, companion={"hub_path": root_path, "port_number": 9}),
            ],
            location="PCIROOT(0)#USBROOT(0)",
            container_id="fixture-system",
        )
        root["is_root"] = True
        root["instance_id"] = r"USB\ROOT_HUB30\ROOT"
        root["container_id"] = "fixture-system"

        result = topology.normalize_usb_topology([root])

        host_ports = [item for item in result["usb_ports"] if item["owner_kind"] == "host"]
        usb_a = [item for item in host_ports if item["connector_form_factor"] == "type_a"]
        type_c = [item for item in host_ports if item["connector_form_factor"] == "type_c"]
        self.assertEqual([port["system_port_number"] for port in usb_a], [8, 9])
        self.assertTrue(all(port["confirmation"] == "connector_metadata" for port in usb_a))
        self.assertTrue(all(port["state"] == "empty" for port in usb_a))
        self.assertEqual(len(type_c), 1)
        self.assertEqual(type_c[0]["confirmation"], "observed_current")
        self.assertTrue(type_c[0]["label"].startswith("电脑本机 · Type-C-1"))
        self.assertTrue(all("USB-A-" in port["label"] for port in usb_a))
        self.assertNotIn(11, [port["system_port_number"] for port in host_ports])
        self.assertNotIn(12, [port["system_port_number"] for port in host_ports])

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

    def test_observed_host_port_remains_visible_after_device_is_removed(self) -> None:
        root = _hub(
            "root",
            vid=0,
            pid=0,
            path=r"\\?\usb#root_hub30#root#{hub}",
            ports=[
                _port(
                    2,
                    state="occupied",
                    protocol="usb3",
                    device={
                        "instance_id": r"USB\VID_1234&PID_5678\EXTERNAL",
                        "friendly_name": "External USB device",
                        "class": "hid",
                        "vid": 0x1234,
                        "pid": 0x5678,
                    },
                ),
                _port(3, protocol="usb3"),
            ],
            location="PCIROOT(0)#USBROOT(0)",
            container_id="fixture-system",
        )
        root["is_root"] = True
        root["instance_id"] = r"USB\ROOT_HUB30\ROOT"
        root["container_id"] = "fixture-system"
        empty_root = copy.deepcopy(root)
        empty_root["ports"][0]["state"] = "empty"
        empty_root["ports"][0]["device"] = None

        fake_api = unittest.mock.Mock()
        fake_api.enumerate_devices.return_value = {}
        fake_api.enumerate_hub_interfaces.return_value = [{"device_path": root["device_path"]}]
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = str(Path(temporary) / "observed-host-ports.json")
            with (
                patch.dict(os.environ, {"AFP_USB_HOST_PORT_REGISTRY": registry_path}),
                patch.object(topology, "_WindowsApi", return_value=fake_api),
                patch.object(
                    topology,
                    "_read_hub",
                    side_effect=[(root, []), (empty_root, [])],
                ),
            ):
                first = topology.discover_windows_usb_topology()
                second = topology.discover_windows_usb_topology()

            first_host = [port for port in first["usb_ports"] if port["owner_kind"] == "host"]
            second_host = [port for port in second["usb_ports"] if port["owner_kind"] == "host"]
            self.assertEqual([port["system_port_number"] for port in first_host], [2])
            self.assertEqual([port["system_port_number"] for port in second_host], [2])
            self.assertEqual(first_host[0]["id"], second_host[0]["id"])
            self.assertEqual(first_host[0]["confirmation"], "observed_current")
            self.assertEqual(second_host[0]["confirmation"], "observed_history")
            self.assertTrue(Path(registry_path).is_file())

    def test_integrated_non_root_hub_is_host_owned(self) -> None:
        child_path = r"\\?\usb#integrated#hub#{fixture}"
        root = _hub(
            "root-system",
            vid=0,
            pid=0,
            path=r"\\?\usb#root_hub30#system#{fixture}",
            ports=[_port(1, state="occupied", user_connectable=False, child_hub_path=child_path)],
            location="HOST-A#ROOT-A",
            container_id="system-container",
            removal_policy=1,
            device_categories=[],
        )
        root["is_root"] = True
        root["instance_id"] = r"USB\ROOT_HUB30\SYSTEM"
        integrated = _hub(
            "integrated",
            vid=0x1234,
            pid=0x0001,
            path=child_path,
            ports=[_port(1, state="occupied", device={"class": "hid", "vid": 1, "pid": 2})],
            location="HOST-A#ROOT-A#INTERNAL",
            container_id="system-container",
            removal_policy=1,
            device_categories=[],
        )

        result = topology.normalize_usb_topology([root, integrated])

        self.assertEqual(result["docks"], [])
        self.assertEqual(result["hubs"], [])
        self.assertEqual({port["owner_kind"] for port in result["usb_ports"]}, {"host"})

    def test_external_plain_hub_is_not_dock(self) -> None:
        hub = _hub(
            "plain",
            vid=0x1234,
            pid=0x5678,
            path=r"\\?\usb#plain#hub#{fixture}",
            ports=[_port(1), _port(2, protocol="usb3")],
            container_id="plain-hub-container",
            removal_policy=3,
            device_categories=[],
            container_function_classes=["hub"],
        )

        result = topology.normalize_usb_topology([hub])

        self.assertEqual(result["docks"], [])
        self.assertEqual(result["hubs"][0]["classification"], "external_hub")
        self.assertNotIn("拓展坞", result["hubs"][0]["label"])
        self.assertTrue(all(port["owner_kind"] == "hub" for port in result["usb_ports"]))

    def test_multifunction_external_container_is_dock(self) -> None:
        hub = _hub(
            "multifunction",
            vid=0x4321,
            pid=0x8765,
            path=r"\\?\usb#multi#hub#{fixture}",
            ports=[_port(1), _port(2, protocol="usb3")],
            container_id="multifunction-container",
            removal_policy=2,
            device_categories=[],
            container_function_classes=["hub", "net", "audio"],
        )

        result = topology.normalize_usb_topology([hub])

        self.assertEqual(result["hubs"], [])
        self.assertEqual(result["docks"][0]["classification"], "confirmed_dock")
        self.assertIn("external_container", result["docks"][0]["classification_evidence"])
        self.assertIn("multifunction_container", result["docks"][0]["classification_evidence"])

    def test_classification_does_not_depend_on_host_controller_or_location(self) -> None:
        first_fixture = _genesys_fixture()
        second_fixture = copy.deepcopy(first_fixture)
        for index, hub in enumerate(second_fixture, 1):
            hub["id"] = f"other-host-hub-{index}"
            hub["device_path"] = rf"\\?\usb#other-controller#{index}#{{fixture}}"
            hub["location_paths"] = [f"HOST-B#ROOT-Z#USB({index + 7})"]
        for hub in second_fixture:
            for port in hub["ports"]:
                port["companion"] = None

        first = topology.normalize_usb_topology(first_fixture)
        second = topology.normalize_usb_topology(second_fixture)

        self.assertEqual([item["classification"] for item in first["docks"]], ["confirmed_dock"])
        self.assertEqual([item["classification"] for item in second["docks"]], ["confirmed_dock"])
        self.assertEqual({port["owner_kind"] for port in first["usb_ports"]}, {"dock"})
        self.assertEqual({port["owner_kind"] for port in second["usb_ports"]}, {"dock"})

    def test_incomplete_evidence_keeps_ports_as_unconfirmed_hub(self) -> None:
        hub = _hub(
            "unknown",
            vid=0x05E3,
            pid=0x0626,
            path=r"\\?\usb#unknown#hub#{fixture}",
            ports=[_port(1), _port(2), _port(3)],
            container_id="",
            removal_policy=None,
            device_categories=[],
            container_function_classes=["hub"],
        )
        hub["friendly_name"] = "Three Port Dock"

        result = topology.normalize_usb_topology([hub])

        self.assertEqual(result["docks"], [])
        self.assertEqual(result["hubs"][0]["classification"], "unconfirmed_accessory")
        self.assertIn("insufficient_identity", result["hubs"][0]["classification_evidence"])
        self.assertEqual(len(result["usb_ports"]), 3)

    def test_pnp_identity_fields_are_sanitized_in_public_topology(self) -> None:
        hub = _hub(
            "privacy",
            vid=0x1111,
            pid=0x2222,
            path=r"\\?\usb#privacy#hub#{fixture}",
            ports=[_port(1, state="occupied", device={
                "instance_id": r"USB\VID_1111&PID_2222\MACHINE-SERIAL",
                "parent_instance_id": r"PCI\HOST-CONTROLLER\PRIVATE",
                "container_id": "private-container-guid",
                "location_paths": ["PRIVATE-HOST#ROOT#USB(9)"],
                "removal_policy": 3,
                "device_category": "hid",
                "class": "hid",
                "vid": 0x1111,
                "pid": 0x2222,
            })],
            container_id="private-container-guid",
            device_categories=[],
            container_function_classes=["hub"],
        )

        serialized = json.dumps(topology.normalize_usb_topology([hub]))

        self.assertNotIn("private-container-guid", serialized)
        self.assertNotIn("HOST-CONTROLLER", serialized)
        self.assertNotIn("PRIVATE-HOST", serialized)
        self.assertIn("container_key", serialized)
        self.assertIn("parent_key", serialized)


if __name__ == "__main__":
    unittest.main()
