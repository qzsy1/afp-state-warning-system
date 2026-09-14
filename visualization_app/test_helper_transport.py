from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_capture_helper_entry import dispatch_command, resolve_runtime_args  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
