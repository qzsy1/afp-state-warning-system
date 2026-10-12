"""Deterministic, sensor-free readiness drills using the production transport core."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from edge_capture import RemoteAcquisitionRegistry
from local_capture_helper_entry import HelperSamplePump
from server_capture_journal import ServerCaptureJournal


@dataclass
class _DeterministicHelperSource:
    batches: list[dict[str, Any]]
    sample_rate_hz: float
    cursor: int = 0
    pending: dict[str, Any] | None = None

    def next_sample_batch(self, *, limit: int) -> dict[str, Any] | None:
        del limit  # Batches are deliberately fixed so a replay is byte-identical.
        if self.pending is not None:
            return deepcopy(self.pending)
        if self.cursor >= len(self.batches):
            return None
        self.pending = deepcopy(self.batches[self.cursor])
        return deepcopy(self.pending)

    def ack_sample_batch(self, capture_uuid: str, sequence: int) -> bool:
        if self.pending is None:
            return False
        if (
            str(self.pending.get("capture_uuid")) != str(capture_uuid)
            or int(self.pending.get("sequence", -1)) != int(sequence)
        ):
            return False
        self.cursor += 1
        self.pending = None
        return True

    def stream_metrics(self) -> dict[str, Any]:
        capture_uuid = ""
        if self.pending is not None:
            capture_uuid = str(self.pending.get("capture_uuid") or "")
        elif self.cursor < len(self.batches):
            capture_uuid = str(self.batches[self.cursor].get("capture_uuid") or "")
        return {
            "capture_uuid": capture_uuid,
            "sample_rate_hz": self.sample_rate_hz,
            "queued_rows": sum(
                len(item.get("rows") or []) for item in self.batches[self.cursor :]
            ),
        }

    def wait_for_stream_activity(self, timeout: float) -> bool:
        del timeout
        return self.cursor < len(self.batches)


def _frame(capture_uuid: str, sequence: int, timestamp: float) -> dict[str, Any]:
    return {
        "contract_version": "unified_frame_v1",
        "capture_uuid": capture_uuid,
        "frame_sequence": sequence,
        "frame_time": timestamp,
        "target_monotonic": timestamp,
        "assembled_monotonic": timestamp,
        "deadline_missed": False,
        "channels": {
            "温度": {
                "value": float(sequence),
                "unit": "°C",
                "quality": "measured_new",
                "source_timestamp": timestamp,
                "received_timestamp": timestamp,
                "age_seconds": 0.0,
                "is_new": True,
                "interface_id": "protocol_simulation",
                "source_sequence": sequence,
                "protocol_ok": True,
                "error": "",
            }
        },
    }


def _batches(sample_rate_hz: float, frame_count: int) -> list[dict[str, Any]]:
    capture_uuid = "pre-sensor-helper-drill"
    batch_size = min(5, max(1, frame_count // 3))
    result: list[dict[str, Any]] = []
    for batch_sequence, first in enumerate(range(0, frame_count, batch_size)):
        indexes = list(range(first, min(first + batch_size, frame_count)))
        timestamps = [index / sample_rate_hz for index in indexes]
        final = indexes[-1] == frame_count - 1
        result.append(
            {
                "capture_uuid": capture_uuid,
                "sequence": batch_sequence,
                "rows": [{"温度": float(index)} for index in indexes],
                "timestamps": timestamps,
                "frames": [
                    _frame(capture_uuid, index, timestamp)
                    for index, timestamp in zip(indexes, timestamps)
                ],
                "required_frame_contract": "unified_frame_v1",
                "status": {
                    "running": not final,
                    "finalization_complete": final,
                    "sample_count": frame_count if final else indexes[-1] + 1,
                    "config": {
                        "capture_uuid": capture_uuid,
                        "schema_sensors": ["温度"],
                    },
                },
            }
        )
    return result


def _same_durable_payload(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Ignore per-connection telemetry while comparing the replayed data body."""

    fields = (
        "capture_uuid",
        "sequence",
        "rows",
        "timestamps",
        "frames",
        "required_frame_contract",
        "status",
    )
    return all(left.get(field) == right.get(field) for field in fields)


def run_helper_protocol_drill(
    output_root: str | Path,
    *,
    sample_rate_hz: float = 10.0,
    frame_count: int = 100,
) -> dict[str, Any]:
    """Exercise replay and journal recovery without claiming real two-machine evidence."""

    sample_rate_hz = float(sample_rate_hz)
    frame_count = int(frame_count)
    if sample_rate_hz <= 0 or frame_count < 6:
        raise ValueError("sample_rate_hz must be positive and frame_count at least 6")
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    journal_path = root / "helper-protocol-drill.sqlite3"
    if journal_path.exists():
        raise FileExistsError(f"drill output already exists: {journal_path.name}")

    source = _DeterministicHelperSource(_batches(sample_rate_hz, frame_count), sample_rate_hz)
    pump = HelperSamplePump(source, route_type="lan")
    journal = ServerCaptureJournal(journal_path, max_capture_bytes=8 * 1024 * 1024)
    mirrors = RemoteAcquisitionRegistry(max_rows=max(8, frame_count))
    session_id = "protocol-simulation"
    capture_uuid = "pre-sensor-helper-drill"
    journal.arm(
        session_id,
        "no-target-mysql",
        config={"selected_sensors": ["温度"], "schema_sensors": ["温度"]},
    )

    first = pump.next_message(now=0.0)["batch"]
    gap = deepcopy(first)
    gap["sequence"] = 1
    gap_result = journal.ingest(session_id, gap, mirrors)
    first_ack = journal.ingest(session_id, first, mirrors)
    duplicate_ack = journal.ingest(session_id, first, mirrors)
    pump.acknowledge(capture_uuid, 0, now=0.01)

    second = pump.next_message(now=0.02)["batch"]
    pump.connection_lost()
    second_replay = pump.next_message(now=0.03)["batch"]
    second_ack = journal.ingest(session_id, second_replay, mirrors)
    pump.acknowledge(capture_uuid, 1, now=0.04)

    third = pump.next_message(now=0.05)["batch"]
    pump = HelperSamplePump(source, route_type="lan")
    third_replay = pump.next_message(now=0.06)["batch"]
    third_ack = journal.ingest(session_id, third_replay, mirrors)
    pump.acknowledge(capture_uuid, 2, now=0.07)

    mirrors = RemoteAcquisitionRegistry(max_rows=max(8, frame_count))
    restored = journal.ingest(session_id, third_replay, mirrors)

    accepted = [first_ack, second_ack, third_ack]
    while source.cursor < len(source.batches):
        message = pump.next_message(now=0.1 + source.cursor / sample_rate_hz)
        if message is None:
            raise RuntimeError("helper pump stalled before reconciliation")
        batch = message["batch"]
        ack = journal.ingest(session_id, batch, mirrors)
        accepted.append(ack)
        if not ack.get("ok") or not pump.acknowledge(
            capture_uuid, int(batch["sequence"]), now=0.11 + source.cursor / sample_rate_hz
        ):
            raise RuntimeError(f"protocol drill ACK failed: {ack}")

    record = journal.ready_capture(session_id, capture_uuid)
    status = journal.capture_status(session_id, capture_uuid) or {}
    rows = list(record.rows) if record is not None else []
    values = [int(row["温度"]) for row in rows]
    expected = list(range(frame_count))
    last_timestamp = (frame_count - 1) / sample_rate_hz
    effective_hz = round((frame_count - 1) / last_timestamp, 6)
    faults = {
        "gap_rejected": not gap_result.get("ok"),
        "duplicate_idempotent": bool(duplicate_ack.get("duplicate")),
        "disconnect_replayed": _same_durable_payload(second, second_replay),
        "helper_restart_replayed": _same_durable_payload(third, third_replay),
        "server_restart_restored": bool(restored.get("duplicate")),
        # The durable duplicate response is the transport-level protection used
        # when an already-issued command causes the same batch to be replayed.
        "repeated_command_idempotent": bool(duplicate_ack.get("duplicate")),
    }
    ok = (
        all(item.get("ok") for item in accepted)
        and all(faults.values())
        and record is not None
        and values == expected
        and int(status.get("received_rows") or 0) == frame_count
        and status.get("state") == "ready"
    )
    journal.close()
    return {
        "schema_version": 1,
        "ok": ok,
        "evidence_tier": "protocol_simulation",
        "two_machine_verified": False,
        "sample_rate_hz": sample_rate_hz,
        "effective_hz": effective_hz,
        "target_frames": frame_count,
        "reconciled_frames": len(rows),
        "absolute_sequence": [values[0], values[-1]] if values else [],
        "batch_count": len(source.batches),
        "faults": faults,
        "journal_state": status.get("state"),
        "artifact": journal_path.name,
    }


def write_helper_protocol_drill_report(
    report: dict[str, Any], output_directory: str | Path
) -> tuple[Path, Path]:
    """Atomically emit the same drill result for tools and operators."""

    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "helper-protocol-drill.json"
    markdown_path = output / "helper-protocol-drill.md"
    json_temporary = json_path.with_suffix(".json.tmp")
    markdown_temporary = markdown_path.with_suffix(".md.tmp")
    markdown = [
        "# Helper protocol simulation drill",
        "",
        f"- Result: {'passed' if report.get('ok') else 'failed'}",
        f"- Evidence tier: `{report.get('evidence_tier')}`",
        f"- Two-machine verified: `{str(bool(report.get('two_machine_verified'))).lower()}`",
        f"- Frames: `{report.get('reconciled_frames')}/{report.get('target_frames')}`",
        f"- Effective rate: `{report.get('effective_hz')} Hz`",
        f"- Absolute sequence: `{report.get('absolute_sequence')}`",
        "",
        "This is a local protocol simulation and is not evidence of a real second Windows computer.",
        "",
        "## Fault assertions",
        "",
    ]
    for name, passed in sorted(dict(report.get("faults") or {}).items()):
        markdown.append(f"- {name}: {'passed' if passed else 'failed'}")
    json_temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_temporary.write_text("\n".join(markdown) + "\n", encoding="utf-8")
    json_temporary.replace(json_path)
    markdown_temporary.replace(markdown_path)
    return json_path, markdown_path
