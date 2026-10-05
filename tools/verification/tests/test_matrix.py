import json
import tempfile
import unittest
from pathlib import Path

from tools.verification.matrix import load_matrix, select_checks
from tools.verification.models import GateConfigError


def valid_matrix() -> dict:
    return {
        "schema_version": 1,
        "profiles": ["quick", "full", "release"],
        "requirements": [
            {
                "id": "REQ-HARNESS",
                "title": "Harness contract",
                "mandatory": True,
                "profiles": ["quick", "full", "release"],
            },
            {
                "id": "REQ-SIM",
                "title": "Simulation",
                "mandatory": True,
                "profiles": ["full", "release"],
            },
        ],
        "checks": [
            {
                "id": "check-harness",
                "title": "Harness tests",
                "kind": "command",
                "command": ["python", "-m", "unittest"],
                "cwd": ".",
                "timeout_seconds": 30,
                "profiles": ["quick", "full", "release"],
                "environments": ["local", "ci"],
                "blocking": True,
                "evidence_tier": "automated",
                "requirements": ["REQ-HARNESS"],
            },
            {
                "id": "check-simulation",
                "title": "Simulation tests",
                "kind": "command",
                "command": ["python", "-m", "unittest", "visualization_app.test_app"],
                "cwd": ".",
                "timeout_seconds": 60,
                "profiles": ["quick", "full", "release"],
                "environments": ["local", "ci"],
                "blocking": True,
                "evidence_tier": "automated",
                "requirements": ["REQ-SIM"],
                "change_patterns": ["visualization_app/**"],
            },
        ],
    }


class MatrixTests(unittest.TestCase):
    def write_matrix(self, payload: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "matrix.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_missing_matrix_is_configuration_error(self) -> None:
        with self.assertRaisesRegex(GateConfigError, "does not exist"):
            load_matrix(Path("missing-matrix.json"))

    def test_malformed_json_is_configuration_error(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "matrix.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(GateConfigError, "valid JSON"):
            load_matrix(path)

    def test_missing_top_level_field_is_rejected(self) -> None:
        payload = valid_matrix()
        del payload["checks"]
        with self.assertRaisesRegex(GateConfigError, "checks"):
            load_matrix(self.write_matrix(payload))

    def test_unknown_fields_are_rejected(self) -> None:
        payload = valid_matrix()
        payload["surprise"] = True
        with self.assertRaisesRegex(GateConfigError, "unknown"):
            load_matrix(self.write_matrix(payload))

    def test_duplicate_requirement_and_check_ids_are_rejected(self) -> None:
        payload = valid_matrix()
        payload["requirements"].append(dict(payload["requirements"][0]))
        with self.assertRaisesRegex(GateConfigError, "duplicate requirement"):
            load_matrix(self.write_matrix(payload))

        payload = valid_matrix()
        payload["checks"].append(dict(payload["checks"][0]))
        with self.assertRaisesRegex(GateConfigError, "duplicate check"):
            load_matrix(self.write_matrix(payload))

    def test_unknown_profile_environment_and_evidence_tier_are_rejected(self) -> None:
        for field, value, message in (
            ("profiles", ["nightly"], "profile"),
            ("environments", ["cloud"], "environment"),
            ("evidence_tier", "guess", "evidence"),
        ):
            with self.subTest(field=field):
                payload = valid_matrix()
                payload["checks"][0][field] = value
                with self.assertRaisesRegex(GateConfigError, message):
                    load_matrix(self.write_matrix(payload))

    def test_unknown_requirement_reference_is_rejected(self) -> None:
        payload = valid_matrix()
        payload["checks"][0]["requirements"] = ["REQ-NOT-DEFINED"]
        with self.assertRaisesRegex(GateConfigError, "unknown requirement"):
            load_matrix(self.write_matrix(payload))

    def test_absolute_or_parent_working_directory_is_rejected(self) -> None:
        for cwd in ("C:/temp", "../outside"):
            with self.subTest(cwd=cwd):
                payload = valid_matrix()
                payload["checks"][0]["cwd"] = cwd
                with self.assertRaisesRegex(GateConfigError, "cwd"):
                    load_matrix(self.write_matrix(payload))

    def test_quick_selects_always_checks_and_matching_affected_checks(self) -> None:
        matrix = load_matrix(self.write_matrix(valid_matrix()))
        selected = select_checks(matrix, "quick", "local", ["visualization_app/app.py"])
        self.assertEqual([check.id for check in selected], ["check-harness", "check-simulation"])

    def test_quick_unknown_change_uses_safe_baseline(self) -> None:
        matrix = load_matrix(self.write_matrix(valid_matrix()))
        selected = select_checks(matrix, "quick", "local", ["unknown/new.file"])
        self.assertEqual([check.id for check in selected], ["check-harness", "check-simulation"])

    def test_full_rejects_mandatory_requirement_without_selected_check(self) -> None:
        payload = valid_matrix()
        payload["checks"][1]["environments"] = ["local"]
        matrix = load_matrix(self.write_matrix(payload))
        with self.assertRaisesRegex(GateConfigError, "REQ-SIM"):
            select_checks(matrix, "full", "ci")

    def test_valid_matrix_builds_immutable_specs(self) -> None:
        matrix = load_matrix(self.write_matrix(valid_matrix()))
        self.assertEqual(matrix.schema_version, 1)
        self.assertEqual(matrix.requirements[0].id, "REQ-HARNESS")
        self.assertEqual(matrix.checks[1].change_patterns, ("visualization_app/**",))
        with self.assertRaises(Exception):
            matrix.checks[0].id = "changed"

    def test_command_allows_repeated_arguments(self) -> None:
        payload = valid_matrix()
        payload["checks"][0]["command"] = ["tool", "--include", "value", "--include", "other"]
        matrix = load_matrix(self.write_matrix(payload))
        self.assertEqual(matrix.checks[0].command.count("--include"), 2)


if __name__ == "__main__":
    unittest.main()
