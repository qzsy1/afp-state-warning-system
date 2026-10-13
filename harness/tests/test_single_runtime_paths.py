from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "pre_sensor" / "single-runtime-paths.json"
MODULE = ROOT / "pre_sensor" / "SingleRuntimePaths.psm1"


def _ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(script: str) -> subprocess.CompletedProcess[str]:
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


class SingleRuntimePathTests(unittest.TestCase):
    def test_production_paths_are_fixed_under_f_workspace(self) -> None:
        contract = json.loads(CONFIG.read_text(encoding="utf-8"))

        self.assertEqual(contract["workspace_root"], r"F:\new_AFP_exam")
        self.assertEqual(
            contract["source_root"], r"F:\new_AFP_exam\AFP-Readiness-Repo"
        )
        self.assertEqual(
            contract["runtime_root"],
            r"F:\new_AFP_exam\AFP_Integrated_System_Modular",
        )
        self.assertEqual(
            contract["evidence_root"], r"F:\new_AFP_exam\AFP-Main-Evidence"
        )
        self.assertEqual(
            contract["staging_root"], r"F:\new_AFP_exam\.afp-staging"
        )

    def test_rejects_outside_parent_glob_and_reparse_targets(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            sandbox = Path(temporary)
            workspace = sandbox / "workspace"
            outside = sandbox / "outside"
            workspace.mkdir()
            outside.mkdir()
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
@($paths.SourceRoot, $paths.RuntimeRoot, $paths.EvidenceRoot, $paths.StagingRoot) | ForEach-Object {{
    New-Item -ItemType Directory -Path $_ -Force | Out-Null
}}

function Assert-Rejected([string]$Target, [string]$Kind = 'Cleanup') {{
    try {{
        Assert-AfpMutableTarget -Path $Target -Paths $paths -Kind $Kind | Out-Null
        throw "unsafe target accepted: $Target"
    }} catch {{
        if ($_.Exception.Message -like 'unsafe target accepted:*') {{ throw }}
    }}
}}

Assert-Rejected $paths.WorkspaceRoot
Assert-Rejected (Split-Path -Parent $paths.WorkspaceRoot)
Assert-Rejected $paths.SourceRoot
Assert-Rejected ($paths.RuntimeRoot.ToUpperInvariant() + '\')
Assert-Rejected $paths.EvidenceRoot
Assert-Rejected $paths.StagingRoot
Assert-Rejected (Join-Path $paths.WorkspaceRoot '..\outside')
Assert-Rejected (Join-Path $paths.StagingRoot '*') 'Staging'

$allowed = Join-Path $paths.StagingRoot 'deployment-123'
New-Item -ItemType Directory -Path $allowed -Force | Out-Null
$resolved = Assert-AfpMutableTarget -Path $allowed -Paths $paths -Kind 'Staging'
if ($resolved -ne [IO.Path]::GetFullPath($allowed).TrimEnd('\')) {{
    throw "unexpected safe path: $resolved"
}}

$junction = Join-Path $paths.StagingRoot 'outside-link'
New-Item -ItemType Junction -Path $junction -Target {_ps_quote(outside)} | Out-Null
Assert-Rejected $junction 'Staging'
"""
            result = _run_powershell(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
