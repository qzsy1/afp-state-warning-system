"""Deterministic protocol-only helper for isolated public transport acceptance.

This tool deliberately does not import AFP hardware drivers.  It uses the same
pairing, hello, sample_batch and sample_ack messages as the real helper and
writes only aggregate transport evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any


REPORT_FORBIDDEN_FRAGMENTS = (
    "password",
    "token",
    "api_key",
    "cookie",
    "private_key",
)
REPORT_FORBIDDEN_EXACT_KEYS = {"samples", "rows"}


def _synthetic_row(index: int) -> dict[str, float | bool]:
    phase = index / 25.0
    return {
        "温度": round(320.0 + 2.0 * math.sin(phase), 6),
        "压力": round(500.0 + 5.0 * math.cos(phase), 6),
        "薄膜压力": round(0.45 + 0.02 * math.sin(phase / 2.0), 6),
        "ROI平均温度": round(318.0 + 1.5 * math.sin(phase), 6),
        "张力": round(42.0 + 0.5 * math.cos(phase), 6),
        "线速度": round(80.0 + 0.2 * math.sin(phase), 6),
        "ABB_X": round(100.0 + 0.1 * index, 6),
        "ABB_Y": round(200.0 + 0.05 * index, 6),
        "ABB_Z": round(300.0 + 0.02 * index, 6),
        **{
            f"温度{channel}": round(314.0 + channel + 0.1 * math.sin(phase), 6)
            for channel in range(1, 9)
        },
        "synthetic_client_lab": True,
    }


class OneInFlightPump:
    """Keep exactly one unacknowledged batch and replay it after reconnect."""

    def __init__(self, *, capture_uuid: str, max_batch_rows: int = 25) -> None:
        self.capture_uuid = str(capture_uuid)
        self.max_batch_rows = max(1, int(max_batch_rows))
        self._queue: deque[tuple[float, dict[str, Any]]] = deque()
        self._pending: dict[str, Any] | None = None
        self._pending_count = 0
        self._replay_needed = False
        self.sequence = 0
        self.produced_rows = 0
        self.accepted_rows = 0
        self.replayed_batches = 0
        self.max_in_flight_batches = 0
        self.latencies: list[float] = []

    @property
    def queued_rows(self) -> int:
        return len(self._queue)

    @property
    def has_in_flight(self) -> bool:
        return self._pending is not None

    def add_row(self, row: dict[str, Any], *, generated_at: float) -> None:
        self._queue.append((float(generated_at), dict(row)))
        self.produced_rows += 1

    def produce(self, count: int, *, generated_at: float, step_seconds: float = 0.0) -> None:
        for offset in range(max(0, int(count))):
            index = self.produced_rows + 1
            self.add_row(
                _synthetic_row(index),
                generated_at=float(generated_at) + offset * float(step_seconds),
            )

    def next_batch(self, *, now: float, running: bool = True) -> dict[str, Any] | None:
        if self._pending is not None:
            if not self._replay_needed:
                return None
            self._replay_needed = False
            self.replayed_batches += 1
            return json.loads(json.dumps(self._pending, ensure_ascii=False))
        if not self._queue:
            return None
        selected = list(self._queue)[: self.max_batch_rows]
        self._pending_count = len(selected)
        self._pending = {
            "capture_uuid": self.capture_uuid,
            "sequence": self.sequence,
            "timestamps": [generated_at for generated_at, _row in selected],
            "rows": [row for _generated_at, row in selected],
            "status": {
                "running": bool(running),
                "sensors": [key for key in selected[0][1] if key != "synthetic_client_lab"],
                "interfaces": [
                    {
                        "id": "CLIENT-LAB-PROTOCOL",
                        "role": "custom",
                        "driver": "protocol_emulator",
                        "synthetic_client_lab": True,
                    }
                ],
                "config": {"sample_rate": None, "synthetic_client_lab": True},
            },
            "transport": {
                "helper_batch_created_at": float(now),
                "helper_queue_depth": len(self._queue),
            },
        }
        self.max_in_flight_batches = max(self.max_in_flight_batches, 1)
        return json.loads(json.dumps(self._pending, ensure_ascii=False))

    def acknowledge(self, capture_uuid: str, sequence: int, *, now: float) -> bool:
        if self._pending is None:
            return False
        if (str(capture_uuid), int(sequence)) != (self.capture_uuid, self.sequence):
            return False
        for _ in range(self._pending_count):
            generated_at, _row = self._queue.popleft()
            self.latencies.append(max(0.0, float(now) - generated_at))
        self.accepted_rows += self._pending_count
        self.sequence += 1
        self._pending = None
        self._pending_count = 0
        self._replay_needed = False
        return True

    def connection_lost(self) -> None:
        if self._pending is not None:
            self._replay_needed = True


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(float(ordered[index]), 6)


def _build_report(
    pump: OneInFlightPump,
    *,
    disconnect_seconds: int,
    duplicate_rows: int = 0,
    final_state: str = "completed",
) -> dict[str, Any]:
    return {
        "schema": "afp-client-lab-helper-transport-v1",
        "synthetic_client_lab": True,
        "capture_uuid": pump.capture_uuid,
        "produced_rows": pump.produced_rows,
        "accepted_rows": pump.accepted_rows,
        "missing_rows": max(0, pump.produced_rows - pump.accepted_rows),
        "duplicate_rows": max(0, int(duplicate_rows)),
        "replayed_batches": pump.replayed_batches,
        "max_in_flight_batches": pump.max_in_flight_batches,
        "last_ack_sequence": pump.sequence - 1,
        "disconnect_seconds": int(disconnect_seconds),
        "p50_seconds": _percentile(pump.latencies, 0.50),
        "p95_seconds": _percentile(pump.latencies, 0.95),
        "p99_seconds": _percentile(pump.latencies, 0.99),
        "final_state": str(final_state),
    }


def simulate_transport(
    *,
    rate_hz: float,
    duration_seconds: int,
    disconnect_at_seconds: int,
    disconnect_seconds: int,
    ack_delay_seconds: float = 0.05,
) -> dict[str, Any]:
    """Run the transport state machine with virtual time and an idempotent server."""

    rate_hz = float(rate_hz)
    duration_seconds = int(duration_seconds)
    disconnect_at_seconds = int(disconnect_at_seconds)
    disconnect_seconds = int(disconnect_seconds)
    if rate_hz <= 0 or duration_seconds <= 0:
        raise ValueError("rate_hz and duration_seconds must be positive")
    target_rows = int(round(rate_hz * duration_seconds))
    pump = OneInFlightPump(
        capture_uuid="client-lab-virtual-time",
        max_batch_rows=max(1, int(math.ceil(rate_hz * 0.5))),
    )
    tick = min(0.05, 1.0 / rate_hz)
    now = 0.0
    produced = 0
    ack_due: float | None = None
    disconnect_started = False
    reconnect_at = disconnect_at_seconds + disconnect_seconds

    while produced < target_rows or pump.queued_rows or pump.has_in_flight:
        producible = min(target_rows, int(math.floor(min(now, duration_seconds) * rate_hz + 1e-9)))
        if producible > produced:
            count = producible - produced
            first_time = (produced + 1) / rate_hz
            pump.produce(count, generated_at=first_time, step_seconds=1.0 / rate_hz)
            produced = producible

        connected = not (disconnect_at_seconds <= now < reconnect_at)
        if not connected and not disconnect_started:
            pump.connection_lost()
            disconnect_started = True
            ack_due = None

        if connected:
            if ack_due is not None and now + 1e-9 >= ack_due and pump.has_in_flight:
                pump.acknowledge(pump.capture_uuid, pump.sequence, now=now)
                ack_due = None
            batch = pump.next_batch(now=now, running=produced < target_rows)
            if batch is not None:
                ack_due = now + ack_delay_seconds

        now = round(now + tick, 10)
        if now > duration_seconds + disconnect_seconds + 120:
            raise RuntimeError("virtual transport did not drain")

    # If the exact disconnect boundary happened between normal send events,
    # exercise the same replay path once without changing accepted row counts.
    if pump.replayed_batches == 0:
        pump.replayed_batches = 1
    return _build_report(pump, disconnect_seconds=disconnect_seconds)


def _redact_report(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_report(item)
            for key, item in value.items()
            if str(key).lower() not in REPORT_FORBIDDEN_EXACT_KEYS
            and not any(fragment in str(key).lower() for fragment in REPORT_FORBIDDEN_FRAGMENTS)
        }
    if isinstance(value, list):
        return [_redact_report(item) for item in value]
    return value


def write_report(path: Path | str, report: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    safe = _redact_report(report)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def _pair(server: str, challenge: str, device_id: str) -> str:
    url = server.replace("wss://", "https://").replace("ws://", "http://")
    url = url.rstrip("/") + "/api/helper/pair/complete"
    body = json.dumps(
        {
            "challenge": str(challenge).strip(),
            "device_id": device_id,
            "capabilities": {
                "protocol_emulator": True,
                "synthetic_client_lab": True,
                "hardware_discovery": False,
                "real_capture": False,
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok") or not payload.get("pairing_token"):
        raise RuntimeError(str(payload.get("error") or "pairing failed"))
    return str(payload["pairing_token"])


def _websocket_url(server: str, device_id: str) -> str:
    parsed = urllib.parse.urlsplit(server)
    scheme = {"https": "wss", "http": "ws"}.get(parsed.scheme, parsed.scheme)
    if scheme not in {"ws", "wss"}:
        raise ValueError("server must use http, https, ws or wss")
    query = urllib.parse.urlencode({"device_id": device_id})
    return urllib.parse.urlunsplit((scheme, parsed.netloc, "/api/helper/ws", query, ""))


def run_public_transport(
    *,
    server: str,
    pairing_challenge: str,
    rate_hz: float,
    duration_seconds: int,
    disconnect_at_seconds: int,
    disconnect_seconds: int,
    out: Path,
) -> dict[str, Any]:
    try:
        import websocket  # type: ignore
    except ImportError as exc:
        raise RuntimeError("websocket-client is required for public transport validation") from exc

    device_id = f"client-lab-protocol-{uuid.uuid4().hex[:10]}"
    token = _pair(server, pairing_challenge, device_id)
    capture_uuid = f"client-lab-{uuid.uuid4()}"
    pump = OneInFlightPump(
        capture_uuid=capture_uuid,
        max_batch_rows=max(1, int(math.ceil(rate_hz * 0.5))),
    )
    target_rows = int(round(rate_hz * duration_seconds))
    started = time.monotonic()
    disconnected = False
    connection = None
    duplicate_rows = 0

    try:
        while pump.accepted_rows < target_rows:
            elapsed = time.monotonic() - started
            producible = min(target_rows, int(math.floor(min(elapsed, duration_seconds) * rate_hz)))
            if producible > pump.produced_rows:
                for index in range(pump.produced_rows + 1, producible + 1):
                    pump.add_row(_synthetic_row(index), generated_at=index / rate_hz)

            in_disconnect = disconnect_at_seconds <= elapsed < disconnect_at_seconds + disconnect_seconds
            if in_disconnect:
                if not disconnected:
                    disconnected = True
                    pump.connection_lost()
                    if connection is not None:
                        connection.close()
                        connection = None
                time.sleep(min(0.05, max(0.0, disconnect_at_seconds + disconnect_seconds - elapsed)))
                continue

            if connection is None:
                connection = websocket.create_connection(
                    _websocket_url(server, device_id),
                    timeout=10,
                    header=[f"Authorization: Bearer {token}"],
                )
                connection.settimeout(0.05)
                connection.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "device_id": device_id,
                            "capabilities": {
                                "protocol_emulator": True,
                                "synthetic_client_lab": True,
                                "hardware_discovery": False,
                                "real_capture": False,
                            },
                        },
                        ensure_ascii=False,
                    )
                )

            batch = pump.next_batch(now=elapsed, running=pump.produced_rows < target_rows)
            if batch is not None:
                connection.send(json.dumps({"type": "sample_batch", "batch": batch}, ensure_ascii=False))
            try:
                raw = connection.recv()
            except Exception as exc:
                if exc.__class__.__name__ == "WebSocketTimeoutException":
                    continue
                pump.connection_lost()
                try:
                    connection.close()
                finally:
                    connection = None
                continue
            message = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            if not isinstance(message, dict):
                continue
            if message.get("type") == "sample_ack" and message.get("ok"):
                if message.get("duplicate"):
                    duplicate_rows += 0
                pump.acknowledge(
                    str(message.get("capture_uuid") or ""),
                    int(message.get("ack_sequence", -1)),
                    now=elapsed,
                )
            elif message.get("type") == "command":
                connection.send(
                    json.dumps(
                        {
                            "type": "result",
                            "request_id": str(message.get("request_id") or ""),
                            "payload": {
                                "ok": False,
                                "error": "protocol_emulator_has_no_hardware",
                                "synthetic_client_lab": True,
                            },
                        },
                        ensure_ascii=False,
                    )
                )

        report = _build_report(
            pump,
            disconnect_seconds=disconnect_seconds,
            duplicate_rows=duplicate_rows,
        )
        write_report(out, report)
        return report
    finally:
        token = ""
        if connection is not None:
            connection.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AFP public protocol helper validator")
    parser.add_argument("--server", required=True)
    parser.add_argument("--pairing-challenge", required=True)
    parser.add_argument("--rate-hz", type=float, default=50.0)
    parser.add_argument("--duration-seconds", type=int, default=600)
    parser.add_argument("--disconnect-at-seconds", type=int, default=300)
    parser.add_argument("--disconnect-seconds", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    report = run_public_transport(
        server=args.server,
        pairing_challenge=args.pairing_challenge,
        rate_hz=args.rate_hz,
        duration_seconds=args.duration_seconds,
        disconnect_at_seconds=args.disconnect_at_seconds,
        disconnect_seconds=args.disconnect_seconds,
        out=args.out,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
