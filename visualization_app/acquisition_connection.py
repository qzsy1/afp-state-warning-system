"""Process-local device connections, independent of durable capture sessions."""
from contextlib import ExitStack
from copy import deepcopy
import threading
import time
import uuid

from real_acquisition import readiness_config_fingerprint


class AcquisitionConnectionMixin:
    def _init_connection(self):
        self._connection_lock = threading.RLock()
        self._connection_driver = None
        self._connection_config = None
        self._connection_thread = None
        self._connection_cancel = threading.Event()
        self._connection = {"state": "disconnected", "ready": False, "reason": "请先连接数据"}

    def connection_status(self):
        with self._connection_lock:
            result = dict(self._connection)
            driver = self._connection_driver
        if driver is not None:
            result["buffer"] = driver.cache.metrics()
            result["interfaces"] = [{key: row.get(key) for key in (
                "interface_id", "state", "running", "source_sequence", "reconnect_attempts",
                "last_sample_monotonic", "last_error")} for row in driver.worker_status()]
        return result

    def invalidate_data_connection(self, reason):
        with self._connection_lock:
            if self._connection_driver is not None:
                self._connection.update(state="fault", ready=False, reason=reason)
        self.invalidate_readiness(reason="data_connection_invalid")
        if self.thread and self.thread.is_alive():
            self.last_error = reason
            self.stop_event.set()

    def _connection_health(self):
        from acquisition import _freshness_by_channel
        driver, config = self._connection_driver, self._connection_config
        if driver is None or not driver.workers:
            return "设备接收线程未运行"
        states = driver.worker_status()
        connecting = self._connection.get("state") == "connecting"
        attempts = self._connection.get("ready_attempts", {})
        for row in states:
            if (not row["running"] or row["state"] != "running"
                    or (not connecting and row["reconnect_attempts"] != attempts.get(row["interface_id"], 0))):
                return f"{row['interface_id']}连接失效：{row['last_error'] or row['state']}"
        samples = driver.cache.snapshot()
        now = time.monotonic()
        freshness = _freshness_by_channel(config)
        for name in config.selected_sensors:
            sample = samples.get(name)
            if sample is None:
                return f"等待{name}的新完整帧"
            opened = next(row.get("opened_monotonic") or 0 for row in states if row["interface_id"] == sample.interface_id)
            if sample.received_monotonic < opened:
                return f"等待{name}重新连接后的完整新帧"
            limit = freshness.get(name, 1.0)
            if sample.metadata.get("driver") == "huipu_tc64_modbus_rtu":
                limit = max(limit, 2.5 * float(sample.metadata.get("scan_duration_seconds") or 0) + .1)
            if sample.metadata.get("driver") == "esr1001_usb_ttl":
                limit = .25
            if (sample.value is None or sample.protocol_ok is False
                    or sample.metadata.get("current_stream_stable") is False
                    or not 0 <= now - sample.received_monotonic <= limit):
                return f"{name}未满足现有协议、稳定性或新鲜度要求"
        return ""

    def require_data_connection(self, config):
        with self._connection_lock:
            if readiness_config_fingerprint(config) != self._connection.get("fingerprint"):
                if self._connection.get("state") != "disconnected":
                    self._connection.update(state="fault", ready=False, reason="设备身份或配置已变化，请断开后重新连接")
                raise RuntimeError("data_connection_not_ready: 请按当前配置连接数据")
            reason = self._connection_health()
            if not self._connection.get("ready") or reason:
                raise RuntimeError("data_connection_not_ready: " + (reason or self._connection.get("reason", "请先连接数据")))
            return self._connection_driver

    def connect_data(self, config):
        from acquisition import build_driver, MultiInterfaceDriver
        if config.acquisition_mode != "real" or config.real_acquisition_mode != "local_direct":
            raise RuntimeError("连接数据仅适用于本机真实设备采集")
        with self.lifecycle_lock:
            if (self.thread and self.thread.is_alive()) or (self._start_thread and self._start_thread.is_alive()):
                raise RuntimeError("请先停止当前采集并完成保存")
            if self.recovery_state.get("blocked") or self.capture_phase in {"database_pending_retry", "finalization_failed", "stop_timed_out"}:
                raise RuntimeError("上一采集保存或释放尚未完成，请先处理恢复状态")
            with self._connection_lock:
                if self._connection_thread and self._connection_thread.is_alive():
                    raise RuntimeError("设备连接正在进行")
                if self._connection_driver is not None:
                    raise RuntimeError("已有设备连接，请先断开数据再连接")
                clean = deepcopy(config)
                for item in clean.interfaces:
                    for key in list(item):
                        if key.startswith("_"):
                            del item[key]
                clean._session_usb_profiles = {}
                driver = build_driver(clean)
                if not isinstance(driver, MultiInterfaceDriver):
                    raise RuntimeError("连接数据需要明确配置独立设备接口")
                self._connection_config = clean
                self._connection_driver = driver
                self._connection_cancel.clear()
                self._connection = {"id": uuid.uuid4().hex, "state": "connecting", "ready": False,
                    "reason": "正在打开设备、协议握手并等待持续接收就绪",
                    "fingerprint": readiness_config_fingerprint(config), "clock_domain": "server_monotonic",
                    "started_monotonic": time.monotonic(), "ready_monotonic": None}

                def run():
                    try:
                        driver.open()
                        deadline = time.monotonic() + clean.first_sample_timeout_seconds
                        while time.monotonic() < deadline:
                            if self._connection_cancel.is_set():
                                raise RuntimeError("设备连接已取消")
                            if self._connection.get("state") != "connecting":
                                raise RuntimeError(self._connection.get("reason") or "设备配置已变化，请重新连接")
                            reason = self._connection_health()
                            counts = {name: set() for name in clean.selected_sensors}
                            opened = {row['interface_id']: row.get('opened_monotonic') or 0 for row in driver.worker_status()}
                            for sample in driver.cache.events():
                                if (sample.channel_name in counts and sample.value is not None and sample.protocol_ok is not False
                                        and sample.received_monotonic >= opened.get(sample.interface_id, float('inf'))):
                                    counts[sample.channel_name].add(sample.source_sequence)
                            if not reason and all(len(values) >= 2 for values in counts.values()):
                                with self._connection_lock:
                                    if self._connection_cancel.is_set() or self._connection.get("state") != "connecting":
                                        raise RuntimeError(self._connection.get("reason") or "设备连接已失效")
                                    self._connection.update(state="connected", ready=True, reason="已连接，等待采集",
                                        ready_monotonic=time.monotonic(), ready_attempts={row['interface_id']: row['reconnect_attempts'] for row in driver.worker_status()})
                                break
                            self._connection_cancel.wait(.01)
                        else:
                            raise RuntimeError(reason or "持续接收未就绪")
                    except Exception as exc:
                        with self._connection_lock:
                            self._connection.update(state="fault", ready=False, reason=str(exc))
                        try:
                            driver.close(timeout_seconds=clean.stop_sampling_timeout_seconds)
                        except Exception as close_exc:
                            with self._connection_lock:
                                self._connection["reason"] += "; 设备释放失败：" + str(close_exc)

                self._connection_thread = threading.Thread(target=run, name="AFP-ConnectData", daemon=True)
                self._connection_thread.start()
                monitor = threading.Thread(target=self._monitor_connection, name="AFP-ConnectionHealth", daemon=True)
                monitor.start()
        return self.status()

    def _monitor_connection(self):
        driver = self._connection_driver
        while not self._connection_cancel.wait(.1) and self._connection_driver is driver:
            self._refresh_connected_readiness()

    def _two_fresh_observations_since(self, boundary):
        from acquisition import _freshness_by_channel
        config, driver = self._connection_config, self._connection_driver
        freshness = _freshness_by_channel(config)
        samples = driver.cache.events()
        now = time.monotonic()
        counts = {name: set() for name in config.selected_sensors}
        for sample in reversed(samples):
            if sample.channel_name not in counts or len(counts[sample.channel_name]) >= 2:
                continue
            limit = freshness.get(sample.channel_name, 1.)
            if sample.metadata.get('driver') == 'esr1001_usb_ttl':
                limit = .25
            elif sample.metadata.get('driver') == 'huipu_tc64_modbus_rtu':
                limit = max(limit, 2.5 * float(sample.metadata.get('scan_duration_seconds') or 0) + .1)
            if (sample.received_monotonic > boundary and 0 <= now-sample.received_monotonic <= limit
                    and sample.value is not None and sample.protocol_ok is not False
                    and sample.metadata.get('current_stream_stable') is not False):
                counts[sample.channel_name].add(sample.source_sequence)
        return all(len(values) >= 2 for values in counts.values())

    def _refresh_connected_readiness(self):
        with self._connection_lock:
            state = self._connection.get('state')
        if state not in {'connected', 'waiting_data'}:
            return
        driver = self._connection_driver
        attempts = self._connection.get('ready_attempts', {})
        rows = driver.worker_status()
        states_healthy = bool(rows) and all(row['running'] and row['state'] == 'running'
            and row['reconnect_attempts'] == attempts.get(row['interface_id'], 0) for row in rows)
        if states_healthy and time.monotonic() < getattr(self, '_connection_boundary_grace', 0):
            return
        reason = self._connection_health()
        if not states_healthy:
            with self._connection_lock:
                self._connection.update(state='fault', ready=False, reason=reason or '设备接收连接已失效')
            self.invalidate_readiness(reason='data_connection_invalid')
            return
        now = time.monotonic()
        if reason:
            with self._connection_lock:
                if self._connection['state'] != 'waiting_data':
                    events = self._connection.setdefault('quality_interruptions', [])
                    events.append({'server_monotonic': now, 'reason': reason})
                    events[:] = events[-32:]
                    self._connection['unready_since_monotonic'] = now
                self._connection.update(state='waiting_data', ready=False, reason=reason)
        elif state == 'waiting_data' and self._two_fresh_observations_since(self._connection['unready_since_monotonic']):
            with self._connection_lock:
                self._connection.update(state='connected', ready=True, reason='已连接，等待采集', ready_monotonic=now)

    def _begin_connected_capture(self, config):
        from acquisition import USB_DRIVER, RawCaptureWriter, MatrixRecorder
        driver = self.require_data_connection(config)
        with ExitStack() as stack:
            for worker in driver.workers:
                stack.enter_context(worker.receive_lock)
            self.require_data_connection(config)
            self._connection_boundary_grace = time.monotonic() + config.first_sample_timeout_seconds
            for worker in driver.workers:
                device = worker.driver
                boundary = getattr(device, "begin_capture_boundary", None)
                if boundary:
                    boundary()
                worker.source_observations.clear()
            driver.cache.clear()
            self._capture_start_monotonic = time.monotonic()
            driver.interface_quality_metadata = {}
            driver._closed_matrix_sources = {}
            for worker in driver.workers:
                item = next(i for i in config.interfaces if str(i.get("id")) == worker.interface_id)
                device = worker.driver
                if item["driver"] == "m3232_pressure":
                    device.recorder = MatrixRecorder(item["_raw_capture_root"], {
                        "interface_id": worker.interface_id, "configuration": {k:v for k,v in item.items() if not k.startswith("_")},
                        **item["_matrix_csv_context"]}, csv_path=item["_matrix_csv_work_path"])
                elif item["driver"] == USB_DRIVER:
                    device.recorder = RawCaptureWriter(item["_raw_capture_root"], {
                        "interface_id": worker.interface_id, "endpoint": item["endpoint"],
                        "capture_uuid": self.capture_uuid, "capture_generation": config.capture_generation,
                        "device_binding": deepcopy(device.binding), "parameters": deepcopy(device.parameters),
                        "calibration_snapshot": deepcopy(device.calibration), "calibration_digest": device.calibration_digest,
                        "vendor_candidate_snapshot": deepcopy(device.vendor_candidate), "vendor_candidate_digest": device.vendor_candidate_digest,
                        "measurement_context": deepcopy(device.measurement_context), "data_mode": device.data_mode, "bridge": device.bridge})
            self.record_start_event("capture_receive_boundary", boundary_monotonic=self._capture_start_monotonic,
                connection_id=self._connection["id"])

    def _finish_connected_recorders(self):
        from acquisition import MatrixRecorder
        driver = self._connection_driver
        detached = []
        with ExitStack() as stack:
            for worker in driver.workers:
                stack.enter_context(worker.receive_lock)
            for worker in driver.workers:
                device = worker.driver
                recorder = getattr(device, "recorder", None)
                if recorder is None:
                    continue
                evidence = deepcopy(device.quality_metadata)
                device.recorder = None
                detached.append((worker, recorder, evidence))
        errors = []
        for worker, recorder, evidence in detached:
            try:
                recorder.close(evidence)
            except Exception as exc:
                recorder.error = recorder.error or str(exc)
            evidence.update(raw_file=str(recorder.path), raw_metadata_file=str(recorder.metadata_path),
                saved_source_rows=recorder.rows, writer_error=recorder.error)
            if isinstance(recorder, MatrixRecorder):
                evidence.update(matrix_csv_file=str(recorder.csv_path).removesuffix(".partial"), saved_matrix_csv_rows=recorder.csv_rows)
                driver._closed_matrix_sources[str(recorder.csv_path)] = (worker.interface_id, worker.driver, recorder)
            driver.interface_quality_metadata[worker.interface_id] = evidence
            if recorder.error:
                errors.append(worker.interface_id + ": " + recorder.error)
        if errors:
            raise RuntimeError("source_writer_failed: " + "; ".join(errors))
        self.record_start_event("capture_sources_saved")

    def disconnect_data(self):
        driver = self._connection_driver
        connecting = self._connection.get("state") == "connecting"
        if connecting:
            self._connection_cancel.set()
        if driver is not None and connecting:
            driver.cancel_open()
        pending = self.cancel_start()
        if pending is not None:
            return pending
        with self.lifecycle_lock:
            if self.config is not None and not self.finalization_complete:
                self._stop_active()
            if self.thread and self.thread.is_alive():
                raise RuntimeError("采样尚未安全停止，不能断开或退出")
            self._connection_cancel.set()
            started = time.monotonic()
            connection_thread = self._connection_thread
            if connection_thread and connection_thread.is_alive():
                connection_thread.join(10)
            if driver is not None:
                driver.close(timeout_seconds=10)
                if driver.workers or (connection_thread and connection_thread.is_alive()):
                    with self._connection_lock:
                        self._connection.update(state="release_failed", ready=False, reason="设备释放未完成，请重试断开")
                    raise RuntimeError("设备释放未完成，请重试断开")
                errors = [row.get("stop_error") for row in driver.interface_quality_metadata.values() if row.get("stop_error")]
                if errors:
                    with self._connection_lock:
                        self._connection.update(state="release_failed", ready=False, reason="; ".join(errors))
                    raise RuntimeError("停止设备输出失败：" + "; ".join(errors))
            with self._connection_lock:
                self._connection_driver = None
                self._connection.update(state="disconnected", ready=False, reason="设备数据已断开", disconnected_monotonic=time.monotonic(),
                    disconnect_seconds=time.monotonic()-started)
            self.invalidate_readiness(reason="data_disconnected")
            return self.status()
