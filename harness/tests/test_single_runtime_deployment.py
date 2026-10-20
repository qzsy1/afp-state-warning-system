from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "pre_sensor" / "SingleRuntimeDeployment.psm1"
PATH_MODULE = ROOT / "pre_sensor" / "SingleRuntimePaths.psm1"
PATH_CONFIG = ROOT / "pre_sensor" / "single-runtime-paths.json"
CLI = ROOT / "pre_sensor" / "manage-single-runtime-delivery.ps1"
LEGACY_RESERVATION = ROOT / "pre_sensor" / "legacy-cutover-reservation.py"


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
    def test_legacy_reservation_is_persisted_without_plaintext_token(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            runtime = Path(temporary) / "runtime-root"
            state_root = runtime / "runtime"
            state_root.mkdir(parents=True)
            (state_root / "public-ingress-state.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "generation": 1,
                        "public_url": "https://example.test",
                        "provider": "external",
                        "recovery_owner": "external",
                        "candidate_id": "candidate-old",
                        "status": "degraded",
                        "published_at": "2026-10-16T00:00:00+00:00",
                        "layers": {"helper": {"ok": False, "code": "pending"}},
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(LEGACY_RESERVATION),
                    "--runtime-root",
                    str(runtime),
                    "--candidate-id",
                    "candidate-old",
                    "--ttl-seconds",
                    "60",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["allowed"])
            self.assertTrue(payload["reservation_id"])
            persisted = (state_root / "public-ingress-cutover.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(payload["reservation_id"], persisted)

    def test_service_gate_rejects_split_dns_success_without_forced_edge(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$ordinary = {{ param($Name, $Target) [pscustomobject]@{{ passed = $true; detail = 'ordinary route' }} }}
$forced = {{ param($Name, $Target) [pscustomobject]@{{ passed = $false; detail = 'forced edge unavailable' }} }}
$result = Test-AfpRuntimeServices -RuntimeRoot 'D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' -PublicUrl 'https://example.test/' -LanIPv4 '192.0.2.10' -ExpectedCandidateId 'candidate' -FunnelOrigin 'http://127.0.0.1:8770' -ProbeRunner $ordinary -PublicEdgeProbeRunner $forced
if ($result.status -ne 'failed') {{ throw ($result | ConvertTo-Json -Depth 8) }}
if ($result.issues -notcontains 'service probe failed: public_https') {{ throw ($result | ConvertTo-Json -Depth 8) }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_service_gate_rejects_redirect_and_non_200(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$ordinary = {{ param($Name, $Target) [pscustomobject]@{{ passed = $true; detail = 'ok' }} }}
foreach ($status in @(301, 204)) {{
    $forced = {{ param($Name, $Target, $ExpectedHash) [pscustomobject]@{{ passed = $false; detail = "unexpected_http_status:$status" }} }}
    $result = Test-AfpRuntimeServices -RuntimeRoot 'D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' -PublicUrl 'https://example.test/' -LanIPv4 '192.0.2.10' -ExpectedCandidateId 'candidate' -FunnelOrigin 'http://127.0.0.1:8770' -ProbeRunner $ordinary -PublicEdgeProbeRunner $forced
    if ($result.status -ne 'failed') {{ throw ($result | ConvertTo-Json -Depth 8) }}
}}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_active_capture_blocks_promotion_before_process_stop(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$script:stops = 0
$inventory = [pscustomobject]@{{ digest = 'approved'; processes = @() }}
$result = Invoke-AfpVerifiedRuntimePromotion -StagingPath 'D:\AFP_Runtime\.afp-staging\tc64-tests\stage' -Paths ([pscustomobject]@{{ RuntimeRoot='D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' }}) -ApprovedInventoryDigest 'approved' -CurrentInventory $inventory `
    -CutoverReadinessRunner {{ [pscustomobject]@{{ allowed = $false; active_capture_uuids = @('capture-a') }} }} `
    -StopRunner {{ $script:stops++ }} -StartRunner {{ }} -ServiceProbeRunner {{ }} -IngressPublisher {{ }} -CompletionRunner {{ }} -RestoreRunner {{ }}
if ($result.status -ne 'blocked' -or $script:stops -ne 0) {{ throw ($result | ConvertTo-Json -Depth 8) }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_legacy_bootstrap_runs_only_for_missing_reservation_endpoint(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$events = [Collections.Generic.List[string]]::new()
$inventory = [pscustomobject]@{{ digest = 'approved'; processes = @() }}
$result = Invoke-AfpVerifiedRuntimePromotion -StagingPath 'D:\AFP_Runtime\.afp-staging\tc64-tests\stage' -Paths ([pscustomobject]@{{ RuntimeRoot='D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' }}) -ApprovedInventoryDigest 'approved' -CurrentInventory $inventory `
    -CutoverReadinessRunner {{ [pscustomobject]@{{ allowed=$false; error='cutover_readiness_unavailable' }} }} `
    -LegacyBootstrapRunner {{ $events.Add('legacy'); [pscustomobject]@{{ allowed=$false; error='legacy_blocked' }} }} `
    -StopRunner {{ $events.Add('stop') }}
if ($result.status -ne 'blocked' -or ($events -join ',') -ne 'legacy') {{ throw ($result | ConvertTo-Json -Depth 8) }}

$events.Clear()
$result = Invoke-AfpVerifiedRuntimePromotion -StagingPath 'D:\AFP_Runtime\.afp-staging\tc64-tests\stage' -Paths ([pscustomobject]@{{ RuntimeRoot='D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' }}) -ApprovedInventoryDigest 'approved' -CurrentInventory $inventory `
    -CutoverReadinessRunner {{ [pscustomobject]@{{ allowed=$false; error='active_capture' }} }} `
    -LegacyBootstrapRunner {{ $events.Add('legacy'); [pscustomobject]@{{ allowed=$true }} }} `
    -StopRunner {{ $events.Add('stop') }}
if ($result.status -ne 'blocked' -or $events.Count -ne 0) {{ throw ($result | ConvertTo-Json -Depth 8) }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_explicit_pending_helper_keeps_verified_runtime_and_skips_completion(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$events = [Collections.Generic.List[string]]::new()
$inventory = [pscustomobject]@{{ digest = 'approved'; processes = @() }}
$result = Invoke-AfpVerifiedRuntimePromotion -StagingPath 'D:\AFP_Runtime\.afp-staging\tc64-tests\stage' -Paths ([pscustomobject]@{{ RuntimeRoot='D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' }}) -ApprovedInventoryDigest 'approved' -CurrentInventory $inventory -AllowPendingHelper `
    -CutoverReadinessRunner {{ [pscustomobject]@{{ allowed=$true; reservation_id='lease' }} }} `
    -CutoverReleaseRunner {{ param($ReservationId) $events.Add('release'); [pscustomobject]@{{ released=$true }} }} `
    -StopRunner {{ $events.Add('stop') }} `
    -PromotionRunner {{ $events.Add('promote'); [pscustomobject]@{{ status='switched'; swap_path='D:\AFP_Runtime\.afp-staging\tc64-tests\swap' }} }} `
    -StartRunner {{ $events.Add('start') }} `
    -ServiceProbeRunner {{ $events.Add('edge'); [pscustomobject]@{{ status='passed' }} }} `
    -IngressPublisher {{ $events.Add('ingress'); [pscustomobject]@{{ generation=4 }} }} `
    -HelperReadinessRunner {{ param($Ingress) $events.Add('helper'); [pscustomobject]@{{ ready=$false; generation=$Ingress.generation }} }} `
    -IngressCommitter {{ $events.Add('commit') }} `
    -CompletionRunner {{ $events.Add('complete') }} `
    -RestoreRunner {{ $events.Add('restore') }}
if ($result.status -ne 'awaiting_helper' -or -not $result.completion_required) {{ throw ($result | ConvertTo-Json -Depth 8) }}
if (($events -join ',') -ne 'stop,promote,start,edge,ingress,helper,release') {{ throw ($events -join ',') }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_promotion_does_not_complete_before_edge_and_state_gates(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$events = [Collections.Generic.List[string]]::new()
$inventory = [pscustomobject]@{{ digest = 'approved'; processes = @() }}
$result = Invoke-AfpVerifiedRuntimePromotion -StagingPath 'D:\AFP_Runtime\.afp-staging\tc64-tests\stage' -Paths ([pscustomobject]@{{ RuntimeRoot='D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' }}) -ApprovedInventoryDigest 'approved' -CurrentInventory $inventory `
    -CutoverReadinessRunner {{ [pscustomobject]@{{ allowed = $true }} }} `
    -StopRunner {{ $events.Add('stop') }} `
    -PromotionRunner {{ $events.Add('promote'); [pscustomobject]@{{ status='switched' }} }} `
    -StartRunner {{ $events.Add('start') }} `
    -ServiceProbeRunner {{ $events.Add('edge'); [pscustomobject]@{{ status='passed' }} }} `
    -IngressPublisher {{ $events.Add('ingress'); [pscustomobject]@{{ generation=4 }} }} `
    -HelperReadinessRunner {{ param($Ingress) $events.Add('helper'); [pscustomobject]@{{ ready=$true; generation=$Ingress.generation }} }} `
    -IngressCommitter {{ param($Ingress, $Helper) $events.Add('commit'); $Ingress }} `
    -CompletionRunner {{ $events.Add('complete') }} `
    -RestoreRunner {{ $events.Add('restore') }}
if ($result.status -ne 'completed') {{ throw ($result | ConvertTo-Json -Depth 8) }}
if (($events -join ',') -ne 'stop,promote,start,edge,ingress,helper,commit,complete') {{ throw ($events -join ',') }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_each_exception_point_restores_runtime_and_ingress_snapshot(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
foreach ($failure in @('promote','start','edge','ingress','helper','complete')) {{
    $script:restores = 0
    $inventory = [pscustomobject]@{{ digest = 'approved'; processes = @() }}
    $throwAt = {{ param($name) if ($failure -eq $name) {{ throw "failure:$name" }} }}
    $result = Invoke-AfpVerifiedRuntimePromotion -StagingPath 'D:\AFP_Runtime\.afp-staging\tc64-tests\stage' -Paths ([pscustomobject]@{{ RuntimeRoot='D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' }}) -ApprovedInventoryDigest 'approved' -CurrentInventory $inventory `
        -CutoverReadinessRunner {{ [pscustomobject]@{{ allowed = $true }} }} `
        -StopRunner {{ }} `
        -PromotionRunner {{ & $throwAt 'promote'; [pscustomobject]@{{ status='switched' }} }} `
        -StartRunner {{ & $throwAt 'start' }} `
        -ServiceProbeRunner {{ & $throwAt 'edge'; [pscustomobject]@{{ status='passed' }} }} `
        -IngressPublisher {{ & $throwAt 'ingress'; [pscustomobject]@{{ generation=4 }} }} `
        -HelperReadinessRunner {{ param($Ingress) & $throwAt 'helper'; [pscustomobject]@{{ ready=$true; generation=$Ingress.generation }} }} `
        -CompletionRunner {{ & $throwAt 'complete' }} `
        -RestoreRunner {{ param($Promotion, $IngressSnapshot) $script:restores++; [pscustomobject]@{{status='passed'}} }}
    if ($result.status -ne 'rolled_back' -or $script:restores -ne 1) {{ throw ($result | ConvertTo-Json -Depth 8) }}
}}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_rollback_failure_is_terminal_and_preserves_both_evidence_paths(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$inventory = [pscustomobject]@{{ digest = 'approved'; processes = @() }}
$result = Invoke-AfpVerifiedRuntimePromotion -StagingPath 'D:\AFP_Runtime\.afp-staging\tc64-tests\stage' -Paths ([pscustomobject]@{{ RuntimeRoot='D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' }}) -ApprovedInventoryDigest 'approved' -CurrentInventory $inventory `
    -FailureEvidencePath 'D:\AFP_Runtime\.afp-staging\tc64-tests\evidence\promotion.json' -RollbackEvidencePath 'D:\AFP_Runtime\.afp-staging\tc64-tests\evidence\rollback.json' `
    -CutoverReadinessRunner {{ [pscustomobject]@{{ allowed = $true }} }} -StopRunner {{ }} `
    -PromotionRunner {{ throw 'promotion failed' }} -StartRunner {{ }} -ServiceProbeRunner {{ }} -IngressPublisher {{ }} -CompletionRunner {{ }} `
    -RestoreRunner {{ throw 'rollback failed' }}
if ($result.status -ne 'rollback_failed' -or $result.failure_evidence_path -ne 'D:\AFP_Runtime\.afp-staging\tc64-tests\evidence\promotion.json' -or $result.rollback_evidence_path -ne 'D:\AFP_Runtime\.afp-staging\tc64-tests\evidence\rollback.json') {{ throw ($result | ConvertTo-Json -Depth 8) }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

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
$scriptStartup = Get-AfpRuntimeInventory -ProcessRecords @() -ListenerRecords @() -StartupRecords @([pscustomobject]@{{ Name = 'AFP Watchdog'; Value = '"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -File "D:\old\tailscale_funnel_watchdog.ps1"'; Source = 'HKCU Run' }})
if ($scriptStartup.startup[0].target_path -ne 'D:\old\tailscale_funnel_watchdog.ps1') {{ throw ($scriptStartup | ConvertTo-Json -Depth 10) }}
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

    def test_switchover_stops_all_fixed_runtime_processes_and_starts_server_first(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$runtimeExe = 'D:\AFP_Runtime\.afp-staging\tc64-tests\runtime\AFP_Integrated_System_Modular.exe'
$inventory = Get-AfpRuntimeInventory `
    -ProcessRecords @(
        [pscustomobject]@{{ ProcessId = 41; Name = 'AFP_Integrated_System_Modular.exe'; ExecutablePath = $runtimeExe; CommandLine = ('"' + $runtimeExe + '"') }},
        [pscustomobject]@{{ ProcessId = 42; Name = 'AFP_Integrated_System_Modular.exe'; ExecutablePath = $runtimeExe; CommandLine = ('"' + $runtimeExe + '" --server-only') }},
        [pscustomobject]@{{ ProcessId = 99; Name = 'AFP_Integrated_System_Modular.exe'; ExecutablePath = 'D:\other\AFP_Integrated_System_Modular.exe'; CommandLine = 'D:\other\AFP_Integrated_System_Modular.exe --server-only' }}
    ) `
    -ListenerRecords @([pscustomobject]@{{ LocalPort = 8770; LocalAddress = '0.0.0.0'; OwningProcess = 42 }}) `
    -StartupRecords @()
$stopped = [Collections.Generic.List[int]]::new()
$plan = Stop-AfpManagedRuntimeProcesses -Inventory $inventory -RuntimeExecutable $runtimeExe -StopRunner {{ param($PidValue) $stopped.Add([int]$PidValue) }}
if (($stopped -join ',') -ne '41,42') {{ throw "unexpected stop set: $($stopped -join ',')" }}
$started = [Collections.Generic.List[string]]::new()
$count = Start-AfpManagedRuntimeProcesses -Processes $plan -RuntimeExecutable $runtimeExe -WorkingDirectory 'D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' -StartRunner {{ param($Exe, $Arguments, $WorkingDirectory) $started.Add([string]$Arguments) }}
if ($count -ne 2 -or ($started -join ',') -ne '--server-only,') {{ throw "unexpected restart roles: $($started -join ',')" }}
"""
        result = _run_ps(script)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_migration_inventory_includes_directories_disk_and_rollback_inputs(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            legacy = workspace / "legacy-candidate"
            legacy.mkdir(parents=True)
            (legacy / "payload.bin").write_bytes(b"12345")
            (legacy / "RELEASE-MANIFEST.json").write_text(
                json.dumps({"candidate": {"candidate_id": "candidate-old"}}),
                encoding="utf-8",
            )
            marker = legacy / "unchanged.txt"
            marker.write_text("unchanged", encoding="utf-8")
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$runtime = Get-AfpRuntimeInventory `
    -ProcessRecords @([pscustomobject]@{{ ProcessId = 42; Name = 'AFP_Integrated_System_Modular.exe'; ExecutablePath = (Join-Path {_ps_quote(legacy)} 'AFP_Integrated_System_Modular.exe') }}) `
    -ListenerRecords @([pscustomobject]@{{ LocalPort = 8770; LocalAddress = '0.0.0.0'; OwningProcess = 42 }}) `
    -StartupRecords @([pscustomobject]@{{ Name = 'AFP Startup'; TargetPath = (Join-Path {_ps_quote(legacy)} 'watchdog.ps1'); ValueHash = 'abc'; Source = 'Run' }})
$migration = Get-AfpMigrationInventory -Paths $paths -RuntimeInventory $runtime -DirectoryTargets @([pscustomobject]@{{ path = {_ps_quote(legacy)}; kind = 'legacy_candidate' }}) -FreeSpaceProvider {{ param($WorkspaceRoot) 123456 }} -RollbackProbe {{ param($SourceRoot) [pscustomobject]@{{ source_revision = 'abc123'; working_tree_clean = $true; git_lfs_available = $true; required_inputs_available = $true }} }}
$entry = $migration.directories[0]
if ($entry.candidate_id -ne 'candidate-old' -or $entry.size_bytes -lt 5) {{ throw ($migration | ConvertTo-Json -Depth 12) }}
if ($entry.active_process_pids[0] -ne 42 -or $entry.startup_references[0] -ne 'AFP Startup') {{ throw ($migration | ConvertTo-Json -Depth 12) }}
if ($migration.disk.free_bytes -ne 123456 -or -not $migration.rollback.ready) {{ throw ($migration | ConvertTo-Json -Depth 12) }}
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_promotion_is_whole_directory_and_can_restore(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            runtime = workspace / "AFP_Integrated_System_Modular"
            staging = workspace / ".afp-staging" / "deployment-test"
            runtime.mkdir(parents=True)
            staging.mkdir(parents=True)
            (runtime / "old.txt").write_text("old", encoding="utf-8")
            (staging / "new.txt").write_text("new", encoding="utf-8")
            state_dir = runtime / "runtime"
            state_dir.mkdir()
            (state_dir / "public-ingress-state.json").write_text(
                '{"generation":7}', encoding="utf-8"
            )
            (state_dir / "helper_registry.json").write_text(
                '{"helpers":{}}', encoding="utf-8"
            )
            (state_dir / "interface-plans.json").write_text(
                '{"active":"new_collection_v13_0","plans":{}}', encoding="utf-8"
            )
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$inventory = Get-AfpRuntimeInventory -ProcessRecords @() -ListenerRecords @() -StartupRecords @()
$result = Invoke-AfpRuntimePromotion -StagingPath {_ps_quote(staging)} -Paths $paths -ApprovedInventoryDigest $inventory.digest -CurrentInventory $inventory
if (-not (Test-Path -LiteralPath (Join-Path $paths.RuntimeRoot 'new.txt'))) {{ throw 'new runtime missing' }}
if (Test-Path -LiteralPath (Join-Path $paths.RuntimeRoot 'old.txt')) {{ throw 'mixed runtime contents' }}
if ((Get-Content -LiteralPath (Join-Path $paths.RuntimeRoot 'runtime\public-ingress-state.json') -Raw) -notmatch '7') {{ throw 'ingress generation state was not preserved' }}
if (-not (Test-Path -LiteralPath (Join-Path $paths.RuntimeRoot 'runtime\helper_registry.json'))) {{ throw 'helper registry was not preserved' }}
if ((Get-Content -LiteralPath (Join-Path $paths.RuntimeRoot 'runtime\interface-plans.json') -Raw) -notmatch 'new_collection_v13_0') {{ throw 'interface plans were not preserved' }}
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

    def test_transaction_releases_cutover_reservation_on_success_and_failure(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
foreach ($helperReady in @($true, $false)) {{
    $script:released = ''
    $inventory = [pscustomobject]@{{ digest = 'approved'; processes = @() }}
    $result = Invoke-AfpVerifiedRuntimePromotion -StagingPath 'D:\AFP_Runtime\.afp-staging\tc64-tests\stage' -Paths ([pscustomobject]@{{ RuntimeRoot='D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' }}) -ApprovedInventoryDigest 'approved' -CurrentInventory $inventory `
        -CutoverReadinessRunner {{ [pscustomobject]@{{ allowed=$true; reservation_id='lease-1' }} }} `
        -CutoverReleaseRunner {{ param($ReservationId) $script:released = $ReservationId; return $true }} `
        -StopRunner {{ }} -PromotionRunner {{ [pscustomobject]@{{ status='switched' }} }} -StartRunner {{ }} `
        -ServiceProbeRunner {{ [pscustomobject]@{{ status='passed' }} }} `
        -IngressPublisher {{ [pscustomobject]@{{ generation=4 }} }} `
        -HelperReadinessRunner {{ param($Ingress) [pscustomobject]@{{ ready=$helperReady; generation=$Ingress.generation }} }} `
        -CompletionRunner {{ }} -RestoreRunner {{ [pscustomobject]@{{ status='passed' }} }}
    if ($script:released -ne 'lease-1') {{ throw 'cutover reservation was not released' }}
    $expected = if ($helperReady) {{ 'completed' }} else {{ 'rolled_back' }}
    if ($result.status -ne $expected) {{ throw ($result | ConvertTo-Json -Depth 8) }}
}}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_release_failure_prevents_completion_and_reports_rollback_failure(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$script:completed = 0
$inventory = [pscustomobject]@{{ digest='approved'; processes=@() }}
$result = Invoke-AfpVerifiedRuntimePromotion -StagingPath 'D:\AFP_Runtime\.afp-staging\tc64-tests\stage' -Paths ([pscustomobject]@{{RuntimeRoot='D:\AFP_Runtime\.afp-staging\tc64-tests\runtime'}}) -ApprovedInventoryDigest 'approved' -CurrentInventory $inventory `
    -CutoverReadinessRunner {{ [pscustomobject]@{{allowed=$true;reservation_id='lease-1'}} }} `
    -CutoverReleaseRunner {{ return $false }} -StopRunner {{ }} `
    -PromotionRunner {{ [pscustomobject]@{{status='switched'}} }} -StartRunner {{ }} `
    -ServiceProbeRunner {{ [pscustomobject]@{{status='passed'}} }} `
    -IngressPublisher {{ [pscustomobject]@{{generation=4}} }} `
    -HelperReadinessRunner {{ param($Ingress) [pscustomobject]@{{ready=$true;generation=4}} }} `
    -CompletionRunner {{ $script:completed++ }} `
    -RestoreRunner {{ [pscustomobject]@{{status='passed'}} }}
if ($result.status -ne 'rollback_failed' -or $result.rollback_error -ne 'cutover_reservation_release_failed') {{ throw ($result | ConvertTo-Json -Depth 8) }}
if ($script:completed -ne 0) {{ throw 'completion ran before reservation release' }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_failed_rollback_probe_cannot_report_rolled_back(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$inventory = [pscustomobject]@{{ digest='approved'; processes=@() }}
$result = Invoke-AfpVerifiedRuntimePromotion -StagingPath 'D:\AFP_Runtime\.afp-staging\tc64-tests\stage' -Paths ([pscustomobject]@{{RuntimeRoot='D:\AFP_Runtime\.afp-staging\tc64-tests\runtime'}}) -ApprovedInventoryDigest 'approved' -CurrentInventory $inventory `
    -CutoverReadinessRunner {{ [pscustomobject]@{{allowed=$true;reservation_id='lease-1'}} }} `
    -CutoverReleaseRunner {{ return $true }} -StopRunner {{ }} `
    -PromotionRunner {{ [pscustomobject]@{{status='switched'}} }} -StartRunner {{ }} `
    -ServiceProbeRunner {{ [pscustomobject]@{{status='passed'}} }} `
    -IngressPublisher {{ [pscustomobject]@{{generation=4}} }} `
    -HelperReadinessRunner {{ [pscustomobject]@{{ready=$false}} }} `
    -RestoreRunner {{ [pscustomobject]@{{status='failed'}} }}
if ($result.status -ne 'rollback_failed' -or $result.rollback_error -ne 'rollback_service_gate_failed') {{ throw ($result | ConvertTo-Json -Depth 8) }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_rollback_restores_old_candidate_with_higher_generation(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            runtime = workspace / "AFP_Integrated_System_Modular"
            staging = workspace / ".afp-staging" / "deployment-test"
            for target in (runtime, staging):
                (target / "runtime").mkdir(parents=True)
                (target / "PublicIngress.psm1").write_bytes(
                    (ROOT / "visualization_app" / "PublicIngress.psm1").read_bytes()
                )
            old_state = {
                "schema_version": 1,
                "generation": 4,
                "public_url": "https://old.example",
                "provider": "tailscale",
                "recovery_owner": "tailscale",
                "candidate_id": "candidate-old",
                "status": "online",
                "published_at": "2026-10-16T00:00:00Z",
                "layers": {"edge": {"ok": True, "code": "ready"}},
            }
            (runtime / "runtime" / "public-ingress-state.json").write_text(
                json.dumps(old_state), encoding="utf-8"
            )
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$inventory = Get-AfpRuntimeInventory -ProcessRecords @() -ListenerRecords @() -StartupRecords @()
$result = Invoke-AfpVerifiedRuntimePromotion -StagingPath {_ps_quote(staging)} -Paths $paths -ApprovedInventoryDigest $inventory.digest -CurrentInventory $inventory `
    -CutoverReadinessRunner {{ [pscustomobject]@{{ allowed=$true }} }} -StopRunner {{ }} -StartRunner {{ }} `
    -ServiceProbeRunner {{ [pscustomobject]@{{ status='passed' }} }} `
    -IngressPublisher {{
        Import-Module (Join-Path $paths.RuntimeRoot 'PublicIngress.psm1') -Force
        $path = Join-Path $paths.RuntimeRoot 'runtime\public-ingress-state.json'
        Publish-AfpPublicIngressState -Path $path -PublicUrl 'https://new.example' -Provider 'cloudflare' -RecoveryOwner 'cloudflare' -CandidateId 'candidate-new' -ExpectedPreviousCandidateId 'candidate-old' -ExpectedGeneration 4 -MinimumGeneration 5 -Layers @{{ edge=@{{ok=$true;code='ready'}} }}
    }} `
    -HelperReadinessRunner {{ [pscustomobject]@{{ ready=$false }} }} -CompletionRunner {{ }} `
    -RollbackProbeRunner {{ [pscustomobject]@{{ status='passed' }} }}
if ($result.status -ne 'rolled_back') {{ throw ($result | ConvertTo-Json -Depth 8) }}
$restored = Get-Content -LiteralPath (Join-Path $paths.RuntimeRoot 'runtime\public-ingress-state.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($restored.candidate_id -cne 'candidate-old' -or [long]$restored.generation -le 5 -or $restored.status -ne 'degraded') {{ throw ($restored | ConvertTo-Json -Depth 8) }}
if ([string]$restored.layers.helper.code -ne 'rollback_helper_reconciliation_required') {{ throw ($restored | ConvertTo-Json -Depth 8) }}
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_failure_without_previous_ingress_reports_unrecoverable_rollback(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary) / "workspace"
            runtime = workspace / "AFP_Integrated_System_Modular"
            staging = workspace / ".afp-staging" / "deployment-test"
            for target in (runtime, staging):
                (target / "runtime").mkdir(parents=True)
                (target / "PublicIngress.psm1").write_bytes(
                    (ROOT / "visualization_app" / "PublicIngress.psm1").read_bytes()
                )
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(PATH_MODULE)} -Force
Import-Module {_ps_quote(MODULE)} -Force
$paths = Get-AfpDeploymentPaths -ConfigPath {_ps_quote(PATH_CONFIG)} -WorkspaceRootOverride {_ps_quote(workspace)}
$inventory = Get-AfpRuntimeInventory -ProcessRecords @() -ListenerRecords @() -StartupRecords @()
$result = Invoke-AfpVerifiedRuntimePromotion -StagingPath {_ps_quote(staging)} -Paths $paths -ApprovedInventoryDigest $inventory.digest -CurrentInventory $inventory `
    -CutoverReadinessRunner {{ [pscustomobject]@{{ allowed=$true }} }} -StopRunner {{ }} -StartRunner {{ }} `
    -ServiceProbeRunner {{ [pscustomobject]@{{ status='passed' }} }} `
    -IngressPublisher {{
        Import-Module (Join-Path $paths.RuntimeRoot 'PublicIngress.psm1') -Force
        Publish-AfpPublicIngressState -Path (Join-Path $paths.RuntimeRoot 'runtime\public-ingress-state.json') -PublicUrl 'https://new.example' -Provider 'cloudflare' -RecoveryOwner 'cloudflare' -CandidateId 'candidate-new' -Layers @{{ helper=@{{ok=$false;code='pending'}} }}
    }} `
    -HelperReadinessRunner {{ [pscustomobject]@{{ ready=$false }} }} -CompletionRunner {{ }}
if ($result.status -ne 'rollback_failed' -or $result.rollback_error -ne 'rollback_no_previous_ingress') {{ throw ($result | ConvertTo-Json -Depth 8) }}
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
$good = Test-AfpRuntimeServices -RuntimeRoot 'D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' -PublicUrl 'https://example.test/' -LanIPv4 '192.0.2.10' -ExpectedCandidateId 'candidate' -FunnelOrigin 'http://127.0.0.1:8770' -ProbeRunner $pass
if ($good.status -ne 'passed' -or $good.checks.Count -ne 7) {{ throw ($good | ConvertTo-Json -Depth 8) }}
$fail = {{ param($Name, $Target) [pscustomobject]@{{ passed = ($Name -ne 'public_https'); detail = 'probe' }} }}
$bad = Test-AfpRuntimeServices -RuntimeRoot 'D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' -PublicUrl 'https://example.test/' -LanIPv4 '192.0.2.10' -ExpectedCandidateId 'candidate' -FunnelOrigin 'http://127.0.0.1:8770' -ProbeRunner $fail
if ($bad.status -ne 'failed' -or $bad.issues -notcontains 'service probe failed: public_https') {{ throw ($bad | ConvertTo-Json -Depth 8) }}
"""
        result = _run_ps(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_service_gate_waits_for_delayed_startup_and_reports_attempts(self) -> None:
        script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$script:attempt = 0
$probe = {{
    param($Name, $Target)
    if ($Name -eq 'listener_8770') {{ $script:attempt++ }}
    [pscustomobject]@{{ passed = ($script:attempt -ge 3); detail = "attempt=$script:attempt" }}
}}
$result = Wait-AfpRuntimeServices -RuntimeRoot 'D:\AFP_Runtime\.afp-staging\tc64-tests\runtime' -PublicUrl 'https://example.test/' -LanIPv4 '192.0.2.10' -ExpectedCandidateId 'candidate' -FunnelOrigin 'http://127.0.0.1:8770' -TimeoutSeconds 5 -PollIntervalMilliseconds 1 -ProbeRunner $probe -DelayRunner {{ param($Milliseconds) }}
if ($result.status -ne 'passed' -or $result.attempts -ne 3) {{ throw ($result | ConvertTo-Json -Depth 8) }}
if ($result.checks.Count -ne 7) {{ throw ($result | ConvertTo-Json -Depth 8) }}
"""
        result = _run_ps(script)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_public_static_identity_compares_remote_bytes_to_fixed_runtime(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            runtime = Path(temporary) / "AFP_Integrated_System_Modular"
            asset = runtime / "app" / "ui" / "app.js"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"current-static-identity")
            (runtime / "RELEASE-MANIFEST.json").write_text(
                json.dumps({"candidate": {"candidate_id": "candidate-current"}}),
                encoding="utf-8",
            )
            script = rf"""
$ErrorActionPreference = 'Stop'
Import-Module {_ps_quote(MODULE)} -Force
$same = Test-AfpPublicStaticIdentity -RuntimeRoot {_ps_quote(runtime)} -PublicUrl 'https://example.test/' -ExpectedCandidateId 'candidate-current' -ContentFetcher {{ param($Uri) [IO.File]::ReadAllBytes({_ps_quote(asset)}) }}
if (-not $same.passed) {{ throw ($same | ConvertTo-Json -Depth 8) }}
$old = Test-AfpPublicStaticIdentity -RuntimeRoot {_ps_quote(runtime)} -PublicUrl 'https://example.test/' -ExpectedCandidateId 'candidate-current' -ContentFetcher {{ param($Uri) [Text.Encoding]::UTF8.GetBytes('old-static-identity') }}
if ($old.passed) {{ throw ($old | ConvertTo-Json -Depth 8) }}
if ($same.uri -notmatch '^https://example\.test/app\.js\?afp_identity_check=') {{ throw "unexpected URI: $($same.uri)" }}
"""
            result = _run_ps(script)
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_rollback_plan_reuses_staging_and_fixed_runtime(self) -> None:
        source = CLI.read_text(encoding="utf-8-sig")
        self.assertIn('"Rollback"', source)
        self.assertIn("New-AfpRollbackStage", source)
        self.assertIn("Test-AfpReleaseGate", source)
        self.assertIn("New-AfpApprovalSummary", source)
        self.assertIn("Wait-AfpRuntimeServices", source)
        self.assertIn("ExpectedPreviousCandidateId", source)
        self.assertIn("MinimumGeneration", source)
        self.assertIn("FailedIngressGeneration", source)
        self.assertIn('"FinalizeSwitchover"', source)
        self.assertIn("legacy-cutover-reservation.py", source)
        self.assertIn("cutover_reservation_active", source)
        self.assertNotIn("F:\\\\", source)
        self.assertNotIn("previous-runtime", source.lower())
        release = (ROOT / "pre_sensor" / "SingleRuntimeRelease.psm1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("worktree add --detach", release)
        self.assertIn("worktree remove --force", release)
        self.assertIn("New-AfpStagedRelease", release)


if __name__ == "__main__":
    unittest.main()
