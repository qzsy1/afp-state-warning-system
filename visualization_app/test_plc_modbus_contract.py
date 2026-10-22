from __future__ import annotations

import math
import json
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

from acquisition import AcquisitionConfig, AcquisitionManager, ModbusTcpDriver  # noqa: E402
from local_direct_acquisition import LocalDirectAdapter  # noqa: E402
from real_acquisition import (  # noqa: E402
    ChannelQuality,
    ChannelSampleCache,
    FrameAssembler,
)


def _float_words(value: float) -> tuple[int, int]:
    return struct.unpack("<HH", struct.pack("<f", float(value)))


class ModbusFixtureSocket:
    def __init__(self, register_values: dict[int, int]) -> None:
        self.register_values = dict(register_values)
        self.payload = bytearray()
        self.requests: list[bytes] = []

    def sendall(self, payload: bytes) -> None:
        self.requests.append(bytes(payload))
        transaction, protocol, length, unit, function, start, quantity = struct.unpack(
            ">HHHBBHH", payload
        )
        if protocol != 0 or length != 6 or function != 3:
            raise AssertionError("fixture received an invalid FC03 request")
        data = b"".join(
            struct.pack(">H", self.register_values.get(address, 0))
            for address in range(start, start + quantity)
        )
        pdu = bytes((3, len(data))) + data
        self.payload.extend(
            struct.pack(">HHHB", transaction, 0, 1 + len(pdu), unit) + pdu
        )

    def recv(self, size: int) -> bytes:
        result = bytes(self.payload[:size])
        del self.payload[:size]
        return result


class StaticResponseSocket:
    def __init__(self, payload: bytes) -> None:
        self.payload = bytearray(payload)

    def sendall(self, payload: bytes) -> None:
        pass

    def recv(self, size: int) -> bytes:
        result = bytes(self.payload[:size])
        del self.payload[:size]
        return result


def _response(
    *,
    transaction: int = 1,
    protocol: int = 0,
    unit: int = 255,
    function: int = 3,
    data: bytes = b"\x00\x01\x00\x02",
    byte_count: int | None = None,
    length: int | None = None,
) -> bytes:
    count = len(data) if byte_count is None else byte_count
    pdu = bytes((function, count)) + data
    declared = 1 + len(pdu) if length is None else length
    return struct.pack(">HHHB", transaction, protocol, declared, unit) + pdu


class PlcModbusContractTests(unittest.TestCase):
    def test_plc_connection_binds_source_and_rejects_a_different_actual_source(self) -> None:
        driver = ModbusTcpDriver(
            "192.168.125.5:502", source_address="192.168.125.209"
        )
        with patch("acquisition.socket.create_connection") as connect:
            connect.return_value.getsockname.return_value = ("198.18.0.1", 4000)
            with self.assertRaisesRegex(RuntimeError, "network_path_invalid"):
                driver.open()
            connect.assert_called_once_with(
                ("192.168.125.5", 502), timeout=2.0,
                source_address=("192.168.125.209", 0),
            )
            connect.return_value.close.assert_called_once()
            connect.return_value.sendall.assert_not_called()
        self.assertIsNone(driver.sock)

    def test_production_path_reads_all_three_finite_channels(self) -> None:
        registers: dict[int, int] = {}
        expected = {"温度": 102.25, "压力": 35.5, "张力": 12.75}
        register_map = {"温度": 28, "压力": 37, "张力": 23}
        for name, address in register_map.items():
            low, high = _float_words(expected[name])
            registers[address] = low
            registers[address + 1] = high
        driver = ModbusTcpDriver(
            "192.168.125.5:502", register_map=register_map, slave_id=255
        )
        driver.sock = ModbusFixtureSocket(registers)

        sample = driver.read_sample()

        self.assertEqual(sample, expected)
        self.assertEqual([struct.unpack(">HHHBBHH", request)[-2:] for request in driver.sock.requests],
                         [(28, 2), (23, 2), (37, 2)])

    def make_driver(self, values, addresses=None, **kwargs):
        addresses = addresses or {"温度": 28, "张力": 23, "压力": 37}
        registers = {}
        for name, value in values.items():
            low, high = _float_words(value)
            registers[addresses[name]] = low
            registers[addresses[name] + 1] = high
        driver = ModbusTcpDriver("192.168.125.5:502", register_map={name: addresses[name] for name in values}, **kwargs)
        driver.sock = ModbusFixtureSocket(registers)
        return driver

    def test_reference_source_recomputed_values_and_pressure_evidence(self):
        # Expectations recompute the verified C# rules; the C# program is not executed.
        for raw, expected in [(123.456, 123.46), (906.25, 906.25), (1.125, 1.12),
                              (1.375, 1.38), (-2.5, 0.0), (0.0, 0.0), (-0.001, 0.0)]:
            with self.subTest(raw=raw):
                driver = self.make_driver({"温度": -1.125, "张力": -2.5, "压力": raw})
                self.assertEqual(driver.read_sample(), {"温度": -1.12, "张力": -2.5, "压力": expected})
                record = driver.quality_metadata["channel_records"]["压力"]
                self.assertEqual(record["words"], list(_float_words(raw)))
                self.assertEqual(record["decoded_value"], struct.unpack("<f", struct.pack("<f", raw))[0])
                self.assertEqual(record["normalized_value"], expected)
                self.assertEqual(record["negative_pressure_clamped"], raw == -2.5)
                json.dumps(driver.quality_metadata, allow_nan=False)

    def test_real_configuration_normalizes_period_and_preserves_explicit_mapping(self):
        config = AcquisitionConfig(diagnostic_validation=True, acquisition_mode="real", real_acquisition_mode="local_direct",
            selected_sensors=["压力"], interfaces=[{"id":"plc", "role":"plc", "driver":"modbus_tcp",
            "endpoint":"192.168.125.8:1502", "slave_id":17, "register_map":{"压力":100}, "poll_interval_seconds":.05}],
            interface_channel_assignments={"plc":["压力"]})
        item = config.interfaces[0]
        self.assertEqual(item["endpoint"], "192.168.125.8:1502")
        self.assertEqual(item["slave_id"], 17)
        self.assertEqual(item["register_map"], {"压力":100})
        self.assertEqual(item["poll_interval_seconds"], .1)
        self.assertEqual(item["measurement_profile"], "fibercontrol_plc_v1")

    def test_normalized_zero_passes_existing_model_quality_gate_fault_and_stale_do_not(self):
        from real_acquisition import recent_complete_window
        self.assertTrue(recent_complete_window([{"压力":0}], ["压力"], 1,
                        quality_rows=[{"压力":"measured_new"}])["ready"])
        self.assertTrue(recent_complete_window([{"压力":0}], ["压力"], 1,
                        quality_rows=[{"压力":"held_within_freshness"}])["ready"])
        for quality in ("invalid", "missing", "stale"):
            self.assertFalse(recent_complete_window([{"压力":None}], ["压力"], 1,
                             quality_rows=[{"压力":quality}])["ready"])

    def test_custom_map_and_nonpressure_names_remain_uncropped(self):
        driver = self.make_driver({"温度": -3.5, "压力": -2.5}, {"温度": 150, "压力": 500})
        self.assertEqual(driver.read_sample(), {"温度": -3.5, "压力": 0})
        self.assertEqual([struct.unpack(">HHHBBHH", request)[-2:] for request in driver.sock.requests], [(150, 2), (500, 2)])
        driver = self.make_driver({"薄膜压力": -2.5}, {"薄膜压力": 40})
        self.assertEqual(driver.read_sample(), {"薄膜压力": -2.5})

    def test_nonfinite_and_protocol_fault_keep_json_safe_evidence_and_good_channels(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                driver = self.make_driver({"温度": 102.25, "张力": 12.75, "压力": value})
                events = driver.read_samples()
                good = [next(events), next(events)]
                bad = next(events)
                with self.assertRaisesRegex(ValueError, "非有限"):
                    next(events)
                self.assertEqual(good[0][0], {"温度": 102.25})
                self.assertEqual(good[1][0], {"张力": 12.75})
                self.assertEqual(bad[0], {"压力": None})
                self.assertEqual(bad[1]["error_category"], "nonfinite_value")
                self.assertEqual(bad[1]["words"], list(_float_words(value)))
                self.assertIsNone(bad[1]["decoded_value"])
                self.assertFalse(driver.protocol_verified)
                json.dumps(driver.quality_metadata, allow_nan=False)
        driver = ModbusTcpDriver("192.168.125.5:502")
        driver.sock = StaticResponseSocket(_response(transaction=9))
        with self.assertRaises(IOError):
            driver.read_sample()
        self.assertEqual(set(driver.channel_records), {"温度", "张力", "压力"})
        self.assertTrue(all(item["normalized_value"] is None for item in driver.channel_records.values()))
        self.assertTrue(all(item["error_category"] == "protocol_error" for item in driver.channel_records.values()))

    def test_channel_completion_cutoff_hold_stale_and_source_immutability(self):
        from plc_measurement import frame_source_records
        from real_acquisition import UnifiedFrame
        clock_values = iter([10.01, 10.03, 10.12])
        driver = self.make_driver({"温度": 102.25, "张力": 12.75, "压力": -2.5},
                                  clock=lambda: next(clock_values), wall_clock=lambda: 100.0)
        cache = ChannelSampleCache()
        adapter = LocalDirectAdapter("plc", driver, ["温度", "张力", "压力"], cache)
        for payload, record in driver.read_samples():
            adapter._publish(payload, record)
        assembler = FrameAssembler(["温度", "张力", "压力"], 10, {"温度": .5, "张力": .5, "压力": .5},
                                   start_monotonic=10, start_wall_time=100, clock=lambda: 10.2)
        early = assembler.assemble(cache, 10.1)
        self.assertIsNone(early.channels["压力"].value)
        frame = assembler.assemble(cache, 10.2)
        self.assertEqual(frame.channels["压力"].value, 0)
        self.assertEqual(frame.channels["温度"].quality, ChannelQuality.HELD_WITHIN_FRESHNESS)
        held = assembler.assemble(cache, 10.3)
        self.assertEqual(held.channels["压力"].quality, ChannelQuality.HELD_WITHIN_FRESHNESS)
        self.assertEqual(held.channels["压力"].received_monotonic, 10.12)
        self.assertEqual(held.channels["压力"].source_sequence, frame.channels["压力"].source_sequence)
        stale = assembler.assemble(cache, 11)
        self.assertEqual(stale.channels["压力"].quality, ChannelQuality.STALE)
        self.assertIsNone(stale.channels["压力"].value)
        driver.channel_records["压力"] = {"decoded_value": 99}
        evidence = frame_source_records(frame)
        self.assertEqual(evidence["压力"]["decoded_value"], -2.5)
        self.assertEqual(evidence["压力"]["host_monotonic"], 10.12)
        envelope = frame.to_envelope("capture", {"压力": "N"})
        restored = UnifiedFrame.from_envelope(json.loads(json.dumps(envelope, allow_nan=False)))
        self.assertEqual(frame_source_records(restored), evidence)

    def test_fixed_deadline_scheduler_waits_remainder_and_skips_slow_rounds(self):
        current = [0.0]
        starts = []
        durations = iter([.03, .03, .25, .01])
        class Driver:
            fixed_poll_interval_seconds = .1
            def open(self): pass
            def close(self): pass
            def read_sample(self):
                starts.append(current[0])
                current[0] += next(durations)
                return {"压力": 1}
        class Stop:
            done = False
            def is_set(self): return self.done
            def wait(self, delay):
                current[0] += delay
                self.done = len(starts) == 4
        adapter = LocalDirectAdapter("plc", Driver(), ["压力"], ChannelSampleCache(),
                                     poll_interval_seconds=.05, clock=lambda: current[0])
        adapter.stop_event = Stop()
        adapter._run()
        for actual, expected in zip(starts, [0, .1, .2, .5]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(adapter.skipped_poll_cycles, 2)
        self.assertEqual(adapter.status()["actual_poll_interval_seconds"], .1)

    def test_partial_success_does_not_reset_plc_failed_round_reconnect_limit(self):
        driver = self.make_driver({"温度":102.25, "压力":math.inf})
        driver.open = lambda: None
        driver.close = lambda: None
        adapter = LocalDirectAdapter("plc", driver, ["温度", "压力"], ChannelSampleCache(),
            max_reconnect_attempts=2, reconnect_backoff_seconds=.001, max_reconnect_backoff_seconds=.001)
        adapter.start()
        adapter.thread.join(1)
        try:
            self.assertEqual(adapter.state, "failed")
            self.assertEqual(adapter.reconnect_attempts, 2)
            self.assertEqual(adapter.cache.latest("压力").quality, ChannelQuality.INVALID)
            self.assertIsNone(adapter.cache.latest("压力").value)
        finally:
            self.assertTrue(adapter.stop(1))

    def test_fc03_rejects_each_protocol_identity_or_shape_error(self) -> None:
        exception_pdu = struct.pack(">HHHB", 1, 0, 3, 255) + bytes((0x83, 2))
        cases = {
            "transaction": _response(transaction=9),
            "protocol": _response(protocol=1),
            "unit": _response(unit=1),
            "length": _response(length=8)[:-1],
            "function": _response(function=4),
            "exception": exception_pdu,
            "register_count": _response(data=b"\x00\x01", byte_count=2),
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                driver = ModbusTcpDriver("192.168.125.5:502", slave_id=255)
                driver.sock = StaticResponseSocket(payload)
                with self.assertRaises(IOError):
                    driver._read_holding_registers(0, 2)

    def test_partial_register_map_does_not_invent_missing_channel(self) -> None:
        register_map = {"温度": 28, "压力": 37}
        registers: dict[int, int] = {}
        for name, value in {"温度": 90.0, "压力": 30.0}.items():
            low, high = _float_words(value)
            address = register_map[name]
            registers[address] = low
            registers[address + 1] = high
        driver = ModbusTcpDriver(
            "192.168.125.5:502", register_map=register_map, slave_id=255
        )
        driver.sock = ModbusFixtureSocket(registers)

        sample = driver.read_sample()

        self.assertEqual(sample, {"温度": 90.0, "压力": 30.0})
        self.assertNotIn("张力", sample)

    def test_nonfinite_register_value_is_rejected_instead_of_becoming_zero(self) -> None:
        low, high = _float_words(math.nan)
        driver = ModbusTcpDriver(
            "192.168.125.5:502", register_map={"温度": 28}, slave_id=255
        )
        driver.sock = ModbusFixtureSocket({28: low, 29: high})

        with self.assertRaisesRegex(ValueError, "非有限"):
            driver.read_sample()

    def test_old_plc_sample_is_stale_not_a_new_valid_measurement(self) -> None:
        cache = ChannelSampleCache()
        adapter = LocalDirectAdapter(
            "plc_process",
            object(),
            ["温度"],
            cache,
            clock=lambda: 10.0,
            wall_clock=lambda: 100.0,
        )
        self.assertTrue(adapter._publish({"温度": 102.25}))
        assembler = FrameAssembler(
            ["温度"],
            10.0,
            {"温度": 0.5},
            clock=lambda: 11.0,
            wall_clock=lambda: 101.0,
            start_monotonic=10.0,
            start_wall_time=100.0,
        )

        frame = assembler.assemble(cache, deadline_monotonic=11.0)

        self.assertEqual(frame.channels["温度"].quality, ChannelQuality.STALE)
        self.assertNotEqual(
            frame.channels["温度"].quality, ChannelQuality.MEASURED_NEW
        )

    def test_plc_check_requires_all_channels_and_reports_route_protocol_and_quality(self) -> None:
        interface = {
            "id": "plc_process",
            "role": "plc",
            "driver": "modbus_tcp",
            "endpoint": "192.168.125.5:502",
            "physical_interface_id": "ethernet:以太网",
            "physical_interface_kind": "ethernet",
            "physical_verified": True,
        }
        config = AcquisitionConfig(
            diagnostic_validation=True,
            acquisition_mode="real",
            real_acquisition_mode="local_direct",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["温度", "压力", "张力"],
            interfaces=[interface],
            interface_channel_assignments={
                "plc_process": ["温度", "压力", "张力"]
            },
        )
        route = {
            "ok": True,
            "status": "network_path_ready",
            "target_address": "192.168.125.5",
            "selected_interface_id": "ethernet:以太网",
            "actual_interface_id": "ethernet:以太网",
            "actual_source_address": "192.168.125.209",
            "interface_type": "physical",
            "target_subnet_match": True,
            "best_route": True,
        }

        def check(sample: dict[str, float]) -> dict:
            class Driver:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def open(self) -> None:
                    pass

                def read_sample(self):
                    return sample

                def close(self) -> None:
                    pass

            with patch("acquisition.validate_network_path", return_value=route), patch(
                "acquisition.MultiInterfaceDriver", Driver
            ):
                return AcquisitionManager().test_connection(config)

        complete = check({"温度": 102.25, "压力": 35.5, "张力": 12.75})
        complete_interface = complete["interfaces"][0]
        self.assertTrue(complete["ok"])
        self.assertEqual(complete_interface["state"], "data_ready")
        self.assertEqual(complete_interface["network_path"], route)
        self.assertEqual(
            complete_interface["stages"]["protocol"]["state"], "passed"
        )
        for channel, unit in {"温度": "°C", "压力": "N", "张力": "N"}.items():
            channel_result = complete_interface["channel_results"][channel]
            self.assertEqual(channel_result["unit"], unit)
            self.assertEqual(channel_result["quality"], "measured_new")
            self.assertGreater(channel_result["source_timestamp"], 0)

        partial = check({"温度": 102.25, "压力": 35.5})
        partial_interface = partial["interfaces"][0]
        self.assertFalse(partial["ok"])
        self.assertEqual(partial_interface["state"], "partial_channels")
        self.assertEqual(partial_interface["detected_channels"], ["压力", "温度"])
        self.assertEqual(partial_interface["missing_channels"], ["张力"])
        self.assertEqual(
            partial_interface["channel_results"]["张力"]["quality"], "missing"
        )


if __name__ == "__main__":
    unittest.main()
