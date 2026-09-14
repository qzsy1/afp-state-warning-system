from __future__ import annotations

import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from acquisition import AcquisitionManager


class PublicDeviceStatusTests(unittest.TestCase):
    def _module(self):
        spec = importlib.util.find_spec("public_status")
        self.assertIsNotNone(
            spec, "public_status must provide the anonymous hardware projection"
        )
        return importlib.import_module("public_status")

    def test_public_status_exposes_state_but_masks_physical_identifiers(self):
        module = self._module()
        discovery = {
            "physical_interfaces": [
                {
                    "id": "serial:COM9",
                    "endpoint": "COM9",
                    "serial_number": "USB-SECRET-9988",
                }
            ]
        }
        check = {
            "checked_at": 100.0,
            "interfaces": [
                {
                    "id": "m3232_pressure",
                    "role": "pressure",
                    "driver": "m3232_pressure",
                    "endpoint": "COM9",
                    "physical_interface_id": "serial:COM9",
                    "state": "not_connected",
                    "message": "访问 COM9 被拒绝；USB-SECRET-9988",
                    "ok": False,
                }
            ],
        }

        payload = module.build_public_device_status(
            discovery, check, {"running": False}, now=101.0
        )
        encoded = json.dumps(payload, ensure_ascii=False)

        self.assertIn("M3232", encoded)
        self.assertIn("not_connected", encoded)
        self.assertNotIn("COM9", encoded)
        self.assertNotIn("USB-SECRET-9988", encoded)
        pressure = next(
            item for item in payload["interfaces"] if item["role"] == "pressure"
        )
        self.assertEqual(pressure["message"], "接口无法打开或设备未连接")
        self.assertEqual(pressure["age_seconds"], 1.0)

    def test_public_status_always_lists_the_five_declared_interfaces(self):
        module = self._module()

        payload = module.build_public_device_status({}, {}, {"running": False}, now=1.0)

        self.assertEqual(
            [item["role"] for item in payload["interfaces"]],
            ["thermocouple", "plc", "thermal_uvc", "robot", "pressure"],
        )
        self.assertTrue(all(item["state"] == "unchecked" for item in payload["interfaces"]))

    def test_public_status_reads_cached_result_without_reopening_hardware(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = AcquisitionManager(capture_root=Path(folder))
            manager._latest_check_result = {
                "checked_at": 10.0,
                "interfaces": [],
                "sensors": [],
            }
            with patch.object(
                manager,
                "test_connection",
                side_effect=AssertionError("public status must not probe hardware"),
            ):
                result = manager.latest_check_result()

        self.assertEqual(result["checked_at"], 10.0)

    def test_latest_check_result_returns_an_independent_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = AcquisitionManager(capture_root=Path(folder))
            manager._latest_check_result = {
                "checked_at": 10.0,
                "interfaces": [{"role": "pressure", "state": "ok"}],
                "sensors": [],
            }

            returned = manager.latest_check_result()
            returned["interfaces"][0]["state"] = "tampered"

            self.assertEqual(
                manager.latest_check_result()["interfaces"][0]["state"], "ok"
            )


if __name__ == "__main__":
    unittest.main()
