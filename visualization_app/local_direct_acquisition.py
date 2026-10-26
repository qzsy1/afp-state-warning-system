"""Local-device adapter for the explicit ``local_direct`` acquisition mode."""

from __future__ import annotations

import math
import threading
import time
from acquisition_clock import monotonic, wait_interruptible
from copy import deepcopy
from typing import Any, Callable, Iterable, Mapping

from real_acquisition import ChannelQuality, ChannelSample, ChannelSampleCache


_MINIMUM_POLL_SECONDS = {
    "serial_json": 0.005,
    "smrf_hid": 0.02,
    "tcp_json": 0.01,
    "modbus_tcp": 0.1,
    "abb_robot": 0.1,
    "uvc_thermal": 0.03,
    "rtsp_thermal": 0.03,
    "m3232_pressure": 0.005,
    "modbus_rtu_esr1001": 0.05,
    "esr1001_usb_ttl": 0.0,
    "huipu_tc64_modbus_rtu": 0.1,
}

_ESR1001_USB_DRIVER = "esr1001_usb_ttl"
_DEFAULT_USB_RECONNECT_WINDOW_SECONDS = 60.0
_MIN_WINDOW_RECONNECT_BACKOFF_SECONDS = 0.05


def minimum_poll_interval(driver_name: str) -> float:
    return float(_MINIMUM_POLL_SECONDS.get(str(driver_name or "").strip().lower(), 0.005))


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _driver_name(driver: Any) -> str:
    try:
        metadata = getattr(driver, "quality_metadata", {})
    except Exception:
        return ""
    if not isinstance(metadata, Mapping):
        return ""
    return str(metadata.get("driver") or "").strip().lower()


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
        reconnect_window_seconds: float | None = None,
        before_open: Callable[[], Any] | None = None,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.interface_id = str(interface_id)
        self.driver = driver
        self.channels = frozenset(str(item) for item in channels)
        self.cache = cache
        self.actual_poll_interval_seconds = max(
            float(poll_interval_seconds), float(minimum_poll_seconds), 0.0
        )
        self.fixed_poll_interval_seconds = getattr(driver, "fixed_poll_interval_seconds", None)
        if self.fixed_poll_interval_seconds is not None:
            self.actual_poll_interval_seconds = float(self.fixed_poll_interval_seconds)
        self.skipped_poll_cycles = 0
        self.last_round_seconds = None
        self.observed_poll_interval_seconds = None
        self._previous_round_start = None
        self.reconnect_backoff_seconds = max(0.0, float(reconnect_backoff_seconds))
        self.max_reconnect_backoff_seconds = max(
            self.reconnect_backoff_seconds,
            float(max_reconnect_backoff_seconds),
        )
        self.max_reconnect_attempts = max(0, int(max_reconnect_attempts))
        if reconnect_window_seconds is None:
            reconnect_window_seconds = (
                _DEFAULT_USB_RECONNECT_WINDOW_SECONDS
                if _driver_name(driver) in {_ESR1001_USB_DRIVER, "huipu_tc64_modbus_rtu", "m3232_pressure"}
                else 0.0
            )
        self.reconnect_window_seconds = max(0.0, float(reconnect_window_seconds))
        if self.reconnect_window_seconds > 0.0:
            self.max_reconnect_backoff_seconds = max(
                self.max_reconnect_backoff_seconds,
                _MIN_WINDOW_RECONNECT_BACKOFF_SECONDS,
            )
        self.before_open = before_open
        self.clock = clock
        self.wall_clock = wall_clock
        self.stop_event = threading.Event()
        self.opened = threading.Event()
        self.opened_monotonic = None
        self.thread: threading.Thread | None = None
        self._close_lock = threading.Lock()
        self._stop_close_thread: threading.Thread | None = None
        self.source_sequence = 0
        self.read_calls = 0
        self.successful_read_calls = 0
        self.reconnect_attempts = 0
        self._consecutive_reconnect_attempts = 0
        self._reconnect_window_started_monotonic: float | None = None
        self.consecutive_failures = 0
        self.last_error = ""
        self.release_error = ""
        self.last_sample_monotonic: float | None = None
        self.state = "idle"
        self.start_events = []
        self.source_observations: list[dict[str, float | int]] = []
        self.receive_lock = threading.RLock()
        self.capture_paused = False
        self._discard_next_payload = False
        self._sampling_boundary_reset = False

    def _discard_paused_input(self):
        """Bound receive work without publishing or recording paused samples."""
        port = getattr(self.driver, "serial", None)
        if port is not None:
            reset = getattr(port, "reset_input_buffer", None)
            if callable(reset):
                reset()
            else:
                pending = int(getattr(port, "in_waiting", 0))
                if pending:
                    port.read(min(pending, 16384))
            return
        if self.driver.__class__.__name__ == "TcpJsonDriver":
            self._discard_socket_input()
            return
        self.driver.read_sample()

    def _discard_socket_input(self):
        import select
        sock = self.driver.sock
        for _ in range(16):
            if not select.select([sock], [], [], 0)[0]:
                return
            if not sock.recv(16384):
                raise ConnectionError("数据流连接已关闭")
        if select.select([sock], [], [], 0)[0]:
            raise RuntimeError("resume_buffer_overflow: 设备缓存超出有界丢弃容量")

    def reset_sampling_boundary(self):
        """Discard partial input while preserving session counters and recorders."""
        device = self.driver
        self._sampling_boundary_reset = True
        port = getattr(device, "serial", None)
        if port is not None:
            reset = getattr(port, "reset_input_buffer", None)
            if callable(reset):
                reset()
            else:
                pending = int(getattr(port, "in_waiting", 0))
                if pending > 16384:
                    raise RuntimeError("resume_buffer_overflow")
                if pending:
                    port.read(pending)
        kind = _driver_name(device)
        if kind == _ESR1001_USB_DRIVER:
            device.parser = type(device.parser)(device.parser.channels)
            device._pending.clear()
            device._capture_boundary_skip_bytes = 0
            device._capture_discard_frames = 1
            device.last_observation = None
            device.started_monotonic = self.clock()
            device.recent_intervals.clear()
        elif kind == "m3232_pressure":
            offset = device.parser.offset + len(device.parser.buffer)
            device.parser = device._parser()
            device.parser.offset = offset
            device._boundary_discard_line = True
            device.last_frame = None
            device.started = self.clock()
        if hasattr(device, "_rx_buffer"):
            device._rx_buffer.clear()
        if hasattr(device, "_previous_position"):
            device._previous_position = None
        if device.__class__.__name__ == "TcpJsonDriver":
            device.file.close()
            self._discard_socket_input()
            device.file = device.sock.makefile("rb")
        self._discard_next_payload = device.__class__.__name__ in {"SerialJsonDriver", "TcpJsonDriver"}
        self.source_observations.clear()
        self._previous_round_start = None

    def _event(self, stage):
        if len(self.start_events) < 64:
            self.start_events.append({"stage": stage, "server_monotonic": self.clock()})

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

    def _publish(self, payload: Mapping[str, Any], record: Mapping[str, Any] | None = None) -> bool:
        if self._discard_next_payload:
            self._discard_next_payload = False
            return False
        accepted = [
            (str(name), raw_value)
            for name, raw_value in payload.items()
            if str(name) in self.channels
        ]
        if not accepted:
            return False
        self._sampling_boundary_reset = False
        received_monotonic = float(record["host_monotonic"]) if record else self.clock()
        received_wall_time = float(record["host_wall_time"]) if record else self.wall_clock()
        plc_record = record if isinstance(record, Mapping) and record.get("measurement_profile") == "fibercontrol_plc_v1" else None
        abb_record = record if isinstance(record, Mapping) and record.get("measurement_profile") == "fibercontrol_abb_v1" else None
        self.source_sequence = int(record["source_sequence"]) if plc_record or abb_record else self.source_sequence + 1
        self.source_observations.append({
            "source_sequence": self.source_sequence,
            "received_monotonic": received_monotonic,
            "received_wall_time": received_wall_time,
        })
        self.source_observations = self.source_observations[-128:]
        driver_metadata = getattr(self.driver, "quality_metadata", {})
        if isinstance(driver_metadata, Mapping) and driver_metadata.get("driver") == "huipu_tc64_modbus_rtu":
            received_monotonic = float(driver_metadata.get("received_monotonic") or received_monotonic)
            received_wall_time = float(driver_metadata.get("received_wall_time") or received_wall_time)
        if record and isinstance(driver_metadata, Mapping):
            driver_metadata = dict(driver_metadata, latest_record=dict(record))
        if plc_record:
            driver_metadata = {"driver": "modbus_tcp", "measurement_profile": plc_record["measurement_profile"],
                               "latest_record": deepcopy(plc_record)}
        if abb_record:
            driver_metadata = {"driver": "abb_robot", "measurement_profile": abb_record["measurement_profile"],
                               "latest_record": deepcopy(abb_record), "time_basis": abb_record["time_basis"]}
        samples = []
        for name, raw_value in accepted:
            value = _finite(raw_value)
            samples.append(
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
                    device_timestamp=getattr(self.driver, "source_timestamp", None),
                    source_sequence=self.source_sequence,
                    protocol_ok=plc_record["protocol_ok"] if plc_record else getattr(self.driver, "protocol_verified", None),
                    error=(str(plc_record.get("error") or "") if plc_record else
                           str((driver_metadata.get("channel_errors") or {}).get(name) or "")
                           if isinstance(driver_metadata, Mapping) and driver_metadata.get("driver") == "huipu_tc64_modbus_rtu" else ""),
                    metadata=(
                        driver_metadata if isinstance(driver_metadata, Mapping) else {}
                    ),
                )
            )
        self.cache.publish_batch(samples)
        if self.source_sequence <= 2:
            self._event("source_first" if self.source_sequence == 1 else "source_second")
        self.last_sample_monotonic = received_monotonic
        if not plc_record:
            self._consecutive_reconnect_attempts = 0
            self._reconnect_window_started_monotonic = None
        return True

    def _reconnect_window_remaining(self, now: float | None = None) -> float:
        started = self._reconnect_window_started_monotonic
        if started is None:
            return 0.0
        current = self.clock() if now is None else float(now)
        return max(0.0, self.reconnect_window_seconds - (current - started))

    def _safe_close(self) -> None:
        with self._close_lock:
            try:
                if (self.capture_paused or self._sampling_boundary_reset) and _driver_name(self.driver) == _ESR1001_USB_DRIVER:
                    self.reset_sampling_boundary()
                    self.driver._capture_discard_frames = 1 << 60
                    # Shutdown accepts control replies without publishing any data frames.
                    self.driver.parser.synced = True
                self.driver.close()
                self.release_error = ""
            except Exception as exc:
                self.release_error = str(exc)
                if not self.last_error:
                    self.last_error = str(exc)

    def _run(self) -> None:
        round_start = None
        try:
            while not self.stop_event.is_set():
                try:
                    self.state = "opening" if self.reconnect_attempts == 0 else "reconnecting"
                    self._event("open_begin")
                    if self.before_open is not None:
                        self._event("network_path_begin")
                        self.before_open()
                        self._event("network_path_end")
                    reset_cancel = getattr(self.driver, "reset_open_cancellation", None)
                    if callable(reset_cancel):
                        reset_cancel()
                    if self.stop_event.is_set():
                        break
                    self.driver.open()
                    self.opened_monotonic = self.clock()
                    self._event("protocol_open_end")
                    self.opened.set()
                    self.state = "running"
                    next_poll = self.clock()
                    while not self.stop_event.is_set():
                        round_start = self.clock()
                        if self._previous_round_start is not None:
                            self.observed_poll_interval_seconds = round_start - self._previous_round_start
                        self._previous_round_start = round_start
                        with self.receive_lock:
                            paused = self.capture_paused
                            if paused:
                                self._discard_paused_input()
                            else:
                                self.read_calls += 1
                            if not paused and hasattr(self.driver, "read_samples"):
                                for payload, record in self.driver.read_samples():
                                    if self._publish(payload, record):
                                        self.consecutive_failures = 0
                                if self.fixed_poll_interval_seconds is not None:
                                    self._consecutive_reconnect_attempts = 0
                                    self._reconnect_window_started_monotonic = None
                            elif not paused:
                                payload = self.driver.read_sample()
                                if isinstance(payload, Mapping) and payload:
                                    if self._publish(payload):
                                        self.consecutive_failures = 0
                        if paused:
                            self.stop_event.wait(max(.002, self.actual_poll_interval_seconds))
                            next_poll = self.clock()
                            continue
                        self.last_round_seconds = max(0.0, self.clock() - round_start)
                        self.successful_read_calls += 1
                        if self.fixed_poll_interval_seconds is not None:
                            next_poll, skipped = self.next_poll_deadline(next_poll, self.clock(), self.actual_poll_interval_seconds)
                            self.skipped_poll_cycles += skipped
                            delay = max(0.0, next_poll - self.clock())
                            if _driver_name(self.driver) == "abb_robot" and self.clock is monotonic:
                                wait_interruptible(self.stop_event, delay, clock=self.clock)
                            else:
                                self.stop_event.wait(delay)
                        else:
                            self.stop_event.wait(self.actual_poll_interval_seconds)
                    break
                except Exception as exc:
                    if self.fixed_poll_interval_seconds is not None and round_start is not None:
                        self.last_round_seconds = max(0.0, self.clock() - round_start)
                    self.last_error = str(exc) or exc.__class__.__name__
                    self.consecutive_failures += 1
                    self._safe_close()
                    if self.stop_event.is_set():
                        break
                    if (_driver_name(self.driver) == "huipu_tc64_modbus_rtu" and getattr(exc, "code", "") == "binding_unconfirmed") or "binding_unconfirmed:" in str(exc):
                        self.state = "failed"
                        break
                    now = self.clock()
                    recovery_window_enabled = (
                        self.max_reconnect_attempts > 0
                        and self.reconnect_window_seconds > 0.0
                        and self.last_sample_monotonic is not None
                    )
                    if (
                        recovery_window_enabled
                        and self._reconnect_window_started_monotonic is None
                    ):
                        self._reconnect_window_started_monotonic = now
                    retry_by_attempts = (
                        self._consecutive_reconnect_attempts
                        < self.max_reconnect_attempts
                    )
                    retry_by_window = (
                        recovery_window_enabled
                        and self._reconnect_window_remaining(now) > 0.0
                    )
                    if not (retry_by_attempts or retry_by_window):
                        self.state = "failed"
                        break
                    self.reconnect_attempts += 1
                    self._consecutive_reconnect_attempts += 1
                    self.state = "reconnecting"
                    delay = min(
                        self.max_reconnect_backoff_seconds,
                        self.reconnect_backoff_seconds
                        * (2 ** min(30, max(0, self._consecutive_reconnect_attempts - 1))),
                    )
                    if recovery_window_enabled:
                        delay = max(delay, _MIN_WINDOW_RECONNECT_BACKOFF_SECONDS)
                    if retry_by_window and not retry_by_attempts:
                        delay = min(delay, self._reconnect_window_remaining(now))
                    self.stop_event.wait(delay)
        finally:
            self._safe_close()
            if self.state != "failed":
                self.state = "stopped"

    @staticmethod
    def next_poll_deadline(previous: float, completed: float, period: float) -> tuple[float, int]:
        deadline = previous + period
        skipped = max(0, math.floor((completed - deadline) / period) + 1) if completed > deadline else 0
        return deadline + skipped * period, skipped

    def stop(self, timeout_seconds: float = 0.5) -> bool:
        deadline = monotonic() + max(0.0, float(timeout_seconds))
        self.stop_event.set()
        cancel = getattr(self.driver, "cancel_open", None)
        if callable(cancel):
            cancel()
        close_thread = self._stop_close_thread
        if close_thread is None or not close_thread.is_alive():
            close_thread = threading.Thread(
                target=self._safe_close,
                name=f"AFP-Close-{self.interface_id}",
                daemon=True,
            )
            self._stop_close_thread = close_thread
            close_thread.start()
        close_thread.join(max(0.0, deadline - monotonic()))
        thread = self.thread
        if thread is not None:
            thread.join(max(0.0, deadline - monotonic()))
        stopped = (
            (thread is None or not thread.is_alive())
            and not close_thread.is_alive()
            and not bool(getattr(self.driver, "native_call_pending", False))
            and not self.release_error
        )
        if not stopped:
            self.state = "stop_timed_out"
        return stopped

    def status(self) -> dict[str, Any]:
        thread = self.thread
        first_sample_received = self.last_sample_monotonic is not None
        reported_state = self.state
        if reported_state == "running" and not first_sample_received:
            reported_state = "waiting_first_sample"
        reconnect_window_remaining = self._reconnect_window_remaining()
        return {
            "driver_quality": dict(getattr(self.driver, "quality_metadata", {}) or {}),
            "raw_file": str(self.driver.recorder.path) if getattr(self.driver, "recorder", None) else None,
            "mode": self.mode,
            "fault_domain": "local_device",
            "interface_id": self.interface_id,
            "state": reported_state,
            "running": bool(thread is not None and thread.is_alive()),
            "opened": self.opened.is_set(),
            "opened_monotonic": self.opened_monotonic,
            "first_sample_received": first_sample_received,
            "read_calls": self.read_calls,
            "successful_read_calls": self.successful_read_calls,
            "read_success_ratio": self.successful_read_calls / self.read_calls if self.read_calls else None,
            "source_sequence": self.source_sequence,
            "source_observations": list(self.source_observations),
            "reconnect_attempts": self.reconnect_attempts,
            "consecutive_reconnect_attempts": self._consecutive_reconnect_attempts,
            "reconnect_window_seconds": self.reconnect_window_seconds,
            "reconnect_window_active": (
                self._reconnect_window_started_monotonic is not None
                and reconnect_window_remaining > 0.0
            ),
            "reconnect_window_remaining_seconds": reconnect_window_remaining,
            "consecutive_failures": self.consecutive_failures,
            "actual_poll_interval_seconds": self.actual_poll_interval_seconds,
            "observed_poll_interval_seconds": self.observed_poll_interval_seconds,
            "last_round_seconds": self.last_round_seconds,
            "skipped_poll_cycles": self.skipped_poll_cycles,
            "last_sample_monotonic": self.last_sample_monotonic,
            "last_error": self.last_error,
            "release_error": self.release_error,
            "start_events": list(self.start_events),
        }
