from __future__ import annotations

import importlib
import importlib.util
import http.client
import json
import os
import tempfile
import threading
import time
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
    def test_lan_operator_has_unique_session_and_helper_backed_real_access(self):
        from web_access import (
            PermissionPolicy,
            RequestIdentity,
            lan_session_id,
            uses_local_capture_helper,
        )

        first = RequestIdentity("lan_operator", lan_session_id("guest-a"), "guest-a")
        second = RequestIdentity("lan_operator", lan_session_id("guest-b"), "guest-b")
        policy = PermissionPolicy()

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertTrue(str(first.session_id).startswith("lan-"))
        self.assertTrue(uses_local_capture_helper(first.role))
        self.assertTrue(
            policy.authorize("POST", "/api/helper/pair/start", first).allowed
        )
        self.assertTrue(
            policy.authorize("POST", "/api/acquisition/start", first).allowed
        )
        denied = policy.authorize("GET", "/api/admin/status", first)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.error, "local_admin_required")

    def test_only_remote_real_roles_use_local_capture_helper(self):
        from web_access import uses_local_capture_helper

        self.assertTrue(uses_local_capture_helper("lan_operator"))
        self.assertTrue(uses_local_capture_helper("authorized"))
        self.assertFalse(uses_local_capture_helper("local_admin"))
        self.assertFalse(uses_local_capture_helper("guest"))

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

    def test_synthetic_package_catalog_download_and_selection_require_real_role(self):
        from web_access import PermissionPolicy, RequestIdentity

        policy = PermissionPolicy()
        guest = RequestIdentity("guest", None, "guest-a")
        authorized = RequestIdentity("authorized", "session-a", "guest-a")
        for method, path in (
            ("GET", "/api/simulation/packages"),
            ("GET", "/api/simulation/package-download"),
            ("POST", "/api/acquisition/select-package"),
        ):
            self.assertFalse(policy.authorize(method, path, guest).allowed)
            self.assertTrue(policy.authorize(method, path, authorized).allowed)

    def test_mysql_preflight_requires_authorized_session(self):
        from web_access import PermissionPolicy, RequestIdentity

        policy = PermissionPolicy()
        guest = RequestIdentity("guest", None, "guest-a")
        authorized = RequestIdentity("authorized", "session-a", "guest-a")

        denied = policy.authorize("POST", "/api/mysql/preflight", guest)
        self.assertFalse(denied.allowed)
        self.assertTrue(
            policy.authorize("POST", "/api/mysql/preflight", authorized).allowed
        )

    def test_process_parameter_routes_separate_guest_simulation_from_real_access(self):
        from web_access import PermissionPolicy, RequestIdentity

        policy = PermissionPolicy()
        guest = RequestIdentity("guest", None, "guest-a")
        authorized = RequestIdentity("authorized", "session-a", "guest-a")

        self.assertTrue(
            policy.authorize(
                "POST", "/api/simulation/process-parameters", guest
            ).allowed
        )
        denied = policy.authorize(
            "POST", "/api/acquisition/process-parameters", guest
        )
        self.assertFalse(denied.allowed)
        self.assertTrue(
            policy.authorize(
                "POST", "/api/acquisition/process-parameters", authorized
            ).allowed
        )

    def test_helper_transport_routes_are_public_but_token_authenticated(self):
        from web_access import PermissionPolicy, RequestIdentity

        policy = PermissionPolicy()
        guest = RequestIdentity("guest", None, "guest-a")
        self.assertTrue(
            policy.authorize("POST", "/api/helper/poll", guest).allowed
        )
        self.assertTrue(
            policy.authorize("POST", "/api/helper/pair/complete", guest).allowed
        )
        self.assertTrue(
            policy.authorize("POST", "/api/helper/samples", guest).allowed
        )
        self.assertTrue(
            policy.authorize(
                "POST", "/api/helper/simulation-source/chunk", guest
            ).allowed
        )

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

    def test_direct_private_lan_client_is_distinguished_from_cloudflare_proxy(self):
        from web_access import is_lan_client

        self.assertTrue(is_lan_client("192.168.101.44", {"Host": "192.168.101.31:8770"}))
        self.assertTrue(is_lan_client("10.0.0.44", {"Host": "10.0.0.10:8770"}))
        self.assertFalse(
            is_lan_client(
                "127.0.0.1",
                {
                    "Host": "temporary-check.trycloudflare.com",
                    "CF-Connecting-IP": "203.0.113.10",
                    "X-Forwarded-Proto": "https",
                },
            )
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

    def test_tailscale_funnel_host_requires_https_from_loopback(self):
        from web_access import is_trusted_tailscale_funnel_request

        headers = {"X-Forwarded-Proto": "https"}
        self.assertTrue(
            is_trusted_tailscale_funnel_request(
                "127.0.0.1", "afp-server.example.ts.net", headers
            )
        )
        self.assertFalse(
            is_trusted_tailscale_funnel_request(
                "192.168.1.50", "afp-server.example.ts.net", headers
            )
        )
        self.assertFalse(
            is_trusted_tailscale_funnel_request(
                "127.0.0.1",
                "afp-server.example.ts.net",
                {"X-Forwarded-Proto": "http"},
            )
        )
        self.assertFalse(
            is_trusted_tailscale_funnel_request(
                "127.0.0.1", "attacker.example.com", headers
            )
        )


class PublicWebHttpTests(unittest.TestCase):
    def test_esr_numeric_route_dispatches_locally_and_denies_guest(self):
        self.request_json("GET", "/api/auth/session")
        request = {"action": "zero", "interface_id": "esr", "capture_generation": "current",
                   "connection_generation": 1, "calibration_digest": ""}
        with patch.object(self.server.dashboard.acquisition, "esr_numeric_action", create=True, return_value={"ok": True, "zero": {"relative_unit": "adc_counts"}}) as action:
            status, result, _ = self.request_json("POST", "/api/acquisition/esr-numeric", request)
            self.assertEqual(status, 403, result)
            action.assert_not_called()
            with patch.object(self.server.RequestHandlerClass, "access_context", "local_admin"):
                status, result, _ = self.request_json("POST", "/api/acquisition/esr-numeric", request)
            self.assertEqual(status, 200, result)
            self.assertTrue(result["ok"])
            action.assert_called_once_with(request)

    def test_local_interface_plans_persist_without_credentials_or_readiness(self):
        route = "/api/admin/acquisition/interface-plans"
        status, _, _ = self.request_json("GET", route)
        self.assertEqual(status, 403)
        plan = {
            "active": "new_collection_v13_0",
            "plans": {"new_collection_v13_0": {
                "interfaces": [{"id": "esr", "role": "esr1001_strain",
                    "driver": "esr1001_usb_ttl", "data_mode": "raw_adc",
                    "endpoint": "COM91", "enabled": True, "baudrate": 115200,
                    "channels": ["CH1_ADC_signed"],
                    "password": "private-value", "physical_verified": True,
                    "calibration_profile": {"secret": "private-value", "K": [1, 2, 3, 4]}}],
                "processingMode": "capture_only", "mysqlEnabled": False,
                "readiness_snapshot": {"ok": True},
            }},
            "token": "private-value",
        }
        status, _, _ = self.request_json("POST", route, plan)
        self.assertEqual(status, 403)
        with patch.object(self.server.RequestHandlerClass, "access_context", "local_admin"):
            status, result, _ = self.request_json("POST", route, plan)
            self.assertEqual(status, 200, result)
            status, result, _ = self.request_json("GET", route)
            self.assertEqual(status, 200, result)
            revision = result["revision"]
            candidate_plan = json.loads(json.dumps(plan))
            candidate_plan["expected_revision"] = revision
            candidate_plan["plans"]["new_collection_v13_0"]["interfaces"][0]["channel_settings"] = {
                "esr_vendor_candidate": {"source_sha256": "fixture", "channels": [1, 2, 3, 4]}}
            status, saved, _ = self.request_json("POST", route, candidate_plan)
            self.assertEqual(status, 200, saved)
            self.assertNotEqual(saved["revision"], revision)
            status, rejected, _ = self.request_json("POST", route, plan)
            self.assertEqual(status, 409, rejected)
            self.assertIn("interface_plan_conflict", rejected["error"])
            candidate_plan["expected_revision"] = revision
            status, rejected, _ = self.request_json("POST", route, candidate_plan)
            self.assertEqual(status, 409, rejected)
            status, result, _ = self.request_json("GET", route)
            self.assertEqual(status, 200, result)
        from pre_sensor_config import InterfacePlanStore
        stored = InterfacePlanStore(self.server.security_store.path.parent / "interface-plans.json").load()
        self.assertEqual(result["preferences"], stored)
        interface = stored["plans"][stored["active"]]["interfaces"][0]
        self.assertEqual(interface["driver"], "esr1001_usb_ttl")
        self.assertEqual(interface["channels"], ["CH1_ADC_signed"])
        self.assertNotIn("physical_verified", interface)
        rendered = json.dumps(stored)
        self.assertNotIn("private-value", rendered)
        self.assertNotIn("readiness_snapshot", rendered)

    def setUp(self) -> None:
        from app import RELEASE_CONTRACT, create_server
        from guest_simulation import GuestSimulationManager
        from public_ingress_state import release_candidate_id
        from web_auth import SecurityStore

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        source = root / "simulation.csv"
        source.write_bytes(
            "转速,位移,温度1,温度2,温度3,温度4,温度5,温度6,温度7,温度8,压力,振动\n"
            "100,1,350,350,350,350,350,350,350,350,400,0\n".encode("utf-8")
        )
        self.default_simulation_source = source.resolve()
        self.hardware_start_calls = 0
        self.hardware_discovery_calls = 0
        self.acquisition_configs = []

        class FakeAcquisition:
            def status(inner_self):
                return {"running": False, "sensors": []}

            def discover_interfaces(inner_self):
                self.hardware_discovery_calls += 1
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

            def test_connection(inner_self, config):
                self.acquisition_configs.append(("test", config))
                return {"ok": True, "interfaces": [], "channels": []}

            def read_process_parameters(inner_self, config):
                self.acquisition_configs.append(("parameters", config))
                return {"ok": True, "parameters": {}}

            def start(inner_self, _config):
                self.hardware_start_calls += 1
                raise AssertionError("guest request must not start hardware")

        dashboard = SimpleNamespace(
            acquisition=FakeAcquisition(),
            live=lambda **kwargs: {"acquisition": kwargs["acquisition"].status()},
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
                    "new_collection_demo": {"source_file": str(self.default_simulation_source)},
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
        ingress_state = root / "public-ingress-state.json"
        ingress_state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": 1,
                    "public_url": "https://afp.example.test",
                    "provider": "tailscale",
                    "recovery_owner": "tailscale",
                    "candidate_id": release_candidate_id(RELEASE_CONTRACT),
                    "status": "online",
                    "published_at": "2026-10-15T00:00:00+00:00",
                    "layers": {"edge": {"ok": True}},
                }
            ),
            encoding="utf-8",
        )
        self.server = create_server(
            "127.0.0.1",
            0,
            {"urls": ["http://127.0.0.1:8770/"]},
            dashboard=dashboard,
            security_store=store,
            guest_manager=guest_manager,
            access_context="public",
            ingress_state_file=ingress_state,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookies: dict[str, str] = {}

    def test_cutover_readiness_is_loopback_only_and_contains_no_secrets(self):
        status, payload, _ = self.request_json("GET", "/api/ingress/cutover-readiness")
        self.assertEqual(status, 403, payload)
        handler = self.server.RequestHandlerClass
        handler.access_context = "local_admin"
        try:
            status, payload, _ = self.request_json("GET", "/api/ingress/cutover-readiness")
        finally:
            handler.access_context = "public"
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["allowed"])
        self.assertEqual(payload["generation"], 1)
        encoded = json.dumps(payload)
        for forbidden in ("pairing_token", "challenge", "payload", "password", "cookie"):
            self.assertNotIn(forbidden, encoded.lower())

        fake = object.__new__(handler)
        fake.client_address = ("192.168.1.20", 12345)
        self.assertFalse(handler._is_loopback_client(fake))

    def test_cutover_readiness_fails_closed_when_local_status_is_unavailable(self):
        handler = self.server.RequestHandlerClass
        handler.access_context = "local_admin"
        try:
            with patch.object(
                self.server.dashboard.acquisition,
                "status",
                side_effect=RuntimeError("unavailable"),
            ):
                status, payload, _ = self.request_json(
                    "GET", "/api/ingress/cutover-readiness"
                )
        finally:
            handler.access_context = "public"
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["allowed"])
        self.assertIn(
            "local_acquisition_status_unavailable",
            payload["blocking_request_ids"],
        )

    def test_cutover_reservation_blocks_capture_and_requires_admin_listener(self):
        status, payload, _ = self.request_json(
            "POST", "/api/ingress/cutover-reservation", {}
        )
        self.assertEqual(status, 403, payload)
        handler = self.server.RequestHandlerClass
        handler.access_context = "local_admin"
        try:
            status, reservation, _ = self.request_json(
                "POST", "/api/ingress/cutover-reservation", {}
            )
            self.assertEqual(status, 200, reservation)
            self.assertTrue(reservation["reservation_id"])
            blocked = self.server.dashboard.helper_registry.command(
                "missing", "start_capture", {}
            )
            self.assertEqual(blocked["error"], "ingress_cutover_in_progress")
            status, released, _ = self.request_json(
                "POST",
                "/api/ingress/cutover-release",
                {"reservation_id": reservation["reservation_id"]},
            )
            self.assertEqual(status, 200, released)
            self.assertTrue(released["released"])
        finally:
            handler.access_context = "public"

    def test_cutover_reservation_does_not_block_local_admin_simulation(self):
        handler = self.server.RequestHandlerClass
        handler.access_context = "local_admin"
        reservation_id = ""
        try:
            status, reservation, _ = self.request_json(
                "POST", "/api/ingress/cutover-reservation", {}
            )
            self.assertEqual(status, 200, reservation)
            reservation_id = reservation["reservation_id"]
            with patch.object(
                self.server.dashboard.acquisition,
                "start",
                return_value={"running": True},
            ) as start:
                status, payload, _ = self.request_json(
                    "POST",
                    "/api/acquisition/start",
                    {
                        "acquisition_mode": "simulation",
                        "driver": "simulator",
                        "processing_mode": "capture_only",
                        "simulation_source_type": "single_csv",
                        "simulation_source_path": str(self.default_simulation_source),
                        "source_file": str(self.default_simulation_source),
                    },
                )

            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["running"])
            start.assert_called_once()
        finally:
            if reservation_id:
                self.request_json(
                    "POST",
                    "/api/ingress/cutover-release",
                    {"reservation_id": reservation_id},
                )
            handler.access_context = "public"

    def test_create_server_replaces_unconfigured_registry_and_reuses_configured_one(self):
        from app import create_server
        from helper_relay import HelperRegistry

        dashboard = self.server.dashboard
        ingress_state_file = dashboard.helper_registry.ingress_state_path
        dashboard.helper_registry = HelperRegistry()
        handler = self.server.RequestHandlerClass
        replacement = create_server(
            "127.0.0.1",
            0,
            dashboard=dashboard,
            security_store=handler.security_store,
            guest_manager=handler.guest_manager,
            control_lease=handler.control_lease,
            access_context="local_admin",
            ingress_state_file=ingress_state_file,
        )
        try:
            configured = dashboard.helper_registry
            self.assertIsNotNone(configured.persistence_path)
            self.assertEqual(configured.ingress_state_path, ingress_state_file)
            second = create_server(
                "127.0.0.1",
                0,
                dashboard=dashboard,
                security_store=handler.security_store,
                guest_manager=handler.guest_manager,
                control_lease=handler.control_lease,
                access_context="public",
                ingress_state_file=ingress_state_file,
            )
            try:
                self.assertIs(second.dashboard.helper_registry, configured)
            finally:
                second.server_close()
        finally:
            replacement.server_close()

    def test_stale_generation_sample_submission_is_rejected(self):
        self.request_json("GET", "/api/auth/session")
        self.request_json(
            "POST", "/api/auth/login", {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        _, challenge, _ = self.request_json("POST", "/api/helper/pair/start", {})
        _, paired, _ = self.request_json(
            "POST", "/api/helper/pair/complete",
            {"challenge": challenge["challenge"], "device_id": "visitor-stale"},
        )
        status, result, _ = self.request_json(
            "POST", "/api/helper/samples",
            {
                "device_id": "visitor-stale",
                "batch": {
                    "ingress_generation": 0,
                    "capture_uuid": "stale-capture",
                    "sequence": 0,
                    "rows": [],
                    "timestamps": [],
                },
            },
            headers={"Authorization": f"Bearer {paired['pairing_token']}"},
            csrf=False,
        )
        self.assertEqual(status, 401)
        self.assertEqual(result["error"], "ingress_generation_mismatch")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_authorized_target_preflight_uses_matching_server_secret_but_does_not_return_it(self):
        self.request_json("GET", "/api/auth/session")
        login, _, _ = self.request_json(
            "POST", "/api/auth/login", {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(login, 200)
        self.server.RequestHandlerClass.target_profiles._profile_loader = lambda: {
            "target": {"host": "db.test", "port": 3306, "user": "afp_app",
                       "database": "afp", "password": "only-on-server"}
        }
        seen = []

        def verify(store):
            seen.append(store.settings.password)
            return {"ok": store.settings.password == "only-on-server", "database": "afp"}

        with patch("app.mysql_test_existing_database", side_effect=verify):
            status, result, _ = self.request_json("POST", "/api/mysql/test", {
                "mysql_enabled": True, "mysql_host": "db.test", "mysql_port": 3306,
                "mysql_user": "afp_app", "mysql_database": "afp",
                "mysql_password": "", "read_only": True,
            })
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(seen, ["only-on-server"])
        self.assertTrue(result["config_id"])
        self.assertNotIn("only-on-server", json.dumps(result))

    def test_authorized_acquisition_routes_accept_target_profile_identifier(self):
        self.request_json("GET", "/api/auth/session")
        login, _, _ = self.request_json(
            "POST", "/api/auth/login", {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(login, 200)
        acquired, _, _ = self.request_json(
            "POST", "/api/real/control/acquire", {},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(acquired, 200)
        request = {
            "acquisition_mode": "real",
            "real_acquisition_mode": "local_direct",
            "mysql_target_config_id": "opaque-id",
            "interfaces": [{
                "id": "plc_process",
                "enabled": True,
                "role": "plc",
                "driver": "modbus_tcp",
                "endpoint": "192.0.2.10:502",
                "physical_interface_id": "ethernet-test",
                "physical_interface_kind": "ethernet",
            }],
        }

        test_status, test_result, _ = self.request_json(
            "POST", "/api/acquisition/test", request
        )
        parameter_status, parameter_result, _ = self.request_json(
            "POST", "/api/acquisition/process-parameters", request
        )

        self.assertEqual(test_status, 200, test_result)
        self.assertTrue(test_result["ok"])
        self.assertEqual(parameter_status, 200, parameter_result)
        self.assertTrue(parameter_result["ok"])
        self.assertEqual(
            [config.mysql_target_config_id for _, config in self.acquisition_configs],
            ["opaque-id", "opaque-id"],
        )

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
        try:
            connection.request(method, path, raw_body, request_headers)
            response = connection.getresponse()
            raw = response.read()
            response_headers = response.getheaders()
            response_status = response.status
        finally:
            connection.close()
        for name, value in response_headers:
            if name.lower() != "set-cookie":
                continue
            first = value.split(";", 1)[0]
            if "=" in first:
                cookie_name, cookie_value = first.split("=", 1)
                self.cookies[cookie_name] = cookie_value
        return response_status, json.loads(raw.decode("utf-8")), response_headers

    def test_guest_real_start_is_denied_before_acquisition_manager(self):
        self.request_json("GET", "/api/auth/session")

        status, payload, _ = self.request_json(
            "POST", "/api/acquisition/start", {"acquisition_mode": "real"}
        )

        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "real_access_required")
        self.assertEqual(self.hardware_start_calls, 0)

    def test_terminal_helper_result_rejection_is_an_http_ack(self):
        with patch.object(
            self.server.dashboard.helper_registry,
            "accept_result",
            return_value={
                "ok": False,
                "error": "command_state_lost",
                "terminal": True,
                "request_id": "stale-result",
            },
        ):
            status, payload, _ = self.request_json(
                "POST",
                "/api/helper/result",
                {
                    "device_id": "helper-a",
                    "request_id": "stale-result",
                    "payload": {"ok": True},
                },
                headers={"Authorization": "Bearer expired-token"},
                csrf=False,
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["terminal"])
        self.assertEqual(payload["error"], "command_state_lost")

    def test_paired_helper_samples_feed_authorized_session_status(self):
        self.request_json("GET", "/api/auth/session")
        login_status, _, _ = self.request_json(
            "POST",
            "/api/auth/login",
            {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(login_status, 200)
        pair_status, challenge, _ = self.request_json(
            "POST", "/api/helper/pair/start", {}
        )
        self.assertEqual(pair_status, 200)
        complete_status, paired, _ = self.request_json(
            "POST",
            "/api/helper/pair/complete",
            {
                "challenge": challenge["challenge"],
                "device_id": "visitor-pc",
                "capabilities": {"real_capture": True},
            },
        )
        self.assertEqual(complete_status, 200)

        sample_status, accepted, _ = self.request_json(
            "POST",
            "/api/helper/samples",
            {
                "device_id": "visitor-pc",
                "batch": {
                    "ingress_generation": paired["ingress_generation"],
                    "capture_uuid": "capture-a",
                    "sequence": 0,
                    "rows": [{"温度": 350.0}],
                    "timestamps": [1.0],
                    "status": {
                        "running": True,
                        "config": {
                            "acquisition_mode": "real",
                            "dataset_schema": "legacy_original",
                            "selected_sensors": ["温度"],
                        },
                        "sensors": [],
                    },
                },
            },
            headers={"Authorization": f"Bearer {paired['pairing_token']}"},
            csrf=False,
        )
        self.assertEqual(sample_status, 200)
        self.assertTrue(accepted["ok"])

        status_code, remote_status, _ = self.request_json(
            "GET", "/api/acquisition/status"
        )
        self.assertEqual(status_code, 200)
        self.assertTrue(remote_status["remote_source"])
        self.assertTrue(remote_status["first_sample_received"])
        self.assertEqual(remote_status["capture_uuid"], "capture-a")

    def test_real_helper_target_mysql_is_server_only_and_finalizes_after_last_batch(self):
        from release_contract import load_release_contract

        contract = load_release_contract(start=__file__)
        self.request_json("GET", "/api/auth/session")
        login, _, _ = self.request_json(
            "POST", "/api/auth/login", {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(login, 200)
        _, challenge, _ = self.request_json("POST", "/api/helper/pair/start", {})
        _, paired, _ = self.request_json("POST", "/api/helper/pair/complete", {
            "challenge": challenge["challenge"], "device_id": "visitor-pc",
            "capabilities": {
                "real_capture": True,
                "unified_frame_v1": True,
                "unified_frame_v2": True,
                "protocol_version": contract.helper_protocol_version,
                "helper_version": contract.helper_version,
                "build_id": contract.release_revision,
                "unified_frame_contract": contract.unified_frame_contract,
                "configuration_schema_version": contract.configuration_schema_version,
                "readiness_schema_version": contract.readiness_schema_version,
                "device_readiness_contract": "plc_abb_uvc_readiness_v1",
                "unified_device_readiness_v1": True,
            },
        })
        handler = self.server.RequestHandlerClass
        handler.target_profiles._profile_loader = lambda: {"target": {
            "host": "db.test", "port": 3306, "user": "afp_app",
            "database": "afp", "password": "server-only-secret",
        }}
        saved = []
        preflights = []

        class Store:
            def __init__(self, settings):
                self.settings = settings

            def preflight(self, *, write_test=False):
                preflights.append((self.settings.host, write_test))
                return {"ok": True, "write_test": write_test}

            def save_layer(self, _config, **kwargs):
                saved.append((self.settings.password, list(kwargs["rows"])))
                return {"ok": True, "saved_rows": len(saved[-1][1]), "database": "afp"}

        handler.target_saver.store_factory = Store
        start_payload = {
            "processing_mode": "capture_only", "acquisition_mode": "real",
            "dataset_schema": "new_collection_v11_3", "driver": "m3232_pressure",
            "endpoint": "COM4", "selected_sensors": ["薄膜压力"],
            "interfaces": [{"id": "m3232_pressure", "role": "pressure", "enabled": True,
                            "driver": "m3232_pressure", "endpoint": "COM4", "channel_map": {}}],
            "interface_channel_assignments": {"m3232_pressure": ["薄膜压力"]},
            "specimen_id": "specimen-a", "mysql_enabled": True,
            "mysql_host": "db.test", "mysql_port": 3306, "mysql_user": "afp_app",
            "mysql_database": "afp", "mysql_password": "",
        }
        _, command, _ = self.request_json("POST", "/api/helper/command", {
            "command": "start_capture", "payload": start_payload,
        })
        self.assertTrue(command["ok"])
        self.assertEqual(preflights, [("db.test", True)])
        _, polled, _ = self.request_json("POST", "/api/helper/poll", {"device_id": "visitor-pc"},
                                         headers={"Authorization": f"Bearer {paired['pairing_token']}"}, csrf=False)
        self.assertNotIn("server-only-secret", json.dumps(polled))
        self.assertFalse(polled["command"]["payload"]["mysql_enabled"])
        request_id = polled["command"]["request_id"]
        self.request_json("POST", "/api/helper/result", {
            "device_id": "visitor-pc", "request_id": request_id,
            "ingress_generation": paired["ingress_generation"],
            "payload": {"running": True, "capture_uuid": "capture-target-a"},
        }, headers={"Authorization": f"Bearer {paired['pairing_token']}"}, csrf=False)
        _, accepted, _ = self.request_json("POST", "/api/helper/samples", {
            "device_id": "visitor-pc", "batch": {
                "ingress_generation": paired["ingress_generation"],
                "capture_uuid": "capture-target-a", "sequence": 0,
                "rows": [{"薄膜压力": 10.0}], "timestamps": [10.0],
                "frames": [{
                    "contract_version": "unified_frame_v1",
                    "capture_uuid": "capture-target-a",
                    "frame_sequence": 0,
                    "frame_time": 10.0,
                    "target_monotonic": 10.0,
                    "assembled_monotonic": 10.0,
                    "deadline_missed": False,
                    "channels": {"薄膜压力": {
                        "value": 10.0, "unit": "N", "quality": "measured_new",
                        "source_timestamp": 10.0, "received_timestamp": 10.0,
                        "age_seconds": 0.0, "is_new": True,
                        "interface_id": "m3232_pressure", "source_sequence": 0,
                        "protocol_ok": True, "error": "",
                    }},
                }],
                "status": {"running": False, "finalization_complete": True, "sample_count": 1},
            },
        }, headers={"Authorization": f"Bearer {paired['pairing_token']}"}, csrf=False)
        self.assertTrue(accepted["ok"])
        for _ in range(20):
            if saved:
                break
            time.sleep(0.05)
        status_code, status, _ = self.request_json("GET", "/api/acquisition/status")
        self.assertEqual(status_code, 200)
        self.assertEqual(saved, [("server-only-secret", [{"薄膜压力": 10.0, "timestamp_unix": 10.0}])], status)
        self.assertEqual(status["server_target"]["state"], "saved")
        self.assertNotIn("server-only-secret", json.dumps(status))

    def test_authorized_session_can_retry_only_its_failed_server_target_capture(self):
        self.request_json("GET", "/api/auth/session")
        login, _, _ = self.request_json(
            "POST", "/api/auth/login", {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(login, 200)
        handler = self.server.RequestHandlerClass
        handler.target_profiles._profile_loader = lambda: {"target": {
            "host": "db.test", "port": 3306, "user": "afp_app",
            "database": "afp", "password": "server-only-secret",
        }}
        session = self.server.security_store.resolve_session(self.cookies["afp_session"])
        self.assertIsNotNone(session)
        selection = handler.target_profiles.resolve(session.session_id, {
            "mysql_enabled": True, "mysql_host": "db.test", "mysql_port": 3306,
            "mysql_user": "afp_app", "mysql_database": "afp", "mysql_password": "",
        })
        handler.target_capture_journal.arm(
            session.session_id, selection.config_id,
            config={"dataset_schema": "new_collection_v11_3", "driver": "simulator",
                    "processing_mode": "capture_only", "selected_sensors": ["温度"],
                    "specimen_id": "retry-specimen"},
            target=selection.public(),
        )
        capture_uuid = "retry-capture"
        accepted = handler.target_capture_journal.ingest(session.session_id, {
            "capture_uuid": capture_uuid, "sequence": 0,
            "rows": [{"温度": 10.0}], "timestamps": [10.0],
            "status": {"running": False, "finalization_complete": True, "sample_count": 1},
        }, handler.dashboard.remote_acquisitions)
        self.assertTrue(accepted["ok"], accepted)
        calls = []

        class Store:
            def __init__(self, settings):
                self.settings = settings

            def save_layer(self, _config, **_kwargs):
                calls.append(self.settings.password)
                return {"ok": len(calls) > 1, "saved_rows": 1, "error": "temporary"}

        handler.target_saver.store_factory = Store
        self.assertFalse(handler.target_saver.save_now(session.session_id, capture_uuid)["ok"])
        original_session_cookie = self.cookies["afp_session"]
        other_token, _other_session = self.server.security_store.authenticate(
            "Correct-Horse-2026", "test-agent", "other-client"
        )
        self.cookies["afp_session"] = other_token
        foreign_status, foreign_payload, _ = self.request_json(
            "POST", "/api/mysql/target/retry", {"capture_uuid": capture_uuid}
        )
        self.assertEqual(foreign_status, 404, foreign_payload)
        self.cookies["afp_session"] = original_session_cookie
        status, payload, _ = self.request_json(
            "POST", "/api/mysql/target/retry", {"capture_uuid": capture_uuid}
        )
        self.assertEqual(status, 200, payload)
        self.assertNotIn("server-only-secret", json.dumps(payload))
        for _ in range(20):
            if handler.target_capture_journal.capture_status(session.session_id, capture_uuid)["state"] == "saved":
                break
            time.sleep(0.05)
        self.assertEqual(handler.target_capture_journal.capture_status(session.session_id, capture_uuid)["state"], "saved")
        self.assertEqual(calls, ["server-only-secret", "server-only-secret"])

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

        # Windows can very occasionally abort a short-lived loopback socket
        # while this large HTTP suite is cycling servers.  This rejected CSRF
        # request cannot execute the operation, so one transport-only retry is
        # safe; a second abort still fails the contract test.
        for attempt in range(2):
            try:
                status, payload, _ = self.request_json(
                    "POST",
                    "/api/simulation/stop",
                    {},
                    headers={"X-AFP-CSRF": "wrong"},
                    csrf=False,
                )
                break
            except (ConnectionAbortedError, ConnectionResetError):
                if attempt == 1:
                    raise
                time.sleep(0.05)

        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "csrf_failed")

    def test_public_device_status_masks_physical_details(self):
        status, payload, _ = self.request_json("GET", "/api/public/device-status")

        self.assertEqual(status, 200)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("COM-secret", encoded)
        self.assertNotIn("secret driver path", encoded)
        self.assertEqual(len(payload["interfaces"]), 6)
        self.assertIn("esr1001_strain", {item["role"] for item in payload["interfaces"]})

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

    def test_guest_simulation_start_does_not_require_public_ingress_state(self):
        self.request_json("GET", "/api/auth/session")
        self.server.dashboard.helper_registry.ingress_state_path.unlink()

        status, payload, _ = self.request_json(
            "POST",
            "/api/simulation/start",
            {"source_profile": "builtin"},
        )

        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["running"])
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

    def test_authorized_remote_bootstrap_keeps_sensor_catalog_without_server_discovery(self):
        self.request_json("GET", "/api/auth/session")
        login_status, _, _ = self.request_json(
            "POST", "/api/auth/login", {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(login_status, 200)

        status, payload, _ = self.request_json(
            "GET", "/api/bootstrap", headers={"X-Forwarded-Proto": "https"}
        )

        self.assertEqual(status, 200)
        acquisition = payload["acquisition"]
        self.assertEqual(
            {item["role"]: item["driver"] for item in acquisition.get("interface_defaults", [])},
            {
                "thermocouple64": "huipu_tc64_modbus_rtu",
                "plc": "modbus_tcp",
                "thermal_uvc": "uvc_thermal",
                "robot": "abb_robot",
                "pressure": "m3232_pressure",
                "esr1001_strain": "modbus_rtu_esr1001",
            },
        )
        self.assertTrue(
            {"thermocouple64", "plc", "thermal_uvc", "robot", "pressure", "esr1001_strain"}.issubset(
                {item["id"] for item in acquisition.get("sensor_types", [])}
            )
        )
        self.assertEqual(acquisition["interface_discovery"], {"physical_interfaces": []})
        self.assertEqual(self.hardware_discovery_calls, 0)

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

    def test_authorized_simulation_start_does_not_require_real_control_lease(self):
        self.request_json("GET", "/api/auth/session")
        login_status, _, _ = self.request_json(
            "POST",
            "/api/auth/login",
            {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(login_status, 200)
        self.assertIsNone(self.server.control_lease.status()["owner_id"])

        guest_acquisition = self.server.guest_manager.ensure_session(
            self.cookies["afp_guest"]
        ).acquisition
        with patch.object(
            guest_acquisition,
            "start",
            return_value={"running": True},
        ) as start:
            status, result, _ = self.request_json(
                "POST",
                "/api/acquisition/start",
                {
                    "acquisition_mode": "simulation", "driver": "simulator",
                    "execution_host": "server",
                    "simulation_execution_choice": "server_explicit",
                },
                headers={"X-Forwarded-Proto": "https"},
            )

        self.assertEqual(status, 200, result)
        self.assertTrue(result["running"])
        self.assertEqual(start.call_count, 1)
        self.assertEqual(self.hardware_start_calls, 0)

    def test_local_admin_server_simulation_accepts_execution_host_metadata(self):
        self.request_json("GET", "/api/auth/session")
        captured = []

        def start(config):
            captured.append(config)
            return {"running": True}

        with patch.object(
            self.server.RequestHandlerClass, "access_context", "local_admin"
        ), patch.object(
            self.server.RequestHandlerClass.dashboard.acquisition,
            "start",
            side_effect=start,
        ):
            status, result, _ = self.request_json(
                "POST",
                "/api/acquisition/start",
                {
                    "acquisition_mode": "simulation",
                    "driver": "simulator",
                    "execution_host": "server",
                },
            )

        self.assertEqual(status, 200, result)
        self.assertTrue(result["running"])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].acquisition_mode, "simulation")

    def test_local_admin_server_simulation_still_rejects_unknown_config_fields(self):
        self.request_json("GET", "/api/auth/session")
        with patch.object(
            self.server.RequestHandlerClass, "access_context", "local_admin"
        ):
            status, result, _ = self.request_json(
                "POST",
                "/api/acquisition/start",
                {
                    "acquisition_mode": "simulation",
                    "driver": "simulator",
                    "unexpected_config_typo": "must-not-be-silently-ignored",
                },
            )

        self.assertEqual(status, 400, result)
        self.assertIn("unexpected_config_typo", result["error"])

    def test_authorized_server_simulation_rejects_implicit_helper_fallback(self):
        self.request_json("GET", "/api/auth/session")
        self.request_json(
            "POST", "/api/auth/login", {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )

        status, result, _ = self.request_json(
            "POST",
            "/api/acquisition/start",
            {"acquisition_mode": "simulation", "driver": "simulator"},
            headers={"X-Forwarded-Proto": "https"},
        )

        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "explicit_server_simulation_required")

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
        payload = {
            "acquisition_mode": "simulation", "driver": "simulator",
            "execution_host": "server",
            "simulation_execution_choice": "server_explicit",
        }
        guest_acquisition = self.server.guest_manager.ensure_session(
            self.cookies["afp_guest"]
        ).acquisition
        with patch.object(
            guest_acquisition,
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

    def test_authorized_simulation_stop_survives_real_control_lease_expiry(self):
        self.request_json("GET", "/api/auth/session")
        self.request_json(
            "POST", "/api/auth/login", {"password": "Correct-Horse-2026"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.request_json(
            "POST", "/api/real/control/acquire", {},
            headers={"X-Forwarded-Proto": "https"},
        )
        guest_id = self.cookies["afp_guest"]
        self.server.guest_manager.start(
            guest_id,
            {"processing_mode": "capture_only", "selected_sensors": ["温度", "压力"]},
        )
        self.server.control_lease.release(self.server.control_lease.status()["owner_id"])

        status, stopped, _ = self.request_json(
            "POST", "/api/acquisition/stop", {"acquisition_mode": "simulation"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 200)
        self.assertFalse(stopped["running"])
        self.assertEqual(self.hardware_start_calls, 0)

        real_status, real_result, _ = self.request_json(
            "POST", "/api/acquisition/stop", {"acquisition_mode": "real"},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(real_status, 409)
        self.assertEqual(real_result["error"], "real_control_required")

    def test_remote_simulation_live_and_status_hide_server_paths(self):
        self.request_json("GET", "/api/auth/session")
        guest_id = self.cookies["afp_guest"]
        self.server.guest_manager.start(
            guest_id,
            {"processing_mode": "capture_only", "selected_sensors": ["温度", "压力"]},
        )
        try:
            session = self.server.guest_manager.ensure_session(guest_id)
            private_status = {
                "running": True,
                "config": {
                    "source_file": str(self.default_simulation_source),
                    "save_root": str(session.save_root),
                },
                "files": [str(session.save_root / "capture.csv")],
            }
            with patch.object(session.acquisition, "status", return_value=private_status):
                status_code, guest_live, _ = self.request_json("GET", "/api/simulation/live")
                self.assertEqual(status_code, 200)
                self.assertEqual(guest_live["acquisition"]["config"]["source_file"], "管理员批准的模拟数据源")
                self.assertEqual(
                    guest_live["acquisition"]["files"][0],
                    f"public_simulation/{guest_id}/capture.csv",
                )
                guest_encoded = json.dumps(guest_live, ensure_ascii=False)
                self.assertNotIn(str(self.default_simulation_source), guest_encoded)
                self.assertNotIn(str(self.server.guest_manager.root), guest_encoded)

                self.request_json(
                    "POST", "/api/auth/login", {"password": "Correct-Horse-2026"},
                    headers={"X-Forwarded-Proto": "https"},
                )
                status_code, authorized_status, _ = self.request_json(
                    "GET", "/api/acquisition/status?acquisition_mode=simulation",
                    headers={"X-Forwarded-Proto": "https"},
                )
                self.assertEqual(status_code, 200)
                self.assertEqual(authorized_status["config"]["source_file"], "管理员批准的模拟数据源")
                self.assertEqual(
                    authorized_status["files"][0],
                    f"public_simulation/{guest_id}/capture.csv",
                )
                encoded = json.dumps(authorized_status, ensure_ascii=False)
                self.assertNotIn(str(self.default_simulation_source), encoded)
                self.assertNotIn(str(self.server.guest_manager.root), encoded)

                status_code, authorized_live, _ = self.request_json(
                    "GET", "/api/live?acquisition_mode=simulation",
                    headers={"X-Forwarded-Proto": "https"},
                )
                self.assertEqual(status_code, 200)
                self.assertEqual(
                    authorized_live["acquisition"]["config"]["source_file"],
                    "管理员批准的模拟数据源",
                )
        finally:
            self.server.guest_manager.stop(guest_id)

    def test_authorized_default_simulation_filename_resolves_to_bootstrap_path(self):
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
        with patch.object(
            self.server.dashboard.acquisition,
            "start",
            return_value={"running": True},
        ) as start:
            status, response_payload, _ = self.request_json(
                "POST",
                "/api/acquisition/start",
                {
                    "acquisition_mode": "simulation",
                    "driver": "simulator",
                    "simulation_source_type": "single_csv",
                    "simulation_source_path": self.default_simulation_source.name,
                    "source_file": self.default_simulation_source.name,
                    "execution_host": "server",
                    "simulation_execution_choice": "server_explicit",
                },
                headers={"X-Forwarded-Proto": "https"},
            )

        self.assertEqual(status, 400)
        self.assertEqual(response_payload["error"], "remote_path_not_accessible")
        start.assert_not_called()


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


class ModelCredentialTests(unittest.TestCase):
    # Reuse the HTTP fixture without inheriting every PublicWebHttpTests test.
    # Inheriting the test case duplicated the complete loopback-server suite
    # and could exhaust short-lived Windows connections during the full gate.
    setUp = PublicWebHttpTests.setUp
    tearDown = PublicWebHttpTests.tearDown
    request_json = PublicWebHttpTests.request_json

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
        self.assertEqual(self.hardware_discovery_calls, 0)
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
        script = (Path(__file__).resolve().parent.parent / "modular_runtime" / "assemble_modular_delivery.ps1").read_text(encoding="utf-8-sig")
        for name in (
            "web_auth.py",
            "web_access.py",
            "public_status.py",
            "guest_simulation.py",
            "control_lease.py",
            "simulation_replay.py",
            "simulation_source_transfer.py",
            "public_ingress_state.py",
        ):
            self.assertIn(f'"{name}"', script)

    def test_build_script_contains_delivery_secret_scan(self):
        script = (Path(__file__).resolve().parent.parent / "modular_runtime" / "assemble_modular_delivery.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("public_web_security.sqlite3", script)
        self.assertIn("sk-[A-Za-z0-9_-]{20,}", script)

    def test_build_script_packages_edge_capture_runtime(self):
        script = (Path(__file__).resolve().parent.parent / "modular_runtime" / "assemble_modular_delivery.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('"edge_capture.py"', script)

    def test_build_script_packages_server_target_mysql_runtime(self):
        script = (Path(__file__).resolve().parent.parent / "modular_runtime" / "assemble_modular_delivery.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('"server_target_mysql.py"', script)
        self.assertIn('"server_capture_journal.py"', script)


if __name__ == "__main__":
    unittest.main()
