import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / ".agents/skills/local-branch-workflow/scripts/local-branch-workflow.ps1"
SHELL = shutil.which("pwsh") or shutil.which("powershell")

# All Git calls are simulated; these checks never create a checkout or alter refs.
DRIVER = r"""
$data = $env:AFP_WORKFLOW_TEST | ConvertFrom-Json
$global:workflowCalls = [Collections.Generic.List[string]]::new()
$global:workflowReplies = @{}
foreach ($property in $data.replies.PSObject.Properties) {
    $global:workflowReplies[$property.Name] = $property.Value
}
function global:git {
    $key = $args -join ' '
    $global:workflowCalls.Add($key)
    if ($key -match '^config --local codex.machine-id (.+)$') {
        $global:workflowReplies['config --local --get codex.machine-id'] =
            [pscustomobject]@{code = 0; output = $Matches[1]}
        $global:LASTEXITCODE = 0
        return
    }
    if (-not $global:workflowReplies.ContainsKey($key)) {
        throw "Unexpected Git command: $key"
    }
    $reply = $global:workflowReplies[$key]
    $global:LASTEXITCODE = [int]$reply.code
    if ($reply.output) { Write-Output $reply.output }
}
$success = $true
$message = ''
try {
    $null = & $data.helper -Action $data.action -TaskSlug 'Workflow Test'
} catch {
    $success = $false
    $message = $_.Exception.Message
}
[pscustomobject]@{
    success = $success
    message = $message
    calls = @($global:workflowCalls.ToArray())
} | ConvertTo-Json -Compress
"""


@unittest.skipUnless(SHELL, "PowerShell is required for workflow behavior checks")
class LocalBranchWorkflowTests(unittest.TestCase):
    def run_helper(self, action="Inspect", overrides=None):
        replies = {
            "rev-parse --show-toplevel": (0, str(REPO)),
            "rev-parse --path-format=absolute --git-dir": (0, str(REPO / ".git")),
            "rev-parse --path-format=absolute --git-common-dir": (0, str(REPO / ".git")),
            "config --local --get codex.machine-id": (0, "PC-A"),
            "symbolic-ref --quiet --short refs/remotes/origin/HEAD": (0, "origin/main"),
            "branch --show-current": (0, "codex/pc-a/workflow-test"),
            "status --short --branch": (0, "## codex/pc-a/workflow-test"),
            "status --porcelain=v1 --untracked-files=all": (0, ""),
            "diff --name-only --diff-filter=U": (0, ""),
            "fetch --prune origin": (0, ""),
            "fetch origin": (0, ""),
            "merge-base --is-ancestor origin/main HEAD": (0, ""),
            "merge --no-edit origin/main": (0, ""),
            "merge --ff-only origin/main": (0, ""),
            "diff --check": (0, ""),
            "show-ref --verify --quiet refs/heads/codex/pc-a/workflow-test": (1, ""),
            "show-ref --verify --quiet refs/remotes/origin/codex/pc-a/workflow-test": (1, ""),
            "switch --no-track -c codex/pc-a/workflow-test origin/main": (0, ""),
        }
        replies.update(overrides or {})
        payload = {
            "helper": str(HELPER),
            "action": action,
            "replies": {key: {"code": code, "output": output} for key, (code, output) in replies.items()},
        }
        completed = subprocess.run(
            [SHELL, "-NoProfile", "-NonInteractive", "-Command", DRIVER],
            cwd=REPO,
            env={**os.environ, "AFP_WORKFLOW_TEST": json.dumps(payload)},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout.strip())

    def test_existing_identity_is_never_rewritten(self):
        result = self.run_helper()
        self.assertTrue(result["success"], result["message"])
        self.assertFalse(any(call.startswith("config --local codex.machine-id ") for call in result["calls"]))

    def test_new_machine_is_initialized_locally_and_read_back(self):
        result = self.run_helper(overrides={"config --local --get codex.machine-id": (1, "")})
        self.assertTrue(result["success"], result["message"])
        self.assertIn("config --local codex.machine-id PC-B", result["calls"])
        self.assertEqual(result["calls"].count("config --local --get codex.machine-id"), 2)

    def test_invalid_or_unreadable_identity_is_preserved(self):
        for reply in ((0, ""), (0, "invalid/id"), (2, "")):
            with self.subTest(reply=reply):
                result = self.run_helper(overrides={"config --local --get codex.machine-id": reply})
                self.assertFalse(result["success"])
                self.assertFalse(any(call.startswith("config --local codex.machine-id ") for call in result["calls"]))

    def test_worktree_is_rejected_before_identity_or_mutation(self):
        result = self.run_helper(overrides={
            "rev-parse --path-format=absolute --git-dir": (0, str(REPO / ".git/worktrees/other")),
        })
        self.assertFalse(result["success"])
        self.assertIn("Worktrees are prohibited", result["message"])
        self.assertFalse(any(call.startswith("config ") for call in result["calls"]))

    def test_start_fetches_and_fast_forwards_before_creating_local_branch(self):
        result = self.run_helper("StartTask", {"branch --show-current": (0, "main")})
        self.assertTrue(result["success"], result["message"])
        calls = result["calls"]
        self.assertLess(calls.index("fetch --prune origin"), calls.index("merge --ff-only origin/main"))
        self.assertLess(calls.index("merge --ff-only origin/main"), calls.index("switch --no-track -c codex/pc-a/workflow-test origin/main"))

    def test_existing_owned_task_is_continued_without_new_branch(self):
        result = self.run_helper("StartTask")
        self.assertTrue(result["success"], result["message"])
        self.assertIn("fetch --prune origin", result["calls"])
        self.assertFalse(any(call.startswith("switch ") for call in result["calls"]))

    def test_foreign_branch_and_main_cannot_prepare_merge(self):
        for branch in ("main", "codex/pc-b/other-task"):
            with self.subTest(branch=branch):
                result = self.run_helper("PrepareMerge", {"branch --show-current": (0, branch)})
                self.assertFalse(result["success"])
                self.assertNotIn("fetch --prune origin", result["calls"])

    def test_dirty_worktree_and_conflicts_are_protected(self):
        for override in (
            {"status --porcelain=v1 --untracked-files=all": (0, "?? user-file.txt")},
            {"status --porcelain=v1 --untracked-files=all": (0, " M app.py")},
            {"diff --name-only --diff-filter=U": (0, "app.py")},
        ):
            with self.subTest(override=override):
                result = self.run_helper("PrepareMerge", override)
                self.assertFalse(result["success"])
                self.assertNotIn("fetch --prune origin", result["calls"])

    def test_outdated_branch_integrates_main_by_normal_merge(self):
        result = self.run_helper("PrepareMerge", {"merge-base --is-ancestor origin/main HEAD": (1, "")})
        self.assertTrue(result["success"], result["message"])
        self.assertIn("merge --no-edit origin/main", result["calls"])

    def test_update_during_testing_requires_integration_and_retest(self):
        result = self.run_helper("VerifyMerge", {"merge-base --is-ancestor origin/main HEAD": (1, "")})
        self.assertFalse(result["success"])
        self.assertIn("repeat the relevant tests", result["message"])
        self.assertIn("fetch origin", result["calls"])

    def test_updating_main_rejects_task_branch(self):
        result = self.run_helper("UpdateMain")
        self.assertFalse(result["success"])
        self.assertNotIn("merge --ff-only origin/main", result["calls"])


if __name__ == "__main__":
    unittest.main()
