import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.verification.evidence import collect_git_evidence
from tools.verification.models import RequirementSpec
from tools.verification.reporting import (
    GateReport,
    ReportWriteError,
    aggregate_requirements,
    redact_text,
    write_reports,
)
from tools.verification.runner import CheckResult


def result(check_id: str, status: str, *, blocking: bool = True, requirement: str = "REQ-1") -> CheckResult:
    return CheckResult(
        check_id=check_id,
        title=check_id,
        status=status,
        started_at="2026-10-02T00:00:00+00:00",
        ended_at="2026-10-02T00:00:01+00:00",
        duration_seconds=1.0,
        command=(sys.executable, "-V"),
        cwd=".",
        exit_code=0 if status == "passed" else 1,
        stdout="output",
        stderr="",
        error="",
        blocking=blocking,
        evidence_tier="automated",
        requirements=(requirement,),
    )


class ReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def init_repo(self) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "tracked.txt").write_text("baseline", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Harness Test",
                "-c",
                "user.email=harness@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ],
            check=True,
        )
        return repo

    def test_collect_git_evidence_records_clean_and_dirty_status(self) -> None:
        repo = self.init_repo()
        clean = collect_git_evidence(repo)
        self.assertFalse(clean.dirty)
        self.assertEqual(len(clean.commit), 40)
        self.assertTrue(clean.branch)
        (repo / "tracked.txt").write_text("changed", encoding="utf-8")
        dirty = collect_git_evidence(repo)
        self.assertTrue(dirty.dirty)
        self.assertIn("tracked.txt", dirty.status)
        self.assertTrue(dirty.status.startswith(" M tracked.txt"), dirty.status)

    def test_aggregate_requirements_fails_on_blocking_failure(self) -> None:
        requirements = (RequirementSpec("REQ-1", "Required", True, ("full",)),)
        aggregated = aggregate_requirements(requirements, (result("ok", "passed"), result("bad", "failed")))
        self.assertEqual(aggregated[0].status, "failed")
        self.assertEqual(aggregated[0].failed_check_ids, ("bad",))

    def test_aggregate_requirements_keeps_optional_failure_as_warning(self) -> None:
        requirements = (RequirementSpec("REQ-1", "Required", True, ("full",)),)
        aggregated = aggregate_requirements(
            requirements,
            (result("ok", "passed"), result("optional", "failed", blocking=False)),
        )
        self.assertEqual(aggregated[0].status, "passed_with_warnings")

    def test_aggregate_requirements_preserves_pending_field_and_unverified(self) -> None:
        requirements = (
            RequirementSpec("REQ-1", "Field", True, ("release",)),
            RequirementSpec("REQ-2", "Missing", True, ("release",)),
        )
        pending = result("field", "pending_field")
        aggregated = aggregate_requirements(requirements, (pending,))
        self.assertEqual([item.status for item in aggregated], ["pending_field", "unverified"])

    def test_redact_text_masks_environment_values_and_key_value_forms(self) -> None:
        environment = {
            "PUBLIC_VALUE": "visible",
            "SERVICE_API_KEY": "super-secret-value",
            "MYSQL_PASSWORD": "database-password",
        }
        source = "key=super-secret-value password: plain-text database-password visible"
        redacted = redact_text(source, environment)
        self.assertNotIn("super-secret-value", redacted)
        self.assertNotIn("database-password", redacted)
        self.assertNotIn("plain-text", redacted)
        self.assertIn("visible", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redact_text_masks_quoted_json_secret_assignments(self) -> None:
        cases = {
            '{"password":"plain-text","api_key": "sk-secret","ok":true}':
                '{"password":"[REDACTED]","api_key": "[REDACTED]","ok":true}',
            r'{"token":"a\"b","user":"alice"}':
                r'{"token":"[REDACTED]","user":"alice"}',
            "{'credential':'two words','user':'alice'}":
                "{'credential':'[REDACTED]','user':'alice'}",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(redact_text(source, {}), expected)

    def make_report(self, repo: Path, secret: str = "") -> GateReport:
        git = collect_git_evidence(repo)
        requirements = (RequirementSpec("REQ-1", "Required", True, ("full",)),)
        checks = (result("check-1", "passed"),)
        return GateReport(
            schema_version=1,
            profile="full",
            environment="local",
            started_at="2026-10-02T00:00:00+00:00",
            ended_at="2026-10-02T00:00:01+00:00",
            git=git,
            interpreter=sys.executable,
            checks=checks[:-1] + (CheckResult(**{**checks[0].__dict__, "stdout": secret}),),
            requirements=aggregate_requirements(requirements, checks),
            exe_decision=None,
            unverified_items=("SMRF field hardware",),
            outcome="passed",
        )

    def test_write_reports_is_atomic_reproducible_and_redacted(self) -> None:
        repo = self.init_repo()
        report = self.make_report(repo, secret="token=report-secret")
        json_path, markdown_path = write_reports(
            report,
            self.root / "results",
            {"MODEL_TOKEN": "report-secret"},
        )
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertEqual(payload["git"]["commit"], report.git.commit)
        self.assertEqual(payload["summary"]["passed"], 1)
        self.assertEqual(payload["summary"]["skipped"], 0)
        self.assertIn(report.git.commit, markdown)
        self.assertIn("SMRF field hardware", markdown)
        self.assertNotIn("report-secret", json_path.read_text(encoding="utf-8"))
        self.assertNotIn("report-secret", markdown)
        self.assertFalse(list((self.root / "results").glob("*.tmp")))

    def test_report_write_failure_is_explicit(self) -> None:
        repo = self.init_repo()
        output = self.root / "not-a-directory"
        output.write_text("file", encoding="utf-8")
        with self.assertRaises(ReportWriteError):
            write_reports(self.make_report(repo), output, os.environ)

    def test_two_runs_in_the_same_second_do_not_overwrite_history(self) -> None:
        repo = self.init_repo()
        first = self.make_report(repo)
        second = GateReport(**{
            **first.__dict__,
            "started_at": "2026-10-02T00:00:00.999999+00:00",
        })
        first_path, _ = write_reports(first, self.root / "results", os.environ)
        second_path, _ = write_reports(second, self.root / "results", os.environ)
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.exists())
        self.assertTrue(second_path.exists())


if __name__ == "__main__":
    unittest.main()
