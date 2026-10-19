from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

import acquisition  # noqa: E402
from local_capture_agent import LocalCaptureAgent  # noqa: E402


class UnifiedDeviceReadinessTests(unittest.TestCase):
    def test_formal_start_rejects_unapproved_uvc_profile_before_driver_construction(self) -> None:
        config = self._config()
        config.interfaces = [config.interfaces[2]]
        config.interface_channel_assignments = {"uvc_temperature": ["ROI平均温度"]}
        config.selected_sensors = ["ROI平均温度"]
        manager = acquisition.AcquisitionManager(recovery_scan=False)
        with patch.object(manager, "_start_new") as start_new:
            with self.assertRaisesRegex(RuntimeError, "device_profile_incomplete"):
                manager.start(config)
        start_new.assert_not_called()

    def test_local_start_requires_current_successful_check(self) -> None:
        config = self._config()
        config.interfaces = [config.interfaces[0]]
        config.interface_channel_assignments = {"plc_process": ["温度", "压力", "张力"]}
        config.selected_sensors = ["温度", "压力", "张力"]

        class Driver:
            def __init__(self, *_args, **_kwargs):
                pass

            def open(self):
                pass

            def read_sample(self):
                return {"温度": 100.0, "压力": 35.0, "张力": 2.0}

            def close(self):
                pass

        manager = acquisition.AcquisitionManager(recovery_scan=False)
        route = {"ok": True, "status": "network_path_ready"}
        with patch("acquisition.require_network_path", return_value=route), patch(
            "acquisition.validate_network_path", return_value=route
        ), patch("acquisition.MultiInterfaceDriver", Driver), patch.object(
            manager, "_start_new", return_value={"running": True}
        ) as start_new:
            with self.assertRaisesRegex(RuntimeError, "readiness_required"):
                manager.start(config)
            checked = manager.test_connection(config)
            self.assertTrue(checked["ok"], checked)
            self.assertTrue(manager.start(config)["running"])
            start_new.reset_mock()
            config.interfaces[0]["endpoint"] = "192.168.125.6:502"
            with self.assertRaisesRegex(RuntimeError, "fingerprint"):
                manager.start(config)
            start_new.assert_not_called()

    def test_public_config_and_manifest_never_contain_abb_credentials(self) -> None:
        config = self._config()
        config.interfaces[1].update(username="private-user", password="private-password")

        public = acquisition.AcquisitionManager._public_config(config)

        self.assertNotIn("private-user", repr(public))
        self.assertNotIn("private-password", repr(public))
        self.assertRegex(public["interfaces"][1]["credential_identifier"], r"^[0-9a-f]{64}$")

    def test_helper_discovery_and_check_declare_the_same_readiness_contract(self) -> None:
        check_result = {
            "ok": True,
            "interfaces": [{"id": "plc_process", "state": "data_ready"}],
            "sensors": [],
        }

        class Manager:
            def discover_interfaces(self):
                return {
                    "physical_interfaces": [],
                    "usb_topology": None,
                    "error": "",
                }

            def test_connection(self, _config):
                return check_result

        agent = LocalCaptureAgent(manager=Manager())
        discovery = agent.discover()
        checked = agent.check_capture(self._config())

        self.assertEqual(
            discovery["device_readiness_contract"],
            "plc_abb_uvc_readiness_v1",
        )
        self.assertTrue(discovery["capabilities"]["unified_device_readiness_v1"])
        self.assertEqual(
            checked["device_readiness_contract"],
            discovery["device_readiness_contract"],
        )
        self.assertEqual(checked["interfaces"], check_result["interfaces"])
        self.assertEqual(
            {key: checked[key] for key in check_result},
            check_result,
        )
        self.assertIn("readiness_snapshot", checked)

    def _config(self) -> acquisition.AcquisitionConfig:
        physical = {
            "physical_interface_id": "ethernet:以太网",
            "physical_interface_kind": "ethernet",
            "physical_verified": True,
        }
        return acquisition.AcquisitionConfig(
            diagnostic_validation=True,
            acquisition_mode="real",
            real_acquisition_mode="local_direct",
            dataset_schema="new_collection_v11_3",
            selected_sensors=[
                "温度", "压力", "张力",
                "ABB_X", "ABB_Y", "ABB_Z", "线速度",
                "ROI平均温度",
            ],
            interfaces=[
                {
                    "id": "plc_process", "role": "plc",
                    "driver": "modbus_tcp", "endpoint": "192.168.125.5:502",
                    **physical,
                },
                {
                    "id": "abb_motion", "role": "robot",
                    "driver": "abb_robot", "endpoint": "192.168.125.1",
                    **physical,
                },
                {
                    "id": "uvc_temperature", "role": "thermal_uvc",
                    "driver": "uvc_thermal", "endpoint": "bsv-uvc-2bdf-0102-v1",
                    "physical_interface_id": "uvc:bsv:fixture",
                    "physical_port_id": "usbport:fixture:1",
                    "physical_interface_kind": "usb_uvc",
                    "physical_transport_family": "usb",
                    "physical_verified": True,
                },
            ],
            interface_channel_assignments={
                "plc_process": ["温度", "压力", "张力"],
                "abb_motion": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
                "uvc_temperature": ["ROI平均温度"],
            },
        )

    def test_mixed_results_keep_all_interfaces_and_stable_final_codes(self) -> None:
        samples = {
            "plc": {"温度": 100.0, "压力": 35.0, "张力": 2.0},
        }

        class Driver:
            def __init__(self, configs, *_args, **_kwargs) -> None:
                self.role = configs[0]["role"]

            def open(self) -> None:
                if self.role == "robot":
                    raise acquisition.AbbRwsError(
                        "authentication_failed", "authentication"
                    )
                if self.role == "thermal_uvc":
                    raise acquisition.UvcDriverError(
                        "driver_incompatible", "load"
                    )

            def read_sample(self):
                return samples.get(self.role)

            def close(self) -> None:
                pass

        route = {
            "ok": True, "status": "network_path_ready",
            "target_address": "192.168.125.5",
            "selected_interface_id": "ethernet:以太网",
            "actual_interface_id": "ethernet:以太网",
            "actual_source_address": "192.168.125.209",
            "interface_type": "physical", "target_subnet_match": True,
            "best_route": True,
        }
        profile_ready = {
            "ready": True,
            "status": "device_profile_ready",
            "profile_id": "fixture",
            "missing_device_facts": [],
        }
        with patch("acquisition.validate_network_path", return_value=route), patch(
            "acquisition.device_profile_readiness", return_value=profile_ready
        ), patch("acquisition.MultiInterfaceDriver", Driver):
            result = acquisition.AcquisitionManager().test_connection(
                self._config(), timeout_seconds=0.1
            )

        self.assertEqual(
            {item["id"]: item["state"] for item in result["interfaces"]},
            {
                "plc_process": "data_ready",
                "abb_motion": "authentication_failed",
                "uvc_temperature": "driver_incompatible",
            },
        )
        self.assertEqual(len(result["interfaces"]), 3)

    def test_every_result_has_explicit_readiness_stage_schema(self) -> None:
        result = {
            "id": "abb_motion",
            "role": "robot",
            "state": "authentication_failed",
            "physical_interface_id": "ethernet:以太网",
            "physical_interface_kind": "ethernet",
            "physical_verified": True,
            "network_path": {"ok": True, "status": "network_path_ready"},
            "errors": ["authentication_failed: ABB RWS authentication"],
            "expected_channels": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
            "detected_channels": [],
            "missing_channels": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
        }

        normalized = acquisition.normalize_interface_readiness(result)

        self.assertEqual(normalized["state"], "authentication_failed")
        self.assertEqual(
            set(normalized["stages"]),
            {
                "physical_binding", "device_identity", "driver",
                "network_path", "usb_binding", "authentication",
                "protocol", "channels", "freshness", "stability",
            },
        )
        self.assertEqual(normalized["stages"]["authentication"]["state"], "failed")
        self.assertEqual(normalized["stages"]["usb_binding"]["state"], "not_applicable")
        self.assertEqual(normalized["stages"]["protocol"]["state"], "not_reached")
        self.assertTrue(all(stage["state"] for stage in normalized["stages"].values()))


if __name__ == "__main__":
    unittest.main()
