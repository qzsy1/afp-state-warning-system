from __future__ import annotations

import json
import unittest

try:
    import diagnostic_tools
except ImportError:  # RED phase: the production module is added after this contract.
    diagnostic_tools = None


INTERFACE_FIXTURES = (
    {
        "id": "thermocouple_8ch",
        "role": "thermocouple",
        "driver": "smrf_hid",
        "endpoint": "SMRFCT08B",
        "physical_interface_id": "usb_hid:auto",
        "physical_interface_kind": "usb_hid",
        "protocol": "smrf_hid",
        "physical_fallback": False,
        "expected_channels": [f"温度{index}" for index in range(1, 9)],
        "state": "identity_unconfirmed",
        "message": "未识别到匹配的 SMRFCT08B USB HID 设备",
    },
    {
        "id": "plc_process",
        "role": "plc",
        "driver": "modbus_tcp",
        "endpoint": "192.168.125.5:502",
        "physical_interface_id": "ethernet:Ethernet",
        "physical_interface_kind": "ethernet",
        "protocol": "modbus_tcp",
        "physical_fallback": False,
        "expected_channels": ["温度", "压力", "张力"],
        "state": "endpoint_unreachable",
        "message": "PLC 端点 192.168.125.5:502 不可达",
    },
    {
        "id": "uvc_temperature",
        "role": "thermal_uvc",
        "driver": "uvc_thermal",
        "endpoint": "BSV UVC (WinUSB)",
        "physical_interface_id": "usb_uvc:auto",
        "physical_interface_kind": "usb_uvc",
        "protocol": "uvc",
        "physical_fallback": False,
        "expected_channels": ["ROI平均温度"],
        "state": "identity_unconfirmed",
        "message": "未识别到可产生 BSV 温度帧的设备",
    },
    {
        "id": "abb_motion",
        "role": "robot",
        "driver": "abb_robot",
        "endpoint": "192.168.125.1",
        "physical_interface_id": "ethernet:Ethernet",
        "physical_interface_kind": "ethernet",
        "protocol": "abb_rws",
        "physical_fallback": False,
        "expected_channels": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
        "state": "endpoint_unreachable",
        "message": "ABB RWS 端点 192.168.125.1 不可达",
    },
    {
        "id": "m3232_pressure",
        "role": "pressure",
        "driver": "m3232_pressure",
        "endpoint": "COM8",
        "physical_interface_id": "serial:COM8",
        "physical_interface_kind": "serial",
        "protocol": "m3232_serial",
        "physical_fallback": True,
        "physical_warning": "当前未识别到匹配协议，已临时分配串口，仅用于测试",
        "expected_channels": ["薄膜压力"],
        "state": "identity_unconfirmed",
        "message": "未确认串口设备为 M3232，未收到有效帧",
    },
)


def no_sensor_hardware_result() -> dict:
    interfaces = []
    sensors = []
    for template in INTERFACE_FIXTURES:
        channels = list(template["expected_channels"])
        interfaces.append(
            {
                **template,
                "enabled": True,
                "physical_verified": False,
                "detected_channels": [],
                "missing_channels": channels,
                "invalid_channels": [],
                "sample_counts": {},
                "invalid_sample_counts": {},
                "errors": [],
                "ok": False,
            }
        )
        sensors.extend(
            {
                "name": name,
                "selected": True,
                "received_samples": 0,
                "invalid_samples": 0,
                "state": "no_data",
                "message": "未检测到数据",
                "ok": False,
            }
            for name in channels
        )
    return {
        "simulated": False,
        "ok": False,
        "interfaces": interfaces,
        "sensors": sensors,
    }


class NoSensorTruthTests(unittest.TestCase):
    def test_no_sensor_truth_has_five_failed_interfaces_and_seventeen_channels(self) -> None:
        result = no_sensor_hardware_result()

        self.assertEqual(len(result["interfaces"]), 5)
        self.assertEqual(len(result["sensors"]), 17)
        self.assertEqual(sum(bool(item["ok"]) for item in result["interfaces"]), 0)
        self.assertEqual(sum(bool(item["ok"]) for item in result["sensors"]), 0)
        self.assertNotIn("传感器损坏", json.dumps(result, ensure_ascii=False))

    def test_placeholder_interfaces_never_count_as_connected(self) -> None:
        self.assertIsNotNone(diagnostic_tools, "diagnostic_tools production module must exist")

        normalized = diagnostic_tools.normalize_no_sensor_states(no_sensor_hardware_result())

        self.assertEqual(sum(bool(item["ok"]) for item in normalized["interfaces"]), 0)
        self.assertEqual(
            normalized["summary"],
            "检查未通过：0/5 个目标传感器接口已由有效数据确认，0/17 个通道正常",
        )
        self.assertNotIn("传感器损坏", json.dumps(normalized, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
