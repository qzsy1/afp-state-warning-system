"""原生USB协议及源帧证据的可执行合同。"""
import csv
import json
import tempfile
import unittest
import sys
import time
import threading
import subprocess
import io
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acquisition_clock import monotonic

from esr1001_fixtures import USB_PARAMETERS, USB_START_RESPONSE, USB_STOP_RESPONSE, USB_DATA, USB_EXPECTED_ADC
from esr1001_usb import (UsbStreamParser, UsbProtocolError, Esr1001UsbDriver, RawCaptureWriter,
                         RAW_CHANNELS, QUERY, START, STOP, decode_adc, parse_parameters, load_calibration)


class SerialStub:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.writes = []
        self.buffer = bytearray()
        self.is_open = True
        self.ignore_stop = False

    @property
    def in_waiting(self):
        return len(self.buffer)

    def write(self, data):
        self.writes.append(data)
        response = {QUERY: USB_PARAMETERS, START: USB_START_RESPONSE + USB_DATA * 3, STOP: USB_STOP_RESPONSE}.get(data, b"")
        if data != STOP or not self.ignore_stop:
            self.buffer.extend(response)

    def read(self, size):
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def close(self):
        self.is_open = False


class StreamingSerial(SerialStub):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.streaming = False
        self.last_frame = monotonic()

    @property
    def in_waiting(self):
        if self.streaming and monotonic() - self.last_frame >= 0.01:
            self.buffer.extend(USB_DATA)
            self.last_frame = monotonic()
        return len(self.buffer)

    def write(self, data):
        super().write(data)
        if data == START:
            self.streaming = True
        elif data == STOP:
            self.streaming = False

    def read(self, size):
        if not self.buffer:
            time.sleep(0.001)
        return super().read(size)


class UsbProtocolTests(unittest.TestCase):
    def test_current_freshness_recovers_without_erasing_historical_gap(self):
        from esr1001_usb import current_stream_is_stable

        driver = SimpleNamespace(
            recent_intervals=(0.08, 0.09, 0.08),
            max_interval=0.41,
            gaps=[{"after_sequence": 12, "interval_seconds": 0.41}],
            parser=SimpleNamespace(discarded_bytes=0, overflows=0),
        )
        self.assertTrue(current_stream_is_stable(driver, 9.9, now=10.0))
        self.assertEqual(driver.max_interval, 0.41)
        self.assertEqual(len(driver.gaps), 1)

        driver.recent_intervals = (0.08, 0.31, 0.08)
        self.assertFalse(current_stream_is_stable(driver, 9.9, now=10.0))
        driver.recent_intervals = (0.08, 0.09)
        self.assertFalse(current_stream_is_stable(driver, 9.9, now=10.0))
        driver.recent_intervals = (0.08, 0.09, 0.08)
        self.assertFalse(current_stream_is_stable(driver, 9.7, now=10.0))

    def test_independent_sample_and_sign_boundaries(self):
        self.assertEqual(decode_adc(USB_DATA), (68794, -41374, -52572, -117937))
        raw = bytes.fromhex("24 80 00 00 7F FF FF 00 00 00 FF FF FF FF")
        self.assertEqual(decode_adc(raw), (8388607, 0, -1, -8388608))
        self.assertEqual(decode_adc(bytes.fromhex("24 FF FF FF FF"), 1), (-1,))
        self.assertEqual(decode_adc(bytes.fromhex("24 80 00 00 7F FF FF FF"), 2), (8388607, -8388608))

    def test_parameters_validate_fields_preserve_reserved(self):
        p = parse_parameters(USB_PARAMETERS)
        self.assertEqual((p["channel_count"], p["gain_code"], p["reserved_hex"]), (4, 6, "6800000000"))
        for offset, value in ((1, 2), (2, 0), (3, 7), (4, 3), (5, 2), (6, 0), (7, 9)):
            bad = bytearray(USB_PARAMETERS)
            bad[offset] = value
            with self.assertRaises(UsbProtocolError):
                parse_parameters(bad)
        with self.assertRaises(UsbProtocolError):
            parse_parameters(USB_START_RESPONSE)

    def test_fragmentation_coalescing_markers_and_resync(self):
        parser = UsbStreamParser()
        stream = USB_PARAMETERS + USB_START_RESPONSE + USB_DATA * 7 + USB_STOP_RESPONSE
        result = []
        for b in stream:
            result.extend(parser.feed(bytes([b])))
        self.assertEqual(result, [USB_PARAMETERS, USB_START_RESPONSE] + [USB_DATA] * 7 + [USB_STOP_RESPONSE])
        self.assertEqual(parser.discarded_bytes, 0)
        parser = UsbStreamParser()
        self.assertEqual(parser.feed(USB_DATA * 2), [])
        self.assertEqual(parser.feed(USB_DATA), [USB_DATA] * 3)
        bad = bytearray(USB_DATA)
        bad[-1] = 0
        recovered = parser.feed(bytes(bad) + b"noise" + USB_DATA * 3)
        self.assertEqual(recovered, [USB_DATA] * 3)
        self.assertGreater(parser.discarded_bytes, 0)
        self.assertEqual(parser.resynchronizations, 2)

    def test_noise_memory_bound(self):
        parser = UsbStreamParser()
        self.assertEqual(parser.feed(b"x" * 100000), [])
        self.assertLessEqual(len(parser.buffer), 65536)
        self.assertEqual(parser.discarded_bytes, 100000)
        self.assertEqual(parser.overflows, 1)

    def driver(self, serial=None, **kw):
        stub = serial or SerialStub()
        driver = Esr1001UsbDriver("COM91", serial_factory=lambda *a, **k: self.configure_stub(stub, a, k), inventory=lambda: [SimpleNamespace(device="COM91", vid=0x1A86, pid=0x7523, location="test")], command_timeout=0.01, **kw)
        return driver, stub

    @staticmethod
    def configure_stub(stub, args, kwargs):
        stub.args, stub.kwargs = args, kwargs
        return stub

    def test_control_lifecycle_exact_writes_and_exclusion(self):
        driver, stub = self.driver()
        driver.open()
        try:
            second, _ = self.driver()
            with self.assertRaisesRegex(UsbProtocolError, "port_busy"):
                second.open()
            samples = driver.read_samples()
            self.assertEqual(len(samples), 3)
            self.assertEqual(tuple(samples[0][0].values()), USB_EXPECTED_ADC)
            self.assertEqual([r[1]["receive_sequence"] for r in samples], [1, 2, 3])
            self.assertEqual(stub.kwargs["baudrate"], 115200)
        finally:
            driver.close()
        self.assertEqual(stub.writes, [QUERY, START, STOP])
        self.assertFalse(stub.is_open)
        self.assertEqual(driver.sequence, 3)

    def test_bounded_start_rejects_buffered_old_responses_and_invalid_parameters(self):
        with patch("esr1001_usb.PORT_READY_GUARD_SECONDS", 0), patch("esr1001_usb.QUERY_START_SPACING_SECONDS", 0):
            stale = SerialStub()
            stale.buffer.extend(USB_PARAMETERS + USB_START_RESPONSE)
            stale.write = lambda data: stale.writes.append(data)
            driver, _ = self.driver(stale, startup_policy="bounded_response")
            with self.assertRaisesRegex(UsbProtocolError, "usb_parameters_timeout"):
                driver.open()
            self.assertFalse(driver.protocol_verified)
            self.assertNotIn(START, stale.writes)
            self.assertFalse(stale.is_open)
            invalid = SerialStub()
            write = invalid.write
            def respond(data):
                write(data)
                if data == QUERY:
                    invalid.buffer[4] = 2
            invalid.write = respond
            driver, _ = self.driver(invalid, startup_policy="bounded_response")
            with self.assertRaisesRegex(UsbProtocolError, "partial_channels"):
                driver.open()
            self.assertNotIn(START, invalid.writes)

    def test_guarded_start_preserves_parameter_wait_and_reconnect_uses_fixed_timing(self):
        from esr1001_usb import GUARDED_START_PARAMETER_HEX
        class GuardedSerial(SerialStub):
            def write(self, data):
                super().write(data)
                if data == QUERY:
                    self.buffer[:] = bytes.fromhex(GUARDED_START_PARAMETER_HEX)
        driver, _ = self.driver(GuardedSerial(), startup_policy="guarded_response")
        waits = []
        with patch.object(driver, "_wait_open_until", side_effect=lambda deadline: waits.append(deadline - monotonic())):
            driver.open()
            try:
                self.assertEqual(driver.quality_metadata["startup_policy"], "guarded_response")
                self.assertTrue(driver.read_samples())
            finally:
                driver.close()
            driver.serial_factory = lambda *args, **kwargs: GuardedSerial()
            driver.reset_open_cancellation()
            driver.open()
            try:
                self.assertEqual(driver.quality_metadata["startup_policy"], "fixed")
            finally:
                driver.close()
        self.assertEqual(len(waits), 4)
        for actual, expected in zip(waits, (.5, 1.0, 1.0, 1.0)):
            self.assertAlmostEqual(actual, expected, delta=.05)

    def test_guarded_start_rejects_changed_parameters_before_start_command(self):
        driver, stub = self.driver(startup_policy="guarded_response")
        with patch.object(driver, "_wait_open_until"):
            with self.assertRaisesRegex(UsbProtocolError, "binding_unconfirmed"):
                driver.open()
        self.assertNotIn(START, stub.writes)
        self.assertFalse(stub.is_open)

    def test_bounded_spacing_uses_command_clock_with_a_slow_matching_reply(self):
        clock = [100.0]
        class Event:
            cancelled = False
            def is_set(self): return self.cancelled
            def set(self): self.cancelled = True
            def wait(self, seconds): clock[0] += seconds; return self.cancelled
        class DelayedSerial(SerialStub):
            ready = None
            def write(self, data):
                if data == QUERY:
                    self.writes.append(data)
                    self.ready = clock[0] + .4
                else:
                    super().write(data)
            def read(self, size):
                clock[0] += .01
                if self.ready is not None and clock[0] >= self.ready:
                    self.buffer.extend(USB_PARAMETERS)
                    self.ready = None
                return super().read(size)
        driver, _ = self.driver(DelayedSerial(), startup_policy="bounded_response")
        driver.command_timeout = .5
        driver._cancel_open = Event()
        with patch("esr1001_usb.monotonic", side_effect=lambda: clock[0]):
            driver.open()
            try:
                commands = list(driver.command_evidence)
                self.assertAlmostEqual(commands[1]["host_monotonic"] - commands[0]["host_monotonic"], 1.3)
                self.assertTrue(commands[0]["matched_response_hex"])
                self.assertTrue(commands[1]["matched_response_hex"])
            finally:
                driver.close()

    def test_cancel_bounded_wait_releases_port_without_late_start(self):
        driver, stub = self.driver(startup_policy="bounded_response")
        errors = []
        def open_driver():
            try:
                driver.open()
            except Exception as exc:
                errors.append(str(exc))
        with patch("esr1001_usb.PORT_READY_GUARD_SECONDS", 0), patch("esr1001_usb.QUERY_START_SPACING_SECONDS", 3):
            worker = threading.Thread(target=open_driver)
            worker.start()
            try:
                deadline = monotonic() + 1
                while not driver.command_evidence and monotonic() < deadline:
                    time.sleep(.001)
                cancelled = monotonic()
                driver.cancel_open()
                worker.join(.5)
                self.assertFalse(worker.is_alive())
                self.assertLess(monotonic()-cancelled, .5)
                self.assertTrue(any("capture_start_cancelled" in error for error in errors))
                self.assertNotIn(START, stub.writes)
                self.assertFalse(stub.is_open)
                replacement, _ = self.driver(startup_policy="bounded_response")
                with patch("esr1001_usb.QUERY_START_SPACING_SECONDS", 0):
                    replacement.open()
                    replacement.close()
            finally:
                driver.cancel_open()
                worker.join(1)

    def test_port_and_parameter_settle_before_start(self):
        driver, stub = self.driver()
        stamps = {}
        configure = driver.serial_factory
        write, read = stub.write, stub.read
        def opened(*args, **kwargs):
            result = configure(*args, **kwargs)
            stamps["opened"] = monotonic()
            return result
        def sent(data):
            stamps[data] = monotonic()
            write(data)
        def received(size):
            data = read(size)
            if data == USB_PARAMETERS:
                stamps["parameters_received"] = monotonic()
            return data
        driver.serial_factory = opened
        stub.write, stub.read = sent, received
        driver.open()
        try:
            self.assertGreaterEqual(stamps[QUERY] - stamps["opened"], 0.95)
            self.assertGreaterEqual(stamps[START] - stamps["parameters_received"], 0.95)
            self.assertEqual(stub.writes, [bytes.fromhex("23 A8 00 00 00 FF"), bytes.fromhex("23 A1 01 00 00 FF")])
        finally:
            driver.close()

    def test_timeout_and_stop_failure_release(self):
        driver, stub = self.driver()
        driver.open()
        stub.ignore_stop = True
        driver.close()
        self.assertIn("usb_stop_timeout", driver.stop_error)
        self.assertFalse(stub.is_open)
        driver, stub = self.driver()
        stub.write = lambda data: stub.writes.append(data)
        with self.assertRaisesRegex(UsbProtocolError, "usb_parameters_timeout"):
            driver.open()
        self.assertEqual(stub.writes, [QUERY])
        self.assertFalse(stub.is_open)

    def test_stop_drains_backlog_without_filling_consumer_queue(self):
        driver, stub = self.driver()
        driver.open()
        driver.read_samples()
        stub.buffer.extend(USB_DATA * 4200)
        driver.command_timeout = 2
        driver.close()
        self.assertEqual(driver.stop_error, "")
        self.assertEqual(driver.command_evidence[-1]["matched_response_hex"], USB_STOP_RESPONSE.hex())
        self.assertEqual(driver.sequence, 4203)
        self.assertEqual(len(driver._pending), 0)
        self.assertFalse(stub.is_open)

    def test_stop_with_full_consumer_queue_preserves_raw_records(self):
        with tempfile.TemporaryDirectory() as root:
            recorder = RawCaptureWriter(root, {"interface_id": "esr"})
            driver, stub = self.driver(recorder=recorder)
            driver.open()
            driver._pending.extend([driver.latest_record] * (4096 - len(driver._pending)))
            stub.buffer.extend(USB_DATA * 2)
            driver.close()
            recorder.close(driver.quality_metadata)
            self.assertEqual(driver.stop_error, "")
            self.assertEqual(driver.sequence, 5)
            self.assertEqual(recorder.rows, 5)
            self.assertEqual(len(driver._pending), 4096)
            self.assertFalse(stub.is_open)

    def test_active_consumer_overflow_remains_an_error(self):
        driver, stub = self.driver()
        driver.open()
        try:
            driver._pending.extend([driver.latest_record] * (4096 - len(driver._pending)))
            stub.buffer.extend(USB_DATA)
            with self.assertRaisesRegex(UsbProtocolError, "usb_consumer_overflow"):
                driver.read_samples()
        finally:
            driver.close()

    def test_stop_backlog_without_reply_still_reports_timeout(self):
        driver, stub = self.driver()
        driver.open()
        driver._pending.extend([driver.latest_record] * (4096 - len(driver._pending)))
        stub.buffer.extend(USB_DATA)
        stub.ignore_stop = True
        driver.close()
        self.assertIn("usb_stop_timeout", driver.stop_error)
        self.assertFalse(stub.is_open)

    def test_probe_preserves_presence_when_open_is_denied_or_protocol_times_out(self):
        import serial
        from esr1001_usb import probe_usb
        interface = {"id": "esr", "endpoint": "COM91", "physical_verified": True}
        for error in (PermissionError(13, "denied"), serial.SerialException("could not open port: PermissionError(13, 'denied', None, 5)")):
            with self.subTest(error=type(error).__name__):
                driver, stub = self.driver()
                driver.serial_factory = lambda *a, **k: (_ for _ in ()).throw(error)
                result = probe_usb(interface, driver_factory=lambda *a, **k: driver)
                self.assertEqual(result["state"], "port_access_denied")
                self.assertEqual(result["stages"]["port_present"]["state"], "passed")
                self.assertEqual(result["stages"]["endpoint_compatible"]["state"], "passed")
                self.assertEqual(result["stages"]["driver_open"]["state"], "failed")
                self.assertFalse(result["ok"])
                self.assertFalse(driver._owns_port)
                self.assertEqual(stub.writes, [])
        driver, stub = self.driver()
        stub.write = lambda data: stub.writes.append(data)
        result = probe_usb(interface, driver_factory=lambda *a, **k: driver)
        self.assertEqual(result["state"], "usb_parameters_timeout")
        self.assertEqual(result["stages"]["driver_open"]["state"], "passed")
        self.assertEqual(result["stages"]["protocol_identified"]["state"], "failed")
        self.assertFalse(stub.is_open)

    def test_writer_readback_and_calibration(self):
        with tempfile.TemporaryDirectory() as root:
            writer = RawCaptureWriter(root, {"purpose": "raw_adc"})
            driver, _ = self.driver(recorder=writer)
            driver.open()
            driver.read_samples()
            driver.close()
            writer.close(driver.quality_metadata)
            with writer.path.open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertEqual(tuple(int(row[k]) for k in RAW_CHANNELS), decode_adc(bytes.fromhex(row["raw_hex"])))
            self.assertEqual(json.loads(writer.metadata_path.read_text(encoding="utf-8"))["saved_source_rows"], 3)
        binding = {"location": "USB_TEST"}
        profile = {"version": 1, "unit": "με", "source": "fixture", "device_binding": binding, "bridge": "quarter", "gain_code": 6, "channels": [{"K": 0.1, "B": -2}] * 4}
        self.assertEqual(len(load_calibration(profile, binding=binding, parameters={"gain_code": 6}, bridge="quarter")[1]), 64)
        for invalid in (None, dict(profile, gain_code=5), dict(profile, channels=[{"K": 0, "B": 0}] * 4)):
            with self.assertRaises(UsbProtocolError):
                load_calibration(invalid, binding=binding, parameters={"gain_code": 6}, bridge="quarter")

    def test_adc_marker_bytes_and_wrong_control_do_not_identify(self):
        frame = bytes.fromhex("24 24 25 26 FF 24 25 26 FF 24 25 26 FF FF")
        parser = UsbStreamParser()
        self.assertEqual(parser.feed(frame * 4), [frame] * 4)
        driver, serial = self.driver()
        def wrong_ack(data):
            serial.writes.append(data)
            serial.buffer.extend(bytes.fromhex("25 A8 00 00 00 00 00 00 00 00 00 00 00 FF"))
        serial.write = wrong_ack
        with self.assertRaisesRegex(UsbProtocolError, "usb_parameters_timeout"):
            driver.open()
        self.assertFalse(driver.protocol_verified)

    def test_nonfinite_and_mismatched_calibration(self):
        binding = {"location": "test"}
        profile = {"version": 1, "unit": "με", "source": "approved-fixture", "device_binding": binding, "bridge": "quarter", "gain_code": 6, "channels": [{"K": -0.1, "B": 2}] * 4}
        for invalid in (dict(profile, device_binding={"location": "other"}), dict(profile, bridge="half"), dict(profile, channels=[{"K": float("nan"), "B": 2}] * 4)):
            with self.assertRaises(UsbProtocolError):
                load_calibration(invalid, binding=binding, parameters={"gain_code": 6}, bridge="quarter")
        driver, _ = self.driver(data_mode="microstrain", calibration=profile, binding=binding, bridge="quarter")
        driver.open()
        try:
            sample = driver.read_samples()[0][0]
            self.assertAlmostEqual(sample["应变1"], 68794 * -0.1 + 2)
            self.assertAlmostEqual(sample["应变2"], -41374 * -0.1 + 2)
            self.assertNotIn(RAW_CHANNELS[0], sample)
        finally:
            driver.close()

    def test_missing_port_changed_identity_and_wrong_channel_count(self):
        driver, stub = self.driver()
        driver.inventory = lambda: []
        with self.assertRaisesRegex(UsbProtocolError, "port_not_present"):
            driver.open()
        self.assertEqual(stub.writes, [])
        driver, stub = self.driver(binding={"location": "other"})
        with self.assertRaisesRegex(UsbProtocolError, "device_identity_mismatch"):
            driver.open()
        self.assertEqual(stub.writes, [])
        driver, stub = self.driver()
        original = stub.write
        def two_channels(data):
            original(data)
            if data == QUERY:
                stub.buffer[4] = 2
        stub.write = two_channels
        with self.assertRaisesRegex(UsbProtocolError, "partial_channels"):
            driver.open()
        self.assertEqual(stub.writes, [QUERY])

    def test_slow_writer_queue_overload_is_visible_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            writer = RawCaptureWriter(root, {}, capacity=1)
            entered, release = threading.Event(), threading.Event()
            original = writer.writer
            class BlockingWriter:
                def writerow(self, record):
                    entered.set()
                    release.wait(2)
                    original.writerow(record)
            writer.writer = BlockingWriter()
            try:
                writer.submit({"receive_sequence": 1})
                self.assertTrue(entered.wait(1))
                writer.submit({"receive_sequence": 2})
                with self.assertRaisesRegex(UsbProtocolError, "raw_writer_overflow"):
                    writer.submit({"receive_sequence": 3})
                self.assertEqual(writer.queue.qsize(), 1)
            finally:
                release.set()
                writer.close({})
            self.assertEqual(writer.rows, 2)
            self.assertTrue(json.loads(writer.metadata_path.read_text(encoding="utf-8"))["writer_error"])


class UsbIntegrationTests(unittest.TestCase):
    @staticmethod
    def config(**overrides):
        from acquisition import AcquisitionConfig
        values = dict(acquisition_mode="real", real_acquisition_mode="local_direct", processing_mode="capture_only", dataset_schema="esr1001_raw_adc", interfaces=[dict(id="esr", role="esr1001_strain", driver="esr1001_usb_ttl", endpoint="COM91", baudrate=9600, data_mode="raw_adc", physical_interface_id="serial:COM91", physical_interface_kind="serial", physical_verified=True)])
        values.update(overrides)
        return AcquisitionConfig(**values)

    def test_connected_usb_disconnect_confirms_stop_after_large_backlog(self):
        from acquisition import AcquisitionManager

        class BackloggedStopSerial(StreamingSerial):
            def write(self, data):
                if data == STOP:
                    self.buffer.extend(USB_DATA * 4200)
                super().write(data)

        serials = []
        def factory(*args, **kwargs):
            stub = BackloggedStopSerial(*args, **kwargs)
            serials.append(stub)
            return stub

        with tempfile.TemporaryDirectory() as root, patch("serial.Serial", side_effect=factory), patch(
            "serial.tools.list_ports.comports", return_value=[SimpleNamespace(device="COM91")]
        ):
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            manager.connect_data(self.config(save_root=root))
            manager._connection_thread.join(10)
            try:
                self.assertTrue(manager.connection_status()["ready"], manager.connection_status())
                driver = manager._connection_driver
                status = manager.disconnect_data()
                self.assertEqual(status["data_connection"]["state"], "disconnected")
                self.assertEqual(driver.workers, [])
                evidence = driver.interface_quality_metadata["esr"]
                self.assertEqual(evidence["stop_error"], "")
                self.assertEqual(evidence["commands"][-1]["matched_response_hex"], USB_STOP_RESPONSE.hex())
                self.assertTrue(all(not stub.is_open for stub in serials))
                self.assertEqual(list(Path(root).rglob("*")), [])
            finally:
                if manager._connection_driver is not None:
                    manager.disconnect_data()

    def test_roundtrip_routing_model_boundary_and_legacy_protocol(self):
        from dataclasses import asdict
        from acquisition import AcquisitionConfig, acquisition_config_from_payload
        config = acquisition_config_from_payload(asdict(self.config()))
        self.assertEqual(config.interfaces[0]["driver"], "esr1001_usb_ttl")
        self.assertEqual(config.interfaces[0]["baudrate"], 115200)
        self.assertEqual(config.selected_sensors, list(RAW_CHANNELS))
        self.assertEqual(config.model_input_sensors, [])
        self.assertFalse(any(name.startswith("应变") for name in config.raw_columns))
        with self.assertRaisesRegex(ValueError, "capture_only"):
            self.config(processing_mode="prediction_warning")
        for schema in ("new_collection_v11_3", "new_collection_v12_0", "new_collection_v13_0"):
            joint = self.config(dataset_schema=schema)
            self.assertEqual(joint.selected_sensors, list(RAW_CHANNELS))
            self.assertTrue(set(RAW_CHANNELS).issubset(joint.raw_columns))
            self.assertFalse(any(name.startswith("应变") for name in joint.schema_sensors))
            with self.assertRaisesRegex(ValueError, "capture_only"):
                self.config(dataset_schema=schema, processing_mode="prediction_warning")
        with self.assertRaisesRegex(ValueError, "正常数据方案"):
            self.config(dataset_schema="legacy_original")
        old = dict(self.config().interfaces[0], driver="modbus_rtu_esr1001", baudrate=9600)
        config = AcquisitionConfig(acquisition_mode="real", dataset_schema="new_collection_v12_0", interfaces=[old])
        self.assertEqual(config.interfaces[0]["driver"], "modbus_rtu_esr1001")
        self.assertEqual(config.interfaces[0]["baudrate"], 9600)
        old["baudrate"] = 19200
        old["slave_id"] = 11
        config = AcquisitionConfig(acquisition_mode="real", dataset_schema="new_collection_v12_0", interfaces=[old])
        self.assertEqual(config.interfaces[0]["baudrate"], 19200)
        self.assertEqual(config.interfaces[0]["slave_id"], 11)

    def test_fingerprint_and_old_helper_rejection(self):
        from real_acquisition import readiness_config_fingerprint
        from esr1001_usb import require_helper_capabilities
        config = self.config()
        before = readiness_config_fingerprint(config)
        for key, value in (("data_mode", "microstrain"), ("calibration_digest", "changed"), ("bridge", "half"), ("channel_count", 2), ("device_identity", {"location": "changed"})):
            original = dict(config.interfaces[0])
            config.interfaces[0][key] = value
            self.assertNotEqual(readiness_config_fingerprint(config), before, key)
            config.interfaces[0] = original
        config.real_acquisition_mode = "remote_helper"
        config.helper_capabilities = {"esr1001_modbus_rtu": True}
        with self.assertRaisesRegex(RuntimeError, "esr1001_usb_ttl"):
            require_helper_capabilities(config)
        config.helper_capabilities.update(esr1001_usb_ttl=True, esr1001_raw_adc_capture=True)
        require_helper_capabilities(config)
        config.interfaces[0]["data_mode"] = "microstrain"
        with self.assertRaisesRegex(RuntimeError, "esr1001_calibration_v1"):
            require_helper_capabilities(config)

    def test_afp_manager_check_start_stop_and_source_file_reconciliation(self):
        from acquisition import AcquisitionManager
        serials = []
        def factory(*a, **k):
            stub = StreamingSerial(*a, **k)
            serials.append(stub)
            return stub
        with tempfile.TemporaryDirectory() as root, patch("serial.Serial", side_effect=factory), patch("serial.tools.list_ports.comports", return_value=[SimpleNamespace(device="COM91", vid=0x1A86, pid=0x7523)]):
            config = self.config(save_root=root)
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            result = manager.test_connection(config)
            self.assertTrue(result["ok"], result)
            self.assertGreaterEqual(result["interfaces"][0]["elapsed_seconds"], 3)
            self.assertEqual(serials[0].writes, [QUERY, START, STOP])
            manager.start(config)
            time.sleep(0.3)
            with self.assertRaisesRegex(RuntimeError, "port_busy"):
                manager.test_connection(config)
            status = manager.stop()
            self.assertTrue(status["finalization_complete"], status)
            self.assertEqual(status["capture_phase"], "completed", status)
            evidence = manager.driver.interface_quality_metadata["esr"]
            self.assertGreater(evidence["source_frames"], manager.total_sample_count)
            self.assertEqual(evidence["source_frames"], evidence["saved_source_rows"])
            with Path(evidence["raw_file"]).open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), evidence["source_frames"])
            self.assertEqual([int(r["receive_sequence"]) for r in rows], list(range(1, len(rows) + 1)))
            for row in rows:
                self.assertEqual(tuple(int(row[k]) for k in RAW_CHANNELS), decode_adc(bytes.fromhex(row["raw_hex"])))
            self.assertTrue(all(not s.is_open for s in serials))

    def test_concurrent_identical_checks_share_one_hardware_probe(self):
        from acquisition import AcquisitionManager

        started = threading.Event()
        release = threading.Event()
        call_lock = threading.Lock()
        probe_calls = 0

        def probe(interface):
            nonlocal probe_calls
            with call_lock:
                probe_calls += 1
            started.set()
            self.assertTrue(release.wait(2.0))
            return {
                **interface,
                "enabled": True,
                "state": "data_ready",
                "final_status": "data_ready",
                "message": "四路原始数据可用，标定未确认",
                "ok": True,
                "expected_channels": list(RAW_CHANNELS),
                "detected_channels": list(RAW_CHANNELS),
                "missing_channels": [],
                "invalid_channels": [],
                "sample_counts": {name: 3 for name in RAW_CHANNELS},
                "errors": [],
            }

        with tempfile.TemporaryDirectory() as root, patch("acquisition.probe_usb", side_effect=probe):
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            config = self.config(save_root=root)
            results = []
            failures = []

            def run_check():
                try:
                    results.append(manager.test_connection(config))
                except Exception as exc:
                    failures.append(exc)

            first = threading.Thread(target=run_check)
            second = threading.Thread(target=run_check)
            first.start()
            self.assertTrue(started.wait(1.0))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(2.0)
            second.join(2.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(probe_calls, 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(results[0]["checked_at"], results[1]["checked_at"])

    def test_different_check_fingerprints_queue_behind_one_physical_probe(self):
        from acquisition import AcquisitionManager

        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        release_second = threading.Event()
        guard = threading.Lock()
        calls = []
        active = 0
        max_active = 0

        def probe(interface):
            nonlocal active, max_active
            endpoint = interface["endpoint"]
            with guard:
                calls.append(endpoint)
                active += 1
                max_active = max(max_active, active)
            try:
                if endpoint == "COM91":
                    first_started.set()
                    self.assertTrue(release_first.wait(2.0))
                else:
                    second_started.set()
                    self.assertTrue(release_second.wait(2.0))
                return {
                    **interface,
                    "enabled": True,
                    "state": "data_ready",
                    "message": "ready",
                    "ok": True,
                    "expected_channels": list(RAW_CHANNELS),
                    "detected_channels": list(RAW_CHANNELS),
                    "missing_channels": [],
                    "invalid_channels": [],
                    "sample_counts": {name: 3 for name in RAW_CHANNELS},
                    "errors": [],
                }
            finally:
                with guard:
                    active -= 1

        with tempfile.TemporaryDirectory() as root, patch("acquisition.probe_usb", side_effect=probe):
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            first_config = self.config(save_root=root)
            second_config = self.config(save_root=root)
            second_config.interfaces[0].update(
                endpoint="COM92",
                physical_interface_id="serial:COM92",
            )
            results = []
            failures = []

            def run(config):
                try:
                    results.append(manager.test_connection(config))
                except Exception as exc:
                    failures.append(exc)

            threads = [
                threading.Thread(target=run, args=(first_config,)),
                threading.Thread(target=run, args=(first_config,)),
                threading.Thread(target=run, args=(second_config,)),
            ]
            threads[0].start()
            self.assertTrue(first_started.wait(1.0))
            threads[1].start()
            threads[2].start()
            time.sleep(0.05)
            self.assertFalse(second_started.is_set())
            release_first.set()
            self.assertTrue(second_started.wait(1.0))
            release_second.set()
            for thread in threads:
                thread.join(2.0)

        self.assertEqual(failures, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(calls.count("COM91"), 1)
        self.assertEqual(calls.count("COM92"), 1)
        self.assertEqual(max_active, 1)
        self.assertEqual(len(results), 3)

    def test_native_frontend_reuses_matching_fixed_local_backend(self):
        from native_frontend_launcher import discover_shared_ui_url

        expected = {
            "status": "ok",
            "version": "2.0.5-rc.2",
            "build_id": "esr1001-native-usb-v2",
            "runtime_revision": "revision-2",
        }
        requests = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        def opener(request, timeout):
            requests.append((request.full_url, timeout))
            return Response(json.dumps(expected).encode("utf-8"))

        self.assertEqual(
            discover_shared_ui_url(
                "http://127.0.0.1:8771/",
                expected_health=expected,
                opener=opener,
            ),
            "http://127.0.0.1:8771/",
        )
        self.assertEqual(requests[0][0], "http://127.0.0.1:8771/api/health")

        mismatch = dict(expected, runtime_revision="old-revision")
        self.assertIsNone(
            discover_shared_ui_url(
                "http://127.0.0.1:8771/",
                expected_health=mismatch,
                opener=opener,
            )
        )

    def test_duplicate_port_rejected_before_probe_and_snapshot_invalidated(self):
        from acquisition import AcquisitionConfig, AcquisitionManager
        from real_acquisition import ReadinessSnapshot
        config = self.config()
        config.dataset_schema = "new_collection_v12_0"
        config.interfaces[0]["data_mode"] = "microstrain"
        config.interfaces.append(dict(config.interfaces[0], id="other", role="pressure", driver="m3232_pressure"))
        config.selected_sensors = ["应变1", "应变2", "应变3", "应变4", "薄膜压力原始响应"]
        config.interface_channel_assignments = {}
        config.interfaces[-1]["data_mode"] = "raw_matrix"
        config = AcquisitionConfig(diagnostic_validation=True, **__import__("dataclasses").asdict(config))
        with tempfile.TemporaryDirectory() as root, patch("serial.Serial") as serial:
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            result = manager.test_connection(config)
            self.assertFalse(result["ok"])
            serial.assert_not_called()
        config = self.config()
        snapshot = ReadinessSnapshot.create(config, {"ok": True, "interfaces": [{"id": "esr", "physical_interface_id": "serial:COM91", "expected_channels": list(RAW_CHANNELS), "detected_channels": list(RAW_CHANNELS), "ok": True}]})
        config.interfaces[0]["data_mode"] = "microstrain"
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            snapshot.validate(config)

    def test_frame_v1_v2_preserve_adc_provenance(self):
        from real_acquisition import ChannelSample, UnifiedFrame
        from thermocouple64 import raw_catalog
        record = {"raw_hex": USB_DATA.hex(), "receive_sequence": 18, "host_wall_time": 1.0, "host_monotonic": 2.0}
        sample = ChannelSample(interface_id="esr", channel_name="应变1", value=1.0, source_sequence=18, metadata={"driver": "esr1001_usb_ttl", "latest_record": record, "calibration_digest": "fixture-digest"})
        frame = UnifiedFrame(0, 2.0, 1.0, {"应变1": sample})
        restored = UnifiedFrame.from_envelope(frame.to_envelope("capture", {"应变1": "με"}))
        self.assertEqual(restored.channels["应变1"].metadata["latest_record"], record)
        catalog = raw_catalog(["应变1"], {"应变1": "με"})
        payload = frame.to_envelope_v2(session_id="capture", mode_id="new_collection_v13_0", catalog=catalog)
        restored = UnifiedFrame.from_envelope_v2(payload, catalog=catalog, expected_session_id="capture", expected_mode_id="new_collection_v13_0")
        self.assertEqual(restored.channels["应变1"].metadata["adc_source"], record)

    def test_frontend_selected_usb_profile_and_readiness_fields(self):
        source = (Path(__file__).parent / "static/app.js").read_text(encoding="utf-8")
        profile = source[source.index("function applySensorTypeProfile("):source.index("function populatePredictionModelTypes(")]
        fingerprint = source[source.index("function readinessInterfaceFingerprint("):source.index("function hardwareConfigFingerprint(")]
        script = r'''
const hidden = {};
const elements = {
  ".interface-role": {value:"esr1001_strain"},
  ".interface-driver": {value:"esr1001_usb_ttl"},
  ".interface-endpoint": {value:"COM91"},
  ".interface-baudrate": {value:"9600"},
  ".interface-data-mode": {value:"raw_adc"},
  ".interface-profile-detail": {textContent:""},
  ".interface-slave-id": {value:"1",closest:()=>({classList:{toggle:(name,on)=>{hidden.slave=on}}})},
  ".interface-usb-options": {classList:{toggle:(name,on)=>{hidden.usb=on}}},
};
const row = {dataset:{},querySelector:key=>elements[key] || null};
function sensorTypeProfile(){return {id:"esr1001_strain",driver:"modbus_rtu_esr1001",baudrate:9600,channels:["应变1"],editable_driver:false};}
applySensorTypeProfile(row,false);
const item={driver:"esr1001_usb_ttl", data_mode:"raw_adc",channel_count:4,calibration_digest:"abc",bridge:"quarter",device_identity:{location:"test"}};
const changed = ["data_mode","channel_count","calibration_digest","bridge"].every(key=>JSON.stringify(readinessInterfaceFingerprint(item))!==JSON.stringify(readinessInterfaceFingerprint({...item,[key]:"changed"})));
console.log(JSON.stringify({driver:elements[".interface-driver"].value,baud:elements[".interface-baudrate"].value,endpoint:elements[".interface-endpoint"].value,detail:elements[".interface-profile-detail"].textContent,hidden,changed}));
'''
        result = subprocess.run(["node", "-e", profile + fingerprint + script], capture_output=True, text=True, encoding="utf-8", check=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["driver"], "esr1001_usb_ttl")
        self.assertEqual(payload["baud"], "115200")
        self.assertEqual(payload["endpoint"], "COM91")
        self.assertEqual(payload["hidden"], {"slave": True, "usb": False})
        self.assertIn("原生USB/TTL", payload["detail"])
        self.assertIn("CH4_ADC_signed", payload["detail"])
        self.assertNotIn("Modbus", payload["detail"])
        self.assertTrue(payload["changed"])

    def test_frontend_raw_schema_applies_one_consistent_usb_configuration(self):
        source = (Path(__file__).parent / "static/app.js").read_text(encoding="utf-8")
        preset = source[
            source.index("function applyDatasetInterfaceRequirements("):
            source.index("function configureDatasetSchema(")
        ]
        script = r'''
const makeRow = (role, driver, baudrate, dataMode) => {
  const nodes = {
    ".interface-role": {value: role},
    ".interface-enabled": {checked: true},
    ".interface-driver": {value: driver},
    ".interface-baudrate": {value: baudrate},
    ".interface-data-mode": {value: dataMode},
  };
  return {nodes, querySelector: (key) => nodes[key] || null};
};
const plc = makeRow("plc", "modbus_tcp", "9600", "raw_adc");
const esr = makeRow("esr1001_strain", "modbus_rtu_esr1001", "9600", "microstrain");
const rows = [plc, esr];
const controls = {
  processingMode: {value: "prediction_warning"},
  mysqlEnabled: {checked: true},
  mysqlLocalEnabled: {checked: true},
  acquisitionMode: {value: "real"},
  interfacePanel: {querySelectorAll: () => rows},
};
let profileApplications = 0;
let routeRefreshes = 0;
function applySensorTypeProfile() { profileApplications += 1; }
function refreshInterfaceCardsForSelection() { routeRefreshes += 1; }
applyDatasetInterfaceRequirements("esr1001_raw_adc");
console.log(JSON.stringify({
  processingMode: controls.processingMode.value,
  mysqlEnabled: controls.mysqlEnabled.checked,
  mysqlLocalEnabled: controls.mysqlLocalEnabled.checked,
  plcEnabled: plc.nodes[".interface-enabled"].checked,
  esrEnabled: esr.nodes[".interface-enabled"].checked,
  driver: esr.nodes[".interface-driver"].value,
  baudrate: esr.nodes[".interface-baudrate"].value,
  dataMode: esr.nodes[".interface-data-mode"].value,
  profileApplications,
  routeRefreshes,
}));
'''
        result = subprocess.run(
            ["node", "-e", preset + script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["processingMode"], "capture_only")
        self.assertFalse(payload["mysqlEnabled"])
        self.assertFalse(payload["mysqlLocalEnabled"])
        self.assertFalse(payload["plcEnabled"])
        self.assertTrue(payload["esrEnabled"])
        self.assertEqual(payload["driver"], "esr1001_usb_ttl")
        self.assertEqual(payload["baudrate"], "115200")
        self.assertEqual(payload["dataMode"], "raw_adc")
        self.assertEqual(payload["profileApplications"], 1)
        self.assertEqual(payload["routeRefreshes"], 1)

    def test_frontend_shared_plan_restores_empty_desktop_storage(self):
        source = (Path(__file__).parent / "static/app.js").read_text(encoding="utf-8")
        functions = source[source.index("function datasetInterfacePreferenceKey("):source.index("function switchDatasetInterfacePreferences(")]
        script = r'''
const assert=require("node:assert/strict");
const normal="new_collection_v13_0";
const expected={active:normal,plans:{[normal]:{interfaces:[{id:"esr",role:"esr1001_strain",driver:"esr1001_usb_ttl",baudrate:115200,enabled:true,data_mode:"raw_adc",channels:["CH1_ADC_signed"]}],processingMode:"capture_only"}}};
const state={accessRole:"local_admin",bootstrap:{acquisition:{schemas:[{id:normal}]}}};
const controls={acquisitionMode:{value:"real"},processingMode:{value:"capture_only"}};
const localStorage={getItem:()=>null,setItem:()=>{throw Error("storage blocked")}};
let queued,posted;
const window={clearTimeout:()=>{},setTimeout:f=>{queued=f;return 1}};
function usesLocalCaptureHelper(){return false}
function toast(message){throw Error(message)}
function interfaceConfigs(){return {interfaces:expected.plans[normal].interfaces}}
async function fetch(path){assert.equal(path,"/api/admin/acquisition/interface-plans");return {ok:true,json:async()=>({preferences:expected,revision:'first'})}}
let nextRevision='first', conflict=false;
async function postJson(path,body){assert.equal(path,"/api/admin/acquisition/interface-plans");assert.equal(body.expected_revision,nextRevision);if(conflict)throw Error('interface_plan_conflict');posted=structuredClone(body);nextRevision+='-next';return {ok:true,revision:nextRevision}}
(async()=>{
 await loadSharedDatasetInterfacePreferences();
 loadDatasetInterfacePreferences();
 assert.equal(state.restoredInterfaceSchema,normal);
 assert.deepEqual(state.interfaceCatalog,expected.plans[normal].interfaces);
 assert.equal(state.hardwareCheck,undefined);
 state.activeInterfaceSchema=normal;
 saveDatasetInterfacePreferences();
 await queued();
 assert.equal(posted.plans[normal].interfaces[0].driver,"esr1001_usb_ttl");
 assert.deepEqual(posted.plans[normal].interfaces[0].channels,["CH1_ADC_signed"]);
 assert.equal(state.interfacePlanRevision,'first-next');
 saveDatasetInterfacePreferences();
 await queued();
 assert.equal(state.interfacePlanRevision,'first-next-next');
 conflict=true;
 const messages=[];
 toast=message=>messages.push(message);
 saveDatasetInterfacePreferences();
 await queued();
 assert.equal(state.interfacePlanConflict,true);
 assert.equal(messages.length,1);
 queued=null;
 queueSharedDatasetInterfacePreferences(expected);
 assert.equal(queued,null);
 state.accessRole="authorized";queued=null;
 queueSharedDatasetInterfacePreferences(expected);
 assert.equal(queued,null);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        subprocess.run(["node", "-e", functions + script], check=True, capture_output=True)

    def test_frontend_plan_roundtrips_reload_and_unbound_rediscovery(self):
        source = (Path(__file__).parent / "static/app.js").read_text(encoding="utf-8")
        functions = source[source.index("function datasetInterfacePreferenceKey("):source.index("function configureDatasetSchema(")]
        functions += source[source.index("function mergeRememberedInterfaceSelections("):source.index("async function discoverInterfaces(")]
        script = r'''
const assert = require("node:assert/strict");
const normal = "new_collection_v13_0", diagnostic = "esr1001_raw_adc";
let state = {bootstrap:{acquisition:{schemas:[{id:normal},{id:diagnostic}]}}};
const controls = {acquisitionMode:{value:"real"}, processingMode:{value:"capture_only"}, mysqlEnabled:{checked:true}, mysqlLocalEnabled:{checked:false}};
let items = [{id:"plc",role:"plc",driver:"modbus_tcp",endpoint:"192.168.125.5:502",enabled:true},
  {id:"abb",role:"robot",driver:"abb_robot",endpoint:"192.168.125.1",enabled:true},
  {id:"esr",role:"esr1001_strain",driver:"esr1001_usb_ttl",endpoint:"COM91",enabled:true,data_mode:"raw_adc",baudrate:115200},
  {id:"uvc",role:"thermal_uvc",enabled:false}];
function rows(){ return items.map(item=>({querySelector:key=>{
  const field={".interface-role":"role",".interface-enabled":"enabled",".interface-driver":"driver",".interface-baudrate":"baudrate",".interface-data-mode":"data_mode"}[key];
  if (!field) return null;
  return {get value(){return item[field]},set value(v){item[field]=v},get checked(){return item[field]},set checked(v){item[field]=v}};
}})); }
controls.interfacePanel={querySelectorAll:rows};
const storage = new Map();
const localStorage={getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)};
function interfaceConfigs(){return {interfaces:structuredClone(items)}}
function renderInterfacePanel(config){items=structuredClone(config)}
function autoAssignPhysicalInterfaces(config){return config}
function usesLocalCaptureHelper(){return false}
function markHardwareCheckStale(){state.hardwareCheck=null}
function applySensorTypeProfile(){}
function refreshInterfaceCardsForSelection(){}
function isModernSchemaId(id){return id.startsWith("new_collection_")}
switchDatasetInterfacePreferences(normal);
const expected=structuredClone(items);
switchDatasetInterfacePreferences(diagnostic);
assert.equal(items[0].enabled,false);
assert.equal(controls.mysqlEnabled.checked,false);
items[2].endpoint="COM92";
switchDatasetInterfacePreferences(normal);
assert.deepEqual(items,expected);
assert.equal(controls.mysqlEnabled.checked,true);
switchDatasetInterfacePreferences(diagnostic);
assert.equal(items[2].endpoint,"COM92");
assert.equal(items[2].driver,"esr1001_usb_ttl");
saveDatasetInterfacePreferences();
state={bootstrap:{acquisition:{schemas:[{id:normal},{id:diagnostic}]}}};
loadDatasetInterfacePreferences();
switchDatasetInterfacePreferences(state.restoredInterfaceSchema);
assert.equal(items[2].endpoint,"COM92");
switchDatasetInterfacePreferences(normal);
assert.deepEqual(items,expected);
state.restoringInterfacePreferences=false;
const merged=mergeRememberedInterfaceSelections([{id:"esr",driver:"modbus_rtu_esr1001",enabled:false},{id:"uvc",enabled:true}]);
assert.equal(merged[0].driver,"esr1001_usb_ttl");
assert.equal(merged[0].enabled,true);
assert.equal(merged[1].enabled,false);
const sensors=effectiveDatasetSensors({id:normal,sensors:["温度","应变1","应变2","应变3","应变4"]});
assert.deepEqual(sensors,["温度","CH1_ADC_signed","CH2_ADC_signed","CH3_ADC_signed","CH4_ADC_signed"]);
console.log("ok");
'''
        result = subprocess.run(["node"], input=functions + script, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frontend_labels_usb_failures_in_chinese(self):
        source = (Path(__file__).parent / "static/app.js").read_text(encoding="utf-8")
        label_function = source[
            source.index("function hardwareStateLabel("):
            source.index("function hardwareInterfaceDetail(")
        ]
        script = r'''
const codes = [
  "port_not_present", "port_access_denied", "port_busy", "device_disconnected",
  "device_identity_mismatch", "usb_parameters_invalid", "usb_parameters_timeout",
  "usb_mode_invalid", "usb_start_timeout", "usb_stop_timeout", "usb_stop_failed",
  "usb_probe_timeout", "usb_no_valid_frame", "usb_frame_invalid",
  "usb_consumer_overflow", "usb_overflow", "raw_writer_failed",
  "raw_writer_overflow", "calibration_missing", "calibration_invalid",
  "calibration_mismatch",
];
console.log(JSON.stringify(Object.fromEntries(codes.map(code => [code, hardwareStateLabel(code)]))));
'''
        result = subprocess.run(
            ["node", "-e", label_function + script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        labels = json.loads(result.stdout)

        self.assertEqual(set(labels), {
            "port_not_present", "port_access_denied", "port_busy", "device_disconnected",
            "device_identity_mismatch", "usb_parameters_invalid", "usb_parameters_timeout",
            "usb_mode_invalid", "usb_start_timeout", "usb_stop_timeout", "usb_stop_failed",
            "usb_probe_timeout", "usb_no_valid_frame", "usb_frame_invalid",
            "usb_consumer_overflow", "usb_overflow", "raw_writer_failed",
            "raw_writer_overflow", "calibration_missing", "calibration_invalid",
            "calibration_mismatch",
        })
        self.assertTrue(all("接口失败（" not in label for label in labels.values()))
        self.assertIn("拒绝", labels["port_access_denied"])
        self.assertIn("四通道", labels["usb_no_valid_frame"])
        self.assertIn("标定待确认", labels["calibration_missing"])

    def test_protocol_docs_distinguish_counts_and_crc(self):
        readme = (Path(__file__).parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("115200-8-N-1", readme)
        self.assertIn("没有 CRC", readme)
        self.assertIn("adc_counts", readme)
        self.assertIn("主CSV按页面显示保存", readme)
        self.assertIn("候选换算不代表标定确认，不改变模型和MySQL的准入条件", readme)

    def test_multiple_candidates_are_not_automatically_claimed(self):
        from visualization_app.test_usb_interface_candidates import run_candidate_script
        result = run_candidate_script(r'''
state.physicalInterfaces = [
  {id:"serial:COM91",kind:"serial",endpoint:"COM91",vid:6790,pid:29987,detected:true},
  {id:"serial:COM92",kind:"serial",endpoint:"COM92",vid:6790,pid:29987,detected:true},
  {id:"hid:mouse",kind:"usb_hid",endpoint:"mouse",detected:true},
  {id:"serial:COM93",kind:"serial",endpoint:"COM93",description:"Bluetooth",detected:true},
];
const config = {id:"esr",role:"esr1001_strain",driver:"esr1001_usb_ttl",endpoint:"",enabled:false};
autoAssignPhysicalInterfaces([config]);
console.log(JSON.stringify(config));
''')
        self.assertFalse(result["enabled"])
        self.assertEqual(result["endpoint"], "")
        self.assertNotIn("physical_interface_id", result)

    def test_existing_template_can_project_usb_diagnostic_configuration(self):
        from pre_sensor_config import validate_pre_sensor_config
        root = Path(__file__).resolve().parents[1]
        payload = json.loads((root / "pre_sensor/config/local_direct.json").read_text(encoding="utf-8"))
        payload["dataset_schema"] = "esr1001_raw_adc"
        interface = next(item for item in payload["interfaces"] if item["role"] == "esr1001_strain")
        interface.update(driver="esr1001_usb_ttl", baudrate=115200, data_mode="raw_adc")
        payload["interfaces"] = [interface]
        payload["channels"] = [{"name": name, "unit": "adc_counts", "interface_id": interface["id"]} for name in RAW_CHANNELS]
        config = validate_pre_sensor_config(payload, references={"AFP_INTERFACE_ESR1001": "COM91", "AFP_SAVE_ROOT": "D:/AFP_Capture", "AFP_LOG_ROOT": "D:/AFP_Capture"})
        self.assertEqual(config.selected_sensors, list(RAW_CHANNELS))
        self.assertEqual(config.interfaces[0]["baudrate"], 115200)

    def test_normal_template_preserves_independent_adc_contract(self):
        from pre_sensor_config import validate_pre_sensor_config, PreSensorConfigError
        from harness.tests.test_pre_sensor_config import _hardware_facts
        root = Path(__file__).resolve().parents[1]
        payload = json.loads((root / "pre_sensor/config/local_direct.json").read_text(encoding="utf-8"))
        payload["interfaces"][0]["hardware_facts"] = _hardware_facts()
        interface = next(item for item in payload["interfaces"] if item["role"] == "esr1001_strain")
        interface.update(driver="esr1001_usb_ttl", baudrate=115200, data_mode="raw_adc")
        references = {item["endpoint_ref"]: "fixture-device" for item in payload["interfaces"]}
        references.update(AFP_SAVE_ROOT="D:/AFP_Capture", AFP_LOG_ROOT="D:/AFP_Capture", AFP_INTERFACE_ESR1001="COM91")
        with self.assertRaisesRegex(PreSensorConfigError, "channel contract"):
            validate_pre_sensor_config(payload, references=references)
        for channel in payload["channels"]:
            if channel["interface_id"] == interface["id"]:
                channel.update(name=RAW_CHANNELS[int(channel["name"][-1])-1], unit="adc_counts")
        config = validate_pre_sensor_config(payload, references=references)
        self.assertEqual(len(config.selected_sensors), 77)
        self.assertTrue(set(RAW_CHANNELS).issubset(config.selected_sensors))
        self.assertNotIn("应变1", config.selected_sensors)

    def test_parameter_only_probe_does_not_report_ready(self):
        from esr1001_usb import probe_usb
        stub = SerialStub()
        def parameter_only(data):
            stub.writes.append(data)
            stub.buffer.extend({QUERY: USB_PARAMETERS, START: USB_START_RESPONSE, STOP: USB_STOP_RESPONSE}.get(data, b""))
        stub.write = parameter_only
        driver = Esr1001UsbDriver("COM91", serial_factory=lambda *a, **k: stub, inventory=lambda: [SimpleNamespace(device="COM91")], command_timeout=0.01)
        result = probe_usb(self.config().interfaces[0], driver_factory=lambda *a, **k: driver)
        self.assertGreaterEqual(result["elapsed_seconds"], 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stages"]["protocol_identified"]["state"], "passed")
        self.assertEqual(result["stages"]["channels_complete"]["state"], "failed")
        self.assertEqual(stub.writes, [QUERY, START, STOP])

    def test_usb_reconnect_preserves_gap_and_does_not_block_other_worker(self):
        from local_direct_acquisition import LocalDirectAdapter
        from real_acquisition import ChannelSampleCache, FrameAssembler
        serials = [StreamingSerial(), StreamingSerial()]
        serials[0].disconnected = False
        original = serials[0].read
        def failing_read(size):
            if serials[0].disconnected:
                raise OSError("USB fixture disconnected")
            return original(size)
        serials[0].read = failing_read
        opened = []
        def serial_factory(*a, **kw):
            current = serials[min(len(opened), 1)]
            opened.append(current)
            return current
        driver = Esr1001UsbDriver("COM91", serial_factory=serial_factory, inventory=lambda: [SimpleNamespace(device="COM91")], command_timeout=0.01)
        cache = ChannelSampleCache()
        adapter = LocalDirectAdapter("esr", driver, RAW_CHANNELS, cache, reconnect_backoff_seconds=0.3)
        class OtherDriver:
            def open(self): pass
            def close(self): pass
            def read_sample(self): return {"温度": 20.0}
        other = LocalDirectAdapter("other", OtherDriver(), ["温度"], cache)
        try:
            adapter.start(); other.start()
            self.assertTrue(adapter.opened.wait(4))
            deadline = monotonic() + 1
            while driver.sequence < 3 and monotonic() < deadline:
                time.sleep(0.005)
            serials[0].disconnected = True
            initial_other = other.source_sequence
            time.sleep(0.27)
            frame = FrameAssembler(RAW_CHANNELS, 10.0, start_monotonic=monotonic(), start_wall_time=time.time()).assemble(cache)
            self.assertTrue(all(s.value is None for s in frame.channels.values()))
            self.assertGreater(other.source_sequence, initial_other)
            deadline = monotonic() + 4
            while driver.connection_generation < 2 and monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(driver.connection_generation, 2)
            deadline = monotonic() + 2
            while not driver.gaps and monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(driver.gaps)
        finally:
            adapter.stop(2); other.stop(2)
        self.assertFalse(any(s.is_open for s in serials))

    def test_usb_reconnect_window_outlives_attempt_limit_and_requires_new_observation(self):
        from local_direct_acquisition import LocalDirectAdapter
        from real_acquisition import ChannelSampleCache

        class DisconnectableUsbDriver:
            quality_metadata = {"driver": "esr1001_usb_ttl"}

            def __init__(self):
                self.disconnected = threading.Event()
                self.open_calls = 0

            def open(self):
                self.open_calls += 1

            def read_sample(self):
                if self.disconnected.is_set():
                    raise OSError("USB fixture disconnected")
                return {name: float(self.open_calls) for name in RAW_CHANNELS}

            def close(self):
                return None

        driver = DisconnectableUsbDriver()
        cache = ChannelSampleCache()
        adapter = LocalDirectAdapter(
            "esr",
            driver,
            RAW_CHANNELS,
            cache,
            poll_interval_seconds=0.001,
            minimum_poll_seconds=0.001,
            reconnect_backoff_seconds=0.01,
            max_reconnect_backoff_seconds=0.02,
            max_reconnect_attempts=2,
        )
        self.assertEqual(adapter.status()["reconnect_window_seconds"], 60.0)
        adapter.start()
        try:
            deadline = monotonic() + 0.5
            while adapter.source_sequence < 1 and monotonic() < deadline:
                time.sleep(0.005)
            self.assertGreaterEqual(adapter.source_sequence, 1)
            driver.disconnected.set()
            deadline = monotonic() + 0.5
            while adapter.reconnect_attempts < 3 and monotonic() < deadline:
                time.sleep(0.005)
            self.assertGreaterEqual(adapter.reconnect_attempts, 3, adapter.status())
            self.assertTrue(adapter.status()["reconnect_window_active"])
            sequence_before_recovery = adapter.source_sequence

            driver.disconnected.clear()
            deadline = monotonic() + 0.5
            while adapter.source_sequence <= sequence_before_recovery and monotonic() < deadline:
                time.sleep(0.005)
            status = adapter.status()
            self.assertGreater(adapter.source_sequence, sequence_before_recovery, status)
            self.assertEqual(status["consecutive_reconnect_attempts"], 0)
            self.assertFalse(status["reconnect_window_active"])
            self.assertEqual(
                {cache.latest(name).source_sequence for name in RAW_CHANNELS},
                {adapter.source_sequence},
            )
        finally:
            adapter.stop(0.5)

    def test_usb_reconnect_window_expires_with_rate_limited_retries(self):
        from local_direct_acquisition import LocalDirectAdapter
        from real_acquisition import ChannelSampleCache

        class FailingAfterFirstObservation:
            quality_metadata = {"driver": "esr1001_usb_ttl"}

            def __init__(self):
                self.delivered = False

            def open(self):
                return None

            def read_sample(self):
                if not self.delivered:
                    self.delivered = True
                    return {name: 1.0 for name in RAW_CHANNELS}
                raise OSError("USB fixture remains disconnected")

            def close(self):
                return None

        adapter = LocalDirectAdapter(
            "esr",
            FailingAfterFirstObservation(),
            RAW_CHANNELS,
            ChannelSampleCache(),
            poll_interval_seconds=0.001,
            minimum_poll_seconds=0.001,
            reconnect_backoff_seconds=0.0,
            max_reconnect_backoff_seconds=0.0,
            max_reconnect_attempts=1,
            reconnect_window_seconds=0.16,
        )
        started = monotonic()
        adapter.start()
        try:
            deadline = started + 1.0
            while adapter.status()["running"] and monotonic() < deadline:
                time.sleep(0.005)
            elapsed = monotonic() - started
            status = adapter.status()
            self.assertEqual(status["state"], "failed", status)
            self.assertGreaterEqual(elapsed, 0.12)
            self.assertLess(elapsed, 1.0)
            self.assertGreater(status["reconnect_attempts"], 1)
            self.assertLessEqual(status["reconnect_attempts"], 5)
            self.assertFalse(status["reconnect_window_active"])
            self.assertEqual(status["reconnect_window_remaining_seconds"], 0.0)
        finally:
            adapter.stop(0.5)

    def test_calibrated_csv_mysql_summary_and_source_rows(self):
        from acquisition import AcquisitionConfig, AcquisitionManager
        binding = {"location": "test"}
        calibration = {"version": 1, "unit": "με", "source": "approved-test-fixture", "device_binding": binding, "bridge": "quarter", "gain_code": 6, "channels": [{"K": 0.01, "B": -2}] * 4}
        interface = dict(self.config().interfaces[0], data_mode="microstrain", calibration_profile=calibration, bridge="quarter", device_identity=binding)
        saved = []
        class Store:
            def __init__(self, settings): pass
            def preflight(self, **kwargs): return {"ok": True}
            def save_layer(self, config, **kwargs):
                rows = list(kwargs["rows"])
                saved.append({"rows": rows, "summary": kwargs["summary"]})
                return {"ok": True, "saved_rows": len(rows)}
        with tempfile.TemporaryDirectory() as root, patch("serial.Serial", side_effect=StreamingSerial), patch("serial.tools.list_ports.comports", return_value=[SimpleNamespace(device="COM91", location="test")]), patch("acquisition.MySQLCaptureStore", Store):
            config = AcquisitionConfig(acquisition_mode="real", dataset_schema="new_collection_v12_0", interfaces=[interface], processing_mode="capture_only", save_root=root, mysql_enabled=True)
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            self.assertTrue(manager.test_connection(config)["ok"])
            manager.start(config)
            time.sleep(0.25)
            result = manager.stop()
            self.assertTrue(result["finalization_complete"], result)
            self.assertTrue(saved)
            with manager.raw_path.open(encoding="gb18030", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), len(saved[0]["rows"]))
            for row in rows:
                self.assertEqual(row["应变1"], "685.9")
                self.assertNotIn("CH1_ADC_signed", row)
            evidence = saved[0]["summary"]["data_quality"]["esr1001_usb"]["esr"]
            self.assertTrue(evidence["calibration_digest"])
            self.assertGreater(evidence["source_frames"], len(rows))
            self.assertEqual(evidence["saved_source_rows"], evidence["source_frames"])

    def test_joint_raw_adc_csv_mysql_and_source_evidence(self):
        from acquisition import AcquisitionConfig, AcquisitionManager
        from test_plc_abb_uvc_fault_isolation import ContinuousDriver
        from contextlib import ExitStack
        interfaces = [
            dict(id="plc", role="plc", driver="modbus_tcp", endpoint="192.168.125.5:502", physical_interface_id="ethernet:test", physical_interface_kind="ethernet", physical_verified=True),
            dict(id="abb", role="robot", driver="abb_robot", endpoint="192.168.125.1", physical_interface_id="ethernet:test", physical_interface_kind="ethernet", physical_verified=True),
            self.config().interfaces[0],
        ]
        network_values = {"温度": 102.0, "压力": 35.0, "张力": 2.0, "ABB_X": 412.0, "ABB_Y": 2205.0, "ABB_Z": 629.0, "线速度": 0.0}
        saved = []
        class Store:
            def __init__(self, settings): pass
            def preflight(self, **kwargs): return {"ok": True}
            def save_layer(self, config, **kwargs):
                rows = list(kwargs["rows"])
                saved.append({"rows": rows, "summary": kwargs["summary"]})
                return {"ok": True, "saved_rows": len(rows)}
        route = {"ok": True, "status": "network_path_ready", "actual_source_address": "192.168.125.209", "actual_interface_id": "ethernet:test"}
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            for target, value in {
                "acquisition.validate_network_path": route, "acquisition.require_network_path": route,
                "acquisition.ModbusTcpDriver": ContinuousDriver({k: network_values[k] for k in ("温度", "压力", "张力")}),
                "acquisition.AbbRobotDriver": ContinuousDriver({k: network_values[k] for k in ("ABB_X", "ABB_Y", "ABB_Z", "线速度")}),
                "serial.tools.list_ports.comports": [SimpleNamespace(device="COM91", location="test")],
            }.items():
                stack.enter_context(patch(target, return_value=value))
            stack.enter_context(patch("serial.Serial", side_effect=StreamingSerial))
            stack.enter_context(patch("acquisition.MySQLCaptureStore", Store))
            config = AcquisitionConfig(acquisition_mode="real", dataset_schema="new_collection_v13_0", interfaces=interfaces, processing_mode="capture_only", save_root=root, mysql_enabled=True)
            self.assertEqual(set(config.selected_sensors), set(network_values) | set(RAW_CHANNELS))
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            checked = manager.test_connection(config)
            self.assertTrue(checked["ok"], checked)
            manager.start(config)
            try:
                time.sleep(0.45)
            finally:
                result = manager.stop()
            self.assertTrue(result["finalization_complete"], result)
            with manager.raw_path.open(encoding="gb18030", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertGreater(len(rows), 2)
            self.assertEqual(len(rows), result["sample_count"])
            self.assertEqual(len(rows), len(saved[0]["rows"]))
            for row in rows:
                for name, expected in network_values.items():
                    self.assertGreaterEqual(float(row[name]), expected)
                    self.assertLess(float(row[name]), expected + 1)
                for name, expected in zip(RAW_CHANNELS, USB_EXPECTED_ADC):
                    self.assertEqual(float(row[name]), expected)
                self.assertNotIn("应变1", row)
            evidence = saved[0]["summary"]["data_quality"]["esr1001_usb"]["esr"]
            self.assertGreater(evidence["source_frames"], len(rows))
            self.assertEqual(evidence["source_frames"], evidence["saved_source_rows"])
            self.assertFalse(evidence.get("calibration_digest"))

    def test_helper_raw_capture_batch_preserves_frame_and_units(self):
        from acquisition import AcquisitionManager
        from local_capture_agent import LocalCaptureAgent
        from local_capture_helper_entry import helper_capabilities
        from edge_capture import RemoteAcquisitionMirror
        caps = helper_capabilities()
        self.assertTrue(caps["unified_frame_v2"])
        for name in ("esr1001_usb_ttl", "esr1001_raw_adc_capture", "esr1001_calibration_v1"):
            self.assertTrue(caps[name])
        with tempfile.TemporaryDirectory() as root, patch("serial.Serial", side_effect=StreamingSerial), patch("serial.tools.list_ports.comports", return_value=[SimpleNamespace(device="COM91")]):
            config = self.config(save_root=root, real_acquisition_mode="remote_helper", helper_session_id="fixture", execution_host="helper_local", helper_capabilities=caps)
            agent = LocalCaptureAgent(AcquisitionManager(Path(root), recovery_scan=False), simulation_cache_root=Path(root) / "cache")
            self.assertTrue(agent.check_capture(config)["ok"])
            agent.start_capture(config)
            try:
                time.sleep(0.25)
                batch = agent.next_sample_batch()
                self.assertTrue(batch["frames"])
                frame = batch["frames"][-1]
                self.assertEqual(frame["channels"][RAW_CHANNELS[0]]["unit"], "adc_counts")
                record = frame["channels"][RAW_CHANNELS[0]]["metadata"]["latest_record"]
                self.assertEqual(decode_adc(bytes.fromhex(record["raw_hex"])), USB_EXPECTED_ADC)
                mirror = RemoteAcquisitionMirror()
                mirror.configure_contract(RAW_CHANNELS, {name: "adc_counts" for name in RAW_CHANNELS})
                accepted = mirror.ingest(batch)
                self.assertTrue(accepted["ok"], accepted)
                self.assertEqual(mirror.frame_envelopes()[-1]["channels"][RAW_CHANNELS[0]]["metadata"]["latest_record"], record)
                self.assertTrue(agent.ack_sample_batch(batch["capture_uuid"], batch["sequence"]))
            finally:
                stopped = agent.stop_capture()
            self.assertTrue(stopped["finalization_complete"], stopped)


if __name__ == "__main__":
    unittest.main()
