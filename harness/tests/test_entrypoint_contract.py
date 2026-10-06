import re
import subprocess
import unittest
from pathlib import Path


class EntrypointContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).parents[2]
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
        self.assertIn('"harness\\engine\\run.ps1"', self.wrapper)
        self.assertIn("exit $LASTEXITCODE", self.wrapper)
        self.assertNotIn("quality_gate.py", self.wrapper)
        self.assertNotIn("Invoke-Expression", self.wrapper)
        self.assertNotIn("Start-Process", self.wrapper)

    def test_python_discovery_order_is_explicit(self) -> None:
        run = (self.repo / "harness" / "engine" / "run.ps1").read_text(encoding="utf-8")
        positions = [run.index("$env:AFP_PYTHON"), run.index('".venv\\Scripts\\python.exe"'), run.index('"py"'), run.index('"python"')]
        self.assertEqual(positions, sorted(positions))

    def test_wrapper_refreshes_path_after_dependency_installation(self) -> None:
        run = (self.repo / "harness" / "engine" / "run.ps1").read_text(encoding="utf-8")
        self.assertIn("$env:AFP_PYTHON", run)
        self.assertIn("local-settings.json", run)
        self.assertIn(".venv\\Scripts\\python.exe", run)

    def test_workflow_uses_single_windows_harness_entry(self) -> None:
        self.assertRegex(self.workflow, r"(?m)^\s*quality-gate:\s*$")
        self.assertIn("windows-latest", self.workflow)
        self.assertIn("harness/engine/run.ps1", self.workflow)
        self.assertIn("-Profile full", self.workflow)
        self.assertIn("-Environment ci", self.workflow)
        self.assertNotIn("tools/verification/quality_gate.py", self.workflow)

    def test_documentation_defines_branch_and_evidence_boundaries(self) -> None:
        for phrase in (
            "quick", "full", "release", "quality-gate", "分支保护", "禁止合并",
            "现场硬件", "不得", "SHA-256", "harness/reports",
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

    def test_new_entrypoint_can_list_checks_from_any_current_directory(self) -> None:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(self.repo / "harness" / "engine" / "run.ps1"), "-List"],
            cwd=self.repo.parent, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("harness-contracts", completed.stdout)


if __name__ == "__main__":
    unittest.main()
