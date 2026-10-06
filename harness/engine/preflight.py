from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from .models import CheckSpec


@dataclass(frozen=True)
class PreflightIssue:
    category: str
    item: str
    message: str
    affected_checks: tuple[str, ...]
    suggestion: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _node_version() -> tuple[int | None, str]:
    executable = shutil.which("node")
    if not executable:
        return None, "node executable was not found on PATH"
    try:
        value = subprocess.check_output([executable, "--version"], text=True, timeout=10).strip()
        return int(value.lstrip("v").split(".", 1)[0]), value
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return None, f"could not read Node.js version: {exc}"


def run_preflight(
    repo_root: Path,
    checks: Sequence[CheckSpec],
    *,
    environment: str = "local",
    baseline_exe: str | None = None,
    python_version: tuple[int, int] | None = None,
) -> tuple[PreflightIssue, ...]:
    root = Path(repo_root).resolve()
    issues: list[PreflightIssue] = []
    check_ids = tuple(check.id for check in checks)
    version = python_version or (sys.version_info.major, sys.version_info.minor)
    if version < (3, 11):
        issues.append(PreflightIssue(
            "environment", "Python 3.11", f"Python {version[0]}.{version[1]} is too old",
            check_ids, "Install Python 3.11 and recreate the project .venv.",
        ))
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    if environment == "local" and not venv_python.is_file():
        issues.append(PreflightIssue(
            "environment", "project .venv", f"project interpreter is missing: {venv_python}",
            check_ids, "Create .venv with Python 3.11 and install the project requirements.",
        ))
    for check in checks:
        cwd = (root / check.cwd).resolve()
        if not cwd.is_dir():
            issues.append(PreflightIssue(
                "environment", "working directory", f"working directory does not exist: {cwd}",
                (check.id,), f"Restore the directory declared by check {check.id}.",
            ))
    node_checks = tuple(check.id for check in checks if check.id == "frontend-public-contracts")
    if node_checks:
        node_major, detail = _node_version()
        if node_major is None or node_major < 22:
            issues.append(PreflightIssue(
                "environment", "Node.js 22", f"Node.js 22+ is required; {detail}",
                node_checks, "Install Node.js 22 LTS and reopen the command window.",
            ))
    exe_checks = tuple(check.id for check in checks if check.id.startswith("release-"))
    if exe_checks:
        exe_path = Path(baseline_exe) if baseline_exe else None
        if exe_path and not exe_path.is_absolute():
            exe_path = root / exe_path
        if not exe_path or not exe_path.is_file():
            shown = str(exe_path) if exe_path else "not configured"
            issues.append(PreflightIssue(
                "environment", "baseline EXE", f"baseline EXE is missing: {shown}",
                exe_checks, "Run harness\\setup.cmd and select the trusted stable EXE.",
            ))
    return tuple(issues)


def format_preflight(issues: Sequence[PreflightIssue]) -> str:
    if not issues:
        return "Environment preflight passed."
    lines = [f"Environment preflight found {len(issues)} issue(s):"]
    for issue in issues:
        lines.extend((
            f"- {issue.item}: {issue.message}",
            f"  Affected: {', '.join(issue.affected_checks) or 'all checks'}",
            f"  Fix: {issue.suggestion}",
        ))
    return "\n".join(lines)
