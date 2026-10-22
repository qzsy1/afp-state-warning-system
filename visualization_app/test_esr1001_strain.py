from __future__ import annotations

import math
import struct
import sys
import json
import tempfile
import time
import unittest
import csv
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acquisition_clock import monotonic

import acquisition  # noqa: E402
from esr1001_fixtures import (  # noqa: E402
    ENABLE_FOUR_CHANNELS_RESPONSE,
    ILLEGAL_ADDRESS_RESPONSE,
    MANUAL_RESPONSE,
    READ_REQUEST,
    START_RESPONSE,
    STOP_RESPONSE,
)


class FakeSerial:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.writes: list[bytes] = []
        self.closed = False
        self.reset_calls = 0

    def write(self, payload: bytes) -> int:
        self.writes.append(bytes(payload))
        return len(payload)

    def flush(self) -> None:
        return

    def reset_input_buffer(self) -> None:
        self.reset_calls += 1

    def read(self, size: int = 1) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) <= size:
            return chunk
        self.chunks.insert(0, chunk[size:])
        return chunk[:size]

    def close(self) -> None:
        self.closed = True


class Esr1001ProtocolTests(unittest.TestCase):
    def test_protocol_fixtures_have_valid_lengths_and_crc(self) -> None:
        fixtures = [
            READ_REQUEST,
            MANUAL_RESPONSE,
            ENABLE_FOUR_CHANNELS_RESPONSE,
            START_RESPONSE,
            STOP_RESPONSE,
            ILLEGAL_ADDRESS_RESPONSE,
        ]
        self.assertEqual(len(READ_REQUEST), 8)
        self.assertEqual(len(MANUAL_RESPONSE), 21)
        for frame in fixtures:
            self.assertTrue(acquisition.esr1001_crc_valid(frame), frame.hex(" "))
        self.assertEqual(READ_REQUEST, acquisition.esr1001_read_request())

    def test_crc16_modbus_golden_vector_is_low_byte_first(self) -> None:
        payload = bytes.fromhex("01 04 00 30 00 08")
        self.assertEqual(acquisition.crc16_modbus(payload), 0xC3F1)
        self.assertEqual(acquisition.esr1001_append_crc(payload), READ_REQUEST)

    def test_manual_frame_maps_device_order_atomically(self) -> None:
        values = acquisition.parse_esr1001_read_response(MANUAL_RESPONSE, slave_id=1)
        self.assertEqual(list(values), ["应变1", "应变2", "应变3", "应变4"])
        expected = [32023678.0, 32023326.0, 32023672.0, 602.7048950195312]
        for actual, wanted in zip(values.values(), expected):
            self.assertAlmostEqual(actual, wanted, places=4)

    def test_read_response_rejects_identity_length_crc_and_non_finite_values(self) -> None:
        invalid_frames = []
        wrong_slave = bytearray(MANUAL_RESPONSE)
        wrong_slave[0] = 2
        invalid_frames.append(bytes(wrong_slave))
        wrong_function = bytearray(MANUAL_RESPONSE)
        wrong_function[1] = 3
        invalid_frames.append(bytes(wrong_function))
        wrong_count = bytearray(MANUAL_RESPONSE)
        wrong_count[2] = 14
        invalid_frames.append(bytes(wrong_count))
        wrong_crc = bytearray(MANUAL_RESPONSE)
        wrong_crc[-2] ^= 0x01
        invalid_frames.append(bytes(wrong_crc))
        for frame in invalid_frames:
            with self.assertRaises(acquisition.Esr1001ProtocolError):
                acquisition.parse_esr1001_read_response(frame, slave_id=1)

        payload = bytearray(MANUAL_RESPONSE[:-2])
        payload[3:7] = struct.pack(">f", math.inf)
        non_finite = acquisition.esr1001_append_crc(bytes(payload))
        with self.assertRaisesRegex(acquisition.Esr1001ProtocolError, "non_finite"):
            acquisition.parse_esr1001_read_response(non_finite, slave_id=1)

    def test_exception_preserves_function_and_code_and_illegal_address_is_firmware_gate(self) -> None:
        with self.assertRaises(acquisition.Esr1001FirmwareIncompatibleError) as caught:
            acquisition.parse_esr1001_read_response(ILLEGAL_ADDRESS_RESPONSE, slave_id=1)
        self.assertEqual(caught.exception.function_code, 0x84)
        self.assertEqual(caught.exception.exception_code, 0x02)

    def test_transaction_reads_fragmented_frame_and_ignores_leading_residue(self) -> None:
        serial_port = FakeSerial([b"\x99", MANUAL_RESPONSE[:2], MANUAL_RESPONSE[2:8], MANUAL_RESPONSE[8:]])
        driver = acquisition.Esr1001StrainDriver("COM7", serial_factory=lambda **_: serial_port)
        driver.serial = serial_port
        with patch("acquisition.monotonic", side_effect=[0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06]):
            frame = driver._transact(READ_REQUEST, expected_function=0x04, deadline_seconds=0.5)
        self.assertEqual(frame, MANUAL_RESPONSE)
        self.assertEqual(serial_port.writes, [READ_REQUEST])

    def test_transaction_keeps_only_one_frame_from_concatenated_input(self) -> None:
        serial_port = FakeSerial([MANUAL_RESPONSE + MANUAL_RESPONSE])
        driver = acquisition.Esr1001StrainDriver("COM7", serial_factory=lambda **_: serial_port)
        driver.serial = serial_port
        first = driver._transact(READ_REQUEST, expected_function=0x04, deadline_seconds=0.5)
        driver._reset_before_request = False
        second = driver._read_rtu_frame(expected_function=0x04, deadline_seconds=0.5)
        self.assertEqual(first, MANUAL_RESPONSE)
        self.assertEqual(second, MANUAL_RESPONSE)

    def test_transaction_timeout_is_classified(self) -> None:
        serial_port = FakeSerial([])
        driver = acquisition.Esr1001StrainDriver("COM7", serial_factory=lambda **_: serial_port)
        driver.serial = serial_port
        with self.assertRaisesRegex(acquisition.Esr1001ProtocolError, "timeout"):
            driver._read_rtu_frame(expected_function=0x04, deadline_seconds=0.001)

    def test_open_and_close_only_write_channel_count_and_start_stop(self) -> None:
        serial_port = FakeSerial([
            MANUAL_RESPONSE,
            ENABLE_FOUR_CHANNELS_RESPONSE,
            START_RESPONSE,
            STOP_RESPONSE,
        ])
        serial_factory_calls: list[dict[str, object]] = []

        def factory(**kwargs):
            serial_factory_calls.append(kwargs)
            return serial_port

        driver = acquisition.Esr1001StrainDriver("COM7", serial_factory=factory)
        driver.open()
        driver.close()
        self.assertEqual(serial_factory_calls[0]["port"], "COM7")
        self.assertEqual(serial_factory_calls[0]["baudrate"], 9600)
        self.assertEqual(serial_port.writes[0], READ_REQUEST)
        registers = {int.from_bytes(frame[2:4], "big") for frame in serial_port.writes[1:]}
        self.assertEqual(registers, {0x0015, 0x0009})
        self.assertNotIn(0x0013, registers)


class Esr1001CatalogTests(unittest.TestCase):
    def test_new_schema_has_six_interfaces_and_twenty_one_unique_channels(self) -> None:
        old_sensors = acquisition.ACQUISITION_SCHEMAS["new_collection_v11_3"]["sensors"]
        new_sensors = acquisition.ACQUISITION_SCHEMAS["new_collection_v12_0"]["sensors"]
        self.assertEqual(len(old_sensors), 17)
        self.assertEqual(len(new_sensors), 21)
        self.assertEqual(new_sensors[-4:], ["应变1", "应变2", "应变3", "应变4"])
        interfaces = acquisition.default_capture_interfaces()
        self.assertEqual(len(interfaces), 6)
        config = acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v12_0",
            simulation_source_path="simulation.csv",
            interfaces=interfaces,
            selected_sensors=list(new_sensors),
        )
        self.assertEqual(config.interface_channel_assignments["esr1001_strain"], new_sensors[-4:])
        owners = [
            interface_id
            for interface_id, channels in config.interface_channel_assignments.items()
            if "应变1" in channels
        ]
        self.assertEqual(owners, ["esr1001_strain"])

    def test_strain_metadata_uses_microstrain_and_never_invents_stress(self) -> None:
        for name in ["应变1", "应变2", "应变3", "应变4"]:
            metadata = acquisition.SENSOR_CHANNEL_METADATA[name]
            self.assertEqual(metadata["unit"], "με")
            self.assertEqual(metadata["dtype"], "float32")
            self.assertIn("ESR1001-A", metadata["source"])
        self.assertFalse(any("应力" in name for name in acquisition.ALL_SENSOR_COLUMNS))

    def test_driver_factory_polling_and_freshness_register_esr1001(self) -> None:
        interface = next(
            item
            for item in acquisition.default_capture_interfaces()
            if item["id"] == "esr1001_strain"
        )
        self.assertEqual(interface["endpoint"], "")
        self.assertEqual(interface["baudrate"], 9600)
        self.assertEqual(interface["freshness_seconds"], 0.25)
        from local_direct_acquisition import minimum_poll_interval

        self.assertGreaterEqual(minimum_poll_interval("modbus_rtu_esr1001"), 0.05)

    def test_four_channels_publish_with_one_observation_time_and_sequence(self) -> None:
        from local_direct_acquisition import LocalDirectAdapter

        cache = acquisition.ChannelSampleCache()
        adapter = LocalDirectAdapter(
            "esr1001_strain",
            object(),
            acquisition.STRAIN_SENSOR_COLUMNS,
            cache,
            clock=lambda: 12.5,
            wall_clock=lambda: 1000.25,
        )
        values = acquisition.parse_esr1001_read_response(MANUAL_RESPONSE)
        self.assertTrue(adapter._publish(values))
        samples = [cache.latest(name) for name in acquisition.STRAIN_SENSOR_COLUMNS]
        self.assertTrue(all(sample is not None for sample in samples))
        self.assertEqual({sample.received_monotonic for sample in samples}, {12.5})
        self.assertEqual({sample.received_wall_time for sample in samples}, {1000.25})
        self.assertEqual({sample.source_sequence for sample in samples}, {1})

    def test_esr1001_timeout_does_not_block_another_interface_worker(self) -> None:
        from local_direct_acquisition import LocalDirectAdapter

        class TimedOutDriver:
            def open(self):
                return

            def read_sample(self):
                raise acquisition.Esr1001ProtocolError("timeout", "no response")

            def close(self):
                return

        class HealthyDriver:
            def open(self):
                return

            def read_sample(self):
                return {"温度": 123.5}

            def close(self):
                return

        cache = acquisition.ChannelSampleCache()
        failed = LocalDirectAdapter(
            "esr1001_strain", TimedOutDriver(), acquisition.STRAIN_SENSOR_COLUMNS,
            cache, max_reconnect_attempts=0,
        )
        healthy = LocalDirectAdapter(
            "plc_process", HealthyDriver(), ["温度"], cache,
            poll_interval_seconds=0.001, minimum_poll_seconds=0.001,
        )
        failed.start()
        healthy.start()
        deadline = time.time() + 0.5
        while cache.latest("温度") is None and time.time() < deadline:
            time.sleep(0.005)
        failed.stop()
        healthy.stop()
        self.assertIsNotNone(cache.latest("温度"))
        self.assertEqual(failed.state, "failed")
        self.assertIn("timeout", failed.last_error)

    def test_twenty_one_collected_channels_keep_sixteen_input_model_boundary(self) -> None:
        import online_inference

        interfaces = acquisition.default_capture_interfaces()
        config = acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v12_0",
            simulation_source_path="simulation.csv",
            interfaces=interfaces,
            selected_sensors=acquisition.LATEST_COLLECTION_SENSOR_COLUMNS.copy(),
            model_input_sensors=online_inference.NEW_MODEL_SENSOR_COLUMNS.copy(),
            model_output_sensors=online_inference.NEW_MODEL_SENSOR_COLUMNS.copy(),
        )
        self.assertEqual(len(config.selected_sensors), 21)
        self.assertEqual(config.model_input_sensors, online_inference.NEW_MODEL_SENSOR_COLUMNS)
        self.assertEqual(len(config.model_input_sensors), 16)
        self.assertFalse(set(acquisition.STRAIN_SENSOR_COLUMNS) & set(config.model_input_sensors))

    def test_model_with_strain_requires_matching_dimensions_and_scaler(self) -> None:
        import online_inference

        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "checkpoint.pth"
            checkpoint.write_bytes(b"metadata-contract-only")
            inputs = [*online_inference.NEW_MODEL_SENSOR_COLUMNS, "应变1"]
            metadata = {
                "architecture": "I-ModernTCN",
                "model_type": "i_T_G",
                "enc_in": len(inputs),
                "seq_len": 24,
                "pred_len": 24,
                "input_sensors": inputs,
                "output_sensors": inputs,
                "model_columns": inputs,
                "scaler_mean": [0.0] * 16,
                "scaler_scale": [1.0] * 16,
            }
            checkpoint.with_suffix(".json").write_text(
                json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
            )
            with patch.object(
                online_inference, "classify_model_source", return_value="external"
            ), patch.object(online_inference, "_remember_model_selection"):
                with self.assertRaisesRegex(ValueError, "标准化参数长度"):
                    online_inference.inspect_prediction_model(
                        checkpoint,
                        model_type="i_T_G",
                        schema_mode="new_collection_v12_0",
                    )
                metadata["scaler_mean"] = [0.0] * len(inputs)
                metadata["scaler_scale"] = [1.0] * len(inputs)
                checkpoint.with_suffix(".json").write_text(
                    json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
                )
                profile = online_inference.inspect_prediction_model(
                    checkpoint,
                    model_type="i_T_G",
                    schema_mode="new_collection_v12_0",
                )
        self.assertEqual(profile["input_sensors"], inputs)

    def test_new_csv_columns_and_old_replay_contract_remain_distinct(self) -> None:
        new_config = acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v12_0",
            simulation_source_path="simulation.csv",
            interfaces=acquisition.default_capture_interfaces(),
            selected_sensors=acquisition.LATEST_COLLECTION_SENSOR_COLUMNS.copy(),
            processing_mode="capture_only",
        )
        self.assertEqual(
            [name for name in new_config.raw_columns if name.startswith("应变")],
            acquisition.STRAIN_SENSOR_COLUMNS,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_path = root / "old.csv"
            new_path = root / "new.csv"
            for path, columns in (
                (old_path, acquisition.NEW_COLLECTION_SENSOR_COLUMNS),
                (new_path, acquisition.LATEST_COLLECTION_SENSOR_COLUMNS),
            ):
                with path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=columns)
                    writer.writeheader()
                    writer.writerow({name: index + 0.5 for index, name in enumerate(columns)})
            old_driver = acquisition.SimulatorDriver(
                old_path, acquisition.NEW_COLLECTION_SENSOR_COLUMNS
            )
            old_driver.open()
            old_sample = old_driver.read_sample()
            old_driver.close()
            new_driver = acquisition.SimulatorDriver(
                new_path, acquisition.LATEST_COLLECTION_SENSOR_COLUMNS
            )
            new_driver.open()
            new_sample = new_driver.read_sample()
            new_driver.close()
        self.assertFalse(set(acquisition.STRAIN_SENSOR_COLUMNS) & set(old_sample))
        self.assertTrue(set(acquisition.STRAIN_SENSOR_COLUMNS) <= set(new_sample))

    def test_mysql_sensor_json_uses_new_schema_keys_in_both_sample_tables(self) -> None:
        from mysql_storage import MySQLCaptureStore, MySQLSettings

        class Cursor:
            def __init__(self):
                self.executed = []

            def execute(self, sql, parameters=None):
                self.executed.append((str(sql), parameters))

            def executemany(self, sql, values):
                self.executed.append((str(sql), tuple(values)))

            def close(self):
                return

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                return

            def rollback(self):
                return

            def close(self):
                return

        config = acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v12_0",
            simulation_source_path="simulation.csv",
            interfaces=acquisition.default_capture_interfaces(),
            selected_sensors=acquisition.LATEST_COLLECTION_SENSOR_COLUMNS.copy(),
            processing_mode="capture_only",
            capture_uuid="esr1001-contract",
        )
        row = {name: float(index) for index, name in enumerate(config.schema_sensors)}
        connection = Connection()
        store = MySQLCaptureStore(MySQLSettings(enabled=True, database="afp_test"))
        with patch.object(store, "_connect", return_value=("fake", connection)):
            result = store.save_layer(
                config,
                rows=[row],
                layer_file=None,
                full_specimen_file=None,
                timestamp_file=None,
                folder_path=None,
                summary={},
            )
        self.assertTrue(result["ok"], result)
        batches = [
            values
            for sql, values in connection.cursor_instance.executed
            if "INSERT INTO AFP_SENSOR_SAMPLE" in sql.upper()
            or "INSERT INTO AFP_SAMPLE_ALL" in sql.upper()
        ]
        self.assertEqual(len(batches), 2)
        payloads = [json.loads(batch[0][5]) for batch in batches]
        for payload in payloads:
            self.assertEqual(
                {name: payload[name] for name in acquisition.STRAIN_SENSOR_COLUMNS},
                {name: row[name] for name in acquisition.STRAIN_SENSOR_COLUMNS},
            )
        self.assertEqual(payloads[0], payloads[1])

        statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed)
        self.assertNotIn("ALTER TABLE", statements.upper())
        self.assertIn("ON DUPLICATE KEY UPDATE", statements.upper())

    def test_quality_summary_preserves_valid_rows_and_records_strain_outages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = acquisition.AcquisitionManager(Path(temporary), recovery_scan=False)
            manager.config = acquisition.AcquisitionConfig(
                acquisition_mode="simulation",
                dataset_schema="new_collection_v12_0",
                simulation_source_path="simulation.csv",
                interfaces=acquisition.default_capture_interfaces(),
                selected_sensors=acquisition.LATEST_COLLECTION_SENSOR_COLUMNS.copy(),
                processing_mode="capture_only",
            )
            manager.total_sample_count = 4
            manager.timestamps.extend([1000.0, 1000.1, 1000.2, 1000.3])
            manager.sensor_received.update({name: 2 for name in acquisition.STRAIN_SENSOR_COLUMNS})
            manager.sensor_last_time.update({name: 1000.3 for name in acquisition.STRAIN_SENSOR_COLUMNS})
            measured = {name: "measured_new" for name in acquisition.STRAIN_SENSOR_COLUMNS}
            missing = {name: "missing" for name in acquisition.STRAIN_SENSOR_COLUMNS}
            manager.frame_quality.extend([measured, missing, missing, measured])
            driver = object.__new__(acquisition.MultiInterfaceDriver)
            driver.interface_quality_metadata = {
                "esr1001_strain": {
                    "protocol_error_counts": {"timeout": 1, "crc": 1},
                    "last_error": "crc: invalid",
                }
            }
            manager.driver = driver

            summary = manager._data_quality_summary()

        strain = summary["esr1001_strain"]
        self.assertEqual(strain["observation_counts"]["应变1"], 2)
        self.assertEqual(strain["last_valid_time"]["应变4"], 1000.3)
        self.assertEqual(
            strain["missing_or_stale_intervals"]["应变1"],
            [{"start_frame": 1, "end_frame": 2}],
        )
        self.assertEqual(strain["protocol_error_counts"], {"timeout": 1, "crc": 1})
        self.assertEqual(summary["sample_count"], 4)

    def test_layer_and_whole_specimen_csv_keep_one_row_per_frame_and_blank_missing_strain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.csv"
            columns = ["时间", *acquisition.LATEST_COLLECTION_SENSOR_COLUMNS]
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerow({"时间": 0.0, **{name: float(index) for index, name in enumerate(acquisition.LATEST_COLLECTION_SENSOR_COLUMNS)}})
                writer.writerow({"时间": 0.1, **{name: float(index + 100) for index, name in enumerate(acquisition.NEW_COLLECTION_SENSOR_COLUMNS)}})
            config = acquisition.AcquisitionConfig(
                acquisition_mode="simulation",
                dataset_schema="new_collection_v12_0",
                simulation_source_type="single_csv",
                simulation_source_path=str(source),
                source_file=str(source),
                interfaces=acquisition.default_capture_interfaces(),
                selected_sensors=acquisition.LATEST_COLLECTION_SENSOR_COLUMNS.copy(),
                processing_mode="capture_only",
                save_root=str(root / "capture"),
                sample_rate_hz=100.0,
                specimen_id="S1", condition_id="C1", run_id="R1",
            )
            manager = acquisition.AcquisitionManager(root / "capture", recovery_scan=False)
            manager.start(config)
            deadline = time.time() + 2.0
            while manager.total_sample_count < 4 and time.time() < deadline:
                time.sleep(0.01)
            result = manager.stop()
            self.assertTrue(result["finalization_complete"], result)
            layer_rows = manager._read_layer_rows(Path(result["layer_file"]))
            whole_rows = manager._read_layer_rows(Path(result["full_specimen_file"]))

        self.assertEqual(len(layer_rows), result["sample_count"])
        self.assertEqual(layer_rows, whole_rows)
        strain_header = [name for name in layer_rows[0] if name.startswith("应变")]
        self.assertEqual(strain_header, acquisition.STRAIN_SENSOR_COLUMNS)
        self.assertTrue(any(row["应变1"] == "" for row in layer_rows))
        self.assertFalse(any(row["应变1"] == "0" for row in layer_rows))

    def test_flat_export_expands_strain_only_for_requested_schema(self) -> None:
        from mysql_storage import FLAT_EXPORT_COLUMNS, MySQLCaptureStore, MySQLSettings

        class Cursor:
            def __init__(self, schema_id: str):
                self.schema_id = schema_id
                self.rows = []
                self.executed = []

            def execute(self, sql, parameters=None):
                self.executed.append((str(sql), parameters))
                sensor = {"温度": 1.0}
                if self.schema_id == "new_collection_v12_0":
                    sensor.update({name: float(index) for index, name in enumerate(acquisition.STRAIN_SENSOR_COLUMNS, 1)})
                if "SELECT process_json, sensor_json" in str(sql):
                    self.rows = [("{}", json.dumps(sensor, ensure_ascii=False))]
                else:
                    source = {
                        "condition_id": "C1", "schema_id": self.schema_id,
                        "specimen_key": "C1#1#S1", "specimen_id": "S1",
                        "replicate_no": 1, "run_id": "R1", "layer_no": 1,
                        "sample_index": 0, "timestamp_iso": "2026-01-01T00:00:00",
                        "timestamp_unix": 1.0,
                        "sensor_json": json.dumps(sensor, ensure_ascii=False),
                        "process_json": "{}", "saved_at": "2026-01-01 00:00:00",
                    }
                    self.rows = [tuple(source.get(name) for name in FLAT_EXPORT_COLUMNS)]

            def fetchmany(self, _size):
                rows, self.rows = self.rows, []
                return rows

            def close(self):
                return

        class Connection:
            def __init__(self, cursor):
                self.cursor_instance = cursor

            def cursor(self):
                return self.cursor_instance

            def close(self):
                return

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            headers = {}
            for schema_id in ("new_collection_v11_3", "new_collection_v12_0"):
                cursor = Cursor(schema_id)
                store = MySQLCaptureStore(MySQLSettings(enabled=True, database="afp_test"))
                with patch.object(store, "_connect", return_value=("fake", Connection(cursor))):
                    result = store.export_flat_csv(root / f"{schema_id}.csv", schema_id=schema_id)
                self.assertTrue(result["ok"], result)
                with (root / f"{schema_id}.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                    headers[schema_id] = next(csv.reader(handle))
                self.assertTrue(all(parameters == (schema_id,) for _, parameters in cursor.executed))

        self.assertFalse(set(acquisition.STRAIN_SENSOR_COLUMNS) & set(headers["new_collection_v11_3"]))
        self.assertTrue(set(acquisition.STRAIN_SENSOR_COLUMNS) <= set(headers["new_collection_v12_0"]))

    def test_pending_mysql_retry_preserves_v12_schema_and_strain_values(self) -> None:
        from mysql_storage import MySQLSettings

        class FakeStore:
            calls = []

            def __init__(self, settings):
                self.settings = settings

            def test_connection(self):
                return {"ok": True}

            def save_layer(self, config, **kwargs):
                type(self).calls.append((config.dataset_schema, kwargs["rows"]))
                return {"ok": True, "saved_rows": len(kwargs["rows"])}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "layer.csv"
            row = {name: float(index + 1) for index, name in enumerate(acquisition.LATEST_COLLECTION_SENSOR_COLUMNS)}
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=acquisition.LATEST_COLLECTION_SENSOR_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            config = acquisition.AcquisitionConfig(
                dataset_schema="new_collection_v12_0",
                processing_mode="capture_only",
                acquisition_mode="simulation",
                simulation_source_path=str(source),
                source_file=str(source),
                interfaces=acquisition.default_capture_interfaces(),
                selected_sensors=acquisition.LATEST_COLLECTION_SENSOR_COLUMNS.copy(),
                capture_uuid="AFP-esr-retry",
                mysql_enabled=True,
                mysql_database="afp_test",
            )
            pending = root / "esr_mysql_pending.json"
            pending.write_text(json.dumps({
                "config": acquisition.AcquisitionManager._public_config(config),
                "layer_file": str(source), "folder_path": str(root), "summary": {},
            }, ensure_ascii=False), encoding="utf-8")
            manager = acquisition.AcquisitionManager(root / "capture", recovery_scan=False)
            with patch("acquisition.MySQLCaptureStore", FakeStore):
                result = manager._retry_pending_mysql(
                    MySQLSettings(enabled=True, database="afp_test"), root
                )

        self.assertEqual(result["succeeded"], 1, result)
        schema_id, retried_rows = FakeStore.calls[-1]
        self.assertEqual(schema_id, "new_collection_v12_0")
        self.assertEqual(
            {name: float(retried_rows[0][name]) for name in acquisition.STRAIN_SENSOR_COLUMNS},
            {name: row[name] for name in acquisition.STRAIN_SENSOR_COLUMNS},
        )

    def test_training_json_expansion_preserves_new_keys_without_filling_old_rows(self) -> None:
        import pandas as pd
        import training_data

        frame = pd.DataFrame(
            [
                {"schema_id": "new_collection_v11_3", "sensor_json": '{"温度": 1.0}'},
                {
                    "schema_id": "new_collection_v12_0",
                    "sensor_json": json.dumps(
                        {"温度": 2.0, "应变1": 11.0, "应变2": 12.0},
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        expanded = training_data._expand_json_fields(frame)
        self.assertTrue(math.isnan(expanded.loc[0, "应变1"]))
        self.assertEqual(expanded.loc[1, "应变1"], 11.0)
        self.assertEqual(expanded.loc[1, "应变2"], 12.0)

    def test_same_com_port_is_rejected_before_any_driver_is_built(self) -> None:
        interfaces = [
            {
                "id": "m3232_pressure",
                "role": "pressure",
                "driver": "m3232_pressure",
                "endpoint": "COM7",
                "physical_interface_id": "serial:first",
                "physical_interface_kind": "serial",
            },
            {
                "id": "esr1001_strain",
                "role": "esr1001_strain",
                "driver": "modbus_rtu_esr1001",
                "endpoint": "com7",
                "physical_interface_id": "serial:second",
                "physical_interface_kind": "serial",
            },
        ]
        with patch("acquisition.MultiInterfaceDriver") as driver:
            with self.assertRaisesRegex(ValueError, "独占串口.*重复"):
                acquisition.AcquisitionConfig(
                    acquisition_mode="real",
                    dataset_schema="new_collection_v12_0",
                    interfaces=interfaces,
                    interface_channel_assignments={
                        "m3232_pressure": ["薄膜压力"],
                        "esr1001_strain": acquisition.STRAIN_SENSOR_COLUMNS,
                    },
                    selected_sensors=["薄膜压力", *acquisition.STRAIN_SENSOR_COLUMNS],
                )
        driver.assert_not_called()

    def test_local_agent_projects_six_roles_without_claiming_serial_identity(self) -> None:
        from local_capture_agent import LocalCaptureAgent

        class Manager:
            @staticmethod
            def discover_interfaces():
                return {
                    "physical_interfaces": [
                        {
                            "id": "serial:COM7",
                            "kind": "serial",
                            "protocol": "serial",
                            "endpoint": "COM7",
                            "detected": True,
                            "identity_confirmed": False,
                        },
                        {
                            "id": "serial:COM9",
                            "kind": "serial",
                            "protocol": "serial",
                            "endpoint": "COM9",
                            "detected": True,
                            "identity_confirmed": False,
                        },
                    ],
                    "error": "",
                }

        discovered = LocalCaptureAgent(Manager()).discover()
        by_role = {item["role"]: item for item in discovered["sensor_bindings"]}
        self.assertEqual(len(by_role), 6)
        self.assertIn("esr1001_strain", by_role)
        self.assertEqual(by_role["esr1001_strain"]["protocol"], "modbus_rtu")
        self.assertEqual(by_role["esr1001_strain"]["driver"], "modbus_rtu_esr1001")
        self.assertEqual(by_role["esr1001_strain"]["sensor_data_state"], "unknown")
        self.assertTrue(discovered["capabilities"]["esr1001_modbus_rtu"])

    def test_helper_declares_esr1001_execution_capability(self) -> None:
        import local_capture_helper_entry

        capabilities = local_capture_helper_entry.helper_capabilities()
        self.assertTrue(capabilities["esr1001_modbus_rtu"])
        self.assertIn("esr1001_strain", capabilities["interface_roles"])
        self.assertIn("modbus_rtu_esr1001", capabilities["drivers"])

    def test_web_native_simulation_and_diagnostics_use_runtime_six_by_twenty_one_contract(self) -> None:
        from diagnostic_tools import normalize_no_sensor_states
        from interface_agent import build_agent_events

        app_source = (Path(__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
        native_source = (Path(__file__).resolve().parent / "native_integrated_app.py").read_text(encoding="utf-8")
        self.assertIn('id: "esr1001_strain"', app_source)
        self.assertIn('["modbus_rtu_esr1001", "ESR1001-A Modbus RTU"]', app_source)
        self.assertIn('"new_collection_v12_0"', app_source)
        self.assertIn("default_capture_interfaces()", native_source)
        self.assertIn('"new_collection_v12_0"', native_source)

        simulation_file = Path(__file__).resolve().parent / "simulation_packages" / "afp_synthetic_quick_240.csv"
        with simulation_file.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle))
        self.assertEqual(
            [name for name in header if name in acquisition.LATEST_COLLECTION_SENSOR_COLUMNS],
            acquisition.LATEST_COLLECTION_SENSOR_COLUMNS,
        )

        hardware = {
            "interfaces": [{
                "id": "esr1001_strain", "role": "esr1001_strain",
                "driver": "modbus_rtu_esr1001", "state": "no_response",
                "message": "no_response: timeout", "ok": False,
                "sample_counts": {name: 0 for name in acquisition.STRAIN_SENSOR_COLUMNS},
            }],
            "sensors": [{"name": name, "state": "no_data"} for name in acquisition.STRAIN_SENSOR_COLUMNS],
        }
        normalized = normalize_no_sensor_states(hardware)
        self.assertIn("0/6", normalized["summary"])
        self.assertIn("0/21", normalized["summary"])
        events = build_agent_events(normalized)
        self.assertEqual(events[0]["interface_id"], "esr1001_strain")
        self.assertNotIn(events[0]["state"], {"ok", "healthy"})

    def test_hardware_probe_classifies_esr1001_crc_failure(self) -> None:
        interface = {
            "id": "esr1001_strain",
            "role": "esr1001_strain",
            "driver": "modbus_rtu_esr1001",
            "endpoint": "COM7",
            "physical_interface_id": "serial:COM7",
            "physical_interface_kind": "serial",
            "physical_verified": True,
        }
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            dataset_schema="new_collection_v12_0",
            interfaces=[interface],
            interface_channel_assignments={
                "esr1001_strain": acquisition.STRAIN_SENSOR_COLUMNS
            },
            selected_sensors=acquisition.STRAIN_SENSOR_COLUMNS.copy(),
            processing_mode="capture_only",
        )
        with patch("acquisition.MultiInterfaceDriver") as driver:
            driver.return_value.open.side_effect = RuntimeError(
                "crc: 响应CRC16/MODBUS校验失败"
            )
            result = acquisition.AcquisitionManager().test_connection(
                config, timeout_seconds=0.01
            )
        probe = result["interfaces"][0]
        self.assertEqual(probe["state"], "crc_error")
        self.assertFalse(probe["ok"])
        self.assertIn("ESR1001-A协议探针失败", probe["message"])

    def test_hardware_probe_error_catalog_is_distinguishable(self) -> None:
        cases = {
            "FileNotFoundError: COM7": "port_missing",
            "PermissionError: access is denied": "port_occupied",
            "could not configure port parameters": "serial_parameter_error",
            "timeout: no response": "no_response",
            "wrong_slave: expected 1": "wrong_slave",
            "crc: invalid": "crc_error",
            "length: expected 21": "length_error",
            "modbus_exception: 0x03": "modbus_exception",
            "firmware_incompatible: 0x0030": "firmware_incompatible",
        }
        self.assertEqual(
            {text: acquisition.classify_esr1001_probe_error(text) for text in cases},
            cases,
        )

    def test_esr1001_physical_binding_and_protocol_fields_round_trip(self) -> None:
        interface = {
            "id": "esr1001_strain",
            "role": "esr1001_strain",
            "driver": "modbus_rtu_esr1001",
            "endpoint": "COM9",
            "baudrate": 9600,
            "slave_id": 7,
            "physical_interface_id": "serial:usb-rs485:ABC123",
            "physical_port_id": "usbport:hub1:port4",
            "physical_interface_kind": "serial",
            "physical_verified": True,
        }
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            dataset_schema="new_collection_v12_0",
            interfaces=[interface],
            interface_channel_assignments={
                "esr1001_strain": acquisition.STRAIN_SENSOR_COLUMNS
            },
            selected_sensors=acquisition.STRAIN_SENSOR_COLUMNS.copy(),
            processing_mode="capture_only",
        )
        public = acquisition.AcquisitionManager._public_config(config)
        restored = acquisition.AcquisitionConfig(**public)
        actual = restored.interfaces[0]
        for key in (
            "endpoint", "baudrate", "slave_id", "physical_interface_id",
            "physical_port_id", "physical_interface_kind",
        ):
            self.assertEqual(actual[key], config.interfaces[0][key])

    def test_remote_helper_stream_contains_normalized_strain_and_quality_only(self) -> None:
        from local_capture_agent import LocalCaptureAgent

        class Manager:
            def stream_rows_since(self, cursor, limit):
                return {
                    "rows": [{"应变1": 1.0, "应变2": 2.0, "应变3": 3.0, "应变4": 4.0}],
                    "timestamps": [1000.0],
                    "frames": [
                        {
                            "sequence": 1,
                            "values": {"应变1": 1.0, "应变2": 2.0, "应变3": 3.0, "应变4": 4.0},
                            "quality": {name: "measured_new" for name in acquisition.STRAIN_SENSOR_COLUMNS},
                            "source_interfaces": {name: "esr1001_strain" for name in acquisition.STRAIN_SENSOR_COLUMNS},
                        }
                    ],
                    "next_cursor": 1,
                    "total_count": 1,
                    "truncated": False,
                }

            def status(self):
                return {"running": True, "sample_count": 1}

            def stream_counts(self):
                return {"total_count": 1}

        agent = LocalCaptureAgent(Manager())
        agent._capture_uuid = "remote-esr1001"
        batch = agent.next_sample_batch(limit=10)
        self.assertEqual(batch["frame_contract"], "unified_frame_v1")
        self.assertEqual(batch["rows"][0]["应变4"], 4.0)
        self.assertEqual(
            batch["frames"][0]["source_interfaces"]["应变1"],
            "esr1001_strain",
        )
        self.assertNotIn("endpoint", batch["rows"][0])

    def test_local_remote_and_simulation_keep_the_same_six_interface_mapping(self) -> None:
        def real_interfaces(location: str):
            interfaces = acquisition.default_capture_interfaces()
            for index, item in enumerate(interfaces):
                profile = acquisition.SENSOR_INTERFACE_PROFILES[item["role"]]
                item["location"] = location
                item["physical_interface_kind"] = profile["physical_kind"]
                if item["role"] in {"plc", "robot"}:
                    item["physical_interface_id"] = "ethernet:industrial"
                else:
                    item["physical_interface_id"] = f"{profile['physical_kind']}:{index}"
                item["physical_verified"] = True
                if item["role"] == "thermocouple64":
                    item["hardware_facts"] = {
                        "manufacturer": "test-fixture",
                        "model": "test-fixture",
                        "manual_source": "test-fixture-only",
                        "nameplate_identity": "test-fixture-only",
                        "protocol_version": "test-fixture-only",
                        "interface_type": "test-fixture-only",
                        "electrical_topology": "test-fixture-only",
                        "sampling_rate_hz": 1,
                        "measurement_range_c": "test-fixture-only",
                        "cold_junction_compensation": "test-fixture-only",
                        "fault_codes": ["test-fixture-only"],
                        "vendor_channel_mapping": {"test-fixture-only": "test-fixture-only"},
                    }
            next(item for item in interfaces if item["id"] == "esr1001_strain")["endpoint"] = "COM9"
            return interfaces

        simulation = acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v13_0",
            simulation_source_path="simulation.csv",
            interfaces=acquisition.default_capture_interfaces(),
            selected_sensors=acquisition.LATEST_COLLECTION_SENSOR_COLUMNS.copy(),
            processing_mode="capture_only",
        )
        local = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            real_acquisition_mode="local_direct",
            dataset_schema="new_collection_v13_0",
            interfaces=real_interfaces("local"),
            selected_sensors=["薄膜压力原始响应" if n == "薄膜压力" else n for n in acquisition.LATEST_COLLECTION_SENSOR_COLUMNS],
            processing_mode="capture_only",
        )
        remote = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            real_acquisition_mode="remote_helper",
            execution_host="helper_local",
            helper_session_id="helper-1",
            helper_capabilities={"unified_frame_v1": True, "unified_frame_v2": True},
            dataset_schema="new_collection_v13_0",
            interfaces=real_interfaces("remote_helper"),
            selected_sensors=["薄膜压力原始响应" if n == "薄膜压力" else n for n in acquisition.LATEST_COLLECTION_SENSOR_COLUMNS],
            processing_mode="capture_only",
        )
        self.assertEqual(
            simulation.interface_channel_assignments,
            {key:["薄膜压力" if n == "薄膜压力原始响应" else n for n in names] for key,names in local.interface_channel_assignments.items()},
        )
        self.assertEqual(local.interface_channel_assignments, remote.interface_channel_assignments)
        self.assertEqual(
            [item["id"] for item in simulation.interfaces],
            [item["id"] for item in local.interfaces],
        )


if __name__ == "__main__":
    unittest.main()
