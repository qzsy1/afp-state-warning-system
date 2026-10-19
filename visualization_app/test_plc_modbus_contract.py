from __future__ import annotations

import math
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
        self.assertEqual(len(driver.sock.requests), 1)

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
