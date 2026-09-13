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


def diagnostic_context_for(hardware_result: dict):
    events = [
        {
            "interface_id": item["id"],
            "interface_label": item["id"],
            "role": item["role"],
            "driver": item["driver"],
            "endpoint": item["endpoint"],
            "physical_interface_id": item["physical_interface_id"],
            "physical_interface_kind": item["physical_interface_kind"],
            "protocol": item["protocol"],
            "physical_fallback": item.get("physical_fallback", False),
            "physical_warning": item.get("physical_warning", ""),
            "sensor_name": item["expected_channels"][0],
            "channels": list(item["expected_channels"]),
            "state": item["state"],
            "message": item["message"],
            "evidence": {
                "expected_channels": list(item["expected_channels"]),
                "detected_channels": [],
                "missing_channels": list(item["expected_channels"]),
                "invalid_channels": [],
                "sample_counts": {},
            },
        }
        for item in hardware_result["interfaces"]
    ]
    discovery = {
        "physical_interfaces": [
            {
                "id": "ethernet:Ethernet",
                "kind": "ethernet",
                "endpoint": "Ethernet",
                "addresses": ["192.168.10.20"],
                "detected": True,
                "shared_roles": ["plc", "robot"],
            },
            {
                "id": "serial:COM8",
                "kind": "serial",
                "endpoint": "COM8",
                "detected": True,
            },
            {
                "id": "usb_hid:auto",
                "kind": "usb_hid",
                "endpoint": "SMRFCT08B",
                "detected": False,
                "auto_assignable": True,
            },
            {
                "id": "usb_uvc:auto",
                "kind": "usb_uvc",
                "endpoint": "BSV UVC (WinUSB)",
                "detected": False,
                "auto_assignable": True,
            },
        ],
        "plc_reachable": False,
        "abb_reachable": False,
        "uvc_dll_found": True,
        "thermocouple_reachable": False,
    }
    return diagnostic_tools.DiagnosticToolContext(
        events=tuple(events),
        hardware_result=hardware_result,
        discovery=discovery,
        acquisition_status={"running": False, "sensors": hardware_result["sensors"]},
    )


class DiagnosticToolTests(unittest.TestCase):
    def test_all_allowlisted_tools_return_evidence_ids(self) -> None:
        self.assertTrue(hasattr(diagnostic_tools, "DiagnosticToolContext"))
        context = diagnostic_context_for(no_sensor_hardware_result())
        checks = (
            ("inspect_interface_mapping", {"interface_id": "thermocouple_8ch"}),
            ("recheck_interface", {"interface_id": "m3232_pressure", "duration_seconds": 1}),
            ("get_recent_channel_stats", {"channel_names": ["薄膜压力"], "window_seconds": 30}),
            (
                "get_cross_interface_timeline",
                {"interface_ids": ["plc_process", "abb_motion"], "window_seconds": 60},
            ),
            ("check_network_path", {"interface_id": "plc_process"}),
            ("inspect_protocol_frame", {"interface_id": "m3232_pressure"}),
            (
                "lookup_device_knowledge",
                {"device_type": "m3232_pressure", "symptom": "no_data"},
            ),
            (
                "compare_related_signals",
                {"channel_names": ["压力", "薄膜压力"], "window_seconds": 30},
            ),
        )

        results = [
            diagnostic_tools.execute_tool(context, name, arguments)
            for name, arguments in checks
        ]

        self.assertEqual([item["evidence_id"] for item in results], [f"EV-{i:03d}" for i in range(1, 9)])
        self.assertEqual([item["tool"] for item in results], [name for name, _ in checks])
        self.assertFalse(results[5]["evidence_available"])
        self.assertTrue(results[7]["insufficient_data"])

    def test_tool_definitions_expose_only_the_eight_read_only_tools(self) -> None:
        definitions = diagnostic_tools.tool_definitions()

        names = {item["function"]["name"] for item in definitions}
        self.assertEqual(
            names,
            {
                "inspect_interface_mapping",
                "recheck_interface",
                "get_recent_channel_stats",
                "get_cross_interface_timeline",
                "check_network_path",
                "inspect_protocol_frame",
                "lookup_device_knowledge",
                "compare_related_signals",
            },
        )
        self.assertTrue(all(item["type"] == "function" for item in definitions))

    def test_tool_registry_rejects_unknown_tool_interface_and_network_target(self) -> None:
        context = diagnostic_context_for(no_sensor_hardware_result())

        with self.assertRaisesRegex(diagnostic_tools.DiagnosticToolError, "不允许"):
            diagnostic_tools.execute_tool(context, "run_command", {"command": "whoami"})
        with self.assertRaisesRegex(diagnostic_tools.DiagnosticToolError, "未知接口"):
            diagnostic_tools.execute_tool(
                context, "check_network_path", {"interface_id": "8.8.8.8"}
            )
        with self.assertRaisesRegex(diagnostic_tools.DiagnosticToolError, "网络接口"):
            diagnostic_tools.execute_tool(
                context, "check_network_path", {"interface_id": "m3232_pressure"}
            )

    def test_tool_registry_enforces_duration_and_window_limits(self) -> None:
        context = diagnostic_context_for(no_sensor_hardware_result())

        with self.assertRaisesRegex(diagnostic_tools.DiagnosticToolError, "3 秒"):
            diagnostic_tools.execute_tool(
                context,
                "recheck_interface",
                {"interface_id": "m3232_pressure", "duration_seconds": 4},
            )
        with self.assertRaisesRegex(diagnostic_tools.DiagnosticToolError, "120 秒"):
            diagnostic_tools.execute_tool(
                context,
                "get_recent_channel_stats",
                {"channel_names": ["薄膜压力"], "window_seconds": 121},
            )


if __name__ == "__main__":
    unittest.main()
