from __future__ import annotations

import sys
import inspect
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

import acquisition  # noqa: E402
from real_acquisition import ReadinessSnapshot, readiness_config_fingerprint  # noqa: E402


class InterfaceBindingModelTests(unittest.TestCase):
    def test_old_network_config_adds_separate_binding_defaults_without_changing_endpoint(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v11_3",
            interfaces=[
                {
                    "id": "plc_process",
                    "role": "plc",
                    "driver": "modbus_tcp",
                    "endpoint": "192.168.125.5:502",
                }
            ],
            selected_sensors=["温度", "压力", "张力"],
        )

        interface = config.interfaces[0]
        self.assertEqual(interface["endpoint"], "192.168.125.5:502")
        self.assertEqual(interface["physical_interface_id"], "")
        self.assertEqual(interface["physical_port_id"], "")
        self.assertEqual(interface["physical_interface_kind"], "ethernet")
        self.assertEqual(interface.get("physical_transport_family"), "ethernet")
        self.assertEqual(interface.get("device_identity"), {})

    def test_binding_and_device_identity_survive_config_serialization_round_trip(self) -> None:
        original = acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v11_3",
            interfaces=[
                {
                    "id": "uvc_temperature",
                    "role": "thermal_uvc",
                    "driver": "uvc_thermal",
                    "endpoint": "BSV UVC (WinUSB)",
                    "physical_interface_id": "uvc:2bdf:0102",
                    "physical_port_id": "usbport:external:1",
                    "physical_interface_kind": "usb_uvc",
                    "physical_transport_family": "usb",
                    "device_identity": {
                        "instance_id": r"USB\VID_2BDF&PID_0102&MI_00\FIXTURE",
                        "container_id": "fixture-container",
                        "vid": 0x2BDF,
                        "pid": 0x0102,
                        "service": "usbvideo",
                    },
                }
            ],
            selected_sensors=["ROI平均温度"],
        )

        restored = acquisition.AcquisitionConfig(**asdict(original))
        interface = restored.interfaces[0]

        self.assertEqual(interface["endpoint"], "BSV UVC (WinUSB)")
        self.assertEqual(interface["physical_interface_id"], "uvc:2bdf:0102")
        self.assertEqual(interface["physical_port_id"], "usbport:external:1")
        self.assertEqual(interface["physical_interface_kind"], "usb_uvc")
        self.assertEqual(interface["physical_transport_family"], "usb")
        self.assertEqual(
            interface["device_identity"],
            {
                "instance_id": r"USB\VID_2BDF&PID_0102&MI_00\FIXTURE",
                "container_id": "fixture-container",
                "vid": 0x2BDF,
                "pid": 0x0102,
                "service": "usbvideo",
            },
        )


class NetworkRouteFactTests(unittest.TestCase):
    def test_network_snapshot_uses_windows_fallback_without_psutil(self) -> None:
        expected = [{
            "id": "ethernet:以太网",
            "name": "以太网",
            "description": "ASIX USB Gigabit Ethernet",
            "ipv4_bindings": [
                {"address": "192.168.125.209", "netmask": "255.255.255.0"}
            ],
        }]

        with patch.dict(sys.modules, {"psutil": None}), patch(
            "acquisition._windows_network_adapter_snapshot",
            return_value=expected,
            create=True,
        ) as windows_snapshot:
            actual = acquisition._network_adapter_snapshot()

        self.assertEqual(actual, expected)
        windows_snapshot.assert_called_once_with()

    def test_network_facts_rank_only_direct_physical_route_for_industrial_targets(self) -> None:
        build_facts = getattr(acquisition, "network_interface_route_facts", None)
        self.assertTrue(callable(build_facts), "network route fact builder is required")
        targets = {"plc": "192.168.125.5", "robot": "192.168.125.1"}
        route_sources = {"plc": "192.168.125.209", "robot": "192.168.125.209"}

        industrial = build_facts(
            "以太网",
            "ASIX USB to Gigabit Ethernet Family Adapter",
            [{"address": "192.168.125.209", "netmask": "255.255.255.0"}],
            targets,
            route_sources,
        )
        apipa = build_facts(
            "蓝牙网络连接",
            "Bluetooth Device (Personal Area Network)",
            [{"address": "169.254.123.214", "netmask": "255.255.0.0"}],
            targets,
            route_sources,
        )
        wlan = build_facts(
            "WLAN",
            "Intel Wi-Fi 6 AX201",
            [{"address": "192.168.15.162", "netmask": "255.255.255.0"}],
            targets,
            route_sources,
        )
        tunnel = build_facts(
            "FlClash",
            "Meta Tunnel",
            [{"address": "198.18.0.1", "netmask": "255.254.0.0"}],
            targets,
            route_sources,
        )

        self.assertEqual(industrial["interface_type"], "physical")
        self.assertTrue(industrial["target_routes"]["plc"]["auto_eligible"])
        self.assertTrue(industrial["target_routes"]["robot"]["auto_eligible"])
        self.assertEqual(industrial["target_routes"]["plc"]["source_address"], "192.168.125.209")
        self.assertTrue(apipa["apipa_only"])
        self.assertFalse(apipa["target_routes"]["plc"]["auto_eligible"])
        self.assertEqual(wlan["interface_type"], "wireless")
        self.assertFalse(wlan["target_routes"]["plc"]["auto_eligible"])
        self.assertEqual(tunnel["interface_type"], "virtual")
        self.assertFalse(tunnel["target_routes"]["plc"]["auto_eligible"])


class NetworkPathGateTests(unittest.TestCase):
    def test_gate_uses_tcp_source_when_udp_route_appears_direct(self) -> None:
        with patch("acquisition.socket.create_connection") as connect, patch(
            "acquisition._route_source_address", return_value="192.168.125.209"
        ):
            connected = connect.return_value.__enter__.return_value
            connected.getsockname.return_value = ("198.18.0.1", 40000)
            result = acquisition.validate_network_path(
                self.interface, adapter_snapshot=self.adapters
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["actual_source_address"], "198.18.0.1")
        self.assertEqual(result["actual_interface_id"], "ethernet:FlClash")
        connect.assert_called_once_with(("192.168.125.5", 502), timeout=2.0)
        connected.sendall.assert_not_called()

    def setUp(self) -> None:
        self.interface = {
            "id": "plc_process",
            "role": "plc",
            "driver": "modbus_tcp",
            "endpoint": "192.168.125.5:502",
            "physical_interface_id": "ethernet:以太网",
            "physical_interface_kind": "ethernet",
            "physical_verified": True,
        }
        self.adapters = [
            {
                "id": "ethernet:以太网",
                "name": "以太网",
                "description": "ASIX USB to Gigabit Ethernet Family Adapter",
                "ipv4_bindings": [
                    {"address": "192.168.125.209", "netmask": "255.255.255.0"}
                ],
            },
            {
                "id": "ethernet:FlClash",
                "name": "FlClash",
                "description": "Meta Tunnel",
                "ipv4_bindings": [
                    {"address": "198.18.0.1", "netmask": "255.254.0.0"}
                ],
            },
        ]

    def test_network_path_gate_accepts_direct_route_and_rejects_source_or_proxy_mismatch(self) -> None:
        validate = getattr(acquisition, "validate_network_path", None)
        self.assertTrue(callable(validate), "network path validator is required")

        direct = validate(
            self.interface,
            adapter_snapshot=self.adapters,
            route_source="192.168.125.209",
        )
        wrong_source = validate(
            self.interface,
            adapter_snapshot=self.adapters,
            route_source="192.168.15.162",
        )
        proxy = validate(
            {**self.interface, "physical_interface_id": "ethernet:FlClash"},
            adapter_snapshot=self.adapters,
            route_source="198.18.0.1",
        )

        self.assertTrue(direct["ok"])
        self.assertEqual(direct["status"], "network_path_ready")
        self.assertFalse(wrong_source["ok"])
        self.assertEqual(wrong_source["status"], "network_path_invalid")
        self.assertEqual(wrong_source["actual_source_address"], "192.168.15.162")
        self.assertFalse(proxy["ok"])
        self.assertEqual(proxy["status"], "network_path_invalid")
        self.assertEqual(proxy["interface_type"], "virtual")

    def test_immediate_check_blocks_network_driver_on_invalid_path(self) -> None:
        config = acquisition.AcquisitionConfig(
            diagnostic_validation=True,
            acquisition_mode="real",
            real_acquisition_mode="local_direct",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["温度", "压力", "张力"],
            interfaces=[self.interface],
            interface_channel_assignments={
                "plc_process": ["温度", "压力", "张力"]
            },
        )
        invalid = {
            "ok": False,
            "status": "network_path_invalid",
            "target_address": "192.168.125.5",
            "selected_interface_id": "ethernet:以太网",
            "actual_interface_id": "ethernet:WLAN",
            "actual_source_address": "192.168.15.162",
            "interface_type": "physical",
            "target_subnet_match": False,
            "best_route": False,
        }

        with patch("acquisition.validate_network_path", return_value=invalid, create=True), \
             patch("acquisition.MultiInterfaceDriver") as driver:
            result = acquisition.AcquisitionManager().test_connection(
                config, timeout_seconds=0.1
            )

        driver.assert_not_called()
        self.assertEqual(result["interfaces"][0]["state"], "network_path_invalid")
        self.assertEqual(result["interfaces"][0]["network_path"], invalid)

    def test_connection_probe_window_starts_after_slow_network_open(self) -> None:
        now = [0.0]

        class Driver:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def open(self) -> None:
                now[0] = 2.0

            def read_sample(self):
                return {"温度": 20.0, "压力": 1.0, "张力": 2.0}

            def close(self) -> None:
                pass

        config = acquisition.AcquisitionConfig(
            diagnostic_validation=True,
            acquisition_mode="real",
            real_acquisition_mode="local_direct",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["温度", "压力", "张力"],
            interfaces=[self.interface],
            interface_channel_assignments={
                "plc_process": ["温度", "压力", "张力"]
            },
        )
        valid = {
            "ok": True,
            "status": "network_path_ready",
            "target_address": "192.168.125.5",
            "selected_interface_id": "ethernet:以太网",
            "actual_interface_id": "ethernet:以太网",
            "actual_source_address": "192.168.125.209",
            "interface_type": "physical",
            "target_subnet_match": True,
            "best_route": True,
        }

        with patch("acquisition.validate_network_path", return_value=valid), patch(
            "acquisition.MultiInterfaceDriver", Driver
        ), patch("acquisition.time.time", side_effect=lambda: now[0]):
            result = acquisition.AcquisitionManager().test_connection(config)

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["interfaces"][0]["detected_channels"],
            ["压力", "张力", "温度"],
        )

    def test_network_gate_runs_before_initial_open_and_each_reconnect(self) -> None:
        signature = inspect.signature(acquisition.LocalDirectAdapter)
        self.assertIn("before_open", signature.parameters)
        gate_calls = []

        class Driver:
            def __init__(self) -> None:
                self.open_calls = 0

            def open(self) -> None:
                self.open_calls += 1

            def read_sample(self) -> dict:
                raise OSError("network disconnected")

            def close(self) -> None:
                return None

        def gate() -> None:
            gate_calls.append(len(gate_calls) + 1)
            if len(gate_calls) > 1:
                raise RuntimeError("network_path_invalid")

        driver = Driver()
        adapter = acquisition.LocalDirectAdapter(
            "plc_process",
            driver,
            ["温度"],
            acquisition.ChannelSampleCache(),
            before_open=gate,
            reconnect_backoff_seconds=0.001,
            max_reconnect_backoff_seconds=0.001,
            max_reconnect_attempts=1,
        )
        adapter.start()
        deadline = time.monotonic() + 0.5
        while adapter.status()["running"] and time.monotonic() < deadline:
            time.sleep(0.005)
        adapter.stop(0.1)

        self.assertEqual(gate_calls, [1, 2])
        self.assertEqual(driver.open_calls, 1)
        self.assertEqual(adapter.status()["last_error"], "network_path_invalid")

    def test_formal_start_revalidates_network_path_before_starting_driver(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            real_acquisition_mode="local_direct",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["温度", "压力", "张力"],
            interfaces=[self.interface],
            interface_channel_assignments={
                "plc_process": ["温度", "压力", "张力"]
            },
        )
        manager = acquisition.AcquisitionManager()

        with patch(
            "acquisition.require_network_path",
            side_effect=RuntimeError("network_path_invalid: source changed"),
        ) as gate, patch.object(manager, "_start_new") as start_new:
            with self.assertRaisesRegex(RuntimeError, "network_path_invalid"):
                manager.start(config)

        gate.assert_called_once_with(config.interfaces[0])
        start_new.assert_not_called()

    def test_passed_session_reuses_matching_network_path_until_driver_open_gate(self) -> None:
        checked = {
            **self.interface,
            "ok": True,
            "state": "data_ready",
            "network_path": {
                "ok": True,
                "status": "network_path_ready",
                "target_address": "192.168.125.5",
                "target_port": 502,
                "selected_interface_id": "ethernet:以太网",
                "actual_interface_id": "ethernet:以太网",
                "actual_source_address": "192.168.125.209",
            },
        }
        reused = acquisition.reusable_network_path(self.interface, [checked])
        self.assertEqual(reused, checked["network_path"])
        changed = {**self.interface, "endpoint": "192.168.125.6:502"}
        self.assertIsNone(acquisition.reusable_network_path(changed, [checked]))
        changed = {**self.interface, "physical_interface_id": "ethernet:other"}
        self.assertIsNone(acquisition.reusable_network_path(changed, [checked]))
        changed = {**self.interface, "source_address": "192.168.125.210"}
        self.assertIsNone(acquisition.reusable_network_path(changed, [checked]))
        matching = {**self.interface, "source_address": "192.168.125.209"}
        self.assertEqual(
            acquisition.reusable_network_path(matching, [checked]),
            checked["network_path"],
        )

    def test_formal_start_does_not_repeat_passed_session_network_probe(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            real_acquisition_mode="local_direct",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["温度", "压力", "张力"],
            interfaces=[dict(self.interface)],
            interface_channel_assignments={
                "plc_process": ["温度", "压力", "张力"]
            },
        )
        checked = {
            **self.interface,
            "ok": True,
            "state": "data_ready",
            "network_path": {
                "ok": True,
                "status": "network_path_ready",
                "target_address": "192.168.125.5",
                "target_port": 502,
                "selected_interface_id": "ethernet:以太网",
                "actual_interface_id": "ethernet:以太网",
                "actual_source_address": "192.168.125.209",
            },
        }
        result = {
            "ok": True,
            "interfaces": [checked],
            "sensors": [
                {"name": name, "ok": True, "state": "ok"}
                for name in config.selected_sensors
            ],
        }
        manager = acquisition.AcquisitionManager()
        manager._readiness_snapshot = ReadinessSnapshot.create(config, result)
        manager._session_readiness_fingerprint = readiness_config_fingerprint(config)
        manager._latest_check_result = result
        with patch(
            "acquisition.require_network_path",
            side_effect=AssertionError("passed session repeated the network probe"),
        ), patch.object(manager, "_start_new", return_value={"ok": True}) as start_new:
            manager.start(config)
        start_new.assert_called_once()
        self.assertEqual(config.interfaces[0]["network_path"], checked["network_path"])
        preflight = manager.status()["start_preflight"]
        self.assertTrue(preflight["readiness_session_reused"])
        reuse = next(
            item for item in preflight["events"]
            if item["stage"] == "network_path_reused"
        )
        self.assertEqual(reuse["interface_id"], "plc_process")
        self.assertEqual(reuse["source_address"], "192.168.125.209")

    def test_formal_start_probes_when_snapshot_is_not_current_session(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            real_acquisition_mode="local_direct",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["温度", "压力", "张力"],
            interfaces=[dict(self.interface)],
            interface_channel_assignments={
                "plc_process": ["温度", "压力", "张力"]
            },
        )
        checked = {
            **self.interface,
            "ok": True,
            "state": "data_ready",
            "network_path": {
                "ok": True,
                "status": "network_path_ready",
                "target_address": "192.168.125.5",
                "target_port": 502,
                "selected_interface_id": "ethernet:以太网",
                "actual_interface_id": "ethernet:以太网",
                "actual_source_address": "192.168.125.209",
            },
        }
        result = {
            "ok": True,
            "interfaces": [checked],
            "sensors": [
                {"name": name, "ok": True, "state": "ok"}
                for name in config.selected_sensors
            ],
        }
        manager = acquisition.AcquisitionManager()
        manager._readiness_snapshot = ReadinessSnapshot.create(config, result)
        manager._session_readiness_fingerprint = ""
        probed = dict(checked["network_path"])
        with patch(
            "acquisition.require_network_path", return_value=probed
        ) as gate, patch.object(
            manager, "_start_new", return_value={"ok": True}
        ) as start_new:
            manager.start(config)
        gate.assert_called_once_with(config.interfaces[0])
        start_new.assert_called_once()
        preflight = manager.status()["start_preflight"]
        self.assertFalse(preflight["readiness_session_reused"])
        stages = [item["stage"] for item in preflight["events"]]
        self.assertIn("network_path_probe_begin", stages)
        self.assertIn("network_path_probe_end", stages)


class DeviceProfileContractTests(unittest.TestCase):
    def test_default_profiles_separate_confirmed_protocol_facts_from_pending_uvc_vendor_facts(self) -> None:
        by_role = {
            item["role"]: item
            for item in acquisition.default_capture_interfaces()
        }

        self.assertEqual(
            by_role["plc"].get("device_profile"),
            {
                "id": "panasonic-plc-process-v1",
                "version": 1,
                "approval_status": "approved",
                "unit_id": 255,
                "register_map": {"温度": 28, "压力": 37, "张力": 23},
                "byte_order": "little_endian_bytes",
                "word_order": "low_word_first",
                "freshness_seconds": 0.5,
            },
        )
        self.assertEqual(
            by_role["robot"].get("device_profile"),
            {
                "id": "abb-rws-rob1-base-v1",
                "version": 1,
                "approval_status": "approved",
                "mechanical_unit": "ROB_1",
                "coordinate_system": "Base",
                "resource": "/rw/motionsystem/mechunits/ROB_1/robtarget?coordinate=Base&json=1",
                "authentication": "digest_challenge",
                "freshness_seconds": 0.5,
            },
        )
        self.assertEqual(
            by_role["thermal_uvc"].get("device_profile"),
            {
                "id": "bsv-uvc-2bdf-0102-v1",
                "version": 1,
                "approval_status": "vendor_confirmation_required",
                "observed_identity": {"vid": 0x2BDF, "pid": 0x0102},
                "required_vendor_facts": {
                    "windows_binding": None,
                    "interface_number": None,
                    "endpoints": None,
                    "temperature_range": None,
                    "frame_format": None,
                    "conversion": None,
                    "freshness_seconds": None,
                },
            },
        )


class LocatedDefectRegressionTests(unittest.TestCase):
    def test_abb_digest_challenge_is_not_silently_returned_as_no_data(self) -> None:
        from visualization_app.test_abb_digest_contract import DigestFixture

        with DigestFixture(password="different") as fixture:
            driver = acquisition.AbbRobotDriver(
                fixture.endpoint, username="operator", password="secret"
            )
            try:
                with self.assertRaisesRegex(Exception, "authentication_failed"):
                    driver.read_sample()
            finally:
                driver.close()

    def test_bsv_camera_identity_is_bound_to_usb_port_without_dll_presence_guess(self) -> None:
        topology = {
            "schema_version": 2,
            "state": "complete",
            "provider": "fixture",
            "errors": [],
            "docks": [],
            "hubs": [{"id": "hub:external", "label": "USB 扩展设备 1"}],
            "usb_ports": [
                {
                    "id": "usbport:external:1",
                    "owner_kind": "hub",
                    "dock_id": "",
                    "hub_id": "hub:external",
                    "label": "USB 扩展设备 1 · USB2-1",
                    "state": "occupied",
                    "connector_type": "usb2",
                    "connector_form_factor": "type_a",
                    "user_connectable": True,
                    "device_id": "usbdev:bsv",
                },
                {
                    "id": "usbport:external:2",
                    "owner_kind": "hub",
                    "dock_id": "",
                    "hub_id": "hub:external",
                    "label": "USB 扩展设备 1 · USB2-2",
                    "state": "occupied",
                    "connector_type": "usb2",
                    "connector_form_factor": "type_a",
                    "user_connectable": True,
                    "device_id": "usbdev:other-camera",
                },
            ],
            "devices": [
                {
                    "id": "usbdev:bsv",
                    "parent_port_id": "usbport:external:1",
                    "friendly_name": "UVC Camera",
                    "vid": 0x2BDF,
                    "pid": 0x0102,
                    "serial": "F10615613",
                    "instance_id": r"USB\VID_2BDF&PID_0102&MI_00\7&FIXTURE&0&0000",
                    "service": "usbvideo",
                    "driver_provider": "Microsoft",
                    "location_paths": [
                        "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)#USB(1)#USBMI(0)"
                    ],
                },
                {
                    "id": "usbdev:other-camera",
                    "parent_port_id": "usbport:external:2",
                    "friendly_name": "FHD Camera",
                    "vid": 0x3277,
                    "pid": 0x00B9,
                    "serial": "OTHER",
                    "instance_id": r"USB\VID_3277&PID_00B9&MI_00\OTHER",
                    "service": "usbvideo",
                    "driver_provider": "Microsoft",
                    "location_paths": [],
                },
            ],
        }

        with patch("acquisition.discover_windows_usb_topology", return_value=topology), \
             patch("acquisition._windows_camera_pnp_facts", return_value=[
                 {
                     "instance_id": r"USB\VID_2BDF&PID_0102&MI_00\7&FIXTURE&0&0000",
                     "friendly_name": "UVC Camera",
                     "service": "usbvideo",
                     "driver_provider": "Microsoft",
                     "driver_version": "10.0.26100.1",
                     "pnp_status": "OK",
                     "location_paths": [
                         "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)#USB(1)#USBMI(0)"
                     ],
                     "container_id": "{BSV}",
                     "vid": 0x2BDF,
                     "pid": 0x0102,
                 },
                 {
                     "instance_id": r"USB\VID_3277&PID_00B9&MI_00\OTHER",
                     "friendly_name": "FHD Camera",
                     "service": "usbvideo",
                     "driver_provider": "Microsoft",
                     "driver_version": "10.0.26100.1",
                     "pnp_status": "OK",
                     "location_paths": [],
                     "container_id": "{OTHER}",
                     "vid": 0x3277,
                     "pid": 0x00B9,
                 },
             ]), \
             patch("acquisition.enumerate_smrf_hid_devices", return_value=[]), \
             patch("acquisition.socket.create_connection", side_effect=OSError("offline")), \
             patch("acquisition.Path.is_file", return_value=True):
            result = acquisition.AcquisitionManager.discover_interfaces()

        uvc = next(
            item for item in result["physical_interfaces"]
            if item.get("kind") == "usb_uvc"
        )
        self.assertTrue(uvc.get("driver_available"))
        self.assertTrue(uvc.get("device_detected"))
        self.assertTrue(uvc.get("topology_bound"))
        self.assertTrue(uvc.get("detected"))
        self.assertEqual(uvc.get("vid"), 0x2BDF)
        self.assertEqual(uvc.get("pid"), 0x0102)
        self.assertEqual(uvc.get("parent_port_id"), "usbport:external:1")
        self.assertEqual(uvc.get("service"), "usbvideo")
        self.assertEqual(uvc.get("driver_provider"), "Microsoft")
        self.assertEqual(uvc.get("driver_version"), "10.0.26100.1")
        self.assertFalse(
            next(
                item for item in result["camera_devices"]
                if item["friendly_name"] == "FHD Camera"
            )["compatible_with_bsv_temperature"]
        )
        self.assertNotIn(
            uvc["id"],
            result["interface_transport_catalog"]["unlocated_usb_interface_ids"],
        )
        self.assertEqual(
            [
                item.get("parent_port_id")
                for item in result["physical_interfaces"]
                if item.get("kind") == "usb_uvc" and item.get("device_detected")
            ],
            ["usbport:external:1"],
        )

    def test_formal_uvc_check_fails_closed_before_open_when_vendor_facts_are_pending(self) -> None:
        config = acquisition.AcquisitionConfig(
            diagnostic_validation=True,
            acquisition_mode="real",
            real_acquisition_mode="local_direct",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["ROI平均温度"],
            interfaces=[
                {
                    "id": "uvc_temperature",
                    "enabled": True,
                    "role": "thermal_uvc",
                    "driver": "uvc_thermal",
                    "endpoint": "BSV UVC (WinUSB)",
                    "physical_interface_id": "uvc:2bdf:0102",
                    "physical_port_id": "usbport:external:1",
                    "physical_interface_kind": "usb_uvc",
                    "physical_verified": True,
                }
            ],
            interface_channel_assignments={"uvc_temperature": ["ROI平均温度"]},
        )

        with patch("acquisition.MultiInterfaceDriver") as driver:
            result = acquisition.AcquisitionManager().test_connection(
                config, timeout_seconds=0.1
            )

        driver.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["interfaces"][0]["state"], "device_profile_incomplete")
        self.assertEqual(
            result["interfaces"][0]["missing_device_facts"],
            [
                "conversion",
                "endpoints",
                "frame_format",
                "freshness_seconds",
                "interface_number",
                "temperature_range",
                "windows_binding",
            ],
        )


if __name__ == "__main__":
    unittest.main()
