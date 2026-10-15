from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


class PublicTunnelWatchdogContractTests(unittest.TestCase):
    def test_watchdog_checks_origin_and_tunnel_health_before_restarting(self):
        source = Path(__file__).with_name("public_tunnel_watchdog.ps1")
        text = source.read_text(encoding="utf-8-sig")
        self.assertIn("/api/health", text)
        self.assertIn("20241/metrics", text)
        self.assertIn("cloudflared_tunnel_ha_connections", text)
        self.assertIn("Start-Process", text)
        self.assertIn("127.0.0.1:8770", text)

    def test_cloudflare_watchdog_is_non_owner_without_explicit_cutover(self):
        text = self.source_text()
        self.assertIn("PublicIngress.psm1", text)
        self.assertIn('"Cutover"', text)
        self.assertIn("[long]$ExpectedGeneration", text)
        self.assertIn("[switch]$ApproveOwnerCutover", text)
        self.assertIn("Test-WatchdogMutationAllowed", text)
        self.assertIn("non_owner_observe_only", text)
        self.assertIn("Restore-PreviousIngressState", text)
        self.assertIn("Retire-PreviousIngress", text)
        self.assertIn("funnel reset", text)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
    def test_explicit_cutover_retires_only_the_previous_tailscale_provider(self):
        source = str(Path(__file__).with_name("public_tunnel_watchdog.ps1").resolve()).replace("'", "''")
        harness = textwrap.dedent(
            f"""
            $source = Get-Content -LiteralPath '{source}' -Raw
            $tokens = $null; $errors = $null
            $ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
            $definition = $ast.FindAll({{
                param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'Retire-PreviousIngress'
            }}, $true) | Select-Object -First 1
            Invoke-Expression $definition.Extent.Text
            $script:calls = 0
            $retirer = {{ $script:calls += 1; return $true }}
            if (-not (Retire-PreviousIngress 'tailscale' $retirer)) {{ exit 21 }}
            if ($script:calls -ne 1) {{ exit 22 }}
            if (Retire-PreviousIngress 'unknown' $retirer) {{ exit 23 }}
            if ($script:calls -ne 1) {{ exit 24 }}
            exit 0
            """
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "-"],
            input=harness, text=True, capture_output=True, timeout=15, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_layered_status_requires_helper_success(self):
        text = self.source_text()
        for field in (
            "overall_status",
            "origin",
            "control_plane",
            "dns",
            "public_edge",
            "ingress",
            "helper",
            "non_sensitive_error",
        ):
            self.assertIn(field, text)
        self.assertIn("helper_recovery_failed", text)

    def test_watchdog_registers_logon_and_startup_recovery(self):
        source = Path(__file__).with_name("public_tunnel_watchdog.ps1")
        text = source.read_text(encoding="utf-8-sig")
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", text)
        self.assertIn("New-ScheduledTaskTrigger -AtStartup", text)
        self.assertIn("Register-ScheduledTask", text)
        self.assertIn("Unregister-ScheduledTask", text)
        self.assertIn("CurrentVersion\\Run", text)
        self.assertIn("Set-ItemProperty", text)

    def test_watchdog_persists_current_quick_tunnel_url(self):
        source = Path(__file__).with_name("public_tunnel_watchdog.ps1")
        text = source.read_text(encoding="utf-8-sig")
        self.assertIn("trycloudflare", text)
        self.assertIn("quick-tunnel-url.txt", text)
        self.assertIn("-Encoding ASCII", text)

    def test_watchdog_requires_end_to_end_public_health(self):
        source = Path(__file__).with_name("public_tunnel_watchdog.ps1")
        text = source.read_text(encoding="utf-8-sig")
        self.assertIn("function Test-PublicTunnel", text)
        self.assertIn('$PublicUrl.TrimEnd("/") + "/api/health"', text)
        self.assertIn("if (-not (Test-PublicTunnel $url)) { return $false }", text)

    def test_watchdog_keeps_local_helper_running_after_tunnel_recovery(self):
        source = Path(__file__).with_name("public_tunnel_watchdog.ps1")
        text = source.read_text(encoding="utf-8-sig")
        self.assertIn("AFP_Local_Capture_Helper.exe", text)
        self.assertIn("function Ensure-Helper", text)
        self.assertIn('Start-Process -FilePath $HelperExe', text)

    def test_watchdog_starts_origin_without_opening_desktop_window(self):
        source = Path(__file__).with_name("public_tunnel_watchdog.ps1")
        text = source.read_text(encoding="utf-8-sig")
        self.assertIn('-ArgumentList "--server-only"', text)
        self.assertIn('-ArgumentList "--background"', text)

    def test_watchdog_prefers_the_delivery_directory_that_contains_it(self):
        source = Path(__file__).with_name("public_tunnel_watchdog.ps1")
        text = source.read_text(encoding="utf-8-sig")
        self.assertIn("$DeliveryRoot = $PSScriptRoot", text)
        self.assertIn('Test-Path (Join-Path $DeliveryRoot "AFP_Integrated_System_Modular.exe")', text)

    def test_watchdog_has_no_versioned_or_legacy_delivery_fallback(self):
        text = self.source_text()
        self.assertIn('"AFP_Integrated_System_Modular"', text)
        self.assertNotIn("AFP_Integrated_System_Modular_v2.0.4_Agentic", text)
        self.assertNotIn(r"F:\AFP_Integrated_Modular_v2", text)

    def source_text(self):
        return Path(__file__).with_name("public_tunnel_watchdog.ps1").read_text(
            encoding="utf-8-sig"
        )


if __name__ == "__main__":
    unittest.main()
