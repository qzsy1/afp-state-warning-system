---
name: local-branch-workflow
description: Manage this repository's single-checkout local task-branch workflow. Use automatically for implementation, development, modification, bug fixes, continuing work, committing or creating a PR, checking or merging the current task, updating local main, or viewing work and PRs from another computer.
---

# Local Branch Workflow

Use the repository's ordinary Local checkout and the helper at `scripts/local-branch-workflow.ps1`. Never create a Git worktree, Codex Worktree, extra clone, or project copy. Read every applicable `AGENTS.md` before changing files.

## Shared invariants

- Read `git config --local --get codex.machine-id`; never replace an existing value. If absent, the helper sets PC-B when this Skill exists, or PC-A when it does not, then reads the value back. Identity is stored only in this clone's `.git/config`, never in tracked documentation. Distinct physical computers must have distinct identities; stop if an existing identity is already in use on another computer.
- Confirm `git rev-parse --git-dir` and `git rev-parse --git-common-dir` resolve to the same directory. Stop if they differ.
- Protect all existing tracked and untracked changes. Never delete, overwrite, stage, stash, or commit changes whose ownership is unclear.
- Modify existing files in place. Do not preserve versions with `_v2`, `_final`, `_new`, `_backup`, or equivalent copies.
- Keep documents and comments limited to the current effective state. Do not create process reports, AI histories, temporary phase documents, or duplicate summaries.
- Run the helper from the repository root with PowerShell: `& ./.agents/skills/local-branch-workflow/scripts/local-branch-workflow.ps1 -Action <action>`.
- Infer actions and task slugs from the user's natural language. Never require the user to supply a mode, machine ID, branch name, or Git command. On Windows, if `gh` is missing from PATH, check the standard installed GitHub CLI location before reporting it unavailable.

## Start or continue implementation

1. Run the helper with `-Action Inspect`.
2. Fetch remote state and inspect `git branch -r --list 'origin/codex/*'` plus open GitHub PRs. Use `gh pr list --state open` when authenticated; report a real access limitation otherwise.
3. If on `main`, require a clean worktree. Generate a short lowercase task slug and run the helper with `-Action StartTask -TaskSlug <slug>`. This fast-forwards local `main` to the detected remote default branch and creates `codex/<machine-id-lowercase>/<task-slug>` from it.
4. If already on a task branch owned by this machine, continue it. If the branch belongs to another machine, stop without modifying files.
5. Change only the requested files and run real tests matching the scope. Use `./harness/engine/run.ps1 -Profile quick -Environment local` for the normal local gate, narrower existing checks when they fully cover a focused change, and `-Profile full` locally when the change requires the complete F-drive-backed regression suite. GitHub Actions runs the CI-supported Harness checks declared in `.github/workflows/quality-gate.yml`.
6. Do not invent commands or results. If the user did not request a commit or PR, leave the work on the local task branch and report its status.
7. When the user requests a PR, commit only task-owned changes and complete the synchronization and test checks below before pushing and creating or updating a PR targeting `main`. A Draft PR is appropriate only after the branch has a complete, verifiable commit that helps coordinate work; it must pass the same synchronization and test checks.

## Deploy completed changes

- After the task's implementation and relevant checks pass, automatically update `D:\AFP_Runtime\AFP_Integrated_System_Modular` once for the task using the controlled staging and switchover workflow. Routine internal edits and intermediate checks do not require repeated assembly or updates. Documentation-only or test-only changes do not require republishing unchanged runtime content. The user has granted standing authorization for these routine updates; do not request release permission again.
- Build only under `D:\AFP_Runtime\.afp-staging`, preserve runtime preferences and capture data, and stop acquisition safely before replacing the running release. Launch from the fixed runtime directory and verify the release identity, file digests, process paths, runtime revision, and served page assets. Routine cleanup of verified task-owned staging artifacts and old-runtime swap directories is also authorized. Clean them after success or failure; if permissions, file locks, or automatic approval review block cleanup, list the retained paths and reason without bypassing tool policy or reporting cleanup as passed. Blocked cleanup does not block delivery of software that has passed verification.
- If verification or deployment fails, acquisition cannot safely stop, or the user explicitly defers deployment, report the actual runtime state and the reason. Report software deployment and physical device acceptance separately. Startup configuration changes still require their own authorization.

## Prepare a PR or merge a task

1. Before creating or updating a PR, or on an explicit merge request, run the helper with `-Action PrepareMerge`; it rejects `main`, foreign-machine branches, dirty state, and unresolved conflicts, then fetches `origin` and merges `origin/main` when necessary without rebasing. PR authorization does not authorize merging.
2. Resolve any conflict according to both sides' current valid behavior. Do not restore obsolete files or create copied variants.
3. Run the relevant real tests, followed by `git diff --check` and `git status --short --branch`.
4. Run the helper with `-Action VerifyMerge`. If it reports that `origin/main` advanced, repeat preparation and all relevant tests.
5. Push without force and create or update the PR. Read the GitHub PR state and confirm its base, head, head commit, conflict state, changed files, and required checks.
6. Merge only on the user's explicit merge request and when the task branch contains current `origin/main`, the worktree is as expected, tests and required GitHub checks pass, conflicts are absent, and the PR contains only task-related changes. Re-fetch and check ancestry immediately before merging, use the expected head SHA for the merge, and rely on the strict required up-to-date GitHub check to close the final race. If main advances, repeat integration, tests, verification, and GitHub checks.
7. After merge, switch to local `main` and run the helper with `-Action UpdateMain`. Delete only this machine's merged branch and only after confirming its task commit is in `origin/main`; never delete another machine's branch.

## Update or inspect only

- For “update local main”, require a clean worktree on `main`, then run `-Action UpdateMain`.
- For “view another computer's tasks and PRs”, fetch with prune, list `origin/codex/*`, and inspect open PRs without changing branches or files.
