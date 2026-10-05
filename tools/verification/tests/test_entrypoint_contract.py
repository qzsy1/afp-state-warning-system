import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


class EntrypointContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).parents[3]
        cls.wrapper = (cls.repo / "tools" / "verification" / "run_quality_gate.ps1").read_text(encoding="utf-8")
        cls.workflow = (cls.repo / ".github" / "workflows" / "quality-gate.yml").read_text(encoding="utf-8")
        cls.docs = (cls.repo / "docs" / "quality-gate.md").read_text(encoding="utf-8")

    def test_wrapper_exposes_and_forwards_the_cli_contract(self) -> None:
        for name in (
            "Profile", "Environment", "Matrix", "ExeRules", "BaseRef", "ChangedFile",
            "BaselineExe", "BaselineExeSha256", "ReportDir",
        ):
            with self.subTest(name=name):
                self.assertRegex(self.wrapper, rf"\${name}\b")
        self.assertIn("quality_gate.py", self.wrapper)
        self.assertIn("exit $exitCode", self.wrapper)
        self.assertNotIn("Invoke-Expression", self.wrapper)
        self.assertNotIn("Start-Process", self.wrapper)

    def test_python_discovery_order_is_explicit(self) -> None:
        positions = [
            self.wrapper.index('Name = "AFP_PYTHON"'),
            self.wrapper.index('Name = ".venv"'),
            self.wrapper.index('Name = "py -3.11"'),
            self.wrapper.index('Name = "py -3"'),
            self.wrapper.index('Name = "python"'),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_wrapper_refreshes_path_after_dependency_installation(self) -> None:
        self.assertIn("GetEnvironmentVariable(\"Path\", \"Machine\")", self.wrapper)
        self.assertIn("GetEnvironmentVariable(\"Path\", \"User\")", self.wrapper)
        self.assertIn("$env:Path", self.wrapper)

    def test_workflow_uses_single_windows_harness_entry(self) -> None:
        self.assertRegex(self.workflow, r"(?m)^\s*quality-gate:\s*$")
        self.assertIn("windows-latest", self.workflow)
        self.assertIn("tools/verification/run_quality_gate.ps1", self.workflow)
        self.assertIn("-Profile full", self.workflow)
        self.assertIn("-Environment ci", self.workflow)
        self.assertNotIn("tools/verification/quality_gate.py", self.workflow)

    def test_documentation_defines_branch_and_evidence_boundaries(self) -> None:
        for phrase in (
            "quick", "full", "release", "quality-gate", "分支保护", "禁止合并",
            "现场硬件", "不得", "SHA-256", "verification/results",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.docs)

    def test_entry_files_do_not_embed_secret_values(self) -> None:
        combined = "\n".join((self.wrapper, self.workflow, self.docs))
        forbidden = [
            r"sk-[A-Za-z0-9]{12,}",
            r"(?i)password\s*[:=]\s*['\"][^'\"]+",
            r"(?i)api[_-]?key\s*[:=]\s*['\"][^'\"]+",
        ]
        for pattern in forbidden:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, combined))

    def test_wrapper_falls_back_when_requested_python_version_is_absent(self) -> None:
        payload = {
            "schema_version": 1,
            "profiles": ["quick", "full", "release"],
            "requirements": [
                {"id": "HARNESS-001", "title": "Harness", "mandatory": True, "profiles": ["quick"]}
            ],
            "checks": [
                {
                    "id": "probe", "title": "Probe", "kind": "command",
                    "command": ["{python}", "-c", "print('wrapper-ok')"], "cwd": ".",
                    "timeout_seconds": 30, "profiles": ["quick"],
                    "environments": ["local"], "blocking": True,
                    "evidence_tier": "automated", "requirements": ["HARNESS-001"]
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix = root / "matrix.json"
            reports = root / "reports"
            matrix.write_text(json.dumps(payload), encoding="utf-8")
            completed = subprocess.run(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(self.repo / "tools" / "verification" / "run_quality_gate.ps1"),
                    "-Profile", "quick", "-Matrix", str(matrix), "-ReportDir", str(reports),
                ],
                cwd=self.repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertEqual(len(list(reports.glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
