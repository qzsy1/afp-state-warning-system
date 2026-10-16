from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest


class TailscaleFunnelWatchdogContractTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(__file__).with_name("tailscale_funnel_watchdog.ps1")

    def test_watchdog_uses_persistent_funnel_for_existing_origin(self):
        self.assertEqual(self.source.read_bytes()[:3], b"\xef\xbb\xbf")
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("tailscale.exe", text)
        self.assertIn("funnel --bg --https=443 --yes http://127.0.0.1:8770", text)
        self.assertIn("127.0.0.1:8770", text)
        self.assertNotIn("trycloudflare", text)

    def test_watchdog_uses_shared_ingress_generation_and_explicit_cutover(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("PublicIngress.psm1", text)
        self.assertIn('"Cutover"', text)
        self.assertIn("[long]$ExpectedGeneration", text)
        self.assertIn("[switch]$ApproveOwnerCutover", text)
        self.assertIn("Test-WatchdogMutationAllowed", text)
        self.assertIn("Publish-AfpPublicIngressState", text)
        self.assertIn("Restore-PreviousIngressState", text)
        self.assertIn("Retire-PreviousIngress", text)
        self.assertIn('@("funnel", "reset")', text)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
    def test_explicit_cutover_retires_only_the_previous_cloudflare_provider(self):
        source = str(self.source.resolve()).replace("'", "''")
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
            if (-not (Retire-PreviousIngress 'cloudflare' $retirer)) {{ exit 21 }}
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

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
    def test_non_owner_and_active_capture_decisions_fail_closed(self):
        source = str(self.source.resolve()).replace("'", "''")
        harness = textwrap.dedent(
            f"""
            $source = Get-Content -LiteralPath '{source}' -Raw
            $tokens = $null; $errors = $null
            $ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
            $definition = $ast.FindAll({{
                param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'Test-WatchdogMutationAllowed'
            }}, $true) | Select-Object -First 1
            if ($null -eq $definition) {{ exit 91 }}
            Invoke-Expression $definition.Extent.Text
            $state = [pscustomobject]@{{ generation = 8; provider = 'cloudflare'; recovery_owner = 'cloudflare' }}
            $ready = [pscustomobject]@{{ allowed = $true }}
            if ((Test-WatchdogMutationAllowed $state 'tailscale' 'Run' 0 $false $ready).allowed) {{ exit 21 }}
            if ((Test-WatchdogMutationAllowed $state 'tailscale' 'Cutover' 8 $false $ready).allowed) {{ exit 22 }}
            $owned = [pscustomobject]@{{ generation = 8; provider = 'tailscale'; recovery_owner = 'tailscale' }}
            if ((Test-WatchdogMutationAllowed $owned 'tailscale' 'Cutover' 8 $false $ready).allowed) {{ exit 25 }}
            if ((Test-WatchdogMutationAllowed $owned 'tailscale' 'Cutover' 7 $true $ready).allowed) {{ exit 26 }}
            $blocked = [pscustomobject]@{{ allowed = $false; active_capture_uuids = @('capture-a') }}
            if ((Test-WatchdogMutationAllowed $state 'tailscale' 'Cutover' 8 $true $blocked).allowed) {{ exit 23 }}
            if (-not (Test-WatchdogMutationAllowed $state 'tailscale' 'Cutover' 8 $true $ready).allowed) {{ exit 24 }}
            exit 0
            """
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "-"],
            input=harness,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_watchdog_derives_and_persists_stable_tsnet_url(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("status --json", text)
        self.assertIn(".ts.net", text)
        self.assertIn("funnel-url.txt", text)
        self.assertIn("-Encoding ASCII", text)

    def test_watchdog_requires_origin_funnel_and_public_health(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("function Test-Origin", text)
        self.assertIn("function Test-Funnel", text)
        self.assertIn("function Test-PublicFunnel", text)
        self.assertIn('/api/health', text)

    def test_watchdog_keeps_app_helper_and_funnel_available_after_logon(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("AFP_Integrated_System_Modular.exe", text)
        self.assertIn("AFP_Local_Capture_Helper.exe", text)
        self.assertIn("function Ensure-Helper", text)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", text)
        self.assertIn("CurrentVersion\\Run", text)

    def test_watchdog_prefers_the_delivery_directory_that_contains_it(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("$DeliveryRoot = $PSScriptRoot", text)
        self.assertIn('Test-Path (Join-Path $DeliveryRoot "AFP_Integrated_System_Modular.exe")', text)

    def test_watchdog_has_no_versioned_or_legacy_delivery_fallback(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn('"AFP_Integrated_System_Modular"', text)
        self.assertNotIn("AFP_Integrated_System_Modular_v2.0.4_Agentic", text)
        self.assertNotIn(r"F:\AFP_Integrated_Modular_v2", text)

    def test_watchdog_starts_origin_without_opening_a_second_desktop_window(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn('-ArgumentList "--server-only"', text)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
    def test_unhealthy_origin_starts_server_when_only_gui_process_exists(self):
        source = str(self.source.resolve()).replace("'", "''")
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

            $script:startCount = 0
            function Test-Origin {{
                return $script:startCount -ge 1
            }}
            function Test-OriginListener {{ return $false }}
            function Get-AppProcess {{
                return [pscustomobject]@{{ ProcessId = 4100; Role = 'desktop_gui' }}
            }}
            function Test-Path {{
                param([string]$LiteralPath, [string]$PathType)
                return $true
            }}
            function Start-Process {{
                param(
                    [string]$FilePath,
                    [object]$ArgumentList,
                    [string]$WorkingDirectory,
                    [string]$WindowStyle
                )
                $script:startCount += 1
            }}
            function Start-Sleep {{ param([int]$Seconds, [int]$Milliseconds) }}
            function Write-FunnelState {{
                param([string]$Code, [string]$Message, [string]$PublicUrl = '')
            }}

            $AppExe = 'C:\\test\\AFP_Integrated_System_Modular.exe'
            $DeliveryRoot = 'C:\\test'
            $OriginLaunchMutexName = 'Local\\AFP_Watchdog_GUI_Recovery_Test'
            $result = Ensure-Origin
            if (-not $result) {{ exit 21 }}
            if ($script:startCount -ne 1) {{ exit 23 }}
            exit 0
            """
        )

        completed = subprocess.run(
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
            timeout=15,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
    def test_origin_listener_probe_reports_only_actual_8770_listener(self):
        source = str(self.source.resolve()).replace("'", "''")
        harness = textwrap.dedent(
            f"""
            $source = Get-Content -LiteralPath '{source}' -Raw
            $tokens = $null
            $errors = $null
            $ast = [System.Management.Automation.Language.Parser]::ParseInput(
                $source, [ref]$tokens, [ref]$errors
            )
            if ($errors.Count -ne 0) {{ exit 90 }}
            $definition = $ast.FindAll({{
                param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'Test-OriginListener'
            }}, $true) | Select-Object -First 1
            if ($null -eq $definition) {{ exit 91 }}
            Invoke-Expression $definition.Extent.Text

            $script:hasListener = $false
            function Get-NetTCPConnection {{
                param([string]$State, [int]$LocalPort, [string]$ErrorAction)
                if ($script:hasListener -and $State -eq 'Listen' -and $LocalPort -eq 8770) {{
                    return [pscustomobject]@{{ LocalPort = 8770; State = 'Listen' }}
                }}
            }}

            if (Test-OriginListener) {{ exit 31 }}
            $script:hasListener = $true
            if (-not (Test-OriginListener)) {{ exit 32 }}
            exit 0
            """
        )

        completed = subprocess.run(
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
            timeout=15,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
    def test_concurrent_origin_recovery_starts_only_one_server(self):
        source = str(self.source.resolve()).replace("'", "''")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gate = str(root / "gate").replace("'", "''")
            health = str(root / "health").replace("'", "''")
            starts = str(root / "starts.log").replace("'", "''")
            mutex_name = f"Local\\AFP_Watchdog_Test_{root.name}"
            harness = textwrap.dedent(
                f"""
                $source = Get-Content -LiteralPath '{source}' -Raw
                $tokens = $null
                $errors = $null
                $ast = [System.Management.Automation.Language.Parser]::ParseInput(
                    $source, [ref]$tokens, [ref]$errors
                )
                if ($errors.Count -ne 0) {{ exit 90 }}
                $definition = $ast.FindAll({{
                    param($node)
                    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                    $node.Name -eq 'Ensure-Origin'
                }}, $true) | Select-Object -First 1
                if ($null -eq $definition) {{ exit 91 }}
                Invoke-Expression $definition.Extent.Text

                $OriginLaunchMutexName = '{mutex_name}'
                $AppExe = 'C:\\test\\AFP_Integrated_System_Modular.exe'
                $DeliveryRoot = 'C:\\test'
                function Test-Origin {{
                    if ([System.IO.File]::Exists('{health}')) {{ return $true }}
                    [System.Threading.Thread]::Sleep(150)
                    return $false
                }}
                function Test-OriginListener {{ return $false }}
                function Test-Path {{
                    param([string]$LiteralPath, [string]$PathType)
                    return $true
                }}
                function Start-Process {{
                    param(
                        [string]$FilePath,
                        [object]$ArgumentList,
                        [string]$WorkingDirectory,
                        [string]$WindowStyle
                    )
                    [System.IO.File]::AppendAllText('{starts}', "start`n")
                    [System.Threading.Thread]::Sleep(200)
                    [System.IO.File]::WriteAllText('{health}', 'ok')
                }}
                function Start-Sleep {{
                    param([int]$Seconds, [int]$Milliseconds)
                    [System.Threading.Thread]::Sleep(10)
                }}
                function Write-FunnelState {{
                    param([string]$Code, [string]$Message, [string]$PublicUrl = '')
                }}

                while (-not [System.IO.File]::Exists('{gate}')) {{
                    [System.Threading.Thread]::Sleep(10)
                }}
                if (Ensure-Origin) {{ exit 0 }}
                exit 21
                """
            )
            processes = [
                subprocess.Popen(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        harness,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            time.sleep(0.2)
            Path(gate).write_text("go", encoding="ascii")
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                outputs.append((process.returncode, stdout, stderr))

            self.assertEqual(outputs[0][0], 0, outputs[0][1] + outputs[0][2])
            self.assertEqual(outputs[1][0], 0, outputs[1][1] + outputs[1][2])
            start_lines = (
                Path(starts).read_text(encoding="utf-8").splitlines()
                if Path(starts).is_file()
                else []
            )
            self.assertEqual(start_lines, ["start"])

    def test_watchdog_records_actionable_authentication_state(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("funnel-status.json", text)
        self.assertIn("tailscale_login_required", text)
        self.assertIn("funnel_approval_required", text)
        self.assertNotIn('" + $result.Output', text)

    def test_first_funnel_approval_wait_is_bounded(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("WaitForExit", text)
        self.assertIn("Stop-Process -Id $process.Id -Force", text)
        self.assertIn("Stop-CandidateIngress | Out-Null", text)

    def test_public_health_uses_shared_forced_edge_probe(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("Resolve-AfpPublicEdgeAddresses", text)
        self.assertIn("Invoke-AfpForcedPublicProbe", text)
        self.assertNotIn("function Test-PublicIPv4", text)
        self.assertNotIn("function Invoke-ForcedPublicProbe", text)

    def test_helper_build_updates_watchdog_in_existing_delivery(self):
        build = Path(__file__).with_name("build_local_capture_helper.ps1")
        text = build.read_text(encoding="utf-8-sig")
        self.assertIn("tailscale_funnel_watchdog.ps1", text)
        self.assertIn("Copy-Item", text)

    def test_helper_build_accepts_an_exact_virtualenv_interpreter(self):
        build = Path(__file__).with_name("build_local_capture_helper.ps1")
        text = build.read_text(encoding="utf-8-sig")
        self.assertIn("Test-Path -LiteralPath $PythonExecutable -PathType Leaf", text)
        self.assertIn("$pythonPrefix = @()", text)
        self.assertIn("& $pythonCommand @pythonPrefix -m PyInstaller", text)
        self.assertIn('[string]$DeliveryRoot = ""', text)
        self.assertIn('[string]$OutputDirectory = ""', text)
        self.assertIn("afp-local-helper-build-", text)

    def test_helper_build_refreshes_delivery_integrity_manifest(self):
        build = Path(__file__).with_name("build_local_capture_helper.ps1")
        text = build.read_text(encoding="utf-8-sig")
        self.assertIn("Get-FileHash -Algorithm SHA256", text)
        self.assertIn('Join-Path $DeliveryRoot "SHA256SUMS.txt"', text)
        self.assertIn('$_ .Name -ne "SHA256SUMS.txt"'.replace("$_ ", "$_"), text)


if __name__ == "__main__":
    unittest.main()
