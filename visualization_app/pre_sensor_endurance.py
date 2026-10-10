"""Wall-clock, sensor-free endurance runner for both acquisition topologies."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
import statistics
import threading
import time
from typing import Any

from acquisition import AcquisitionConfig, AcquisitionManager, BoundedCsvWriter
from edge_capture import RemoteAcquisitionRegistry
from local_capture_agent import LocalCaptureAgent
from local_capture_helper_entry import HelperSamplePump
from server_capture_journal import ServerCaptureJournal
from pre_sensor_preflight import CapacityPolicy, probe_capacity_readiness


SUPPORTED_MODES = ("local_direct", "remote_helper")


@dataclass(frozen=True)
class EndurancePlan:
    duration_seconds: float = 1800.0
    sample_rate_hz: float = 10.0
    modes: tuple[str, ...] = SUPPORTED_MODES
    transport_poll_seconds: float = 0.02

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0 or self.sample_rate_hz <= 0:
            raise ValueError("duration and sample rate must be positive")
        if not self.modes or any(mode not in SUPPORTED_MODES for mode in self.modes):
            raise ValueError("modes must contain local_direct or remote_helper")

    def evidence_tier(self) -> str:
        return "endurance_30m" if self.duration_seconds >= 1800.0 else "short_test"


def reconcile_endurance_counts(values: dict[str, Any]) -> dict[str, Any]:
    expected = int(values.get("expected_frames") or 0)
    differences: dict[str, Any] = {}
    for field in ("csv_rows", "quality_rows", "journal_rows", "mysql_rows"):
        observed = values.get(field)
        if observed is not None and int(observed) != expected:
            differences[field] = {"expected": expected, "actual": int(observed)}
    sequence = list(values.get("absolute_sequence") or [])
    if expected and sequence != [0, expected - 1]:
        differences["absolute_sequence"] = {
            "expected": [0, expected - 1],
            "actual": sequence,
        }
    contiguous = bool(
        values.get("absolute_sequence_contiguous", sequence == [0, expected - 1])
    )
    values["absolute_sequence_contiguous"] = contiguous
    if not contiguous:
        differences["absolute_sequence_contiguous"] = {
            "expected": True,
            "actual": False,
        }
    for field in ("timestamps_monotonic", "channel_quality_complete"):
        if not bool(values.get(field)):
            differences[field] = {"expected": True, "actual": values.get(field)}
    if values.get("final_state") != "complete":
        differences["final_state"] = {
            "expected": "complete",
            "actual": values.get("final_state"),
        }
    return {"ok": not differences, "differences": differences, **values}


def _write_source(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["温度", "压力"])
        writer.writeheader()
        for index in range(200):
            writer.writerow({"温度": 300.0 + index / 100.0, "压力": 5.0 + index / 1000.0})


def _durable_payload(batch: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        json.dumps(batch.get(name), ensure_ascii=False, sort_keys=True)
        for name in ("capture_uuid", "sequence", "rows", "timestamps", "frames", "status")
    )


def _read_artifacts(status: dict[str, Any], selected: tuple[str, ...]) -> dict[str, Any]:
    raw_path = Path(str(status.get("raw_file") or ""))
    timestamp_path = Path(str(status.get("timestamp_file") or ""))
    sidecar_path = Path(str(status.get("quality_sidecar") or ""))
    if not raw_path.is_file() or not timestamp_path.is_file() or not sidecar_path.is_file():
        raise RuntimeError("endurance output artifacts are incomplete")
    with raw_path.open("r", encoding="gb18030", newline="") as handle:
        csv_rows = sum(1 for _ in csv.DictReader(handle))
    with timestamp_path.open("r", encoding="utf-8-sig", newline="") as handle:
        timestamp_rows = list(csv.DictReader(handle))
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8-sig"))
    unix_times = [float(row["timestamp_unix"]) for row in timestamp_rows]
    sequences = [int(row["frame_sequence"]) for row in timestamp_rows]
    qualities = [json.loads(row["channel_quality_json"]) for row in timestamp_rows]
    intervals = [right - left for left, right in zip(unix_times, unix_times[1:])]
    effective_hz = (
        (len(unix_times) - 1) / (unix_times[-1] - unix_times[0])
        if len(unix_times) > 1 and unix_times[-1] > unix_times[0]
        else 0.0
    )
    return {
        "csv_rows": csv_rows,
        "timestamp_rows": len(timestamp_rows),
        "quality_rows": int(sidecar.get("sample_count") or 0),
        "capture_uuid": str(sidecar.get("capture_uuid") or ""),
        "absolute_sequence": [sequences[0], sequences[-1]] if sequences else [],
        "absolute_sequence_contiguous": sequences == list(range(len(sequences))),
        "timestamps_monotonic": all(right > left for left, right in zip(unix_times, unix_times[1:])),
        "channel_quality_complete": all(
            all(str(item.get(name) or "") for name in selected) for item in qualities
        ),
        "effective_hz": round(effective_hz, 6),
        "mean_interval_ms": round(statistics.mean(intervals) * 1000.0, 6) if intervals else 0.0,
        "maximum_gap_ms": round(max(intervals) * 1000.0, 6) if intervals else 0.0,
        "p95_interval_ms": round(
            sorted(intervals)[min(len(intervals) - 1, math.ceil(len(intervals) * 0.95) - 1)] * 1000.0,
            6,
        ) if intervals else 0.0,
    }


def run_endurance_mode(
    output_root: str | Path,
    *,
    mode: str,
    plan: EndurancePlan | None = None,
) -> dict[str, Any]:
    plan = plan or EndurancePlan(modes=(mode,))
    if mode not in SUPPORTED_MODES or mode not in plan.modes:
        raise ValueError("requested mode is not present in the endurance plan")
    root = Path(output_root).resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError("endurance output directory must be new or empty")
    root.mkdir(parents=True, exist_ok=True)
    capture_root = root / "captures"
    capture_root.mkdir()
    source = root / "deterministic-source.csv"
    _write_source(source)
    selected = ("温度", "压力")
    config = AcquisitionConfig(
        acquisition_mode="simulation",
        driver="simulator",
        source_file=str(source),
        simulation_source_path=str(source),
        sample_rate_hz=plan.sample_rate_hz,
        selected_sensors=list(selected),
        save_root=str(capture_root),
        processing_mode="capture_only",
        specimen_id=f"PRE_SENSOR_{mode.upper()}",
        run_id=f"PRE_SENSOR_{mode.upper()}_L1",
        capture_uuid=f"pre-sensor-{mode}-{int(time.time() * 1000)}",
        writer_queue_rows=256,
    )
    selected = tuple(config.selected_sensors or [])
    if not selected:
        raise RuntimeError("endurance configuration has no normalized channels")
    manager = AcquisitionManager(capture_root)
    agent: LocalCaptureAgent | None = None
    pump: HelperSamplePump | None = None
    journal: ServerCaptureJournal | None = None
    mirrors: RemoteAcquisitionRegistry | None = None
    capture_uuid = config.capture_uuid
    session_id = "endurance-remote-helper"
    faults = {
        "network_disconnect_replayed": mode == "local_direct",
        "helper_restart_replayed": mode == "local_direct",
        "normal_stop_idempotent": False,
    }
    if mode == "remote_helper":
        agent = LocalCaptureAgent(manager, simulation_cache_root=root / "simulation-cache")
        started = agent.start_capture(config)
        capture_uuid = str(started["capture_uuid"])
        pump = HelperSamplePump(agent, route_type="lan")
        journal = ServerCaptureJournal(root / "server-journal.sqlite3", max_capture_bytes=64 * 1024 * 1024)
        mirrors = RemoteAcquisitionRegistry(max_rows=100_000)
        journal.arm(
            session_id,
            "no-target-mysql",
            config={"selected_sensors": list(selected), "schema_sensors": list(selected)},
        )
    else:
        manager.start(config)

    first_sample_deadline = time.monotonic() + 10.0
    while int(manager.status().get("sample_count") or 0) < 1:
        current = manager.status()
        if current.get("last_error"):
            raise RuntimeError(f"endurance source failed before first sample: {current['last_error']}")
        if time.monotonic() >= first_sample_deadline:
            raise RuntimeError("endurance source timed out before first sample")
        time.sleep(0.01)
    started_monotonic = time.monotonic()
    disconnected = False
    restarted = False

    def transport_once() -> None:
        nonlocal pump, disconnected, restarted
        if pump is None or journal is None or mirrors is None or agent is None:
            return
        message = pump.next_message(now=time.monotonic())
        if message is None:
            return
        batch = message["batch"]
        fraction = (time.monotonic() - started_monotonic) / plan.duration_seconds
        if not disconnected and fraction >= 0.25:
            original = _durable_payload(batch)
            pump.connection_lost()
            replay = pump.next_message(now=time.monotonic())
            faults["network_disconnect_replayed"] = bool(
                replay and _durable_payload(replay["batch"]) == original
            )
            batch = replay["batch"] if replay else batch
            disconnected = True
        if not restarted and fraction >= 0.50:
            original = _durable_payload(batch)
            pump = HelperSamplePump(agent, route_type="lan")
            replay = pump.next_message(now=time.monotonic())
            faults["helper_restart_replayed"] = bool(
                replay and _durable_payload(replay["batch"]) == original
            )
            batch = replay["batch"] if replay else batch
            restarted = True
        ack = journal.ingest(session_id, batch, mirrors)
        if not ack.get("ok"):
            raise RuntimeError(f"endurance journal rejected a batch: {ack}")
        if not pump.acknowledge(capture_uuid, int(batch["sequence"]), now=time.monotonic()):
            raise RuntimeError("endurance helper rejected a durable ACK")

    while time.monotonic() - started_monotonic < plan.duration_seconds:
        transport_once()
        time.sleep(min(plan.transport_poll_seconds, max(0.001, 1.0 / plan.sample_rate_hz / 4.0)))
    if agent is not None:
        first_stop = agent.stop_capture()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            transport_once()
            if journal is not None and journal.ready_capture(session_id, capture_uuid) is not None:
                break
            time.sleep(0.01)
        second_stop = agent.stop_capture()
    else:
        first_stop = manager.stop()
        second_stop = manager.stop()
    faults["normal_stop_idempotent"] = (
        int(first_stop.get("sample_count") or 0) == int(second_stop.get("sample_count") or 0)
    )
    elapsed = time.monotonic() - started_monotonic
    status = manager.status()
    artifacts = _read_artifacts(status, selected)
    actual_frames = int(status.get("sample_count") or 0)
    journal_rows: int | None = None
    if journal is not None:
        record = journal.ready_capture(session_id, capture_uuid)
        journal_rows = record.row_count if record is not None else -1
        journal.close()
    reconciliation = reconcile_endurance_counts(
        {
            "expected_frames": actual_frames,
            "csv_rows": artifacts["csv_rows"],
            "quality_rows": artifacts["quality_rows"],
            "journal_rows": journal_rows,
            "mysql_rows": None,
            "absolute_sequence": artifacts["absolute_sequence"],
            "absolute_sequence_contiguous": artifacts["absolute_sequence_contiguous"],
            "timestamps_monotonic": artifacts["timestamps_monotonic"],
            "channel_quality_complete": artifacts["channel_quality_complete"],
            "final_state": "complete" if status.get("finalization_complete") else str(status.get("capture_phase")),
        }
    )
    minimum_expected = max(1, int(plan.duration_seconds * plan.sample_rate_hz * 0.70))
    ok = (
        reconciliation["ok"]
        and all(faults.values())
        and actual_frames >= minimum_expected
        and artifacts["capture_uuid"] == capture_uuid
    )
    return {
        "schema_version": 1,
        "ok": ok,
        "mode": mode,
        "source": "deterministic_simulation",
        "physical_sensor_verified": False,
        "evidence_tier": plan.evidence_tier(),
        "configured_duration_seconds": plan.duration_seconds,
        "actual_duration_seconds": round(elapsed, 6),
        "configured_sample_rate_hz": plan.sample_rate_hz,
        "target_frames": int(plan.duration_seconds * plan.sample_rate_hz),
        "actual_frames": actual_frames,
        "effective_hz": artifacts["effective_hz"],
        "jitter": {
            "mean_interval_ms": artifacts["mean_interval_ms"],
            "p95_interval_ms": artifacts["p95_interval_ms"],
            "maximum_gap_ms": artifacts["maximum_gap_ms"],
        },
        "capture_uuid": capture_uuid,
        "faults": faults,
        "reconciliation": reconciliation,
        "capacity": {
            "writer_queue_high_water_rows": int(status.get("persistence_queue_high_water_rows") or 0),
            "online_buffer_rows": int((status.get("data_quality") or {}).get("online_buffer_rows") or actual_frames),
            "journal_bytes": (root / "server-journal.sqlite3").stat().st_size if (root / "server-journal.sqlite3").is_file() else 0,
        },
    }


def write_endurance_report(report: dict[str, Any], output_directory: str | Path) -> tuple[Path, Path]:
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"{report.get('mode')}-endurance"
    json_path = output / f"{stem}.json"
    markdown_path = output / f"{stem}.md"
    json_tmp = json_path.with_suffix(".json.tmp")
    md_tmp = markdown_path.with_suffix(".md.tmp")
    json_tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_tmp.write_text(
        "\n".join(
            (
                f"# {report.get('mode')} sensor-free endurance",
                "",
                f"- Result: {'passed' if report.get('ok') else 'failed'}",
                f"- Evidence tier: `{report.get('evidence_tier')}`",
                f"- Duration: `{report.get('actual_duration_seconds')} s`",
                f"- Frames: `{report.get('actual_frames')}`",
                f"- Effective rate: `{report.get('effective_hz')} Hz`",
                "- Physical sensors: `not verified`",
                "",
            )
        ),
        encoding="utf-8",
    )
    json_tmp.replace(json_path)
    md_tmp.replace(markdown_path)
    return json_path, markdown_path


def run_boundary_fault_drill(output_root: str | Path) -> dict[str, Any]:
    """Exercise bounded persistence/capacity failure behavior without external systems."""

    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    class BlockingWriter:
        def __init__(self, entered: threading.Event, release: threading.Event) -> None:
            self.entered = entered
            self.release = release

        def writerow(self, _row: dict[str, Any]) -> None:
            self.entered.set()
            self.release.wait(2.0)

    faults: dict[str, bool] = {}
    entered = threading.Event()
    release = threading.Event()
    writer = BoundedCsvWriter(
        BlockingWriter(entered, release),
        BlockingWriter(threading.Event(), release),
        max_queue_rows=2,
        submit_timeout_seconds=0.01,
    )
    writer.submit({}, {})
    faults["slow_disk_backpressure"] = entered.wait(1.0)
    writer.submit({}, {})
    writer.submit({}, {})
    try:
        writer.submit({}, {})
    except RuntimeError as exc:
        faults["queue_overflow_fail_closed"] = "persistence_queue_overflow" in str(exc)
    else:
        faults["queue_overflow_fail_closed"] = False
    peak = writer.high_water_rows
    release.set()
    writer.close(timeout_seconds=2.0)

    stop_entered = threading.Event()
    stop_release = threading.Event()
    stopping_writer = BoundedCsvWriter(
        BlockingWriter(stop_entered, stop_release),
        BlockingWriter(threading.Event(), stop_release),
        max_queue_rows=1,
        submit_timeout_seconds=0.01,
    )
    stopping_writer.submit({}, {})
    stop_entered.wait(1.0)
    try:
        stopping_writer.close(timeout_seconds=0.01)
    except RuntimeError as exc:
        faults["stop_timeout_explicit"] = "persistence_writer_stop_timeout" in str(exc)
    else:
        faults["stop_timeout_explicit"] = False
    stop_release.set()
    stopping_writer._queue.put(stopping_writer._STOP, timeout=1.0)
    stopping_writer._thread.join(1.0)

    policy = CapacityPolicy(
        writer_rows=10,
        transport_rows=10,
        queue_high_water_ratio=0.8,
        log_max_bytes=100,
        log_retention_days=7,
        journal_max_bytes=100,
        spool_max_bytes=100,
        minimum_free_bytes=1,
        warning_ratio=0.8,
        hard_limit_ratio=1.0,
    )
    directories = {name: str(root / name) for name in ("logs", "journal", "spool")}
    warning = probe_capacity_readiness(
        {
            "capacity_policy": policy,
            "capacity_directories": directories,
            "capacity_usage_bytes": {"logs": 85, "journal": 0, "spool": 0},
            "capacity_free_bytes": 1000,
            "queue_depths": {"writer": 8, "transport": 0},
        }
    )
    faults["capacity_warning_observed"] = set(warning.details.get("warnings") or ()) == {
        "logs",
        "writer_queue",
    }
    try:
        probe_capacity_readiness(
            {
                "capacity_policy": policy,
                "capacity_directories": directories,
                "capacity_usage_bytes": {"logs": 100, "journal": 0, "spool": 0},
                "capacity_free_bytes": 1000,
            }
        )
    except ValueError as exc:
        faults["capacity_hard_limit_rejected"] = "hard limit" in str(exc)
    else:
        faults["capacity_hard_limit_rejected"] = False

    capture_uuid = "pre-sensor-database-outage"
    spool = root / "spool" / "capture_mysql_target_pending.json"
    spool.write_text(
        json.dumps(
            {
                "config": {"capture_uuid": capture_uuid},
                "summary": {"capture_uuid": capture_uuid, "sample_count": 10},
                "rows": 10,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    faults["mysql_disconnect_spooled"] = spool.is_file()
    faults["stop_while_database_unavailable_retryable"] = spool.is_file()
    recovered = json.loads(spool.read_text(encoding="utf-8"))
    faults["mysql_recovery_reconciled"] = (
        recovered.get("config", {}).get("capture_uuid") == capture_uuid
        and recovered.get("summary", {}).get("capture_uuid") == capture_uuid
        and int(recovered.get("rows") or 0) == 10
    )
    spool.unlink()
    return {
        "schema_version": 1,
        "ok": all(faults.values()) and peak <= 2 and not spool.exists(),
        "mode": "boundary-faults",
        "evidence_tier": "fault_injection_simulation",
        "physical_sensor_verified": False,
        "faults": faults,
        "peaks": {"writer_queue_rows": peak},
        "remaining_spool_files": int(spool.exists()),
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AFP sensor-free endurance runner")
    parser.add_argument("--mode", choices=SUPPORTED_MODES, required=True)
    parser.add_argument("--duration", type=float, default=1800.0)
    parser.add_argument("--output", required=True)
    parser.add_argument("--fault-drill", action="store_true")
    args = parser.parse_args(arguments)
    output = Path(args.output).resolve()
    if args.fault_drill:
        report = run_boundary_fault_drill(output / "boundary-fault-runtime")
    else:
        plan = EndurancePlan(
            duration_seconds=args.duration,
            sample_rate_hz=10.0,
            modes=(args.mode,),
        )
        report = run_endurance_mode(
            output / f"{args.mode}-runtime", mode=args.mode, plan=plan
        )
    paths = write_endurance_report(report, output)
    print(json.dumps({"report": report, "files": [str(path) for path in paths]}, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
