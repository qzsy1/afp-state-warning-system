from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .models import CheckSpec


OUTPUT_LIMIT = 64 * 1024


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    title: str
    status: str
    started_at: str
    ended_at: str
    duration_seconds: float
    command: tuple[str, ...]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    error: str
    blocking: bool
    evidence_tier: str
    requirements: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _bounded(value: bytes | str | None) -> str:
    text = _decode(value)
    if len(text) <= OUTPUT_LIMIT:
        return text
    omitted = len(text) - OUTPUT_LIMIT
    return f"{text[:OUTPUT_LIMIT]}\n... [truncated {omitted} characters]"


def _result(
    check: CheckSpec,
    *,
    status: str,
    started_at: str,
    start_time: float,
    command: tuple[str, ...],
    cwd: str,
    exit_code: int | None = None,
    stdout: bytes | str | None = None,
    stderr: bytes | str | None = None,
    error: str = "",
) -> CheckResult:
    return CheckResult(
        check_id=check.id,
        title=check.title,
        status=status,
        started_at=started_at,
        ended_at=_now(),
        duration_seconds=round(time.monotonic() - start_time, 6),
        command=command,
        cwd=cwd,
        exit_code=exit_code,
        stdout=_bounded(stdout),
        stderr=_bounded(stderr),
        error=error,
        blocking=check.blocking,
        evidence_tier=check.evidence_tier,
        requirements=check.requirements,
    )


def run_check(
    check: CheckSpec,
    repo_root: Path,
    inherited_env: Mapping[str, str],
    *,
    command_variables: Mapping[str, str] | None = None,
) -> CheckResult:
    started_at = _now()
    start_time = time.monotonic()
    if check.kind == "field":
        return _result(
            check,
            status="pending_field",
            started_at=started_at,
            start_time=start_time,
            command=(),
            cwd=check.cwd,
            error="field evidence requires explicit human acceptance",
        )

    variables = {"{python}": sys.executable}
    variables.update(command_variables or {})
    command = tuple(variables.get(part, part) for part in check.command)
    unresolved = tuple(part for part in command if part.startswith("{") and part.endswith("}"))
    if unresolved:
        return _result(
            check,
            status="not_started",
            started_at=started_at,
            start_time=start_time,
            command=command,
            cwd=check.cwd,
            error=f"unresolved command variable: {', '.join(unresolved)}",
        )
    root = Path(repo_root).resolve()
    working_directory = (root / check.cwd).resolve()
    try:
        working_directory.relative_to(root)
    except ValueError:
        return _result(
            check,
            status="not_started",
            started_at=started_at,
            start_time=start_time,
            command=command,
            cwd=str(working_directory),
            error=f"working directory escapes repository: {working_directory}",
        )
    if not working_directory.is_dir():
        return _result(
            check,
            status="not_started",
            started_at=started_at,
            start_time=start_time,
            command=command,
            cwd=str(working_directory),
            error=f"working directory does not exist: {working_directory}",
        )
    try:
        completed = subprocess.run(
            command,
            cwd=working_directory,
            env=dict(inherited_env),
            capture_output=True,
            text=False,
            timeout=check.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _result(
            check,
            status="timed_out",
            started_at=started_at,
            start_time=start_time,
            command=command,
            cwd=str(working_directory),
            stdout=exc.stdout,
            stderr=exc.stderr,
            error=f"command timed out after {check.timeout_seconds:g} seconds",
        )
    except (OSError, ValueError) as exc:
        return _result(
            check,
            status="not_started",
            started_at=started_at,
            start_time=start_time,
            command=command,
            cwd=str(working_directory),
            error=f"command could not start: {exc}",
        )
    return _result(
        check,
        status="passed" if completed.returncode == 0 else "failed",
        started_at=started_at,
        start_time=start_time,
        command=command,
        cwd=str(working_directory),
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
