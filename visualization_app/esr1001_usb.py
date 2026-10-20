"""ESR1001-A 原生 USB/TTL：结构校验、主机观测及显式标定。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import queue
import threading
import time
import uuid
from collections import deque
from pathlib import Path

USB_DRIVER = "esr1001_usb_ttl"
RAW_CHANNELS = tuple(f"CH{i}_ADC_signed" for i in range(1, 5))
QUERY = bytes.fromhex("23 A8 00 00 00 FF")
START = bytes.fromhex("23 A1 01 00 00 FF")
STOP = bytes.fromhex("23 A1 00 00 00 FF")
PORT_SETTLE_SECONDS = 1.0
START_SETTLE_SECONDS = 1.0
PORT_READY_GUARD_SECONDS = 0.5
QUERY_START_SPACING_SECONDS = 1.3
BOUNDED_START_PARAMETER_HEX = "26016306040001086800000000ff"
RATE_CODES = {0xB0, 0xA1, 0x92, 0x82, 0x72, 0x63, 0x53, 0x43, 0x33, 0x23, 0x13, 0xF1, 0xF2}
FRESHNESS_LIMIT_SECONDS = 0.25
FRESHNESS_RECOVERY_INTERVALS = 3


class UsbProtocolError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def current_stream_is_stable(driver, latest_host_monotonic, *, now=None):
    """Return current readiness after a recovered gap while preserving gap evidence."""
    current = time.monotonic() if now is None else float(now)
    intervals = tuple(getattr(driver, "recent_intervals", ()))
    parser = driver.parser
    return (
        0 <= current - float(latest_host_monotonic) <= FRESHNESS_LIMIT_SECONDS
        and len(intervals) >= FRESHNESS_RECOVERY_INTERVALS
        and all(0 <= value <= FRESHNESS_LIMIT_SECONDS for value in intervals)
        and not parser.discarded_bytes
        and not parser.overflows
    )


def parse_parameters(frame):
    raw = bytes(frame)
    if len(raw) != 14 or raw[0] != 0x26 or raw[-1] != 0xFF:
        raise UsbProtocolError("usb_parameters_invalid", "参数帧结构无效")
    if (raw[1] not in {1, 3} or raw[2] not in RATE_CODES or raw[3] > 6
            or raw[4] not in {1, 2, 4} or raw[5] not in {0, *range(3, 11)}
            or not 1 <= raw[6] <= 247 or raw[7] not in {0x04, 0x08, 0x10, 0x20, 0x30, 0x60}):
        raise UsbProtocolError("usb_parameters_invalid", "参数字段不符合协议")
    return dict(zip(("mode", "rate_code", "gain_code", "channel_count", "filter_code", "rs485_address", "rs485_baud_code"), raw[1:8]), reserved_hex=raw[8:13].hex(), raw_hex=raw.hex())


def decode_adc(frame, channels=4):
    raw = bytes(frame)
    if channels not in {1, 2, 4} or len(raw) != 2 + channels * 3 or raw[0] != 0x24 or raw[-1] != 0xFF:
        raise UsbProtocolError("usb_frame_invalid", "ADC帧结构无效")
    values = [int.from_bytes(raw[i:i + 3], "big", signed=True) for i in range(1, len(raw) - 1, 3)]
    return tuple(values[1:] + values[:1]) if channels > 1 else tuple(values)


class UsbStreamParser:
    """固定长度消费；失步后须连续三帧确认，不提供 CRC 证据。"""
    capacity = 65536

    def __init__(self, channels=4):
        self.channels = channels
        self.buffer = bytearray()
        self.synced = False
        self.discarded_bytes = self.overflows = self.resynchronizations = self.invalid_frames = 0

    def _frame_at(self, offset):
        if len(self.buffer) <= offset:
            return None
        head = self.buffer[offset]
        size = 2 + self.channels * 3 if head == 0x24 else 14 if head in {0x25, 0x26} else 0
        if not size:
            return False
        if len(self.buffer) < offset + size:
            return None
        raw = bytes(self.buffer[offset:offset + size])
        if raw[-1] != 0xFF:
            return False
        if head == 0x26:
            try:
                parse_parameters(raw)
            except UsbProtocolError:
                return False
        if head == 0x25 and raw[1] not in range(0xA1, 0xA9):
            return False
        return raw

    def feed(self, data):
        overflow = max(0, len(self.buffer) + len(data) - self.capacity)
        if overflow:
            self.overflows += 1
            self.discarded_bytes += overflow
            self.buffer = (self.buffer + data)[-self.capacity:]
            self.synced = False
        else:
            self.buffer.extend(data)
        result = []
        while self.buffer:
            frame = self._frame_at(0)
            if frame is None:
                break
            if frame is False:
                self.invalid_frames += 1
                self.discarded_bytes += 1
                del self.buffer[0]
                self.synced = False
                continue
            # 参数/控制事务本身不发布观测，可在流同步前完成握手。
            if not self.synced and frame[0] == 0x24:
                offset = len(frame)
                candidates = [frame]
                for _ in range(2):
                    following = self._frame_at(offset)
                    if following is None:
                        return result
                    if following is False:
                        break
                    candidates.append(following)
                    offset += len(following)
                if len(candidates) != 3:
                    self.discarded_bytes += 1
                    self.invalid_frames += 1
                    del self.buffer[0]
                    continue
                self.synced = True
                self.resynchronizations += 1
            result.append(frame)
            del self.buffer[:len(frame)]
        return result

    def metrics(self):
        return {name: getattr(self, name) for name in ("discarded_bytes", "overflows", "resynchronizations", "invalid_frames")} | {"buffered_bytes": len(self.buffer), "structure_synchronized": self.synced, "crc_available": False}


def load_calibration(source, *, binding, parameters, bridge):
    if not source:
        raise UsbProtocolError("calibration_missing", "微应变需要逐通道K/B档案")
    try:
        profile = json.loads(Path(source).read_text(encoding="utf-8")) if isinstance(source, (str, Path)) else dict(source)
        canonical = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (OSError, TypeError, ValueError) as exc:
        raise UsbProtocolError("calibration_invalid", "标定档案无法读取或包含非有限值") from exc
    if (profile.get("version") != 1 or profile.get("unit") != "με"
            or not profile.get("source") or not binding or profile.get("device_binding") != binding
            or not bridge or profile.get("bridge") != bridge
            or profile.get("gain_code") != parameters["gain_code"]):
        raise UsbProtocolError("calibration_mismatch", "来源、设备绑定、桥路、增益或单位不匹配")
    coefficients = profile.get("channels")
    if not isinstance(coefficients, list) or len(coefficients) != 4:
        raise UsbProtocolError("calibration_invalid", "需要四路K/B")
    try:
        valid = all(math.isfinite(float(c["K"])) and float(c["K"]) != 0 and math.isfinite(float(c["B"])) for c in coefficients)
    except (TypeError, KeyError, ValueError):
        valid = False
    if not valid:
        raise UsbProtocolError("calibration_invalid", "K/B必须有限且K非零")
    return profile, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RawCaptureWriter:
    """有界源帧队列；写入错误和过载阻止成功收尾。"""
    fields = ("receive_sequence", "host_wall_time", "host_monotonic", *RAW_CHANNELS, "unit", "raw_hex", "connection_generation", "calibration_digest")

    def __init__(self, root, metadata, capacity=4096):
        root = Path(root)
        if not root.is_dir():
            raise ValueError("原始保存目录必须存在")
        self.path = root / f"ESR1001_USB_{uuid.uuid4().hex}.csv"
        self.metadata_path = self.path.with_suffix(".json")
        self.metadata = metadata
        self.queue = queue.Queue(maxsize=capacity)
        self.rows = 0
        self.error = ""
        self.closed = False
        self.stream = self.path.open("x", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(self.stream, self.fields, extrasaction="ignore")
        self.writer.writeheader()
        self.thread = threading.Thread(target=self._run, name="AFP-ESR1001-RawWriter", daemon=True)
        self.thread.start()

    def submit(self, record):
        if self.error or self.closed:
            raise UsbProtocolError("raw_writer_failed", self.error or "写入已关闭")
        try:
            self.queue.put_nowait(dict(record))
        except queue.Full:
            self.error = "原始帧写入队列过载"
            raise UsbProtocolError("raw_writer_overflow", self.error)

    def _run(self):
        try:
            while True:
                record = self.queue.get()
                if record is None:
                    break
                self.writer.writerow(record)
                self.rows += 1
            self.stream.flush()
            os.fsync(self.stream.fileno())
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.stream.close()

    def close(self, evidence):
        if self.closed:
            return
        self.closed = True
        if self.thread.is_alive():
            try:
                self.queue.put(None, timeout=2)
            except queue.Full:
                self.error = self.error or "原始队列未完成收尾"
            self.thread.join(5)
        if self.thread.is_alive():
            self.error = self.error or "原始文件收尾超时"
        payload = self.metadata | evidence | {"saved_source_rows": self.rows, "writer_error": self.error, "raw_file": str(self.path)}
        self.metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


_PORT_LOCK = threading.Lock()
_PORT_OWNERS = set()


def require_helper_capabilities(config):
    if getattr(config, "acquisition_mode", "") != "real" or getattr(config, "real_acquisition_mode", "") != "remote_helper":
        return
    for item in getattr(config, "interfaces", None) or []:
        if item.get("enabled", True) and item.get("driver") == USB_DRIVER:
            required = [USB_DRIVER, "esr1001_raw_adc_capture"]
            if item.get("data_mode") == "microstrain":
                required.append("esr1001_calibration_v1")
            missing = [name for name in required if not (config.helper_capabilities or {}).get(name)]
            if missing:
                raise RuntimeError("helper_capability_missing: " + ", ".join(missing))


class Esr1001UsbDriver:
    open_timeout_seconds = 7.0

    def __init__(self, endpoint, *, data_mode="raw_adc", calibration=None, binding=None, bridge="", command_timeout=2.0, serial_factory=None, inventory=None, recorder=None, startup_policy="fixed"):
        self.endpoint = str(endpoint).strip()
        self.data_mode = data_mode
        if data_mode not in {"raw_adc", "microstrain"}:
            raise ValueError("USB数据模式必须为raw_adc或microstrain")
        self.calibration_source = calibration
        self.binding = dict(binding or {})
        self.bridge = bridge
        self.command_timeout = max(0.01, min(float(command_timeout), 2.0))
        self.serial_factory = serial_factory
        self.inventory = inventory
        self.recorder = recorder
        if startup_policy not in {"fixed", "bounded_response"}:
            raise ValueError("unsupported USB startup policy")
        self.startup_policy = startup_policy
        self._effective_startup_policy = startup_policy
        self._cancel_open = threading.Event()
        self.serial = None
        self.parser = UsbStreamParser()
        self.parameters = {}
        self.protocol_verified = False
        self.open_stages = set()
        self.start_events = []
        self.started = False
        self.sequence = 0
        self.connection_generation = 0
        self.open_attempts = 0
        self.latest_record = None
        self.last_observation = None
        self.first_observation = None
        self.max_interval = 0.0
        self.recent_intervals = deque(maxlen=FRESHNESS_RECOVERY_INTERVALS)
        self.gaps = []
        self.calibration = None
        self.calibration_digest = ""
        self.stop_error = ""
        self.stop_failures = []
        self._owns_port = False
        self._io_lock = threading.RLock()
        self._pending = deque()
        self._controls = deque(maxlen=32)
        self._parameter_frames = deque(maxlen=4)
        self.started_monotonic = None
        self.enumerated_identity = None
        self.received_bytes = 0
        self.received_tail = b""
        self.command_evidence = deque(maxlen=32)
        self._previous_metrics = {key: 0 for key in ("discarded_bytes", "overflows", "resynchronizations", "invalid_frames")}

    @property
    def quality_metadata(self):
        metrics = self.parser.metrics()
        metrics.update({key: metrics[key] + value for key, value in self._previous_metrics.items()})
        duration = (self.last_observation - self.first_observation) if self.first_observation is not None and self.last_observation is not None else 0
        metrics["actual_source_rate_hz"] = (self.sequence - 1) / duration if duration > 0 else None
        metrics.update(received_bytes=self.received_bytes, received_tail_hex=self.received_tail.hex(), commands=list(self.command_evidence), start_events=list(self.start_events), startup_policy=self._effective_startup_policy, current_stream_stable=bool(self.last_observation is not None and current_stream_is_stable(self, self.last_observation)))
        return {"driver": USB_DRIVER, "data_mode": self.data_mode, "unit": "adc_counts" if self.data_mode == "raw_adc" else "με", "calibration_state": "confirmed" if self.calibration else "标定未确认", "calibration_digest": self.calibration_digest, "parameters": dict(self.parameters), "source_frames": self.sequence, "connection_generation": self.connection_generation, "max_interval_seconds": self.max_interval, "recent_intervals_seconds": list(self.recent_intervals), "freshness_recovery_intervals": FRESHNESS_RECOVERY_INTERVALS, "stop_error": self.stop_error, "stop_failures": list(self.stop_failures), "latest_record": dict(self.latest_record or {}), "gaps": list(self.gaps[-128:]), **metrics}

    def _read(self):
        pending = int(getattr(self.serial, "in_waiting", 0))
        data = self.serial.read(min(max(pending, 1), 16384))
        self.received_bytes += len(data)
        self.received_tail = (self.received_tail + data)[-96:]
        for raw in self.parser.feed(data):
            if raw[0] == 0x26:
                if self.parameters and parse_parameters(raw) != self.parameters:
                    raise UsbProtocolError("device_identity_mismatch", "采集期间设备参数变化，必须重新检查")
                self._parameter_frames.append(raw)
            elif raw[0] == 0x25:
                self._controls.append(raw)
            elif self.started:
                values = decode_adc(raw, self.parser.channels)
                mono, wall = time.monotonic(), time.time()
                self.sequence += 1
                if self.sequence <= FRESHNESS_RECOVERY_INTERVALS + 1:
                    self.start_events.append({"stage": "source_frame", "sequence": self.sequence, "server_monotonic": mono})
                if self.first_observation is None:
                    self.first_observation = mono
                if self.last_observation is not None:
                    interval = mono - self.last_observation
                    self.max_interval = max(self.max_interval, interval)
                    self.recent_intervals.append(interval)
                    if interval > FRESHNESS_LIMIT_SECONDS:
                        self.gaps.append({"after_sequence": self.sequence - 1, "interval_seconds": interval})
                        self.gaps = self.gaps[-128:]
                self.last_observation = mono
                record = dict(zip(RAW_CHANNELS, values), receive_sequence=self.sequence, host_wall_time=wall, host_monotonic=mono, raw_hex=raw.hex(), unit="adc_counts", connection_generation=self.connection_generation, calibration_digest=self.calibration_digest)
                if self.recorder:
                    self.recorder.submit(record)
                self.latest_record = record
                if len(self._pending) >= 4096:
                    raise UsbProtocolError("usb_consumer_overflow", "源观测消费队列过载")
                self._pending.append(record)

    def _command(self, command):
        if self._effective_startup_policy == "bounded_response" and command != STOP:
            if int(getattr(self.serial, "in_waiting", 0)):
                self._read()
            if self.parser.buffer:
                raise UsbProtocolError("usb_stale_response", "命令发送前仍有不完整旧帧，必须重新检查")
        self._controls.clear()
        self._parameter_frames.clear()
        self.serial.write(command)
        command_record = {"command_hex": command.hex(), "host_monotonic": time.monotonic(), "matched_response_hex": ""}
        self.command_evidence.append(command_record)
        deadline = time.monotonic() + self.command_timeout
        while time.monotonic() < deadline:
            if command != STOP and self._cancel_open.is_set():
                raise UsbProtocolError("capture_start_cancelled", "设备启动已取消")
            self._read()
            if command == QUERY and self._parameter_frames:
                command_record["matched_response_hex"] = self._parameter_frames[0].hex()
                return parse_parameters(self._parameter_frames.popleft())
            if command != QUERY:
                for raw in self._controls:
                    if raw[1:5] == command[1:5] and raw[5:13] == bytes(8):
                        command_record["matched_response_hex"] = raw.hex()
                        return raw
        stage = "parameters" if command == QUERY else "start" if command == START else "stop"
        raise UsbProtocolError(f"usb_{stage}_timeout", "未收到匹配应答")

    def reset_open_cancellation(self):
        self._cancel_open.clear()

    def cancel_open(self):
        self._cancel_open.set()

    def _wait_open_until(self, deadline):
        if self._cancel_open.wait(max(0.0, deadline-time.monotonic())):
            raise UsbProtocolError("capture_start_cancelled", "设备启动已取消")

    def open(self):
        with self._io_lock:
            if self._cancel_open.is_set():
                raise UsbProtocolError("capture_start_cancelled", "设备启动已取消")
            self.open_stages.clear()
            self._effective_startup_policy = "fixed" if self.open_attempts else self.startup_policy
            self.open_attempts += 1
            self.start_events = [{"stage": "open_begin", "server_monotonic": time.monotonic()}]
            self.protocol_verified = False
            if not self.endpoint:
                raise ValueError("必须显式选择USB串口")
            key = self.endpoint.casefold()
            with _PORT_LOCK:
                if key in _PORT_OWNERS:
                    raise UsbProtocolError("port_busy", "端口被其他会话占用")
                _PORT_OWNERS.add(key)
                self._owns_port = True
            try:
                import serial
                from serial.tools import list_ports
                inventory = self.inventory or list_ports.comports
                ports = [p for p in inventory() if p.device.casefold() == key]
                self.start_events.append({"stage": "enumeration_end", "server_monotonic": time.monotonic()})
                if not ports:
                    raise UsbProtocolError("port_not_present", "所选端口当前不存在，请重新识别")
                self.open_stages.add("port_present")
                port = ports[0]
                current_identity = {name: getattr(port, name, None) for name in ("vid", "pid", "serial_number", "location", "hwid")}
                if self.enumerated_identity is not None and self.enumerated_identity != current_identity:
                    raise UsbProtocolError("device_identity_mismatch", "重连枚举身份变化，请重新检查")
                self.enumerated_identity = current_identity
                for name, expected in self.binding.items():
                    if expected and name in {"vid", "pid", "serial_number", "location", "hwid"} and getattr(port, name, None) != expected:
                        raise UsbProtocolError("device_identity_mismatch", "当前枚举身份变化，请重新绑定")
                self.open_stages.add("endpoint_compatible")
                try:
                    self.serial = (self.serial_factory or serial.Serial)(self.endpoint, baudrate=115200, bytesize=8, parity="N", stopbits=1, timeout=0.02, write_timeout=self.command_timeout)
                except (OSError, serial.SerialException) as exc:
                    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) == 13 or "PermissionError(13," in str(exc):
                        raise UsbProtocolError("port_access_denied", "串口访问被拒绝；请关闭其他占用程序或检查访问权限") from exc
                    raise
                self.open_stages.add("driver_open")
                self.start_events.append({"stage": "port_settle_begin", "server_monotonic": time.monotonic()})
                port_guard = PORT_READY_GUARD_SECONDS if self._effective_startup_policy == "bounded_response" else PORT_SETTLE_SECONDS
                self._wait_open_until(time.monotonic() + port_guard)
                self.start_events.append({"stage": "port_settle_end", "server_monotonic": time.monotonic()})
                for key in self._previous_metrics:
                    self._previous_metrics[key] += self.parser.metrics()[key]
                self.parser = UsbStreamParser()
                self._pending.clear()
                if self._effective_startup_policy == "bounded_response":
                    reset_input = getattr(self.serial, "reset_input_buffer", None)
                    if callable(reset_input):
                        reset_input()
                    else:
                        pending = int(getattr(self.serial, "in_waiting", 0))
                        if pending:
                            self.serial.read(pending)
                parameters = self._command(QUERY)
                self.start_events.append({"stage": "query_matched", "server_monotonic": time.monotonic()})
                self.protocol_verified = True
                self.open_stages.add("protocol_identified")
                if parameters["mode"] != 1:
                    raise UsbProtocolError("usb_mode_invalid", "设备不在USB TTL模式")
                if parameters["channel_count"] != 4:
                    raise UsbProtocolError("partial_channels", "设备未启用四通道；需操作者确认配置")
                if self.parameters and self.parameters != parameters:
                    raise UsbProtocolError("device_identity_mismatch", "重连参数变化，必须重新检查")
                self.parameters = parameters
                if self.data_mode == "microstrain":
                    calibration, digest = load_calibration(self.calibration_source, binding=self.binding, parameters=parameters, bridge=self.bridge)
                    if self.calibration_digest and self.calibration_digest != digest:
                        raise UsbProtocolError("calibration_mismatch", "会话期间标定内容变化，必须停止并重新检查")
                    self.calibration, self.calibration_digest = calibration, digest
                self.connection_generation += 1
                self.stop_error = ""
                self.start_events.append({"stage": "start_settle_begin", "server_monotonic": time.monotonic()})
                start_deadline = (self.command_evidence[-1]["host_monotonic"] + QUERY_START_SPACING_SECONDS
                                  if self._effective_startup_policy == "bounded_response" else time.monotonic() + START_SETTLE_SECONDS)
                self._wait_open_until(start_deadline)
                self.start_events.append({"stage": "start_settle_end", "server_monotonic": time.monotonic()})
                self.started = True
                self.started_monotonic = time.monotonic()
                self._command(START)
                self.start_events.append({"stage": "start_matched", "server_monotonic": time.monotonic()})
            except Exception:
                self.close()
                raise

    def read_samples(self):
        with self._io_lock:
            if self.serial is None:
                raise UsbProtocolError("device_disconnected", "USB串口已关闭")
            self._read()
            if self.recorder and self.recorder.error:
                raise UsbProtocolError("raw_writer_failed", self.recorder.error)
            records = list(self._pending)
            self._pending.clear()
            if not records and time.monotonic() - (self.last_observation or self.started_monotonic) > 1.0:
                raise UsbProtocolError("usb_no_valid_frame", "连续一秒没有有效源帧")
            result = []
            for record in records:
                if self.data_mode == "raw_adc":
                    values = {name: record[name] for name in RAW_CHANNELS}
                else:
                    values = {f"应变{i + 1}": record[name] * float(c["K"]) + float(c["B"]) for i, (name, c) in enumerate(zip(RAW_CHANNELS, self.calibration["channels"]))}
                    if not all(math.isfinite(value) for value in values.values()):
                        raise UsbProtocolError("calibration_invalid", "换算结果非有限值")
                result.append((values, record))
            return result

    def read_sample(self):
        samples = self.read_samples()
        return samples[-1][0] if samples else None

    def close(self):
        self.cancel_open()
        with self._io_lock:
            try:
                if self.serial is not None and self.started:
                    try:
                        self._command(STOP)
                    except Exception as exc:
                        self.stop_error = str(exc)
                        self.stop_failures.append({"connection_generation": self.connection_generation, "error": self.stop_error})
                        self.stop_failures = self.stop_failures[-128:]
            finally:
                self.started = False
                try:
                    if self.serial is not None:
                        self.serial.close()
                finally:
                    self.serial = None
                    if self._owns_port:
                        with _PORT_LOCK:
                            _PORT_OWNERS.discard(self.endpoint.casefold())
                        self._owns_port = False


def probe_usb(interface, *, driver_factory=Esr1001UsbDriver, data_window=3.0):
    """检查和启动共享驱动；数据窗口从握手完成后开始。"""
    from real_acquisition import ReadinessReport, ReadinessStage, READINESS_STAGE_NAMES
    expected = RAW_CHANNELS if interface.get("data_mode", "raw_adc") == "raw_adc" else tuple(f"应变{i}" for i in range(1, 5))
    stage_states = {name: "pending" for name in READINESS_STAGE_NAMES}
    driver = driver_factory(interface.get("endpoint", ""), data_mode=interface.get("data_mode", "raw_adc"), calibration=interface.get("calibration_profile"), binding=interface.get("device_identity"), bridge=interface.get("bridge", ""))
    samples = []
    state, message = "usb_no_valid_frame", "USB无有效四路帧"
    started = time.monotonic()
    overall_deadline = started + 12.0
    try:
        driver.open()
        for name in ("port_present", "endpoint_compatible", "driver_open", "protocol_identified"):
            stage_states[name] = "passed"
        deadline = time.monotonic() + max(3.0, min(float(data_window), 5.0))
        while time.monotonic() < deadline:
            if time.monotonic() >= overall_deadline:
                raise UsbProtocolError("usb_probe_timeout", "USB检查整体期限已到")
            try:
                samples.extend(driver.read_samples())
            except UsbProtocolError as exc:
                if exc.code != "usb_no_valid_frame":
                    raise
            time.sleep(0.001)
        if samples:
            stage_states["channels_complete"] = stage_states["values_valid"] = "passed"
            stable = current_stream_is_stable(driver, samples[-1][1]["host_monotonic"])
            stage_states["freshness_stable"] = "passed" if stable else "failed"
            if stable:
                stage_states["ready"] = "passed"
                state = "data_ready"
                message = "四路原始数据可用，标定未确认" if interface.get("data_mode", "raw_adc") == "raw_adc" else "四路标定微应变可用"
            else:
                state, message = "stale", "USB新鲜度或流结构不稳定"
    except Exception as exc:
        state, message = getattr(exc, "code", "device_open_failed"), str(exc)
        for name in driver.open_stages:
            stage_states[name] = "passed"
    finally:
        driver.close()
    if driver.stop_error:
        state, message = "usb_stop_failed", driver.stop_error
        stage_states["ready"] = "failed"
    if not interface.get("physical_verified", False):
        state, message = "physical_unverified", "协议检查保留；当前物理绑定未确认，请重新选择接口"
        stage_states["endpoint_compatible"] = stage_states["ready"] = "failed"
    for name in stage_states:
        if stage_states[name] == "pending":
            stage_states[name] = "failed"
            break
    evidence = driver.quality_metadata
    stages = tuple(ReadinessStage(name, stage_states[name], evidence={"data_mode": driver.data_mode, "source_frames": driver.sequence, "parameters": driver.parameters}) for name in READINESS_STAGE_NAMES)
    report = ReadinessReport(str(interface.get("id", "")), "ready" if state == "data_ready" else state, stages, expected_channels=tuple(expected), detected_channels=tuple(expected) if samples else (), missing_channels=() if samples else tuple(expected), message=message)
    return {
        **{key: interface.get(key) for key in ("id", "role", "endpoint", "physical_interface_id", "physical_port_id", "physical_interface_kind", "physical_verified")},
        "enabled": True, "driver": USB_DRIVER, "protocol": USB_DRIVER,
        "state": state, "final_status": state, "message": message, "ok": report.ready,
        "expected_channels": list(expected), "detected_channels": list(expected) if samples else [],
        "missing_channels": [] if samples else list(expected), "invalid_channels": [],
        "sample_counts": {name: driver.sequence for name in expected}, "errors": [] if report.ready else [message],
        "channel_results": {name: {"value": samples[-1][0].get(name) if samples else None, "unit": "adc_counts" if driver.data_mode == "raw_adc" else "με", "quality": "measured_new" if report.ready else "missing"} for name in expected},
        "stages": {stage.name: stage.to_dict() for stage in stages}, "readiness_report": report.to_dict(),
        "usb_evidence": evidence, "elapsed_seconds": time.monotonic() - started, "overall_deadline_seconds": 12.0,
    }
