"""Composable, fail-closed pre-sensor readiness orchestration.

The module has no dependency on the development Harness so the same report
model can run from an assembled field candidate.
"""

from __future__ import annotations

import json
import os
import queue
import ctypes
import hashlib
import importlib.util
import struct
import shutil
import uuid
import socket
import subprocess
import re
import locale
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from pathlib import Path


_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))


READINESS_STATUSES = frozenset(
    {"passed", "failed", "blocked", "physical_sensor_pending"}
)

_WINDOWS_REMEDIATION_SCRIPT = """# AFP Windows readiness remediation review
# Review only: this script reads state and never changes firewall or power policy.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Write-Host "Review only - no system setting will be changed."
Get-NetFirewallProfile | Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction
netsh winhttp show proxy
powercfg /getactivescheme
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
powercfg /query SCHEME_CURRENT SUB_USB USBSELECTIVE
Get-NetAdapterPowerManagement -ErrorAction Continue | Format-Table -AutoSize
Get-Service W32Time | Select-Object Name, Status, StartType
w32tm /query /status
Write-Host "Any suggested change requires separate administrator approval."
"""


class ExternalEnvironmentBlocked(RuntimeError):
    def __init__(self, evidence: str, remediation: str) -> None:
        super().__init__(evidence)
        self.evidence = evidence
        self.remediation = remediation


class PhysicalSensorPending(RuntimeError):
    def __init__(self, evidence: str, remediation: str) -> None:
        super().__init__(evidence)
        self.evidence = evidence
        self.remediation = remediation


@dataclass(frozen=True)
class ProbeOutcome:
    status: str
    evidence: str
    remediation: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Probe:
    check_id: str
    title: str
    runner: Callable[[Mapping[str, Any]], ProbeOutcome]
    timeout_seconds: float = 10.0
    external_environment: bool = False
    physical_sensor: bool = False

    def __post_init__(self) -> None:
        if not self.check_id.strip() or not self.title.strip():
            raise ValueError("probe check_id and title are required")
        if self.timeout_seconds <= 0:
            raise ValueError("probe timeout_seconds must be positive")
        if self.external_environment and self.physical_sensor:
            raise ValueError("a probe cannot be both external and physical")


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    title: str
    status: str
    evidence: str
    remediation: str
    duration_seconds: float
    external_environment: bool
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreflightSummary:
    total: int
    passed: int
    failed: int
    blocked: int
    physical_sensor_pending: int
    outcome: str


@dataclass(frozen=True)
class PreflightReport:
    schema_version: int
    candidate_identity: str
    mode: str
    started_at: str
    completed_at: str
    duration_seconds: float
    summary: PreflightSummary
    checks: tuple[CheckResult, ...]


@dataclass(frozen=True)
class CapacityPolicy:
    writer_rows: int
    transport_rows: int
    queue_high_water_ratio: float
    log_max_bytes: int
    log_retention_days: int
    journal_max_bytes: int
    spool_max_bytes: int
    minimum_free_bytes: int
    warning_ratio: float
    hard_limit_ratio: float

    @classmethod
    def from_config(cls, payload: Mapping[str, Any]) -> "CapacityPolicy":
        queues = payload.get("queues")
        logging = payload.get("logging")
        capacity = payload.get("capacity")
        for name, value in (
            ("queues", queues),
            ("logging", logging),
            ("capacity", capacity),
        ):
            if not isinstance(value, Mapping):
                raise ValueError(f"{name} capacity configuration is missing")

        def positive(section: Mapping[str, Any], field_name: str) -> int:
            try:
                result = int(section.get(field_name) or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field_name} must be a positive integer") from exc
            if result <= 0:
                raise ValueError(f"{field_name} must be a positive finite limit")
            return result

        writer_rows = positive(queues, "writer_rows")
        transport_rows = positive(queues, "transport_rows")
        max_total_mb = positive(logging, "max_total_mb")
        retention_days = positive(logging, "retention_days")
        journal_mb = positive(capacity, "journal_max_mb")
        spool_mb = positive(capacity, "retry_spool_max_mb")
        minimum_free_mb = positive(capacity, "minimum_free_mb")
        try:
            queue_warning = float(queues.get("high_water_ratio"))
            warning = float(capacity.get("warning_ratio"))
            hard = float(capacity.get("hard_limit_ratio"))
        except (TypeError, ValueError) as exc:
            raise ValueError("capacity ratios must be numeric") from exc
        if not 0 < queue_warning < 1:
            raise ValueError("high_water_ratio must be between 0 and 1")
        if not 0 < warning < 1:
            raise ValueError("warning_ratio must be between 0 and 1")
        if not warning <= hard <= 1:
            raise ValueError("hard_limit_ratio must be between warning_ratio and 1")
        if queues.get("overflow_policy") != "fail_closed":
            raise ValueError("overflow_policy must be fail_closed")
        return cls(
            writer_rows=writer_rows,
            transport_rows=transport_rows,
            queue_high_water_ratio=queue_warning,
            log_max_bytes=max_total_mb * 1024 * 1024,
            log_retention_days=retention_days,
            journal_max_bytes=journal_mb * 1024 * 1024,
            spool_max_bytes=spool_mb * 1024 * 1024,
            minimum_free_bytes=minimum_free_mb * 1024 * 1024,
            warning_ratio=warning,
            hard_limit_ratio=hard,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _summary(results: Sequence[CheckResult]) -> PreflightSummary:
    counts = Counter(item.status for item in results)
    if counts["failed"]:
        outcome = "failed"
    elif counts["blocked"]:
        outcome = "blocked"
    elif counts["physical_sensor_pending"]:
        outcome = "physical_sensor_pending"
    else:
        outcome = "passed"
    return PreflightSummary(
        total=len(results),
        passed=counts["passed"],
        failed=counts["failed"],
        blocked=counts["blocked"],
        physical_sensor_pending=counts["physical_sensor_pending"],
        outcome=outcome,
    )


def _execute_probe(probe: Probe, context: Mapping[str, Any]) -> CheckResult:
    started = time.monotonic()
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put_nowait(("outcome", probe.runner(context)))
        except BaseException as exc:  # isolated and converted below
            result_queue.put_nowait(("exception", exc))

    worker = threading.Thread(
        target=invoke,
        name=f"preflight-{probe.check_id}",
        daemon=True,
    )
    worker.start()
    worker.join(probe.timeout_seconds)
    duration = time.monotonic() - started
    if worker.is_alive():
        return CheckResult(
            probe.check_id,
            probe.title,
            "failed",
            f"probe timed out after {probe.timeout_seconds:g} seconds",
            "Resolve the stalled dependency and rerun the preflight",
            duration,
            probe.external_environment,
        )
    try:
        kind, value = result_queue.get_nowait()
    except queue.Empty:
        kind, value = "exception", RuntimeError("probe returned no result")

    if kind == "exception":
        if isinstance(value, ExternalEnvironmentBlocked):
            if not probe.external_environment:
                status = "failed"
                evidence = "probe misclassified a local requirement as external: " + str(value)
                remediation = "Correct the probe classification before rerunning"
            else:
                status = "blocked"
                evidence = value.evidence
                remediation = value.remediation
        elif isinstance(value, PhysicalSensorPending):
            if not probe.physical_sensor:
                status = "failed"
                evidence = "physical_sensor_pending is not allowed for this check"
                remediation = "Correct the check classification before rerunning"
            else:
                status = "physical_sensor_pending"
                evidence = value.evidence
                remediation = value.remediation
        else:
            status = "failed"
            evidence = f"{type(value).__name__}: {value}"
            remediation = "Fix the reported probe error and rerun the preflight"
        return CheckResult(
            probe.check_id,
            probe.title,
            status,
            evidence,
            remediation,
            duration,
            probe.external_environment,
        )

    if not isinstance(value, ProbeOutcome) or value.status not in READINESS_STATUSES:
        return CheckResult(
            probe.check_id,
            probe.title,
            "failed",
            "probe returned an invalid readiness outcome",
            "Correct the probe implementation and rerun the preflight",
            duration,
            probe.external_environment,
        )
    if value.status == "physical_sensor_pending" and not probe.physical_sensor:
        return CheckResult(
            probe.check_id,
            probe.title,
            "failed",
            "physical_sensor_pending is not allowed for this check",
            "Correct the check classification before rerunning",
            duration,
            probe.external_environment,
        )
    if value.status == "blocked" and not probe.external_environment:
        return CheckResult(
            probe.check_id,
            probe.title,
            "failed",
            "blocked is reserved for declared external environment checks",
            "Return failed for local defects or mark a real external dependency",
            duration,
            probe.external_environment,
        )
    remediation = value.remediation
    if value.status != "passed" and not remediation.strip():
        return CheckResult(
            probe.check_id,
            probe.title,
            "failed",
            "non-passing probe omitted remediation",
            "Add an actionable remediation and rerun",
            duration,
            probe.external_environment,
        )
    return CheckResult(
        probe.check_id,
        probe.title,
        value.status,
        str(value.evidence),
        remediation,
        duration,
        probe.external_environment,
        dict(value.details),
    )


def run_preflight(
    probes: Sequence[Probe],
    *,
    context: Mapping[str, Any],
    candidate_identity: str,
    mode: str,
) -> PreflightReport:
    if mode not in {"local_direct", "remote_helper"}:
        raise ValueError("mode must be local_direct or remote_helper")
    if not str(candidate_identity).strip():
        raise ValueError("candidate_identity is required")
    ids = [probe.check_id for probe in probes]
    if len(ids) != len(set(ids)):
        raise ValueError("preflight probe check_id values must be unique")
    started_at = _utc_now()
    started = time.monotonic()
    results = tuple(_execute_probe(probe, context) for probe in probes)
    duration = time.monotonic() - started
    return PreflightReport(
        schema_version=1,
        candidate_identity=str(candidate_identity),
        mode=mode,
        started_at=started_at,
        completed_at=_utc_now(),
        duration_seconds=duration,
        summary=_summary(results),
        checks=results,
    )


def preflight_payload(report: PreflightReport) -> dict[str, Any]:
    return asdict(report)


def render_preflight_json(report: PreflightReport) -> str:
    return json.dumps(preflight_payload(report), ensure_ascii=False, indent=2) + "\n"


def render_preflight_markdown(report: PreflightReport) -> str:
    lines = [
        "# AFP 传感器前预检报告",
        "",
        f"- 候选版本：`{report.candidate_identity}`",
        f"- 模式：`{report.mode}`",
        f"- 总体状态：`{report.summary.outcome}`",
        f"- 检查数：{report.summary.total}",
        "",
        "| 检查 ID | 状态 | 证据 | 修复建议 |",
        "|---|---|---|---|",
    ]
    for item in report.checks:
        evidence = item.evidence.replace("|", "\\|").replace("\n", " ")
        remediation = item.remediation.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{item.check_id}` | `{item.status}` | {evidence} | {remediation} |"
        )
    return "\n".join(lines) + "\n"


def write_preflight_report(
    report: PreflightReport,
    output_directory: Path,
    *,
    basename: str = "pre-sensor-readiness",
) -> tuple[Path, Path]:
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / f"{basename}.json"
    markdown_path = output / f"{basename}.md"
    json_temporary = output / f".{basename}.json.tmp"
    markdown_temporary = output / f".{basename}.md.tmp"
    try:
        json_temporary.write_text(render_preflight_json(report), encoding="utf-8")
        markdown_temporary.write_text(
            render_preflight_markdown(report), encoding="utf-8"
        )
        os.replace(json_temporary, json_path)
        os.replace(markdown_temporary, markdown_path)
    finally:
        for path in (json_temporary, markdown_temporary):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return json_path, markdown_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _native_machine(path: Path) -> int:
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ValueError("missing MZ header")
            stream.seek(0x3C)
            offset_raw = stream.read(4)
            if len(offset_raw) != 4:
                raise ValueError("truncated DOS header")
            pe_offset = int.from_bytes(offset_raw, "little")
            stream.seek(pe_offset)
            if stream.read(4) != b"PE\0\0":
                raise ValueError("missing PE header")
            machine_raw = stream.read(2)
            if len(machine_raw) != 2:
                raise ValueError("truncated PE header")
            return int.from_bytes(machine_raw, "little")
    except OSError as exc:
        raise ValueError(f"native component is unreadable: {exc}") from exc


def detect_runtime_driver_statuses(
    root: str | Path,
    *,
    platform_name: str | None = None,
    hid_loader: Callable[[str], Any] | None = None,
) -> dict[str, bool]:
    """Detect runtime driver prerequisites without requiring attached devices."""

    candidate_root = Path(root)
    platform = str(platform_name or os.name)
    hid_available = False
    if platform == "nt":
        loader = hid_loader or getattr(ctypes, "WinDLL", None)
        if callable(loader):
            try:
                loader("hid.dll")
                hid_available = True
            except (OSError, AttributeError):
                hid_available = False
    return {
        "serial_port": platform == "nt",
        "ethernet": True,
        "uvc": (candidate_root / "native_dll" / "BsvUvcNative.dll").is_file(),
        # SMRF uses the Windows HID API through ctypes; it does not import the
        # unrelated third-party Python package named ``hid``.
        "smrf_hid": hid_available,
    }


def probe_candidate_runtime(context: Mapping[str, Any]) -> ProbeOutcome:
    """Verify candidate-local runtime, modules, native files, SDKs and drivers."""

    root = Path(str(context["root"])).resolve()
    manifest_path = root / "RELEASE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    requirements = manifest.get("runtime_requirements")
    if not isinstance(requirements, Mapping):
        raise ValueError("runtime_requirements are missing from release manifest")

    if requirements.get("python_runtime") != "bundled":
        raise ValueError("release does not declare a bundled Python runtime")
    internal = root / "_internal"
    runtime_files = tuple(internal.glob("python*.dll")) + tuple(
        internal.glob("python*.zip")
    )
    if not internal.is_dir() or not runtime_files:
        raise ValueError("bundled Python runtime is missing from the candidate")

    module_checker = context.get("module_checker")
    if not callable(module_checker):
        module_checker = lambda name: importlib.util.find_spec(str(name)) is not None
    modules = requirements.get("required_modules")
    if not isinstance(modules, list) or not modules:
        raise ValueError("required_modules declaration is missing")
    missing_modules = [str(name) for name in modules if not module_checker(str(name))]
    if missing_modules:
        raise ValueError(f"required module is missing: {', '.join(missing_modules)}")

    native_components = requirements.get("native_components")
    if not isinstance(native_components, list) or not native_components:
        raise ValueError("native component declarations are missing")
    expected_machine = 0x8664 if struct.calcsize("P") == 8 else 0x14C
    for item in native_components:
        if not isinstance(item, Mapping):
            raise ValueError("native component declaration is invalid")
        component_id = str(item.get("id") or "<unknown>")
        relative = str(item.get("path") or "")
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"native component {component_id} is missing")
        try:
            digest = _sha256(path)
        except OSError as exc:
            raise ValueError(f"native component {component_id} is unreadable: {exc}") from exc
        declared_digest = str(item.get("sha256") or "").lower()
        if not declared_digest or digest != declared_digest:
            raise ValueError(f"native component {component_id} digest mismatch")
        try:
            machine = _native_machine(path)
        except ValueError as exc:
            raise ValueError(
                f"native component {component_id} is not compatible: {exc}"
            ) from exc
        if machine != expected_machine:
            raise ValueError(
                f"native component {component_id} is not compatible with this runtime"
            )

    sdks = requirements.get("vendor_sdks")
    if not isinstance(sdks, list) or not sdks:
        raise ValueError("vendor SDK declarations are missing")
    for item in sdks:
        if not isinstance(item, Mapping) or not item.get("id"):
            raise ValueError("vendor SDK declaration is invalid")
        sdk_id = str(item["id"])
        detection = str(item.get("detection") or "")
        detection_path = (root / detection).resolve()
        if (
            not detection
            or root not in detection_path.parents
            or not detection_path.is_file()
        ):
            raise ValueError(f"vendor SDK {sdk_id} is not installed in the candidate")

    drivers = requirements.get("drivers")
    statuses = context.get("driver_status")
    if not isinstance(drivers, list) or not drivers:
        raise ValueError("driver declarations are missing")
    if not isinstance(statuses, Mapping):
        statuses = detect_runtime_driver_statuses(root)
    for item in drivers:
        if not isinstance(item, Mapping) or not item.get("id"):
            raise ValueError("driver declaration is invalid")
        driver_id = str(item["id"])
        if statuses.get(driver_id) is not True:
            raise ValueError(f"required driver {driver_id} is unavailable")

    return ProbeOutcome(
        "passed",
        "bundled runtime, modules, native components, SDKs and drivers verified",
        details={
            "runtime_files": len(runtime_files),
            "required_modules": len(modules),
            "native_components": len(native_components),
            "vendor_sdks": len(sdks),
            "drivers": len(drivers),
        },
    )


def probe_save_directory(context: Mapping[str, Any]) -> ProbeOutcome:
    raw_path = str(context.get("save_path") or "").strip()
    if not raw_path:
        raise ValueError("save path is empty")
    root = Path(raw_path).expanduser().resolve()
    maximum_path_length = int(context.get("maximum_path_length") or 240)
    probe_name = f".afp-preflight-{uuid.uuid4().hex}"
    temporary = root / f"{probe_name}.tmp"
    final = root / f"{probe_name}.ready"
    if max(len(str(temporary)), len(str(final))) > maximum_path_length:
        raise ValueError(
            f"save path length exceeds safe limit {maximum_path_length}: {len(str(final))}"
        )

    directory_factory = context.get("directory_factory")
    if not callable(directory_factory):
        directory_factory = lambda path: path.mkdir(parents=True, exist_ok=True)
    try:
        directory_factory(root)
    except Exception as exc:
        raise ValueError(f"save directory create failed: {exc}") from exc
    if not root.is_dir():
        raise ValueError("save directory create failed: target is not a directory")

    disk_usage = context.get("disk_usage")
    if not callable(disk_usage):
        disk_usage = shutil.disk_usage
    try:
        free_bytes = int(disk_usage(root).free)
    except Exception as exc:
        raise ValueError(f"save directory free space query failed: {exc}") from exc
    minimum_free = int(context.get("minimum_free_bytes") or 1)
    if free_bytes < minimum_free:
        raise ValueError(
            f"save directory free space is insufficient: {free_bytes} < {minimum_free}"
        )

    content = (b"AFP-PRE-SENSOR-WRITE-PROBE\n" * 2048)[:65536]
    writer = context.get("writer")
    if not callable(writer):
        def writer(path: Path, payload: bytes) -> None:
            with path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

    renamer = context.get("renamer")
    if not callable(renamer):
        renamer = os.replace
    cleanup = context.get("cleanup")
    if not callable(cleanup):
        cleanup = lambda path: path.unlink(missing_ok=True)

    stage = "write"
    write_started = time.monotonic()
    try:
        writer(temporary, content)
        write_seconds = time.monotonic() - write_started
        if not temporary.is_file() or temporary.stat().st_size != len(content):
            raise ValueError("write did not produce the complete probe file")
        maximum_write = float(context.get("maximum_write_seconds") or 2.0)
        if write_seconds > maximum_write:
            raise ValueError(
                f"slow write exceeded {maximum_write:g} seconds: {write_seconds:.6f}"
            )
        stage = "rename"
        renamer(temporary, final)
        if not final.is_file() or final.read_bytes() != content:
            raise ValueError("renamed probe file could not be verified")
        stage = "cleanup"
        cleanup(final)
        if temporary.exists() or final.exists():
            raise ValueError("probe files remain after cleanup")
    except Exception as exc:
        cleanup_errors: list[str] = []
        for path in (temporary, final):
            if path.exists():
                try:
                    cleanup(path)
                except Exception as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
        if stage == "write" and "slow write" in str(exc):
            message = str(exc)
        else:
            message = f"save directory {stage} failed: {exc}"
        if cleanup_errors:
            message += "; cleanup failed: " + "; ".join(cleanup_errors)
        raise ValueError(message) from exc

    return ProbeOutcome(
        "passed",
        "save directory create/write/close/rename/cleanup probe passed",
        details={
            "bytes_written": len(content),
            "write_seconds": write_seconds,
            "free_bytes": free_bytes,
            "minimum_free_bytes": minimum_free,
        },
    )


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError as exc:
        raise ValueError(f"capacity directory is unreadable: {path}: {exc}") from exc
    return total


def probe_capacity_readiness(context: Mapping[str, Any]) -> ProbeOutcome:
    policy = context.get("capacity_policy")
    if not isinstance(policy, CapacityPolicy):
        config = context.get("configuration")
        if not isinstance(config, Mapping):
            raise ValueError("capacity policy or configuration is required")
        policy = CapacityPolicy.from_config(config)
    directories = context.get("capacity_directories")
    if not isinstance(directories, Mapping):
        raise ValueError("capacity_directories are required")
    required_names = ("logs", "journal", "spool")
    paths: dict[str, Path] = {}
    for name in required_names:
        raw = str(directories.get(name) or "").strip()
        if not raw:
            raise ValueError(f"{name} capacity directory is missing")
        path = Path(raw).expanduser().resolve()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"{name} capacity directory cannot be created: {exc}") from exc
        paths[name] = path

    usage_override = context.get("capacity_usage_bytes")
    if isinstance(usage_override, Mapping):
        usage = {name: int(usage_override.get(name) or 0) for name in required_names}
    else:
        usage = {name: _directory_size(path) for name, path in paths.items()}
    limits = {
        "logs": policy.log_max_bytes,
        "journal": policy.journal_max_bytes,
        "spool": policy.spool_max_bytes,
    }
    warnings: list[str] = []
    ratios: dict[str, float] = {}
    for name in required_names:
        value = usage[name]
        if value < 0:
            raise ValueError(f"{name} capacity usage cannot be negative")
        ratio = value / limits[name]
        ratios[name] = ratio
        if ratio >= policy.hard_limit_ratio:
            raise ValueError(f"{name} reached its hard limit")
        if ratio >= policy.warning_ratio:
            warnings.append(name)

    free_override = context.get("capacity_free_bytes")
    if free_override is None:
        free_bytes = min(shutil.disk_usage(path).free for path in paths.values())
    else:
        free_bytes = int(free_override)
    if free_bytes < policy.minimum_free_bytes:
        raise ValueError(
            "minimum free disk capacity is not met: "
            f"{free_bytes} < {policy.minimum_free_bytes}"
        )

    queue_depths = context.get("queue_depths")
    if not isinstance(queue_depths, Mapping):
        queue_depths = {"writer": 0, "transport": 0}
    queue_limits = {
        "writer_queue": policy.writer_rows,
        "transport_queue": policy.transport_rows,
    }
    queue_ratios: dict[str, float] = {}
    for label, limit in queue_limits.items():
        key = label.removesuffix("_queue")
        depth = int(queue_depths.get(key) or 0)
        ratio = depth / limit
        queue_ratios[label] = ratio
        if depth > limit or ratio >= policy.hard_limit_ratio:
            raise ValueError(f"{label} reached its hard limit")
        if ratio >= policy.queue_high_water_ratio:
            warnings.append(label)

    evidence = "capacity limits and current usage are within hard limits"
    if warnings:
        evidence += "; warning threshold reached: " + ", ".join(warnings)
    return ProbeOutcome(
        "passed",
        evidence,
        details={
            "usage_bytes": usage,
            "limits_bytes": limits,
            "usage_ratios": ratios,
            "queue_ratios": queue_ratios,
            "free_bytes": free_bytes,
            "minimum_free_bytes": policy.minimum_free_bytes,
            "log_retention_days": policy.log_retention_days,
            "warnings": warnings,
        },
    )


def probe_mysql_readiness(context: Mapping[str, Any]) -> ProbeOutcome:
    profile = context.get("mysql_profile")
    if not isinstance(profile, Mapping) or not profile:
        raise ExternalEnvironmentBlocked(
            "target MySQL profile is not available",
            "Provide AFP_MYSQL_PROFILE for the real target instance with connect, "
            "schema, INSERT, SELECT and ROLLBACK permissions, then rerun",
        )
    if context.get("mysql_evidence_tier") != "target_environment":
        raise ValueError(
            "MySQL readiness requires target environment evidence; simulation is insufficient"
        )
    try:
        from mysql_storage import MySQLCaptureStore, MySQLSettings
    except ImportError:
        from .mysql_storage import MySQLCaptureStore, MySQLSettings
    settings = MySQLSettings.from_mapping(
        {
            **dict(profile),
            "mysql_enabled": True,
        }
    )
    factory = context.get("mysql_store_factory")
    store = factory(settings) if callable(factory) else MySQLCaptureStore(settings)
    result = store.preflight(require_schema=True, write_test=True)
    if not isinstance(result, Mapping):
        raise ValueError("target MySQL preflight returned an invalid result")
    stage = str(result.get("stage") or "unknown")
    required_flags = (
        bool(result.get("ok")),
        bool(result.get("schema_ready")),
        bool(result.get("write_test")),
        bool(result.get("rollback_verified")),
    )
    if not all(required_flags):
        category = ""
        error_detail = result.get("error_detail")
        if isinstance(error_detail, Mapping):
            category = str(error_detail.get("category") or "")
        suffix = f" ({category})" if category else ""
        raise ValueError(f"target MySQL preflight failed at stage {stage}{suffix}")
    stages = result.get("stages")
    expected_stages = {"connect", "schema", "write", "rollback"}
    if not isinstance(stages, Mapping) or any(
        stages.get(name) is not True for name in expected_stages
    ):
        raise ValueError("target MySQL preflight did not prove every required stage")
    details = {
        "host": settings.host,
        "port": settings.port,
        "database": settings.database,
        "user": settings.user,
        "tls_requested": bool(result.get("tls_requested", settings.ssl_enabled)),
        "driver": str(result.get("driver") or ""),
        "stages": {name: True for name in sorted(expected_stages)},
        "rollback_verified": True,
        "evidence_tier": "target_environment",
    }
    return ProbeOutcome(
        "passed",
        "target MySQL connect/schema/write/rollback probe passed without residue",
        details=details,
    )


def probe_persistence_recovery(context: Mapping[str, Any]) -> ProbeOutcome:
    raw_path = str(context.get("save_path") or "").strip()
    if not raw_path:
        raise ValueError("save path is empty; persistence recovery cannot be scanned")
    try:
        from acquisition import scan_persistence_recovery
    except ImportError:
        from .acquisition import scan_persistence_recovery
    recovery = scan_persistence_recovery(Path(raw_path))
    if recovery["database_pending_retry"]:
        raise ValueError(
            "database_pending_retry: complete MySQL retry and reconciliation before "
            "the next formal capture"
        )
    if recovery["incomplete_files"]:
        raise ValueError(
            "finalization_failed: incomplete capture files require recovery or "
            "operator-reviewed archival before the next formal capture"
        )
    return ProbeOutcome(
        "passed",
        "no database retry or incomplete finalization remains from a prior capture",
        details=dict(recovery),
    )


def probe_helper_readiness(context: Mapping[str, Any]) -> ProbeOutcome:
    mode = str(context.get("mode") or "")
    if mode == "local_direct":
        return ProbeOutcome(
            "passed",
            "Helper is not applicable to local_direct mode",
            details={"not_applicable": True, "evidence_tier": "local_environment"},
        )
    if mode != "remote_helper":
        raise ValueError("Helper readiness requires an explicit acquisition mode")
    provider = context.get("helper_status_provider")
    if not callable(provider):
        raise ExternalEnvironmentBlocked(
            "a second Windows Helper environment is not available to this preflight",
            "Provide the second Windows computer, install the same candidate Helper, "
            "pair it, and rerun the two-machine preflight",
        )
    state = provider()
    if not isinstance(state, Mapping):
        raise ValueError("Helper status provider returned an invalid result")
    required = (
        "installed",
        "process_running",
        "port_open",
        "health_ok",
        "paired",
        "readiness_imported",
        "http_ok",
        "websocket_ok",
        "proxy_ok",
    )
    failed = [field_name for field_name in required if state.get(field_name) is not True]
    if failed:
        raise ValueError("Helper readiness checks failed: " + ", ".join(failed))
    capabilities = state.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise ValueError("Helper capabilities are missing")
    try:
        from release_contract import (
            ReleaseContractError,
            assert_helper_contract,
            load_release_contract,
        )
    except ImportError:
        from .release_contract import (
            ReleaseContractError,
            assert_helper_contract,
            load_release_contract,
        )
    contract = load_release_contract(start=Path(__file__))
    try:
        assert_helper_contract(capabilities, contract)
    except ReleaseContractError as exc:
        raise ValueError(str(exc)) from exc
    evidence_tier = str(
        context.get("helper_evidence_tier") or "protocol_simulation"
    )
    if evidence_tier not in {"protocol_simulation", "two_machine_environment"}:
        raise ValueError("Helper evidence tier is invalid")
    evidence = "Helper installation, health, pairing, readiness and transports passed"
    if evidence_tier == "protocol_simulation":
        evidence += "; protocol simulation only, not a two-machine acceptance"
    return ProbeOutcome(
        "passed",
        evidence,
        details={
            **{field_name: True for field_name in required},
            "evidence_tier": evidence_tier,
            "version": contract.helper_version,
            "release_revision": contract.release_revision,
        },
    )


def generate_windows_remediation_script(_findings: Sequence[str]) -> str:
    return _WINDOWS_REMEDIATION_SCRIPT


def _run_readonly_command(command: Sequence[str]) -> str:
    completed = subprocess.run(
        tuple(command),
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False),
        errors="replace",
        timeout=15,
        check=False,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    lowered = output.lower()
    if "access is denied" in lowered or "access denied" in lowered or "拒绝访问" in output:
        raise PermissionError(output.strip() or "access denied")
    if completed.returncode != 0:
        raise RuntimeError(output.strip() or f"command exited {completed.returncode}")
    return output


def _adapter_power_disabled_from_registry(
    interface_guid: str,
    runner: Callable[[Sequence[str]], str] = _run_readonly_command,
) -> bool:
    """Read the documented adapter opt-out bit when a driver rejects NetAdapter PM."""
    normalized = str(interface_guid or "").strip().strip("{}")
    if not normalized:
        return False
    class_key = (
        r"HKLM\SYSTEM\CurrentControlSet\Control\Class"
        r"\{4d36e972-e325-11ce-bfc1-08002be10318}"
    )
    search = runner(
        (
            "reg",
            "query",
            class_key,
            "/s",
            "/f",
            "{" + normalized + "}",
            "/d",
            "/e",
        )
    )
    keys = re.findall(
        r"(?im)^(HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Control\\Class"
        r"\\\{4d36e972-e325-11ce-bfc1-08002be10318\}\\[0-9]{4})\s*$",
        search,
    )
    if not keys:
        return False
    values = runner(("reg", "query", keys[0], "/v", "PnPCapabilities"))
    matched = re.search(
        r"(?im)^\s*PnPCapabilities\s+REG_DWORD\s+(0x[0-9A-Fa-f]+)\s*$",
        values,
    )
    return bool(matched) and bool(int(matched.group(1), 16) & 0x20)


def _active_physical_adapter_power_saving_disabled() -> bool:
    inventory = _run_readonly_command(
        (
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$items=@(Get-NetAdapter -Physical -ErrorAction Stop | "
            "Where-Object {$_.Status -eq 'Up'} | "
            "Select-Object @{n='name';e={$_.Name}},@{n='guid';e={[string]$_.InterfaceGuid}}); "
            "$items | ConvertTo-Json -Compress",
        )
    ).strip()
    if not inventory:
        return False
    parsed = json.loads(inventory)
    adapters = parsed if isinstance(parsed, list) else [parsed]
    if not adapters:
        return False
    for adapter in adapters:
        name = str(adapter.get("name") or "").replace("'", "''")
        guid = str(adapter.get("guid") or "")
        if not name or not guid:
            return False
        try:
            output = _run_readonly_command(
                (
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"$item=Get-NetAdapterPowerManagement -Name '{name}' -ErrorAction Stop; "
                    "$item.AllowComputerToTurnOffDevice -ne 'Enabled'",
                )
            )
            if "true" not in output.lower():
                return False
        except RuntimeError:
            if not _adapter_power_disabled_from_registry(guid):
                return False
    return True


def _windows_time_status_synchronized(output: str) -> bool:
    text = str(output or "")
    lowered = text.lower()
    if not text.strip() or "local cmos clock" in lowered or "free-running system clock" in lowered:
        return False
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    leap = re.search(r":\s*([0-9]+)\s*(?:\(|$)", first_line)
    return bool(leap) and int(leap.group(1)) != 3


def _power_setting_disabled(subgroup: str, setting: str) -> bool:
    output = _run_readonly_command(
        ("powercfg", "/query", "SCHEME_CURRENT", subgroup, setting)
    )
    values = re.findall(r"(?:AC Power Setting Index|交流电源设置索引)[^0-9A-Fa-f]*(0x[0-9A-Fa-f]+)", output)
    return bool(values) and int(values[-1], 16) == 0


def _default_windows_state(context: Mapping[str, Any]) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("Windows readiness probe requires Windows")
    _run_readonly_command(
        (
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-NetFirewallProfile | Out-Null",
        )
    )
    port = int(context.get("required_port") or 0)
    port_available = True
    if port:
        test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            test_socket.bind(("127.0.0.1", port))
        except OSError:
            port_available = False
        finally:
            test_socket.close()
    proxy = str(
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or ""
    ).strip()
    no_proxy = str(os.environ.get("NO_PROXY") or "").lower()
    proxy_bypass_local = not proxy or (
        "127.0.0.1" in no_proxy and "localhost" in no_proxy
    )
    adapter_power_saving_disabled = False
    adapter_power_query_error = ""
    try:
        adapter_power_saving_disabled = (
            _active_physical_adapter_power_saving_disabled()
        )
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        adapter_power_query_error = str(exc)
    service_output = _run_readonly_command(
        (
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-Service W32Time -ErrorAction Stop).Status -eq 'Running'",
        )
    )
    try:
        time_output = _run_readonly_command(("w32tm", "/query", "/status"))
        synchronized = _windows_time_status_synchronized(time_output)
    except RuntimeError:
        synchronized = False
    state = {
        "firewall_query_ok": True,
        "port_available": port_available,
        "proxy_bypass_local": proxy_bypass_local,
        "sleep_disabled_ac": _power_setting_disabled("SUB_SLEEP", "STANDBYIDLE"),
        "adapter_power_saving_disabled": adapter_power_saving_disabled,
        "usb_selective_suspend_disabled": _power_setting_disabled(
            "2a737441-1930-4402-8d77-b2bebba308a3",
            "48e6b7a6-50f5-4782-a5d4-53bb8f07e226",
        ),
        "time_service_running": "true" in service_output.lower(),
        "time_synchronized": synchronized,
        "clock_offset_seconds": 0.0 if synchronized else float("inf"),
    }
    if adapter_power_query_error:
        state["adapter_power_query_error"] = adapter_power_query_error
    return state


def probe_windows_readiness(context: Mapping[str, Any]) -> ProbeOutcome:
    provider = context.get("windows_state_provider")
    if not callable(provider):
        provider = lambda: _default_windows_state(context)
    try:
        state = provider()
    except PermissionError as exc:
        raise ExternalEnvironmentBlocked(
            f"Windows readiness state could not be read: {exc}",
            "Run the review-only probe with administrator access and rerun; "
            "do not change policy without separate approval",
        ) from exc
    if not isinstance(state, Mapping):
        raise ValueError("Windows state provider returned an invalid result")
    required = (
        "firewall_query_ok",
        "port_available",
        "proxy_bypass_local",
        "sleep_disabled_ac",
        "adapter_power_saving_disabled",
        "usb_selective_suspend_disabled",
        "time_service_running",
        "time_synchronized",
    )
    failed_fields = [field_name for field_name in required if state.get(field_name) is not True]
    if failed_fields:
        raise ValueError(
            "Windows readiness checks failed: " + ", ".join(failed_fields)
        )
    maximum_offset = float(context.get("maximum_clock_offset_seconds") or 1.0)
    try:
        offset = abs(float(state.get("clock_offset_seconds")))
    except (TypeError, ValueError) as exc:
        raise ValueError("clock_offset_seconds is unavailable") from exc
    if offset > maximum_offset:
        raise ValueError(
            f"clock_offset_seconds exceeds limit: {offset:g} > {maximum_offset:g}"
        )
    return ProbeOutcome(
        "passed",
        "firewall, port, proxy, power management and time state are ready",
        details=dict(state),
    )


def _release_manifest_probe(context: Mapping[str, Any]) -> ProbeOutcome:
    root = Path(str(context["root"])).resolve()
    manifest_path = root / "RELEASE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != 1:
        raise ValueError("RELEASE-MANIFEST schema_version must be 1")
    groups = (
        manifest.get("static_assets"),
        manifest.get("configuration_templates"),
    )
    checked = 0
    for group in groups:
        if not isinstance(group, Mapping):
            raise ValueError("release manifest asset group is missing")
        required = group.get("required")
        digests = group.get("sha256")
        if not isinstance(required, list) or not isinstance(digests, Mapping):
            raise ValueError("release manifest asset hashes are missing")
        for relative_value in required:
            relative = str(relative_value)
            path = (root / relative).resolve()
            if root not in path.parents or not path.is_file():
                raise ValueError(f"managed release asset is missing: {relative}")
            if _sha256(path) != str(digests.get(relative) or "").lower():
                raise ValueError(f"managed release asset hash mismatch: {relative}")
            checked += 1
    candidate = manifest.get("candidate")
    if not isinstance(candidate, Mapping) or not candidate.get("candidate_id"):
        raise ValueError("candidate identity is missing")
    return ProbeOutcome(
        "passed",
        f"candidate manifest and {checked} declared assets verified",
        details={"checked_assets": checked},
    )


def _configuration_probe(context: Mapping[str, Any]) -> ProbeOutcome:
    config_path = Path(str(context["config_path"])).resolve()
    payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    references = context.get("references")
    if not isinstance(references, Mapping):
        raise ValueError("configuration references are unavailable")
    try:
        from pre_sensor_config import validate_pre_sensor_config
    except ImportError:
        from .pre_sensor_config import validate_pre_sensor_config
    config = validate_pre_sensor_config(payload, references=references)
    return ProbeOutcome(
        "passed",
        f"{config.real_acquisition_mode} configuration validated before start",
        details={"channels": len(config.selected_sensors or [])},
    )


def _physical_probe(_context: Mapping[str, Any]) -> ProbeOutcome:
    raise PhysicalSensorPending(
        "physical sensor identity and readings require the on-site devices",
        "Complete only the items in the real-device verification checklist on site",
    )


def _references_from_environment(config_payload: Mapping[str, Any]) -> dict[str, Any]:
    references: dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and value.startswith("AFP_"):
            raw = os.environ.get(value)
            if raw is None:
                return
            if value.endswith("_PROFILE"):
                try:
                    references[value] = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{value} must contain a JSON object") from exc
            else:
                references[value] = raw

    visit(config_payload)
    return references


def run_candidate_preflight(
    root: Path,
    *,
    mode: str,
    config_path: Path | None = None,
    output_directory: Path | None = None,
) -> tuple[PreflightReport, Path, Path]:
    candidate_root = Path(root).resolve()
    manifest = json.loads(
        (candidate_root / "RELEASE-MANIFEST.json").read_text(encoding="utf-8-sig")
    )
    candidate = manifest.get("candidate") or {}
    candidate_identity = str(candidate.get("candidate_id") or "").strip()
    if config_path is None:
        candidates = (
            candidate_root / "config" / "pre_sensor" / f"{mode}.json",
            candidate_root / "pre_sensor" / "config" / f"{mode}.json",
        )
        config_path = next((path for path in candidates if path.is_file()), candidates[0])
    payload = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    references = _references_from_environment(payload)
    mysql_profile: Mapping[str, Any] | None = None
    raw_mysql_profile = os.environ.get("AFP_MYSQL_PROFILE")
    if raw_mysql_profile:
        try:
            parsed_profile = json.loads(raw_mysql_profile)
        except json.JSONDecodeError as exc:
            raise ValueError("AFP_MYSQL_PROFILE must contain a JSON object") from exc
        if not isinstance(parsed_profile, Mapping):
            raise ValueError("AFP_MYSQL_PROFILE must contain a JSON object")
        mysql_profile = parsed_profile
    storage = payload.get("storage") if isinstance(payload, Mapping) else None
    logging_config = payload.get("logging") if isinstance(payload, Mapping) else None
    capacity = payload.get("capacity") if isinstance(payload, Mapping) else None
    save_path = ""
    minimum_free_bytes = 1
    if isinstance(storage, Mapping):
        save_path = str(references.get(str(storage.get("save_root_ref") or "")) or "")
    if isinstance(capacity, Mapping):
        minimum_free_bytes = int(capacity.get("minimum_free_mb") or 0) * 1024 * 1024
    log_path = ""
    if isinstance(logging_config, Mapping):
        log_path = str(
            references.get(str(logging_config.get("directory_ref") or "")) or ""
        )
    capacity_directories = {
        "logs": log_path,
        "journal": str(Path(save_path) / "server_journal") if save_path else "",
        "spool": str(Path(save_path) / "retry_spool") if save_path else "",
    }
    probes = (
        Probe("release.manifest", "候选包与发行清单", _release_manifest_probe, 30),
        Probe(
            "runtime.candidate_dependencies",
            "候选运行库、模块、DLL、SDK 与驱动",
            probe_candidate_runtime,
            30,
        ),
        Probe("config.semantic", "模式配置与互斥", _configuration_probe, 10),
        Probe(
            "storage.save_directory",
            "保存目录与磁盘原子写入",
            probe_save_directory,
            30,
        ),
        Probe(
            "storage.capacity_policy",
            "队列、日志、journal 与 retry spool 容量",
            probe_capacity_readiness,
            30,
        ),
        Probe(
            "environment.target_mysql",
            "目标 MySQL 连接、schema、权限、写入与回滚",
            probe_mysql_readiness,
            60,
            external_environment=True,
        ),
        Probe(
            "storage.persistence_recovery",
            "上一采集数据库重试与收尾恢复",
            probe_persistence_recovery,
            30,
        ),
        Probe(
            "environment.second_windows",
            "Helper 安装、健康、配对、能力与 HTTP/WebSocket",
            probe_helper_readiness,
            60,
            external_environment=True,
        ),
        Probe(
            "environment.windows_policy_access",
            "Windows 防火墙、代理、电源与时间同步",
            probe_windows_readiness,
            60,
            external_environment=True,
        ),
        Probe(
            "physical.device_identity_ports",
            "真实设备身份与物理端口",
            _physical_probe,
            physical_sensor=True,
        ),
    )
    report = run_preflight(
        probes,
        context={
            "root": str(candidate_root),
            "config_path": str(Path(config_path).resolve()),
            "references": references,
            "save_path": save_path,
            "minimum_free_bytes": minimum_free_bytes,
            "configuration": payload,
            "capacity_directories": capacity_directories,
            "mysql_profile": mysql_profile,
            "mysql_evidence_tier": (
                "target_environment" if mysql_profile is not None else "unavailable"
            ),
            "mode": mode,
        },
        candidate_identity=candidate_identity,
        mode=mode,
    )
    output = Path(output_directory or candidate_root / "verification" / "pre-sensor")
    json_path, markdown_path = write_preflight_report(report, output)
    return report, json_path, markdown_path
