from __future__ import annotations

import sys
import unittest
import tempfile
import time
import csv
import json
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_capture_agent import LocalCaptureAgent  # noqa: E402
from local_capture_agent import HelperTransport  # noqa: E402


class LocalCaptureAgentTests(unittest.TestCase):
    def test_tc64_short_scan_csv_mysql_and_helper_frames_agree(self):
        from acquisition import AcquisitionConfig, AcquisitionManager, default_capture_interfaces
        from local_capture_helper_entry import helper_capabilities
        from test_hp_thermocouple64 import InstrumentSerial
        from real_acquisition import UnifiedFrame
        from thermocouple64 import THERMOCOUPLE64_CHANNELS, strict_group_means
        saved = []
        ports = []
        class Store:
            def __init__(self, settings): pass
            def preflight(self, **kwargs): return {"ok": True}
            def save_layer(self, config, **kwargs):
                saved.append(list(kwargs["rows"]))
                return {"ok": True, "saved_rows": len(saved[-1])}
        def serial_factory(**kwargs):
            port = InstrumentSerial()
            port.short_only = True
            ports.append(port)
            self.assertEqual(kwargs["port"], "COM17")
            return port
        with tempfile.TemporaryDirectory() as temporary, patch("serial.Serial", side_effect=serial_factory), patch("acquisition.MySQLCaptureStore", Store):
            root = Path(temporary)
            interface = default_capture_interfaces()[0]
            interface.update(endpoint="COM17", physical_interface_id="serial:COM17", physical_interface_kind="serial", physical_verified=True)
            config = AcquisitionConfig(acquisition_mode="real", real_acquisition_mode="remote_helper", execution_host="helper_local",
                helper_session_id="fixture", helper_capabilities=helper_capabilities(), dataset_schema="new_collection_v13_0",
                interfaces=[interface], selected_sensors=list(THERMOCOUPLE64_CHANNELS), processing_mode="capture_only", save_root=str(root), mysql_enabled=True)
            manager = AcquisitionManager(root, recovery_scan=False)
            agent = LocalCaptureAgent(manager, simulation_cache_root=root/"cache")
            checked = agent.check_capture(config)
            self.assertTrue(checked["ok"], checked)
            self.assertEqual(checked["interfaces"][0]["driver_diagnostics"]["read_profile"], "channel_temperature")
            self.assertTrue(checked["interfaces"][0]["protocol_verified"])
            agent.start_capture(config)
            try:
                deadline = time.monotonic()+2
                while manager.status()["sample_count"] < 3 and time.monotonic()<deadline:
                    time.sleep(.01)
                batch = agent.next_sample_batch()
                self.assertTrue(batch["frames"])
                self.assertEqual(manager.driver.drivers[0].read_profile, "channel_temperature")
                for raw, frame, stamp in zip(batch["rows"], batch["frames"], batch["timestamps"]):
                    restored = UnifiedFrame.from_envelope_v2(frame, expected_session_id=batch["capture_uuid"], expected_mode_id="new_collection_v13_0", catalog=batch["frame_catalog"])
                    self.assertEqual(restored.values(), {n:raw[n] for n in THERMOCOUPLE64_CHANNELS})
                    self.assertEqual(restored.target_wall_time, stamp)
                    self.assertEqual(strict_group_means(restored.values())["热电偶均值8"]["value"], 34.875)
                    self.assertTrue(all(s.source_sequence == 1 for s in restored.channels.values()))
                self.assertTrue(agent.ack_sample_batch(batch["capture_uuid"], batch["sequence"]))
            finally:
                stopped = agent.stop_capture()
            self.assertTrue(stopped["finalization_complete"], stopped)
            with manager.raw_path.open(encoding="gb18030", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), len(saved[0]))
            for csv_row, mysql_row in zip(rows, saved[0]):
                for i, name in enumerate(THERMOCOUPLE64_CHANNELS):
                    self.assertEqual(float(csv_row[name]), 20+i/4)
                    self.assertEqual(float(csv_row[name]), float(mysql_row[name]))
            with Path(stopped["timestamp_file"]).open(encoding="utf-8-sig", newline="") as handle:
                timestamps = list(csv.DictReader(handle))
            self.assertEqual(len(timestamps), len(rows))
            self.assertTrue(all(json.loads(row["channel_quality_json"])["温度64"] in ("measured_new", "held_within_freshness") for row in timestamps))
            self.assertTrue(all(p.closed for p in ports))

    def test_tc64_candidates_use_serial_transport_without_claiming_identity(self):
        for order in ([6, 7], [7, 6]):
            manager = Mock()
            manager.discover_interfaces.return_value = {"physical_interfaces": [
                {"id": f"serial:COM{n}", "endpoint": f"COM{n}", "kind": "serial", "protocol": "serial",
                 "detected": True, "compatible_roles": ["thermocouple64", "esr1001_strain"]}
                for n in order]}
            tc = LocalCaptureAgent(manager=manager).discover()["sensor_bindings"][0]
            self.assertIsNone(tc["physical_interface_id"])
            self.assertEqual({p["endpoint"] for p in tc["candidates"]}, {"COM6", "COM7"})
            self.assertFalse(tc["identity_confirmed"])

    def test_discover_forwards_usb_topology_and_keeps_empty_ports_out_of_bindings(self):
        topology = {
            "schema_version": 1,
            "state": "complete",
            "provider": "fixture",
            "errors": [],
            "docks": [{"id": "dock:1", "label": "拓展坞 1"}],
            "usb_ports": [{
                "id": "usbport:1", "dock_id": "dock:1", "user_connectable": True,
                "state": "empty", "connector_type": "usb3", "supported_protocols": ["usb3"],
            }],
            "devices": [],
        }
        manager = Mock()
        manager.discover_interfaces.return_value = {
            "physical_interfaces": [],
            "usb_topology": topology,
            "interface_transport_catalog": {
                "schema_version": 1,
                "usb_ports": [{
                    "id": "usbport:1", "owner_kind": "dock", "dock_id": "dock:1",
                    "label": "拓展坞 1 · USB3-1", "endpoints": [],
                }],
                "unlocated_usb_interface_ids": [],
                "native_serial_interface_ids": [],
                "ethernet_interface_ids": [],
            },
            "defaults": [],
            "sensor_type_profiles": [],
            "channel_metadata": {},
            "error": "",
        }
        payload = LocalCaptureAgent(manager=manager).discover()
        self.assertEqual(payload["usb_topology"]["schema_version"], 2)
        self.assertEqual(payload["usb_topology"]["docks"], [])
        self.assertEqual(payload["usb_topology"]["hubs"][0]["classification"], "unconfirmed_accessory")
        self.assertEqual(payload["usb_topology"]["usb_ports"][0]["owner_kind"], "hub")
        self.assertEqual(payload["interfaces"], [])
        self.assertTrue(payload["capabilities"]["usb_topology_v1"])
        self.assertTrue(payload["capabilities"]["usb_topology_v2"])
        self.assertTrue(payload["capabilities"]["interface_transport_catalog_v1"])
        self.assertTrue(payload["capabilities"]["interface_transport_catalog_v2"])
        self.assertEqual(payload["interface_transport_catalog"]["schema_version"], 2)
        self.assertEqual(payload["interface_transport_catalog"]["usb_ports"][0]["id"], "usbport:1")
        self.assertEqual(payload["interface_transport_catalog"]["usb_ports"][0]["owner_kind"], "hub")
        self.assertEqual(payload["interface_transport_catalog"]["usb_ports"][0]["dock_id"], "")
        self.assertNotIn("拓展坞", payload["interface_transport_catalog"]["usb_ports"][0]["label"])

    def test_sample_batch_reads_only_incremental_rows_when_manager_supports_cursor(self):
        manager = Mock()
        manager.start.return_value = {"running": True}
        manager.status.return_value = {"running": True, "config": {"sample_rate_hz": 10.0}}
        manager.stream_rows_since.side_effect = [
            {
                "rows": [{"温度": 350.0}, {"温度": 351.0}],
                "timestamps": [1.0, 2.0],
                "next_cursor": 2,
                "total_count": 3,
                "buffer_base": 0,
                "truncated": False,
            },
            {
                "rows": [{"温度": 352.0}],
                "timestamps": [3.0],
                "next_cursor": 3,
                "total_count": 3,
                "buffer_base": 0,
                "truncated": False,
            },
        ]
        manager.stream_counts.return_value = {
            "total_count": 3,
            "buffered_count": 3,
            "buffer_base": 0,
        }
        agent = LocalCaptureAgent(manager=manager)
        started = agent.start_capture(Mock())

        first = agent.next_sample_batch(limit=2)
        agent.ack_sample_batch(started["capture_uuid"], first["sequence"])
        second = agent.next_sample_batch(limit=2)

        self.assertEqual(first["rows"], [{"温度": 350.0}, {"温度": 351.0}])
        self.assertEqual(second["rows"], [{"温度": 352.0}])
        self.assertEqual(
            [call.args[:2] for call in manager.stream_rows_since.call_args_list],
            [(0, 2), (2, 2)],
        )
        manager.numeric_matrix.assert_not_called()

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

    def test_stop_marks_pending_rows_as_history_and_completes_after_final_ack(self):
        manager = Mock()
        manager.start.return_value = {"running": True}
        manager.stop.return_value = {
            "running": False,
            "capture_saved": True,
            "finalization_complete": True,
        }
        manager.status.return_value = {
            "running": True,
            "sample_count": 2,
            "config": {"sample_rate": 10.0},
        }
        manager.numeric_matrix.return_value = (
            [{"温度": 350.0}, {"温度": 351.0}],
            [1.0, 1.1],
        )
        agent = LocalCaptureAgent(manager=manager)
        started = agent.start_capture(Mock())
        pending = agent.next_sample_batch(limit=1)
        self.assertTrue(pending["status"]["running"])

        manager.status.return_value = {
            "running": False,
            "sample_count": 2,
            "capture_saved": True,
            "finalization_complete": True,
            "config": {"sample_rate": 10.0},
        }
        stopped = agent.stop_capture()
        replay = agent.next_sample_batch(limit=1)

        self.assertEqual(stopped["flush_state"], "flushing")
        self.assertEqual(stopped["generated_rows"], 2)
        self.assertEqual(stopped["queued_rows"], 2)
        self.assertIsNotNone(stopped["local_stopped_at"])
        self.assertEqual(replay["capture_uuid"], started["capture_uuid"])
        self.assertFalse(replay["status"]["running"])
        self.assertEqual(replay["status"]["flush_state"], "flushing")
        self.assertFalse(replay["status"]["finalization_complete"])

        self.assertTrue(agent.ack_sample_batch(started["capture_uuid"], replay["sequence"]))
        second = agent.next_sample_batch(limit=1)
        self.assertFalse(second["status"]["running"])
        self.assertFalse(second["status"]["finalization_complete"])
        self.assertTrue(agent.ack_sample_batch(started["capture_uuid"], second["sequence"]))
        completed = agent.next_sample_batch(limit=1)

        self.assertEqual(completed["rows"], [])
        self.assertFalse(completed["status"]["running"])
        self.assertEqual(completed["status"]["flush_state"], "completed")
        self.assertTrue(completed["status"]["finalization_complete"])
        self.assertEqual(completed["transport"]["helper_queue_depth"], 0)

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
    def test_discover_returns_six_logical_sensor_bindings_without_legacy_thermocouple(self, discover):
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
            [
                "thermocouple64", "plc_process", "uvc_temperature",
                "abb_motion", "m3232_pressure", "esr1001_strain",
            ],
        )
        self.assertNotIn("thermocouple_8ch", [item["role"] for item in result["sensor_bindings"]])
        self.assertEqual(result["sensor_bindings"][1]["physical_interface_id"], "ethernet:工控网卡")
        self.assertEqual(result["sensor_bindings"][3]["physical_interface_id"], "ethernet:工控网卡")
        self.assertEqual(result["sensor_bindings"][4]["physical_interface_id"], "serial:COM3")
        self.assertIsNone(result["sensor_bindings"][5]["physical_interface_id"])
        self.assertEqual(result["sensor_bindings"][5]["protocol"], "modbus_rtu")

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

    @patch("local_capture_agent.select_capture_folder", return_value="D:\\AFP_Capture")
    @patch("local_capture_agent.check_capture_save_root", return_value={"ok": True})
    def test_save_folder_operations_execute_on_helper_computer(self, check_root, select_folder):
        agent = LocalCaptureAgent(manager=Mock())

        selected = agent.select_folder("D:\\")
        checked = agent.check_save_root("D:\\AFP_Capture")

        self.assertEqual(selected, {"selected": True, "path": "D:\\AFP_Capture"})
        self.assertTrue(checked["ok"])
        select_folder.assert_called_once_with("D:\\")
        check_root.assert_called_once_with("D:\\AFP_Capture")


if __name__ == "__main__":
    unittest.main()
