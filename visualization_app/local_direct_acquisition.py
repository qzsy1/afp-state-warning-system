"""Local-device adapter for the explicit ``local_direct`` acquisition mode."""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from real_acquisition import ChannelQuality, ChannelSample, ChannelSampleCache


_MINIMUM_POLL_SECONDS = {
    "serial_json": 0.005,
    "smrf_hid": 0.02,
    "tcp_json": 0.01,
    "modbus_tcp": 0.05,
    "abb_robot": 0.1,
    "uvc_thermal": 0.03,
    "rtsp_thermal": 0.03,
    "m3232_pressure": 0.005,
}


def minimum_poll_interval(driver_name: str) -> float:
    return float(_MINIMUM_POLL_SECONDS.get(str(driver_name or "").strip().lower(), 0.005))


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class LocalDirectAdapter:
    """Own one local driver lifecycle and publish only shared channel samples."""

    mode = "local_direct"

    def __init__(
        self,
        interface_id: str,
        driver: Any,
        channels: Iterable[str],
        cache: ChannelSampleCache,
        *,
        poll_interval_seconds: float = 0.005,
        minimum_poll_seconds: float = 0.005,
        reconnect_backoff_seconds: float = 0.05,
        max_reconnect_backoff_seconds: float = 1.0,
        max_reconnect_attempts: int = 5,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.interface_id = str(interface_id)
        self.driver = driver
        self.channels = frozenset(str(item) for item in channels)
        self.cache = cache
        self.actual_poll_interval_seconds = max(
            float(poll_interval_seconds), float(minimum_poll_seconds), 0.0
        )
        self.reconnect_backoff_seconds = max(0.0, float(reconnect_backoff_seconds))
        self.max_reconnect_backoff_seconds = max(
            self.reconnect_backoff_seconds,
            float(max_reconnect_backoff_seconds),
        )
        self.max_reconnect_attempts = max(0, int(max_reconnect_attempts))
        self.clock = clock
        self.wall_clock = wall_clock
        self.stop_event = threading.Event()
        self.opened = threading.Event()
        self.thread: threading.Thread | None = None
        self.source_sequence = 0
        self.read_calls = 0
        self.reconnect_attempts = 0
        self._consecutive_reconnect_attempts = 0
        self.consecutive_failures = 0
        self.last_error = ""
        self.last_sample_monotonic: float | None = None
        self.state = "idle"

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.state = "opening"
        self.thread = threading.Thread(
            target=self._run,
            name=f"AFP-LocalDirect-{self.interface_id}",
            daemon=True,
        )
        self.thread.start()

    def _publish(self, payload: Mapping[str, Any]) -> bool:
        accepted = [
            (str(name), raw_value)
            for name, raw_value in payload.items()
            if str(name) in self.channels
        ]
        if not accepted:
            return False
        received_monotonic = self.clock()
        received_wall_time = self.wall_clock()
        self.source_sequence += 1
        driver_metadata = getattr(self.driver, "quality_metadata", {})
        for name, raw_value in accepted:
            value = _finite(raw_value)
            self.cache.publish(
                ChannelSample(
                    interface_id=self.interface_id,
                    channel_name=str(name),
                    value=value,
                    quality=(
                        ChannelQuality.MEASURED_NEW
                        if value is not None
                        else ChannelQuality.INVALID
                    ),
                    received_wall_time=received_wall_time,
                    received_monotonic=received_monotonic,
                    source_sequence=self.source_sequence,
                    protocol_ok=getattr(self.driver, "protocol_verified", None),
                    metadata=(
                        driver_metadata if isinstance(driver_metadata, Mapping) else {}
                    ),
                )
            )
        self.last_sample_monotonic = received_monotonic
        self._consecutive_reconnect_attempts = 0
        return True

    def _safe_close(self) -> None:
        try:
            self.driver.close()
        except Exception as exc:
            if not self.last_error:
                self.last_error = str(exc)

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    self.state = "opening" if self.reconnect_attempts == 0 else "reconnecting"
                    self.driver.open()
                    self.opened.set()
                    self.state = "running"
                    while not self.stop_event.is_set():
                        self.read_calls += 1
                        payload = self.driver.read_sample()
                        if isinstance(payload, Mapping) and payload:
                            if self._publish(payload):
                                self.consecutive_failures = 0
                        self.stop_event.wait(self.actual_poll_interval_seconds)
                    break
                except Exception as exc:
                    self.last_error = str(exc) or exc.__class__.__name__
                    self.consecutive_failures += 1
                    self._safe_close()
                    if self.stop_event.is_set():
                        break
                    if self._consecutive_reconnect_attempts >= self.max_reconnect_attempts:
                        self.state = "failed"
                        break
                    self.reconnect_attempts += 1
                    self._consecutive_reconnect_attempts += 1
                    self.state = "reconnecting"
                    delay = min(
                        self.max_reconnect_backoff_seconds,
                        self.reconnect_backoff_seconds
                        * (2 ** max(0, self.reconnect_attempts - 1)),
                    )
                    self.stop_event.wait(delay)
        finally:
            self._safe_close()
            if self.state != "failed":
                self.state = "stopped"

    def stop(self, timeout_seconds: float = 0.5) -> bool:
        self.stop_event.set()
        self._safe_close()
        thread = self.thread
        if thread is not None:
            thread.join(max(0.0, float(timeout_seconds)))
        stopped = thread is None or not thread.is_alive()
        if not stopped:
            self.state = "stop_timed_out"
        return stopped

    def status(self) -> dict[str, Any]:
        thread = self.thread
        first_sample_received = self.last_sample_monotonic is not None
        reported_state = self.state
        if reported_state == "running" and not first_sample_received:
            reported_state = "waiting_first_sample"
        return {
            "mode": self.mode,
            "fault_domain": "local_device",
            "interface_id": self.interface_id,
            "state": reported_state,
            "running": bool(thread is not None and thread.is_alive()),
            "opened": self.opened.is_set(),
            "first_sample_received": first_sample_received,
            "read_calls": self.read_calls,
            "source_sequence": self.source_sequence,
            "reconnect_attempts": self.reconnect_attempts,
            "consecutive_reconnect_attempts": self._consecutive_reconnect_attempts,
            "consecutive_failures": self.consecutive_failures,
            "actual_poll_interval_seconds": self.actual_poll_interval_seconds,
            "last_sample_monotonic": self.last_sample_monotonic,
            "last_error": self.last_error,
        }
