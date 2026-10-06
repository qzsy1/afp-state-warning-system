import unittest
from pathlib import Path

from harness.engine.matrix import load_matrix, select_checks


class RegressionMatrixContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).parents[2]
        cls.path = cls.repo / "harness" / "config" / "regression-matrix.json"
        cls.matrix = load_matrix(cls.path)

    def test_requirement_families_cover_the_approved_balanced_scope(self) -> None:
        families = {requirement.id.split("-", 1)[0] for requirement in self.matrix.requirements}
        self.assertEqual(
            families,
            {
                "HARNESS", "STARTUP", "SIMULATION", "MODE_ISOLATION", "INTERFACES",
                "USB_TOPOLOGY",
                "EDGE_HELPER", "CSV", "MYSQL", "DIAGNOSIS", "LAN_PUBLIC",
                "WEBSOCKET", "MODEL_CHANNELS", "PREDICTION_WARNING", "EXE_REUSE",
                "RELEASE_EVIDENCE", "FIELD_BOUNDARY",
            },
        )

    def test_all_commands_have_existing_working_directories_and_sources(self) -> None:
        for check in self.matrix.checks:
            with self.subTest(check=check.id):
                self.assertTrue((self.repo / check.cwd).is_dir())
                if (
                    check.kind == "command"
                    and check.command[0] not in {"{python}", "{baseline_exe}"}
                    and "/" in check.command[0]
                ):
                    self.assertTrue((self.repo / check.cwd / check.command[0]).is_file())

    def test_ci_full_is_portable_automated_and_has_complete_coverage(self) -> None:
        checks = select_checks(self.matrix, "full", "ci")
        self.assertGreaterEqual(len(checks), 6)
        for check in checks:
            with self.subTest(check=check.id):
                self.assertEqual(check.kind, "command")
                self.assertEqual(check.evidence_tier, "automated")
                joined = " ".join(check.command).lower()
                self.assertNotIn("password", joined)
                self.assertNotIn("api_key", joined)
                self.assertNotIn("delivery/", joined)

    def test_release_contains_all_packaged_diagnostic_modes(self) -> None:
        release_checks = [
            check
            for check in self.matrix.checks
            if check.id.startswith("release-") and check.kind == "command"
        ]
        release_commands = [" ".join(check.command) for check in release_checks]
        for flag in (
            "--module-status", "--self-test", "--verify-files",
            "--integration-smoke", "--functional-smoke",
        ):
            with self.subTest(flag=flag):
                self.assertTrue(any(flag in command for command in release_commands))
        self.assertTrue(release_checks)
        self.assertTrue(all(check.command[0] == "{baseline_exe}" for check in release_checks))

    def test_module_level_function_contracts_are_explicitly_collected(self) -> None:
        check = next(item for item in self.matrix.checks if item.id == "native-function-contracts")
        command = " ".join(check.command)
        self.assertIn("harness.engine.function_tests", command)
        self.assertIn("visualization_app.test_native_integrated_app", command)
        self.assertIn("visualization_app.test_mysql_visibility", command)
        self.assertIn("full", check.profiles)
        self.assertIn("release", check.profiles)

    def test_packaged_functional_smoke_has_a_bounded_unattended_timeout(self) -> None:
        check = next(item for item in self.matrix.checks if item.id == "release-functional-smoke")
        self.assertLessEqual(check.timeout_seconds, 600)

    def test_missing_historical_causal_metrics_are_explicit_nonblocking_evidence(self) -> None:
        check = next(item for item in self.matrix.checks if item.id == "causal-history-evidence")
        self.assertFalse(check.blocking)
        self.assertEqual(check.evidence_tier, "unverified")
        self.assertEqual(check.environments, ("local",))

    def test_large_dashboard_assets_are_local_integration_not_ci_inputs(self) -> None:
        dashboard = next(item for item in self.matrix.checks if item.id == "dashboard-runtime-contracts")
        acquisition = next(item for item in self.matrix.checks if item.id == "acquisition-storage-contracts")
        self.assertEqual(dashboard.environments, ("local",))
        self.assertIn("test_app", dashboard.command)
        self.assertNotIn("test_app", acquisition.command)

    def test_field_checks_are_explicit_nonblocking_evidence(self) -> None:
        field_checks = [check for check in self.matrix.checks if check.kind == "field"]
        self.assertGreaterEqual(len(field_checks), 2)
        for check in field_checks:
            with self.subTest(check=check.id):
                self.assertEqual(check.evidence_tier, "field")
                self.assertFalse(check.blocking)
                self.assertEqual(check.command, ())

    def test_usb_dock_checks_cover_automated_and_field_evidence(self) -> None:
        automated = next(item for item in self.matrix.checks if item.id == "usb-dock-topology-contracts")
        field = next(item for item in self.matrix.checks if item.id == "field-usb-dock-topology")
        self.assertTrue(automated.blocking)
        self.assertEqual(automated.evidence_tier, "automated")
        self.assertEqual(automated.environments, ("local", "ci"))
        self.assertIn("USB_TOPOLOGY-001", automated.requirements)
        self.assertIn("harness.engine.usb_dock_contract", automated.command)
        self.assertFalse(field.blocking)
        self.assertEqual(field.evidence_tier, "field")
        self.assertEqual(field.profiles, ("release",))
        self.assertIn("USB_TOPOLOGY-001", field.requirements)


if __name__ == "__main__":
    unittest.main()
