from __future__ import annotations

import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

from harness.engine.matrix import load_matrix
from harness.engine.public_ingress import evaluate_public_ingress


ROOT = Path(__file__).resolve().parents[2]


class PublicIngressGateTests(unittest.TestCase):
    def test_local_and_control_plane_health_cannot_replace_public_edge(self) -> None:
        result = evaluate_public_ingress(
            origin_ready=True,
            control_plane_ready=True,
            public_edge_ready=False,
            current_url="https://server.example/",
            current_owner_confirmed=True,
            client_ready=True,
        )

        self.assertEqual("failed", result.status)
        self.assertEqual("public_edge_unverified", result.reason)
        self.assertFalse(result.public_ready)
        self.assertFalse(result.may_retire_previous)
        self.assertFalse(result.step3_allowed)

    def test_previous_ingress_can_retire_only_after_owner_is_confirmed(self) -> None:
        unowned = evaluate_public_ingress(
            origin_ready=True,
            control_plane_ready=True,
            public_edge_ready=True,
            current_url="https://server.example/",
            current_owner_confirmed=False,
        )
        owned = evaluate_public_ingress(
            origin_ready=True,
            control_plane_ready=True,
            public_edge_ready=True,
            current_url="https://server.example/",
            current_owner_confirmed=True,
        )

        self.assertTrue(unowned.public_ready)
        self.assertFalse(unowned.may_retire_previous)
        self.assertTrue(owned.may_retire_previous)

    def test_url_change_invalidates_client_readiness_and_step3(self) -> None:
        result = evaluate_public_ingress(
            origin_ready=True,
            control_plane_ready=True,
            public_edge_ready=True,
            previous_url="https://old.example/",
            current_url="https://new.example/",
            current_owner_confirmed=True,
            client_ready=True,
        )

        self.assertTrue(result.client_readiness_stale)
        self.assertFalse(result.step3_allowed)

    def test_matrix_routes_public_ingress_contract_to_every_profile(self) -> None:
        matrix = load_matrix(ROOT / "harness" / "config" / "regression-matrix.json")
        requirements = {item.id: item for item in matrix.requirements}
        checks = {item.id: item for item in matrix.checks}

        requirement = requirements["PRE_SENSOR_PUBLIC_INGRESS-001"]
        check = checks["public-ingress-cutover-contracts"]
        self.assertEqual(("quick", "full", "release"), requirement.profiles)
        self.assertTrue(requirement.mandatory)
        self.assertEqual(("quick", "full", "release"), check.profiles)
        self.assertTrue(check.blocking)
        self.assertEqual("automated", check.evidence_tier)
        self.assertIn(requirement.id, check.requirements)


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
        completed = self.run_powershell(
            r"""
            function Resolve-AfpPublicEdgeAddresses {
                param([string]$HostName)
                return @('1.1.1.1')
            }
            $script:seenHost = ''
            $script:seenAddress = ''
            $probe = {
                param([Uri]$Uri, [string]$Address)
                $script:seenHost = $Uri.DnsSafeHost
                $script:seenAddress = $Address
                return $true
            }
            if (-not (Test-PublicFunnel 'https://device.example/' $probe)) { exit 31 }
            if ($script:seenHost -ne 'device.example') { exit 32 }
            if ($script:seenAddress -ne '1.1.1.1') { exit 33 }
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
