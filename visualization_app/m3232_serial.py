"""M3232 UART text matrices; host sequence/time do not imply a device clock or CRC."""
from __future__ import annotations

import hashlib
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import uuid

PROTOCOL = "m3232_uart_text_v1"
BAUDRATE = 230400
RAW_CHANNEL = "薄膜压力原始响应"
FORCE_CHANNEL = "薄膜压力"
CAPABILITY = "m3232_uart_text_v1"
CSV_CAPABILITY = "m3232_matrix_csv_v1"
MATRIX_CSV_SUFFIX = "_薄膜压力完整矩阵数据.csv"


def matrix_csv_path(main_csv):
    main_csv = Path(main_csv)
    return main_csv.with_name(main_csv.stem + MATRIX_CSV_SUFFIX)


def normalize_config(item):
    item.setdefault("protocol_profile", PROTOCOL)
    if item["protocol_profile"] != PROTOCOL:
        raise ValueError("不支持的M3232协议档")
    for name, default, maximum in (("matrix_rows", 32, 256), ("matrix_cols", 32, 256)):
        item[name] = int(item.get(name) or default)
        if not 1 <= item[name] <= maximum:
            raise ValueError("M3232矩阵尺寸无效")
    item["data_mode"] = str(item.get("data_mode") or "raw_matrix")
    if item["data_mode"] not in {"raw_matrix", "calibrated_force"}:
        raise ValueError("M3232模式必须为raw_matrix或calibrated_force")
    for name, default in (("first_frame_timeout_seconds", 2.0), ("freshness_seconds", 2.0),
                          ("frame_timeout_seconds", 2.0)):
        item[name] = float(item.get(name, default))
        if not math.isfinite(item[name]) or not 0.01 <= item[name] <= 30:
            raise ValueError("M3232超时参数无效")
    item["max_buffer_bytes"] = int(item.get("max_buffer_bytes", 262144))
    if not 4096 <= item["max_buffer_bytes"] <= 1048576:
        raise ValueError("M3232缓冲上限无效")
    item["channels"] = [RAW_CHANNEL if item["data_mode"] == "raw_matrix" else FORCE_CHANNEL]
    calibration = item.get("calibration_profile")
    if calibration:
        profile = load_calibration(calibration, item.get("device_identity") or {}, item["matrix_rows"], item["matrix_cols"])
        item["calibration_digest"] = profile["digest"]
    else:
        item["calibration_digest"] = ""
    return item


def load_calibration(value, binding, rows, cols):
    profile = json.loads(Path(value).read_text(encoding="utf-8")) if isinstance(value, str) else dict(value)
    if (profile.get("schema") != "m3232_force_calibration_v1"
            or profile.get("formula") != "linear_total" or profile.get("unit") != "N"
            or profile.get("matrix_shape") != [rows, cols]
            or not profile.get("evidence") or not profile.get("version")
            or not profile.get("device_identity") or profile["device_identity"] != binding):
        raise ValueError("M3232标定依据、设备绑定或矩阵尺寸不匹配")
    for key in ("scale", "zero_total", "raw_min", "raw_max"):
        if isinstance(profile.get(key), bool) or not isinstance(profile.get(key), (int, float)) or not math.isfinite(profile[key]):
            raise ValueError("M3232标定系数无效")
    if profile["raw_min"] >= profile["raw_max"]:
        raise ValueError("M3232标定范围无效")
    if profile.get("expires_at") and time.time() >= float(profile["expires_at"]):
        raise ValueError("M3232标定已过期")
    profile["digest"] = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return profile


def require_helper_capabilities(config):
    if getattr(config, "acquisition_mode", "") == "real" and getattr(config, "real_acquisition_mode", "") == "remote_helper":
        if any(i.get("enabled", True) and i.get("driver") == "m3232_pressure" for i in config.interfaces or []):
            for capability in (CAPABILITY, CSV_CAPABILITY):
                if not (config.helper_capabilities or {}).get(capability):
                    raise RuntimeError("helper_capability_missing: " + capability)


class MatrixParser:
    def __init__(self, rows=32, cols=32, max_bytes=262144, timeout=2.0, clock=time.monotonic):
        self.rows, self.cols, self.max_bytes, self.timeout, self.clock = rows, cols, max_bytes, timeout, clock
        self.buffer = bytearray()
        self.started = None
        self.offset = 0
        self.rejected = 0
        self.last_error = ""

    def reject(self, reason):
        self.rejected += 1
        self.last_error = reason

    def _drop(self, count):
        del self.buffer[:count]
        self.offset += count
        self.started = None

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while self.buffer:
            if self.started is not None and self.clock() - self.started > self.timeout:
                self.reject("incomplete_frame_timeout")
                boundary = self.buffer.find(b"\n[[")
                self._drop(boundary + 1 if boundary >= 0 else len(self.buffer))
                continue
            start = self.buffer.find(b"[")
            if start < 0:
                self.reject("non_matrix_bytes")
                self._drop(len(self.buffer))
                break
            if start:
                self.reject("non_matrix_bytes")
                self._drop(start)
            if self.started is None:
                self.started = self.clock()
            # JSON string quoting is tracked so brackets in invalid string values cannot split frames.
            depth, quoted, escaped, end = 0, False, False, None
            for index, char in enumerate(self.buffer):
                if quoted:
                    if escaped: escaped = False
                    elif char == 92: escaped = True
                    elif char == 34: quoted = False
                elif char == 34: quoted = True
                elif char == 91: depth += 1
                elif char == 93:
                    depth -= 1
                    if depth == 0:
                        end = index + 1
                        break
            if end is None:
                if len(self.buffer) > self.max_bytes:
                    self.reject("buffer_limit")
                    boundary = self.buffer.rfind(b"\n[[")
                    self._drop(boundary + 1 if boundary >= 0 else len(self.buffer))
                    continue
                break
            tail = end
            while tail < len(self.buffer) and self.buffer[tail] in (32, 9, 13): tail += 1
            if tail == len(self.buffer):
                if len(self.buffer) > self.max_bytes:
                    self.reject("buffer_limit")
                    self._drop(len(self.buffer))
                break
            if self.buffer[tail] != 10:
                self.reject("missing_frame_lf")
                self._drop(tail)
                continue
            raw = bytes(self.buffer[:tail + 1])
            offset = self.offset
            self._drop(tail + 1)
            try:
                if len(raw) > self.max_bytes: raise ValueError("buffer_limit")
                matrix = json.loads(raw.decode("ascii"))
                if not isinstance(matrix, list) or len(matrix) != self.rows: raise ValueError("matrix_shape")
                if any(not isinstance(row, list) or len(row) != self.cols for row in matrix): raise ValueError("matrix_shape")
                if any(type(v) not in (int, float) or not math.isfinite(v) for row in matrix for v in row): raise ValueError("non_finite_or_non_numeric")
                if not math.isfinite(math.fsum(v for row in matrix for v in row)): raise ValueError("total_overflow")
            except (ValueError, OverflowError) as exc:
                self.reject(str(exc)[:120])
                continue
            frames.append((matrix, raw, offset))
        return frames


class MatrixRecorder:
    """Bounded asynchronous JSONL evidence with rotation and explicit failure."""
    def __init__(self, root, metadata, capacity=2048, rotate_bytes=16777216, *, csv_path=None):
        root = Path(root)
        if not root.is_dir(): raise ValueError("M3232保存目录不存在")
        self.path = root / ("M3232_" + uuid.uuid4().hex + ".jsonl")
        self.metadata_path = self.path.with_suffix(".json")
        self.metadata, self.rotate_bytes = metadata, rotate_bytes
        self.rows, self.error, self.closed = 0, "", False
        self.csv_path = Path(csv_path) if csv_path else None
        self.csv_rows = 0
        self.csv_stream = None
        self.files = []
        self.queue = queue.Queue(maxsize=capacity)
        self.stream = self.path.open("x", encoding="utf-8")
        try:
            if self.csv_path:
                self.csv_stream = self.csv_path.open("x", encoding="utf-8-sig", newline="")
                self.pixel_columns = [f"R{r:02d}C{c:02d}" for r in range(1, 33) for c in range(1, 33)]
                self.csv_writer = csv.DictWriter(self.csv_stream, fieldnames=[
                    "capture_uuid", "capture_generation", "run_id", "layer_id", "main_csv",
                    "interface_id", "connection_generation", "receive_sequence", "host_time_iso",
                    "host_wall_time", "host_monotonic", "device_timestamp", "device_sequence",
                    "byte_offset", "sha256", "pixel_unit", "raw_total", *self.pixel_columns])
                self.csv_writer.writeheader()
                self.csv_stream.flush()
        except Exception:
            self.stream.close()
            if self.csv_stream:
                self.csv_stream.close()
            raise
        self.files.append(self.path)
        self.thread = threading.Thread(target=self._run, daemon=True, name="AFP-M3232-Writer")
        self.thread.start()

    def submit(self, record):
        if self.error or self.closed: raise RuntimeError("m3232_writer_failed: " + (self.error or "closed"))
        try: self.queue.put_nowait(record)
        except queue.Full:
            self.error = "matrix_queue_overflow"
            raise RuntimeError(self.error)

    def _run(self):
        try:
            flushed = time.monotonic()
            while True:
                record = self.queue.get()
                if record is None: break
                if self.stream.tell() >= self.rotate_bytes:
                    self.stream.flush()
                    os.fsync(self.stream.fileno())
                    self.stream.close()
                    path = self.path.with_name(self.path.stem + ".part%d.jsonl" % len(self.files))
                    self.stream = path.open("x", encoding="utf-8")
                    self.files.append(path)
                self.stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                if record.get("kind") == "matrix":
                    self.rows += 1
                    if self.csv_stream:
                        matrix = record["matrix"]
                        if (len(matrix) != 32 or any(len(row) != 32 for row in matrix)
                                or any(type(v) not in (int, float) or not math.isfinite(v) for row in matrix for v in row)):
                            raise ValueError("matrix_csv_requires_32x32_finite_pixels")
                        row = {key: self.metadata.get(key, "") for key in (
                            "capture_uuid", "capture_generation", "run_id", "layer_id", "main_csv", "interface_id")}
                        row.update({key: record.get(key) for key in (
                            "connection_generation", "receive_sequence", "host_wall_time", "host_monotonic",
                            "device_timestamp", "device_sequence", "byte_offset", "sha256", "raw_total")})
                        row.update(host_time_iso=datetime.fromtimestamp(record["host_wall_time"]).astimezone().isoformat(timespec="microseconds"), pixel_unit="raw")
                        row.update(zip(self.pixel_columns, (v for pixels in matrix for v in pixels)))
                        self.csv_writer.writerow(row)
                        self.csv_rows += 1
                if time.monotonic() - flushed >= 0.5:
                    self.stream.flush()
                    if self.csv_stream:
                        self.csv_stream.flush()
                    flushed = time.monotonic()
            self.stream.flush()
            os.fsync(self.stream.fileno())
            if self.csv_stream:
                self.csv_stream.flush()
                os.fsync(self.csv_stream.fileno())
        except Exception as exc: self.error = str(exc)
        finally:
            self.stream.close()
            if self.csv_stream:
                self.csv_stream.close()

    def close(self, evidence):
        if self.closed: return
        self.closed = True
        if self.thread.is_alive():
            try: self.queue.put(None, timeout=2)
            except queue.Full: self.error = self.error or "matrix_writer_finalize_timeout"
            self.thread.join(5)
        if self.thread.is_alive(): self.error = self.error or "matrix_writer_finalize_timeout"
        payload = dict(self.metadata, **evidence)
        files = []
        if not self.thread.is_alive():
            for path in self.files:
                with path.open("rb") as stream:
                    files.append({"path": str(path), "sha256": hashlib.file_digest(stream, "sha256").hexdigest()})
        payload.update(saved_source_rows=self.rows, writer_error=self.error,
                       matrix_csv_file=str(self.csv_path).removesuffix(".partial") if self.csv_path else None,
                       saved_matrix_csv_rows=self.csv_rows,
                       files=files)
        self.metadata_path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


class M3232PressureDriver:
    def __init__(self, endpoint="", baudrate=BAUDRATE, matrix_rows=32, matrix_cols=32, *,
                 data_mode="raw_matrix", device_identity=None, calibration_profile=None,
                 protocol_profile=PROTOCOL, first_frame_timeout_seconds=2.0, freshness_seconds=2.0,
                 frame_timeout_seconds=2.0, max_buffer_bytes=262144, serial_factory=None,
                 inventory=None, recorder=None, clock=time.monotonic, wall_clock=time.time):
        self.config = normalize_config(dict(protocol_profile=protocol_profile, matrix_rows=matrix_rows,
            matrix_cols=matrix_cols, data_mode=data_mode, device_identity=device_identity or {},
            calibration_profile=calibration_profile, first_frame_timeout_seconds=first_frame_timeout_seconds,
            freshness_seconds=freshness_seconds, frame_timeout_seconds=frame_timeout_seconds, max_buffer_bytes=max_buffer_bytes))
        self.endpoint, self.baudrate = str(endpoint).strip(), int(baudrate)
        self.binding = dict(device_identity or {})
        self.clock, self.wall_clock = clock, wall_clock
        self.serial_factory, self.inventory, self.recorder = serial_factory, inventory, recorder
        self.serial = None
        self.sequence = 0
        self.total_frames = 0
        self.total_bytes = 0
        self.protocol_verified = False
        self.open_timeout_seconds = self.config["first_frame_timeout_seconds"] + 0.5
        self.calibration = load_calibration(calibration_profile, self.binding, matrix_rows, matrix_cols) if calibration_profile else None
        if data_mode == "calibrated_force" and not self.calibration:
            raise ValueError("M3232缺少有效力值标定，请选择原始矩阵模式")
        self.last_record = None
        self.last_frame = None
        self.started = self.clock()
        self.parser = self._parser()

    def _parser(self):
        c = self.config
        return MatrixParser(c["matrix_rows"], c["matrix_cols"], c["max_buffer_bytes"], c["frame_timeout_seconds"], self.clock)

    @property
    def quality_metadata(self):
        now = self.clock()
        return dict(driver="m3232_pressure", protocol_profile=PROTOCOL, data_mode=self.config["data_mode"],
            unit="raw" if self.config["data_mode"] == "raw_matrix" else "N", calibrated=bool(self.calibration),
            calibration_digest=self.config["calibration_digest"], matrix_shape=[self.config["matrix_rows"], self.config["matrix_cols"]],
            endpoint=self.endpoint, baudrate=self.baudrate, framing="8N1/LF", protocol_verified=self.protocol_verified,
            received_bytes=self.total_bytes, valid_frames=self.total_frames, rejected_frames=self.parser.rejected,
            last_error=self.parser.last_error, received_monotonic=self.last_frame,
            frame_age_seconds=None if self.last_frame is None else now-self.last_frame,
            valid_frame_rate_hz=self.sequence/max(now-self.started, 0.001), checksum="structure_only_no_vendor_crc",
            connection_generation=getattr(self, "generation", ""), device_identity=self.binding,
            stop_error=getattr(self, "stop_error", ""),
            writer_error=self.recorder.error if self.recorder else "",
            saved_matrix_csv_rows=self.recorder.csv_rows if self.recorder else 0,
            latest_matrix=self.last_record)

    def _evidence(self, **record):
        if self.recorder:
            self.recorder.submit({"connection_generation": self.generation, "host_monotonic": self.clock(), "host_wall_time": self.wall_clock(), **record})

    def open(self):
        if not self.endpoint or not self.binding.get("confirmed") or self.binding.get("endpoint", "").casefold() != self.endpoint.casefold():
            raise ValueError("binding_unconfirmed: 请确认M3232接线/拔插与当前串口归属")
        from serial.tools import list_ports
        ports = self.inventory() if self.inventory else list_ports.comports()
        match = next((p for p in ports if str(p.device).casefold() == self.endpoint.casefold()), None)
        if match is None: raise OSError("M3232绑定串口未连接")
        for key in ("vid", "pid", "serial_number", "location"):
            if self.binding.get(key) not in (None, "") and self.binding[key] != getattr(match, key, None):
                raise ValueError("binding_unconfirmed: M3232串口身份已变化")
        if not any(self.binding.get(k) not in (None, "") for k in ("serial_number", "location")):
            raise ValueError("binding_unconfirmed: 缺少可核对的序列号或USB位置")
        if self.serial_factory is None:
            import serial
            factory = serial.Serial
        else: factory = self.serial_factory
        self.parser = self._parser()
        self.sequence = 0
        self.last_frame, self.last_record, self.protocol_verified = None, None, False
        self.started, self.generation = self.clock(), uuid.uuid4().hex
        self.stop_error = ""
        try:
            self.serial = factory(self.endpoint, self.baudrate, bytesize=8, parity="N", stopbits=1,
                                  timeout=0, write_timeout=0.25, xonxoff=False, rtscts=False, dsrdtr=False)
            self.serial.reset_input_buffer()
            self.serial.write(b"begin\n")
            self._evidence(kind="serial", direction="tx", raw_hex=b"begin\n".hex())
        except Exception:
            self.close()
            raise

    def read_samples(self):
        if self.serial is None: return []
        pending = int(self.serial.in_waiting)
        chunk = self.serial.read(min(pending, 65536)) if pending else b""
        if chunk:
            self._evidence(kind="serial", direction="rx", byte_offset=self.parser.offset + len(self.parser.buffer), raw_hex=chunk.hex())
            self.total_bytes += len(chunk)
        results = []
        for matrix, raw, offset in self.parser.feed(chunk):
            now, wall = self.clock(), self.wall_clock()
            self.sequence += 1
            self.total_frames += 1
            self.last_frame, self.protocol_verified = now, True
            total = math.fsum(v for row in matrix for v in row)
            value, channel = total, RAW_CHANNEL
            if self.calibration and self.config["data_mode"] == "calibrated_force":
                p = self.calibration
                if not p["raw_min"] <= total <= p["raw_max"]: raise ValueError("M3232原始响应超出标定有效范围")
                value, channel = (total-p["zero_total"])*p["scale"], FORCE_CHANNEL
                if not math.isfinite(value): raise ValueError("M3232标定结果非有限值")
            record = dict(kind="matrix", host_monotonic=now, host_wall_time=wall, receive_sequence=self.sequence,
                connection_generation=self.generation, byte_offset=offset, sha256=hashlib.sha256(raw).hexdigest(),
                matrix=matrix, raw_total=total, unit="raw" if channel == RAW_CHANNEL else "N",
                calibration_digest=self.config["calibration_digest"], device_timestamp=None, device_sequence=None)
            self.last_record = record
            self._evidence(**record)
            results.append(({channel: value}, record))
        if not results and self.clock() - (self.last_frame if self.last_frame is not None else self.started) > self.config["freshness_seconds" if self.last_frame is not None else "first_frame_timeout_seconds"]:
            raise TimeoutError("M3232断流" if self.last_frame is not None else "M3232等待有效首帧超时")
        return results

    def read_sample(self):
        results = self.read_samples()
        return results[-1][0] if results else None

    def close(self):
        if self.serial is None: return
        try:
            self.serial.write(b"end\n")
            self._evidence(kind="serial", direction="tx", raw_hex=b"end\n".hex())
        except Exception as exc:
            self.stop_error = str(exc)
            raise
        finally:
            try:
                self.serial.close()
            finally:
                self.serial = None
