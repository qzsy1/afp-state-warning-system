"""真实采集启动任务及仅限当前进程的检测授权。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import threading
import time
from acquisition_clock import monotonic
import uuid

from real_acquisition import readiness_config_fingerprint


class AcquisitionStartMixin:
    def _init_start_lifecycle(self):
        self._start_job_lock = threading.RLock()
        self._start_thread = None
        self._start_cancel = threading.Event()
        self._start_job = {}
        self.readiness_session_id = uuid.uuid4().hex
        self.readiness_version = 0
        self.readiness_invalid_reason = "initial_preparation"
        self._session_readiness_fingerprint = ""
        self._interface_readiness_cache = {}
        self._start_events = []
        self._source_observations = {}
        self._curve_start_observations = {}
        self._curve_start_saved = False
        self._capture_start_monotonic = None
        self._stream_version_signature = None
        self._stream_version_value = 0

    def stream_version(self):
        """Return a cheap monotonic version for the live dashboard payload."""

        with self.lock:
            with self._start_job_lock:
                thread = self.thread
                config = self.config
                start_state = str(self._start_job.get("state") or "")
                active = bool(
                    (thread is not None and thread.is_alive())
                    or start_state in {"starting", "cancelling"}
                    or self.capture_phase
                    in {"starting", "running", "stopping_sampling", "finalizing"}
                )
                status_tick = int(monotonic() * (4 if active else 1))
                signature = (
                    status_tick,
                    bool(thread is not None and thread.is_alive()),
                    str(self.capture_phase),
                    str(self.capture_uuid),
                    str(getattr(config, "capture_generation", "")),
                    str(getattr(config, "start_request_id", "")),
                    int(self.total_sample_count),
                    len(self.rows),
                    bool(self._curve_start_saved),
                    len(self._start_events),
                    str(self._start_job.get("id") or ""),
                    start_state,
                    str(self._start_job.get("error") or ""),
                    int(self.readiness_version),
                    str(self.readiness_invalid_reason),
                    bool(self.capture_saved),
                    bool(self.finalization_complete),
                    bool(self.persistence_failed),
                    int(self.persistence_queue_depth),
                    int(self.persistence_queue_high_water_rows),
                    str(self.last_error),
                    self.started_at,
                    self.stopped_at,
                    str((self.mysql_status or {}).get("state") or ""),
                    bool((self.recovery_state or {}).get("blocked")),
                )
                if signature != self._stream_version_signature:
                    self._stream_version_signature = signature
                    self._stream_version_value += 1
                return self._stream_version_value

    def record_start_event(self, stage, **details):
        with self._start_job_lock:
            if len(self._start_events) < 256:
                self._start_events.append({"stage": stage, "server_monotonic": monotonic(), **details})

    def capture_timeline(self):
        with self._start_job_lock:
            return {"clock_domain": "server_monotonic", "start_request_id": self._start_job.get("start_request_id") or getattr(self.config, "start_request_id", ""),
                    "capture_generation": self._start_job.get("capture_generation") or getattr(self.config, "capture_generation", ""),
                    "operation_id": self._start_job.get("id"), "events": deepcopy(self._start_events)}

    def _begin_capture_timeline(self, config):
        if threading.current_thread() is not self._start_thread:
            config.capture_generation = uuid.uuid4().hex
            self._start_job = {}
        self._capture_start_monotonic = monotonic()
        self._start_events = []
        self._source_observations = {}
        self._curve_start_observations = {}
        self._curve_start_saved = False
        self.record_start_event("capture_prepare")

    def session_readiness_matches(self, config):
        return bool(self._readiness_snapshot is not None and
                    self._session_readiness_fingerprint == readiness_config_fingerprint(config))

    def usb_startup_profiles(self, config):
        from esr1001_usb import USB_DRIVER, BOUNDED_START_PARAMETER_HEX, GUARDED_START_PARAMETER_HEX, QUERY, START, STOP
        if not self.session_readiness_matches(config):
            return {}
        enabled = {str(item.get("id")) for item in config.interfaces or []
                   if item.get("enabled", True) and item.get("driver") == USB_DRIVER}
        profiles = {}
        for row in self._latest_check_result.get("interfaces", []):
            evidence = row.get("usb_evidence") or {}
            parameters_hex = (evidence.get("parameters") or {}).get("raw_hex")
            if (str(row.get("id")) not in enabled or not row.get("ok")
                    or row.get("driver") != USB_DRIVER or row.get("state") != "data_ready"
                    or parameters_hex not in {BOUNDED_START_PARAMETER_HEX, GUARDED_START_PARAMETER_HEX}
                    or evidence.get("current_stream_stable") is not True
                    or evidence.get("stop_error") or evidence.get("stop_failures")):
                continue
            matched = {command.get("command_hex"): command.get("matched_response_hex")
                       for command in evidence.get("commands", [])}
            start_response = (b"\x25" + START[1:5] + bytes(8) + b"\xff").hex()
            if (matched.get(QUERY.hex()) == parameters_hex
                    and matched.get(START.hex()) == start_response):
                if parameters_hex == BOUNDED_START_PARAMETER_HEX:
                    profiles[str(row["id"])] = "bounded_response"
                elif matched.get(STOP.hex()) == (b"\x25" + STOP[1:5] + bytes(8) + b"\xff").hex():
                    profiles[str(row["id"])] = "guarded_response"
        return profiles

    def invalidate_readiness(self, interface_ids=None, reason="device_evidence_invalid"):
        with self.lock:
            if self._readiness_snapshot is not None or self._session_readiness_fingerprint:
                self.readiness_version += 1
            self.readiness_invalid_reason = reason
            self._readiness_snapshot = None
            self._session_readiness_fingerprint = ""
            if interface_ids is None:
                self._interface_readiness_cache.clear()
            else:
                for identity in interface_ids:
                    self._interface_readiness_cache.pop(identity, None)

    def _assert_start_not_cancelled(self):
        if self._start_cancel.is_set() and threading.current_thread() is self._start_thread:
            raise RuntimeError("capture_start_cancelled")

    def start_operation_status(self):
        with self._start_job_lock:
            result = dict(self._start_job)
        if result:
            end = result.get("finished_monotonic") or monotonic()
            result["elapsed_seconds"] = max(0.0, end - result["started_monotonic"])
            if result.get("state") == "running" and not (self.thread and self.thread.is_alive()):
                result["startup_result"] = "running"
                result["state"] = "completed" if self.finalization_complete else self.capture_phase
        return result

    def start_async(self, config, *, on_finished=None):
        with self._start_job_lock:
            request_digest = hashlib.sha256(json.dumps(vars(config), sort_keys=True, default=str).encode()).hexdigest()
            if config.start_request_id and self._start_job.get("start_request_id") == config.start_request_id:
                if request_digest != self._start_request_config_digest:
                    raise RuntimeError("start_request_config_conflict")
                if on_finished is not None:
                    on_finished()
                return {"accepted": True, "running": self._start_job.get("state") == "running",
                        "capture_phase": self._start_job.get("state"),
                        "capture_uuid": self._start_job.get("capture_uuid"),
                        "capture_generation": self._start_job.get("capture_generation"),
                        "start_request_id": config.start_request_id, "start_operation": self.start_operation_status()}
            if self._start_thread is not None and self._start_thread.is_alive():
                raise RuntimeError("capture_start_in_progress")
            if self.thread is not None and self.thread.is_alive() and self.config.acquisition_mode == "real":
                raise RuntimeError("真实采集已经在运行，请先停止当前采集")
            self._start_cancel.clear()
            self._start_request_config_digest = request_digest
            config = deepcopy(config)
            config.capture_generation = uuid.uuid4().hex
            config.capture_uuid = self._capture_uuid_for_start(config)
            self._start_job = {"id": uuid.uuid4().hex, "state": "starting",
                               "start_request_id": config.start_request_id,
                               "capture_generation": config.capture_generation,
                               "capture_uuid": config.capture_uuid, "started_monotonic": monotonic(),
                               "first_sample_seconds": None, "all_channels_seconds": None, "error": ""}

            def run():
                state, error = "running", ""
                try:
                    self._assert_start_not_cancelled()
                    self.start(config)
                    with self._start_job_lock:
                        self._assert_start_not_cancelled()
                        self._start_job["state"] = "running"
                except Exception as exc:
                    cancelled = self._start_cancel.is_set()
                    state, error = ("cancelled", "") if cancelled else ("failed", str(exc))
                    if cancelled:
                        from esr1001_usb import USB_DRIVER
                        interrupted_usb = [str(item.get("id")) for item in config.interfaces or []
                                           if item.get("enabled", True) and item.get("driver") == USB_DRIVER]
                        if interrupted_usb:
                            self.invalidate_readiness(interrupted_usb, reason="usb_start_transaction_cancelled")
                    else:
                        self.invalidate_readiness()
                    try:
                        if self.thread is not None and self.thread.is_alive():
                            self._stop_active()
                    except Exception as cleanup:
                        state, error = "failed", str(cleanup)
                    with self.lock:
                        if self.capture_phase == "stop_timed_out":
                            state, error = "failed", self.last_error or "设备释放超时，不能开始新采集"
                        elif self.capture_phase not in {"database_pending_retry", "finalization_failed"}:
                            self.capture_phase = "start_cancelled" if state == "cancelled" else "start_failed"
                        self.last_error = error
                finally:
                    with self._start_job_lock:
                        self._start_job.update(state=state, error=error, finished_monotonic=monotonic())
                        if state == "running":
                            self._start_job["all_channels_seconds"] = (
                                self._start_job["finished_monotonic"] - self._start_job["started_monotonic"])
                    if on_finished is not None:
                        on_finished()

            self._start_thread = threading.Thread(target=run, name="AFP-CaptureStart", daemon=True)
            response = {"accepted": True, "running": False, "capture_phase": "starting",
                        "start_request_id": config.start_request_id, "capture_generation": config.capture_generation,
                        "capture_uuid": config.capture_uuid, "start_operation": dict(self._start_job)}
            self._start_thread.start()
            return response

    def cancel_start(self):
        with self._start_job_lock:
            if (self._start_thread is None or not self._start_thread.is_alive()
                    or self._start_job.get("state") not in {"starting", "cancelling"}):
                return None
            self._start_cancel.set()
            self.stop_event.set()
            self._start_job["state"] = "cancelling"
        driver = self.driver
        if driver is not None and driver is self._connection_driver:
            self.invalidate_data_connection("启动已取消，正在释放设备；下次采集请重新连接数据")
        cancel = getattr(driver, "cancel_open", None)
        if callable(cancel):
            cancel()
        return {"accepted": True, "running": False, "capture_phase": "cancelling_start",
                "capture_uuid": self._start_job["capture_uuid"],
                "start_operation": self.start_operation_status()}

    def _record_start_frame(self, frame=None):
        with self._start_job_lock:
            if self._capture_start_monotonic is None:
                return
            cache = getattr(self.driver, "cache", None)
            if cache is None and frame is None:
                return
            samples = reversed(cache.events()) if cache is not None else frame.channels.values()
            now = monotonic()
            for sample in samples:
                name = sample.channel_name
                if name not in self.config.selected_sensors:
                    continue
                if (sample.quality.value != "measured_new" or sample.value is None
                        or sample.metadata.get("current_stream_stable") is False
                        or sample.protocol_ok is False or sample.source_sequence is None
                        or sample.received_monotonic < self._capture_start_monotonic):
                    continue
                observations = self._source_observations.setdefault(name, [])
                identity = (sample.interface_id, sample.source_sequence, sample.received_monotonic)
                if len(observations) < 2 and identity not in observations:
                    observations.append(identity)
                    self.record_start_event("source_observation", channel=name,
                                            interface_id=sample.interface_id, source_sequence=sample.source_sequence,
                                            received_monotonic=sample.received_monotonic, ordinal=len(observations))
                if self._curve_start_saved:
                    continue
                freshness = float(next((item.get("freshness_seconds") or 1 for item in self.config.interfaces or []
                                        if item.get("id") == sample.interface_id), 1))
                if sample.metadata.get("driver") == "huipu_tc64_modbus_rtu":
                    freshness = max(freshness, 2.5 * float(sample.metadata.get("scan_duration_seconds") or 0)
                                    + float(sample.metadata.get("poll_interval_seconds") or .1))
                if sample.metadata.get("driver") == "esr1001_usb_ttl":
                    freshness = .25
                if not 0 <= now - sample.received_monotonic <= freshness:
                    continue
                points = self._curve_start_observations.setdefault(name, [])
                points[:] = [point for point in points if now - point["received_monotonic"] <= freshness]
                if all(point["source_sequence"] != sample.source_sequence for point in points):
                    points.append({"value": sample.value, "interface_id": sample.interface_id,
                                   "capture_generation": self.config.capture_generation,
                                   "start_request_id": self.config.start_request_id,
                                   "source_sequence": sample.source_sequence, "received_monotonic": sample.received_monotonic,
                                   "quality": "measured_new", "protocol_ok": sample.protocol_ok, "after_start": True,
                                   "stable": sample.metadata.get("current_stream_stable", True)})
                    points.sort(key=lambda point: (point["received_monotonic"], point["source_sequence"]))
                    points[:] = points[-2:]
            if (not self._curve_start_saved and self.config.acquisition_mode == "real"
                    and all(len(self._curve_start_observations.get(name, [])) == 2 for name in self.config.selected_sensors)):
                for points in self._curve_start_observations.values():
                    points.sort(key=lambda point: point["received_monotonic"])
                self._write_start_observations()
                self._curve_start_saved = True
                self.record_start_event("S2_all_source_observations")
