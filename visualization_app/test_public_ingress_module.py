from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "visualization_app" / "PublicIngress.psm1"


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    escaped_module = str(MODULE).replace("'", "''")
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"$ErrorActionPreference='Stop'; Import-Module -Force '{escaped_module}'; {script}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


class PublicIngressModuleTests(unittest.TestCase):
    def assert_ps_ok(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)

    def test_global_address_classifier_rejects_every_special_range_and_accepts_public_v4_v6(self) -> None:
        rejected = [
            "0.0.0.1", "10.0.0.1", "100.64.0.1", "127.0.0.1",
            "169.254.0.1", "172.16.0.1", "192.0.0.1", "192.0.2.1",
            "192.88.99.1", "192.168.1.1", "198.18.0.1", "198.51.100.1",
            "203.0.113.1", "224.0.0.1", "240.0.0.1", "255.255.255.255",
            "::", "::1", "::ffff:10.0.0.1", "64:ff9b:1::1", "100::1",
            "2001:2::1", "2001:db8::1", "2002::1", "fc00::1", "fe80::1",
            "ff00::1", "fe80::1%12",
        ]
        accepted = ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"]
        vectors = [{"address": item, "expected": False} for item in rejected]
        vectors += [{"address": item, "expected": True} for item in accepted]
        literal = json.dumps(vectors, separators=(",", ":")).replace("'", "''")
        completed = run_powershell(
            f"$vectors=ConvertFrom-Json '{literal}'; "
            "$result=@($vectors | ForEach-Object { [pscustomobject]@{ address=$_.address; actual=(Test-AfpGlobalIPAddress -Address $_.address); expected=$_.expected } }); "
            "$result | ConvertTo-Json -Compress"
        )
        self.assert_ps_ok(completed)
        results = json.loads(completed.stdout)
        self.assertEqual(
            [(item["address"], item["actual"]) for item in results],
            [(item["address"], item["expected"]) for item in results],
        )

    def test_forced_probe_uses_resolve_noproxy_no_redirect_and_requires_exact_200(self) -> None:
        completed = run_powershell(
            "$all=@(); foreach($status in @(200,204,301,0)){ "
            "$captured=$null; $runner={ param($Arguments,$BodyPath) $script:captured=@($Arguments); "
            "[pscustomobject]@{ exit_code=($(if($status -eq 0){7}else{0})); status_code=$status } }; "
            "$probe=Invoke-AfpForcedPublicProbe -Uri ([Uri]'https://Example.COM/health') -Address '1.1.1.1' -CurlRunner $runner; "
            "$all += [pscustomobject]@{status=$status; ok=$probe.ok; args=@($script:captured)} }; "
            "$all | ConvertTo-Json -Depth 5 -Compress"
        )
        self.assert_ps_ok(completed)
        results = json.loads(completed.stdout)
        self.assertEqual([item["ok"] for item in results], [True, False, False, False])
        args = results[0]["args"]
        self.assertIn("--noproxy", args)
        self.assertIn("*", args)
        self.assertIn("--max-redirs", args)
        self.assertIn("0", [str(value) for value in args])
        self.assertIn("--resolve", args)
        self.assertIn("example.com:443:1.1.1.1", [str(value).lower() for value in args])
        self.assertIn("--write-out", args)
        self.assertNotIn("--location", args)

    def test_dns_mixed_answers_keep_only_global_addresses(self) -> None:
        completed = run_powershell(
            "$resolver={ param($HostName,$RecordType) if($RecordType -eq 'A'){ @('10.0.0.2','1.1.1.1','203.0.113.5') } "
            "else { @('fe80::1','2606:4700:4700::1111','2001:db8::1') } }; "
            "@(Resolve-AfpPublicEdgeAddresses -HostName 'example.com' -Resolver $resolver) | ConvertTo-Json -Compress"
        )
        self.assert_ps_ok(completed)
        self.assertEqual(json.loads(completed.stdout), ["1.1.1.1", "2606:4700:4700::1111"])

    def test_atomic_publish_increments_only_on_url_provider_or_owner_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "public-ingress-state.json"
            escaped = str(state_path).replace("'", "''")
            completed = run_powershell(
                f"$path='{escaped}'; $layers=@{{dns=@{{ok=$true}}; edge=@{{ok=$true}}}}; "
                "$a=Publish-AfpPublicIngressState -Path $path -PublicUrl 'https://Example.com/' -Provider 'TAILSCALE' -RecoveryOwner 'TAILSCALE' -CandidateId 'candidate-1' -Layers $layers; "
                "$b=Publish-AfpPublicIngressState -Path $path -PublicUrl 'https://example.com' -Provider 'tailscale' -RecoveryOwner 'tailscale' -CandidateId 'candidate-1' -Layers $layers -ExpectedGeneration 1; "
                "$c=Publish-AfpPublicIngressState -Path $path -PublicUrl 'https://next.example.com' -Provider 'tailscale' -RecoveryOwner 'tailscale' -CandidateId 'candidate-1' -Layers $layers -ExpectedGeneration 1; "
                "@($a,$b,$c) | Select-Object changed,generation | ConvertTo-Json -Compress"
            )
            self.assert_ps_ok(completed)
            self.assertEqual(
                json.loads(completed.stdout),
                [
                    {"changed": True, "generation": 1},
                    {"changed": False, "generation": 1},
                    {"changed": True, "generation": 2},
                ],
            )

    def test_atomic_publish_preserves_previous_state_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "public-ingress-state.json"
            escaped = str(state_path).replace("'", "''")
            completed = run_powershell(
                f"$path='{escaped}'; $layers=@{{edge=@{{ok=$true}}}}; "
                "$null=Publish-AfpPublicIngressState -Path $path -PublicUrl 'https://one.example' -Provider 'tailscale' -RecoveryOwner 'tailscale' -CandidateId 'candidate-1' -Layers $layers; "
                "$before=[IO.File]::ReadAllText($path); Set-ItemProperty -LiteralPath $path -Name IsReadOnly -Value $true; $failed=$false; "
                "try { $null=Publish-AfpPublicIngressState -Path $path -PublicUrl 'https://two.example' -Provider 'tailscale' -RecoveryOwner 'tailscale' -CandidateId 'candidate-1' -Layers $layers } catch { $failed=$true } finally { Set-ItemProperty -LiteralPath $path -Name IsReadOnly -Value $false }; "
                "$after=[IO.File]::ReadAllText($path); $temps=@(Get-ChildItem -LiteralPath (Split-Path $path) -Filter '*.tmp').Count; "
                "[pscustomobject]@{failed=$failed; preserved=($before -eq $after); temps=$temps} | ConvertTo-Json -Compress"
            )
            self.assert_ps_ok(completed)
            self.assertEqual(
                json.loads(completed.stdout),
                {"failed": True, "preserved": True, "temps": 0},
            )

    def test_state_reader_rejects_corruption_generation_rollback_and_candidate_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "public-ingress-state.json"
            escaped = str(state_path).replace("'", "''")
            completed = run_powershell(
                f"$path='{escaped}'; $layers=@{{edge=@{{ok=$true}}}}; "
                "$null=Publish-AfpPublicIngressState -Path $path -PublicUrl 'https://one.example' -Provider 'tailscale' -RecoveryOwner 'tailscale' -CandidateId 'candidate-1' -Layers $layers; "
                "$null=Publish-AfpPublicIngressState -Path $path -PublicUrl 'https://two.example' -Provider 'tailscale' -RecoveryOwner 'tailscale' -CandidateId 'candidate-1' -Layers $layers; "
                "$valid=Read-AfpPublicIngressState -Path $path -ExpectedCandidateId 'candidate-1'; "
                "$json=Get-Content -LiteralPath $path -Raw | ConvertFrom-Json; $json.generation=1; $json | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $path -Encoding UTF8; "
                "$rollback=$false; try { Read-AfpPublicIngressState -Path $path | Out-Null } catch { $rollback=$true }; "
                "$json.generation=2; $json | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $path -Encoding UTF8; "
                "$mismatch=$false; try { Read-AfpPublicIngressState -Path $path -ExpectedCandidateId 'candidate-2' | Out-Null } catch { $mismatch=$true }; "
                "Set-Content -LiteralPath $path -Value '{broken' -Encoding UTF8; $corrupt=$false; try { Read-AfpPublicIngressState -Path $path | Out-Null } catch { $corrupt=$true }; "
                "[pscustomobject]@{valid_generation=$valid.generation; rollback=$rollback; mismatch=$mismatch; corrupt=$corrupt} | ConvertTo-Json -Compress"
            )
            self.assert_ps_ok(completed)
            self.assertEqual(
                json.loads(completed.stdout),
                {"valid_generation": 2, "rollback": True, "mismatch": True, "corrupt": True},
            )

    def test_recovery_owner_is_exact_and_case_normalized(self) -> None:
        completed = run_powershell(
            "$state=[pscustomobject]@{provider='TAILSCALE'; recovery_owner='TailScale'}; "
            "@((Test-AfpIngressRecoveryOwner -State $state -Provider 'tailscale'), "
            "(Test-AfpIngressRecoveryOwner -State $state -Provider 'TAILSCALE'), "
            "(Test-AfpIngressRecoveryOwner -State $state -Provider 'cloudflare'), "
            "(Test-AfpIngressRecoveryOwner -State $state -Provider 'tailscale-other')) | ConvertTo-Json -Compress"
        )
        self.assert_ps_ok(completed)
        self.assertEqual(json.loads(completed.stdout), [True, True, False, False])


if __name__ == "__main__":
    unittest.main()
