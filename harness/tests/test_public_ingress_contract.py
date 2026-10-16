from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from harness.engine.matrix import load_matrix
from harness.engine.public_ingress import adapt_public_ingress_report


ROOT = Path(__file__).resolve().parents[2]


class PublicIngressGateTests(unittest.TestCase):
    def test_report_adapter_preserves_production_decision_fields(self) -> None:
        result = adapt_public_ingress_report(
            {
                "overall_status": "failed",
                "generation": 7,
                "recovery_owner": "tailscale",
                "public_edge": {"ok": False, "code": "unexpected_http_status"},
                "helper": {"ok": True, "code": "helper_ready"},
                "non_sensitive_error": "public_edge_unverified",
            }
        )

        self.assertEqual("failed", result.overall_status)
        self.assertEqual(7, result.generation)
        self.assertFalse(result.public_edge_ok)
        self.assertTrue(result.helper_ok)

    def test_matrix_routes_public_ingress_contract_to_every_profile(self) -> None:
        matrix = load_matrix(ROOT / "harness" / "config" / "regression-matrix.json")
        requirements = {item.id: item for item in matrix.requirements}
        checks = {item.id: item for item in matrix.checks}

        required_ids = {
            "PRE_SENSOR_PUBLIC_INGRESS-001",
            "PRE_SENSOR_INGRESS_GENERATION-001",
            "PRE_SENSOR_INGRESS_CUTOVER-001",
            "PRE_SENSOR_HELPER_GENERATION-001",
            "PRE_SENSOR_MODEL_PROVENANCE-001",
        }
        for requirement_id in required_ids:
            requirement = requirements[requirement_id]
            self.assertEqual(("quick", "full", "release"), requirement.profiles)
            self.assertTrue(requirement.mandatory)
        check = checks["public-ingress-cutover-contracts"]
        self.assertEqual(("quick", "full", "release"), check.profiles)
        self.assertTrue(check.blocking)
        self.assertEqual("automated", check.evidence_tier)
        self.assertTrue(required_ids.issubset(set(check.requirements)))
        command = " ".join(check.command)
        for module in (
            "visualization_app.test_public_ingress_module",
            "visualization_app.test_public_ingress_state",
            "visualization_app.test_helper_relay",
            "visualization_app.test_helper_transport",
            "harness.tests.test_single_runtime_deployment",
            "visualization_app.test_model_selection_source_contract",
        ):
            self.assertIn(module, command)


@unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
class TailscalePublicEdgeBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = ROOT / "visualization_app" / "tailscale_funnel_watchdog.ps1"

    def run_powershell(self, body: str) -> subprocess.CompletedProcess[str]:
        source = str(self.source).replace("'", "''")
        harness = textwrap.dedent(
            f"""
            $source = Get-Content -LiteralPath '{source}' -Raw
            $tokens = $null
            $errors = $null
            $ast = [System.Management.Automation.Language.Parser]::ParseInput(
                $source, [ref]$tokens, [ref]$errors
            )
            if ($errors.Count -ne 0) {{ exit 90 }}
            $functions = $ast.FindAll({{
                param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
            }}, $true)
            Invoke-Expression (($functions | ForEach-Object {{ $_.Extent.Text }}) -join "`n`n")
            {body}
            """
        )
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "-",
            ],
            input=harness,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    def assert_success(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_public_probe_fails_without_public_dns_addresses(self) -> None:
        completed = self.run_powershell(
            r"""
            $script:probeCalls = 0
            function Resolve-AfpPublicEdgeAddresses { param([string]$HostName) return @() }
            $probe = {
                param([Uri]$Uri, [string]$Address)
                $script:probeCalls += 1
                return $true
            }
            if (Test-PublicFunnel 'https://device.example/' $probe) { exit 21 }
            if ($script:probeCalls -ne 0) { exit 22 }
            exit 0
            """
        )
        self.assert_success(completed)

    def test_public_probe_forces_sni_host_to_public_edge_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "app" / "ui" / "app.js"
            asset.parent.mkdir(parents=True)
            asset.write_text("candidate", encoding="utf-8")
            delivery = str(root).replace("'", "''")
            completed = self.run_powershell(
                rf"""
            $DeliveryRoot = '{delivery}'
            function Resolve-AfpPublicEdgeAddresses {{
                param([string]$HostName)
                return @('1.1.1.1')
            }}
            $script:seenHost = ''
            $script:seenAddress = ''
            $script:seenHash = ''
            $probe = {{
                param([Uri]$Uri, [string]$Address, [string]$ExpectedSha256)
                $script:seenHost = $Uri.DnsSafeHost
                $script:seenAddress = $Address
                if ($ExpectedSha256) {{ $script:seenHash = $ExpectedSha256 }}
                return $true
            }}
            if (-not (Test-PublicFunnel 'https://device.example/' $probe)) {{ exit 31 }}
            if ($script:seenHost -ne 'device.example') {{ exit 32 }}
            if ($script:seenAddress -ne '1.1.1.1') {{ exit 33 }}
            if ($script:seenHash -notmatch '^[0-9A-F]{{64}}$') {{ exit 34 }}
            exit 0
            """
            )
        self.assert_success(completed)

    def test_install_does_not_retire_existing_ingress_automatically(self) -> None:
        completed = self.run_powershell(
            r"""
            $script:retireCalls = 0
            function Ensure-Funnel { return $true }
            function Disable-LegacyQuickTunnel { $script:retireCalls += 1 }
            function New-ScheduledTaskAction { return [pscustomobject]@{} }
            function New-ScheduledTaskTrigger { return [pscustomobject]@{} }
            function New-ScheduledTaskSettingsSet { return [pscustomobject]@{} }
            function New-ScheduledTaskPrincipal { return [pscustomobject]@{} }
            function Register-ScheduledTask { }
            $script:PSCommandPath = 'C:\test\tailscale_funnel_watchdog.ps1'
            $TaskName = 'test-task'
            $RunKey = 'HKCU:\test'
            $RunValueName = 'test-run'
            if (-not (Install-Watchdog)) { exit 41 }
            if ($script:retireCalls -ne 0) { exit 42 }
            exit 0
            """
        )
        self.assert_success(completed)


if __name__ == "__main__":
    unittest.main()
