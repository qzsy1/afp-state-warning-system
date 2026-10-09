from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from harness.engine.diagnostics import issue_from_check, issues_from_report
from harness.engine.error_report import write_error_report, write_machine_logs
from harness.engine.harness_cli import load_local_settings, main as harness_main, save_local_settings
from harness.engine.matrix import load_matrix
from harness.engine.models import CheckSpec, GateConfigError
from harness.engine.preflight import run_preflight
from harness.engine.profiles import load_profiles, select_profile_checks, select_single_check
from harness.engine.reporting import ReportWriteError


class ProfileAndCmdContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).parents[2]
        cls.matrix = load_matrix(cls.repo / "harness/config/regression-matrix.json")
        cls.profiles = load_profiles(cls.repo / "harness/config/profiles.json")

    def test_stable_profile_composition_and_single_selection(self) -> None:
        self.assertEqual(len(self.profiles["quick"]), 9)
        self.assertEqual(len(self.profiles["full"]), 19)
        self.assertEqual(len(self.profiles["release"]), 28)
        self.assertEqual(self.profiles["release"][:19], self.profiles["full"])
        for profile in ("quick", "full", "release"):
            with self.subTest(profile=profile, check="runtime"):
                self.assertIn("modular-runtime-contracts", self.profiles[profile])
            with self.subTest(profile=profile, check="frontend-behavior"):
                self.assertIn("frontend-behavior-contracts", self.profiles[profile])
        for profile in ("full", "release"):
            with self.subTest(profile=profile, check="lan-public"):
                self.assertIn("frontend-public-contracts", self.profiles[profile])
        selected = select_profile_checks(self.matrix, self.profiles, "quick", "local")
        self.assertEqual(tuple(item.id for item in selected), self.profiles["quick"])
        self.assertEqual(select_single_check(self.matrix, "harness-contracts", "local").id, "harness-contracts")
        with self.assertRaises(GateConfigError):
            select_single_check(self.matrix, "unknown", "local")

    def test_list_and_unknown_check_cli_contract(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(harness_main(["list"]), 0)
        self.assertIn("harness-contracts", output.getvalue())
        self.assertEqual(harness_main(["check", "unknown-check"]), 2)

    def test_each_matrix_check_has_ascii_forwarding_cmd(self) -> None:
        checks_dir = self.repo / "harness/checks"
        for check in self.matrix.checks:
            with self.subTest(check=check.id):
                path = checks_dir / f"{check.id}.cmd"
                raw = path.read_bytes()
                raw.decode("ascii")
                text = raw.decode("ascii")
                self.assertIn(f"-Check {check.id}", text)
                self.assertIn("pause", text.lower())
                self.assertIn("AFP Harness launcher started", text)
                self.assertIn("exit /b %HARNESS_RC%", text)
                self.assertNotIn("python -m unittest", text.lower())
                self.assertIn('"%~dp0', text)

    def test_cmd_contract_supports_nonroot_launch_and_ascii_locale(self) -> None:
        entry = self.repo / "harness/quick.cmd"
        raw = entry.read_bytes()
        raw.decode("ascii")
        text = raw.decode("ascii")
        self.assertIn('"%~dp0engine\\run.ps1"', text)
        self.assertIn("chcp 65001", text)
        self.assertNotIn("runas", text.lower())


class PreflightAndSettingsTests(unittest.TestCase):
    def check(self, check_id: str = "probe", cwd: str = ".") -> CheckSpec:
        return CheckSpec(check_id, check_id, "command", ("{python}", "-V"), cwd, 5.0,
                         ("full",), ("local",), True, "automated", ("REQ",))

    def test_preflight_reports_python_venv_workdir_node_and_exe_with_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checks = (
                self.check(cwd="missing"),
                self.check("frontend-behavior-contracts"),
                self.check("frontend-public-contracts"),
                self.check("release-self-test"),
            )
            with patch("harness.engine.preflight._node_version", return_value=(None, "not found")):
                issues = run_preflight(root, checks, baseline_exe=None, python_version=(3, 10))
            joined = " ".join(issue.message + issue.suggestion for issue in issues)
            for phrase in ("Python 3.10", ".venv", "working directory", "Node.js 22", "baseline EXE"):
                self.assertIn(phrase, joined)
            self.assertTrue(all(issue.affected_checks and issue.suggestion for issue in issues))
            node_issue = next(issue for issue in issues if issue.item == "Node.js 22")
            self.assertEqual(
                node_issue.affected_checks,
                ("frontend-behavior-contracts", "frontend-public-contracts"),
            )

    def test_local_settings_only_allow_nonsecret_paths_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / "stable.exe"
            exe.write_bytes(b"stable")
            settings = root / "local-settings.json"
            save_local_settings("python.exe", str(exe), settings)
            payload = load_local_settings(settings)
            self.assertEqual(set(payload), {"python_path", "baseline_exe", "baseline_exe_sha256"})
            settings.write_text(json.dumps({"password": "secret"}), encoding="utf-8")
            with self.assertRaises(GateConfigError):
                load_local_settings(settings)


class DiagnosticsAndHtmlTests(unittest.TestCase):
    def failed(self, output: str, status: str = "failed") -> dict[str, object]:
        return {
            "check_id": "sample", "title": "Sample", "status": status,
            "blocking": True, "exit_code": 1, "error": "", "stderr": output, "stdout": "",
        }

    def test_diagnostic_parses_python_unittest_node_timeout_and_pending(self) -> None:
        python = issue_from_check(self.failed('test_x (pkg.Case) ... FAIL\n  File "C:/repo/a.py", line 42\nAssertionError: no'), Path("a.log"), "sample.cmd")
        self.assertEqual((python.test_name, python.file, python.line, python.category), ("test_x", "C:/repo/a.py", 42, "test_failure"))
        node = issue_from_check(self.failed("at fn (C:/repo/app.js:17:3)"), Path("n.log"), "sample.cmd")
        self.assertEqual((node.file, node.line), ("C:/repo/app.js", 17))
        timeout = issue_from_check(self.failed("", "timed_out"), Path("t.log"), "sample.cmd")
        self.assertEqual(timeout.category, "timeout")
        pending = issue_from_check(self.failed("", "pending_field"), Path("f.log"), "sample.cmd")
        self.assertEqual(pending.category, "field_pending")
        missing = issue_from_check(self.failed("command could not start", "not_started"), Path("m.log"), "sample.cmd")
        self.assertEqual(missing.category, "environment")
        policy = issues_from_report({"checks": [], "unverified_items": ["dirty worktree policy"]}, Path("run"))
        self.assertEqual(policy[0].category, "release_policy")

    def test_machine_logs_and_error_only_html(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "logs/run"
            reports = root / "reports"
            payload = {"profile": "quick", "outcome": "failed", "checks": [self.failed("boom"), {
                "check_id": "passed", "title": "Passed title", "status": "passed", "blocking": True,
                "exit_code": 0, "error": "", "stderr": "", "stdout": "ok", "command": [],
            }], "unverified_items": []}
            write_machine_logs(payload, run)
            issues = issues_from_report(payload, run)
            path = write_error_report(issues, reports, payload)
            html = path.read_text(encoding="utf-8")
            self.assertIn("sample.cmd", html)
            self.assertNotIn("Passed title", html)
            self.assertTrue((run / "sample.log").is_file())
            self.assertIsNone(write_error_report((), reports, payload))
            self.assertFalse((reports / "latest-errors.html").exists())

    def test_html_write_failure_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "file"
            output.write_text("not-directory", encoding="utf-8")
            issue = issue_from_check(self.failed("boom"), Path("x.log"), "sample.cmd")
            with self.assertRaises(ReportWriteError):
                write_error_report((issue,), output, {"profile": "quick", "outcome": "failed"})


if __name__ == "__main__":
    unittest.main()
