from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import math
import types
import hashlib
import csv
import threading
import os
from unittest.mock import patch

from m3232_serial import MatrixParser, MatrixRecorder, RAW_CHANNEL, PROTOCOL, load_calibration, matrix_csv_path

# PDF 数据通信协议_M3232(1).pdf p1, SHA256 29d2b460bb11a8914a966671e0ebdbb643800687947ae48b3c79962aa87f6c76.
# A transcription of the zero example is protocol-only evidence, never physical acceptance.
ZERO_MATRIX = [[0] * 32 for _ in range(32)]
ZERO_FRAME = json.dumps(ZERO_MATRIX, separators=(",", ":")).encode() + b"\n"


class SerialDouble:
    def __init__(self, data=b"", *args, **kwargs):
        self.data, self.writes, self.closed = data, [], False
    @property
    def in_waiting(self): return len(self.data)
    def read(self, count):
        result, self.data = self.data[:count], self.data[count:]
        return result
    def write(self, data): self.writes.append(data)
    def reset_input_buffer(self): self.data = b""
    def close(self): self.closed = True


def driver_double(**kwargs):
    port = types.SimpleNamespace(device="COM_TEST", vid=6790, pid=29987, serial_number="", location="test-only")
    serial = SerialDouble()
    driver = M3232PressureDriver("COM_TEST", device_identity={"confirmed": True, "endpoint": "COM_TEST", "location": "test-only"},
        serial_factory=lambda *a, **k: serial, inventory=lambda: [port], **kwargs)
    return driver, serial

from acquisition import (
    AcquisitionConfig,
    M3232PressureDriver,
    M3232_BAUDRATE,
    NEW_COLLECTION_SENSOR_COLUMNS,
    CURRENT_COLLECTION_SENSOR_COLUMNS,
    SimulatorDriver,
    default_capture_interfaces,
    _resolve_interface_channel_assignments,
)
from generate_pressure_simulation import generate as generate_pressure_simulation
from generate_thermocouple64_simulation import generate as generate_thermocouple64_simulation


class M3232PressureDriverTests(unittest.TestCase):
    def test_manual_default_baudrate_is_used(self) -> None:
        self.assertEqual(M3232_BAUDRATE, 230400)

    def test_parser_requires_lf_and_accepts_every_byte_fragment(self) -> None:
        parser = MatrixParser()
        for value in ZERO_FRAME[:-1]: self.assertEqual(parser.feed(bytes([value])), [])
        frames = parser.feed(b"\n")
        self.assertEqual([f[0] for f in frames], [ZERO_MATRIX])
        self.assertEqual(sum(len(row) for row in frames[0][0]), 1024)
        self.assertEqual(parser.feed(b""), [])

    def test_zero_is_valid_but_noise_partial_and_empty_are_not_observations(self) -> None:
        driver, serial = driver_double()
        driver.open()
        for data in [b"", b"noise\n", ZERO_FRAME[:400]]:
            serial.data = data
            self.assertIsNone(driver.read_sample())
            self.assertFalse(driver.protocol_verified)
        serial.data = ZERO_FRAME[400:]
        self.assertEqual(driver.read_sample(), {RAW_CHANNEL: 0.0})
        observed = driver.last_frame
        self.assertIsNone(driver.read_sample())
        self.assertEqual(driver.last_frame, observed)
        serial.data = ZERO_FRAME
        self.assertEqual(driver.read_sample(), {RAW_CHANNEL: 0.0})
        self.assertEqual(driver.sequence, 2)
        driver.close()
        self.assertEqual(serial.writes, [b"begin\n", b"end\n"])
        self.assertTrue(serial.closed)

    def test_multiframe_pretty_crlf_and_reject_invalid_values(self):
        parser = MatrixParser()
        for invalid in [[], [[0]], [[False]*32 for _ in range(32)], [["0"]*32 for _ in range(32)],
                        [[math.nan]*32 for _ in range(32)], [[math.inf]*32 for _ in range(32)]]:
            self.assertEqual(parser.feed(json.dumps(invalid).encode()+b"\n"), [])
        self.assertEqual(parser.feed(b"[[\xff]]\n"), [])
        frames = parser.feed(json.dumps(ZERO_MATRIX, indent=1).encode()+b"\r\n"+ZERO_FRAME)
        self.assertEqual(len(frames), 2)
        self.assertGreaterEqual(parser.rejected, 7)

    def test_timeout_limit_and_recovery(self):
        now = [0.0]
        parser = MatrixParser(max_bytes=4096, clock=lambda: now[0])
        self.assertEqual(parser.feed(b"[["+b" "*5000), [])
        self.assertEqual(len(parser.feed(ZERO_FRAME)), 1)
        parser.feed(b"[[")
        now[0] = 3
        self.assertEqual(len(parser.feed(b"\n"+ZERO_FRAME)), 1)
        self.assertEqual(parser.rejected, 2)

    def test_first_frame_timeout_restart_and_disconnect(self):
        now = [0.0]
        driver, serial = driver_double(clock=lambda:now[0])
        driver.open()
        now[0] = 2.1
        with self.assertRaises(TimeoutError): driver.read_samples()
        driver.close()
        driver.open()
        self.assertIsNone(driver.last_record)
        self.assertFalse(driver.protocol_verified)
        serial.data = ZERO_FRAME+ZERO_FRAME
        records = driver.read_samples()
        self.assertEqual([r[1]["receive_sequence"] for r in records], [1,2])
        now[0] += 2.1
        with self.assertRaises(TimeoutError): driver.read_samples()
        driver.close()

    def test_no_commands_without_identity_confirmation(self):
        driver = M3232PressureDriver("COM_TEST")
        with self.assertRaisesRegex(ValueError, "binding_unconfirmed"): driver.open()

    def test_unconfirmed_force_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "标定"): M3232PressureDriver(data_mode="calibrated_force")

    def test_config_preserves_explicit_baud_and_fingerprints_protocol_semantics(self):
        from real_acquisition import readiness_config_fingerprint
        from pre_sensor_config import InterfacePlanStore
        from copy import deepcopy
        interface = dict(id="film", role="pressure", driver="m3232_pressure", endpoint="COM_TEST", baudrate=115200,
                         physical_interface_id="serial:COM_TEST", data_mode="raw_matrix")
        cfg = AcquisitionConfig(acquisition_mode="real", processing_mode="capture_only", dataset_schema="new_collection_v13_0",
                                interfaces=[interface], selected_sensors=[RAW_CHANNEL])
        self.assertEqual(cfg.interfaces[0]["baudrate"], 115200)
        self.assertEqual(cfg.selected_sensors, [RAW_CHANNEL])
        self.assertEqual(cfg.interface_channel_assignments, {"film":[RAW_CHANNEL]})
        self.assertIn(RAW_CHANNEL, cfg.raw_columns)
        self.assertNotIn("薄膜压力", cfg.raw_columns)
        digest = readiness_config_fingerprint(cfg)
        for key, value in [("matrix_rows",16), ("matrix_cols",16), ("protocol_profile","other"),
                           ("data_mode","calibrated_force"), ("first_frame_timeout_seconds",1),
                           ("frame_timeout_seconds",1), ("max_buffer_bytes",131072), ("calibration_digest","changed")]:
            changed = deepcopy(cfg)
            changed.interfaces[0][key] = value
            self.assertNotEqual(readiness_config_fingerprint(changed),digest,key)
        plan = InterfacePlanStore.sanitize({"active":cfg.dataset_schema,"plans":{cfg.dataset_schema:{"interfaces":cfg.interfaces}}})
        self.assertEqual(plan["plans"][cfg.dataset_schema]["interfaces"][0]["matrix_rows"],32)

    def test_recorder_rotates_and_accounts_each_matrix_with_digests(self):
        with tempfile.TemporaryDirectory() as root:
            recorder = MatrixRecorder(root, {"source":"synthetic_test_only"}, rotate_bytes=100)
            for sequence in range(4): recorder.submit({"kind":"matrix","receive_sequence":sequence,"matrix":ZERO_MATRIX})
            recorder.close({"valid_frames":4})
            summary = json.loads(recorder.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["saved_source_rows"],4)
            self.assertEqual(summary["writer_error"],"")
            self.assertEqual(len(summary["files"]),4)
            self.assertTrue(all(len(f["sha256"]) == 64 for f in summary["files"]))
            with self.assertRaisesRegex(RuntimeError,"writer_failed"):recorder.submit({})

    def test_helper_requires_current_protocol_capability(self):
        from m3232_serial import require_helper_capabilities
        config = types.SimpleNamespace(acquisition_mode="real",real_acquisition_mode="remote_helper",
            interfaces=[dict(driver="m3232_pressure")],helper_capabilities={})
        with self.assertRaisesRegex(RuntimeError,"helper_capability_missing"):require_helper_capabilities(config)
        config.helper_capabilities[PROTOCOL]=True
        with self.assertRaisesRegex(RuntimeError, "m3232_matrix_csv_v1"):
            require_helper_capabilities(config)
        config.helper_capabilities["m3232_matrix_csv_v1"]=True
        require_helper_capabilities(config)

    def test_matrix_csv_preserves_source_order_pixels_and_equal_zero_frames(self):
        import csv
        with tempfile.TemporaryDirectory() as root:
            path = matrix_csv_path(Path(root) / "main.csv")
            rec = MatrixRecorder(root, {"capture_uuid": "test-only", "main_csv": "main.csv"}, csv_path=path)
            matrix = [[r * 32 + c for c in range(32)] for r in range(32)]
            for seq, pixels in enumerate([matrix, ZERO_MATRIX, ZERO_MATRIX], 1):
                rec.submit(dict(kind="matrix", receive_sequence=seq, host_wall_time=1700000000.0, host_monotonic=2.0, matrix=pixels))
            rec.close({"valid_frames": 3})
            self.assertEqual(rec.error, "")
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
                pixels = [key for key in reader.fieldnames if key.startswith("R")]
            self.assertEqual(len(pixels), 1024)
            self.assertEqual([int(rows[0][key]) for key in pixels], list(range(1024)))
            self.assertEqual([int(row["receive_sequence"]) for row in rows], [1, 2, 3])
            self.assertTrue(all(row[key] == "0" for row in rows[1:] for key in pixels))
            with self.assertRaises(FileExistsError):
                MatrixRecorder(root, {}, csv_path=path)

    def test_matrix_csv_write_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            rec = MatrixRecorder(root, {}, csv_path=Path(root)/"pixels.csv")
            rec.submit(dict(kind="matrix", matrix=[[0]], host_wall_time=1))
            rec.close({"valid_frames": 1})
            self.assertIn("requires_32x32", rec.error)
            self.assertEqual(rec.csv_rows, 0)

    def test_multi_pass_stop_keeps_completed_matrix_counts(self):
        from acquisition import MultiInterfaceDriver
        class Worker:
            def __init__(self, identity, driver_name, slow=False):
                self.interface_id, self.slow, self.calls = identity, slow, 0
                self.driver = types.SimpleNamespace(quality_metadata={"driver": driver_name, "valid_frames": 5,
                    "saved_matrix_csv_rows": 5, "saved_source_rows": 5})
            def stop(self, **kwargs):
                self.calls += 1
                return not self.slow or self.calls > 2
            def status(self):
                return {"interface_id": self.interface_id, "state": "stopped" if self.calls > 2 or not self.slow else "stopping"}
        driver = MultiInterfaceDriver([], [])
        driver.workers = [Worker("film", "m3232_pressure"), Worker("slow", "huipu_tc64_modbus_rtu", True)]
        driver.close()
        self.assertEqual(len(driver.workers), 1)
        driver.close()
        self.assertEqual(driver.workers, [])
        self.assertEqual(driver.interface_quality_metadata["film"]["saved_matrix_csv_rows"], 5)
        self.assertEqual({row["interface_id"] for row in driver.final_worker_status}, {"film", "slow"})

    def test_matrix_csv_io_write_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            rec = MatrixRecorder(root, {}, csv_path=Path(root)/"pixels.csv")
            with patch.object(rec.csv_writer, "writerow", side_effect=OSError("test-only disk write failure")):
                rec.submit(dict(kind="matrix", matrix=ZERO_MATRIX, host_wall_time=1700000000))
                rec.close({"valid_frames": 1})
            self.assertIn("disk write failure", rec.error)
            self.assertEqual(rec.csv_rows, 0)

    def test_layer_matrix_pairing_combines_and_archives_without_overwrite(self):
        import csv
        import acquisition as a
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            manager.config = a.AcquisitionConfig(acquisition_mode="simulation", dataset_schema="new_collection_v13_0",
                selected_sensors=["压力"], processing_mode="capture_only", source_file="fixture-only.csv")
            manager.session_dir, manager.session_stamp = Path(root), "test"
            token = a._parameter_token(manager.config)
            for layer in (1, 2):
                main = Path(root) / f"{token}_第{layer}层.CSV"
                with main.open("w", encoding="gb18030", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=manager.config.raw_columns)
                    writer.writeheader()
                    writer.writerow({"压力": layer})
                rec = MatrixRecorder(root, {"main_csv": main.name, "layer_id": layer}, csv_path=matrix_csv_path(main))
                rec.submit(dict(kind="matrix", matrix=ZERO_MATRIX, host_wall_time=1700000000, receive_sequence=1))
                rec.close({"valid_frames": 1})
                self.assertEqual(rec.error, "")
            combined = manager._rebuild_whole_specimen()
            with matrix_csv_path(combined).open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual([row["layer_id"] for row in csv.DictReader(stream)], ["1", "2"])
            manager.raw_path = Path(root)/f"{token}_第1层.CSV"
            before = matrix_csv_path(manager.raw_path).read_bytes()
            archived = manager._archive_existing(manager.raw_path, "重采铺层")
            self.assertEqual(matrix_csv_path(archived).read_bytes(), before)
            self.assertFalse(matrix_csv_path(manager.raw_path).exists())
            folder = manager._archive_previous_specimen(token, {})
            self.assertTrue(matrix_csv_path(Path(folder)/combined.name).is_file())

    def test_disabled_legacy_film_options_do_not_block_another_interface(self):
        cfg = AcquisitionConfig(acquisition_mode="real", processing_mode="capture_only", selected_sensors=["压力"],
            interfaces=[dict(id="plc-test",role="custom",driver="serial_json",endpoint="COM_TEST",physical_interface_id="serial:COM_TEST", channel_map={"p":"压力"}),
                        dict(id="film",enabled=False,role="pressure",driver="m3232_pressure",data_mode="raw_adc")])
        self.assertEqual(cfg.selected_sensors,["压力"])

    def test_afp_csv_mysql_and_helper_frames_reconcile_with_matrix_sidecar(self):
        import csv
        import time
        from acquisition import AcquisitionManager
        from local_capture_agent import LocalCaptureAgent
        from local_capture_helper_entry import helper_capabilities
        from contextlib import ExitStack
        binding = dict(endpoint="COM_TEST",confirmed=True,location="fixture-only")
        port = types.SimpleNamespace(device="COM_TEST",location="fixture-only")
        class StreamingSerial(SerialDouble):
            active = False
            @property
            def in_waiting(self): return len(ZERO_FRAME) if self.active else 0
            def read(self,n): return ZERO_FRAME
            def write(self,data):
                super().write(data)
                self.active = data == b"begin\n"
        saved = []
        class Store:
            def __init__(self,settings): pass
            def preflight(self,**kwargs): return {"ok":True}
            def save_layer(self,config,**kwargs):
                rows = list(kwargs["rows"])
                saved.append(rows)
                return {"ok":True,"saved_rows":len(rows)}
        for remote in (False, True):
            with self.subTest(helper=remote), tempfile.TemporaryDirectory() as root, ExitStack() as stack:
                stack.enter_context(patch("serial.Serial",side_effect=StreamingSerial))
                stack.enter_context(patch("serial.tools.list_ports.comports",return_value=[port]))
                stack.enter_context(patch("acquisition.MySQLCaptureStore",Store))
                cfg = AcquisitionConfig(acquisition_mode="real",processing_mode="capture_only",dataset_schema="new_collection_v13_0",
                    interfaces=[dict(id="film",role="pressure",driver="m3232_pressure",endpoint="COM_TEST",device_identity=binding,physical_interface_id="serial:COM_TEST",physical_verified=True)],
                    selected_sensors=[RAW_CHANNEL],save_root=root,mysql_enabled=True,
                    real_acquisition_mode="remote_helper" if remote else "local_direct",
                    execution_host="helper_local" if remote else "", helper_session_id="fixture" if remote else "",
                    helper_capabilities=helper_capabilities() if remote else {})
                manager = AcquisitionManager(Path(root),recovery_scan=False)
                agent = LocalCaptureAgent(manager,simulation_cache_root=Path(root)/"cache") if remote else None
                checked = agent.check_capture(cfg) if agent else manager.test_connection(cfg)
                self.assertTrue(checked["ok"],checked)
                if agent: agent.start_capture(cfg)
                else: manager.start(cfg)
                try:
                    time.sleep(.25)
                    if agent:
                        batch = agent.next_sample_batch()
                        frames = batch["frames"]
                        self.assertTrue(frames)
                        from real_acquisition import UnifiedFrame
                        restored = UnifiedFrame.from_envelope_v2(frames[-1], expected_session_id=manager.capture_uuid,
                            expected_mode_id=cfg.dataset_schema,catalog=batch["frame_catalog"])
                        self.assertEqual(batch["frame_catalog"]["channels"][0]["unit"],"raw")
                        source = restored.channels[RAW_CHANNEL].metadata["matrix_source"]
                        self.assertEqual(source["unit"],"raw")
                        self.assertEqual(restored.channels[RAW_CHANNEL].received_monotonic,source["host_monotonic"])
                        self.assertEqual(len(source["sha256"]),64)
                finally: result = manager.stop()
                self.assertTrue(result["finalization_complete"],result)
                self.assertTrue(result["capture_saved"],result)
                with manager.raw_path.open(encoding="gb18030",newline="") as stream: rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows),result["sample_count"])
                self.assertEqual(len(rows),len(saved[-1]))
                self.assertTrue(all(RAW_CHANNEL in row and "薄膜压力" not in row for row in rows))
                evidence = manager.driver.interface_quality_metadata["film"]
                self.assertEqual(evidence["valid_frames"],evidence["saved_source_rows"])
                self.assertEqual(evidence["writer_error"],"")
                summary = json.loads(Path(evidence["raw_metadata_file"]).read_text(encoding="utf-8"))
                self.assertEqual(summary["saved_source_rows"],evidence["valid_frames"])
                with matrix_csv_path(manager.raw_path).open(encoding="utf-8-sig", newline="") as stream:
                    matrix_rows = list(csv.DictReader(stream))
                self.assertEqual(len(matrix_rows), evidence["valid_frames"])
                self.assertEqual(evidence["saved_matrix_csv_rows"], evidence["valid_frames"])
                self.assertTrue(all(row["capture_uuid"] == manager.capture_uuid and row["main_csv"] == manager.raw_path.name for row in matrix_rows))
                self.assertFalse(manager.matrix_work_path.exists())

    def test_local_adapter_uses_source_record_and_does_not_publish_empty_reads(self):
        from local_direct_acquisition import LocalDirectAdapter
        from real_acquisition import ChannelSampleCache
        driver, serial = driver_double()
        driver.open()
        serial.data = ZERO_FRAME+ZERO_FRAME
        cache = ChannelSampleCache()
        adapter = LocalDirectAdapter("film",driver,[RAW_CHANNEL],cache)
        records = driver.read_samples()
        for payload,record in records:self.assertTrue(adapter._publish(payload,record))
        observed = adapter.last_sample_monotonic
        self.assertEqual(adapter.source_sequence,2)
        self.assertEqual(driver.read_samples(),[])
        self.assertEqual(adapter.last_sample_monotonic,observed)
        driver.close()

    def test_raw_sum_preserves_low_and_negative_values(self):
        driver, serial = driver_double(matrix_rows=2, matrix_cols=2)
        driver.open()
        serial.data = b"[[1,-2],[3,20]]\n"
        self.assertEqual(driver.read_sample(), {RAW_CHANNEL:22})
        self.assertEqual(driver.last_record["matrix"], [[1,-2],[3,20]])
        driver.close()

    def test_calibration_requires_binding_shape_range_and_evidence(self):
        driver, serial = driver_double()
        binding = driver.binding
        profile = dict(schema="m3232_force_calibration_v1", formula="linear_total", unit="N", matrix_shape=[32,32],
                       device_identity=binding, evidence="TEST ONLY synthetic coefficients; no physical accuracy claim", version="test",
                       scale=2.0, zero_total=0, raw_min=0, raw_max=100)
        self.assertTrue(load_calibration(profile,binding,32,32)["digest"])
        with self.assertRaises(ValueError): load_calibration(profile,binding,2,2)
        with self.assertRaises(ValueError): load_calibration(profile,{},32,32)
        profile["scale"] = math.inf
        with self.assertRaises(ValueError): load_calibration(profile,binding,32,32)

    def test_generated_full_channel_csv_is_readable_by_simulator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "thermocouple64"
            generate_thermocouple64_simulation(output)
            path = output / "quick_240.csv"
            driver = SimulatorDriver(path, CURRENT_COLLECTION_SENSOR_COLUMNS)
            try:
                driver.open()
                sample = driver.read_sample()
                self.assertIsNotNone(sample)
                self.assertEqual(set(CURRENT_COLLECTION_SENSOR_COLUMNS), set(sample))
            finally:
                driver.close()

    def test_m3232_precedes_esr1001_in_six_interface_catalog(self) -> None:
        interfaces = default_capture_interfaces()
        self.assertEqual(len(interfaces), 6)
        pressure = interfaces[4]
        self.assertEqual(pressure["id"], "m3232_pressure")
        self.assertEqual(pressure["role"], "pressure")
        self.assertEqual(pressure["driver"], "m3232_pressure")
        self.assertEqual(pressure["channels"], [RAW_CHANNEL])
        self.assertEqual(pressure["endpoint"], "")

    def test_process_pressure_and_film_pressure_route_to_different_interfaces(self) -> None:
        interfaces = default_capture_interfaces()
        assignments = _resolve_interface_channel_assignments(
            interfaces,
            requested=None,
            capture_sensors=["压力", RAW_CHANNEL],
        )
        self.assertEqual(assignments["plc_process"], ["压力"])
        self.assertEqual(assignments["m3232_pressure"], [RAW_CHANNEL])


class EmptyCancelledMatrixTests(unittest.TestCase):
    def test_failed_serial_release_cannot_prove_empty_cancellation(self):
        driver, serial = driver_double()
        driver.open()
        with patch.object(serial, "close", side_effect=OSError("close_failed")):
            with self.assertRaisesRegex(OSError, "close_failed"):
                driver.close()
        self.assertIsNone(driver._close_receive_pending)

    def fixture(self, root):
        context = dict(capture_uuid="capture", capture_generation="a" * 32, run_id="capture-L1",
                       layer_id=1, main_csv="main.csv", interface_id="film")
        target = matrix_csv_path(Path(root) / "main.csv")
        recorder = MatrixRecorder(root, context, csv_path=target.with_name(target.name + ".partial"))
        driver = types.SimpleNamespace(serial=None, _close_receive_pending=0, total_bytes=0,
                                       total_frames=0, parser=MatrixParser(), stop_error="")
        return driver, recorder, context

    def test_confirmed_empty_file_is_preserved_and_does_not_block_reopened_manager(self):
        from acquisition import AcquisitionManager, scan_persistence_recovery
        from m3232_serial import archive_empty_cancelled_matrix
        with tempfile.TemporaryDirectory() as root:
            driver, recorder, context = self.fixture(root)
            recorder.close({})
            original = recorder.csv_path.read_bytes()
            self.assertTrue(scan_persistence_recovery(root)["blocked"])
            result = archive_empty_cancelled_matrix(driver, recorder, session_dir=root, context=context)
            self.assertEqual(Path(result["archived_path"]).read_bytes(), original)
            self.assertEqual(result["sha256"], hashlib.sha256(original).hexdigest())
            self.assertFalse(recorder.csv_path.exists())
            reopened = AcquisitionManager(Path(root))
            self.assertFalse(reopened.recovery_state["blocked"])
            self.assertEqual(reopened.capture_phase, "idle")
            other = Path(root) / "other.csv.partial"
            other.write_text("real,data\n1,2\n")
            self.assertTrue(scan_persistence_recovery(root)["blocked"])
            self.assertEqual(other.read_text(), "real,data\n1,2\n")

    def test_uncertain_or_nonempty_files_remain_recovery_blockers(self):
        from acquisition import scan_persistence_recovery
        from m3232_serial import archive_empty_cancelled_matrix
        for case in ["live_writer", "received_bytes", "frame", "half_frame", "pending_receive",
                     "unknown_receive", "writer_error", "stop_error", "raw_receive", "malformed_header",
                     "csv_data", "wrong_owner"]:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
                driver, recorder, context = self.fixture(root)
                try:
                    if case == "raw_receive":
                        recorder.submit(dict(kind="serial", direction="rx", raw_hex="5b5b30"))
                    if case == "frame":
                        recorder.submit(dict(kind="matrix", matrix=ZERO_MATRIX, host_wall_time=1700000000,
                                             receive_sequence=1))
                    if case != "live_writer":
                        recorder.close({})
                    if case == "received_bytes": driver.total_bytes = 1
                    if case == "half_frame": driver.parser.buffer.extend(b"[[0")
                    if case == "pending_receive": driver._close_receive_pending = 1
                    if case == "unknown_receive": driver._close_receive_pending = None
                    if case == "writer_error": recorder.error = "disk_full"
                    if case == "stop_error": driver.stop_error = "release_failed"
                    if case == "wrong_owner": context = {**context, "capture_generation": "b" * 32}
                    if case == "malformed_header": recorder.csv_path.write_text("R01C01\n", encoding="utf8")
                    if case == "csv_data":
                        with recorder.csv_path.open("a", encoding="utf-8", newline="") as stream:
                            csv.writer(stream).writerow(["0"] * 1041)
                    before = recorder.csv_path.read_bytes()
                    self.assertIsNone(archive_empty_cancelled_matrix(driver, recorder, session_dir=root, context=context))
                    self.assertEqual(recorder.csv_path.read_bytes(), before)
                    self.assertTrue(scan_persistence_recovery(root)["blocked"])
                finally:
                    recorder.close({})

    def test_only_matching_cancelled_start_with_no_samples_can_archive(self):
        import acquisition as a
        for blocked in [None, "samples", "generation", "not_cancelled"]:
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as root:
                driver, recorder, context = self.fixture(root)
                recorder.close({})
                manager = a.AcquisitionManager(Path(root), recovery_scan=False)
                config = types.SimpleNamespace(capture_generation=context["capture_generation"],
                    run_id=context["run_id"], layer=0,
                    interfaces=[dict(id="film", enabled=True, driver="m3232_pressure")])
                manager.config, manager.capture_uuid = config, context["capture_uuid"]
                manager.session_dir, manager.raw_path = Path(root), Path(root) / context["main_csv"]
                manager.driver = object.__new__(a.MultiInterfaceDriver)
                manager.driver._closed_matrix_sources = {"film": ("film", driver, recorder)}
                manager._start_thread = threading.current_thread()
                manager._start_cancel.set()
                manager._start_job = dict(state="cancelling", capture_generation=context["capture_generation"])
                if blocked == "samples": manager.total_sample_count = 1
                if blocked == "generation": manager._start_job["capture_generation"] = "b" * 32
                if blocked == "not_cancelled": manager._start_cancel.clear()
                manager._archive_cancelled_empty_matrix_files(config)
                self.assertEqual(recorder.csv_path.exists(), blocked is not None)

    def test_archive_failure_restores_original_recovery_blocker(self):
        from acquisition import scan_persistence_recovery
        from m3232_serial import archive_empty_cancelled_matrix
        with tempfile.TemporaryDirectory() as root:
            driver, recorder, context = self.fixture(root)
            recorder.close({})
            before = recorder.csv_path.read_bytes()
            replace = os.replace
            def fail_audit(source, destination):
                if Path(destination).name == "cancelled_empty_matrix.json":
                    raise OSError("disk_full")
                return replace(source, destination)
            with patch("m3232_serial.os.replace", side_effect=fail_audit):
                with self.assertRaisesRegex(OSError, "disk_full"):
                    archive_empty_cancelled_matrix(driver, recorder, session_dir=root, context=context)
            self.assertEqual(recorder.csv_path.read_bytes(), before)
            self.assertTrue(scan_persistence_recovery(root)["blocked"])

    def test_cancel_during_device_open_archives_only_its_empty_matrix(self):
        import acquisition as a
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = a.AcquisitionConfig(acquisition_mode="real", processing_mode="capture_only",
                dataset_schema="new_collection_v13_0", selected_sensors=[RAW_CHANNEL], save_root=root,
                interfaces=[dict(id="film", role="pressure", driver="m3232_pressure", endpoint="COM_TEST",
                    device_identity=dict(confirmed=True, endpoint="COM_TEST", location="fixture-only"),
                    physical_interface_id="serial:COM_TEST", physical_verified=True)])
            entered = threading.Event()
            records = []
            def build(current):
                item = current.interfaces[0]
                context = {**item["_matrix_csv_context"], "interface_id": "film"}
                recorder = MatrixRecorder(item["_raw_capture_root"], context,
                                          csv_path=item["_matrix_csv_work_path"])
                recorder.close({})
                closed = types.SimpleNamespace(serial=None, _close_receive_pending=0, total_bytes=0,
                    total_frames=0, parser=MatrixParser(), stop_error="")
                multi = a.MultiInterfaceDriver([], [RAW_CHANNEL])
                multi._closed_matrix_sources = {"film": ("film", closed, recorder)}
                def opening():
                    entered.set()
                    manager._start_cancel.wait(3)
                    raise RuntimeError("capture_start_cancelled")
                multi.open = opening
                records.append(recorder)
                return multi
            with patch("acquisition.build_driver", side_effect=build):
                manager.start_async(cfg)
                self.assertTrue(entered.wait(2))
                manager.cancel_start()
                manager._start_thread.join(3)
            self.assertFalse(manager._start_thread.is_alive())
            self.assertEqual(manager.start_operation_status()["state"], "cancelled")
            self.assertFalse(records[0].csv_path.exists())
            self.assertFalse(a.AcquisitionManager(Path(root)).recovery_state["blocked"])


if __name__ == "__main__":
    unittest.main()
