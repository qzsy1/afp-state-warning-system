from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "pre_sensor" / "SingleRuntimeRelease.psm1"
PATH_MODULE = ROOT / "pre_sensor" / "SingleRuntimePaths.psm1"
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


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_release_fixture(root: Path) -> None:
    (root / "local_helper").mkdir(parents=True)
    (root / "config/pre_sensor").mkdir(parents=True)
    (root / "app/ui").mkdir(parents=True)
    (root / "native_dll").mkdir(parents=True)
    files = {
        "AFP_Integrated_System_Modular.exe": b"launcher",
        "local_helper/AFP_Local_Capture_Helper.exe": b"helper",
        "config/pre_sensor/schema.json": b"schema",
        "config/pre_sensor/local_direct.json": b"local",
        "config/pre_sensor/remote_helper.json": b"remote",
        "app/ui/app.js": b"asset",
        "native_dll/BsvUvcNative.dll": b"bsv",
        "native_dll/libusb-1.0.dll": b"usb",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    identity = {
        "application_version": "2.0.5-rc.1",
        "helper_version": "2.0.5-rc.1",
        "launcher_version": "2.0.5-rc.1",
        "release_revision": "test",
    }
    manifest = {
        "schema_version": 1,
        "identity": identity,
        "contracts": {
            "helper_protocol_version": 2,
            "unified_frame_contract": "unified_frame_v1",
            "configuration_schema_version": 1,
            "readiness_schema_version": 1,
        },
        "candidate": {
            "candidate_id": "2.0.5-rc.1-test",
            "source_revision": "abc123",
            "created_at": "2026-10-13T00:00:00Z",
        },
        "static_assets": {
            "required": ["app/ui/app.js"],
            "sha256": {"app/ui/app.js": _hash(root / "app/ui/app.js")},
        },
        "configuration_templates": {
            "required": [
                "config/pre_sensor/schema.json",
                "config/pre_sensor/local_direct.json",
                "config/pre_sensor/remote_helper.json",
            ],
            "sha256": {
                relative: _hash(root / relative)
                for relative in (
                    "config/pre_sensor/schema.json",
                    "config/pre_sensor/local_direct.json",
                    "config/pre_sensor/remote_helper.json",
                )
            },
        },
        "managed_files": {
            "manifest": "SHA256SUMS.txt",
            "digest_algorithm": "sha256",
        },
        "runtime_requirements": {
            "required_executables": [
                {
                    "id": "local_capture_helper",
                    "path": "local_helper/AFP_Local_Capture_Helper.exe",
                    "sha256": _hash(
                        root / "local_helper/AFP_Local_Capture_Helper.exe"
                    ),
                }
            ],
            "native_components": [
                {
                    "id": "bsv_uvc",
                    "path": "native_dll/BsvUvcNative.dll",
                    "sha256": _hash(root / "native_dll/BsvUvcNative.dll"),
                },
                {
                    "id": "libusb",
                    "path": "native_dll/libusb-1.0.dll",
                    "sha256": _hash(root / "native_dll/libusb-1.0.dll"),
                },
            ],
            "vendor_sdks": [{"id": "bsv"}],
            "drivers": [{"id": "uvc"}],
        },
    }
    (root / "RELEASE-MANIFEST.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (root / "VERSION.json").write_text(
        json.dumps(
            {
                "application_version": "2.0.5-rc.1",
                "launcher_version": "2.0.5-rc.1",
            }
        ),
        encoding="utf-8",
    )
    managed = [
        relative
        for relative in files
        if relative != "AFP_Integrated_System_Modular.exe"
    ] + ["AFP_Integrated_System_Modular.exe", "RELEASE-MANIFEST.json", "VERSION.json"]
    lines = [f"{_hash(root / relative)} *{relative}" for relative in managed]
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


class SingleRuntimeReleaseTests(unittest.TestCase):
    def test_release_gate_accepts_matching_fixture_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            candidate = Path(temporary) / "candidate"
            candidate.mkdir()
            _write_release_fixture(candidate)
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$runner = {{ param($Exe, $Argument) [pscustomobject]@{{ ExitCode = 0; Output = 'ok' }} }}
$good = Test-AfpReleaseGate -CandidatePath {_ps_quote(candidate)} -ExpectedCandidateId '2.0.5-rc.1-test' -ExecutableCheckRunner $runner
if ($good.status -ne 'passed' -or $good.issues.Count -ne 0 -or $good.checks.Count -ne 3) {{ throw ($good | ConvertTo-Json -Depth 8) }}
Set-Content -LiteralPath {_ps_quote(candidate / 'app/ui/app.js')} -Value 'tampered' -Encoding UTF8
$bad = Test-AfpReleaseGate -CandidatePath {_ps_quote(candidate)} -ExpectedCandidateId '2.0.5-rc.1-test' -ExecutableCheckRunner $runner
if ($bad.status -ne 'failed' -or -not ($bad.issues -match 'hash mismatch')) {{ throw ($bad | ConvertTo-Json -Depth 8) }}
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_preconditions_block_missing_git_lfs_and_build_inputs(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            runtime = workspace / "AFP_Integrated_System_Modular"
            runtime.mkdir(parents=True)
            marker = runtime / "marker.txt"
            marker.write_text("unchanged", encoding="utf-8")
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$result = Test-AfpBuildReadiness -Paths $paths -RepoRoot (Join-Path $paths.WorkspaceRoot 'missing-repo') -GitRef 'missing-ref' -RequiredInputs @((Join-Path $paths.WorkspaceRoot 'missing-input')) -RequiredBytes 1
if ($result.status -ne 'blocked') {{ throw ($result | ConvertTo-Json -Depth 8) }}
if ($result.issues.Count -lt 2) {{ throw 'expected multiple readiness issues' }}
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_reference_resolution_and_peak_capacity_use_real_release_size(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            source = workspace / "AFP-Readiness-Repo"
            historical = source / "delivery" / "AFP_Integrated_System_Modular_previous"
            historical.mkdir(parents=True)
            (historical / "first.bin").write_bytes(b"1234")
            (historical / "second.bin").write_bytes(b"123456")
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$reference = Resolve-AfpReferenceRelease -Paths $paths
if ($reference -ne {_ps_quote(historical)}) {{ throw "unexpected reference: $reference" }}
$result = Test-AfpBuildReadiness -Paths $paths -RepoRoot {_ps_quote(ROOT)} -GitRef 'HEAD' -RequiredInputs @({_ps_quote(ROOT / 'pre_sensor' / 'release-contract.json')}) -ReferenceRelease $reference -FreeSpaceProvider {{ param($WorkspaceRoot) 9 }}
if ($result.status -ne 'blocked') {{ throw ($result | ConvertTo-Json -Depth 8) }}
if ($result.required_bytes -ne 10 -or $result.free_bytes -ne 9) {{ throw ($result | ConvertTo-Json -Depth 8) }}
if (-not ($result.issues -contains 'insufficient staging capacity')) {{ throw ($result | ConvertTo-Json -Depth 8) }}
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_staged_release_binds_build_script_parameters_by_name(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            source = workspace / "AFP-Readiness-Repo" / "pre_sensor"
            source.mkdir(parents=True)
            reference = workspace / "reference"
            reference.mkdir()
            fake = source / "build-candidate.ps1"
            fake.write_text(
                "param([string]$ReferenceRelease,[string]$WorkspaceRoot,[string]$SourceRoot)\n"
                "if ($ReferenceRelease -ne $env:AFP_TEST_REFERENCE) { throw 'reference binding failed' }\n"
                "if ($WorkspaceRoot -ne $env:AFP_TEST_WORKSPACE) { throw 'workspace binding failed' }\n"
                "[pscustomobject]@{ staging_path = (Join-Path $WorkspaceRoot '.afp-staging\\deployment-test'); candidate_id = 'candidate-test' } | ConvertTo-Json\n",
                encoding="utf-8-sig",
            )
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$env:AFP_TEST_REFERENCE = {_ps_quote(reference)}
$env:AFP_TEST_WORKSPACE = {_ps_quote(workspace)}
try {{
    $result = New-AfpStagedRelease -Paths $paths -ReferenceRelease {_ps_quote(reference)}
    if ($result.candidate_id -ne 'candidate-test') {{ throw ($result | ConvertTo-Json -Depth 8) }}
}} finally {{
    Remove-Item Env:AFP_TEST_REFERENCE, Env:AFP_TEST_WORKSPACE -ErrorAction SilentlyContinue
}}
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_staging_cleanup_removes_only_deployment_child(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            staging = workspace / ".afp-staging"
            target = staging / "deployment-test"
            sibling = staging / "deployment-keep"
            target.mkdir(parents=True)
            sibling.mkdir()
            (target / "file.txt").write_text("delete", encoding="utf-8")
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
Remove-AfpDeploymentStaging -StagingPath {_ps_quote(target)} -Paths $paths
"""
            result = _run_ps(script)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse(target.exists())
            self.assertTrue(sibling.exists())


if __name__ == "__main__":
    unittest.main()
