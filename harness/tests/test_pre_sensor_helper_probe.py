from __future__ import annotations

import json
import unittest
from pathlib import Path

from visualization_app.pre_sensor_preflight import (
    ExternalEnvironmentBlocked,
    probe_helper_readiness,
)


ROOT = Path(__file__).resolve().parents[2]


class HelperReadinessProbeTests(unittest.TestCase):
    def _status(self) -> dict:
        contract = json.loads(
            (ROOT / "pre_sensor" / "release-contract.json").read_text(encoding="utf-8")
        )
        identity = contract["identity"]
        values = contract["contracts"]
        return {
            "installed": True,
            "process_running": True,
            "port_open": True,
            "health_ok": True,
            "paired": True,
            "readiness_imported": True,
            "http_ok": True,
            "websocket_ok": True,
            "proxy_ok": True,
            "capabilities": {
                "protocol_version": values["helper_protocol_version"],
                "helper_version": identity["helper_version"],
                "build_id": identity["release_revision"],
                "unified_frame_contract": values["unified_frame_contract"],
                "configuration_schema_version": values["configuration_schema_version"],
                "readiness_schema_version": values["readiness_schema_version"],
                "device_readiness_contract": "plc_abb_uvc_readiness_v1",
                "unified_device_readiness_v1": True,
                "unified_frame_v1": True,
                "unified_frame_v2": True,
            },
        }

    def test_helper_install_health_pairing_transport_and_contract_pass(self) -> None:
        outcome = probe_helper_readiness(
            {
                "mode": "remote_helper",
                "helper_status_provider": self._status,
                "helper_evidence_tier": "two_machine_environment",
            }
        )
        self.assertEqual(outcome.status, "passed")
        self.assertEqual(outcome.details["evidence_tier"], "two_machine_environment")
        self.assertNotIn("capabilities", outcome.details)

    def test_each_helper_stage_and_version_mismatch_fails_closed(self) -> None:
        fields = (
            "installed",
            "process_running",
            "port_open",
            "health_ok",
            "paired",
            "readiness_imported",
            "http_ok",
            "websocket_ok",
            "proxy_ok",
        )
        for field in fields:
            with self.subTest(field=field):
                state = self._status()
                state[field] = False
                with self.assertRaisesRegex(ValueError, field):
                    probe_helper_readiness(
                        {
                            "mode": "remote_helper",
                            "helper_status_provider": lambda state=state: state,
                            "helper_evidence_tier": "two_machine_environment",
                        }
                    )
        state = self._status()
        state["capabilities"]["helper_version"] = "wrong"
        with self.assertRaisesRegex(ValueError, "release_contract_mismatch"):
            probe_helper_readiness(
                {
                    "mode": "remote_helper",
                    "helper_status_provider": lambda: state,
                    "helper_evidence_tier": "two_machine_environment",
                }
            )

    def test_missing_second_machine_is_blocked_and_local_mode_is_not_applicable(self) -> None:
        with self.assertRaises(ExternalEnvironmentBlocked):
            probe_helper_readiness({"mode": "remote_helper"})
        local = probe_helper_readiness({"mode": "local_direct"})
        self.assertEqual(local.status, "passed")
        self.assertTrue(local.details["not_applicable"])

    def test_protocol_simulation_is_labeled_and_cannot_claim_two_machine(self) -> None:
        outcome = probe_helper_readiness(
            {
                "mode": "remote_helper",
                "helper_status_provider": self._status,
                "helper_evidence_tier": "protocol_simulation",
            }
        )
        self.assertEqual(outcome.details["evidence_tier"], "protocol_simulation")
        self.assertIn("simulation", outcome.evidence)


if __name__ == "__main__":
    unittest.main()
