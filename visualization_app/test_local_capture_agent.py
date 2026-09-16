from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_capture_agent import LocalCaptureAgent  # noqa: E402
from local_capture_agent import HelperTransport  # noqa: E402


class LocalCaptureAgentTests(unittest.TestCase):
    def test_transport_builds_hello_without_secrets(self):
        transport = HelperTransport(
            "wss://example.test/helper", "pairing-secret", device_id="device-a"
        )
        message = transport.hello(capabilities={"real_capture": True})
        self.assertEqual(message["type"], "hello")
        self.assertEqual(message["device_id"], "device-a")
        self.assertNotIn("pairing-secret", str(message))

    def test_transport_rejects_non_json_or_unknown_command(self):
        transport = HelperTransport("wss://example.test/helper", "pairing-secret")
        with self.assertRaises(ValueError):
            transport.decode_command("not-json")
        with self.assertRaises(ValueError):
            transport.decode_command('{"type":"command","command":"shell"}')

    def test_transport_accepts_process_parameter_read_command(self):
        command = HelperTransport.decode_command(
            '{"type":"command","request_id":"read-1",'
            '"command":"read_process_parameters","payload":{}}'
        )

        self.assertEqual(command["command"], "read_process_parameters")
        self.assertEqual(command["request_id"], "read-1")

    @patch("local_capture_agent.AcquisitionManager.discover_interfaces")
    def test_discover_returns_five_logical_sensor_bindings(self, discover):
        discover.return_value = {
            "physical_interfaces": [
                {"id": "hid:smrf", "kind": "usb_hid", "protocol": "smrf_hid", "detected": True},
                {"id": "ethernet:工控网卡", "kind": "ethernet", "protocol": "ethernet", "detected": True},
                {"id": "uvc:bsv", "kind": "usb_uvc", "protocol": "uvc", "driver_available": True},
                {"id": "serial:COM3", "kind": "serial", "protocol": "serial", "detected": True},
            ],
            "sensor_type_profiles": {},
            "defaults": [],
        }
        result = LocalCaptureAgent().discover()
        self.assertEqual(
            [item["role"] for item in result["sensor_bindings"]],
            ["thermocouple_8ch", "plc_process", "uvc_temperature", "abb_motion", "m3232_pressure"],
        )
        self.assertEqual(result["sensor_bindings"][1]["physical_interface_id"], "ethernet:工控网卡")
        self.assertEqual(result["sensor_bindings"][3]["physical_interface_id"], "ethernet:工控网卡")
        self.assertEqual(result["sensor_bindings"][4]["physical_interface_id"], "serial:COM3")

    @patch("local_capture_agent.AcquisitionManager")
    def test_capture_methods_delegate_to_existing_manager(self, manager_cls):
        manager = manager_cls.return_value
        manager.status.return_value = {"running": False}
        agent = LocalCaptureAgent(manager=manager)
        config = Mock()

        agent.start_capture(config)
        agent.stop_capture()
        self.assertTrue(manager.start.called)
        self.assertTrue(manager.stop.called)

    @patch("local_capture_agent.AcquisitionManager")
    def test_process_parameter_read_delegates_to_existing_manager(self, manager_cls):
        manager = manager_cls.return_value
        manager.read_process_parameters.return_value = {
            "ok": True,
            "values": {"pid_angle_deg": 5.0},
        }
        agent = LocalCaptureAgent(manager=manager)
        config = Mock()

        result = agent.read_process_parameters(config)

        self.assertEqual(result["values"]["pid_angle_deg"], 5.0)
        manager.read_process_parameters.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
