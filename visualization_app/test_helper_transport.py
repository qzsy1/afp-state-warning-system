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

dispatch_command = helper_entry.dispatch_command
resolve_runtime_args = helper_entry.resolve_runtime_args
should_repair_pairing = helper_entry.should_repair_pairing


class HelperTransportTests(unittest.TestCase):
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
        self.assertIn("ThreadPoolExecutor", source)
        self.assertIn("executor.submit", source)
        self.assertIn("_dispatch_and_send_websocket", source)

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

    def test_helper_cli_prefills_current_tunnel_url_instead_of_stale_saved_url(self):
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
            self.assertEqual(result, ("https://current-public.trycloudflare.com/", expected))
            self.assertEqual(seen["server"], "https://current-public.trycloudflare.com/")

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

    def test_background_helper_uses_current_tunnel_instead_of_stale_saved_url(self):
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

            self.assertEqual(result.server, "https://current-public.trycloudflare.com/")
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
        self.assertIn("ThreadPoolExecutor", source)
        self.assertIn("max_workers=1", source)
        self.assertIn("executor.submit", source)

    def test_http_poll_loop_keeps_heartbeats_while_command_is_slow(self):
        polls = []
        results = []
        finished = threading.Event()

        class FakeTransport:
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
                                "request_id": "slow-check",
                                "command": "check_capture",
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
            return {"request_id": "slow-check", "payload": {"ok": False}}

        with unittest.mock.patch.object(helper_entry, "HelperTransport", FakeTransport), \
             unittest.mock.patch.object(helper_entry, "LocalCaptureAgent", return_value=Mock()), \
             unittest.mock.patch.object(helper_entry, "dispatch_command", side_effect=slow_dispatch):
            helper_entry.run_http_forever("https://example.test", "token", "local-helper")

        self.assertGreaterEqual(len(polls), 4)
        self.assertTrue(finished.wait(2.0))
        self.assertEqual(results[0][0], "api/helper/result")


if __name__ == "__main__":
    unittest.main()
