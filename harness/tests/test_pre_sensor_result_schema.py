from __future__ import annotations

import unittest

from harness.engine.models import (
    PHYSICAL_SENSOR_CHECK_IDS,
    PreSensorResult,
)
from harness.engine.reporting import aggregate_pre_sensor_results, pre_sensor_payload


class PreSensorResultSchemaTests(unittest.TestCase):
    def test_only_physical_whitelist_may_be_pending(self) -> None:
        physical_id = next(iter(PHYSICAL_SENSOR_CHECK_IDS))
        result = PreSensorResult(
            check_id=physical_id,
            title="physical check",
            status="physical_sensor_pending",
            evidence="No sensor is attached",
            remediation="Connect the named sensor during field acceptance",
        )
        self.assertEqual(result.status, "physical_sensor_pending")
        self.assertFalse(result.external_environment)

        for check_id in (
            "environment.second_windows",
            "environment.target_mysql",
            "environment.admin_privilege",
            "environment.network_access",
            "storage.save_directory",
        ):
            with self.subTest(check_id=check_id):
                with self.assertRaisesRegex(ValueError, "physical_sensor_pending"):
                    PreSensorResult(
                        check_id=check_id,
                        title="not physical",
                        status="physical_sensor_pending",
                        evidence="unavailable",
                        remediation="provide environment",
                        external_environment=True,
                    )

    def test_external_environment_absence_is_blocked_with_remediation(self) -> None:
        for check_id in (
            "environment.second_windows",
            "environment.target_mysql",
            "environment.admin_privilege",
            "environment.network_access",
        ):
            with self.subTest(check_id=check_id):
                result = PreSensorResult(
                    check_id=check_id,
                    title="external prerequisite",
                    status="blocked",
                    evidence="not available on this host",
                    remediation="Provide the named environment and rerun this check",
                    external_environment=True,
                )
                self.assertTrue(result.external_environment)
                self.assertTrue(result.remediation)

        with self.assertRaisesRegex(ValueError, "remediation"):
            PreSensorResult(
                check_id="environment.target_mysql",
                title="target database",
                status="blocked",
                evidence="not configured",
                remediation="",
                external_environment=True,
            )

    def test_aggregation_and_payload_preserve_all_four_states(self) -> None:
        physical_id = next(iter(PHYSICAL_SENSOR_CHECK_IDS))
        results = (
            PreSensorResult("release.manifest", "manifest", "passed", "digest ok", ""),
            PreSensorResult(
                "storage.save_directory",
                "storage",
                "failed",
                "rename failed",
                "Select a writable directory",
            ),
            PreSensorResult(
                "environment.target_mysql",
                "target database",
                "blocked",
                "not configured",
                "Provide target connection settings",
                external_environment=True,
            ),
            PreSensorResult(
                physical_id,
                "physical device",
                "physical_sensor_pending",
                "not connected",
                "Validate on site",
            ),
        )

        summary = aggregate_pre_sensor_results(results)
        self.assertEqual(summary.outcome, "failed")
        self.assertEqual((summary.passed, summary.failed, summary.blocked), (1, 1, 1))
        self.assertEqual(summary.physical_sensor_pending, 1)

        payload = pre_sensor_payload(results)
        self.assertEqual(payload["summary"]["outcome"], "failed")
        self.assertEqual(
            {item["status"] for item in payload["checks"]},
            {"passed", "failed", "blocked", "physical_sensor_pending"},
        )
        blocked = next(item for item in payload["checks"] if item["status"] == "blocked")
        self.assertTrue(blocked["external_environment"])
        self.assertIn("Provide", blocked["remediation"])


if __name__ == "__main__":
    unittest.main()
