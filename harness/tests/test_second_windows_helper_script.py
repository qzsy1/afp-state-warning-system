from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "pre_sensor" / "second-windows-helper.ps1"


class SecondWindowsHelperScriptTests(unittest.TestCase):
    def test_script_exposes_all_operator_actions_and_fails_closed(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        for action in ("install", "start", "stop", "restart", "check", "export"):
            self.assertIn(f'"{action}"', text)
        self.assertIn('$ErrorActionPreference = "Stop"', text)
        self.assertRegex(text, r"exit\s+\$exitCode")
        self.assertIn("RELEASE-MANIFEST.json", text)
        self.assertIn("SHA256SUMS.txt", text)
        self.assertIn("AFP_Local_Capture_Helper.exe", text)
        self.assertIn("evidence_tier", text)
        self.assertIn("two_machine_environment", text)

    def test_script_has_no_secret_or_automatic_system_policy_mutation(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(text, re.compile(r"(?i)(password|token)\s*=\s*['\"]\S+"))
        forbidden = (
            "Set-NetFirewallProfile",
            "New-NetFirewallRule",
            "powercfg /change",
            "Set-NetAdapterPowerManagement",
            "Set-TimeZone",
        )
        for command in forbidden:
            self.assertNotIn(command, text)
        self.assertIn("review-windows-remediation.ps1", text)

    def test_candidate_assembly_includes_helper_drill_and_second_machine_entrypoint(self) -> None:
        assembly = (ROOT / "modular_runtime" / "assemble_modular_delivery.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("second-windows-helper.ps1", assembly)
        self.assertIn("pre_sensor_drill.py", assembly)


if __name__ == "__main__":
    unittest.main()
