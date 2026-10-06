from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitEvidenceError(RuntimeError):
    """Raised when repository identity cannot be collected."""


@dataclass(frozen=True)
class GitEvidence:
    commit: str
    branch: str
    dirty: bool
    status: str


def _git(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
        raise GitEvidenceError(f"git {' '.join(arguments)} failed: {detail}")
    # Porcelain status uses the first two columns for index/worktree state.
    # Removing leading whitespace corrupts the first changed path (for
    # example `` M .gitignore`` becomes ``M .gitignore``).  Git's scalar
    # commands used here only need trailing line endings removed.
    return completed.stdout.rstrip()


def collect_git_evidence(repo_root: Path) -> GitEvidence:
    root = Path(repo_root).resolve()
    commit = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "branch", "--show-current") or "DETACHED"
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    return GitEvidence(commit=commit, branch=branch, dirty=bool(status), status=status)


def changed_files_from_git(repo_root: Path, base_ref: str) -> tuple[str, ...]:
    output = _git(Path(repo_root).resolve(), "diff", "--name-only", "--diff-filter=ACMRTUXB", f"{base_ref}...HEAD")
    return tuple(line.replace("\\", "/") for line in output.splitlines() if line.strip())
