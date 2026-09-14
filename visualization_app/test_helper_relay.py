from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helper_relay import HelperRegistry  # noqa: E402


class HelperRelayTests(unittest.TestCase):
    def test_pairing_is_one_time_and_status_hides_secret(self):
        registry = HelperRegistry()
        challenge = registry.start_pairing("session-a")
        result = registry.complete_pairing(
            challenge["challenge"], "device-a", {"hardware_discovery": True}
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["pairing_token"])
        self.assertFalse(
            registry.complete_pairing(
                challenge["challenge"], "device-b", {}
            )["ok"]
        )
        status = registry.status("session-a")
        self.assertTrue(status["paired"])
        self.assertNotIn("pairing_token", status)

    def test_command_requires_pairing_and_allow_list(self):
        registry = HelperRegistry()
        self.assertEqual(
            registry.command("session-a", "discover", {})["error"],
            "helper_not_paired",
        )
        challenge = registry.start_pairing("session-a")
        registry.complete_pairing(challenge["challenge"], "device-a", {})
        self.assertEqual(registry.command("session-a", "discover", {})["command"], "discover")
        self.assertEqual(
            registry.command("session-a", "shell", {})["error"],
            "command_not_allowed",
        )

    def test_http_poll_and_result_use_pairing_token(self):
        registry = HelperRegistry()
        challenge = registry.start_pairing("session-a")
        paired = registry.complete_pairing(challenge["challenge"], "device-a", {})
        token = paired["pairing_token"]
        self.assertTrue(registry.authenticate("device-a", token))
        request = registry.command("session-a", "discover", {})
        polled = registry.poll("device-a", token)
        self.assertEqual(polled["command"]["request_id"], request["request_id"])
        registry.accept_result("device-a", token, request["request_id"], {"ok": True})
        self.assertEqual(registry.pop_result("session-a", request["request_id"]), {"ok": True})


if __name__ == "__main__":
    unittest.main()
