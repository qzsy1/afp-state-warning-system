from __future__ import annotations

import sys
import json
import inspect
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_capture_helper_entry as helper_entry  # noqa: E402
import local_capture_agent  # noqa: E402

dispatch_command = helper_entry.dispatch_command
resolve_runtime_args = helper_entry.resolve_runtime_args
should_repair_pairing = helper_entry.should_repair_pairing


def _blocking_hardware_check_worker(_message, _result_queue):
    time.sleep(10.0)


class HelperTransportTests(unittest.TestCase):
    def test_helper_handshake_declares_protocol_and_isolated_check_lifecycle(self):
        capabilities = helper_entry.helper_capabilities()
        self.assertGreaterEqual(capabilities["protocol_version"], 2)
        self.assertEqual(
            capabilities["build_id"],
            "20261008-helper-full-diagnosis-v1",
        )
        self.assertEqual(capabilities["command_lifecycle"], "isolated_hardware_check")
        self.assertTrue(capabilities["simulation_replay_v1"])

    def test_dispatcher_materializes_simulation_source_before_start(self):
        agent = Mock()
        agent.prepare_simulation_source.return_value = {
            "path": "C:/helper-cache/content/source.csv",
            "source_type": "single_csv",
            "cache_reused": False,
        }
        agent.start_capture.return_value = {"ok": True, "running": True}
        source_fetcher = Mock()
        dispatcher = helper_entry.HelperCommandDispatcher(
            agent,
            source_fetcher=source_fetcher,
        )
        responses = []
        try:
            dispatcher.submit(
                {
                    "type": "command",
                    "request_id": "simulation-start-1",
                    "command": "start_capture",
                    "payload": {
                        "acquisition_mode": "simulation",
                        "simulation_source_transfer": {
                            "ticket": "ticket-a",
                            "manifest": {
                                "source_type": "single_csv",
                                "files": [{"relative_path": "source.csv"}],
                            },
                        },
                    },
                },
                responses.append,
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not responses:
                time.sleep(0.01)
            self.assertTrue(responses[0]["payload"]["running"])
            agent.prepare_simulation_source.assert_called_once()
            config = agent.start_capture.call_args.args[0]
            self.assertEqual(config.acquisition_mode, "simulation")
            self.assertEqual(
                config.simulation_source_path,
                "C:/helper-cache/content/source.csv",
            )
        finally:
            dispatcher.close()

    def test_blocking_hardware_check_does_not_delay_status_or_capture_control(self):
        dispatcher_type = getattr(helper_entry, "HelperCommandDispatcher", None)
        self.assertIsNotNone(
            dispatcher_type,
            "helper must route hardware checks outside the capture-control executor",
        )

        class BlockingCheckRunner:
            def __init__(self):
                self.started = threading.Event()
                self.cancelled = threading.Event()

            def start(self, _message, _callback):
                self.started.set()
                return True

            def cancel(self, **_kwargs):
                self.cancelled.set()
                return True

            def close(self):
                self.cancelled.set()

        runner = BlockingCheckRunner()
        agent = Mock()
        agent.status.return_value = {"ok": True, "running": False}
        agent.start_capture.return_value = {"ok": True, "running": True}
        dispatcher = dispatcher_type(agent, check_runner=runner)
        results = []
        try:
            dispatcher.submit(
                {
                    "type": "command",
                    "request_id": "check-1",
                    "command": "check_capture",
                    "payload": {},
                },
                results.append,
            )
            self.assertTrue(runner.started.wait(0.2))
            dispatcher.submit(
                {
                    "type": "command",
                    "request_id": "status-1",
                    "command": "status",
                    "payload": {},
                },
                results.append,
            )
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and not results:
                time.sleep(0.01)
            self.assertEqual(results[0]["request_id"], "status-1")

            dispatcher.submit(
                {
                    "type": "command",
                    "request_id": "start-1",
                    "command": "start_capture",
                    "payload": {},
                },
                results.append,
            )
            self.assertTrue(runner.cancelled.wait(0.2))
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and len(results) < 2:
                time.sleep(0.01)
            self.assertEqual(results[1]["request_id"], "start-1")
        finally:
            dispatcher.close()

    def test_hardware_check_process_is_terminated_at_its_deadline(self):
        runner = helper_entry.HardwareCheckProcess(
            timeout_seconds=1.0,
            worker=_blocking_hardware_check_worker,
        )
        finished = threading.Event()
        responses = []
        try:
            started = runner.start(
                {
                    "type": "command",
                    "request_id": "check-timeout",
                    "command": "check_capture",
                    "payload": {},
                },
                lambda response: (responses.append(response), finished.set()),
            )
            self.assertTrue(started)
            self.assertTrue(finished.wait(3.0))
            self.assertEqual(responses[0]["request_id"], "check-timeout")
            self.assertEqual(
                responses[0]["payload"]["error"],
                "hardware_check_timed_out",
            )
        finally:
            runner.close()

    def test_helper_local_mysql_explicit_first_check_initializes_missing_afp_schema(self):
        store = Mock()
        store.preflight.side_effect = [
            {
                "ok": False,
                "stage": "schema",
                "schema_ready": False,
                "missing_objects": ["afp_condition", "afp_sensor_sample"],
                "error": "AFP数据库表结构不完整",
            },
            {
                "ok": True,
                "stage": "ready",
                "schema_ready": True,
                "missing_objects": [],
                "write_test": True,
            },
        ]
        store.initialize_schema.return_value = {"ok": True, "initialized": True}
        settings = Mock()

        with patch.object(local_capture_agent, "MySQLCaptureStore", return_value=store):
            result = local_capture_agent.LocalCaptureAgent(Mock()).mysql_preflight(
                settings,
                write_test=True,
                initialize_if_missing=True,
            )

        store.initialize_schema.assert_called_once_with(create_database=False)
        self.assertEqual(store.preflight.call_count, 2)
        self.assertTrue(result["ok"])
        self.assertTrue(result["schema_initialized"])
        self.assertEqual(result["scope"], "helper_local")

    def test_helper_local_mysql_schema_initialization_failure_is_not_hidden(self):
        store = Mock()
        store.preflight.return_value = {
            "ok": False,
            "stage": "schema",
            "schema_ready": False,
            "missing_objects": ["afp_condition"],
            "error": "AFP数据库表结构不完整",
        }
        store.initialize_schema.return_value = {
            "ok": False,
            "error": "1142 (42000): CREATE command denied",
        }

        with patch.object(local_capture_agent, "MySQLCaptureStore", return_value=store):
            result = local_capture_agent.LocalCaptureAgent(Mock()).mysql_preflight(
                Mock(), initialize_if_missing=True
            )

        self.assertFalse(result["ok"])
        self.assertIn("1142", result["error"])
        self.assertEqual(result["error_detail"]["category"], "authorization")

    def test_helper_local_mysql_missing_database_is_created_on_explicit_first_check(self):
        store = Mock()
        store.preflight.side_effect = [
            {
                "ok": False,
                "stage": "connect",
                "database": "afp_state_warning",
                "error": "1049 (42000): Unknown database 'afp_state_warning'",
            },
            {
                "ok": True,
                "stage": "ready",
                "schema_ready": True,
                "write_test": True,
                "database": "afp_state_warning",
            },
        ]
        store.initialize_schema.return_value = {
            "ok": True,
            "initialized": True,
            "database_creation_requested": True,
        }

        with patch.object(local_capture_agent, "MySQLCaptureStore", return_value=store):
            result = local_capture_agent.LocalCaptureAgent(Mock()).mysql_preflight(
                Mock(),
                write_test=True,
                initialize_if_missing=True,
            )

        store.initialize_schema.assert_called_once_with(create_database=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["schema_initialized"])
        self.assertEqual(result["scope"], "helper_local")

    def test_ack_driven_sample_pump_sends_next_batch_immediately_after_ack(self):
        pump_type = getattr(helper_entry, "HelperSamplePump", None)
        self.assertIsNotNone(pump_type, "helper must expose an ACK-driven sample pump")
        agent = Mock()
        first = {
            "capture_uuid": "capture-a", "sequence": 0,
            "rows": [{"温度": float(index)} for index in range(25)],
            "timestamps": [index / 50.0 for index in range(25)],
            "status": {"running": True, "config": {"sample_rate": 50.0}},
        }
        second = {
            "capture_uuid": "capture-a", "sequence": 1,
            "rows": [{"温度": 25.0}], "timestamps": [0.5],
            "status": {"running": True, "config": {"sample_rate": 50.0}},
        }
        agent.next_sample_batch.side_effect = [first, second]
        agent.ack_sample_batch.return_value = True
        agent.stream_metrics.return_value = {"queued_rows": 26, "sample_rate_hz": 50.0}
        pump = pump_type(agent)

        message_one = pump.next_message(now=10.0)
        self.assertEqual(message_one["batch"]["sequence"], 0)
        self.assertIsNone(pump.next_message(now=10.1), "only one batch may be in flight")
        self.assertTrue(pump.acknowledge("capture-a", 0, now=10.1))
        message_two = pump.next_message(now=10.1)

        self.assertEqual(message_two["batch"]["sequence"], 1)
        self.assertEqual(agent.next_sample_batch.call_args_list[0].kwargs["limit"], 25)

    def test_adaptive_batch_limit_uses_fixed_route_rtt_without_switching_endpoint(self):
        agent = Mock()
        agent.stream_metrics.return_value = {"sample_rate_hz": 10.0, "queued_rows": 20}

        self.assertEqual(
            helper_entry.adaptive_sample_batch_limit(
                agent, route_type="lan", rtt_ms=20.0
            ),
            2,
        )
        self.assertEqual(
            helper_entry.adaptive_sample_batch_limit(
                agent, route_type="public", rtt_ms=50.0
            ),
            3,
        )
        self.assertEqual(
            helper_entry.adaptive_sample_batch_limit(
                agent, route_type="public", rtt_ms=400.0
            ),
            6,
        )

    def test_public_batch_limit_sustains_ten_hz_over_1_2_second_ack(self):
        agent = Mock()
        agent.stream_metrics.return_value = {
            "sample_rate_hz": 10.0,
            "queued_rows": 450,
        }

        limit = helper_entry.adaptive_sample_batch_limit(
            agent,
            route_type="public",
            rtt_ms=1161.0,
        )

        self.assertGreaterEqual(limit, 14)
        self.assertLessEqual(limit, 50)
        self.assertGreater(limit / 1.161, 10.0)

    def test_sample_pump_tracks_ack_rtt_and_waits_on_stream_event(self):
        batch = {
            "capture_uuid": "capture-a", "sequence": 0,
            "rows": [{"温度": 25.0}], "timestamps": [0.0],
            "status": {"running": True},
        }
        agent = Mock()
        agent.next_sample_batch.return_value = batch
        agent.ack_sample_batch.return_value = True
        agent.stream_metrics.return_value = {
            "capture_uuid": "capture-a", "sample_rate_hz": 10.0, "queued_rows": 1,
        }
        agent.wait_for_stream_activity.return_value = True
        pump = helper_entry.HelperSamplePump(agent, route_type="public")

        self.assertTrue(pump.wait_for_activity(0.25))
        agent.wait_for_stream_activity.assert_called_once_with(0.25)
        self.assertIsNotNone(pump.next_message(now=1.0))
        self.assertTrue(pump.acknowledge("capture-a", 0, now=1.4))
        self.assertAlmostEqual(pump.metrics()["rtt_ms"], 400.0, places=3)
        self.assertTrue(pump.wait_for_activity(0.25))

    def test_ack_driven_sample_pump_replays_pending_batch_after_reconnect(self):
        pump_type = getattr(helper_entry, "HelperSamplePump", None)
        self.assertIsNotNone(pump_type, "helper must expose an ACK-driven sample pump")
        pending = {
            "capture_uuid": "capture-a", "sequence": 4,
            "rows": [{"温度": 350.0}], "timestamps": [1.0],
            "status": {"running": True},
        }
        agent = Mock()
        agent.next_sample_batch.return_value = pending
        agent.stream_metrics.return_value = {"queued_rows": 1, "sample_rate_hz": 10.0}
        pump = pump_type(agent)

        first = pump.next_message(now=1.0)
        pump.connection_lost()
        replay = pump.next_message(now=1.1)

        self.assertEqual(first["batch"], replay["batch"])
        agent.ack_sample_batch.assert_not_called()

    def test_new_capture_is_not_blocked_by_previous_capture_wire_batch(self):
        agent = Mock()
        old_batch = {
            "capture_uuid": "capture-old", "sequence": 7,
            "rows": [{"温度": 350.0}], "timestamps": [1.0],
            "status": {"running": False},
        }
        new_batch = {
            "capture_uuid": "capture-new", "sequence": 0,
            "rows": [{"温度": 351.0}], "timestamps": [0.1],
            "status": {"running": True},
        }
        agent.next_sample_batch.side_effect = [old_batch, new_batch]
        agent.stream_metrics.return_value = {
            "capture_uuid": "capture-old", "queued_rows": 1, "sample_rate_hz": 10.0,
        }
        pump = helper_entry.HelperSamplePump(agent)

        self.assertEqual(pump.next_message(now=1.0)["batch"]["capture_uuid"], "capture-old")
        agent.stream_metrics.return_value = {
            "capture_uuid": "capture-new", "queued_rows": 1, "sample_rate_hz": 10.0,
        }
        message = pump.next_message(now=1.1)

        self.assertIsNotNone(message, "new capture must retire the previous wire batch")
        self.assertEqual(message["batch"]["capture_uuid"], "capture-new")
        self.assertEqual(message["batch"]["sequence"], 0)

    def test_prepare_source_command_downloads_without_starting_capture(self):
        agent = Mock()
        agent.prepare_simulation_source.return_value = {
            "path": "C:/private/cache/source.csv",
            "source_type": "single_csv",
            "content_sha256": "a" * 64,
            "files": 1,
            "relative_paths": ["source.csv"],
            "bytes": 128,
            "channels": ["温度1"],
            "numeric_channels": ["温度1"],
            "valid_rows": 240,
            "state": "ready",
            "cache_hit": False,
        }
        responses = []
        dispatcher = helper_entry.HelperCommandDispatcher(
            agent,
            source_fetcher=lambda *_args: {"ok": True},
        )
        try:
            dispatcher.submit(
                {
                    "type": "command",
                    "request_id": "prepare-a",
                    "command": "prepare_simulation_source",
                    "payload": {
                        "simulation_source_transfer": {"ticket": "ticket-a", "manifest": {}},
                        "selected_sensors": ["温度1"],
                    },
                },
                responses.append,
            )
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not responses:
                time.sleep(0.01)
        finally:
            dispatcher.close()

        self.assertTrue(responses)
        self.assertTrue(responses[0]["payload"]["ok"])
        self.assertEqual(responses[0]["payload"]["state"], "ready")
        self.assertNotIn("path", responses[0]["payload"])
        agent.start_capture.assert_not_called()

    def test_start_uses_prepared_cache_reference_without_downloading_again(self):
        agent = Mock()
        agent.resolve_prepared_simulation_source.return_value = {
            "path": "C:/private/cache/source.csv",
            "source_type": "single_csv",
            "content_sha256": "a" * 64,
            "state": "ready",
        }
        agent.start_capture.return_value = {
            "ok": True, "running": True, "capture_uuid": "capture-a"
        }
        responses = []
        fetcher = Mock(side_effect=AssertionError("ready start must not download source bytes"))
        dispatcher = helper_entry.HelperCommandDispatcher(agent, source_fetcher=fetcher)
        try:
            dispatcher.submit(
                {
                    "type": "command",
                    "request_id": "start-ready",
                    "command": "start_capture",
                    "payload": {
                        "acquisition_mode": "simulation",
                        "selected_sensors": ["温度1"],
                        "simulation_source_ready": {
                            "content_sha256": "a" * 64,
                            "source_type": "single_csv",
                            "relative_paths": ["source.csv"],
                        },
                    },
                },
                responses.append,
            )
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not responses:
                time.sleep(0.01)
        finally:
            dispatcher.close()

        self.assertTrue(responses[0]["payload"]["running"])
        fetcher.assert_not_called()
        agent.resolve_prepared_simulation_source.assert_called_once()

    def test_saved_pairing_server_is_not_rewritten_by_tunnel_hint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            hint = Path(temp_dir) / "funnel-url.txt"
            hint.write_text("https://new-public.example.test/\n", encoding="utf-8")
            selected = helper_entry.preferred_setup_server(
                "http://192.168.10.20:8770",
                server_hint_path=hint,
            )

        self.assertEqual(selected, "http://192.168.10.20:8770")

    def test_helper_capabilities_report_fixed_route_without_credentials_or_path(self):
        capabilities = helper_entry.helper_capabilities(
            "https://user:secret@desktop.example.test/private?token=bad"
        )

        self.assertEqual(capabilities["paired_server_url"], "https://desktop.example.test")
        self.assertEqual(capabilities["paired_route_type"], "public")
        self.assertNotIn("secret", str(capabilities))
        self.assertNotIn("private", str(capabilities))

    def test_ack_driven_sample_pump_keeps_up_with_ten_minutes_at_50hz(self):
        class QueuedAgent:
            def __init__(self):
                self.rows = []
                self.cursor = 0
                self.sequence = 0
                self.pending = None

            def produce_until(self, count):
                while len(self.rows) < count:
                    index = len(self.rows)
                    self.rows.append((index / 50.0, {"温度": float(index)}))

            def stream_metrics(self):
                return {
                    "queued_rows": len(self.rows) - self.cursor,
                    "sample_rate_hz": 50.0,
                }

            def next_sample_batch(self, *, limit):
                if self.pending is not None:
                    return dict(self.pending)
                if self.cursor >= len(self.rows):
                    return None
                selected = self.rows[self.cursor:self.cursor + limit]
                self.pending = {
                    "capture_uuid": "capture-50hz",
                    "sequence": self.sequence,
                    "timestamps": [item[0] for item in selected],
                    "rows": [item[1] for item in selected],
                    "status": {"running": True},
                }
                return dict(self.pending)

            def ack_sample_batch(self, capture_uuid, sequence):
                if capture_uuid != "capture-50hz" or sequence != self.sequence:
                    return False
                self.cursor += len(self.pending["rows"])
                self.sequence += 1
                self.pending = None
                return True

        agent = QueuedAgent()
        pump = helper_entry.HelperSamplePump(agent)
        latencies = []
        for tick in range(1, 2401):
            now = tick * 0.25
            agent.produce_until(min(30_000, int(now * 50)))
            while True:
                message = pump.next_message(now=now)
                if message is None:
                    break
                batch = message["batch"]
                latencies.extend(now - timestamp for timestamp in batch["timestamps"])
                self.assertTrue(
                    pump.acknowledge(batch["capture_uuid"], batch["sequence"], now=now)
                )

        ordered = sorted(latencies)
        p95 = ordered[int(len(ordered) * 0.95) - 1]
        self.assertEqual(agent.cursor, 30_000)
        self.assertEqual(agent.stream_metrics()["queued_rows"], 0)
        self.assertLessEqual(p95, 1.0)

    def test_http_sample_flush_acknowledges_only_accepted_batch(self):
        agent = Mock()
        agent.next_sample_batch.return_value = {
            "capture_uuid": "capture-a",
            "sequence": 3,
            "rows": [{"温度": 350.0}],
            "timestamps": [1.0],
            "status": {"running": True},
        }
        transport = Mock()
        transport.http_json.return_value = {
            "ok": True,
            "capture_uuid": "capture-a",
            "ack_sequence": 3,
        }

        result = helper_entry.flush_http_sample_batch(agent, transport, "pc-01")

        self.assertTrue(result["ok"])
        transport.http_json.assert_called_once()
        self.assertEqual(transport.http_json.call_args.args[0], "api/helper/samples")
        agent.ack_sample_batch.assert_called_once_with("capture-a", 3)

    def test_http_sample_flush_keeps_pending_batch_when_server_rejects_it(self):
        agent = Mock()
        agent.next_sample_batch.return_value = {
            "capture_uuid": "capture-a",
            "sequence": 3,
            "rows": [],
            "timestamps": [],
            "status": {"running": True},
        }
        transport = Mock()
        transport.http_json.return_value = {"ok": False, "error": "sample_sequence_gap"}

        helper_entry.flush_http_sample_batch(agent, transport, "pc-01")

        agent.ack_sample_batch.assert_not_called()

    def test_websocket_hardware_commands_run_off_heartbeat_loop(self):
        source = inspect.getsource(helper_entry.run_forever)
        self.assertIn("HelperCommandDispatcher", source)
        self.assertIn("dispatcher.submit", source)
        dispatcher_source = inspect.getsource(helper_entry.HelperCommandDispatcher)
        self.assertIn("HardwareCheckProcess", dispatcher_source)

    @patch("local_capture_helper_entry.run_http_forever")
    @patch("local_capture_helper_entry.run_forever", return_value=False)
    def test_auto_fallback_reuses_same_agent_instance(self, run_wss, run_http):
        from local_capture_helper_entry import run_auto_forever

        sentinel_agent = object()
        run_auto_forever("https://afp.example.test", "token", "pc-01", agent=sentinel_agent)

        self.assertIs(run_wss.call_args.kwargs["agent"], sentinel_agent)
        self.assertIs(run_http.call_args.kwargs["agent"], sentinel_agent)

    def test_dispatch_discover_returns_structured_result(self):
        agent = Mock()
        agent.discover.return_value = {"sensor_bindings": []}
        result = dispatch_command(
            agent,
            '{"type":"command","request_id":"r1","command":"discover","payload":{}}',
        )
        self.assertEqual(result["type"], "result")
        self.assertEqual(result["request_id"], "r1")
        self.assertEqual(result["payload"]["sensor_bindings"], [])

    def test_dispatch_rejects_unknown_command(self):
        with self.assertRaises(ValueError):
            dispatch_command(
                Mock(),
                '{"type":"command","request_id":"r1","command":"shell"}',
            )

    def test_dispatch_returns_known_command_failure_to_webpage(self):
        agent = Mock()
        agent.discover.side_effect = RuntimeError("设备枚举失败")
        result = dispatch_command(
            agent,
            '{"type":"command","request_id":"r1","command":"discover","payload":{}}',
        )
        self.assertEqual(result["request_id"], "r1")
        self.assertFalse(result["payload"]["ok"])
        self.assertIn("设备枚举失败", result["payload"]["error"])

    def test_mysql_profile_commands_persist_locally_and_preflight_without_browser_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper-config.json"
            helper_entry.save_runtime_config(
                "https://afp.example.test", "pair-token", path=path
            )
            agent = Mock()
            agent.mysql_preflight.return_value = {"ok": True, "database": "afp_state_warning"}
            save_command = {
                "type": "command",
                "request_id": "save-1",
                "command": "mysql_profile_save",
                "payload": {
                    "mysql_enabled": True,
                    "mysql_host": "127.0.0.1",
                    "mysql_port": 3306,
                    "mysql_user": "root",
                    "mysql_password": "local-secret",
                    "mysql_database": "afp_state_warning",
                },
            }
            preflight_command = {
                "type": "command",
                "request_id": "test-1",
                "command": "mysql_preflight",
                "payload": {
                    "use_saved_profile": True,
                    "write_test": True,
                    "initialize_if_missing": True,
                },
            }
            with patch.object(helper_entry, "default_runtime_config_path", return_value=path):
                saved = dispatch_command(agent, json.dumps(save_command))
                tested = dispatch_command(agent, json.dumps(preflight_command))

        self.assertTrue(saved["payload"]["configured"])
        self.assertTrue(tested["payload"]["ok"])
        self.assertEqual(tested["payload"]["scope"], "helper_local")
        self.assertEqual(tested["payload"]["execution_host"], "visitor_local_computer")
        settings = agent.mysql_preflight.call_args.args[0]
        self.assertEqual(settings.password, "local-secret")
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertTrue(agent.mysql_preflight.call_args.kwargs["write_test"])
        self.assertTrue(agent.mysql_preflight.call_args.kwargs["initialize_if_missing"])

    def test_helper_cli_without_arguments_returns_setup_guidance_instead_of_argparse_exit(self):
        output = []
        result = resolve_runtime_args(
            [], input_fn=lambda _prompt: "", output_fn=output.append,
            config_path=Path(__file__).with_name(".missing-helper-config.json"),
        )
        self.assertIsNone(result)
        self.assertTrue(any("网页地址" in line for line in output))
        self.assertTrue(any("配对码" in line for line in output))

    def test_helper_cli_prompts_for_pairing_code_when_server_is_given(self):
        prompts = []
        result = resolve_runtime_args(
            ["--server", "http://127.0.0.1:8770"],
            input_fn=lambda prompt: (prompts.append(prompt) or "challenge-1"),
            output_fn=lambda _line: None,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.server, "http://127.0.0.1:8770")
        self.assertEqual(result.pairing_challenge, "challenge-1")
        self.assertTrue(any("配对码" in prompt for prompt in prompts))

    def test_helper_cli_normalizes_displayed_pairing_code_label(self):
        result = resolve_runtime_args(
            ["--server", "http://127.0.0.1:8770"],
            input_fn=lambda _prompt: "配对码：challenge-1",
            output_fn=lambda _line: None,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.pairing_challenge, "challenge-1")

    def test_helper_cli_falls_back_to_setup_gui_when_console_input_is_unavailable(self):
        expected = object()
        def unavailable(_prompt):
            raise EOFError
        result = resolve_runtime_args(
            [], input_fn=unavailable, gui_fn=lambda _server="": expected,
            output_fn=lambda _line: None,
            config_path=Path(__file__).with_name(".missing-helper-config.json"),
        )
        self.assertIs(result, expected)

    def test_helper_cli_uses_setup_gui_if_pairing_prompt_closes(self):
        expected = object()
        result = resolve_runtime_args(
            ["--server", "https://example.test"],
            input_fn=lambda _prompt: (_ for _ in ()).throw(EOFError),
            gui_fn=lambda server="": (server, expected),
            output_fn=lambda _line: None,
        )
        self.assertEqual(result, ("https://example.test", expected))

    def test_authentication_failure_requires_repair_instead_of_silent_retry(self):
        self.assertTrue(
            should_repair_pairing(RuntimeError("辅助服务连接失败：HTTP Error 401: Unauthorized"))
        )
        self.assertFalse(should_repair_pairing(RuntimeError("网络连接暂时失败")))

    def test_pairing_config_round_trips_without_plaintext_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper-config.json"
            save_runtime_config = getattr(helper_entry, "save_runtime_config", None)
            load_saved_runtime_config = getattr(helper_entry, "load_saved_runtime_config", None)
            self.assertIsNotNone(save_runtime_config)
            self.assertIsNotNone(load_saved_runtime_config)
            helper_entry.save_runtime_config(
                "https://afp.example.test",
                "secret-pairing-token",
                "local-helper",
                path=path,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("secret-pairing-token", path.read_text(encoding="utf-8"))
            self.assertEqual(
                load_saved_runtime_config(path=path),
                {
                    "server": "https://afp.example.test",
                    "pairing_token": "secret-pairing-token",
                    "device_id": "local-helper",
                    "transport": "auto",
                },
            )
            self.assertEqual(payload["version"], 1)

    def test_local_mysql_profile_round_trips_without_plaintext_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper-config.json"
            helper_entry.save_runtime_config(
                "https://afp.example.test",
                "secret-pairing-token",
                "local-helper",
                path=path,
            )
            metadata = helper_entry.save_local_mysql_profile(
                {
                    "mysql_enabled": True,
                    "mysql_host": "127.0.0.1",
                    "mysql_port": 3306,
                    "mysql_user": "root",
                    "mysql_password": "machine-only-secret",
                    "mysql_database": "afp_state_warning",
                },
                path=path,
            )
            loaded = helper_entry.load_local_mysql_profile(path=path)
            raw = path.read_text(encoding="utf-8")

        self.assertTrue(metadata["configured"])
        self.assertEqual(metadata["scope"], "helper_local")
        self.assertNotIn("machine-only-secret", raw)
        self.assertEqual(loaded["mysql_password"], "machine-only-secret")
        self.assertEqual(loaded["mysql_host"], "127.0.0.1")

    def test_unreadable_local_mysql_profile_is_unconfigured_not_empty_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper-config.json"
            helper_entry.save_runtime_config(
                "https://afp.example.test",
                "secret-pairing-token",
                "local-helper",
                path=path,
            )
            helper_entry.save_local_mysql_profile(
                {
                    "mysql_enabled": True,
                    "mysql_host": "127.0.0.1",
                    "mysql_port": 3306,
                    "mysql_user": "root",
                    "mysql_password": "secret",
                    "mysql_database": "afp_state_warning",
                },
                path=path,
            )
            with patch.object(helper_entry, "_unprotect_secret", side_effect=OSError("DPAPI")):
                self.assertIsNone(helper_entry.load_local_mysql_profile(path=path))
                metadata = helper_entry.local_mysql_profile_metadata(path=path)

        self.assertFalse(metadata["configured"])
        self.assertEqual(metadata["state"], "decrypt_failed")

    def test_helper_cli_shows_setup_gui_for_saved_config_when_double_clicked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper-config.json"
            hint_path = Path(directory) / "missing-tunnel-url.txt"
            save_runtime_config = getattr(helper_entry, "save_runtime_config", None)
            self.assertIsNotNone(save_runtime_config)
            save_runtime_config(
                "https://afp.example.test",
                "secret-pairing-token",
                "local-helper",
                path=path,
            )

            def unexpected_prompt(_prompt):
                raise AssertionError("saved helper config should use the setup gui")

            expected = object()
            result = resolve_runtime_args(
                [],
                input_fn=unexpected_prompt,
                output_fn=lambda _line: None,
                gui_fn=lambda server="": (server, expected),
                config_path=path,
                server_hint_path=hint_path,
            )
            self.assertEqual(result, ("https://afp.example.test", expected))

    def test_helper_cli_preserves_saved_pairing_url_when_tunnel_hint_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper-config.json"
            hint_path = Path(directory) / "quick-tunnel-url.txt"
            hint_path.write_text("https://current-public.trycloudflare.com/\n", encoding="utf-8")
            save_runtime_config = getattr(helper_entry, "save_runtime_config", None)
            self.assertIsNotNone(save_runtime_config)
            save_runtime_config(
                "https://stale-public.trycloudflare.com",
                "secret-pairing-token",
                "local-helper",
                path=path,
            )

            seen = {}
            expected = object()
            result = resolve_runtime_args(
                [],
                input_fn=lambda _prompt: (_ for _ in ()).throw(AssertionError("should use gui")),
                output_fn=lambda _line: None,
                gui_fn=lambda server="": (seen.setdefault("server", server), expected),
                config_path=path,
                server_hint_path=hint_path,
            )
            self.assertEqual(result, ("https://stale-public.trycloudflare.com", expected))
            self.assertEqual(seen["server"], "https://stale-public.trycloudflare.com")

    def test_helper_accepts_powershell_utf8_bom_in_tunnel_url_file(self):
        with tempfile.TemporaryDirectory() as directory:
            hint_path = Path(directory) / "quick-tunnel-url.txt"
            hint_path.write_text(
                "\ufeffhttps://current-public.trycloudflare.com/\r\n",
                encoding="utf-8",
            )

            self.assertEqual(
                helper_entry.load_current_server_hint(path=hint_path),
                "https://current-public.trycloudflare.com/",
            )

    def test_helper_default_server_hint_prefers_tailscale_funnel_file(self):
        hint_path = helper_entry.default_server_hint_path()
        self.assertEqual(hint_path.name, "funnel-url.txt")
        self.assertIn("tailscale", str(hint_path).lower())

    def test_helper_cli_resumes_saved_config_in_background_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper-config.json"
            hint_path = Path(directory) / "missing-tunnel-url.txt"
            save_runtime_config = getattr(helper_entry, "save_runtime_config", None)
            self.assertIsNotNone(save_runtime_config)
            save_runtime_config(
                "https://afp.example.test",
                "secret-pairing-token",
                "local-helper",
                path=path,
            )

            def unexpected_prompt(_prompt):
                raise AssertionError("background helper config should avoid prompting")

            result = resolve_runtime_args(
                ["--background"],
                input_fn=unexpected_prompt,
                output_fn=lambda _line: None,
                config_path=path,
                server_hint_path=hint_path,
            )
            self.assertEqual(result.server, "https://afp.example.test")
            self.assertEqual(result.pairing_token, "secret-pairing-token")
            self.assertEqual(result.device_id, "local-helper")
            self.assertEqual(result.transport, "auto")

    def test_background_helper_preserves_saved_pairing_url_when_tunnel_hint_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper-config.json"
            hint_path = Path(directory) / "quick-tunnel-url.txt"
            hint_path.write_text("https://current-public.trycloudflare.com/\n", encoding="utf-8")
            helper_entry.save_runtime_config(
                "https://stale-public.trycloudflare.com",
                "secret-pairing-token",
                "local-helper",
                path=path,
            )

            result = resolve_runtime_args(
                ["--background"],
                input_fn=lambda _prompt: (_ for _ in ()).throw(AssertionError("should not prompt")),
                output_fn=lambda _line: None,
                config_path=path,
                server_hint_path=hint_path,
            )

            self.assertEqual(result.server, "https://stale-public.trycloudflare.com")
            self.assertEqual(result.pairing_token, "secret-pairing-token")

    def test_authentication_failure_is_reported_as_repairable_runtime_state(self):
        authentication_failure_message = getattr(helper_entry, "authentication_failure_message", None)
        self.assertIsNotNone(authentication_failure_message)
        message = authentication_failure_message()
        self.assertIn("重新生成配对码", message)
        self.assertIn("辅助程序不会直接退出", message)

    def test_autostart_script_restarts_the_single_delivery_helper(self):
        script = Path(__file__).with_name("install_local_helper_autostart.ps1")
        self.assertTrue(script.is_file())
        text = script.read_text(encoding="utf-8")
        self.assertIn("Register-ScheduledTask", text)
        self.assertIn("RestartCount", text)
        self.assertIn("AFP_Local_Capture_Helper.exe", text)
        self.assertIn("-Uninstall", text)
        self.assertIn('Join-Path $PSScriptRoot "local_helper"', text)

    def test_helper_uses_a_single_instance_mutex(self):
        acquire = getattr(helper_entry, "acquire_single_instance_lock", None)
        release = getattr(helper_entry, "release_single_instance_lock", None)
        self.assertIsNotNone(acquire)
        self.assertIsNotNone(release)
        first = acquire("AFP_Local_Capture_Helper_test")
        try:
            self.assertIsNotNone(first)
            self.assertIsNone(acquire("AFP_Local_Capture_Helper_test"))
        finally:
            release(first)

    def test_http_poll_loop_dispatches_hardware_checks_off_the_heartbeat_thread(self):
        source = inspect.getsource(helper_entry.run_http_forever)
        self.assertIn("HelperCommandDispatcher", source)
        self.assertIn("dispatcher.submit", source)
        self.assertNotIn("max_workers=1", source)

    def test_http_poll_loop_keeps_heartbeats_while_command_is_slow(self):
        polls = []
        results = []
        finished = threading.Event()

        class FakeTransport:
            decode_command = staticmethod(local_capture_agent.HelperTransport.decode_command)

            def __init__(self, *_args, **_kwargs):
                pass

            def http_json(self, path, payload, **_kwargs):
                if path == "api/helper/poll":
                    polls.append(time.monotonic())
                    if len(polls) == 1:
                        return {
                            "ok": True,
                            "command": {
                                "type": "command",
                                "request_id": "slow-discover",
                                "command": "discover",
                                "payload": {},
                            },
                        }
                    if len(polls) >= 4:
                        raise KeyboardInterrupt
                    return {"ok": True, "command": None, "heartbeat": True}
                results.append((path, payload))
                finished.set()
                return {"ok": True}

        def slow_dispatch(_agent, _raw):
            time.sleep(0.7)
            return {"request_id": "slow-discover", "payload": {"ok": False}}

        with unittest.mock.patch.object(helper_entry, "HelperTransport", FakeTransport), \
             unittest.mock.patch.object(helper_entry, "LocalCaptureAgent", return_value=Mock()), \
             unittest.mock.patch.object(helper_entry, "dispatch_command", side_effect=slow_dispatch):
            helper_entry.run_http_forever("https://example.test", "token", "local-helper")

        self.assertGreaterEqual(len(polls), 4)
        self.assertTrue(finished.wait(2.0))
        self.assertEqual(results[0][0], "api/helper/result")


if __name__ == "__main__":
    unittest.main()
