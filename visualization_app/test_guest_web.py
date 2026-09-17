from __future__ import annotations

import importlib
import importlib.util
import io
import json
import base64
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
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


class GuestSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.csv"
        self.source.write_text("温度,压力\n350,400\n", encoding="utf-8")
        self.profile = {
            "source_type": "single_csv",
            "path": str(self.source),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _module(self):
        spec = importlib.util.find_spec("guest_simulation")
        self.assertIsNotNone(
            spec, "guest_simulation must isolate anonymous acquisition sessions"
        )
        return importlib.import_module("guest_simulation")

    def _manager(self, **overrides):
        module = self._module()
        options = {
            "root": self.root / "public_simulation",
            "source_profiles": {"builtin": self.profile},
            "per_session_bytes": 256 * 1024 * 1024,
            "total_bytes": 2 * 1024 * 1024 * 1024,
            "max_running": 4,
        }
        options.update(overrides)
        return module.GuestSimulationManager(**options)

    def test_two_guest_sessions_use_distinct_managers_and_save_roots(self):
        manager = self._manager()
        first_id = "a" * 32
        second_id = "b" * 32

        first = manager.ensure_session(first_id)
        second = manager.ensure_session(second_id)

        self.assertIsNot(first.acquisition, second.acquisition)
        self.assertEqual(first.save_root, manager.root / first_id)
        self.assertEqual(second.save_root, manager.root / second_id)

    def test_guest_payload_cannot_select_real_driver_or_arbitrary_path(self):
        manager = self._manager()
        session_id = "a" * 32

        config = manager.safe_config(
            session_id,
            {
                "acquisition_mode": "real",
                "driver": "m3232_pressure",
                "save_root": "C:\\Windows",
                "simulation_source_type": "mysql",
                "simulation_mysql_password": "attacker-password",
                "mysql_enabled": True,
                "source_profile": "builtin",
                "selected_sensors": ["温度", "压力"],
            },
        )

        self.assertEqual(config.acquisition_mode, "simulation")
        self.assertEqual(config.driver, "simulator")
        self.assertEqual(config.save_root, str(manager.root / session_id))
        self.assertEqual(config.simulation_source_type, "single_csv")
        self.assertEqual(config.simulation_mysql_password, "")
        self.assertFalse(config.mysql_enabled)

    def test_public_simulation_status_never_exposes_server_absolute_paths(self):
        """A remote start/status/stop response must contain only public scopes."""
        manager = self._manager()
        session_id = "a" * 32
        session = manager.ensure_session(session_id)
        payloads = []

        try:
            payloads.append(
                manager.start(
                    session_id,
                    {
                        "source_profile": "builtin",
                        "processing_mode": "capture_only",
                        "selected_sensors": ["温度", "压力"],
                    },
                )
            )
            payloads.append(manager.status(session_id))
        finally:
            payloads.append(manager.stop(session_id))

        def all_strings(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from all_strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from all_strings(item)
            elif isinstance(value, str):
                yield value

        exposed_strings = list(all_strings(payloads))
        self.assertFalse(
            any(str(session.save_root) in value for value in exposed_strings),
            exposed_strings,
        )
        self.assertFalse(
            any(str(self.source) in value for value in exposed_strings),
            exposed_strings,
        )
        self.assertEqual(
            payloads[0]["server_save_scope"],
            f"public_simulation/{session_id}",
        )
        self.assertEqual(
            payloads[0]["config"]["save_root"],
            f"public_simulation/{session_id}",
        )

    def test_uploaded_csv_becomes_the_source_for_only_that_guest_session(self):
        manager = self._manager()
        first_id = "a" * 32
        second_id = "b" * 32
        encoded = base64.b64encode("温度,压力\n351,401\n".encode("utf-8")).decode("ascii")

        result = manager.upload_source(
            first_id,
            "single_csv",
            [{"name": "client.csv", "data": encoded}],
        )

        self.assertTrue(result["selected"])
        first = manager.safe_config(first_id, {})
        second = manager.safe_config(second_id, {})
        self.assertEqual(Path(first.simulation_source_path).name, "client.csv")
        self.assertEqual(Path(second.simulation_source_path), self.source)

    def test_uploaded_source_uses_an_opaque_session_bound_identifier(self):
        module = self._module()
        manager = self._manager()
        first_id = "a" * 32
        second_id = "b" * 32
        encoded = base64.b64encode("温度,压力\n351,401\n".encode("utf-8")).decode("ascii")

        result = manager.upload_source(
            first_id,
            "single_csv",
            [{"name": "client.csv", "data": encoded}],
        )

        self.assertRegex(result["source_id"], r"^[A-Za-z0-9_-]{20,64}$")
        resolved = manager.resolve_uploaded_source(first_id, result["source_id"])
        self.assertEqual(Path(resolved["path"]).name, "client.csv")
        with self.assertRaises(module.GuestSimulationError) as raised:
            manager.resolve_uploaded_source(second_id, result["source_id"])
        self.assertEqual(raised.exception.code, "simulation_source_not_found")

    def test_default_simulation_dataset_can_be_loaded_once_without_leaking_path(self):
        manager = self._manager()

        payload = manager.dataset("a" * 32)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total_rows"], 1)
        self.assertEqual(payload["columns"], ["温度", "压力"])
        self.assertEqual(payload["rows"], [{"温度": 350, "压力": 400}])
        self.assertEqual(payload["name"], "source.csv")
        self.assertNotIn("path", payload)

    def test_uploaded_simulation_dataset_replaces_default_for_current_guest(self):
        manager = self._manager()
        encoded = base64.b64encode("温度,压力\n351,401\n352,402\n".encode("utf-8")).decode("ascii")
        manager.upload_source(
            "a" * 32,
            "single_csv",
            [{"name": "client.csv", "data": encoded}],
        )

        payload = manager.dataset("a" * 32)

        self.assertEqual(payload["total_rows"], 2)
        self.assertEqual(payload["name"], "client.csv")
        self.assertEqual(payload["rows"][1]["温度"], 352)

    def test_guest_process_parameter_read_uses_current_uploaded_source(self):
        manager = self._manager()
        encoded = base64.b64encode(
            (
                "温度,压力,initial_compaction_force_N,placement_speed_mm_s,pid_angle_deg,temperature_setpoint_C\n"
                "351,401,430,95,8,370\n"
            ).encode("utf-8")
        ).decode("ascii")
        manager.upload_source(
            "a" * 32,
            "single_csv",
            [{"name": "process.csv", "data": encoded}],
        )

        result = manager.read_process_parameters("a" * 32, {
            "dataset_schema": "new_collection_v11_3",
            "selected_sensors": ["温度", "压力"],
        })

        self.assertTrue(result["complete"])
        self.assertEqual(result["values"]["initial_compaction_force_N"], 430.0)
        self.assertEqual(result["values"]["temperature_setpoint_C"], 370.0)

    def test_uploaded_folder_requires_csv_files(self):
        manager = self._manager()
        encoded = base64.b64encode(b"not csv").decode("ascii")
        with self.assertRaises(ValueError):
            manager.upload_source(
                "a" * 32,
                "folder_csv",
                [{"name": "notes.txt", "data": encoded}],
            )

    def test_invalid_guest_session_id_is_rejected_before_path_creation(self):
        module = self._module()
        manager = self._manager()

        with self.assertRaises(module.GuestSimulationError) as raised:
            manager.ensure_session("../outside")

        self.assertEqual(raised.exception.code, "invalid_guest_session")
        self.assertFalse((self.root / "outside").exists())

    def test_per_session_quota_is_checked_before_start(self):
        module = self._module()
        manager = self._manager(per_session_bytes=8)
        session_id = "a" * 32
        session = manager.ensure_session(session_id)
        (session.save_root / "existing.bin").write_bytes(b"123456789")

        with self.assertRaises(module.GuestSimulationError) as raised:
            manager.start(session_id, {"source_profile": "builtin"})

        self.assertEqual(raised.exception.code, "guest_quota_exceeded")
        self.assertFalse(session.acquisition.status()["running"])

    def test_download_contains_only_current_guest_directory(self):
        manager = self._manager()
        first_id = "a" * 32
        second_id = "b" * 32
        first = manager.ensure_session(first_id)
        second = manager.ensure_session(second_id)
        (first.save_root / "result.csv").write_bytes(b"x\n1\n")
        (second.save_root / "secret.csv").write_bytes(b"secret")

        raw, name = manager.download_archive(first_id)

        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(archive.namelist(), ["result.csv"])
            self.assertEqual(archive.read("result.csv"), b"x\n1\n")
        self.assertNotIn(b"secret", raw)
        self.assertEqual(name, f"afp_simulation_{first_id}.zip")

    def test_expired_stopped_session_cleanup_does_not_touch_other_sessions(self):
        manager = self._manager()
        stale = manager.ensure_session("a" * 32)
        fresh = manager.ensure_session("b" * 32)
        stale.last_access_at = 10.0
        fresh.last_access_at = 95.0
        stale_file = stale.save_root / "stale.csv"
        fresh_file = fresh.save_root / "fresh.csv"
        stale_file.write_text("x\n1\n", encoding="utf-8")
        fresh_file.write_text("x\n2\n", encoding="utf-8")

        result = manager.cleanup_expired(max_idle_seconds=20.0, now=100.0)

        self.assertEqual(result["removed_sessions"], 1)
        self.assertFalse(stale.save_root.exists())
        self.assertTrue(fresh_file.exists())

    def test_dashboard_live_uses_the_guest_acquisition_argument(self):
        from app import DashboardData

        def source(marker):
            return SimpleNamespace(
                status=lambda: {
                    "marker": marker,
                    "config": {
                        "processing_mode": "capture_only",
                        "dataset_schema": "legacy_original",
                        "selected_sensors": ["温度"],
                    },
                },
                numeric_matrix=lambda: ([{"温度": 350.0}], [0.0]),
            )

        dashboard = DashboardData.__new__(DashboardData)
        dashboard.acquisition = source("real")
        dashboard._capture_only_live = lambda **kwargs: {
            "source_marker": kwargs["status"]["marker"]
        }

        payload = dashboard.live(
            sensor_id=0,
            history=48,
            step=1,
            threshold=0.5,
            rho=0.5,
            indicator="TC-HI",
            model_kind="random_forest",
            prediction_horizon=24,
            processing_mode="capture_only",
            acquisition=source("guest"),
        )

        self.assertEqual(payload["source_marker"], "guest")


if __name__ == "__main__":
    unittest.main()
