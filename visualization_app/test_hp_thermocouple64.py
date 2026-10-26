from __future__ import annotations

import struct
import sys
import time
import unittest
import threading
import tempfile
import json
import csv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acquisition
from hp_thermocouple64 import (
    HpProtocolError, HpThermocouple64Driver, append_crc, decode_channel,
    parse_response, read_request,
    decode_temperature, settings_binding,
)
from local_direct_acquisition import LocalDirectAdapter
from real_acquisition import ChannelQuality, ChannelSampleCache, FrameAssembler, readiness_config_fingerprint, UnifiedFrame
from thermocouple64 import THERMOCOUPLE64_CHANNELS, strict_group_means


def channel_record(value: float, *, unit: int = 0, enabled: bool = True, sensor_type: int = 0) -> list[int]:
    high, low = struct.unpack(">HH", struct.pack(">f", value))
    return [0, unit, sensor_type, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            0 if enabled else 1, 0, 0, 0, low, high]


class InstrumentSerial:
    """In-memory instrument emulator; values are synthetic protocol fixtures."""

    def __init__(self, *, fragment: int = 7) -> None:
        self.records = [channel_record(20 + index / 4) for index in range(64)]
        self.fragment = fragment
        self.pending = b""
        self.writes: list[bytes] = []
        self.closed = False
        self.corrupt_start: int | None = None
        self.truncate_start: int | None = None
        self.exception: bytes | None = None
        self.short_only = False
        self.metadata_exception = False
        self.responses: dict[int, bytes] = {}

    @property
    def in_waiting(self):
        return len(self.pending)

    def reset_input_buffer(self) -> None:
        self.pending = b""

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        address, function, start, count = struct.unpack(">BBHH", payload[:-2])
        if function != 3 or count > 125 or start + count > 1280:
            raise AssertionError("unexpected acquisition request")
        if self.short_only and (start % 20, count) not in ((1, 2), (14, 1), (18, 2)):
            raise AssertionError("仅支持逐通道短请求")
        registers = sum(self.records, [])[start:start + count]
        self.pending = self.exception or append_crc(
            bytes([address, 3, count * 2]) + struct.pack(f">{count}H", *registers)
        )
        if self.metadata_exception and start % 20 != 18:
            self.pending = append_crc(bytes([address, 0x83, 2]))
        if start in self.responses:
            self.pending = self.responses[start]
        if start == self.corrupt_start:
            self.pending = self.pending[:-1] + bytes([self.pending[-1] ^ 1])
        if start == self.truncate_start:
            self.pending = self.pending[:10]
        return len(payload)

    def flush(self) -> None:
        pass

    def read(self, size: int) -> bytes:
        chunk, self.pending = self.pending[:min(size, self.fragment)], self.pending[min(size, self.fragment):]
        return chunk

    def close(self) -> None:
        self.closed = True


class HpThermocouple64Tests(unittest.TestCase):
    def test_manual_crc_vectors_and_word_order(self) -> None:
        self.assertEqual(read_request(1, 0, 4), bytes.fromhex("01 03 00 00 00 04 44 09"))
        self.assertEqual(parse_response(bytes.fromhex("01 03 08 00 01 00 00 00 08 00 02 85 14"), 1, 4), (1, 0, 8, 2))
        record = [0] * 20
        record[18:20] = [0xE666, 0x42F6]
        self.assertAlmostEqual(decode_channel(tuple(record))[0], 123.45, places=4)
        for address in (0, 248):
            with self.assertRaises(ValueError):
                read_request(address, 0, 2)
        with self.assertRaises(ValueError):
            read_request(1, 0, 128)

    def test_all_64_channels_in_eleven_fragmented_transactions(self) -> None:
        port = InstrumentSerial(fragment=1)
        factory = unittest.mock.Mock(return_value=port)
        driver = HpThermocouple64Driver("COM17", slave_id=7, serial_factory=factory, read_profile="full_record_block")
        driver.open()
        try:
            values = driver.read_sample()
            self.assertEqual(list(values), list(THERMOCOUPLE64_CHANNELS))
            self.assertEqual(values, {f"温度{i + 1}": 20 + i / 4 for i in range(64)})
            spans = [struct.unpack(">BBHH", p[:-2])[2:] for p in port.writes]
            self.assertEqual(spans, [(i * 120, 120) for i in range(10)] + [(1200, 80)])
            self.assertTrue(all(p[0] == 7 for p in port.writes))
            self.assertTrue(driver.protocol_verified)
            self.assertEqual(driver.observation_count, 1)
            factory.assert_called_once_with(port="COM17", baudrate=115200, bytesize=8, parity="N", stopbits=1, timeout=0, write_timeout=0.5)
            port.records[63] = channel_record(-12.5)
            self.assertEqual(driver.read_sample()["温度64"], -12.5)
            self.assertEqual(driver.observation_count, 2)
        finally:
            driver.close()
        self.assertTrue(port.closed)
        self.assertFalse(driver.protocol_verified)

    def test_channel_units_and_faults_do_not_poison_other_channels(self) -> None:
        port = InstrumentSerial()
        port.records[0] = channel_record(32, unit=2)
        port.records[1] = channel_record(273.15, unit=1)
        port.records[2] = channel_record(500, enabled=False)
        port.records[3] = channel_record(float("nan"))
        port.records[4] = channel_record(float("inf"))
        port.records[5] = channel_record(10, unit=3)
        port.records[6] = channel_record(10, sensor_type=15)
        driver = HpThermocouple64Driver("COM17", serial_factory=lambda **_: port)
        driver.open()
        try:
            values = driver.read_sample()
            self.assertEqual(values["温度1"], 0)
            self.assertAlmostEqual(values["温度2"], 0, places=4)
            self.assertTrue(all(values[f"温度{i}"] is None for i in range(3, 8)))
            self.assertEqual(values["温度64"], 35.75)
            self.assertEqual(driver.quality_metadata["channel_errors"]["温度3"], "channel_disabled")
            self.assertIsNone(strict_group_means(values)["热电偶均值1"]["value"])
            self.assertEqual(strict_group_means(values)["热电偶均值8"]["value"], 34.875)
        finally:
            driver.close()

    def test_corrupt_or_incomplete_final_block_never_publishes_partial_scan(self) -> None:
        for failure, code in (("corrupt_start", "crc"), ("truncate_start", "timeout")):
            with self.subTest(failure=failure):
                port = InstrumentSerial()
                driver = HpThermocouple64Driver("COM17", timeout=0.01, serial_factory=lambda **_: port, read_profile="full_record_block")
                driver.open()
                driver.read_sample()
                setattr(port, failure, 1200)
                with self.assertRaises(HpProtocolError) as caught:
                    driver.read_sample()
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(driver.protocol_verified)
                self.assertEqual(driver.observation_count, 1)
                driver.close()

    def test_crc_identity_function_count_and_vendor_exceptions(self) -> None:
        for frame, code in (
            (append_crc(b"\x02\x03\x02\x00\x00"), "address"),
            (append_crc(b"\x01\x04\x02\x00\x00"), "function"),
            (append_crc(b"\x01\x03\x04\x00\x00\x00\x00"), "byte_count"),
            (append_crc(b"\x01\x83\x02\x10"), "device_exception"),
            (append_crc(b"\x01\x83\x02"), "device_exception"),
        ):
            with self.assertRaises(HpProtocolError) as caught:
                parse_response(frame, 1, 1)
            self.assertEqual(caught.exception.code, code)
        for payload in (b"\x01\x83\x02\x10", b"\x01\x83\x02"):
            port = InstrumentSerial(fragment=1)
            port.exception = append_crc(payload)
            driver = HpThermocouple64Driver("COM17", serial_factory=lambda **_: port)
            with self.assertRaisesRegex(HpProtocolError, "device_exception"):
                driver.open()
            self.assertTrue(port.closed)
        frame = append_crc(b"\x01\x03\x02\x00\x00")
        with self.assertRaisesRegex(HpProtocolError, "crc"):
            parse_response(frame[:-1] + bytes([frame[-1] ^ 1]), 1, 1)

    def test_real_config_migrates_pending_driver_and_rejects_address_zero(self) -> None:
        interface = acquisition.default_capture_interfaces()[0]
        interface.update(endpoint="COM17", physical_interface_id="serial:COM17", physical_interface_kind="serial", driver="thermocouple64_protocol_pending", slave_id=7)
        config = acquisition.AcquisitionConfig(acquisition_mode="real", dataset_schema="new_collection_v13_0", interfaces=[interface], selected_sensors=list(THERMOCOUPLE64_CHANNELS))
        self.assertEqual(config.interfaces[0]["driver"], "huipu_tc64_modbus_rtu")
        self.assertEqual(config.interfaces[0]["slave_id"], 7)
        self.assertEqual(config.interface_channel_assignments[interface["id"]], list(THERMOCOUPLE64_CHANNELS))
        before = readiness_config_fingerprint(config)
        config.interfaces[0]["slave_id"] = 8
        self.assertNotEqual(readiness_config_fingerprint(config), before)
        interface["slave_id"] = 0
        with self.assertRaisesRegex(ValueError, "地址"):
            acquisition.AcquisitionConfig(acquisition_mode="real", dataset_schema="new_collection_v13_0", interfaces=[interface], selected_sensors=list(THERMOCOUPLE64_CHANNELS))

    def test_single_interface_factory_forwards_instrument_address(self) -> None:
        normalized = acquisition.AcquisitionConfig(
            acquisition_mode="real", dataset_schema="new_collection_v13_0", driver="huipu_tc64_modbus_rtu",
            endpoint="COM17", slave_id=7, timeout=.28, read_profile="full_record_block", diagnostic_validation=True,
        )
        self.assertEqual(normalized.interfaces[0]["slave_id"], 7)
        self.assertEqual(normalized.interfaces[0]["timeout"], .28)
        self.assertEqual(normalized.interfaces[0]["read_profile"], "full_record_block")
        config = SimpleNamespace(
            acquisition_mode="real", interfaces=[],
            driver="huipu_tc64_modbus_rtu", endpoint="COM17",
            baudrate=19200, slave_id=7,
        )
        with patch.object(acquisition, "HpThermocouple64Driver") as factory:
            driver = acquisition.build_driver(config)
        self.assertIs(driver, factory.return_value)
        factory.assert_called_once_with("COM17", 19200, 7, 0.5, read_profile="channel_temperature", channel_settings=None)

    def test_multi_interface_worker_publishes_full_channel_set(self) -> None:
        interface = acquisition.default_capture_interfaces()[0]
        interface["endpoint"] = "COM17"
        driver = acquisition.MultiInterfaceDriver([interface], list(THERMOCOUPLE64_CHANNELS))
        port = InstrumentSerial()
        with patch("serial.Serial", return_value=port):
            driver.open()
            try:
                deadline = time.monotonic() + 1
                while len(driver.cache.snapshot()) < 64 and time.monotonic() < deadline:
                    time.sleep(0.005)
                samples = driver.cache.snapshot()
                self.assertEqual(len(samples), 64)
                self.assertEqual(samples["温度64"].value, 35.75)
                self.assertTrue(samples["温度64"].protocol_ok)
                self.assertEqual(samples["温度64"].quality, ChannelQuality.MEASURED_NEW)
                self.assertEqual(samples["温度64"].interface_id, interface["id"])
                self.assertEqual(strict_group_means(driver.read_sample())["热电偶均值8"]["value"], 34.875)
            finally:
                driver.close()
        self.assertTrue(port.closed)

    def test_adapter_reconnects_without_publishing_partial_failed_scan(self) -> None:
        bad = InstrumentSerial()
        bad.corrupt_start = 1278
        good = InstrumentSerial()
        cache = ChannelSampleCache()
        identity = {"serial_number": "TC64", "vid": 1, "pid": 2}
        driver = HpThermocouple64Driver("COM17", serial_factory=unittest.mock.Mock(side_effect=[bad, good]), device_identity=identity,
                                      port_provider=lambda: [SimpleNamespace(device="COM17", location=None, **identity)])
        worker = LocalDirectAdapter("tc64", driver, THERMOCOUPLE64_CHANNELS, cache, reconnect_backoff_seconds=0.001)
        worker.start()
        try:
            deadline = time.monotonic() + 8
            while len(cache.snapshot()) < 64 and time.monotonic() < deadline:
                time.sleep(0.005)
            samples = cache.snapshot()
            self.assertEqual(len(samples), 64)
            self.assertTrue(bad.closed)
            self.assertGreaterEqual(worker.reconnect_attempts, 1)
            self.assertTrue(all(s.source_sequence == 1 for s in samples.values()))
        finally:
            self.assertTrue(worker.stop())
        self.assertTrue(good.closed)

    def test_reference_vectors_and_float_precision(self):
        vectors = [
            (1, "01 03 00 12 00 02 64 0E", "01 03 04 64 2B 41 FB E4 D8", 31.424),
            (8, "01 03 00 9E 00 02 A5 E5", "01 03 04 1E 6F 41 FA 7C 15", 31.265),
            (9, "01 03 00 B2 00 02 64 2C", "01 03 04 A4 3D 41 FB 39 1C", 31.455),
            (56, "01 03 04 5E 00 02 A4 E9", "01 03 04 6C 28 41 F8 56 B9", 31.053),
            (57, "01 03 04 72 00 02 65 20", "01 03 04 6A 28 41 FD 96 32", 31.677),
            (64, "01 03 04 FE 00 02 A4 CB", "01 03 04 52 C3 41 FB 6B 64", 31.415),
        ]
        settings = {"unit": 0, "input_type": 0, "switch": 0}
        for channel, request, response, csv in vectors:
            with self.subTest(channel=channel):
                self.assertEqual(read_request(1, 18 + (channel - 1) * 20, 2), bytes.fromhex(request))
                words = parse_response(bytes.fromhex(response), 1, 2)
                value, error = decode_temperature(words, settings)
                self.assertEqual(error, "")
                self.assertLessEqual(abs(value - csv), 0.0005)
                self.assertNotEqual(value, round(value, 3))
                self.assertGreater(abs(decode_temperature(words[::-1], settings)[0] - csv), 0.0005)
        for value in (-12.34567, 0, 123456, float("nan"), float("inf"), -float("inf")):
            result, error = decode_temperature(tuple(channel_record(value)[18:]), settings)
            if value in (float("inf"), -float("inf")) or value != value:
                self.assertIsNone(result)
                self.assertEqual(error, "non_finite")
            else:
                self.assertAlmostEqual(result, value, places=4)

    def test_default_short_only_scan_order_and_atomic_failure(self):
        port = InstrumentSerial(fragment=1)
        port.short_only = True
        port.records[0] = channel_record(float("nan"))
        driver = HpThermocouple64Driver("COM17", timeout=0.05, serial_factory=lambda **_: port)
        driver.open()
        first = driver.read_sample()
        self.assertIsNone(first["温度1"])
        self.assertEqual(first["温度64"], 35.75)
        spans = [struct.unpack(">BBHH", p[:-2])[2:] for p in port.writes]
        self.assertEqual(spans, [(i * 20 + offset, n) for i in range(64) for offset, n in ((1, 2), (14, 1), (18, 2))])
        self.assertEqual(driver.quality_metadata["valid_temperature_count"], 63)
        port.records[63] = channel_record(777)
        port.corrupt_start = 1278
        with self.assertRaisesRegex(HpProtocolError, "crc"):
            driver.read_sample()
        self.assertEqual(driver.observation_count, 1)
        self.assertFalse(driver.protocol_verified)
        self.assertEqual(first["温度64"], 35.75)
        driver.close()

    def test_nonblocking_silence_window_does_not_accumulate_read_timeout(self):
        class TimeoutAwareInstrumentSerial(InstrumentSerial):
            read_timeout = 0.02

            def read(self, size: int) -> bytes:
                data = super().read(size)
                if not data and self.read_timeout:
                    time.sleep(self.read_timeout)
                return data

        port = TimeoutAwareInstrumentSerial(fragment=256)

        def factory(**kwargs):
            port.read_timeout = kwargs["timeout"]
            return port

        driver = HpThermocouple64Driver("COM17", timeout=0.05, serial_factory=factory)
        started = time.perf_counter()
        driver.open()
        elapsed = time.perf_counter() - started
        try:
            self.assertLess(elapsed, 1.5)
            self.assertEqual(driver.quality_metadata["valid_temperature_count"], 64)
        finally:
            driver.close()

    def test_steady_scans_reuse_connection_verified_channel_settings(self):
        port = InstrumentSerial(fragment=256)
        port.short_only = True
        driver = HpThermocouple64Driver("COM17", timeout=0.05, serial_factory=lambda **_: port)
        driver.open()
        try:
            first = driver.read_sample()
            initial_spans = [struct.unpack(">BBHH", payload[:-2])[2:] for payload in port.writes]
            self.assertEqual(
                initial_spans,
                [(index * 20 + offset, count) for index in range(64) for offset, count in ((1, 2), (14, 1), (18, 2))],
            )
            settings = json.loads(json.dumps(driver.quality_metadata["channel_settings"], ensure_ascii=False))

            second = driver.read_sample()
            steady_spans = [struct.unpack(">BBHH", payload[:-2])[2:] for payload in port.writes[len(initial_spans):]]
            self.assertEqual(steady_spans, [(index * 20 + 18, 2) for index in range(64)])
            self.assertEqual(second, first)
            self.assertEqual(driver.quality_metadata["channel_settings"], settings)
        finally:
            driver.close()

    def test_metadata_rejection_requires_matching_operator_confirmation(self):
        settings = {"confirmed": True, "binding": settings_binding("COM17", 115200, 1, "channel_temperature"),
                    "channels": {"温度64": {"unit": 0, "input_type": 0, "switch": 0}}}
        for confirmation, valid_count in (({}, 0), (settings, 1), ({**settings, "binding": {}}, 0)):
            port = InstrumentSerial()
            port.short_only = port.metadata_exception = True
            driver = HpThermocouple64Driver("COM17", channel_settings=confirmation, serial_factory=lambda **_: port)
            driver.open()
            values = driver.read_sample()
            self.assertTrue(driver.protocol_verified)
            self.assertEqual(driver.quality_metadata["valid_temperature_count"], valid_count)
            self.assertIsNone(values["温度1"])
            if valid_count:
                self.assertEqual(values["温度64"], 35.75)
                self.assertEqual(driver.quality_metadata["channel_settings"]["温度64"]["source"], "operator_confirmed")
            driver.close()
        for response, code in ((b"", "timeout"), (b"\x01\x03\x04\0\0\0\0\0\0", "crc")):
            port = InstrumentSerial()
            port.responses[1] = response
            driver = HpThermocouple64Driver("COM17", timeout=0.01, channel_settings=settings, serial_factory=lambda **_: port)
            with self.assertRaisesRegex(HpProtocolError, code):
                driver.open()
            self.assertFalse(driver.protocol_verified)

    def test_live_framing_failures_are_classified_and_bounded(self):
        for response, code in (
            (append_crc(b"\x02\x03\x04\0\0\0\0"), "address"),
            (append_crc(b"\x01\x04\x04\0\0\0\0"), "function"),
            (append_crc(b"\x01\x03\x02\0\0"), "byte_count"),
            (append_crc(b"\x01\x03\x04\0\0\0\0") + b"extra", "trailing_data"),
            (b"", "timeout"),
        ):
            with self.subTest(code=code):
                port = InstrumentSerial(fragment=256)
                port.responses[1] = response
                driver = HpThermocouple64Driver("COM17", timeout=0.01, serial_factory=lambda **_: port)
                started = time.monotonic()
                with self.assertRaisesRegex(HpProtocolError, code):
                    driver.open()
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertEqual(driver.quality_metadata["error_code"], code)
                self.assertLessEqual(len(driver.quality_metadata["last_transaction"]["response_hex"]), 767)
                self.assertTrue(port.closed)

    def test_cancellation_stops_following_channels_and_releases_port(self):
        port = InstrumentSerial()
        driver = HpThermocouple64Driver("COM17", serial_factory=lambda **_: port)
        write = port.write
        def cancel_after_first(payload):
            result = write(payload)
            driver._cancel.set()
            return result
        port.write = cancel_after_first
        with self.assertRaisesRegex(HpProtocolError, "cancelled"):
            driver.open()
        self.assertEqual(len(port.writes), 1)
        self.assertTrue(port.closed)
        self.assertGreater(driver.open_timeout_seconds, 192 * driver.timeout)

    def test_unknown_identity_does_not_reopen_another_device(self):
        port = InstrumentSerial()
        driver = HpThermocouple64Driver("COM17", serial_factory=lambda **_: port)
        driver.open()
        driver.close()
        with self.assertRaisesRegex(HpProtocolError, "binding_unconfirmed"):
            driver.open()

    def test_residual_exception_crc_and_fragmented_extra_data(self):
        port = InstrumentSerial(fragment=1)
        driver = HpThermocouple64Driver("COM17", timeout=.05, serial_factory=lambda **_: port)
        driver.serial = port
        port.pending = b"old"
        with self.assertRaisesRegex(HpProtocolError, "residual_data"):
            driver._read_registers(18, 2)
        self.assertEqual(port.writes, [])
        for response, code in ((append_crc(b"\x01\x83\x02")[:-1]+b"\x00", "crc"),
                               (append_crc(b"\x01\x03\x04\0\0\0\0")+b"\xff", "trailing_data")):
            port.pending = b""
            port.responses[18] = response
            started = time.monotonic()
            with self.assertRaisesRegex(HpProtocolError, code):
                driver._read_registers(18, 2)
            self.assertLess(time.monotonic()-started, .2)
        driver.close()

    def test_open_failures_and_active_session_probe_conflict(self):
        for error, code in ((PermissionError("Access is denied"), "port_busy"), (FileNotFoundError("COM missing"), "port_open_failed")):
            factory = unittest.mock.Mock(side_effect=error)
            driver = HpThermocouple64Driver("COM17", serial_factory=factory)
            with self.assertRaises(type(error)):
                driver.open()
            self.assertEqual(driver.quality_metadata["error_code"], code)
            self.assertEqual(driver.quality_metadata["tx_bytes"], 0)
            self.assertFalse(driver.protocol_verified)
        manager = acquisition.AcquisitionManager(recovery_scan=False)
        manager.thread = SimpleNamespace(is_alive=lambda: True)
        manager.config = SimpleNamespace(acquisition_mode="real")
        with self.assertRaisesRegex(RuntimeError, "port_busy"):
            manager.test_connection(SimpleNamespace())

    def test_stable_identity_reenumeration_requires_full_scan_and_setting_recheck(self):
        identity = {"vid": 1027, "pid": 24577, "serial_number": "TC64", "location": "old"}
        ports = [InstrumentSerial(), InstrumentSerial()]
        ports[1].metadata_exception = True
        settings = {"confirmed": True, "binding": settings_binding("COM17", 115200, 1, "channel_temperature", identity),
                    "channels": {"温度64": {"unit": 0, "input_type": 0, "switch": 0}}}
        factory = unittest.mock.Mock(side_effect=ports)
        enumerations = [[SimpleNamespace(device="COM17", **identity)], [SimpleNamespace(device="COM19", serial_number="TC64", vid=1027, pid=24577, location="new")]]
        driver = HpThermocouple64Driver("COM17", serial_factory=factory, device_identity=identity, channel_settings=settings,
            port_provider=unittest.mock.Mock(side_effect=enumerations))
        driver.open()
        driver.read_sample()
        driver.close()
        driver.open()
        self.assertEqual(factory.call_args.kwargs["port"], "COM19")
        self.assertEqual(driver.observation_count, 2)
        self.assertTrue(all(v is None for v in driver.read_sample().values()))
        self.assertTrue(driver.protocol_verified)
        self.assertEqual(driver.quality_metadata["valid_temperature_count"], 0)
        self.assertTrue(ports[0].closed)
        driver.close()

    def test_slow_scan_observation_is_held_then_stale_without_fake_updates(self):
        port = InstrumentSerial()
        driver = HpThermocouple64Driver("COM17", serial_factory=lambda **_: port)
        driver.open()
        values = driver.read_sample()
        cache = ChannelSampleCache()
        driver.quality_metadata.update(received_monotonic=100., received_wall_time=1000., scan_duration_seconds=4.)
        worker = LocalDirectAdapter("tc64", driver, THERMOCOUPLE64_CHANNELS, cache)
        worker._publish(values)
        assembler = FrameAssembler(THERMOCOUPLE64_CHANNELS, 10, {n: 1 for n in THERMOCOUPLE64_CHANNELS}, start_monotonic=100, start_wall_time=1000, clock=lambda:100)
        new = assembler.assemble(cache, 100.)
        held = assembler.assemble(cache, 105.)
        stale = assembler.assemble(cache, 111.)
        for name in THERMOCOUPLE64_CHANNELS:
            self.assertEqual(new.channels[name].quality, ChannelQuality.MEASURED_NEW)
            self.assertEqual(held.channels[name].quality, ChannelQuality.HELD_WITHIN_FRESHNESS)
            self.assertEqual(stale.channels[name].quality, ChannelQuality.STALE)
            self.assertEqual(held.channels[name].source_sequence, 1)
            self.assertEqual(new.channels[name].received_wall_time, 1000.)
        self.assertEqual(strict_group_means(held.values())["热电偶均值8"]["value"], 34.875)
        self.assertIsNone(strict_group_means(stale.values())["热电偶均值8"]["value"])
        driver.close()

    def test_reconnect_window_outlives_five_attempts_and_expires_or_stops(self):
        class Device:
            quality_metadata = {"driver": "huipu_tc64_modbus_rtu"}
            def __init__(self):
                self.opens = 0
                self.delivered = False
                self.recover = False
            def open(self): self.opens += 1
            def close(self): pass
            def read_sample(self):
                if not self.delivered or self.recover:
                    self.delivered = True
                    return {n: float(self.opens) for n in THERMOCOUPLE64_CHANNELS}
                raise OSError("设备断开")
        for recover in (True, False):
            device = Device()
            cache = ChannelSampleCache()
            worker = LocalDirectAdapter("tc64", device, THERMOCOUPLE64_CHANNELS, cache,
                reconnect_backoff_seconds=.001, max_reconnect_backoff_seconds=.01,
                max_reconnect_attempts=5, reconnect_window_seconds=.6)
            worker.start()
            try:
                deadline = time.monotonic()+2
                while worker.reconnect_attempts < 6 and time.monotonic()<deadline:
                    time.sleep(.005)
                self.assertGreater(worker.reconnect_attempts, 5, worker.status())
                sequence = worker.source_sequence
                if recover:
                    device.recover = True
                    while worker.source_sequence == sequence and time.monotonic()<deadline:
                        time.sleep(.005)
                    self.assertGreater(worker.source_sequence, sequence)
                    self.assertEqual(len({s.source_sequence for s in cache.snapshot().values()}), 1)
                else:
                    while worker.status()["running"] and time.monotonic()<deadline:
                        time.sleep(.005)
                    self.assertEqual(worker.state, "failed")
                    self.assertEqual(worker.source_sequence, sequence)
            finally:
                self.assertTrue(worker.stop())

    def test_configuration_plan_round_trip_preserves_mode_and_operator_settings(self):
        from pre_sensor_config import InterfacePlanStore
        interface = acquisition.default_capture_interfaces()[0]
        interface.update(enabled=False, endpoint="COM17", read_profile="full_record_block", selection_origin="manual",
                         channel_settings={"confirmed": True, "channels": {"温度1": {"unit": 2, "input_type": 1, "switch": 0}}}, protocol_verified=True)
        plan = {"active": "new_collection_v13_0", "plans": {"new_collection_v13_0": {"interfaces": [interface]}}}
        with tempfile.TemporaryDirectory() as root:
            store = InterfacePlanStore(Path(root)/"plan.json")
            saved = store.save(plan)
            restored = store.load()
        item = restored["plans"][restored["active"]]["interfaces"][0]
        self.assertEqual(item["read_profile"], "full_record_block")
        self.assertEqual(item["channel_settings"], interface["channel_settings"])
        self.assertFalse(item["enabled"])
        self.assertEqual(item["selection_origin"], "manual")
        self.assertNotIn("protocol_verified", item)
        missing = dict(interface)
        missing.pop("read_profile")
        missing["enabled"] = True
        missing["physical_interface_id"] = "serial:COM17"
        config = acquisition.AcquisitionConfig(acquisition_mode="real", interfaces=[missing], dataset_schema="new_collection_v13_0", selected_sensors=list(THERMOCOUPLE64_CHANNELS))
        self.assertEqual(config.interfaces[0]["read_profile"], "channel_temperature")
        missing["read_profile"] = "automatic"
        with self.assertRaisesRegex(ValueError, "读取方式"):
            acquisition.AcquisitionConfig(acquisition_mode="real", interfaces=[missing], dataset_schema="new_collection_v13_0")


if __name__ == "__main__":
    unittest.main()
