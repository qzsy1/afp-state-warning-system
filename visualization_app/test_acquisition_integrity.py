from __future__ import annotations

import csv
import http.client
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acquisition  # noqa: E402
from generate_pressure_simulation import generate as generate_pressure_simulation  # noqa: E402
from generate_thermocouple64_simulation import generate as generate_thermocouple64_simulation  # noqa: E402
from acquisition import (  # noqa: E402
    AcquisitionConfig,
    AcquisitionManager,
    CURRENT_COLLECTION_SENSOR_COLUMNS,
    LATEST_COLLECTION_SENSOR_COLUMNS,
    MySQLSettings,
    NEW_COLLECTION_SENSOR_COLUMNS,
    SimulatorDriver,
    check_capture_save_root,
    default_capture_interfaces,
)
from training_data import read_excel_or_folder  # noqa: E402


class AcquisitionIntegrityTests(unittest.TestCase):
    def test_plc_production_frames_csv_sidecar_and_mysql_share_clamped_sources(self):
        from test_plc_modbus_contract import PlcModbusContractTests
        from test_mysql_identity import _FakeConnection
        from mysql_storage import MySQLCaptureStore
        from contextlib import ExitStack
        staging = Path(r"D:\AFP_Runtime\.afp-staging")
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging, prefix="plc-contract-") as temp:
            root = Path(temp)
            driver = PlcModbusContractTests().make_driver({"温度": 102.25, "张力": 12.75, "压力": -2.5})
            driver.open = lambda: None
            driver.close = lambda: None
            config = AcquisitionConfig(
                processing_mode="capture_only", acquisition_mode="real", real_acquisition_mode="local_direct",
                dataset_schema="new_collection_v11_3", selected_sensors=["温度", "压力", "张力"],
                sample_rate_hz=20, save_root=str(root), condition_id="PLC_FIXTURE",
                interfaces=[{"id":"plc", "role":"plc", "driver":"modbus_tcp", "endpoint":"192.168.125.5:502",
                             "physical_interface_id":"ethernet:fixture", "physical_interface_kind":"ethernet", "physical_verified":True}],
                interface_channel_assignments={"plc":["温度", "压力", "张力"]})
            manager = AcquisitionManager(root, recovery_scan=False)
            route = {"ok":True, "actual_source_address":"192.168.125.209"}
            with ExitStack() as stack:
                stack.enter_context(patch("acquisition.ModbusTcpDriver", return_value=driver))
                stack.enter_context(patch("acquisition.require_network_path", return_value=route))
                stack.enter_context(patch("acquisition.validate_network_path", return_value=route))
                stack.enter_context(patch("acquisition.device_profile_readiness", return_value={"ready":True,"missing_device_facts":[]}))
                self.assertTrue(manager.test_connection(config)["ok"])
                manager.start(config)
                try:
                    deadline = time.monotonic() + 3
                    while manager.total_sample_count < 8 and time.monotonic() < deadline:
                        time.sleep(.01)
                finally:
                    stopped = manager.stop()
            self.assertTrue(stopped["capture_saved"], stopped.get("last_error"))
            with Path(stopped["layer_file"]).open(encoding="gb18030", newline="") as handle:
                rows = list(csv.DictReader(handle))
            with Path(stopped["timestamp_file"]).open(encoding="utf-8-sig", newline="") as handle:
                sidecar = list(csv.DictReader(handle))
            self.assertEqual(len(rows), len(sidecar))
            source_rows = []
            for row, timestamp in zip(rows, sidecar):
                records = json.loads(timestamp["channel_source_record_json"])
                if row.get("压力") not in (None, ""):
                    self.assertEqual(float(row["压力"]), 0)
                    self.assertEqual(records["压力"]["decoded_value"], -2.5)
                    self.assertEqual(records["压力"]["normalized_value"], 0)
                    self.assertTrue(records["压力"]["negative_pressure_clamped"])
                    self.assertEqual(records["压力"]["source_sequence"], json.loads(timestamp["channel_source_sequence_json"])["压力"])
                    source_rows.append(records["压力"])
            self.assertGreaterEqual(len(source_rows), 5)
            self.assertIn("held_within_freshness", {record["frame_quality"] for record in source_rows})
            self.assertGreater(len({record["source_sequence"] for record in source_rows}), 1)
            connection = _FakeConnection()
            store = MySQLCaptureStore(MySQLSettings(enabled=True, database="afp_fixture"))
            with patch.object(store, "_connect", return_value=("fake", connection)):
                result = store.save_layer(config, rows=iter(rows), layer_file=stopped["layer_file"], full_specimen_file=None,
                    timestamp_file=stopped["timestamp_file"], folder_path=None, summary={})
            self.assertTrue(result["ok"], result)
            evidence = [json.loads(value[4]) for sql, values in connection.cursor_instance.executed
                        if "INSERT INTO afp_source_evidence" in sql for value in values]
            self.assertEqual(len(evidence), len(sidecar))
            for item, timestamp in zip(evidence, sidecar):
                self.assertEqual(item["channels"], json.loads(timestamp["channel_source_record_json"]))
            samples = [value for sql, values in connection.cursor_instance.executed if "INSERT INTO afp_sensor_sample" in sql for value in values]
            self.assertTrue(all(json.loads(value[5]).get("压力", 0) == 0 for value in samples))
            manifest = json.loads(manager.session_metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["plc_measurement"]["plc"]["measurement_profile"], "fibercontrol_plc_v1")

    def test_start_blocker_message_identifies_file_and_recovery_action(self) -> None:
        manager = AcquisitionManager(recovery_scan=False)
        manager.finalization_complete = False
        manager.capture_phase = "finalization_failed"
        manager.recovery_state["files"] = [{"name": "unfinished-matrix.csv.partial"}]
        with self.assertRaisesRegex(RuntimeError, "unfinished-matrix.csv.partial.*恢复.*补传"):
            manager._start_new(AcquisitionConfig())

    def test_csv_merge_preserves_mapping_quoting_and_irregular_row_behavior(self) -> None:
        cases = [
            ("a,b\r\n1,2\r\n\r\n3\r\n", ["a", "b"], "raise"),
            ('a,b\r\n"a,b","line1\r\nline2"\r\n', ["a", "b"], "raise"),
            ("b,a\r\n2,1\r\n", ["a", "b"], "raise"),
            ("a,a\r\n1,2\r\n", ["a", "a"], "raise"),
            ("a,b\r\n1,2,3\r\n", ["a", "b"], "ignore"),
            ("a,b\r\n1,2,3\r\n", ["a", "b"], "raise"),
            ("", ["a", "b"], "raise"),
        ]
        for text, columns, extrasaction in cases:
            with self.subTest(text=text, columns=columns, extrasaction=extrasaction):
                outputs, failures = [], []
                for optimized in (False, True):
                    output = io.StringIO(newline="")
                    reader = csv.DictReader(io.StringIO(text, newline=""))
                    try:
                        if optimized:
                            acquisition._append_csv_rows(output, reader, columns, extrasaction=extrasaction)
                        else:
                            csv.DictWriter(output, fieldnames=columns, extrasaction=extrasaction).writerows(reader)
                        failures.append(None)
                    except ValueError as error:
                        failures.append(str(error))
                    outputs.append(output.getvalue())
                self.assertEqual(outputs[0], outputs[1])
                self.assertEqual(failures[0], failures[1])

    def test_simulation_five_interface_defaults_materialize_distinct_channel_assignments(self) -> None:
        interfaces = acquisition.default_capture_interfaces()
        config = acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v13_0",
            simulation_source_type="single_csv",
            simulation_source_path="simulation.csv",
            interfaces=interfaces,
            selected_sensors=CURRENT_COLLECTION_SENSOR_COLUMNS.copy(),
        )

        self.assertEqual(
            config.interface_channel_assignments,
            {
                "thermocouple_64ch": [f"温度{index}" for index in range(1, 65)],
                "plc_process": ["温度", "压力", "张力"],
                "uvc_temperature": ["ROI平均温度"],
                "abb_motion": ["线速度", "ABB_X", "ABB_Y", "ABB_Z"],
                "m3232_pressure": ["薄膜压力"],
                "esr1001_strain": ["应变1", "应变2", "应变3", "应变4"],
            },
        )

    def test_simulation_five_interface_routes_still_build_one_simulation_driver(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v11_3",
            simulation_source_type="single_csv",
            simulation_source_path="simulation.csv",
            interfaces=acquisition.default_capture_interfaces(),
            selected_sensors=acquisition.NEW_COLLECTION_SENSOR_COLUMNS.copy(),
        )

        with patch.object(acquisition, "SimulatorDriver", wraps=acquisition.SimulatorDriver) as driver:
            built = acquisition.build_driver(config)

        self.assertIsInstance(built, acquisition.SimulatorDriver)
        self.assertNotIsInstance(built, acquisition.MultiInterfaceDriver)
        driver.assert_called_once_with(
            Path("simulation.csv"),
            acquisition.NEW_COLLECTION_SENSOR_COLUMNS,
        )

    def test_default_simulation_basename_resolves_to_configured_absolute_path(self) -> None:
        resolver = getattr(acquisition, "resolve_default_simulation_source", None)
        self.assertTrue(callable(resolver), "authorized simulation needs a canonical source resolver")
        with tempfile.TemporaryDirectory() as temp:
            default_source = Path(temp) / "SIM_PRESSURE_M3232_new_collection.csv"
            default_source.write_text("温度\n350\n", encoding="utf-8")
            payload = {
                "simulation_source_type": "single_csv",
                "simulation_source_path": default_source.name,
                "source_file": default_source.name,
            }

            normalized = resolver(payload, default_source)

        self.assertEqual(normalized["simulation_source_path"], str(default_source.resolve()))
        self.assertEqual(normalized["source_file"], str(default_source.resolve()))

    def test_simulation_check_reports_each_mapped_interface_as_available(self) -> None:
        interfaces = default_capture_interfaces()
        assignments = {
            "thermocouple_64ch": [f"温度{index}" for index in range(1, 65)],
            "plc_process": ["温度", "压力", "张力"],
            "uvc_temperature": ["ROI平均温度"],
            "abb_motion": ["线速度", "ABB_X", "ABB_Y", "ABB_Z"],
            "m3232_pressure": ["薄膜压力"],
            "esr1001_strain": ["应变1", "应变2", "应变3", "应变4"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            simulation_root = Path(temporary) / "thermocouple64"
            generate_thermocouple64_simulation(simulation_root)
            source = simulation_root / "quick_240.csv"
            config = AcquisitionConfig(
                driver="simulator",
                acquisition_mode="simulation",
                dataset_schema="new_collection_v13_0",
                simulation_source_path=str(source),
                source_file=str(source),
                interfaces=interfaces,
                interface_channel_assignments=assignments,
                selected_sensors=CURRENT_COLLECTION_SENSOR_COLUMNS.copy(),
            )
            result = AcquisitionManager().test_connection(config, timeout_seconds=0.1)
        self.assertEqual(
            {item["id"] for item in result["interfaces"]},
            set(assignments),
        )
        self.assertTrue(all(item["enabled"] for item in result["interfaces"]))
        self.assertTrue(all(item["ok"] for item in result["interfaces"]))
        self.assertEqual(
            result["interfaces"][-1]["detected_channels"],
            ["应变1", "应变2", "应变3", "应变4"],
        )

    def test_published_default_v12_simulation_source_passes_backend_readiness(self) -> None:
        from app import NEW_DEMO_SOURCE, create_server

        interfaces = default_capture_interfaces()
        assignments = {
            "thermocouple_64ch": [f"温度{index}" for index in range(1, 65)],
            "plc_process": ["温度", "压力", "张力"],
            "uvc_temperature": ["ROI平均温度"],
            "abb_motion": ["线速度", "ABB_X", "ABB_Y", "ABB_Z"],
            "m3232_pressure": ["薄膜压力"],
            "esr1001_strain": ["应变1", "应变2", "应变3", "应变4"],
        }
        request = {
            "driver": "simulator",
            "acquisition_mode": "simulation",
            "dataset_schema": "new_collection_v13_0",
            "simulation_source_type": "single_csv",
            "simulation_source_path": str(NEW_DEMO_SOURCE),
            "source_file": str(NEW_DEMO_SOURCE),
            "interfaces": interfaces,
            "interface_channel_assignments": assignments,
            "selected_sensors": CURRENT_COLLECTION_SENSOR_COLUMNS.copy(),
        }
        with tempfile.TemporaryDirectory() as temporary:
            manager = AcquisitionManager(Path(temporary) / "capture")
            dashboard = SimpleNamespace(
                acquisition=manager,
                validate_prediction_setup=lambda config, load_model=False: {},
            )
            server = create_server(
                "127.0.0.1",
                0,
                dashboard=dashboard,
                access_context="local_admin",
                ingress_state_file=Path(temporary) / "public-ingress-state.json",
            )
            guest_source = server.guest_manager.source_status(
                "published_default_source_contract"
            )
            self.assertTrue(
                set(LATEST_COLLECTION_SENSOR_COLUMNS).issubset(
                    set(guest_source["channels"])
                ),
                guest_source,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(
                    acquisition,
                    "MultiInterfaceDriver",
                    side_effect=AssertionError("模拟检查不得构造物理多接口驱动"),
                ):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_port, timeout=10
                    )
                    try:
                        connection.request(
                            "POST",
                            "/api/acquisition/test",
                            json.dumps(request).encode("utf-8"),
                            {"Content-Type": "application/json"},
                        )
                        response = connection.getresponse()
                        result = json.loads(response.read().decode("utf-8"))
                    finally:
                        connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(response.status, 200, result)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(len(result["interfaces"]), 6)
        self.assertTrue(all(item["ok"] for item in result["interfaces"]))
        self.assertEqual(
            [item["name"] for item in result["sensors"] if not item["ok"]],
            [],
        )

    def test_save_root_status_requires_existing_writable_nonempty_directory(self) -> None:
        self.assertFalse(check_capture_save_root("")["ok"])
        relative_status = check_capture_save_root("AFP_Capture")
        self.assertFalse(relative_status["ok"])
        self.assertEqual(relative_status["code"], "relative")
        self.assertIn("绝对路径", relative_status["message"])
        with tempfile.TemporaryDirectory() as temporary:
            existing = Path(temporary)
            status = check_capture_save_root(str(existing))
            self.assertTrue(status["ok"])
            missing = existing / "does-not-exist"
            missing_status = check_capture_save_root(str(missing))
            self.assertFalse(missing_status["ok"])
            self.assertFalse(missing.exists())

    def test_empty_save_root_keeps_simulation_in_memory_without_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "default"
            source = Path(temporary) / "simulation.csv"
            columns = [
                "转速", "位移", *[f"温度{index}" for index in range(1, 9)], "压力", "振动"
            ]
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows({name: index + 1 for name in columns} for index in range(8))
            manager = AcquisitionManager(root)
            config = AcquisitionConfig(
                driver="simulator",
                source_file=str(source),
                sample_rate_hz=1000.0,
                selected_sensors=[
                    "转速", "位移", *[f"温度{index}" for index in range(1, 9)], "压力", "振动"
                ],
                save_root="",
            )
            manager.start(config)
            deadline = time.time() + 3.0
            while manager.status()["sample_count"] < 8 and time.time() < deadline:
                time.sleep(0.02)
            stopped = manager.stop()
            self.assertGreaterEqual(stopped["sample_count"], 8)
            self.assertFalse(stopped["capture_saved"])
            self.assertFalse(stopped["save_enabled"])
            self.assertIsNone(stopped["raw_file"])
            self.assertEqual(stopped["save_status"]["code"], "empty")
            self.assertEqual(list(root.rglob("*")), [])

    def test_physical_binding_allows_plc_and_abb_to_share_one_ethernet_adapter(self) -> None:
        interfaces = [
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
            selected_sensors=["温度", "压力", "薄膜压力原始响应", "ROI平均温度", "张力", "线速度", "ABB_X", "ABB_Y", "ABB_Z"],
            interfaces=interfaces,
            interface_channel_assignments={
                "plc_process": ["温度", "压力", "张力"],
                "uvc_temperature": ["ROI平均温度"],
                "abb_motion": ["线速度", "ABB_X", "ABB_Y", "ABB_Z"],
                "m3232_pressure": ["薄膜压力原始响应"],
            },
        )
        self.assertEqual(config.interfaces[0]["physical_interface_id"], config.interfaces[2]["physical_interface_id"])
        self.assertEqual(config.interfaces[0]["physical_interface_kind"], "ethernet")

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

    def test_historical_thermocouple_cannot_be_enabled_for_real_capture(self) -> None:
        with self.assertRaisesRegex(ValueError, "历史文件只读回放"):
            AcquisitionConfig(
                acquisition_mode="real", dataset_schema="new_collection_v11_3",
                selected_sensors=["温度1"],
                interfaces=[
                    {
                        "id": "thermocouple_8ch", "enabled": True,
                        "role": "thermocouple", "driver": "smrf_hid",
                        "endpoint": "", "physical_port_id": "usbport:test",
                        "physical_interface_id": "", "physical_interface_kind": "usb_hid",
                        "physical_verified": False,
                    },
                ],
                interface_channel_assignments={"thermocouple_8ch": ["温度1"]},
            )

    def test_simulation_allows_shared_logical_source_for_multiple_interfaces(self) -> None:
        """An uploaded file is one logical source, not a real adapter binding."""
        interfaces = [
            {
                "id": "thermocouple_8ch", "enabled": True,
                "role": "thermocouple", "driver": "smrf_hid",
                "endpoint": "SMRFCT08B", "physical_interface_id": "simulation_source",
                "physical_interface_kind": "usb_hid",
            },
            {
                "id": "plc_process", "enabled": True,
                "role": "plc", "driver": "modbus_tcp",
                "endpoint": "192.168.125.5:502", "physical_interface_id": "simulation_source",
                "physical_interface_kind": "ethernet",
            },
        ]
        config = AcquisitionConfig(
            acquisition_mode="simulation", dataset_schema="new_collection_v11_3",
            simulation_source_path="simulation.csv", selected_sensors=["温度1", "温度", "压力"],
            interfaces=interfaces,
            interface_channel_assignments={
                "thermocouple_8ch": ["温度1"],
                "plc_process": ["温度", "压力"],
            },
        )
        self.assertEqual(config.interface_channel_assignments["plc_process"], ["温度", "压力"])

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

    def test_physical_binding_allows_explicit_serial_fallback_for_testing(self) -> None:
        with self.assertRaisesRegex(ValueError, "历史文件只读回放"):
            AcquisitionConfig(
                acquisition_mode="real", dataset_schema="new_collection_v11_3",
                selected_sensors=["温度1"],
                interfaces=[
                    {"id": "thermocouple_8ch", "enabled": True, "role": "thermocouple", "driver": "smrf_hid", "endpoint": "COM1", "physical_interface_id": "serial:COM1", "physical_interface_kind": "serial", "physical_fallback": True},
                ],
                interface_channel_assignments={"thermocouple_8ch": ["温度1"]},
            )

    def test_connection_skips_unverified_protocol_driver_on_serial_fallback(self) -> None:
        """A temporary COM fallback must not invoke a blocking USB/UVC driver."""
        config = AcquisitionConfig(
            acquisition_mode="real", dataset_schema="new_collection_v11_3",
            selected_sensors=["ROI平均温度"],
            interfaces=[
                {
                    "id": "uvc_temperature", "enabled": True,
                    "role": "thermal_uvc", "driver": "uvc_thermal",
                    "endpoint": "COM1", "physical_interface_id": "serial:COM1",
                    "physical_interface_kind": "serial", "physical_fallback": True,
                },
            ],
            interface_channel_assignments={"uvc_temperature": ["ROI平均温度"]},
        )
        manager = AcquisitionManager()
        with patch("acquisition.MultiInterfaceDriver") as driver:
            result = manager.test_connection(config, timeout_seconds=0.1)
        driver.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["interfaces"][0]["state"], "not_connected")
        self.assertIn("临时分配串口", result["interfaces"][0]["message"])

    def test_connection_does_not_probe_uvc_with_unapproved_device_profile(self) -> None:
        config = AcquisitionConfig(
            acquisition_mode="real", dataset_schema="new_collection_v11_3",
            selected_sensors=["ROI平均温度"],
            interfaces=[
                {
                    "id": "uvc_temperature", "enabled": True,
                    "role": "thermal_uvc", "driver": "uvc_thermal",
                    "endpoint": "BSV UVC (WinUSB)", "physical_interface_id": "uvc:bsv",
                    "physical_interface_kind": "usb_uvc", "physical_verified": False,
                },
            ],
            interface_channel_assignments={"uvc_temperature": ["ROI平均温度"]},
        )
        manager = AcquisitionManager()
        with patch("acquisition.MultiInterfaceDriver") as driver:
            driver.return_value.read_sample.return_value = {"ROI平均温度": 42.5}
            result = manager.test_connection(config, timeout_seconds=0.1)
        driver.assert_not_called()
        self.assertEqual(result["interfaces"][0]["detected_channels"], [])
        self.assertEqual(result["interfaces"][0]["state"], "device_profile_incomplete")
        self.assertIn("设备档", result["interfaces"][0]["message"])
        self.assertFalse(result["interfaces"][0]["ok"])
        self.assertFalse(result["ok"])

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
        (root / "capture").mkdir(parents=True, exist_ok=True)
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
            quality_sidecar = Path(summary["quality_sidecar"])
            self.assertTrue(quality_sidecar.is_file())
            quality_payload = json.loads(quality_sidecar.read_text(encoding="utf-8"))
            self.assertEqual(quality_payload["capture_uuid"], second["capture_uuid"])
            self.assertEqual(quality_payload["sample_count"], second["sample_count"])
            with Path(second["raw_file"]).open("r", encoding="gb18030", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                {row["specimen_id"] for row in rows}, {second["capture_uuid"]}
            )
            with Path(second["timestamp_file"]).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                timestamp_rows = list(csv.DictReader(handle))
            self.assertTrue(
                all(row["channel_quality_json"] for row in timestamp_rows)
            )
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
            (root / "capture").mkdir(parents=True, exist_ok=True)
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
        class FakeStore:
            def __init__(self, settings):
                self.settings = settings

            def preflight(self, *, write_test=False):
                return {"ok": True, "write_test": write_test}

            def save_layer(self, config, **kwargs):
                raise AssertionError("empty capture must not be uploaded")

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
            with patch("acquisition.MySQLCaptureStore", FakeStore):
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

            def preflight(self, *, write_test=False):
                return {"ok": True, "write_test": write_test}

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

            def preflight(self, *, write_test=False):
                return {"ok": True, "write_test": write_test}

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

            def preflight(self, *, write_test=False):
                return {"ok": True, "write_test": write_test}

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
            (root / "capture").mkdir(parents=True, exist_ok=True)
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
            stages = [event["stage"] for event in first["capture_timeline"]["events"]]
            expected = ["stop_requested", "sampling_loop_stopped", "persistence_drain_begin",
                        "persistence_drain_end", "devices_released", "file_finalize_begin",
                        "file_finalize_end", "specimen_rebuild_begin", "specimen_rebuild_end",
                        "finalization_result"]
            self.assertEqual([stage for stage in stages if stage in expected], expected)
            result = first["capture_timeline"]["events"][-1]
            self.assertTrue(result["complete"])
            self.assertTrue(result["capture_saved"])

    def test_completed_layer_is_saved_to_local_and_target_mysql(self) -> None:
        class FakeStore:
            calls: list[str] = []

            def __init__(self, settings):
                self.settings = settings

            def test_connection(self):
                return {"ok": True}

            def preflight(self, *, write_test=False):
                return {"ok": True, "write_test": write_test}

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
            (root / "capture").mkdir(parents=True, exist_ok=True)
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

    def test_mysql_is_saved_from_memory_when_csv_root_is_empty(self) -> None:
        class FakeStore:
            calls: list[int] = []

            def __init__(self, settings):
                self.settings = settings

            def test_connection(self):
                return {"ok": True}

            def preflight(self, *, write_test=False):
                return {"ok": True, "write_test": write_test}

            def save_layer(self, config, **kwargs):
                type(self).calls.append(len(kwargs["rows"]))
                return {"ok": True, "saved_rows": len(kwargs["rows"])}

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
                save_root="",
                mysql_local_enabled=True,
                mysql_local_host="127.0.0.1",
                mysql_local_database="afp_test",
            )
            with patch("acquisition.MySQLCaptureStore", FakeStore):
                manager.start(config)
                deadline = time.time() + 5.0
                while manager.status()["sample_count"] < 20 and time.time() < deadline:
                    time.sleep(0.01)
                result = manager.stop()
            self.assertFalse(result["capture_saved"])
            self.assertTrue(result["mysql"]["ok"], result)
            self.assertEqual(FakeStore.calls, [result["sample_count"]])

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
            (root / "capture").mkdir(parents=True, exist_ok=True)
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

    def test_active_simulation_is_finalized_and_replaced_by_new_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manager = AcquisitionManager(root / "unused")
            first = AcquisitionConfig(
                processing_mode="capture_only",
                dataset_schema="new_collection_v11_3",
                driver="simulator",
                acquisition_mode="simulation",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                sample_rate_hz=10.0,
                save_root="",
                condition_id="FIRST",
            )
            second = AcquisitionConfig(
                processing_mode="capture_only",
                dataset_schema="new_collection_v11_3",
                driver="simulator",
                acquisition_mode="simulation",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                sample_rate_hz=10.0,
                save_root="",
                condition_id="SECOND",
            )

            manager.start(first)
            self.assertTrue(manager.status()["running"])
            replaced = manager.start(second)
            try:
                self.assertTrue(replaced["running"])
                self.assertEqual(replaced["config"]["condition_id"], "SECOND")
            finally:
                manager.stop()

    def test_active_real_capture_cannot_be_replaced_implicitly(self) -> None:
        manager = AcquisitionManager()
        manager.config = AcquisitionConfig(acquisition_mode="simulation", driver="simulator")
        manager.config.acquisition_mode = "real"
        manager.thread = SimpleNamespace(is_alive=lambda: True)
        replacement = AcquisitionConfig(acquisition_mode="simulation", driver="simulator")

        with self.assertRaisesRegex(RuntimeError, "真实采集已经在运行"):
            manager.start(replacement)


if __name__ == "__main__":
    unittest.main()
