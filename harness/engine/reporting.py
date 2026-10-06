from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence import GitEvidence
from .models import RequirementSpec
from .runner import CheckResult


SECRET_NAME = re.compile(r"(?:api[_-]?key|token|password|passwd|secret|credential)", re.IGNORECASE)
SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?<![\w-])
    (?P<key_quote>["']?)
    (?P<key>api[_ -]?key|token|password|passwd|secret|credential)
    (?P=key_quote)
    (?![\w-])
    (?P<separator>\s*[:=]\s*)
    (?P<value>
        "(?:\\.|[^"\\\r\n])*"
        | '(?:\\.|[^'\\\r\n])*'
        | [^\s,;}\]&]+
    )
    """
)


class ReportWriteError(RuntimeError):
    """Raised when the audit reports cannot be written safely."""


@dataclass(frozen=True)
class RequirementResult:
    id: str
    title: str
    mandatory: bool
    status: str
    check_ids: tuple[str, ...]
    failed_check_ids: tuple[str, ...]
    evidence_tiers: tuple[str, ...]


@dataclass(frozen=True)
class GateReport:
    schema_version: int
    profile: str
    environment: str
    started_at: str
    ended_at: str
    git: GitEvidence
    interpreter: str
    checks: tuple[CheckResult, ...]
    requirements: tuple[RequirementResult, ...]
    exe_decision: Any
    unverified_items: tuple[str, ...]
    outcome: str


def redact_text(text: str, environment: Mapping[str, str]) -> str:
    result = text
    secret_values = {
        value
        for name, value in environment.items()
        if SECRET_NAME.search(name) and isinstance(value, str) and len(value) >= 4
    }
    for value in sorted(secret_values, key=len, reverse=True):
        result = result.replace(value, "[REDACTED]")

    def mask_assignment(match: re.Match[str]) -> str:
        value = match.group("value")
        value_quote = (
            value[0]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}
            else ""
        )
        return (
            f"{match.group('key_quote')}{match.group('key')}{match.group('key_quote')}"
            f"{match.group('separator')}{value_quote}[REDACTED]{value_quote}"
        )

    return SECRET_ASSIGNMENT.sub(mask_assignment, result)


def aggregate_requirements(
    requirements: Sequence[RequirementSpec],
    checks: Sequence[CheckResult],
) -> tuple[RequirementResult, ...]:
    aggregated: list[RequirementResult] = []
    failure_statuses = {"failed", "timed_out", "not_started"}
    for requirement in requirements:
        related = tuple(check for check in checks if requirement.id in check.requirements)
        blocking_failures = tuple(
            check for check in related if check.blocking and check.status in failure_statuses
        )
        warnings = tuple(
            check for check in related if not check.blocking and check.status in failure_statuses
        )
        if blocking_failures:
            status = "failed"
        elif any(check.status == "passed" for check in related):
            status = "passed_with_warnings" if warnings else "passed"
        elif any(check.status == "pending_field" for check in related):
            status = "pending_field"
        else:
            status = "unverified"
        aggregated.append(
            RequirementResult(
                id=requirement.id,
                title=requirement.title,
                mandatory=requirement.mandatory,
                status=status,
                check_ids=tuple(check.check_id for check in related),
                failed_check_ids=tuple(
                    check.check_id for check in related if check.status in failure_statuses
                ),
                evidence_tiers=tuple(sorted({check.evidence_tier for check in related})),
            )
        )
    return tuple(aggregated)


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _redact_value(value: Any, environment: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, environment)
    if isinstance(value, list):
        return [_redact_value(item, environment) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, environment) for key, item in value.items()}
    return value


def report_payload(report: GateReport, environment: Mapping[str, str]) -> dict[str, Any]:
    payload = _plain(report)
    check_counts = Counter(check.status for check in report.checks)
    requirement_counts = Counter(item.status for item in report.requirements)
    payload["summary"] = {
        "total": len(report.checks),
        "passed": check_counts["passed"],
        "failed": check_counts["failed"] + check_counts["timed_out"] + check_counts["not_started"],
        "skipped": check_counts["skipped_environment"],
        "pending_field": check_counts["pending_field"],
        "requirements_total": len(report.requirements),
        "requirements_passed": requirement_counts["passed"] + requirement_counts["passed_with_warnings"],
        "requirements_failed": requirement_counts["failed"],
        "requirements_unverified": requirement_counts["unverified"],
    }
    return _redact_value(payload, environment)


def _markdown(payload: dict[str, Any]) -> str:
    git = payload["git"]
    summary = payload["summary"]
    lines = [
        f"# Quality Gate: {payload['outcome']}",
        "",
        f"- Profile: `{payload['profile']}`",
        f"- Environment: `{payload['environment']}`",
        f"- Commit: `{git['commit']}`",
        f"- Branch: `{git['branch']}`",
        f"- Dirty worktree: `{git['dirty']}`",
        f"- Started: `{payload['started_at']}`",
        f"- Ended: `{payload['ended_at']}`",
        f"- Checks: {summary['passed']} passed, {summary['failed']} failed, {summary['pending_field']} field pending",
        "",
        "## Checks",
        "",
        "| ID | Status | Tier | Exit | Duration (s) |",
        "|---|---|---|---:|---:|",
    ]
    for check in payload["checks"]:
        exit_code = "" if check["exit_code"] is None else str(check["exit_code"])
        lines.append(
            f"| {check['check_id']} | {check['status']} | {check['evidence_tier']} | {exit_code} | {check['duration_seconds']} |"
        )
    lines.extend(["", "## Requirement coverage", "", "| ID | Status | Checks |", "|---|---|---|"])
    for requirement in payload["requirements"]:
        lines.append(
            f"| {requirement['id']} | {requirement['status']} | {', '.join(requirement['check_ids'])} |"
        )
    lines.extend(["", "## Unverified items", ""])
    if payload["unverified_items"]:
        lines.extend(f"- {item}" for item in payload["unverified_items"])
    else:
        lines.append("- None")
    lines.extend(["", "## Git status", "", "```text", git["status"] or "clean", "```", ""])
    return "\n".join(lines)


def write_reports(
    report: GateReport,
    output_dir: Path,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    environment = os.environ if environment is None else environment
    payload = report_payload(report, environment)
    try:
        timestamp = datetime.fromisoformat(report.started_at).strftime("%Y%m%d%H%M%S%f")
    except ValueError:
        timestamp = re.sub(r"[^0-9]", "", report.started_at) or "unknown"
    safe_profile = re.sub(r"[^A-Za-z0-9._-]+", "-", report.profile).strip("-") or "unknown"
    stem = f"quality-gate-{safe_profile}-{report.git.commit[:7]}-{timestamp}"
    output_dir = Path(output_dir)
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    token = uuid.uuid4().hex
    json_temp = output_dir / f".{stem}.{token}.json.tmp"
    markdown_temp = output_dir / f".{stem}.{token}.md.tmp"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        markdown_temp.write_text(_markdown(payload), encoding="utf-8")
        json_temp.replace(json_path)
        markdown_temp.replace(markdown_path)
    except (OSError, TypeError, ValueError) as exc:
        for temp in (json_temp, markdown_temp):
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        raise ReportWriteError(f"could not write quality-gate reports: {exc}") from exc
    return json_path, markdown_path
