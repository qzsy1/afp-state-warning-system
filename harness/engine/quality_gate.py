from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from harness.engine.evidence import (  # noqa: E402
    GitEvidenceError,
    changed_files_from_git,
    collect_git_evidence,
)
from harness.engine.exe_policy import (  # noqa: E402
    ExePolicyError,
    classify_changed_files,
    load_exe_rules,
    verify_reuse_hash,
)
from harness.engine.matrix import load_matrix, select_checks  # noqa: E402
from harness.engine.models import GateConfigError  # noqa: E402
from harness.engine.profiles import (  # noqa: E402
    load_profiles,
    select_profile_checks,
    select_single_check,
)
from harness.engine.reporting import (  # noqa: E402
    GateReport,
    ReportWriteError,
    aggregate_requirements,
    write_reports,
)
from harness.engine.runner import run_check  # noqa: E402


EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_CHECK_FAILED = 3
EXIT_RELEASE_POLICY = 4
EXIT_REPORT_WRITE = 5


class GateArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise GateConfigError(message)


def _parser() -> GateArgumentParser:
    parser = GateArgumentParser(description="Run the AFP release regression quality gate")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--profile", choices=("quick", "full", "release"))
    target.add_argument("--check")
    parser.add_argument("--environment", default="local", choices=("local", "ci"))
    parser.add_argument("--matrix", default="harness/config/regression-matrix.json")
    parser.add_argument("--profiles")
    parser.add_argument("--exe-rules", default="harness/config/exe-rebuild-rules.json")
    parser.add_argument("--report-dir", default="harness/reports")
    changed = parser.add_mutually_exclusive_group()
    changed.add_argument("--base-ref")
    changed.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--baseline-exe")
    parser.add_argument("--baseline-exe-sha256")
    return parser


def _path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _status_changed_files(status: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in status.splitlines():
        value = line[3:].strip() if len(line) > 3 else ""
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        if value:
            paths.append(value.strip('"').replace("\\", "/"))
    return tuple(dict.fromkeys(paths))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _print_error(message: str) -> None:
    print(f"quality-gate: {message}", file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    root = Path(repo_root or Path(__file__).resolve().parents[2]).resolve()
    environment = dict(os.environ if environ is None else environ)
    started_at = _now()
    try:
        args = _parser().parse_args(argv)
        matrix = load_matrix(_path(root, args.matrix))
        rules = load_exe_rules(_path(root, args.exe_rules))
        git = collect_git_evidence(root)
        if args.changed_file:
            changed_files = tuple(args.changed_file)
        elif args.base_ref:
            changed_files = changed_files_from_git(root, args.base_ref)
        else:
            changed_files = _status_changed_files(git.status)
        exe_decision = classify_changed_files(changed_files, rules)
        if args.check:
            selected = (select_single_check(matrix, args.check, args.environment),)
        elif args.profiles:
            profiles = load_profiles(_path(root, args.profiles))
            selected = select_profile_checks(matrix, profiles, args.profile, args.environment)
        else:
            selected = select_checks(matrix, args.profile, args.environment, changed_files)
    except (GateConfigError, GitEvidenceError) as exc:
        _print_error(str(exc))
        return EXIT_CONFIG

    command_variables: dict[str, str] = {}
    if args.baseline_exe:
        command_variables["{baseline_exe}"] = str(_path(root, args.baseline_exe).resolve())
    print(f"quality-gate: selected {len(selected)} check(s); execution is starting", flush=True)
    collected_results = []
    for index, check in enumerate(selected, start=1):
        print(
            f"[{index}/{len(selected)}] START {check.id} - {check.title} "
            f"(timeout {check.timeout_seconds:g}s)",
            flush=True,
        )
        print("  The command may be quiet while its output is being captured.", flush=True)
        result = run_check(check, root, environment, command_variables=command_variables)
        collected_results.append(result)
        print(
            f"[{index}/{len(selected)}] {result.status.upper()} {check.id} "
            f"({result.duration_seconds:.2f}s, exit={result.exit_code})",
            flush=True,
        )
    results = tuple(collected_results)
    selected_requirement_ids = {item for check in selected for item in check.requirements}
    active_requirements = tuple(
        requirement for requirement in matrix.requirements
        if (args.profile and args.profile in requirement.profiles)
        or (args.check and requirement.id in selected_requirement_ids)
    )
    requirement_results = aggregate_requirements(active_requirements, results)
    release_issues: list[str] = []
    hash_evidence = None
    if args.profile == "release":
        if git.dirty:
            release_issues.append("release profile cannot pass with a dirty worktree")
        if exe_decision.decision == "manual_review":
            release_issues.append("changed files require manual EXE rebuild review")
        elif exe_decision.decision == "rebuild_required":
            release_issues.append("changed files require an authorized EXE rebuild and a new release run")
        elif not args.baseline_exe or not args.baseline_exe_sha256:
            release_issues.append("stable reuse requires baseline EXE path and baseline EXE SHA-256")
        else:
            try:
                hash_evidence = verify_reuse_hash(
                    _path(root, args.baseline_exe),
                    args.baseline_exe_sha256,
                )
                if not hash_evidence.matches:
                    release_issues.append("baseline EXE SHA-256 does not match the current EXE")
            except ExePolicyError as exc:
                release_issues.append(str(exc))

    failing_statuses = {"failed", "timed_out", "not_started"}
    required_failures = [
        result
        for result in results
        if result.blocking
        and (result.status in failing_statuses or result.status == "pending_field")
    ]
    unverified = [
        result.title
        for result in results
        if result.status == "pending_field"
        or (result.evidence_tier == "unverified" and result.status != "passed")
    ]
    unverified.extend(release_issues)
    if release_issues:
        outcome = "release_policy_failed"
        exit_code = EXIT_RELEASE_POLICY
    elif required_failures:
        outcome = "failed"
        exit_code = EXIT_CHECK_FAILED
    else:
        outcome = "passed"
        exit_code = EXIT_OK
    exe_payload = {
        "decision": exe_decision.decision,
        "changed_files": list(exe_decision.changed_files),
        "matches": [asdict(match) for match in exe_decision.matches],
        "reasons": list(exe_decision.reasons),
        "hash_evidence": asdict(hash_evidence) if hash_evidence is not None else None,
    }
    report = GateReport(
        schema_version=1,
        profile=args.profile or f"check:{args.check}",
        environment=args.environment,
        started_at=started_at,
        ended_at=_now(),
        git=git,
        interpreter=sys.executable,
        checks=results,
        requirements=requirement_results,
        exe_decision=exe_payload,
        unverified_items=tuple(unverified),
        outcome=outcome,
    )
    try:
        json_path, markdown_path = write_reports(report, _path(root, args.report_dir), environment)
    except ReportWriteError as exc:
        _print_error(str(exc))
        return EXIT_REPORT_WRITE
    print(f"quality-gate: {outcome} ({len(results)} checks)")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
