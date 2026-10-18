---
name: local-branch-workflow
description: Manage this repository's single-checkout local task-branch workflow. Use automatically for implementation, development, modification, bug fixes, continuing work, committing or creating a PR, checking or merging the current task, updating local main, or viewing work and PRs from another computer.
---

# Local Branch Workflow

Use the repository's ordinary Local checkout and the helper at `scripts/local-branch-workflow.ps1`. Never create a Git worktree, Codex Worktree, extra clone, or project copy. Read every applicable `AGENTS.md` before changing files.

## Shared invariants

- Read `git config --local --get codex.machine-id`; never replace an existing value. The configured value is local clone state and must not enter tracked documentation.
- Confirm `git rev-parse --git-dir` and `git rev-parse --git-common-dir` resolve to the same directory. Stop if they differ.
- Protect all existing tracked and untracked changes. Never delete, overwrite, stage, stash, or commit changes whose ownership is unclear.
- Modify existing files in place. Do not preserve versions with `_v2`, `_final`, `_new`, `_backup`, or equivalent copies.
- Keep documents and comments limited to the current effective state. Do not create process reports, AI histories, temporary phase documents, or duplicate summaries.
- Run the helper from the repository root with PowerShell: `& ./.agents/skills/local-branch-workflow/scripts/local-branch-workflow.ps1 -Action <action>`.

## Start or continue implementation

1. Run the helper with `-Action Inspect`.
2. Fetch remote state and inspect `git branch -r --list 'origin/codex/*'` plus open GitHub PRs. Use `gh pr list --state open` when authenticated; report a real access limitation otherwise.
3. If on `main`, require a clean worktree. Generate a short lowercase task slug and run the helper with `-Action StartTask -TaskSlug <slug>`. This fast-forwards local `main` to the detected remote default branch and creates `codex/<machine-id-lowercase>/<task-slug>` from it.
4. If already on a task branch owned by this machine, continue it. If the branch belongs to another machine, stop without modifying files.
5. Change only the requested files and run real tests matching the scope. Use `./harness/engine/run.ps1 -Profile quick -Environment local` for the normal local gate, narrower existing checks when they fully cover a focused change, and `-Profile full` locally when the change requires the complete F-drive-backed regression suite. GitHub Actions runs the CI-supported Harness checks declared in `.github/workflows/quality-gate.yml`.
6. Do not invent commands or results. If the user did not request a commit or PR, leave the work on the local task branch and report its status.
7. When the user requests a PR, commit only task-owned changes, push the current branch normally, and create or update a PR targeting `main`. A Draft PR is appropriate only after the branch has a complete, verifiable commit that helps coordinate work.

## Prepare and merge a task

1. Require the user's explicit merge request. Run the helper with `-Action PrepareMerge`; it rejects `main`, foreign-machine branches, dirty state, and unresolved conflicts, then fetches `origin` and merges `origin/main` when necessary without rebasing.
2. Resolve any conflict according to both sides' current valid behavior. Do not restore obsolete files or create copied variants.
3. Run the relevant real tests, followed by `git diff --check` and `git status --short --branch`.
4. Run the helper with `-Action VerifyMerge`. If it reports that `origin/main` advanced, repeat preparation and all relevant tests.
5. Push without force and create or update the PR. Read the GitHub PR state and confirm its base, head, head commit, conflict state, changed files, and required checks.
6. Merge only when the task branch contains current `origin/main`, the worktree is as expected, tests and required GitHub checks pass, conflicts are absent, and the PR contains only task-related changes.
7. After merge, switch to local `main` and run the helper with `-Action UpdateMain`. Delete only this machine's merged branch and only after confirming its task commit is in `origin/main`; never delete another machine's branch.

## Update or inspect only

- For “update local main”, require a clean worktree on `main`, then run `-Action UpdateMain`.
- For “view another computer's tasks and PRs”, fetch with prune, list `origin/codex/*`, and inspect open PRs without changing branches or files.
