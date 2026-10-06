from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from harness.engine.diagnostics import DiagnosticIssue, issues_from_report
from harness.engine.error_report import invalidate_latest, write_error_report, write_machine_logs
from harness.engine.matrix import load_matrix
from harness.engine.models import GateConfigError
from harness.engine.preflight import format_preflight, run_preflight
from harness.engine.profiles import load_profiles, select_profile_checks, select_single_check
from harness.engine.quality_gate import EXIT_CONFIG, EXIT_REPORT_WRITE, main as quality_gate_main
from harness.engine.reporting import ReportWriteError


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "harness" / "config" / "regression-matrix.json"
PROFILES = ROOT / "harness" / "config" / "profiles.json"
RULES = ROOT / "harness" / "config" / "exe-rebuild-rules.json"
SETTINGS = ROOT / "harness" / "config" / "local-settings.json"
LOGS = ROOT / "harness" / "logs"
REPORTS = ROOT / "harness" / "reports"
ALLOWED_SETTING_KEYS = {"python_path", "baseline_exe", "baseline_exe_sha256"}


class HarnessArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise GateConfigError(message)


def _parser() -> HarnessArgumentParser:
    parser = HarnessArgumentParser(description="AFP manual regression harness")
    parser.add_argument("--environment", choices=("local", "ci"), default="local")
    parser.add_argument("--open-report", action="store_true")
    parser.add_argument("--base-ref")
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--baseline-exe")
    parser.add_argument("--baseline-exe-sha256")
    parser.add_argument("--matrix", default=str(MATRIX))
    parser.add_argument("--profiles", default=str(PROFILES))
    parser.add_argument("--exe-rules", default=str(RULES))
    parser.add_argument("--log-root", default=str(LOGS))
    parser.add_argument("--report-dir", default=str(REPORTS))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    check = sub.add_parser("check")
    check.add_argument("check_id")
    profile = sub.add_parser("profile")
    profile.add_argument("profile_name", choices=("quick", "full", "release"))
    sub.add_parser("environment")
    setup = sub.add_parser("setup")
    setup.add_argument("--python-path")
    setup.add_argument("--exe-path")
    return parser


def load_local_settings(path: Path = SETTINGS) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateConfigError(f"local settings are not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict) or not set(payload).issubset(ALLOWED_SETTING_KEYS):
        raise GateConfigError("local settings may contain only python_path, baseline_exe, and baseline_exe_sha256")
    if not all(isinstance(value, str) for value in payload.values()):
        raise GateConfigError("all local setting values must be strings")
    return payload


def save_local_settings(python_path: str, exe_path: str, path: Path = SETTINGS) -> Path:
    exe = Path(exe_path)
    if not exe.is_absolute():
        exe = ROOT / exe
    if not exe.is_file():
        raise GateConfigError(f"baseline EXE does not exist: {exe}")
    payload = {
        "python_path": python_path,
        "baseline_exe": str(exe.resolve()),
        "baseline_exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _run_id(target: str) -> str:
    safe = target.replace(":", "-")
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{safe}"


def _preflight_issues_to_diagnostics(issues: Sequence[Any], run_dir: Path) -> tuple[DiagnosticIssue, ...]:
    return tuple(DiagnosticIssue(
        category="environment", severity="error", check_id="environment",
        title=issue.item, message=f"{issue.message} Fix: {issue.suggestion}",
        test_name=None, file=None, line=None, exit_code=None,
        log_path=str(run_dir / "state.json"), rerun_command="harness\\checks\\00_environment.cmd",
    ) for issue in issues)


def _latest_json(run_dir: Path) -> Path:
    files = sorted(run_dir.glob("quality-gate-*.json"), key=lambda item: item.stat().st_mtime_ns)
    if not files:
        raise ReportWriteError(f"quality gate did not create machine state in {run_dir}")
    return files[-1]


def _open(path: Path | None, enabled: bool) -> None:
    if enabled and path is not None:
        webbrowser.open(path.resolve().as_uri())


def _target_checks(args: argparse.Namespace, matrix: Any, profiles: dict[str, tuple[str, ...]]) -> tuple[Any, ...]:
    if args.command == "check":
        return (select_single_check(matrix, args.check_id, args.environment),)
    if args.command == "profile":
        return select_profile_checks(matrix, profiles, args.profile_name, args.environment)
    return tuple(check for check in matrix.checks if args.environment in check.environments)


def _environment_command(args: argparse.Namespace, matrix: Any, profiles: dict[str, tuple[str, ...]], settings: Mapping[str, str]) -> int:
    checks = _target_checks(args, matrix, profiles)
    baseline = args.baseline_exe or settings.get("baseline_exe")
    issues = run_preflight(ROOT, checks, environment=args.environment, baseline_exe=baseline)
    print(format_preflight(issues))
    return EXIT_CONFIG if issues else 0


def _setup(args: argparse.Namespace) -> int:
    python_path = args.python_path or str(ROOT / ".venv" / "Scripts" / "python.exe")
    exe_path = args.exe_path
    if not exe_path:
        default_exes = sorted((ROOT / "delivery").glob("**/*.exe")) if (ROOT / "delivery").is_dir() else []
        default = str(default_exes[0]) if default_exes else ""
        exe_path = input(f"Trusted baseline EXE path [{default}]: ").strip() or default
    path = save_local_settings(python_path, exe_path)
    print(f"Local settings saved: {path}")
    print("No passwords, API keys, or tokens were stored.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "setup":
            return _setup(args)
        matrix_path = Path(args.matrix)
        profiles_path = Path(args.profiles)
        rules_path = Path(args.exe_rules)
        log_root = Path(args.log_root)
        report_dir = Path(args.report_dir)
        matrix = load_matrix(matrix_path)
        profiles = load_profiles(profiles_path)
        settings = load_local_settings()
        if args.command == "list":
            for check in matrix.checks:
                print(f"{check.id}\t{check.title}\t{check.evidence_tier}")
            return 0
        if args.command == "environment":
            return _environment_command(args, matrix, profiles, settings)
        checks = _target_checks(args, matrix, profiles)
    except GateConfigError as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    target = args.check_id if args.command == "check" else args.profile_name
    run_dir = log_root / _run_id(target)
    baseline_exe = args.baseline_exe or settings.get("baseline_exe")
    baseline_sha = args.baseline_exe_sha256 or settings.get("baseline_exe_sha256")
    print("=" * 68, flush=True)
    print(f"AFP Harness started: {target}", flush=True)
    print(f"Checks selected: {len(checks)}", flush=True)
    print(f"Machine logs: {run_dir}", flush=True)
    print("Running environment preflight...", flush=True)
    preflight = run_preflight(ROOT, checks, environment=args.environment, baseline_exe=baseline_exe)
    if preflight:
        print(format_preflight(preflight), file=sys.stderr)
        payload: dict[str, object] = {
            "profile": target, "outcome": "environment_failed",
            "preflight": [issue.to_dict() for issue in preflight], "checks": [],
        }
        try:
            write_machine_logs(payload, run_dir)
            report = write_error_report(_preflight_issues_to_diagnostics(preflight, run_dir), report_dir, payload)
            _open(report, args.open_report)
        except ReportWriteError as exc:
            print(f"harness: {exc}", file=sys.stderr)
            return EXIT_REPORT_WRITE
        return EXIT_CONFIG

    print("Environment preflight passed. Starting checks now.", flush=True)

    gate_args = [
        "--environment", args.environment,
        "--matrix", str(matrix_path), "--profiles", str(profiles_path),
        "--exe-rules", str(rules_path), "--report-dir", str(run_dir),
    ]
    if args.command == "check":
        gate_args.extend(("--check", args.check_id))
    else:
        gate_args.extend(("--profile", args.profile_name))
    if args.base_ref:
        gate_args.extend(("--base-ref", args.base_ref))
    for path in args.changed_file:
        gate_args.extend(("--changed-file", path))
    if baseline_exe:
        gate_args.extend(("--baseline-exe", baseline_exe))
    if baseline_sha:
        gate_args.extend(("--baseline-exe-sha256", baseline_sha))
    exit_code = quality_gate_main(gate_args, repo_root=ROOT, environ=os.environ)
    try:
        payload = json.loads(_latest_json(run_dir).read_text(encoding="utf-8"))
        write_machine_logs(payload, run_dir)
        issues = issues_from_report(payload, run_dir)
        report = write_error_report(issues, report_dir, payload)
        _open(report, args.open_report)
    except (OSError, ValueError, json.JSONDecodeError, ReportWriteError) as exc:
        print(f"harness: could not finalize reports: {exc}", file=sys.stderr)
        return EXIT_REPORT_WRITE
    passed = sum(1 for check in payload.get("checks", []) if check.get("status") == "passed")
    total = len(payload.get("checks", []))
    duration = sum(float(check.get("duration_seconds", 0)) for check in payload.get("checks", []))
    print(f"Harness summary: {passed}/{total} passed, {duration:.2f}s")
    if issues:
        print(f"Problems: {len(issues)}; report: {report_dir / 'latest-errors.html'}")
    else:
        print("No problem report was generated.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
