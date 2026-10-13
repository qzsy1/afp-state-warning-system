from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "pre_sensor" / "SingleRuntimeEvidence.psm1"
PATH_MODULE = ROOT / "pre_sensor" / "SingleRuntimePaths.psm1"
DEPLOYMENT_MODULE = ROOT / "pre_sensor" / "SingleRuntimeDeployment.psm1"
PATH_CONFIG = ROOT / "pre_sensor" / "single-runtime-paths.json"


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


class SingleRuntimeEvidenceTests(unittest.TestCase):
    def test_evidence_copy_hashes_files_and_never_echoes_secret(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            base = Path(temporary)
            workspace = base / "workspace"
            source = base / "source"
            clean_dest = workspace / "AFP-Main-Evidence" / "clean"
            secret_dest = workspace / "AFP-Main-Evidence" / "secret"
            source.mkdir()
            (source / "utf8.txt").write_text("normal evidence", encoding="utf-8")
            (source / "utf16.txt").write_text("正常证据", encoding="utf-16")
            (source / "binary.bin").write_bytes(bytes(range(64)))
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$clean = Copy-AfpEvidenceSafely -Source {_ps_quote(source)} -Destination {_ps_quote(clean_dest)} -Paths $paths
if ($clean.status -ne 'passed' -or -not $clean.allow_source_delete -or $clean.files.Count -ne 3) {{ throw ($clean | ConvertTo-Json -Depth 10) }}
$verified = Test-AfpEvidenceManifest -Destination {_ps_quote(clean_dest)}
if ($verified.status -ne 'passed' -or -not $verified.allow_source_delete) {{ throw ($verified | ConvertTo-Json -Depth 10) }}
Set-Content -LiteralPath {_ps_quote(clean_dest / 'utf8.txt')} -Value 'tampered' -Encoding UTF8
$mismatch = Test-AfpEvidenceManifest -Destination {_ps_quote(clean_dest)}
if ($mismatch.status -ne 'failed' -or $mismatch.allow_source_delete -or $mismatch.issues[0].rule -ne 'sha256_mismatch') {{ throw ($mismatch | ConvertTo-Json -Depth 10) }}
Set-Content -LiteralPath {_ps_quote(source / 'secret.txt')} -Value 'token=super-secret-value' -Encoding UTF8
$blocked = Copy-AfpEvidenceSafely -Source {_ps_quote(source)} -Destination {_ps_quote(secret_dest)} -Paths $paths
$json = $blocked | ConvertTo-Json -Depth 10
if ($blocked.status -ne 'failed' -or $blocked.allow_source_delete) {{ throw $json }}
if ($json -match 'super-secret-value') {{ throw 'secret value leaked in result' }}
if (Test-Path -LiteralPath {_ps_quote(secret_dest)}) {{ throw 'secret evidence was copied' }}
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            manifest = json.loads(
                (clean_dest / "migration-manifest.json").read_text(encoding="utf-8-sig")
            )
            self.assertEqual(len(manifest["files"]), 3)

    def test_cleanup_preview_blocks_protected_glob_active_and_drifted_targets(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            base = Path(temporary)
            workspace = base / "workspace"
            candidate = workspace / "old-candidate"
            candidate.mkdir(parents=True)
            (candidate / "file.bin").write_bytes(b"x")
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(DEPLOYMENT_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$activeRecords = @([pscustomobject]@{{ ProcessId = 7; Name = 'AFP_Integrated_System_Modular.exe'; ExecutablePath = (Join-Path {_ps_quote(candidate)} 'app.exe') }})
$active = Get-AfpRuntimeInventory -ProcessRecords $activeRecords -ListenerRecords @([pscustomobject]@{{ LocalPort = 8770; LocalAddress = '0.0.0.0'; OwningProcess = 7 }}) -StartupRecords @()
$blocked = Get-AfpCleanupPreview -Targets @({_ps_quote(candidate)}, $paths.RuntimeRoot, (Join-Path $paths.WorkspaceRoot '*')) -Paths $paths -Inventory $active
if ($blocked.status -ne 'blocked' -or @($blocked.targets | Where-Object {{ $_.status -eq 'blocked' }}).Count -ne 3) {{ throw ($blocked | ConvertTo-Json -Depth 10) }}
$idle = Get-AfpRuntimeInventory -ProcessRecords @() -ListenerRecords @() -StartupRecords @()
$ready = Get-AfpCleanupPreview -Targets @({_ps_quote(candidate)}) -Paths $paths -Inventory $idle
if ($ready.status -ne 'approval_required') {{ throw ($ready | ConvertTo-Json -Depth 10) }}
try {{
    Remove-AfpApprovedTargets -Preview $ready -PreviewDigest $ready.digest -Paths $paths -CurrentInventory $active
    throw 'inventory drift accepted'
}} catch {{ if ($_.Exception.Message -eq 'inventory drift accepted') {{ throw }} }}
if (-not (Test-Path -LiteralPath {_ps_quote(candidate)})) {{ throw 'target deleted without valid approval' }}
exit 0
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_cleanup_removes_only_approved_exact_target(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            target = workspace / "old-candidate"
            sibling = workspace / "keep-candidate"
            target.mkdir(parents=True)
            sibling.mkdir()
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(DEPLOYMENT_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$inventory = Get-AfpRuntimeInventory -ProcessRecords @() -ListenerRecords @() -StartupRecords @()
$preview = Get-AfpCleanupPreview -Targets @({_ps_quote(target)}) -Paths $paths -Inventory $inventory
$result = Remove-AfpApprovedTargets -Preview $preview -PreviewDigest $preview.digest -Paths $paths -CurrentInventory $inventory
if ($result.status -ne 'passed' -or $result.deleted.Count -ne 1) {{ throw ($result | ConvertTo-Json -Depth 10) }}
"""
            result = _run_ps(script)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse(target.exists())
            self.assertTrue(sibling.exists())

    def test_final_report_validates_statuses_and_core_fields(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            output = workspace / "AFP-Main-Evidence" / "final.json"
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$state = [pscustomobject]@{{ runtime_identity = 'candidate'; runtime_path = $paths.RuntimeRoot; remaining_candidate_count = 0; released_bytes = 123; evidence_directory = $paths.EvidenceRoot; remaining_blockers = @('helper'); pairing = 'blocked'; simulation = 'passed'; physical_sensor = 'physical_sensor_pending' }}
$path = Write-AfpSingleRuntimeReport -State $state -OutputPath {_ps_quote(output)} -Paths $paths
$payload = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
if ($payload.runtime_identity -ne 'candidate' -or $payload.remaining_candidate_count -ne 0 -or $payload.pairing -ne 'blocked') {{ throw ($payload | ConvertTo-Json -Depth 10) }}
"""
            result = _run_ps(script)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
