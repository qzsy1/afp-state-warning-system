from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_capture_helper_entry as helper_entry  # noqa: E402

dispatch_command = helper_entry.dispatch_command
resolve_runtime_args = helper_entry.resolve_runtime_args
should_repair_pairing = helper_entry.should_repair_pairing


class HelperTransportTests(unittest.TestCase):
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
            [], input_fn=lambda _prompt: "", output_fn=output.append
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
            save_runtime_config(
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
                    "transport": "https",
                },
            )
            self.assertEqual(payload["version"], 1)

    def test_helper_cli_resumes_saved_config_without_prompting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper-config.json"
            save_runtime_config = getattr(helper_entry, "save_runtime_config", None)
            self.assertIsNotNone(save_runtime_config)
            save_runtime_config(
                "https://afp.example.test",
                "secret-pairing-token",
                "local-helper",
                path=path,
            )

            def unexpected_prompt(_prompt):
                raise AssertionError("saved helper config should avoid prompting")

            result = resolve_runtime_args(
                [], input_fn=unexpected_prompt, output_fn=lambda _line: None, config_path=path
            )
            self.assertEqual(result.server, "https://afp.example.test")
            self.assertEqual(result.pairing_token, "secret-pairing-token")
            self.assertEqual(result.device_id, "local-helper")
            self.assertEqual(result.transport, "https")

    def test_authentication_failure_is_reported_as_repairable_runtime_state(self):
        authentication_failure_message = getattr(helper_entry, "authentication_failure_message", None)
        self.assertIsNotNone(authentication_failure_message)
        message = authentication_failure_message()
        self.assertIn("重新生成配对码", message)
        self.assertIn("辅助程序不会直接退出", message)


if __name__ == "__main__":
    unittest.main()
