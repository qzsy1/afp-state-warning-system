from __future__ import annotations

import unittest
from pathlib import Path

from harness.engine.matrix import load_matrix


ROOT = Path(__file__).resolve().parents[2]


class PreSensorReadinessMatrixContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = load_matrix(ROOT / "harness" / "config" / "regression-matrix.json")

    def test_pre_sensor_requirement_families_have_stable_profiles(self) -> None:
        expected = {
            "PRE_SENSOR_RELEASE-001": ("quick", "full", "release"),
            "PRE_SENSOR_ENVIRONMENT-001": ("quick", "full", "release"),
            "PRE_SENSOR_ENDURANCE-001": ("full", "release"),
            "PRE_SENSOR_FIELD_OPS-001": ("full", "release"),
            "PRE_SENSOR_PUBLIC_INGRESS-001": ("quick", "full", "release"),
            "PRE_SENSOR_INGRESS_GENERATION-001": ("quick", "full", "release"),
            "PRE_SENSOR_INGRESS_CUTOVER-001": ("quick", "full", "release"),
            "PRE_SENSOR_HELPER_GENERATION-001": ("quick", "full", "release"),
            "PRE_SENSOR_MODEL_PROVENANCE-001": ("quick", "full", "release"),
        }
        requirements = {item.id: item for item in self.matrix.requirements}

        for requirement_id, profiles in expected.items():
            with self.subTest(requirement=requirement_id):
                self.assertIn(requirement_id, requirements)
                requirement = requirements.get(requirement_id)
                if requirement is not None:
                    self.assertTrue(requirement.mandatory)
                    self.assertEqual(profiles, requirement.profiles)

    def test_pre_sensor_checks_are_blocking_and_evidence_scoped(self) -> None:
        expected = {
            "pre-sensor-readiness-contracts": (
                ("quick", "full", "release"),
                "automated",
                {"PRE_SENSOR_RELEASE-001", "PRE_SENSOR_ENVIRONMENT-001"},
            ),
            "delivery-readonly-baseline": (
                ("quick", "full", "release"),
                "automated",
                {"PRE_SENSOR_RELEASE-001"},
            ),
            "public-ingress-cutover-contracts": (
                ("quick", "full", "release"),
                "automated",
                {
                    "PRE_SENSOR_PUBLIC_INGRESS-001",
                    "PRE_SENSOR_INGRESS_GENERATION-001",
                    "PRE_SENSOR_INGRESS_CUTOVER-001",
                    "PRE_SENSOR_HELPER_GENERATION-001",
                    "PRE_SENSOR_MODEL_PROVENANCE-001",
                },
            ),
            "pre-sensor-endurance-contracts": (
                ("full", "release"),
                "protocol_simulation",
                {"PRE_SENSOR_ENDURANCE-001"},
            ),
            "pre-sensor-field-operations-contracts": (
                ("full", "release"),
                "automated",
                {"PRE_SENSOR_FIELD_OPS-001"},
            ),
        }
        checks = {item.id: item for item in self.matrix.checks}

        for check_id, (profiles, evidence_tier, requirements) in expected.items():
            with self.subTest(check=check_id):
                self.assertIn(check_id, checks)
                check = checks.get(check_id)
                if check is not None:
                    self.assertTrue(check.blocking)
                    self.assertEqual(profiles, check.profiles)
                    self.assertEqual(evidence_tier, check.evidence_tier)
                    self.assertTrue(requirements.issubset(check.requirements))

        delivery = checks.get("delivery-readonly-baseline")
        self.assertIsNotNone(delivery)
        if delivery is not None:
            self.assertIn("harness.engine.delivery_readonly", delivery.command)

    def test_external_environment_and_physical_sensor_checks_are_distinct(self) -> None:
        checks = {item.id: item for item in self.matrix.checks}
        self.assertIn("environment-second-windows-readiness", checks)
        self.assertIn("environment-target-mysql-readiness", checks)
        self.assertIn("field-real-hardware", checks)
        second_windows = checks.get("environment-second-windows-readiness")
        target_mysql = checks.get("environment-target-mysql-readiness")
        physical = checks.get("field-real-hardware")
        if second_windows is not None and target_mysql is not None and physical is not None:
            self.assertEqual("command", second_windows.kind)
            self.assertEqual("command", target_mysql.kind)
            self.assertEqual("field", physical.kind)
            self.assertNotEqual(second_windows.evidence_tier, physical.evidence_tier)
            self.assertNotEqual(target_mysql.evidence_tier, physical.evidence_tier)


if __name__ == "__main__":
    unittest.main()
