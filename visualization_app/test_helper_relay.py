from __future__ import annotations

import sys
import time
import unittest
import tempfile
from unittest.mock import patch
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
        self.assertEqual(polled["command"]["type"], "command")
        self.assertEqual(polled["command"]["request_id"], request["request_id"])
        registry.accept_result("device-a", token, request["request_id"], {"ok": True})
        self.assertEqual(registry.pop_result("session-a", request["request_id"]), {"ok": True})

    def test_mysql_relation_map_is_allowlisted(self):
        registry = HelperRegistry()
        challenge = registry.start_pairing("session-a")
        registry.complete_pairing(challenge["challenge"], "device-a", {})
        request = registry.command("session-a", "mysql_relation_map", {})
        self.assertTrue(request["ok"])
        self.assertEqual(request["command"], "mysql_relation_map")

    def test_pairing_accepts_code_copied_with_display_label(self):
        registry = HelperRegistry()
        challenge = registry.start_pairing("session-a")
        displayed = f"配对码：{challenge['challenge']}"
        result = registry.complete_pairing(displayed, "device-a", {})
        self.assertTrue(result["ok"])

    def test_stale_helper_is_reported_offline_and_command_is_not_queued(self):
        registry = HelperRegistry(heartbeat_ttl_seconds=15)
        challenge = registry.start_pairing("session-a")
        registry.complete_pairing(challenge["challenge"], "device-a", {})
        with patch("helper_relay.time.time", return_value=time.time() + 16):
            status = registry.status("session-a")
            request = registry.command("session-a", "discover", {})
        self.assertTrue(status["paired"])
        self.assertFalse(status["online"])
        self.assertFalse(request["queued"])
        self.assertEqual(request["error"], "helper_offline")

    def test_pairing_survives_server_restart_without_persisting_plaintext_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper_registry.json"
            first = HelperRegistry(persistence_path=path)
            challenge = first.start_pairing("session-a")
            paired = first.complete_pairing(challenge["challenge"], "device-a", {"discover": True})
            token = paired["pairing_token"]
            self.assertTrue(path.is_file())
            self.assertNotIn(token, path.read_text(encoding="utf-8"))

            restarted = HelperRegistry(persistence_path=path)
            status = restarted.status("session-a")
            self.assertTrue(status["paired"])
            self.assertFalse(status["online"])
            self.assertEqual(restarted.authenticate("device-a", token), "session-a")


if __name__ == "__main__":
    unittest.main()
