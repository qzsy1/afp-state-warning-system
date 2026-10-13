from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "pre_sensor" / "SingleRuntimeDeployment.psm1"
PATH_MODULE = ROOT / "pre_sensor" / "SingleRuntimePaths.psm1"
PATH_CONFIG = ROOT / "pre_sensor" / "single-runtime-paths.json"
HELPER_INSTALLER = ROOT / "visualization_app" / "install_local_helper_autostart.ps1"


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


class SingleRuntimeStartupContractTests(unittest.TestCase):
    def test_helper_installer_targets_fixed_runtime_without_old_fallback(self) -> None:
        source = HELPER_INSTALLER.read_text(encoding="utf-8-sig")
        self.assertIn('"AFP_Integrated_System_Modular\\local_helper"', source)
        self.assertNotIn("AFP_Integrated_System_Modular_v2.0.4_Agentic", source)
        self.assertNotIn(r"F:\AFP_Integrated_Modular_v2", source)

    def test_startup_preview_has_old_new_and_recovery_values(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$records = @([pscustomobject]@{{ Name = 'AFP_Tailscale_Funnel_Watchdog'; Source = 'HKCU Run'; Value = 'powershell -File D:\old\tailscale_funnel_watchdog.ps1 -Mode Run' }})
$inventory = Get-AfpRuntimeInventory -ProcessRecords @() -ListenerRecords @() -StartupRecords $records
$preview = Get-AfpStartupRepairPreview -Inventory $inventory -Paths $paths -StartupRecords $records
if ($preview.status -ne 'approval_required' -or $preview.changes.Count -ne 1) {{ throw ($preview | ConvertTo-Json -Depth 10) }}
$change = $preview.changes[0]
if (-not $change.old_value -or -not $change.new_value -or $change.recovery_value -ne $change.old_value) {{ throw ($preview | ConvertTo-Json -Depth 10) }}
if ($change.new_value -notmatch [regex]::Escape($paths.RuntimeRoot)) {{ throw 'fixed runtime missing from preview' }}
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_unapproved_startup_repair_does_not_apply(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            marker = Path(temporary) / "mutated.txt"
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$records = @([pscustomobject]@{{ Name = 'AFP_Tailscale_Funnel_Watchdog'; Source = 'HKCU Run'; Value = 'powershell -File D:\old\tailscale_funnel_watchdog.ps1 -Mode Run' }})
$inventory = Get-AfpRuntimeInventory -ProcessRecords @() -ListenerRecords @() -StartupRecords $records
$preview = Get-AfpStartupRepairPreview -Inventory $inventory -Paths $paths -StartupRecords $records
$runner = {{ param($Change) Set-Content -LiteralPath {_ps_quote(marker)} -Value 'changed' }}
try {{
    Set-AfpApprovedStartupTargets -Preview $preview -PreviewDigest 'wrong' -ApplyRunner $runner
    throw 'unapproved repair accepted'
}} catch {{
    if ($_.Exception.Message -eq 'unapproved repair accepted') {{ throw }}
}}
exit 0
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
