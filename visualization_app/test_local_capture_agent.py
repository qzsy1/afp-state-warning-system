from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_capture_agent import LocalCaptureAgent  # noqa: E402
from local_capture_agent import HelperTransport  # noqa: E402


class LocalCaptureAgentTests(unittest.TestCase):
    def test_sample_batch_is_replayed_until_ack_then_advances(self):
        manager = Mock()
        manager.start.return_value = {"running": True, "config": {"acquisition_mode": "real"}}
        manager.status.return_value = {"running": True, "config": {"acquisition_mode": "real"}}
        manager.numeric_matrix.return_value = (
            [{"温度": 350.0}, {"温度": 351.0}, {"温度": 352.0}],
            [1.0, 2.0, 3.0],
        )
        agent = LocalCaptureAgent(manager=manager)

        started = agent.start_capture(Mock())
        first = agent.next_sample_batch(limit=2)
        replay = agent.next_sample_batch(limit=2)

        self.assertTrue(started["capture_uuid"])
        self.assertEqual(first, replay)
        self.assertEqual(first["sequence"], 0)
        self.assertEqual(len(first["rows"]), 2)
        self.assertTrue(agent.ack_sample_batch(first["capture_uuid"], 0))
        second = agent.next_sample_batch(limit=2)
        self.assertEqual(second["sequence"], 1)
        self.assertEqual(second["rows"], [{"温度": 352.0}])

    def test_batch_ack_for_other_capture_does_not_advance_cursor(self):
        manager = Mock()
        manager.start.return_value = {"running": True}
        manager.status.return_value = {"running": True, "config": {}}
        manager.numeric_matrix.return_value = ([{"压力": 1.0}], [1.0])
        agent = LocalCaptureAgent(manager=manager)
        agent.start_capture(Mock())
        pending = agent.next_sample_batch()

        self.assertFalse(agent.ack_sample_batch("other-capture", pending["sequence"]))
        self.assertEqual(agent.next_sample_batch(), pending)

    def test_stop_emits_final_status_batch_even_when_no_new_rows_arrive(self):
        manager = Mock()
        manager.start.return_value = {"running": True}
        manager.stop.return_value = {"running": False, "finalization_complete": True}
        manager.numeric_matrix.return_value = ([], [])
        manager.status.side_effect = [
            {"running": True, "config": {}},
            {"running": False, "config": {}, "finalization_complete": True},
        ]
        agent = LocalCaptureAgent(manager=manager)
        started = agent.start_capture(Mock())
        initial = agent.next_sample_batch()
        self.assertEqual(initial["rows"], [])
        agent.ack_sample_batch(started["capture_uuid"], initial["sequence"])

        stopped = agent.stop_capture()
        final = agent.next_sample_batch()

        self.assertFalse(stopped["running"])
        self.assertEqual(stopped["capture_uuid"], started["capture_uuid"])
        self.assertEqual(final["rows"], [])
        self.assertFalse(final["status"]["running"])

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

    def test_transport_allows_plain_websocket_only_for_private_network_origin(self):
        private = HelperTransport("http://192.168.101.31:8770", "token", device_id="pc")
        with patch("websocket.create_connection", return_value=object()) as create:
            private.connect_once()
        self.assertTrue(create.call_args.args[0].startswith("ws://192.168.101.31:8770/"))

        public = HelperTransport("http://example.com:8770", "token", device_id="pc")
        with self.assertRaisesRegex(ValueError, "公网"):
            public.connect_once()

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

    @patch("local_capture_agent.select_capture_folder", return_value="F:\\AFP_Capture")
    @patch("local_capture_agent.check_capture_save_root", return_value={"ok": True})
    def test_save_folder_operations_execute_on_helper_computer(self, check_root, select_folder):
        agent = LocalCaptureAgent(manager=Mock())

        selected = agent.select_folder("F:\\")
        checked = agent.check_save_root("F:\\AFP_Capture")

        self.assertEqual(selected, {"selected": True, "path": "F:\\AFP_Capture"})
        self.assertTrue(checked["ok"])
        select_folder.assert_called_once_with("F:\\")
        check_root.assert_called_once_with("F:\\AFP_Capture")


if __name__ == "__main__":
    unittest.main()
