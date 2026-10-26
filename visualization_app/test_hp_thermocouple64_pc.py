from __future__ import annotations

import csv
import dataclasses
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acquisition
from edge_capture import RemoteAcquisitionRegistry
from hp_thermocouple64 import HpProtocolError, HpThermocouple64Driver, parse_pc_response
from local_direct_acquisition import LocalDirectAdapter
from mysql_storage import MySQLCaptureStore, MySQLSettings
from pre_sensor_config import InterfacePlanStore
from real_acquisition import ChannelSampleCache, FrameAssembler, UnifiedFrame, readiness_config_fingerprint
from server_capture_journal import ServerCaptureJournal
from test_mysql_identity import _FakeConnection
from thermocouple64 import THERMOCOUPLE64_CHANNELS as CHANNELS, THERMOCOUPLE64_MODE_ID as MODE, Thermocouple64AdapterContract, raw_catalog


SAMPLES = json.loads((Path(__file__).parent / "testdata/tc64_vendor_samples.json").read_text(encoding="utf-8"))
RAW = bytes.fromhex(SAMPLES["pc_responses"][0]["response_hex"])


def vendor_logged_response(record):
    """Restore only lost unit characters; retain the vendor's original checksum."""
    return record["logged_response"].replace("\ufffd\ufffd", "℃").encode("gb18030") + b"\r\n"


def checksummed(body: str) -> bytes:
    """Synthetic fault/edge-case frames; no physical fault acceptance is implied."""
    payload = (body + ",").encode("gb18030")
    return payload + f"{sum(payload):06d}".encode() + b"\r\n"


class PcSerial:
    def __init__(self, frame=RAW, fragment=17):
        self.frame, self.fragment, self.pending = frame, fragment, b""
        self.writes, self.closed = [], False

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, request):
        if request != b":":
            raise AssertionError("PC mode may only send the vendor temperature request")
        self.writes.append(request)
        self.pending = self.frame
        return len(request)

    def read(self, size):
        count = min(size, self.fragment)
        result, self.pending = self.pending[:count], self.pending[count:]
        return result

    def close(self):
        self.closed = True


class HpThermocouple64PcTests(unittest.TestCase):
    def test_vendor_logged_inputs_match_independently_saved_csv_values(self):
        for record in SAMPLES["vendor_log_csv_pairs"]:
            with self.subTest(csv=record["vendor_csv_name"], line=record["vendor_csv_line"]):
                expected = dict(zip(CHANNELS, map(float, record["temperatures"])))
                port = PcSerial(vendor_logged_response(record), fragment=1)
                driver = HpThermocouple64Driver("VENDOR-LOG-REPLAY", read_profile="pc_temperature", serial_factory=lambda **kw: port)
                driver.open()
                try:
                    self.assertEqual(driver.read_sample(), expected)
                    self.assertEqual(driver.quality_metadata["ambient_temperature_c"], float(record["ambient"]))
                    self.assertEqual(driver.quality_metadata["channel_errors"], {})
                finally:
                    driver.close()

    def test_six_independent_hardware_responses_preserve_all_decimal_values(self):
        for record in SAMPLES["pc_responses"]:
            parsed = parse_pc_response(bytes.fromhex(record["response_hex"]))
            self.assertEqual(list(parsed["values"]), list(CHANNELS))
            self.assertEqual(list(parsed["values"].values()), list(map(float, record["temperatures"])))
            self.assertEqual(parsed["ambient_temperature_c"], float(record["ambient"]))
            self.assertEqual(parsed["device_time_text"], record["device_time"])
            self.assertEqual(parsed["pc_device_address"], "000")
            self.assertEqual(parsed["channel_errors"], {})

    def test_explicit_pc_profile_requests_fragments_and_same_second_observations(self):
        port = PcSerial(fragment=1)
        driver = HpThermocouple64Driver("COM17", read_profile="pc_temperature", serial_factory=lambda **kw: port)
        driver.open()
        try:
            first = driver.read_sample()
            self.assertEqual(driver.read_sample(), first)
            self.assertEqual(driver.observation_count, 2)
            self.assertEqual(port.writes, [b":", b":"])
            self.assertEqual(driver.quality_metadata["protocol"], "huipu_pc_serial")
            self.assertEqual(driver.quality_metadata["timestamp_origin"], "host_scan_completion")
            self.assertGreater(driver.quality_metadata["received_wall_time"], 0)
            port.frame = RAW[:-3] + b"0\r\n"
            with self.assertRaisesRegex(HpProtocolError, "pc_checksum"):
                driver.read_sample()
            self.assertEqual(driver.observation_count, 2)
            self.assertFalse(driver.protocol_verified)
            self.assertEqual(port.writes[-1], b":")
        finally:
            driver.close()
        self.assertTrue(port.closed)

    def test_synthetic_faults_units_and_finite_extremes_are_channel_local(self):
        fields = RAW[:-2].decode("gb18030").rsplit(",", 1)[0].split(",")
        fields[3:11] = ["Open|K|℃", "OFF|K|℃", "OVR|K|℃", "-12.34|K|℃",
                        "273.15|K|K", "32.00|K|℉", "9999.99|K|℃", "25.00|V|V"]
        parsed = parse_pc_response(checksummed(",".join(fields)))
        self.assertEqual([parsed["values"][name] for name in CHANNELS[3:7]], [-12.34, 0.0, 0.0, 9999.99])
        self.assertEqual(parsed["channel_errors"], {"温度1": "thermocouple_open", "温度2": "channel_disabled",
                         "温度3": "unknown_pc_value", "温度8": "non_temperature_unit"})
        port = PcSerial(checksummed(",".join(fields)))
        driver = HpThermocouple64Driver("COM17", read_profile="pc_temperature", serial_factory=lambda **kw: port)
        cache = ChannelSampleCache()
        driver.open()
        try:
            worker = LocalDirectAdapter("tc", driver, CHANNELS, cache)
            worker._publish(driver.read_sample())
            self.assertIsNone(cache.latest("温度1").value)
            self.assertEqual(cache.latest("温度1").error, "thermocouple_open")
            self.assertEqual(cache.latest("温度7").value, 9999.99)
        finally:
            driver.close()

    def test_bad_checksum_structure_encoding_and_model_reject_the_whole_frame(self):
        body = RAW[:-2].decode("gb18030").rsplit(",", 1)[0]
        invalid = [RAW[:-1], RAW + RAW, RAW[:-3] + b"0\r\n",
                   checksummed(body.replace("TCP-64", "TCP-32", 1)),
                   checksummed(body.replace("26.71|K|℃,", "", 1)),
                   checksummed(body.replace("26.71|K|℃", "26.71", 1)),
                   checksummed(body.replace("T:2026", "T:99999", 1)),
                   checksummed(body.replace(",25.7,", ",NaN,", 1))]
        for raw in invalid:
            with self.subTest(raw=raw[:30]), self.assertRaises(HpProtocolError):
                parse_pc_response(raw)
        bad_encoding = b"TCP-64,\xff,"
        with self.assertRaisesRegex(HpProtocolError, "pc_encoding"):
            parse_pc_response(bad_encoding + f"{sum(bad_encoding):06d}".encode() + b"\r\n")

    def test_pc_timeout_is_bounded_and_never_falls_back_to_modbus(self):
        port = PcSerial(b"")
        driver = HpThermocouple64Driver("COM17", read_profile="pc_temperature", serial_factory=lambda **kw: port,
                                       scan_timeout_seconds=0.03)
        started = time.monotonic()
        with self.assertRaises(HpProtocolError):
            driver.open()
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(port.writes, [b":"])
        self.assertTrue(port.closed)

    def test_profile_factory_plan_and_readiness_binding(self):
        item = acquisition.default_capture_interfaces()[0]
        item.update(endpoint="COM17", physical_interface_id="serial:COM17", physical_interface_kind="serial", read_profile="pc_temperature")
        config = acquisition.AcquisitionConfig(acquisition_mode="real", dataset_schema=MODE, interfaces=[item], selected_sensors=list(CHANNELS))
        before = readiness_config_fingerprint(config)
        with patch("serial.Serial", side_effect=lambda **kw: PcSerial()):
            driver = acquisition.build_driver(config)
            driver.open()
            try:
                sample = None
                deadline = time.monotonic() + 1
                while sample is None and time.monotonic() < deadline:
                    sample = driver.read_sample()
                    if sample is None:
                        time.sleep(.005)
                self.assertIsNotNone(sample)
                self.assertEqual(sample["温度64"], 26.59)
            finally:
                driver.close()
        config.interfaces[0]["read_profile"] = "full_record_block"
        self.assertNotEqual(readiness_config_fingerprint(config), before)
        with tempfile.TemporaryDirectory() as root:
            store = InterfacePlanStore(Path(root) / "plan.json")
            store.save({"active": MODE, "plans": {MODE: {"interfaces": [item]}}})
            self.assertEqual(store.load()["plans"][MODE]["interfaces"][0]["read_profile"], "pc_temperature")

    def test_148_vendor_records_and_pc_responses_preserve_values_through_csv_helper_and_sql(self):
        catalog = raw_catalog(CHANNELS, {c: "°C" for c in CHANNELS})
        config = {"selected_sensors": list(CHANNELS), "dataset_schema": MODE}
        adapter, cache = Thermocouple64AdapterContract(), ChannelSampleCache()
        assembler = FrameAssembler(CHANNELS, 1, start_monotonic=0, start_wall_time=0, clock=lambda: 0)
        expected_rows = [{c: float(v) for c, v in zip(CHANNELS, r["temperatures"])} for r in SAMPLES["vendor_records"]]
        expected_rows += [parse_pc_response(bytes.fromhex(r["response_hex"]))["values"] for r in SAMPLES["pc_responses"]]
        expected_rows += [dict(zip(CHANNELS, map(float, r["temperatures"]))) for r in SAMPLES["vendor_log_csv_pairs"]]
        with tempfile.TemporaryDirectory() as root:
            journal = ServerCaptureJournal(Path(root) / "journal.sqlite3")
            mirrors = RemoteAcquisitionRegistry(max_rows=24)
            journal.arm("session", "selection", config=config)
            try:
                for index, expected in enumerate(expected_rows):
                    timestamp = float(index // 2)
                    snapshot = adapter.adapt(device_identity="record-replay", source_sequence=index, source_timestamp=timestamp, values=expected)
                    acquisition.publish_thermocouple64_snapshot(cache, snapshot, received_wall_time=timestamp, received_monotonic=index)
                    frame = dataclasses.replace(assembler.assemble(cache, index), target_wall_time=timestamp)
                    envelope = frame.to_envelope_v2(session_id="capture", mode_id=MODE, catalog=catalog)
                    self.assertEqual(UnifiedFrame.from_envelope_v2(json.loads(json.dumps(envelope)), expected_session_id="capture",
                        expected_mode_id=MODE, catalog=catalog).values(), expected)
                    batch = {"capture_uuid": "capture", "sequence": index, "required_frame_contract": "unified_frame_v2",
                             "frame_catalog": catalog, "frames": [envelope], "rows": [expected], "timestamps": [timestamp],
                             "status": {"running": False, "finalization_complete": True, "sample_count": index + 1, "config": config}}
                    self.assertTrue(journal.ingest("session", batch, mirrors)["ok"])
                recovered = list(journal.ready_capture("session", "capture").rows)
                self.assertEqual([{c: row[c] for c in CHANNELS} for row in recovered], expected_rows)
                output = io.StringIO(newline="")
                source = io.StringIO(newline="")
                writer = csv.DictWriter(source, fieldnames=list(CHANNELS), extrasaction="ignore")
                writer.writeheader()
                writer.writerows(recovered)
                source.seek(0)
                acquisition._append_csv_rows(output, csv.DictReader(source), list(CHANNELS), extrasaction="ignore")
                csv_rows = list(csv.reader(io.StringIO(output.getvalue())))
                self.assertEqual([{c: float(v) for c, v in zip(CHANNELS, row)} for row in csv_rows], expected_rows)
                store = MySQLCaptureStore(MySQLSettings(enabled=True, database="tc64_fixture"))
                connection = _FakeConnection()
                with patch.object(store, "_connect", return_value=("fake", connection)):
                    result = store.save_layer(acquisition.AcquisitionConfig(**config, capture_uuid="fixture"), rows=recovered,
                        layer_file=None, full_specimen_file=None, timestamp_file=None, folder_path=None, summary={})
                self.assertTrue(result["ok"], result)
                for table in ("AFP_SENSOR_SAMPLE", "AFP_SAMPLE_ALL"):
                    saved = [json.loads(row[5]) for sql, values in connection.cursor_instance.executed
                             if "INSERT INTO " + table in sql.upper() for row in values]
                    self.assertEqual(saved, expected_rows)
            finally:
                journal.close()

    def test_local_and_helper_capture_save_the_same_pc_values(self):
        from local_capture_agent import LocalCaptureAgent
        from local_capture_helper_entry import helper_capabilities

        expected = parse_pc_response(RAW)["values"]
        for remote in (False, True):
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as root:
                item = acquisition.default_capture_interfaces()[0]
                item.update(endpoint="COM17", physical_interface_id="serial:COM17", physical_interface_kind="serial",
                            physical_verified=True, read_profile="pc_temperature")
                config = acquisition.AcquisitionConfig(processing_mode="capture_only", acquisition_mode="real",
                    real_acquisition_mode="remote_helper" if remote else "local_direct",
                    execution_host="helper_local" if remote else "server_local", helper_session_id="fixture" if remote else "",
                    dataset_schema=MODE, selected_sensors=list(CHANNELS), sample_rate_hz=10, save_root=root,
                    condition_id="TC64_FIXTURE", interfaces=[item], interface_channel_assignments={item["id"]: list(CHANNELS)})
                config.helper_capabilities = helper_capabilities() if remote else {}
                manager = acquisition.AcquisitionManager(Path(root), recovery_scan=False)
                agent = LocalCaptureAgent(manager, simulation_cache_root=Path(root) / "cache")
                with patch("serial.Serial", side_effect=lambda **kw: PcSerial()), patch("acquisition.device_profile_readiness",
                        return_value={"ready": True, "missing_device_facts": []}):
                    checked = agent.check_capture(config) if remote else manager.test_connection(config)
                    self.assertTrue(checked["ok"], checked)
                    (agent.start_capture if remote else manager.start)(config)
                    try:
                        deadline = time.monotonic() + 4
                        while manager.total_sample_count < 5 and time.monotonic() < deadline:
                            time.sleep(.01)
                    finally:
                        stopped = agent.stop_capture() if remote else manager.stop()
                    self.assertTrue(stopped["capture_saved"], stopped.get("last_error"))
                    with Path(stopped["layer_file"]).open(encoding="gb18030", newline="") as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertGreaterEqual(len(rows), 2)
                    self.assertTrue(any(all(row.get(c) not in (None, "") for c in CHANNELS) for row in rows))
                    for row in rows:
                        for channel in CHANNELS:
                            if row.get(channel) not in (None, ""):
                                self.assertEqual(float(row[channel]), expected[channel])
                    if remote:
                        batch = agent.next_sample_batch()
                        self.assertIsNotNone(batch)
                        for row in batch["rows"]:
                            for channel in CHANNELS:
                                if row.get(channel) is not None:
                                    self.assertEqual(row[channel], expected[channel])


if __name__ == "__main__":
    unittest.main()
