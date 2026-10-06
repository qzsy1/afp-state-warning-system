import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from harness.engine.exe_policy import (
    ExePolicyError,
    classify_changed_files,
    load_exe_rules,
    sha256_file,
    verify_reuse_hash,
)
from harness.engine.models import GateConfigError


class ExePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.real_rules = Path(__file__).parents[2] / "harness" / "config" / "exe-rebuild-rules.json"

    def test_ordinary_external_files_reuse_exe(self) -> None:
        rules = load_exe_rules(self.real_rules)
        decision = classify_changed_files(
            [
                "visualization_app/app.py",
                "visualization_app/static/app.js",
                "docs/quality-gate.md",
                "harness/config/regression-matrix.json",
                "models/predictor.pth",
                ".github/workflows/quality-gate.yml",
            ],
            rules,
        )
        self.assertEqual(decision.decision, "reuse")
        self.assertEqual(len(decision.matches), 6)
        self.assertIn(".github/workflows/quality-gate.yml", decision.changed_files)

    def test_launcher_runtime_and_native_dependencies_require_rebuild(self) -> None:
        rules = load_exe_rules(self.real_rules)
        for path in (
            "modular_runtime/launcher_entry.py",
            "modular_runtime/build_modular_app.ps1",
            "visualization_app/native_driver.dll",
            "visualization_app/requirements.txt",
        ):
            with self.subTest(path=path):
                decision = classify_changed_files([path], rules)
                self.assertEqual(decision.decision, "rebuild_required")
                self.assertEqual(decision.matches[0].path, path)

    def test_rebuild_rule_wins_over_external_overlap_and_mixed_changes(self) -> None:
        rules = load_exe_rules(self.real_rules)
        decision = classify_changed_files(
            ["visualization_app/app.py", "visualization_app/build_desktop_app.ps1"],
            rules,
        )
        self.assertEqual(decision.decision, "rebuild_required")
        self.assertTrue(any(match.category == "rebuild_required" for match in decision.matches))

    def test_unmatched_file_requires_manual_review(self) -> None:
        rules = load_exe_rules(self.real_rules)
        decision = classify_changed_files(["unclassified/runtime.magic"], rules)
        self.assertEqual(decision.decision, "manual_review")
        self.assertIn("unclassified/runtime.magic", decision.reasons[0])

    def test_empty_change_set_is_reuse(self) -> None:
        rules = load_exe_rules(self.real_rules)
        self.assertEqual(classify_changed_files([], rules).decision, "reuse")

    def test_parent_or_absolute_changed_path_is_rejected(self) -> None:
        rules = load_exe_rules(self.real_rules)
        for path in ("../outside.py", "C:/outside.py", "/outside.py"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(GateConfigError, "repository-relative"):
                    classify_changed_files([path], rules)

    def test_invalid_rules_are_rejected(self) -> None:
        path = self.root / "rules.json"
        path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        with self.assertRaises(GateConfigError):
            load_exe_rules(path)

    def test_sha256_file_streams_expected_hash(self) -> None:
        executable = self.root / "stable.exe"
        executable.write_bytes(b"stable executable")
        expected = hashlib.sha256(b"stable executable").hexdigest()
        self.assertEqual(sha256_file(executable), expected)
        evidence = verify_reuse_hash(executable, expected.upper())
        self.assertTrue(evidence.matches)
        self.assertEqual(evidence.current_sha256, expected)

    def test_missing_exe_and_unauthorized_hash_change_fail(self) -> None:
        with self.assertRaisesRegex(ExePolicyError, "does not exist"):
            sha256_file(self.root / "missing.exe")
        executable = self.root / "stable.exe"
        executable.write_bytes(b"changed")
        evidence = verify_reuse_hash(executable, "0" * 64)
        self.assertFalse(evidence.matches)
        self.assertEqual(evidence.baseline_sha256, "0" * 64)

    def test_invalid_baseline_hash_is_rejected(self) -> None:
        executable = self.root / "stable.exe"
        executable.write_bytes(b"stable")
        with self.assertRaisesRegex(ExePolicyError, "SHA-256"):
            verify_reuse_hash(executable, "not-a-hash")


if __name__ == "__main__":
    unittest.main()
