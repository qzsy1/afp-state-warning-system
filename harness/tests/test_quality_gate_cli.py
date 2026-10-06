import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from harness.engine.quality_gate import (
    EXIT_CHECK_FAILED,
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RELEASE_POLICY,
    EXIT_REPORT_WRITE,
    main,
)


def matrix_payload(command: list[str], *, timeout: float = 2.0, environments: list[str] | None = None) -> dict:
    return {
        "schema_version": 1,
        "profiles": ["quick", "full", "release"],
        "requirements": [
            {
                "id": "REQ-HARNESS",
                "title": "Harness",
                "mandatory": True,
                "profiles": ["quick", "full", "release"],
            }
        ],
        "checks": [
            {
                "id": "contract",
                "title": "Contract",
                "kind": "command",
                "command": command,
                "cwd": ".",
                "timeout_seconds": timeout,
                "profiles": ["quick", "full", "release"],
                "environments": environments or ["local", "ci"],
                "blocking": True,
                "evidence_tier": "automated",
                "requirements": ["REQ-HARNESS"],
            }
        ],
    }


class QualityGateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "tracked.txt").write_text("baseline", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.repo), "-c", "user.name=Harness Test",
                "-c", "user.email=harness@example.invalid", "commit", "-qm", "baseline",
            ],
            check=True,
        )
        self.matrix = self.root / "matrix.json"
        self.rules = Path(__file__).parents[2] / "harness" / "config" / "exe-rebuild-rules.json"
        self.reports = self.root / "reports"

    def write_matrix(self, payload: dict) -> None:
        self.matrix.write_text(json.dumps(payload), encoding="utf-8")

    def arguments(self, profile: str = "quick") -> list[str]:
        return [
            "--profile", profile,
            "--matrix", str(self.matrix),
            "--exe-rules", str(self.rules),
            "--report-dir", str(self.reports),
        ]

    def report_payload(self) -> dict:
        files = list(self.reports.glob("*.json"))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text(encoding="utf-8"))

    def test_unknown_profile_and_missing_matrix_return_configuration_error(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "pass"]))
        self.assertEqual(main(self.arguments("unknown"), repo_root=self.repo), EXIT_CONFIG)
        self.matrix.unlink()
        self.assertEqual(main(self.arguments(), repo_root=self.repo), EXIT_CONFIG)

    def test_failing_required_check_returns_nonzero_and_writes_report(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "import sys; sys.exit(9)"]))
        self.assertEqual(main(self.arguments(), repo_root=self.repo), EXIT_CHECK_FAILED)
        payload = self.report_payload()
        self.assertEqual(payload["outcome"], "failed")
        self.assertEqual(payload["checks"][0]["exit_code"], 9)

    def test_console_reports_start_and_finish_before_summary(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "pass"]))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(self.arguments(), repo_root=self.repo), EXIT_OK)
        text = output.getvalue()
        self.assertIn("selected 1 check(s)", text)
        self.assertIn("[1/1] START contract", text)
        self.assertIn("[1/1] PASSED contract", text)
        self.assertLess(text.index("START contract"), text.index("PASSED contract"))

    def test_failed_nonblocking_unverified_check_is_listed_as_unverified(self) -> None:
        payload = matrix_payload(["{python}", "-c", "import sys; sys.exit(7)"])
        payload["checks"][0]["blocking"] = False
        payload["checks"][0]["evidence_tier"] = "unverified"
        self.write_matrix(payload)

        self.assertEqual(main(self.arguments(), repo_root=self.repo), EXIT_OK)
        report = self.report_payload()
        self.assertEqual(report["outcome"], "passed")
        self.assertIn("Contract", report["unverified_items"])

    def test_timeout_returns_nonzero_and_writes_report(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "import time; time.sleep(5)"], timeout=0.05))
        self.assertEqual(main(self.arguments(), repo_root=self.repo), EXIT_CHECK_FAILED)
        self.assertEqual(self.report_payload()["checks"][0]["status"], "timed_out")

    def test_incomplete_coverage_is_configuration_error(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "pass"], environments=["local"]))
        args = self.arguments("full") + ["--environment", "ci"]
        self.assertEqual(main(args, repo_root=self.repo), EXIT_CONFIG)

    def test_successful_quick_and_full_runs_write_current_commit_reports(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "print('ok')"]))
        for profile in ("quick", "full"):
            with self.subTest(profile=profile):
                report_dir = self.root / profile
                args = self.arguments(profile)
                args[args.index(str(self.reports))] = str(report_dir)
                self.assertEqual(main(args, repo_root=self.repo), EXIT_OK)
                payload = json.loads(next(report_dir.glob("*.json")).read_text(encoding="utf-8"))
                commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
                self.assertEqual(payload["git"]["commit"], commit)
                self.assertEqual(payload["outcome"], "passed")

    def test_full_report_excludes_release_only_requirements(self) -> None:
        payload = matrix_payload(["{python}", "-c", "pass"])
        payload["requirements"].append(
            {"id": "REQ-RELEASE", "title": "Release only", "mandatory": True, "profiles": ["release"]}
        )
        payload["checks"][0]["requirements"].append("REQ-RELEASE")
        self.write_matrix(payload)
        self.assertEqual(main(self.arguments("full"), repo_root=self.repo), EXIT_OK)
        ids = {item["id"] for item in self.report_payload()["requirements"]}
        self.assertEqual(ids, {"REQ-HARNESS"})

    def test_dirty_release_is_rejected_with_policy_report(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "pass"]))
        (self.repo / "tracked.txt").write_text("dirty", encoding="utf-8")
        args = self.arguments("release") + ["--changed-file", "docs/change.md"]
        self.assertEqual(main(args, repo_root=self.repo), EXIT_RELEASE_POLICY)
        payload = self.report_payload()
        self.assertEqual(payload["outcome"], "release_policy_failed")
        self.assertTrue(payload["git"]["dirty"])

    def test_release_reuse_requires_exe_evidence(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "pass"]))
        args = self.arguments("release") + ["--changed-file", "docs/change.md"]
        self.assertEqual(main(args, repo_root=self.repo), EXIT_RELEASE_POLICY)
        self.assertIn("baseline EXE", " ".join(self.report_payload()["unverified_items"]))

    def test_release_hash_match_passes_and_mismatch_fails(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "pass"]))
        executable = self.root / "stable.exe"
        executable.write_bytes(b"stable")
        digest = hashlib.sha256(b"stable").hexdigest()
        base = self.arguments("release") + [
            "--changed-file", "docs/change.md",
            "--baseline-exe", str(executable),
            "--baseline-exe-sha256", digest,
        ]
        self.assertEqual(main(base, repo_root=self.repo), EXIT_OK)
        other_reports = self.root / "mismatch"
        mismatch = list(base)
        mismatch[mismatch.index(str(self.reports))] = str(other_reports)
        mismatch[mismatch.index(digest)] = "0" * 64
        self.assertEqual(main(mismatch, repo_root=self.repo), EXIT_RELEASE_POLICY)
        payload = json.loads(next(other_reports.glob("*.json")).read_text(encoding="utf-8"))
        self.assertFalse(payload["exe_decision"]["hash_evidence"]["matches"])

    def test_release_checks_execute_the_selected_baseline_executable(self) -> None:
        self.write_matrix(matrix_payload(["{baseline_exe}", "-c", "print('selected-release')"]))
        executable = Path(sys.executable)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        args = self.arguments("release") + [
            "--changed-file", "docs/change.md",
            "--baseline-exe", str(executable),
            "--baseline-exe-sha256", digest,
        ]
        self.assertEqual(main(args, repo_root=self.repo), EXIT_OK)
        check = self.report_payload()["checks"][0]
        self.assertEqual(Path(check["command"][0]).resolve(), executable.resolve())
        self.assertIn("selected-release", check["stdout"])

    def test_manual_review_decision_blocks_release(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "pass"]))
        args = self.arguments("release") + ["--changed-file", "unclassified/file.magic"]
        self.assertEqual(main(args, repo_root=self.repo), EXIT_RELEASE_POLICY)
        self.assertEqual(self.report_payload()["exe_decision"]["decision"], "manual_review")

    def test_report_write_failure_has_distinct_exit_code(self) -> None:
        self.write_matrix(matrix_payload(["{python}", "-c", "pass"]))
        self.reports.write_text("not a directory", encoding="utf-8")
        self.assertEqual(main(self.arguments(), repo_root=self.repo), EXIT_REPORT_WRITE)


if __name__ == "__main__":
    unittest.main()
