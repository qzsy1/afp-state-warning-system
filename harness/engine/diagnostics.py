from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


PYTHON_LOCATION = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')
NODE_LOCATION = re.compile(r"(?P<file>(?:[A-Za-z]:)?[^\s():]+\.(?:js|mjs|cjs|ts)):(?P<line>\d+)(?::\d+)?")
UNITTEST_NAME = re.compile(r"^(?P<name>test\S+) \([^\r\n]+\) \.{3} (?:FAIL|ERROR)$", re.MULTILINE)


@dataclass(frozen=True)
class DiagnosticIssue:
    category: str
    severity: str
    check_id: str
    title: str
    message: str
    test_name: str | None
    file: str | None
    line: int | None
    exit_code: int | None
    log_path: str
    rerun_command: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _location(text: str) -> tuple[str | None, int | None]:
    matches = list(PYTHON_LOCATION.finditer(text))
    match = matches[-1] if matches else NODE_LOCATION.search(text)
    if not match:
        return None, None
    return match.group("file"), int(match.group("line"))


def issue_from_check(check: Mapping[str, Any], log_path: Path, rerun_command: str) -> DiagnosticIssue | None:
    status = str(check.get("status", ""))
    if status == "passed":
        return None
    output = "\n".join(str(check.get(key, "")) for key in ("error", "stderr", "stdout"))
    file, line = _location(output)
    test_match = UNITTEST_NAME.search(output)
    categories = {
        "timed_out": "timeout",
        "not_started": "environment",
        "pending_field": "field_pending",
        "failed": "test_failure",
    }
    category = categories.get(status, "runtime_error")
    message = str(check.get("error") or "").strip()
    if not message:
        useful = [line_text.strip() for line_text in output.splitlines() if line_text.strip()]
        message = useful[-1] if useful else f"check ended with status {status}"
    return DiagnosticIssue(
        category=category,
        severity="error" if check.get("blocking", False) else "warning",
        check_id=str(check.get("check_id", "unknown")),
        title=str(check.get("title", check.get("check_id", "unknown"))),
        message=message,
        test_name=test_match.group("name") if test_match else None,
        file=file,
        line=line,
        exit_code=check.get("exit_code"),
        log_path=str(log_path),
        rerun_command=rerun_command,
    )


def issues_from_report(payload: Mapping[str, Any], run_dir: Path) -> tuple[DiagnosticIssue, ...]:
    issues: list[DiagnosticIssue] = []
    checks = payload.get("checks", [])
    for check in checks if isinstance(checks, Sequence) else []:
        if not isinstance(check, Mapping):
            continue
        check_id = str(check.get("check_id", "unknown"))
        log_path = run_dir / f"{check_id}.log"
        issue = issue_from_check(check, log_path, f"harness\\checks\\{check_id}.cmd")
        if issue:
            issues.append(issue)
    for message in payload.get("unverified_items", []):
        if any(issue.message == str(message) or issue.title == str(message) for issue in issues):
            continue
        issues.append(DiagnosticIssue(
            "release_policy", "warning", "release-policy", "Release policy",
            str(message), None, None, None, None, str(run_dir / "state.json"),
            "harness\\release.cmd",
        ))
    return tuple(issues)
