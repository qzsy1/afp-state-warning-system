from __future__ import annotations

import importlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


class ReversibleTestProtector:
    def protect(self, value: bytes) -> bytes:
        return b"enc:" + bytes(value)[::-1]

    def unprotect(self, value: bytes) -> bytes:
        raw = bytes(value)
        if not raw.startswith(b"enc:"):
            raise ValueError("invalid protected value")
        return raw[4:][::-1]


class PublicWebAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.security_path = Path(self.temp.name) / "public_web_security.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _module(self):
        spec = importlib.util.find_spec("web_auth")
        self.assertIsNotNone(spec, "web_auth must provide the public authorization boundary")
        return importlib.import_module("web_auth")

    def _store(self):
        module = self._module()
        return module.SecurityStore(self.security_path, ReversibleTestProtector())

    def test_owner_configuration_never_persists_plain_secrets(self):
        store = self._store()
        store.configure_owner(
            "Correct-Horse-2026",
            "sk-private-value",
            "deepseek-ai/DeepSeek-V3",
        )

        raw = self.security_path.read_bytes()
        self.assertNotIn(b"Correct-Horse-2026", raw)
        self.assertNotIn(b"sk-private-value", raw)
        self.assertEqual(
            store.model_credentials(),
            ("sk-private-value", "deepseek-ai/DeepSeek-V3"),
        )

    def test_session_survives_store_restart_until_explicit_revocation(self):
        module = self._module()
        first = module.SecurityStore(self.security_path, ReversibleTestProtector())
        first.configure_owner(
            "Correct-Horse-2026",
            "sk-private-value",
            "deepseek-ai/DeepSeek-V3",
        )
        token, issued = first.authenticate(
            "Correct-Horse-2026", "Chrome/Windows", "test-client"
        )

        reopened = module.SecurityStore(self.security_path, ReversibleTestProtector())
        resolved = reopened.resolve_session(token)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.session_id, issued.session_id)

        reopened.revoke(issued.session_id)
        self.assertIsNone(reopened.resolve_session(token))

    def test_password_change_revokes_every_existing_session(self):
        store = self._store()
        store.configure_owner(
            "Correct-Horse-2026",
            "sk-private-value",
            "deepseek-ai/DeepSeek-V3",
        )
        first_token, _ = store.authenticate(
            "Correct-Horse-2026", "Chrome/Windows", "test-client"
        )
        second_token, _ = store.authenticate(
            "Correct-Horse-2026", "Edge/Windows", "test-client-2"
        )

        store.configure_owner(
            "New-Password-2026",
            "sk-new-value",
            "deepseek-ai/DeepSeek-V3",
        )

        self.assertIsNone(store.resolve_session(first_token))
        self.assertIsNone(store.resolve_session(second_token))
        self.assertIsNotNone(
            store.authenticate(
                "New-Password-2026", "Chrome/Windows", "test-client"
            )[1]
        )

    def test_wrong_password_does_not_issue_a_session(self):
        module = self._module()
        store = self._store()
        store.configure_owner(
            "Correct-Horse-2026",
            "sk-private-value",
            "deepseek-ai/DeepSeek-V3",
        )

        with self.assertRaises(module.AuthenticationError):
            store.authenticate("wrong-password", "Chrome/Windows", "test-client")

        self.assertEqual(store.safe_status()["authorized_sessions"], [])

    def test_short_password_is_rejected_without_changing_configuration(self):
        store = self._store()

        with self.assertRaises(ValueError):
            store.configure_owner("short", "sk-private-value", "deepseek-ai/DeepSeek-V3")

        self.assertFalse(store.safe_status()["configured"])

    def test_safe_status_never_contains_password_key_or_session_token(self):
        store = self._store()
        store.configure_owner(
            "Correct-Horse-2026",
            "sk-private-value",
            "deepseek-ai/DeepSeek-V3",
        )
        token, _ = store.authenticate(
            "Correct-Horse-2026", "Chrome/Windows", "test-client"
        )

        encoded = json.dumps(store.safe_status(), ensure_ascii=False)
        self.assertNotIn("Correct-Horse-2026", encoded)
        self.assertNotIn("sk-private-value", encoded)
        self.assertNotIn(token, encoded)
        self.assertTrue(store.safe_status()["model_configured"])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is available only on Windows")
    def test_windows_dpapi_protector_round_trip(self):
        module = self._module()
        protector = module.WindowsDpapiProtector()
        protected = protector.protect(b"secret-model-key")

        self.assertNotEqual(protected, b"secret-model-key")
        self.assertEqual(protector.unprotect(protected), b"secret-model-key")


if __name__ == "__main__":
    unittest.main()
