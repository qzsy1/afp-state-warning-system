from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "pre_sensor" / "SingleRuntimeDeployment.psm1"
PATH_MODULE = ROOT / "pre_sensor" / "SingleRuntimePaths.psm1"
PATH_CONFIG = ROOT / "pre_sensor" / "single-runtime-paths.json"
CLI = ROOT / "pre_sensor" / "manage-single-runtime-delivery.ps1"


def _ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_ps(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class SingleRuntimeDeploymentTests(unittest.TestCase):
    def test_inventory_is_read_only_sanitized_and_approval_is_complete(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            marker = Path(temporary) / "marker.txt"
            marker.write_text("unchanged", encoding="utf-8")
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$processes = @([pscustomobject]@{{ ProcessId = 42; Name = 'AFP_Integrated_System_Modular.exe'; ExecutablePath = 'D:\old\AFP_Integrated_System_Modular.exe' }})
$listeners = @([pscustomobject]@{{ LocalPort = 8770; LocalAddress = '0.0.0.0'; OwningProcess = 42 }})
$startup = @([pscustomobject]@{{ Name = 'AFP_Tailscale_Funnel_Watchdog'; Value = 'powershell -Token topsecret D:\old\watchdog.ps1'; Source = 'HKCU Run' }})
$inventory = Get-AfpRuntimeInventory -ProcessRecords $processes -ListenerRecords $listeners -StartupRecords $startup -PublicUrl 'https://example.test/path?token=secret' -FunnelOrigin 'http://127.0.0.1:8770'
$json = $inventory | ConvertTo-Json -Depth 10
if ($json -match 'topsecret|token=secret') {{ throw 'inventory leaked a secret' }}
if ($inventory.processes[0].pid -ne 42 -or $inventory.listeners[0].port -ne 8770) {{ throw $json }}
$summary = New-AfpApprovalSummary -Inventory $inventory -Operation Switchover
$summaryJson = $summary | ConvertTo-Json -Depth 10
foreach ($required in @('current_listener_pid','current_listener_path','impact','expected_interruption','rollback_plan','inventory_digest')) {{
    if ($summaryJson -notmatch $required) {{ throw "approval field missing: $required" }}
}}
"""
            result = _run_ps(script)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_inventory_digest_rejects_changed_pid_path_or_startup(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
function New-Inventory($PidValue, $PathValue, $StartupHash) {{
    Get-AfpRuntimeInventory `
        -ProcessRecords @([pscustomobject]@{{ ProcessId = $PidValue; Name = 'AFP_Integrated_System_Modular.exe'; ExecutablePath = $PathValue }}) `
        -ListenerRecords @([pscustomobject]@{{ LocalPort = 8770; LocalAddress = '0.0.0.0'; OwningProcess = $PidValue }}) `
        -StartupRecords @([pscustomobject]@{{ Name = 'AFP'; TargetPath = $PathValue; ValueHash = $StartupHash; Source = 'Run' }})
}}
$baseline = New-Inventory 42 'D:\old\app.exe' 'aaa'
if (-not (Test-AfpInventoryUnchanged -Expected $baseline -Current (New-Inventory 42 'D:\old\app.exe' 'aaa'))) {{ throw 'same inventory rejected' }}
if (Test-AfpInventoryUnchanged -Expected $baseline -Current (New-Inventory 43 'D:\old\app.exe' 'aaa')) {{ throw 'PID drift accepted' }}
if (Test-AfpInventoryUnchanged -Expected $baseline -Current (New-Inventory 42 'D:\other\app.exe' 'aaa')) {{ throw 'path drift accepted' }}
if (Test-AfpInventoryUnchanged -Expected $baseline -Current (New-Inventory 42 'D:\old\app.exe' 'bbb')) {{ throw 'startup drift accepted' }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_promotion_is_whole_directory_and_can_restore(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            runtime = workspace / "AFP_Integrated_System_Modular"
            staging = workspace / ".afp-staging" / "deployment-test"
            runtime.mkdir(parents=True)
            staging.mkdir(parents=True)
            (runtime / "old.txt").write_text("old", encoding="utf-8")
            (staging / "new.txt").write_text("new", encoding="utf-8")
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$inventory = Get-AfpRuntimeInventory -ProcessRecords @() -ListenerRecords @() -StartupRecords @()
$result = Invoke-AfpRuntimePromotion -StagingPath {_ps_quote(staging)} -Paths $paths -ApprovedInventoryDigest $inventory.digest -CurrentInventory $inventory
if (-not (Test-Path -LiteralPath (Join-Path $paths.RuntimeRoot 'new.txt'))) {{ throw 'new runtime missing' }}
if (Test-Path -LiteralPath (Join-Path $paths.RuntimeRoot 'old.txt')) {{ throw 'mixed runtime contents' }}
if (-not (Test-Path -LiteralPath (Join-Path $result.swap_path 'old.txt'))) {{ throw 'rollback material missing' }}
Restore-AfpRuntimePromotion -Promotion $result -Paths $paths
if (-not (Test-Path -LiteralPath (Join-Path $paths.RuntimeRoot 'old.txt'))) {{ throw 'old runtime not restored' }}
if (Test-Path -LiteralPath (Join-Path $paths.RuntimeRoot 'new.txt')) {{ throw 'new runtime remained after restore' }}
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_service_gate_requires_every_named_probe(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$pass = {{ param($Name, $Target) [pscustomobject]@{{ passed = $true; detail = 'ok' }} }}
$good = Test-AfpRuntimeServices -RuntimeRoot 'F:\runtime' -PublicUrl 'https://example.test/' -LanIPv4 '192.0.2.10' -ExpectedCandidateId 'candidate' -FunnelOrigin 'http://127.0.0.1:8770' -ProbeRunner $pass
if ($good.status -ne 'passed' -or $good.checks.Count -ne 7) {{ throw ($good | ConvertTo-Json -Depth 8) }}
$fail = {{ param($Name, $Target) [pscustomobject]@{{ passed = ($Name -ne 'public_https'); detail = 'probe' }} }}
$bad = Test-AfpRuntimeServices -RuntimeRoot 'F:\runtime' -PublicUrl 'https://example.test/' -LanIPv4 '192.0.2.10' -ExpectedCandidateId 'candidate' -FunnelOrigin 'http://127.0.0.1:8770' -ProbeRunner $fail
if ($bad.status -ne 'failed' -or $bad.issues -notcontains 'service probe failed: public_https') {{ throw ($bad | ConvertTo-Json -Depth 8) }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_rollback_plan_reuses_staging_and_fixed_runtime(self) -> None:
        source = CLI.read_text(encoding="utf-8-sig")
        self.assertIn('"Rollback"', source)
        self.assertIn("New-AfpRollbackStage", source)
        self.assertIn("Test-AfpReleaseGate", source)
        self.assertIn("New-AfpApprovalSummary", source)
        self.assertIn("Test-AfpRuntimeServices", source)
        self.assertNotIn("previous-runtime", source.lower())
        release = (ROOT / "pre_sensor" / "SingleRuntimeRelease.psm1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("worktree add --detach", release)
        self.assertIn("worktree remove --force", release)
        self.assertIn("New-AfpStagedRelease", release)


if __name__ == "__main__":
    unittest.main()
