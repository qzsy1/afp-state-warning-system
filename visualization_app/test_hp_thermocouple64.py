from __future__ import annotations

import struct
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acquisition
from hp_thermocouple64 import (
    HpProtocolError, HpThermocouple64Driver, append_crc, decode_channel,
    parse_response, read_request,
)
from local_direct_acquisition import LocalDirectAdapter
from real_acquisition import ChannelQuality, ChannelSampleCache, readiness_config_fingerprint
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

    def reset_input_buffer(self) -> None:
        self.pending = b""

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        address, function, start, count = struct.unpack(">BBHH", payload[:-2])
        if function != 3 or count > 125 or start + count > 1280:
            raise AssertionError("unexpected acquisition request")
        registers = sum(self.records, [])[start:start + count]
        self.pending = self.exception or append_crc(
            bytes([address, 3, count * 2]) + struct.pack(f">{count}H", *registers)
        )
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
        driver = HpThermocouple64Driver("COM17", slave_id=7, serial_factory=factory)
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
            factory.assert_called_once_with(port="COM17", baudrate=115200, bytesize=8, parity="N", stopbits=1, timeout=0.02, write_timeout=0.5)
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
                driver = HpThermocouple64Driver("COM17", timeout=0.01, serial_factory=lambda **_: port)
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
        config = SimpleNamespace(
            acquisition_mode="real", interfaces=[],
            driver="huipu_tc64_modbus_rtu", endpoint="COM17",
            baudrate=19200, slave_id=7,
        )
        with patch.object(acquisition, "HpThermocouple64Driver") as factory:
            driver = acquisition.build_driver(config)
        self.assertIs(driver, factory.return_value)
        factory.assert_called_once_with("COM17", 19200, 7)

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
        bad.corrupt_start = 1200
        good = InstrumentSerial()
        cache = ChannelSampleCache()
        driver = HpThermocouple64Driver("COM17", serial_factory=unittest.mock.Mock(side_effect=[bad, good]))
        worker = LocalDirectAdapter("tc64", driver, THERMOCOUPLE64_CHANNELS, cache, reconnect_backoff_seconds=0.001)
        worker.start()
        try:
            deadline = time.monotonic() + 2
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


if __name__ == "__main__":
    unittest.main()
