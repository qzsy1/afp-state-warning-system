from __future__ import annotations

import unittest
from pathlib import Path


class ManualHarnessLayoutContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).parents[2]
        cls.harness = cls.repo / "harness"

    def test_harness_is_the_single_source_of_truth(self) -> None:
        expected = (
            "engine/harness_cli.py",
            "engine/run.ps1",
            "config/regression-matrix.json",
            "config/profiles.json",
            "config/exe-rebuild-rules.json",
            "tests",
            "checks",
        )
        for relative in expected:
            with self.subTest(relative=relative):
                self.assertTrue((self.harness / relative).exists())

    def test_profile_cmd_entrypoints_exist(self) -> None:
        for name in ("quick.cmd", "full.cmd", "release.cmd", "check_all.cmd", "setup.cmd"):
            with self.subTest(name=name):
                self.assertTrue((self.harness / name).is_file())

    def test_legacy_entrypoint_only_forwards_to_harness(self) -> None:
        wrapper = (self.repo / "tools" / "verification" / "run_quality_gate.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("harness\\engine\\run.ps1", wrapper)
        self.assertNotIn("quality_gate.py", wrapper)


if __name__ == "__main__":
    unittest.main()
