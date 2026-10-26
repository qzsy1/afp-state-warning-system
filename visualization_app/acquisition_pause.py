"""Confirmed pause/resume boundaries for one acquisition and its open files."""
from contextlib import ExitStack
from copy import deepcopy
import threading
import time

from acquisition_clock import monotonic


class AcquisitionPauseMixin:
    def _init_pause(self):
        self._pause_condition = threading.Condition()
        self._sampling_interrupt = threading.Event()
        self._reset_pause()

    def _reset_pause(self):
        self._sampling_interrupt.clear()
        self.pause_state = "sampling"
        self.control_revision = getattr(self, "control_revision", 0) + 1
        self.pause_intervals = []
        self.segment_start_index = 0
        self.segment_boundary_monotonic = None
        self.pause_error = ""

    def _check_capture_control(self, capture_uuid=None, expected_revision=None, capture_generation=None):
        if capture_uuid and capture_uuid != self.capture_uuid:
            raise RuntimeError("capture_identity_changed: 采集会话已变化，请刷新状态")
        if capture_generation and capture_generation != getattr(self.config, "capture_generation", None):
            raise RuntimeError("capture_generation_changed: 采集批次已变化，请刷新状态")
        if expected_revision is not None and int(expected_revision) != self.control_revision:
            raise RuntimeError("capture_control_conflict: 采集状态已变化，请刷新状态")

    def pause(self, *, capture_uuid=None, capture_generation=None, expected_revision=None, timeout_seconds=None):
        with self.lifecycle_lock:
            self._check_capture_control(capture_uuid, capture_generation=capture_generation)
            if self.pause_state == "paused" and self.thread and self.thread.is_alive():
                return self.status()
            self._check_capture_control(capture_uuid, expected_revision)
            if not self.thread or not self.thread.is_alive() or self.capture_phase not in {"running", "pausing"}:
                raise RuntimeError("capture_not_sampling: 当前采集不能中断")
            with self._pause_condition:
                self.pause_state = "pausing"
                self.capture_phase = "pausing"
                self.control_revision += 1
                self._sampling_interrupt.set()
                self._pause_condition.notify_all()
            return self._await_pause_state("paused", timeout_seconds)

    def resume(self, *, capture_uuid=None, capture_generation=None, expected_revision=None, timeout_seconds=None):
        with self.lifecycle_lock:
            self._check_capture_control(capture_uuid, capture_generation=capture_generation)
            if self.pause_state == "sampling" and self.capture_phase == "running":
                return self.status()
            self._check_capture_control(capture_uuid, expected_revision)
            if not self.thread or not self.thread.is_alive() or self.pause_state not in {"paused", "resuming"}:
                raise RuntimeError("capture_not_paused: 当前采集不能继续")
            with self._pause_condition:
                self.pause_error = ""
                self.pause_state = "resuming"
                self.capture_phase = "resuming"
                self.control_revision += 1
                self._pause_condition.notify_all()
            return self._await_pause_state("sampling", timeout_seconds)

    def _await_pause_state(self, wanted, timeout_seconds):
        timeout = (self.config.first_sample_timeout_seconds + self.config.stop_sampling_timeout_seconds
                   if timeout_seconds is None else max(0, float(timeout_seconds)))
        deadline = monotonic() + timeout
        with self._pause_condition:
            while self.pause_state != wanted:
                if self.pause_error:
                    raise RuntimeError(self.pause_error)
                if self.stop_event.is_set() or not self.thread.is_alive():
                    raise RuntimeError("capture_control_interrupted: 采样已结束，请核查保存状态")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("capture_control_timeout: 尚未确认，请刷新实际采集状态")
                self._pause_condition.wait(min(.05, remaining))
        return self.status()

    def _set_sources_paused(self, paused):
        driver = self.driver
        workers = getattr(driver, "workers", [])
        deadline = monotonic() + self.config.stop_sampling_timeout_seconds
        with ExitStack() as stack:
            for worker in workers:
                if not worker.receive_lock.acquire(timeout=max(0, deadline - monotonic())):
                    raise TimeoutError("capture_boundary_timeout: 设备读取尚未确认结束")
                stack.callback(worker.receive_lock.release)
            if not paused:
                if workers and any(worker.state != "running" or not worker.thread or not worker.thread.is_alive() for worker in workers):
                    raise RuntimeError("resume_connection_not_ready: 设备连接失效，请停止并保存")
                for worker in workers:
                    worker.reset_sampling_boundary()
                if workers:
                    driver.cache.clear()
                self.segment_boundary_monotonic = monotonic()
            for worker in workers:
                worker.capture_paused = paused

    def _pause_checkpoint(self, writer, flush_files):
        """Called only between complete sample submissions by the sampling owner."""
        if self.pause_state == "sampling":
            return False
        if self.pause_state == "pausing":
            try:
                self._set_sources_paused(True)
            except TimeoutError as exc:
                with self._pause_condition:
                    self.pause_error = str(exc)
                    self._pause_condition.notify_all()
                self.stop_event.wait(.05)
                return False
            writer.flush(flush_files, timeout_seconds=self.config.writer_stop_timeout_seconds)
            with self._pause_condition:
                if not self.pause_intervals or self.pause_intervals[-1]["resumed_at"] is not None:
                    self.pause_intervals.append({"paused_at": time.time(), "paused_monotonic": monotonic(),
                        "row_index": self.total_sample_count, "resumed_at": None, "resumed_monotonic": None})
                self.pause_state = "paused"
                self.capture_phase = "paused"
                self.control_revision += 1
                self._pause_condition.notify_all()
        with self._pause_condition:
            while self.pause_state == "paused" and not self.stop_event.is_set():
                self._pause_condition.wait(.05)
        if self.stop_event.is_set():
            return False
        try:
            self._set_sources_paused(False)
            workers = getattr(self.driver, "workers", [])
            deadline = monotonic() + self.config.first_sample_timeout_seconds
            while workers and not self.stop_event.is_set():
                samples = self.driver.cache.snapshot()
                if all(name in samples and samples[name].received_monotonic > self.segment_boundary_monotonic
                       and samples[name].value is not None and samples[name].protocol_ok is not False
                       and samples[name].metadata.get("current_stream_stable") is not False
                       for name in self.config.selected_sensors):
                    break
                if monotonic() >= deadline:
                    raise TimeoutError("resume_fresh_sample_timeout: 等待边界后的有效新数据超时，保持中断")
                self.stop_event.wait(.005)
            if self.stop_event.is_set():
                return False
            with self._pause_condition:
                self.pause_intervals[-1].update(resumed_at=time.time(), resumed_monotonic=monotonic())
                self.segment_start_index = self.total_sample_count
                self.pause_state = "sampling"
                self.capture_phase = "running"
                self.control_revision += 1
                self._sampling_interrupt.clear()
                self._pause_condition.notify_all()
            return True
        except Exception as exc:
            with self._pause_condition:
                self.pause_error = str(exc)
                self.pause_state = "pausing"
                self.capture_phase = "pausing"
                self.control_revision += 1
                self._pause_condition.notify_all()
            return False

    def pause_status(self):
        alive = bool(self.thread and self.thread.is_alive())
        active = bool(self.config is not None and not self.finalization_complete
                      and self.capture_phase not in {"idle", "start_failed"})
        return {"session_active": active, "sampling": alive and self.pause_state == "sampling"
                and not self.stop_event.is_set(), "paused": alive and self.pause_state == "paused",
                "control_revision": self.control_revision, "pause_error": self.pause_error,
                "segment_start_index": self.segment_start_index,
                "segment_boundary_monotonic": self.segment_boundary_monotonic,
                "pause_intervals": deepcopy(self.pause_intervals),
                "saving": self.capture_phase in {"stopping_sampling", "finalizing"},
                "save_failed": self.capture_phase in {"database_pending_retry", "finalization_failed", "stop_timed_out"}}
