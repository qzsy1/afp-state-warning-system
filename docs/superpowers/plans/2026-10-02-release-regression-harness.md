# Release Regression Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved balanced (`B`) release regression harness with quick, full, and release profiles, traceable requirement coverage, fail-closed execution, stable-EXE reuse policy, reproducible reports, and a Windows/GitHub entry point.

**Architecture:** A Python-standard-library harness reads two declarative JSON files: one for requirements/check commands and one for EXE rebuild policy. Focused modules validate configuration, execute checks, collect Git and EXE evidence, aggregate requirement results, and atomically write JSON/Markdown reports; a thin CLI coordinates them and a PowerShell wrapper locates Python. Existing tests and packaged diagnostics remain the source of product evidence.

**Tech Stack:** Python 3 standard library, `unittest`, JSON, PowerShell 7/Windows PowerShell, Git, GitHub Actions.

**Spec:** `openspec/changes/release-regression-harness/design.md` plus the three capability specs below `openspec/changes/release-regression-harness/specs/`.

## Global Constraints

- All implementation and documentation remains inside `D:/AFP_Integrated_Modular_v2`.
- Use no new Python dependency for the harness.
- Required check failure, timeout, launch failure, invalid matrix, incomplete mandatory coverage, or report-write failure returns a nonzero process code.
- `quick`, `full`, and `release` are the only accepted profiles; each later profile includes the earlier profile's mandatory capability coverage.
- Evidence tiers are exactly `automated`, `local_integration`, `field`, and `unverified`; lower tiers never claim a higher-tier result.
- Ordinary Python, HTML, CSS, JavaScript, SQL, configuration, model, test, and documentation changes default to stable-EXE reuse.
- The harness never builds, copies, replaces, or deletes an EXE.
- Runtime results go below ignored `verification/results/`; source, tests, matrices, workflow, and documentation remain tracked.
- Existing dirty-worktree changes are user-owned and must not be overwritten or reformatted.

## Review Focus

- A malformed or partly written matrix must fail before any product command runs; Task 1 tests missing fields, duplicates, unknown profiles, and invalid coverage references.
- A command that cannot start, hangs, or emits undecodable output must become a recorded failed check; Task 2 tests all three cases.
- Output containing credentials must be redacted in memory before either report is written; Task 3 tests known environment-secret values and key/value text forms.
- Overlapping or unknown EXE file patterns must resolve conservatively (`rebuild_required` wins; unmatched is `manual_review`); Task 4 pins precedence and unknown handling.
- A stale report or dirty release workspace must not yield a release pass; Tasks 3 and 5 test current commit identity and dirty-worktree blocking.

---

### Task 1: Matrix schema and requirement coverage

**Files:**
- Create: `tools/verification/__init__.py`
- Create: `tools/verification/models.py`
- Create: `tools/verification/matrix.py`
- Create: `tools/verification/tests/__init__.py`
- Create: `tools/verification/tests/test_matrix.py`

**Interfaces:**
- Produces: `GateConfigError`, `RequirementSpec`, `CheckSpec`, `RegressionMatrix`, `load_matrix(path: Path) -> RegressionMatrix`, and `select_checks(matrix: RegressionMatrix, profile: str, environment: str, changed_files: Sequence[str] = ()) -> tuple[CheckSpec, ...]`.
- Matrix top-level keys: `schema_version`, `requirements`, `profiles`, `checks`.
- Check keys: `id`, `title`, `kind`, `command`, `cwd`, `timeout_seconds`, `profiles`, `environments`, `blocking`, `evidence_tier`, `requirements`, and optional `change_patterns` for quick-profile impact selection.

- [ ] Write unit tests for missing matrix, malformed JSON, missing/unknown fields, duplicate IDs, unknown profile/environment/evidence tier, references to unknown requirements, quick change-pattern selection, and mandatory requirements without a selected validation item.
- [ ] Run `python -m unittest tools.verification.tests.test_matrix -v` and confirm failures are caused by missing production modules.
- [ ] Implement immutable dataclasses, strict JSON loading, normalized repository-relative paths, and profile/environment selection.
- [ ] Run the matrix tests and confirm they pass.

### Task 2: Fail-closed command execution

**Files:**
- Create: `tools/verification/runner.py`
- Create: `tools/verification/tests/test_runner.py`

**Interfaces:**
- Consumes: `CheckSpec` from Task 1.
- Produces: `CheckResult` and `run_check(check: CheckSpec, repo_root: Path, inherited_env: Mapping[str, str]) -> CheckResult`.
- Result statuses: `passed`, `failed`, `timed_out`, `not_started`, `pending_field`, `skipped_environment`.

- [ ] Write tests using temporary Python commands for success, nonzero exit, timeout, missing executable, invalid working directory, UTF-8/non-UTF-8 output, and field/manual checks that do not execute commands.
- [ ] Run `python -m unittest tools.verification.tests.test_runner -v` and observe the expected missing-module failure.
- [ ] Implement subprocess execution with explicit argument arrays, bounded output capture, elapsed time, and fail-closed exception handling.
- [ ] Run runner and matrix tests and confirm they pass.

### Task 3: Git evidence, aggregation, redaction, and reports

**Files:**
- Create: `tools/verification/evidence.py`
- Create: `tools/verification/reporting.py`
- Create: `tools/verification/tests/test_reporting.py`

**Interfaces:**
- Produces: `collect_git_evidence(repo_root: Path) -> GitEvidence`, `redact_text(text: str, environment: Mapping[str, str]) -> str`, `aggregate_requirements(...) -> tuple[RequirementResult, ...]`, and `write_reports(report: GateReport, output_dir: Path) -> tuple[Path, Path]`.
- Reports contain schema version, profile, environment, start/end UTC timestamps, Git commit/branch/status, interpreter, check commands/results, requirement counts/results, EXE decision, unverified items, and overall outcome.

- [ ] Write tests for clean/dirty Git evidence, requirement aggregation, blocking versus informational failures, secret redaction, atomic JSON/Markdown output, report-write failure, and report commit identity.
- [ ] Run `python -m unittest tools.verification.tests.test_reporting -v` and observe the expected missing-module failure.
- [ ] Implement Git evidence and deterministic aggregation, then redact before building either serialization.
- [ ] Implement atomic report writes using sibling temporary files and `Path.replace()`.
- [ ] Run Tasks 1-3 tests and confirm they pass.

### Task 4: Stable EXE reuse policy

**Files:**
- Create: `tools/verification/exe_policy.py`
- Create: `verification/exe-rebuild-rules.json`
- Create: `tools/verification/tests/test_exe_policy.py`

**Interfaces:**
- Produces: `load_exe_rules(path: Path) -> ExeRules`, `classify_changed_files(paths: Sequence[str], rules: ExeRules) -> ExeDecision`, and `sha256_file(path: Path) -> str`.
- Decisions: `reuse`, `rebuild_required`, `manual_review`; priority is rebuild, then manual, then reuse.

- [ ] Write tests for ordinary external files, launcher/runtime/native dependency triggers, overlapping patterns, mixed change sets, unmatched paths, path traversal, missing EXE, valid SHA-256, and unauthorized hash change.
- [ ] Run `python -m unittest tools.verification.tests.test_exe_policy -v` and observe the expected missing-module/config failure.
- [ ] Add conservative glob rules and implement classification without invoking any build tool.
- [ ] Implement streaming SHA-256 calculation and baseline comparison evidence.
- [ ] Run Tasks 1-4 tests and confirm they pass.

### Task 5: CLI orchestration and Harness contract

**Files:**
- Create: `tools/verification/quality_gate.py`
- Create: `tools/verification/tests/test_quality_gate_cli.py`
- Modify: `.gitignore`

**Interfaces:**
- CLI: `python tools/verification/quality_gate.py --profile {quick,full,release} [--environment {local,ci}] [--matrix PATH] [--exe-rules PATH] [--base-ref REF | --changed-file PATH ...] [--baseline-exe PATH --baseline-exe-sha256 HEX] [--report-dir PATH]`.
- Exit codes: `0` pass, `2` configuration error, `3` required check failure, `4` release-policy failure, `5` report-write failure.

- [ ] Write CLI tests for unknown profile, missing matrix, failing/timeout checks, incomplete coverage, successful quick/full runs, dirty release rejection, missing release EXE evidence, hash mismatch, manual-review decision, and report-write failure.
- [ ] Run `python -m unittest tools.verification.tests.test_quality_gate_cli -v` and verify expected failures.
- [ ] Implement orchestration in dependency order: configuration, Git/changed files, EXE policy, selected checks, aggregation, reports, exit code.
- [ ] Add `/verification/results/` to `.gitignore` and ensure failures still attempt to produce a report unless report creation itself is impossible.
- [ ] Run all harness unit tests and confirm they pass.

### Task 6: Balanced regression matrix

**Files:**
- Create: `verification/regression-matrix.json`
- Create: `tools/verification/tests/test_regression_matrix_contract.py`

**Interfaces:**
- Requirement families: `HARNESS`, `STARTUP`, `SIMULATION`, `MODE_ISOLATION`, `INTERFACES`, `EDGE_HELPER`, `CSV`, `MYSQL`, `DIAGNOSIS`, `LAN_PUBLIC`, `WEBSOCKET`, `MODEL_CHANNELS`, `PREDICTION_WARNING`, `EXE_REUSE`, `RELEASE_EVIDENCE`, `FIELD_BOUNDARY`.
- Quick always runs Harness contracts and adds low-cost functional groups whose `change_patterns` match the changed-file set; an empty or unrecognized set selects the safe quick baseline. Full runs all portable automated groups; release adds packaged diagnostics and field/manual evidence rows.

- [ ] Write a matrix contract test that loads the real matrix and asserts every spec MUST requirement maps to a check, all referenced commands/working directories exist, CI checks require neither hardware nor credentials, and release contains the five packaged diagnostic modes.
- [ ] Run the contract test and observe failure because the matrix does not exist.
- [ ] Populate checks by grouping existing `unittest` modules for startup/frontend, simulation/mode isolation, interfaces/M3232, edge/helper, CSV/MySQL, diagnosis, LAN/public, WebSocket, model channels, and prediction/warning.
- [ ] Add release command entries for `--module-status`, `--self-test`, `--verify-files`, `--integration-smoke`, and `--functional-smoke`; mark real devices/target MySQL as `field` evidence, never automatic pass.
- [ ] Run the real matrix contract and harness unit suites.

### Task 7: Windows entry, GitHub check, and documentation contract

**Files:**
- Create: `tools/verification/run_quality_gate.ps1`
- Create: `.github/workflows/quality-gate.yml`
- Create: `docs/quality-gate.md`
- Create: `tools/verification/tests/test_entrypoint_contract.py`

**Interfaces:**
- PowerShell parameters mirror the Python CLI and propagate its exact exit code.
- GitHub job name is `quality-gate`; it executes Harness self-tests and the `full/ci` profile on Windows.

- [ ] Write static contract tests for Python discovery order, argument forwarding, workflow use of the single Harness entry point, branch-protection guidance, evidence boundaries, and absence of embedded secrets.
- [ ] Run the contract test and observe expected missing-file failures.
- [ ] Implement the PowerShell wrapper, workflow, and operator documentation.
- [ ] Run entrypoint contract tests and all harness tests.

### Task 8: End-to-end local verification and OpenSpec completion evidence

**Files:**
- Modify: `openspec/changes/release-regression-harness/tasks.md`
- Generated/ignored: `verification/results/*.json`
- Generated/ignored: `verification/results/*.md`

**Interfaces:**
- Consumes the completed harness, real matrices, and current repository tests.
- Produces one current local `full` report; release runs only when a stable EXE path and baseline hash are available.

- [ ] Run `python -m unittest discover -s tools/verification/tests -v`; require zero failures.
- [ ] Run `powershell -ExecutionPolicy Bypass -File tools/verification/run_quality_gate.ps1 -Profile quick`; require exit code 0 and both report formats.
- [ ] Run `powershell -ExecutionPolicy Bypass -File tools/verification/run_quality_gate.ps1 -Profile full`; record exact pass/fail/skip counts and duration without suppressing existing product failures.
- [ ] Run the repository's existing test command or the full matrix command and report every pre-existing failure by name.
- [ ] Run OpenSpec strict validation after restoring a working Node/OpenSpec invocation; if unavailable, record the exact environment blocker and leave task 7.1 unchecked.
- [ ] Inspect `git diff --check`, `git status --short`, and generated paths; confirm no credentials, EXE, delivery copy, or runtime result is tracked.
- [ ] Mark only fully completed OpenSpec tasks as checked and leave hardware/GitHub-remote/branch-protection tasks unchecked when their external evidence is unavailable.
