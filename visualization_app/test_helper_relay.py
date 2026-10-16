from __future__ import annotations

import sys
import json
import threading
import time
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helper_relay import HelperRegistry, select_simulation_execution  # noqa: E402
from public_ingress_state import PublicIngressState  # noqa: E402


def ingress_state(generation: int) -> PublicIngressState:
    return PublicIngressState(
        schema_version=1,
        generation=generation,
        public_url="https://afp.example.test",
        provider="tailscale",
        recovery_owner="tailscale",
        candidate_id="candidate-1",
        status="online",
        published_at="2026-10-15T00:00:00+00:00",
        layers={"edge": {"ok": True}},
    )


class MutableIngressLoader:
    def __init__(self, generation: int = 1):
        self.generation = generation
        self.barrier = None

    def __call__(self, _path, _candidate_id=""):
        if self.barrier is not None:
            self.barrier.wait()
            self.barrier.wait()
        return ingress_state(self.generation)


class HelperRelayTests(unittest.TestCase):
    def setUp(self):
        self.runtime_directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.runtime_directory.cleanup()

    def generation_registry(self, loader, **kwargs):
        ingress_state_path = kwargs.pop(
            "ingress_state_path",
            Path(self.runtime_directory.name) / "public-ingress-state.json",
        )
        return HelperRegistry(
            ingress_state_path=ingress_state_path,
            expected_candidate_id="candidate-1",
            ingress_state_loader=loader,
            **kwargs,
        )

    def pair(self, registry):
        challenge = registry.start_pairing("session-a")
        self.assertTrue(challenge["ok"])
        paired = registry.complete_pairing(challenge["challenge"], "device-a", {})
        self.assertTrue(paired["ok"])
        return paired

    def test_generation_change_revokes_pairing_and_persisted_helper_token(self):
        loader = MutableIngressLoader()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helpers.json"
            registry = self.generation_registry(loader, persistence_path=path)
            paired = self.pair(registry)
            self.assertIn("token_hash", path.read_text(encoding="utf-8"))
            loader.generation = 2
            status = registry.status("session-a")
            self.assertFalse(status["paired"])
            self.assertIsNone(registry.authenticate("device-a", paired["pairing_token"]))
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["helpers"], {})

    def test_generation_change_terminalizes_queued_and_unconfirmed_commands_once(self):
        loader = MutableIngressLoader()
        registry = self.generation_registry(loader)
        paired = self.pair(registry)
        queued = registry.command("session-a", "status", {})
        delivered_messages = []
        self.assertTrue(
            registry.attach(
                "session-a", "device-a", paired["pairing_token"], delivered_messages.append
            )
        )
        delivered = registry.command("session-a", "discover", {})
        loader.generation = 2
        registry.status("session-a")
        expected = {"ok": False, "error": "ingress_generation_changed", "terminal": True}
        self.assertEqual(registry.pop_result("session-a", queued["request_id"]), expected)
        self.assertIsNone(registry.pop_result("session-a", queued["request_id"]))
        self.assertEqual(registry.pop_result("session-a", delivered["request_id"]), expected)
        self.assertIsNone(registry.pop_result("session-a", delivered["request_id"]))

    def test_generation_change_during_poll_never_delivers_old_command(self):
        loader = MutableIngressLoader()
        registry = self.generation_registry(loader)
        paired = self.pair(registry)
        command = registry.command("session-a", "status", {})
        barrier = threading.Barrier(2)
        loader.barrier = barrier
        result = {}

        def poll():
            result.update(registry.poll("device-a", paired["pairing_token"]))

        worker = threading.Thread(target=poll)
        worker.start()
        barrier.wait()
        loader.generation = 2
        barrier.wait()
        worker.join(timeout=2)
        loader.barrier = None
        self.assertFalse(worker.is_alive())
        self.assertNotIn("command", result)
        self.assertEqual(result["error"], "helper_authentication_failed")
        self.assertEqual(
            registry.pop_result("session-a", command["request_id"]),
            {"ok": False, "error": "ingress_generation_changed", "terminal": True},
        )

    def test_restart_after_state_publish_refuses_old_persisted_helper(self):
        loader = MutableIngressLoader()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helpers.json"
            first = self.generation_registry(loader, persistence_path=path)
            paired = self.pair(first)
            loader.generation = 2
            restarted = self.generation_registry(loader, persistence_path=path)
            self.assertIsNone(restarted.authenticate("device-a", paired["pairing_token"]))
            self.assertFalse(restarted.status("session-a")["paired"])

    def test_same_generation_restart_terminalizes_lost_command_once(self):
        loader = MutableIngressLoader()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helpers.json"
            first = self.generation_registry(loader, persistence_path=path)
            self.pair(first)
            request = first.command("session-a", "status", {})
            restarted = self.generation_registry(loader, persistence_path=path)
            self.assertEqual(
                restarted.pop_result("session-a", request["request_id"]),
                {"ok": False, "error": "command_state_lost", "terminal": True},
            )
            self.assertIsNone(
                restarted.pop_result("session-a", request["request_id"])
            )

    def test_late_result_cannot_replace_terminal_and_status_reconciles_blocker(self):
        loader = MutableIngressLoader()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helpers.json"
            first = self.generation_registry(loader, persistence_path=path)
            paired = self.pair(first)
            self.assertTrue(
                first.attach(
                    "session-a",
                    "device-a",
                    paired["pairing_token"],
                    lambda _message: None,
                )
            )
            with patch("helper_relay.helper_capability_mismatches", return_value=()):
                request = first.command("session-a", "start_capture", {})
            restarted = self.generation_registry(loader, persistence_path=path)
            self.assertFalse(restarted.cutover_readiness()["allowed"])
            self.assertEqual(
                restarted.authenticate("device-a", paired["pairing_token"]),
                "session-a",
            )
            accepted = restarted.accept_result(
                "device-a",
                paired["pairing_token"],
                request["request_id"],
                {"ok": True, "running": False},
                ingress_generation=1,
            )
            self.assertFalse(accepted["ok"])
            self.assertEqual(accepted["error"], "command_state_lost")
            self.assertEqual(
                restarted.pop_result("session-a", request["request_id"])["error"],
                "command_state_lost",
            )
            self.assertIsNone(
                restarted.pop_result("session-a", request["request_id"])
            )
            status_request = restarted.command("session-a", "status", {})
            reconciled = restarted.accept_result(
                "device-a",
                paired["pairing_token"],
                status_request["request_id"],
                {"ok": True, "running": False},
                ingress_generation=1,
            )
            self.assertTrue(reconciled["ok"])
            self.assertTrue(restarted.cutover_readiness()["allowed"])

    def test_production_result_requires_explicit_generation(self):
        loader = MutableIngressLoader()
        registry = self.generation_registry(loader)
        paired = self.pair(registry)
        request = registry.command("session-a", "status", {})
        rejected = registry.accept_result(
            "device-a", paired["pairing_token"], request["request_id"], {"ok": True}
        )
        self.assertEqual(rejected["error"], "ingress_generation_mismatch")

    def test_command_and_result_generation_mismatch_are_rejected(self):
        loader = MutableIngressLoader()
        registry = self.generation_registry(loader)
        paired = self.pair(registry)
        request = registry.command("session-a", "status", {})
        self.assertEqual(request["ingress_generation"], 1)
        rejected = registry.accept_result(
            "device-a", paired["pairing_token"], request["request_id"],
            {"ok": True}, ingress_generation=0,
        )
        self.assertEqual(rejected["error"], "ingress_generation_mismatch")
        accepted = registry.accept_result(
            "device-a", paired["pairing_token"], request["request_id"],
            {"ok": True}, ingress_generation=1,
        )
        self.assertTrue(accepted["ok"])

    def test_cutover_readiness_blocks_confirmed_running_capture(self):
        loader = MutableIngressLoader()
        registry = self.generation_registry(loader)
        paired = self.pair(registry)
        with patch("helper_relay.helper_capability_mismatches", return_value=()):
            request = registry.command("session-a", "start_capture", {})
        registry.accept_result(
            "device-a", paired["pairing_token"], request["request_id"],
            {"ok": True, "running": True, "capture_uuid": "capture-1"},
            ingress_generation=1,
        )
        readiness = registry.cutover_readiness()
        self.assertFalse(readiness["allowed"])
        self.assertEqual(readiness["generation"], 1)
        self.assertEqual(readiness["active_capture_uuids"], ["capture-1"])

    def test_cutover_reservation_blocks_new_capture_until_released(self):
        loader = MutableIngressLoader()
        registry = self.generation_registry(loader)
        self.pair(registry)
        reservation = registry.reserve_cutover()
        self.assertTrue(reservation["allowed"])
        blocked = registry.command("session-a", "start_capture", {})
        self.assertEqual(blocked["error"], "ingress_cutover_in_progress")
        self.assertFalse(registry.release_cutover("wrong"))
        self.assertTrue(registry.release_cutover(reservation["reservation_id"]))
        with patch("helper_relay.helper_capability_mismatches", return_value=()):
            allowed = registry.command("session-a", "start_capture", {})
        self.assertTrue(allowed["ok"])

    def test_reservation_owner_can_recheck_readiness(self):
        loader = MutableIngressLoader()
        registry = self.generation_registry(loader)
        reservation = registry.reserve_cutover()
        self.assertTrue(reservation["allowed"])
        self.assertFalse(registry.cutover_readiness()["allowed"])
        owned = registry.cutover_readiness(reservation["reservation_id"])
        self.assertTrue(owned["allowed"])
        self.assertTrue(owned["reservation_active"])

    def test_cutover_reservation_survives_registry_restart(self):
        loader = MutableIngressLoader()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingress_path = root / "public-ingress-state.json"
            helper_path = root / "helper_registry.json"
            first = self.generation_registry(
                loader,
                ingress_state_path=ingress_path,
                persistence_path=helper_path,
            )
            reservation = first.reserve_cutover()
            self.assertTrue(reservation["allowed"])
            restarted = self.generation_registry(
                loader,
                ingress_state_path=ingress_path,
                persistence_path=helper_path,
            )
            self.assertTrue(restarted.cutover_reserved())
            self.assertFalse(restarted.begin_capture_start())
            self.assertTrue(
                restarted.release_cutover(reservation["reservation_id"])
            )
            self.assertTrue(restarted.begin_capture_start())
            restarted.end_capture_start()

    def test_expired_persisted_reservation_is_replaced_under_lock(self):
        loader = MutableIngressLoader()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingress_path = root / "public-ingress-state.json"
            lease_path = root / "public-ingress-cutover.json"
            lease_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "reservation_hash": "0" * 64,
                        "expires_at": time.time() - 1,
                    }
                ),
                encoding="utf-8",
            )
            registry = self.generation_registry(
                loader,
                ingress_state_path=ingress_path,
                persistence_path=root / "helper_registry.json",
            )
            self.assertFalse(registry.cutover_reserved())
            reservation = registry.reserve_cutover()
            self.assertTrue(reservation["allowed"])
            persisted = json.loads(lease_path.read_text(encoding="utf-8"))
            self.assertNotEqual(persisted["reservation_hash"], "0" * 64)
            self.assertTrue(
                registry.release_cutover(reservation["reservation_id"])
            )

    def test_degraded_ingress_blocks_capture_after_reservation_expiry(self):
        degraded = ingress_state(1)
        degraded = PublicIngressState(
            schema_version=degraded.schema_version,
            generation=degraded.generation,
            public_url=degraded.public_url,
            provider=degraded.provider,
            recovery_owner=degraded.recovery_owner,
            candidate_id=degraded.candidate_id,
            status="degraded",
            published_at=degraded.published_at,
            layers={"helper": {"ok": False}},
        )
        registry = HelperRegistry(
            ingress_state_path=Path(self.runtime_directory.name) / "degraded-ingress.json",
            expected_candidate_id="candidate-1",
            ingress_state_loader=lambda *_args: degraded,
        )
        paired = self.pair(registry)
        registry.attach(
            "session-a",
            "device-a",
            paired["pairing_token"],
            lambda _message: None,
        )
        blocked = registry.command("session-a", "start_capture", {})
        self.assertEqual(blocked["error"], "ingress_not_online")
        self.assertFalse(registry.begin_capture_start())

    def test_deployment_readiness_requires_compatible_online_helper(self):
        loader = MutableIngressLoader()
        registry = self.generation_registry(loader)
        paired = self.pair(registry)
        registry.attach(
            "session-a",
            "device-a",
            paired["pairing_token"],
            lambda _message: None,
            capabilities={"command_lifecycle": "wrong"},
        )
        with patch("helper_relay.helper_capability_mismatches", return_value=()):
            self.assertFalse(registry.deployment_readiness()["ready"])
            registry.attach(
                "session-a",
                "device-a",
                paired["pairing_token"],
                lambda _message: None,
                capabilities={"command_lifecycle": "isolated_hardware_check"},
            )
            readiness = registry.deployment_readiness()
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["generation"], 1)
        self.assertEqual(readiness["candidate_id"], "candidate-1")

    def test_terminal_result_is_consumed_once_across_restart(self):
        loader = MutableIngressLoader()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helpers.json"
            registry = self.generation_registry(loader, persistence_path=path)
            self.pair(registry)
            request = registry.command("session-a", "status", {})
            loader.generation = 2
            registry.status("session-a")
            self.assertIsNotNone(registry.pop_result("session-a", request["request_id"]))
            restarted = self.generation_registry(loader, persistence_path=path)
            self.assertIsNone(restarted.pop_result("session-a", request["request_id"]))

    def test_confirmed_capture_remains_a_cutover_blocker_after_restart(self):
        loader = MutableIngressLoader()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helpers.json"
            registry = self.generation_registry(loader, persistence_path=path)
            paired = self.pair(registry)
            with patch("helper_relay.helper_capability_mismatches", return_value=()):
                request = registry.command("session-a", "start_capture", {})
            registry.accept_result(
                "device-a", paired["pairing_token"], request["request_id"],
                {"ok": True, "running": True, "capture_uuid": "capture-persisted"},
                ingress_generation=1,
            )
            restarted = self.generation_registry(loader, persistence_path=path)
            readiness = restarted.cutover_readiness()
            self.assertFalse(readiness["allowed"])
            self.assertEqual(readiness["active_capture_uuids"], ["capture-persisted"])

    def test_remote_simulation_prefers_only_online_compatible_helper(self):
        compatible = {
            "online": True,
            "protocol_compatible": True,
            "capabilities": {"simulation_replay_v1": True},
        }
        old_helper = {
            "online": True,
            "protocol_compatible": True,
            "capabilities": {},
        }
        offline = {
            "online": False,
            "protocol_compatible": True,
            "capabilities": {"simulation_replay_v1": True},
        }

        self.assertEqual(
            select_simulation_execution(compatible),
            {"execution_host": "helper_local", "fallback_reason": ""},
        )
        self.assertEqual(
            select_simulation_execution(old_helper),
            {
                "execution_host": "server",
                "fallback_reason": "helper_simulation_replay_unsupported",
            },
        )
        self.assertEqual(
            select_simulation_execution(offline),
            {"execution_host": "server", "fallback_reason": "helper_offline"},
        )

    def test_default_heartbeat_window_covers_transient_helper_backoff(self):
        registry = HelperRegistry()
        self.assertGreaterEqual(registry.heartbeat_ttl_seconds, 45)

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

    def test_attach_updates_capabilities_and_marks_helper_online(self):
        registry = HelperRegistry()
        challenge = registry.start_pairing("session-a")
        paired = registry.complete_pairing(challenge["challenge"], "device-a", {})
        registry.detach("session-a")

        attached = registry.attach(
            "session-a",
            "device-a",
            paired["pairing_token"],
            lambda _request: None,
            capabilities={"hardware_discovery": True},
        )

        self.assertTrue(attached)
        self.assertTrue(registry.status("session-a")["online"])
        self.assertTrue(registry.status("session-a")["capabilities"]["hardware_discovery"])

    def test_status_distinguishes_online_but_incompatible_helper(self):
        registry = HelperRegistry()
        challenge = registry.start_pairing("session-a")
        registry.complete_pairing(
            challenge["challenge"],
            "device-a",
            {"protocol_version": 1, "hardware_discovery": True},
        )
        status = registry.status("session-a")

        self.assertTrue(status["online"])
        self.assertFalse(status["protocol_compatible"])
        self.assertEqual(status["protocol_version"], 1)

        challenge = registry.start_pairing("session-b")
        registry.complete_pairing(
            challenge["challenge"],
            "device-b",
            {
                "protocol_version": 2,
                "command_lifecycle": "isolated_hardware_check",
            },
        )
        self.assertTrue(registry.status("session-b")["protocol_compatible"])

    def test_stale_websocket_detach_cannot_offline_new_connection(self):
        registry = HelperRegistry()
        challenge = registry.start_pairing("session-a")
        paired = registry.complete_pairing(challenge["challenge"], "device-a", {})
        first_messages = []
        second_messages = []

        first_sender = first_messages.append
        second_sender = second_messages.append
        self.assertTrue(
            registry.attach("session-a", "device-a", paired["pairing_token"], first_sender)
        )
        self.assertTrue(
            registry.attach("session-a", "device-a", paired["pairing_token"], second_sender)
        )

        registry.detach("session-a", sender=first_sender)
        status = registry.status("session-a")
        request = registry.command("session-a", "status", {})

        self.assertTrue(status["online"])
        self.assertTrue(request["queued"])
        self.assertEqual(first_messages, [])
        self.assertEqual(second_messages[0]["request_id"], request["request_id"])

    def test_command_send_failure_returns_terminal_transport_error(self):
        registry = HelperRegistry()
        challenge = registry.start_pairing("session-a")
        paired = registry.complete_pairing(challenge["challenge"], "device-a", {})

        def disconnected_sender(_request):
            raise BrokenPipeError("connection closed")

        self.assertTrue(
            registry.attach(
                "session-a",
                "device-a",
                paired["pairing_token"],
                disconnected_sender,
            )
        )

        try:
            result = registry.command("session-a", "status", {})
        except BrokenPipeError as exc:
            self.fail(f"transport failure escaped command lifecycle: {exc}")

        self.assertFalse(result["ok"])
        self.assertFalse(result["queued"])
        self.assertEqual(result["error"], "helper_transport_send_failed")
        self.assertFalse(registry.status("session-a")["online"])


if __name__ == "__main__":
    unittest.main()
