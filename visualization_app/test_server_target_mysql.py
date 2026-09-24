from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server_target_mysql import ServerTargetProfiles  # noqa: E402
from app import helper_real_capture_payload  # noqa: E402


class ServerTargetProfilesTests(unittest.TestCase):
    def test_helper_real_capture_payload_keeps_local_mysql_and_removes_target_secret(self):
        payload = {
            "mysql_enabled": True, "mysql_host": "db.example", "mysql_port": 3306,
            "mysql_user": "afp_app", "mysql_password": "server-only-secret",
            "mysql_database": "afp", "mysql_target_config_id": "opaque-id",
            "mysql_local_enabled": True, "mysql_local_host": "127.0.0.1",
            "mysql_local_password": "", "simulation_mysql_password": "server-only-secret",
        }
        helper = helper_real_capture_payload(payload)
        self.assertFalse(helper["mysql_enabled"])
        self.assertTrue(helper["mysql_local_enabled"])
        self.assertEqual(helper["mysql_local_host"], "127.0.0.1")
        self.assertNotIn("server-only-secret", str(helper))
        self.assertNotIn("mysql_target_config_id", helper)

    def setUp(self):
        self.profiles = ServerTargetProfiles(lambda: {"target": {
            "host": "db.example", "port": 3306, "user": "afp_app",
            "database": "afp", "password": "server-only-secret",
        }})
        self.default = {
            "mysql_host": "db.example", "mysql_port": 3306,
            "mysql_user": "afp_app", "mysql_database": "afp",
            "mysql_password": "", "mysql_enabled": True,
        }

    def test_blank_web_password_uses_exact_server_profile_without_exposing_secret(self):
        selection = self.profiles.resolve("session-A", self.default)
        self.assertEqual(selection.settings.password, "server-only-secret")
        self.assertNotIn("server-only-secret", repr(selection.public()))
        self.assertEqual(self.profiles.resolve_id("session-A", selection.config_id).settings.password,
                         "server-only-secret")
        with self.assertRaises(ValueError):
            self.profiles.resolve_id("session-B", selection.config_id)

    def test_changed_target_cannot_borrow_server_password(self):
        for change in ({"mysql_host": "other-db"}, {"mysql_port": 3307},
                       {"mysql_user": "other-user"}, {"mysql_database": "other_db"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "密码"):
                self.profiles.resolve("session-A", {**self.default, **change})

    def test_explicit_wrong_password_is_never_replaced(self):
        selection = self.profiles.resolve("session-A", {**self.default, "mysql_password": "wrong"})
        self.assertEqual(selection.settings.password, "wrong")

    def test_preflight_identifier_reuses_exact_configuration_and_rejects_changed_form(self):
        selected = self.profiles.resolve("session-A", self.default)
        current = {**self.default, "mysql_target_config_id": selected.config_id}
        self.assertIs(self.profiles.for_request("session-A", current), selected)
        with self.assertRaises(ValueError):
            self.profiles.for_request("session-A", {**current, "mysql_host": "other-db"})
        with self.assertRaises(ValueError):
            self.profiles.for_request("session-B", current)


if __name__ == "__main__":
    unittest.main()
