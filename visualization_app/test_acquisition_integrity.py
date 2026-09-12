from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acquisition import (  # noqa: E402
    AcquisitionConfig,
    AcquisitionManager,
    MySQLSettings,
    NEW_COLLECTION_SENSOR_COLUMNS,
    SimulatorDriver,
)
from training_data import read_excel_or_folder  # noqa: E402


class AcquisitionIntegrityTests(unittest.TestCase):
    def test_physical_binding_allows_plc_and_abb_to_share_one_ethernet_adapter(self) -> None:
        interfaces = [
            {
                "id": "thermocouple_8ch", "enabled": True, "role": "thermocouple",
                "driver": "smrf_hid", "endpoint": "SMRFCT08B",
                "physical_interface_id": "hid:smrf-01", "physical_interface_kind": "usb_hid",
            },
            {
                "id": "plc_process", "enabled": True, "role": "plc",
                "driver": "modbus_tcp", "endpoint": "192.168.125.5:502",
                "physical_interface_id": "ethernet:工控网卡1", "physical_interface_kind": "ethernet",
            },
            {
                "id": "uvc_temperature", "enabled": True, "role": "thermal_uvc",
                "driver": "uvc_thermal", "endpoint": "BSV UVC (WinUSB)",
                "physical_interface_id": "uvc:bsv-01", "physical_interface_kind": "usb_uvc",
            },
            {
                "id": "abb_motion", "enabled": True, "role": "robot",
                "driver": "abb_robot", "endpoint": "192.168.125.1",
                "physical_interface_id": "ethernet:工控网卡1", "physical_interface_kind": "ethernet",
            },
            {
                "id": "m3232_pressure", "enabled": True, "role": "pressure",
                "driver": "m3232_pressure", "endpoint": "COM8",
                "physical_interface_id": "serial:COM8", "physical_interface_kind": "serial",
            },
        ]
        config = AcquisitionConfig(
            acquisition_mode="real", dataset_schema="new_collection_v11_3",
            selected_sensors=["温度", "压力", "薄膜压力", "ROI平均温度", "张力", "线速度", "ABB_X", "ABB_Y", "ABB_Z", "温度1"],
            interfaces=interfaces,
            interface_channel_assignments={
                "thermocouple_8ch": ["温度1"],
                "plc_process": ["温度", "压力", "张力"],
                "uvc_temperature": ["ROI平均温度"],
                "abb_motion": ["线速度", "ABB_X", "ABB_Y", "ABB_Z"],
                "m3232_pressure": ["薄膜压力"],
            },
        )
        self.assertEqual(config.interfaces[1]["physical_interface_id"], config.interfaces[3]["physical_interface_id"])
        self.assertEqual(config.interfaces[1]["physical_interface_kind"], "ethernet")

    def test_physical_binding_rejects_duplicate_non_shared_interface(self) -> None:
        with self.assertRaisesRegex(ValueError, "物理接口.*重复"):
            AcquisitionConfig(
                acquisition_mode="real", dataset_schema="new_collection_v11_3",
                selected_sensors=["温度"],
                interfaces=[
                    {"id": "a", "enabled": True, "role": "custom", "driver": "serial_json", "endpoint": "COM8", "physical_interface_id": "serial:COM8", "physical_interface_kind": "serial"},
                    {"id": "b", "enabled": True, "role": "custom", "driver": "serial_json", "endpoint": "COM9", "physical_interface_id": "serial:COM8", "physical_interface_kind": "serial"},
                ],
                interface_channel_assignments={"a": ["温度"]},
            )

    def test_physical_binding_rejects_role_protocol_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "协议.*不匹配"):
            AcquisitionConfig(
                acquisition_mode="real", dataset_schema="new_collection_v11_3",
                selected_sensors=["薄膜压力"],
                interfaces=[
                    {"id": "m3232_pressure", "enabled": True, "role": "pressure", "driver": "modbus_tcp", "endpoint": "COM8", "physical_interface_id": "serial:COM8", "physical_interface_kind": "serial"},
                ],
                interface_channel_assignments={"m3232_pressure": ["薄膜压力"]},
            )

    def test_discovery_reports_physical_interface_metadata(self) -> None:
        hid = SimpleNamespace(
            label="SMRFCT08B (Serial=SMRF-01)", product="SMRFCT08B",
            serial="SMRF-01", vendor_id=0x1234, product_id=0x5678,
            path="hid-path-01",
        )
        with patch("acquisition.enumerate_smrf_hid_devices", return_value=[hid]), \
             patch("acquisition.socket.create_connection", side_effect=OSError("offline")):
            result = AcquisitionManager.discover_interfaces()
        kinds = {item["kind"] for item in result["physical_interfaces"]}
        self.assertIn("usb_hid", kinds)
        self.assertIn("ethernet", kinds)
        hid_items = [item for item in result["physical_interfaces"] if item["kind"] == "usb_hid"]
        self.assertEqual(hid_items[0]["protocol"], "smrf_hid")

    def test_discovery_keeps_usb_roles_assignable_when_devices_are_not_present(self) -> None:
        with patch("acquisition.enumerate_smrf_hid_devices", return_value=[]), \
             patch("acquisition.socket.create_connection", side_effect=OSError("offline")):
            result = AcquisitionManager.discover_interfaces()
        by_kind = {item["kind"]: item for item in result["physical_interfaces"]}
        self.assertIn("usb_hid", by_kind)
        self.assertIn("usb_uvc", by_kind)
        self.assertTrue(by_kind["usb_hid"]["auto_assignable"])
        self.assertTrue(by_kind["usb_uvc"]["auto_assignable"])

    def test_unchecked_channels_are_removed_from_interface_assignments(self) -> None:
        config = AcquisitionConfig(
            processing_mode="capture_only",
            acquisition_mode="simulation",
            dataset_schema="new_collection_v11_3",
            driver="simulator",
            simulation_source_path="simulation.csv",
            selected_sensors=["温度1", "压力"],
            interfaces=[
                {
                    "id": "interface_1",
                    "enabled": True,
                    "driver": "simulator",
                    "role": "custom",
                    "endpoint": "",
                }
            ],
            interface_channel_assignments={
                "interface_1": ["温度1", "压力", "张力"]
            },
        )
        self.assertEqual(config.selected_sensors, ["温度1", "压力"])
        self.assertEqual(
            config.interface_channel_assignments,
            {"interface_1": ["温度1", "压力"]},
        )

    def _source(self, root: Path, rows: int = 200) -> Path:
        path = root / "simulation.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=NEW_COLLECTION_SENSOR_COLUMNS
            )
            writer.writeheader()
            for index in range(rows):
                writer.writerow(
                    {
                        name: 20.0 + channel + index * 0.01
                        for channel, name in enumerate(
                            NEW_COLLECTION_SENSOR_COLUMNS
                        )
                    }
                )
        return path

    def _capture(
        self, root: Path, source: Path, layer: int, target_rows: int = 20
    ) -> dict:
        manager = AcquisitionManager(root / "unused")
        config = AcquisitionConfig(
            processing_mode="capture_only",
            dataset_schema="new_collection_v11_3",
            driver="simulator",
            source_file=str(source),
            simulation_source_path=str(source),
            selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
            sample_rate_hz=1000.0,
            save_root=str(root / "capture"),
            condition_id="H06",
            replicate=1,
            layer=layer,
        )
        manager.start(config)
        deadline = time.time() + 5.0
        while manager.status()["sample_count"] < target_rows and time.time() < deadline:
            time.sleep(0.01)
        return manager.stop()

    def test_layers_share_uuid_and_files_are_finalized_with_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            first = self._capture(root, source, 0)
            second = self._capture(root, source, 1)

            self.assertTrue(first["capture_uuid"])
            self.assertEqual(first["capture_uuid"], second["capture_uuid"])
            self.assertEqual(second["completed_layers"], [1, 2])
            self.assertTrue(Path(second["raw_file"]).is_file())
            self.assertFalse(
                Path(str(second["raw_file"]) + ".partial").exists()
            )
            summaries = sorted(
                Path(second["session_dir"]).joinpath("采集记录").glob(
                    "*_采集摘要.json"
                )
            )
            summary = json.loads(summaries[-1].read_text(encoding="utf-8"))
            self.assertEqual(summary["capture_uuid"], second["capture_uuid"])
            self.assertEqual(summary["sample_count"], second["sample_count"])
            self.assertEqual(
                len(summary["file_integrity"]["layer_file"]["sha256"]), 64
            )
            self.assertGreater(
                summary["data_quality"]["effective_sample_rate_hz"], 0
            )

    def test_saved_schema_contains_only_selected_sensor_channels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manager = AcquisitionManager(root / "unused")
            selected = ["温度", "压力"]
            config = AcquisitionConfig(
                processing_mode="capture_only",
                dataset_schema="new_collection_v11_3",
                driver="simulator",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=selected,
                sample_rate_hz=1000.0,
                save_root=str(root / "capture"),
                condition_id="H06",
            )
            manager.start(config)
            deadline = time.time() + 5.0
            while manager.status()["sample_count"] < 8 and time.time() < deadline:
                time.sleep(0.01)
            stopped = manager.stop()

            with Path(stopped["raw_file"]).open(
                "r", encoding="gb18030", newline=""
            ) as handle:
                fieldnames = next(csv.reader(handle))
            self.assertTrue(all(name in fieldnames for name in selected))
            self.assertFalse(
                any(
                    name in fieldnames
                    for name in NEW_COLLECTION_SENSOR_COLUMNS
                    if name not in selected
                )
            )
            self.assertIn("condition_id", fieldnames)
            self.assertIn("layer_id", fieldnames)

    def test_new_layer_one_archives_previous_specimen_without_mixing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            previous = self._capture(root, source, 0)
            previous_uuid = previous["capture_uuid"]
            current = self._capture(root, source, 0)

            self.assertNotEqual(previous_uuid, current["capture_uuid"])
            self.assertEqual(current["completed_layers"], [1])
            archive = Path(current["archived_previous_session"])
            self.assertTrue(archive.is_dir())
            self.assertTrue(any(archive.glob("*_第1层.CSV")))
            with Path(current["raw_file"]).open(
                "r", encoding="gb18030", newline=""
            ) as handle:
                active_rows = list(csv.DictReader(handle))
            self.assertEqual(
                {row["specimen_id"] for row in active_rows},
                {current["capture_uuid"]},
            )

    def test_public_manifest_masks_both_mysql_passwords(self) -> None:
        config = AcquisitionConfig(
            mysql_password="secret",
            mysql_local_password="local-secret",
            simulation_mysql_password="other-secret",
        )
        public = AcquisitionManager._public_config(config)
        self.assertEqual(public["mysql_password"], "***")
        self.assertEqual(public["mysql_local_password"], "***")
        self.assertEqual(public["simulation_mysql_password"], "***")

    def test_local_and_target_mysql_destinations_can_be_enabled_together(self) -> None:
        config = AcquisitionConfig(
            mysql_local_enabled=True,
            mysql_local_host="127.0.0.1",
            mysql_local_database="afp_local",
            mysql_enabled=True,
            mysql_host="192.168.101.31",
            mysql_user="afp_app",
            mysql_password="remote-secret",
            mysql_database="afp_remote",
        )
        destinations = AcquisitionManager._mysql_destinations(config)
        self.assertEqual([name for name, _ in destinations], ["local", "target"])
        self.assertEqual(destinations[0][1].database, "afp_local")
        self.assertEqual(destinations[1][1].host, "192.168.101.31")
        self.assertEqual(destinations[1][1].password, "remote-secret")

    def test_training_import_ignores_archived_specimen_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active = self._source(root, rows=5)
            history = root / "历史版本" / "旧试样"
            history.mkdir(parents=True)
            archived = history / active.name
            archived.write_bytes(active.read_bytes())
            imported = read_excel_or_folder(root)
            self.assertEqual(imported.source_files, [str(active)])
            self.assertEqual(len(imported.frame), 5)

    def test_pending_mysql_record_is_retried_and_marked_synced(self) -> None:
        class FakeStore:
            def __init__(self, settings):
                self.settings = settings

            def test_connection(self):
                return {"ok": True}

            def save_layer(self, config, **kwargs):
                return {
                    "ok": True,
                    "saved_rows": len(kwargs["rows"]),
                    "specimen_key": config.capture_uuid,
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, rows=5)
            pending = root / "x_mysql_pending.json"
            config = AcquisitionConfig(
                dataset_schema="new_collection_v11_3",
                processing_mode="capture_only",
                driver="simulator",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                capture_uuid="AFP-test",
                mysql_enabled=True,
                mysql_database="afp_test",
            )
            pending.write_text(
                json.dumps(
                    {
                        "config": AcquisitionManager._public_config(config),
                        "layer_file": str(source),
                        "folder_path": str(root),
                        "summary": {"completed_layers": [1]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manager = AcquisitionManager(root / "capture")
            settings = MySQLSettings(
                enabled=True, database="afp_test", password="runtime-secret"
            )
            with patch("acquisition.MySQLCaptureStore", FakeStore):
                result = manager._retry_pending_mysql(settings, root)
            self.assertEqual(result["succeeded"], 1, result)
            self.assertFalse(pending.exists())
            self.assertTrue((root / "x_mysql_synced.json").is_file())

    def test_empty_capture_does_not_replace_last_valid_specimen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            previous = self._capture(root, source, 0)
            active_layer = Path(previous["raw_file"])
            previous_bytes = active_layer.read_bytes()
            metadata_path = (
                Path(previous["session_dir"]) / "采集记录" / "当前试样会话.json"
            )
            previous_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

            empty = root / "empty.csv"
            with empty.open("w", encoding="utf-8-sig", newline="") as handle:
                csv.DictWriter(
                    handle, fieldnames=NEW_COLLECTION_SENSOR_COLUMNS
                ).writeheader()
            manager = AcquisitionManager(root / "unused")
            config = AcquisitionConfig(
                processing_mode="capture_only",
                dataset_schema="new_collection_v11_3",
                driver="simulator",
                source_file=str(empty),
                simulation_source_path=str(empty),
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                sample_rate_hz=1000.0,
                save_root=str(root / "capture"),
                condition_id="H06",
                replicate=1,
                layer=0,
                mysql_enabled=True,
                mysql_database="unused",
            )
            manager.start(config)
            time.sleep(0.05)
            failed = manager.stop()

            self.assertFalse(failed["capture_saved"])
            self.assertIsNone(failed["raw_file"])
            self.assertTrue(failed["failed_capture_archive"])
            self.assertTrue(active_layer.is_file())
            self.assertEqual(active_layer.read_bytes(), previous_bytes)
            current_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(
                current_metadata["capture_uuid"],
                previous_metadata["capture_uuid"],
            )
            self.assertFalse(list(root.rglob("*_mysql_pending.json")))
            self.assertEqual(failed["mysql"]["state"], "not_saved")

    def test_orphan_timestamp_partial_is_archived_on_next_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            first = self._capture(root, source, 0)
            session_dir = Path(first["session_dir"])
            orphan = session_dir / "采集记录" / (
                f"{session_dir.name}_第9层_20000101_000000_时间戳.csv.partial"
            )
            orphan.write_text("row_index,timestamp_iso,timestamp_unix\n", encoding="utf-8")

            second = self._capture(root, source, 1)
            self.assertTrue(second["capture_saved"])
            self.assertFalse(orphan.exists())
            self.assertTrue(
                list(
                    session_dir.joinpath("历史版本", "中断采集").glob(
                        "*时间戳*.partial"
                    )
                )
            )

    def test_pending_retry_limit_is_applied_after_database_filter(self) -> None:
        class FakeStore:
            def __init__(self, settings):
                self.settings = settings

            def test_connection(self):
                return {"ok": True}

            def save_layer(self, config, **kwargs):
                return {"ok": True, "saved_rows": len(kwargs["rows"])}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, rows=3)
            for index in range(25):
                config = AcquisitionConfig(
                    capture_uuid=f"wrong-{index}",
                    mysql_enabled=True,
                    mysql_database="another_database",
                )
                (root / f"{index:02d}_mysql_pending.json").write_text(
                    json.dumps(
                        {
                            "config": AcquisitionManager._public_config(config),
                            "layer_file": str(source),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            target_config = AcquisitionConfig(
                capture_uuid="target",
                mysql_enabled=True,
                mysql_database="afp_test",
            )
            target = root / "99_mysql_pending.json"
            target.write_text(
                json.dumps(
                    {
                        "config": AcquisitionManager._public_config(target_config),
                        "layer_file": str(source),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manager = AcquisitionManager(root / "capture")
            settings = MySQLSettings(enabled=True, database="afp_test")
            with patch("acquisition.MySQLCaptureStore", FakeStore):
                result = manager._retry_pending_mysql(settings, root, limit=20)
            self.assertEqual(result["succeeded"], 1, result)
            self.assertEqual(result["skipped"], 25, result)
            self.assertFalse(target.exists())

    def test_public_pending_retry_uses_remote_profile_without_active_capture(self) -> None:
        class FakeStore:
            def __init__(self, settings):
                self.settings = settings

            def test_connection(self):
                return {"ok": True}

            def save_layer(self, config, **kwargs):
                return {
                    "ok": True,
                    "host": self.settings.host,
                    "saved_rows": len(kwargs["rows"]),
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, rows=2)
            config = AcquisitionConfig(
                capture_uuid="remote-retry",
                mysql_enabled=True,
                mysql_host="10.20.30.40",
                mysql_user="afp_app",
                mysql_database="afp_remote",
            )
            pending = root / "remote_mysql_pending.json"
            pending.write_text(
                json.dumps(
                    {
                        "config": AcquisitionManager._public_config(config),
                        "layer_file": str(source),
                        "folder_path": str(root),
                        "summary": {"completed_layers": [1]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manager = AcquisitionManager(root / "capture")
            with patch("acquisition.MySQLCaptureStore", FakeStore):
                result = manager.retry_pending_mysql(
                    {
                        "enabled": True,
                        "host": "10.20.30.40",
                        "user": "afp_app",
                        "database": "afp_remote",
                    },
                    root,
                )
            self.assertEqual(result["succeeded"], 1, result)
            self.assertFalse(pending.exists())
            self.assertEqual(manager.mysql_status["host"], "10.20.30.40")
            self.assertEqual(manager.mysql_status["state"], "synced")

    def test_stop_is_idempotent_and_does_not_upload_twice(self) -> None:
        class FakeStore:
            save_calls = 0

            def __init__(self, settings):
                self.settings = settings

            def test_connection(self):
                return {"ok": True}

            def save_layer(self, config, **kwargs):
                type(self).save_calls += 1
                return {
                    "ok": True,
                    "database": self.settings.database,
                    "layer": config.layer + 1,
                    "saved_rows": len(kwargs["rows"]),
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manager = AcquisitionManager(root / "unused")
            config = AcquisitionConfig(
                processing_mode="capture_only",
                dataset_schema="new_collection_v11_3",
                driver="simulator",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                sample_rate_hz=1000.0,
                save_root=str(root / "capture"),
                condition_id="H06",
                replicate=1,
                layer=0,
                mysql_enabled=True,
                mysql_database="afp_test",
            )
            with patch("acquisition.MySQLCaptureStore", FakeStore):
                manager.start(config)
                deadline = time.time() + 5.0
                while manager.status()["sample_count"] < 20 and time.time() < deadline:
                    time.sleep(0.01)
                first = manager.stop()
                second = manager.stop()
            self.assertTrue(first["capture_saved"])
            self.assertEqual(first["raw_file"], second["raw_file"])
            self.assertEqual(FakeStore.save_calls, 1)

    def test_completed_layer_is_saved_to_local_and_target_mysql(self) -> None:
        class FakeStore:
            calls: list[str] = []

            def __init__(self, settings):
                self.settings = settings

            def test_connection(self):
                return {"ok": True}

            def save_layer(self, config, **kwargs):
                type(self).calls.append(self.settings.host)
                return {
                    "ok": True,
                    "host": self.settings.host,
                    "saved_rows": len(kwargs["rows"]),
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manager = AcquisitionManager(root / "unused")
            config = AcquisitionConfig(
                processing_mode="capture_only",
                dataset_schema="new_collection_v11_3",
                driver="simulator",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                sample_rate_hz=1000.0,
                save_root=str(root / "capture"),
                mysql_local_enabled=True,
                mysql_local_host="127.0.0.1",
                mysql_enabled=True,
                mysql_host="192.168.101.31",
            )
            with patch("acquisition.MySQLCaptureStore", FakeStore):
                manager.start(config)
                deadline = time.time() + 5.0
                while manager.status()["sample_count"] < 20 and time.time() < deadline:
                    time.sleep(0.01)
                result = manager.stop()
            mysql = result["mysql"]
            self.assertTrue(mysql["ok"], mysql)
            self.assertEqual(mysql["destination_count"], 2)
            self.assertEqual(mysql["successful_destinations"], 2)
            self.assertEqual(FakeStore.calls, ["127.0.0.1", "192.168.101.31"])

    def test_driver_open_failure_is_closed_and_does_not_leave_active_session(self) -> None:
        class FailingDriver:
            def __init__(self):
                self.closed = False

            def open(self):
                raise RuntimeError("device-open-failed")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            driver = FailingDriver()
            manager = AcquisitionManager(root / "capture")
            config = AcquisitionConfig(save_root=str(root / "saved"))
            with patch("acquisition.build_driver", return_value=driver):
                with self.assertRaisesRegex(RuntimeError, "device-open-failed"):
                    manager.start(config)
            self.assertTrue(driver.closed)
            self.assertIsNone(manager.thread)
            self.assertTrue(manager.finalization_complete)

    def test_new_start_auto_finalizes_rows_after_driver_read_error(self) -> None:
        class OneRowThenError:
            def __init__(self):
                self.reads = 0

            def open(self):
                return None

            def read_sample(self):
                self.reads += 1
                if self.reads == 1:
                    return {
                        name: 10.0 + index
                        for index, name in enumerate(
                            NEW_COLLECTION_SENSOR_COLUMNS
                        )
                    }
                raise RuntimeError("simulated-read-error")

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manager = AcquisitionManager(root / "unused")
            first = AcquisitionConfig(
                processing_mode="capture_only",
                dataset_schema="new_collection_v11_3",
                driver="simulator",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                sample_rate_hz=1000.0,
                save_root=str(root / "capture"),
                condition_id="FIRST",
                layer=0,
            )
            second = AcquisitionConfig(
                processing_mode="capture_only",
                dataset_schema="new_collection_v11_3",
                driver="simulator",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                sample_rate_hz=1000.0,
                save_root=str(root / "capture"),
                condition_id="SECOND",
                layer=0,
            )
            drivers = [
                OneRowThenError(),
                SimulatorDriver(source, NEW_COLLECTION_SENSOR_COLUMNS.copy()),
            ]
            with patch("acquisition.build_driver", side_effect=drivers):
                manager.start(first)
                deadline = time.time() + 3.0
                while manager.status()["running"] and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(manager.status()["sample_count"], 1)
                manager.start(second)
                first_layers = list(
                    (root / "capture").rglob("CFIRST_R1_*_第1层.CSV")
                )
                self.assertEqual(len(first_layers), 1)
                deadline = time.time() + 3.0
                while manager.status()["sample_count"] < 5 and time.time() < deadline:
                    time.sleep(0.01)
                manager.stop()


if __name__ == "__main__":
    unittest.main()
