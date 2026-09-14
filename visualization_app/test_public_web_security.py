from __future__ import annotations

import importlib
import importlib.util
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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


class PublicWebAccessTests(unittest.TestCase):
    def test_guest_can_read_public_status_but_cannot_call_real_control(self):
        from web_access import PermissionPolicy, RequestIdentity

        guest = RequestIdentity("guest", None, "guest-a")
        policy = PermissionPolicy()

        self.assertTrue(
            policy.authorize("GET", "/api/public/device-status", guest).allowed
        )
        denied = policy.authorize("POST", "/api/acquisition/start", guest)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.error, "real_access_required")

    def test_authorized_session_can_use_real_routes_but_not_admin_routes(self):
        from web_access import PermissionPolicy, RequestIdentity

        identity = RequestIdentity("authorized", "session-a", "guest-a")
        policy = PermissionPolicy()

        self.assertTrue(
            policy.authorize("POST", "/api/acquisition/start", identity).allowed
        )
        denied = policy.authorize("GET", "/api/admin/security", identity)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.error, "local_admin_required")

    def test_spoofed_forwarded_proto_from_non_loopback_is_not_trusted(self):
        from web_access import is_secure_request

        self.assertFalse(
            is_secure_request(
                "192.168.1.50",
                {"X-Forwarded-Proto": "https"},
                "public",
            )
        )
        self.assertTrue(
            is_secure_request(
                "127.0.0.1",
                {"X-Forwarded-Proto": "https"},
                "public",
            )
        )
        self.assertTrue(
            is_secure_request("127.0.0.1", {"Host": "127.0.0.1:8770"}, "public")
        )
        self.assertFalse(
            is_secure_request("192.168.1.50", {"Host": "127.0.0.1:8770"}, "public")
        )

    def test_quick_tunnel_host_requires_cloudflare_https_from_loopback(self):
        from web_access import is_trusted_quick_tunnel_request

        self.assertTrue(
            is_trusted_quick_tunnel_request(
                "127.0.0.1",
                "temporary-check.trycloudflare.com",
                {"X-Forwarded-Proto": "https", "CF-Connecting-IP": "203.0.113.10"},
            )
        )
        self.assertFalse(
            is_trusted_quick_tunnel_request(
                "192.168.1.50",
                "temporary-check.trycloudflare.com",
                {"X-Forwarded-Proto": "https", "CF-Connecting-IP": "203.0.113.10"},
            )
        )
        self.assertFalse(
            is_trusted_quick_tunnel_request(
                "127.0.0.1",
                "attacker.example.com",
                {"X-Forwarded-Proto": "https", "CF-Connecting-IP": "203.0.113.10"},
            )
        )


class PublicWebHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import create_server
        from guest_simulation import GuestSimulationManager
        from web_auth import SecurityStore

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        source = root / "simulation.csv"
        source.write_bytes("温度,压力\n350,400\n".encode("utf-8"))
        self.hardware_start_calls = 0

        class FakeAcquisition:
            def status(inner_self):
                return {"running": False, "sensors": []}

            def discover_interfaces(inner_self):
                return {"interfaces": [{"endpoint": "COM-secret"}]}

            def latest_check_result(inner_self):
                return {
                    "ok": False,
                    "interfaces": [
                        {
                            "role": "thermocouple",
                            "state": "unavailable",
                            "endpoint": "COM-secret",
                            "error": "secret driver path",
                        }
                    ],
                }

            def start(inner_self, _config):
                self.hardware_start_calls += 1
                raise AssertionError("guest request must not start hardware")

        dashboard = SimpleNamespace(
            acquisition=FakeAcquisition(),
            validate_prediction_setup=lambda config, load_model=False: {},
            bootstrap=lambda **kwargs: {
                "manifest": {
                    "result_dir": "C:\\private\\results",
                    "version": "test",
                },
                "specimens": [],
                "sensors": [],
                "indicators": [],
                "defaults": {},
                "acquisition": {
                    "drivers": [],
                    "new_collection_demo": {"source_file": "C:\\private\\demo.csv"},
                    "default_save_root": "C:\\private\\capture",
                },
            },
        )
        store = SecurityStore(root / "security.sqlite3", ReversibleTestProtector())
        store.configure_owner(
            "Correct-Horse-2026",
            "sk-private-value",
            "deepseek-ai/DeepSeek-V3",
        )
        guest_manager = GuestSimulationManager(
            root / "public_simulation",
            {"builtin": {"source_type": "single_csv", "path": str(source)}},
        )
        self.server = create_server(
            "127.0.0.1",
            0,
            {"urls": ["http://127.0.0.1:8770/"]},
            dashboard=dashboard,
            security_store=store,
            guest_manager=guest_manager,
            access_context="public",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookies: dict[str, str] = {}

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request_json(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
        csrf: bool = True,
    ):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        request_headers = {"Host": f"127.0.0.1:{self.server.server_port}"}
        if self.cookies:
            request_headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            if csrf and "afp_csrf" in self.cookies:
                request_headers["X-AFP-CSRF"] = self.cookies["afp_csrf"]
        request_headers.update(headers or {})
        raw_body = None if body is None else json.dumps(body).encode("utf-8")
        connection.request(method, path, raw_body, request_headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = response.getheaders()
        connection.close()
        for name, value in response_headers:
            if name.lower() != "set-cookie":
                continue
            first = value.split(";", 1)[0]
            if "=" in first:
                cookie_name, cookie_value = first.split("=", 1)
                self.cookies[cookie_name] = cookie_value
        return response.status, json.loads(raw.decode("utf-8")), response_headers

    def test_guest_real_start_is_denied_before_acquisition_manager(self):
        self.request_json("GET", "/api/auth/session")

        status, payload, _ = self.request_json(
            "POST", "/api/acquisition/start", {"acquisition_mode": "real"}
        )

        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "real_access_required")
        self.assertEqual(self.hardware_start_calls, 0)

    def test_loopback_login_and_forwarded_https_login_succeed(self):
        self.request_json("GET", "/api/auth/session")
        status, payload, _ = self.request_json(
            "POST", "/api/auth/login", {"password": "Correct-Horse-2026"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "authorized")

        status, payload, response_headers = self.request_json(
            "POST",
            "/api/auth/login",
            {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "authorized")
        self.assertTrue(payload["model_access"])
        self.assertNotIn("token", json.dumps(payload))
        session_cookies = [
            value
            for name, value in response_headers
            if name.lower() == "set-cookie" and value.startswith("afp_session=")
        ]
        self.assertTrue(session_cookies)
        self.assertIn("HttpOnly", session_cookies[0])
        self.assertIn("Secure", session_cookies[0])

    def test_csrf_mismatch_is_rejected(self):
        self.request_json("GET", "/api/auth/session")

        status, payload, _ = self.request_json(
            "POST",
            "/api/simulation/stop",
            {},
            headers={"X-AFP-CSRF": "wrong"},
            csrf=False,
        )

        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "csrf_failed")

    def test_public_device_status_masks_physical_details(self):
        status, payload, _ = self.request_json("GET", "/api/public/device-status")

        self.assertEqual(status, 200)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("COM-secret", encoded)
        self.assertNotIn("secret driver path", encoded)
        self.assertEqual(len(payload["interfaces"]), 5)

    def test_guest_simulation_uses_server_profile_and_never_starts_hardware(self):
        self.request_json("GET", "/api/auth/session")

        status, payload, _ = self.request_json(
            "POST",
            "/api/simulation/start",
            {
                "source_profile": "builtin",
                "acquisition_mode": "real",
                "driver": "m3232_pressure",
                "save_root": "C:\\Windows",
                "selected_sensors": ["温度", "压力"],
                "processing_mode": "capture_only",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["driver"], "simulator")
        self.assertTrue(payload["config"]["save_root"].startswith("public_simulation/"))
        self.assertEqual(self.hardware_start_calls, 0)

        stop_status, _, _ = self.request_json("POST", "/api/simulation/stop", {})
        self.assertEqual(stop_status, 200)

    def test_authorized_cookie_resolves_until_logout(self):
        self.request_json("GET", "/api/auth/session")
        status, _, _ = self.request_json(
            "POST",
            "/api/auth/login",
            {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 200)

        status, payload, _ = self.request_json(
            "GET",
            "/api/auth/session",
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "authorized")
        self.assertTrue(payload["model_access"])
        self.assertNotIn("sk-private-value", json.dumps(payload))

        status, payload, _ = self.request_json(
            "POST",
            "/api/auth/logout",
            {},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "guest")

    def test_unknown_api_route_is_not_allowed_by_prefix(self):
        status, payload, _ = self.request_json("GET", "/api/acquisition/unknown")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "route_not_found")

    def test_guest_bootstrap_masks_local_paths(self):
        status, payload, _ = self.request_json("GET", "/api/bootstrap")

        self.assertEqual(status, 200)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("C:\\private", encoded)
        self.assertEqual(payload["acquisition"]["default_save_root"], "")

    def test_authorized_session_must_acquire_real_control_lease(self):
        self.request_json("GET", "/api/auth/session")
        self.request_json(
            "POST",
            "/api/auth/login",
            {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )

        status, payload, _ = self.request_json(
            "POST", "/api/real/control/acquire", {}, headers={"X-Forwarded-Proto": "https"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["granted"])

        status, payload, _ = self.request_json(
            "GET", "/api/real/control/status", headers={"X-Forwarded-Proto": "https"}
        )
        self.assertEqual(status, 200)
        self.assertIsNotNone(payload["owner_id"])

    def test_duplicate_real_start_request_is_replayed_without_second_hardware_call(self):
        self.request_json("GET", "/api/auth/session")
        self.request_json(
            "POST",
            "/api/auth/login",
            {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.request_json(
            "POST", "/api/real/control/acquire", {}, headers={"X-Forwarded-Proto": "https"}
        )
        payload = {"acquisition_mode": "simulation", "driver": "simulator"}
        with patch.object(
            self.server.dashboard.acquisition,
            "start",
            return_value={"running": True},
        ) as start:
            headers = {
                "X-Forwarded-Proto": "https",
                "X-AFP-Request-ID": "start-001",
            }
            first_status, first, _ = self.request_json(
                "POST", "/api/acquisition/start", payload, headers=headers
            )
            second_status, second, _ = self.request_json(
                "POST", "/api/acquisition/start", payload, headers=headers
            )
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first, second)
        self.assertEqual(start.call_count, 1)


class RealControlLeaseTests(unittest.TestCase):
    def test_only_one_authorized_session_controls_real_acquisition(self):
        from control_lease import RealControlLease

        lease = RealControlLease(heartbeat_timeout_seconds=30)
        self.assertTrue(lease.acquire("session-a", "Chrome", now=0).granted)
        denied = lease.acquire("session-b", "Edge", now=10)
        self.assertFalse(denied.granted)
        self.assertEqual(denied.error, "real_control_busy")
        self.assertTrue(lease.acquire("session-b", "Edge", now=31).granted)

    def test_local_admin_can_take_over_without_expiring_login(self):
        from control_lease import RealControlLease

        lease = RealControlLease()
        lease.acquire("session-a", "Chrome", now=0)
        self.assertTrue(lease.force_takeover().granted)
        self.assertEqual(lease.status(now=1)["owner_id"], "local-admin")

    def test_lease_expiry_only_releases_control_owner(self):
        from control_lease import RealControlLease

        lease = RealControlLease(heartbeat_timeout_seconds=30)
        lease.acquire("session-a", "Chrome", now=0)
        self.assertIsNone(lease.status(now=31)["owner_id"])
        self.assertFalse(lease.heartbeat("session-a", now=31).granted)


class ModelCredentialTests(PublicWebHttpTests):
    def _event(self):
        return {
            "interface_id": "thermocouple",
            "interface_label": "SMRF八通道热电偶",
            "role": "thermocouple",
            "driver": "smrf_hid",
            "endpoint": "COM-secret",
            "protocol": "USB HID",
            "channels": ["温度1"],
            "state": "no_data",
            "message": "未收到数据",
        }

    def test_guest_cannot_smuggle_an_api_key_in_request(self):
        self.request_json("GET", "/api/auth/session")
        with patch(
            "interface_agent.run_interface_diagnoses",
            return_value={"diagnoses": [], "model_status": "offline"},
        ) as run:
            status, payload, _ = self.request_json(
                "POST",
                "/api/agent/diagnose",
                {
                    "api_key": "sk-attacker",
                    "model_name": "attacker/model",
                    "events": [self._event()],
                    "hardware_result": {},
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(run.call_args.kwargs["api_key"], "")
        self.assertNotEqual(
            run.call_args.kwargs["model_name"], "attacker/model"
        )
        self.assertFalse(payload["model_used"])

    def test_authorized_model_call_accepts_explicit_page_credentials_without_returning_key(self):
        self.request_json("GET", "/api/auth/session")
        self.request_json(
            "POST",
            "/api/auth/login",
            {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        with patch(
            "interface_agent.run_interface_diagnoses",
            return_value={"diagnoses": [], "model_status": "success"},
        ) as run:
            status, payload, headers = self.request_json(
                "POST",
                "/api/agent/diagnose",
                {
                    "api_key": "sk-page-value",
                    "model_name": "page/model",
                    "events": [self._event()],
                    "hardware_result": {},
                },
                headers={"X-Forwarded-Proto": "https"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(run.call_args.kwargs["api_key"], "sk-page-value")
        self.assertEqual(run.call_args.kwargs["model_name"], "page/model")
        self.assertTrue(payload["model_used"])
        self.assertNotIn("sk-private-value", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("sk-private-value", json.dumps(headers))


class FrontendAccessContractTests(unittest.TestCase):
    def test_frontend_contains_access_state_controls_and_model_fields(self):
        root = Path(__file__).resolve().parent / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="access-mode-badge"', html)
        self.assertIn('id="unlock-real-mode"', html)
        self.assertIn('id="real-access-password"', html)
        self.assertIn('id="agentApiKeyInput"', html)
        self.assertIn('id="agentModelNameInput"', html)
        self.assertIn("agentApiKeyInput?.value.trim()", script)
        self.assertIn("/api/auth/session", script)


class BuildManifestTests(unittest.TestCase):
    def test_build_script_copies_every_public_web_module(self):
        script = (Path(__file__).resolve().parent.parent / "modular_runtime" / "build_modular_app.ps1").read_text(encoding="utf-8-sig")
        for name in ("web_auth.py", "web_access.py", "public_status.py", "guest_simulation.py", "control_lease.py"):
            self.assertIn(f'"{name}"', script)

    def test_build_script_contains_delivery_secret_scan(self):
        script = (Path(__file__).resolve().parent.parent / "modular_runtime" / "build_modular_app.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("public_web_security.sqlite3", script)
        self.assertIn("sk-[A-Za-z0-9_-]{20,}", script)


if __name__ == "__main__":
    unittest.main()
