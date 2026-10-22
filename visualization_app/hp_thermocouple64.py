"""HuiPu TCP8–TCP64, Modbus protocol A (2021-08-02), read-only acquisition."""

from __future__ import annotations

import math
import struct
import time
from acquisition_clock import monotonic
import threading
from copy import deepcopy
from typing import Any

from thermocouple64 import THERMOCOUPLE64_CHANNELS, THERMOCOUPLE64_DRIVER


class HpProtocolError(ValueError):
    def __init__(self, code: str, message: str, *, exception_code: int | None = None) -> None:
        self.code = code
        self.exception_code = exception_code
        super().__init__(f"慧谱64路热电偶[{code}]：{message}")


def append_crc(data: bytes) -> bytes:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return data + struct.pack("<H", crc)


def read_request(slave_id: int, start: int, count: int) -> bytes:
    if not 1 <= slave_id <= 247:
        raise ValueError("慧谱仪表地址必须在1到247之间，不能使用广播地址0")
    if not 1 <= count <= 125 or start < 0 or start + count > 1280:
        raise ValueError("慧谱读取范围必须位于0～1279，每次最多125个寄存器")
    return append_crc(struct.pack(">BBHH", slave_id, 3, start, count))


def parse_response(frame: bytes, slave_id: int, count: int) -> tuple[int, ...]:
    if len(frame) < 5:
        raise HpProtocolError("length", "响应帧不完整")
    if append_crc(frame[:-2]) != frame:
        raise HpProtocolError("crc", "CRC16校验失败")
    if frame[0] != slave_id:
        raise HpProtocolError("address", "响应地址与所选仪表地址不一致")
    if frame[1] == 0x83:
        if len(frame) not in (5, 6):
            raise HpProtocolError("length", "异常响应长度错误")
        # Protocol A documents two-byte 0x02xx errors; standard RTU uses one.
        code = int.from_bytes(frame[2:-2], "big")
        raise HpProtocolError("device_exception", f"仪表返回异常0x{code:04X}", exception_code=code)
    if frame[1] != 3:
        raise HpProtocolError("function", "期望FC03读取响应")
    if frame[2] != count * 2 or len(frame) != count * 2 + 5:
        raise HpProtocolError("byte_count", "响应字节数与本次请求不一致")
    return struct.unpack(f">{count}H", frame[3:-2])


READ_PROFILES = ("channel_temperature", "full_record_block")


def normalize_read_profile(value: Any = None) -> str:
    profile = str(value or "channel_temperature")
    if profile not in READ_PROFILES:
        raise ValueError("慧谱读取方式必须为channel_temperature或full_record_block")
    return profile


def settings_binding(endpoint: str, baudrate: int, slave_id: int, read_profile: str,
                     device_identity: dict | None = None) -> dict[str, Any]:
    return {"endpoint": str(endpoint).upper(), "baudrate": int(baudrate),
            "slave_id": int(slave_id), "read_profile": normalize_read_profile(read_profile),
            "device_identity": deepcopy(device_identity or {})}


def decode_temperature(words: tuple[int, ...], settings: dict | None) -> tuple[float | None, str]:
    if len(words) != 2:
        raise HpProtocolError("channel_length", "温度必须包含两个寄存器")
    if not settings:
        return None, "settings_unconfirmed"
    switch, sensor_type, unit = (settings.get(k) for k in ("switch", "input_type", "unit"))
    if switch == 1:
        return None, "channel_disabled"
    if type(switch) is not int or switch != 0:
        return None, "invalid_switch"
    if type(sensor_type) is not int or not 0 <= sensor_type <= 10:
        return None, "non_temperature_input"
    if type(unit) is not int or unit not in (0, 1, 2):
        return None, "non_temperature_unit"
    value = struct.unpack(">f", struct.pack(">HH", words[1], words[0]))[0]
    if not math.isfinite(value):
        return None, "non_finite"
    if unit == 1:
        value -= 273.15
    elif unit == 2:
        value = (value - 32.0) * 5.0 / 9.0
    return value, ""


def decode_channel(registers: tuple[int, ...]) -> tuple[float | None, str]:
    if len(registers) != 20:
        raise HpProtocolError("channel_length", "每个通道必须包含20个寄存器")
    return decode_temperature(registers[18:20], {
        "switch": registers[14], "input_type": registers[2], "unit": registers[1],
    })


class HpThermocouple64Driver:
    """只读扫描64路；全轮校验后原子发布，通信失败废弃整轮。"""

    def __init__(
        self, endpoint: str, baudrate: int = 115200, slave_id: int = 1,
        timeout: float = 0.5, *, serial_factory: Any = None,
        read_profile: str = "channel_temperature", channel_settings: dict | None = None,
        device_identity: dict | None = None, port_provider: Any = None,
        scan_timeout_seconds: float | None = None,
    ) -> None:
        read_request(int(slave_id), 0, 1)
        self.endpoint = str(endpoint or "").strip()
        self.baudrate = int(baudrate)
        if self.baudrate not in (2400, 4800, 9600, 19200, 115200):
            raise ValueError("慧谱波特率应为2400、4800、9600、19200或115200")
        self.slave_id = int(slave_id)
        self.timeout = max(0.01, min(float(timeout), 2.0))
        self.read_profile = normalize_read_profile(read_profile)
        self.channel_settings = deepcopy(channel_settings or {})
        if not isinstance(self.channel_settings, dict) or not isinstance(self.channel_settings.get("channels", {}), dict):
            raise ValueError("慧谱通道设置及channels必须为对象")
        self.device_identity = deepcopy(device_identity or {})
        self.port_provider = port_provider
        transactions = 192 if self.read_profile == "channel_temperature" else 11
        wire_bytes = 3136 if self.read_profile == "channel_temperature" else 2703
        budget = transactions * (self.timeout + max(0.003, 35 / self.baudrate) + 0.025) + wire_bytes * 10 / self.baudrate + 1
        self.scan_timeout_seconds = float(scan_timeout_seconds or budget)
        if not math.isfinite(self.scan_timeout_seconds) or not 0 < self.scan_timeout_seconds <= 600:
            raise ValueError("整轮扫描截止必须为有限正数")
        self.open_timeout_seconds = self.scan_timeout_seconds + 1
        self.serial_factory = serial_factory
        self.serial = None
        self.protocol_verified = False
        self.observation_count = 0
        self.error_counts: dict[str, int] = {}
        self.last_error = ""
        self.quality_metadata: dict[str, Any] = {"driver": THERMOCOUPLE64_DRIVER}
        self._pending: dict[str, float | None] | None = None
        self._last_response_time = 0.0
        self._cancel = threading.Event()
        self._open_count = 0
        self._scan_deadline = float("inf")
        self._diagnostic: dict[str, Any] = {}
        self._tx_bytes = self._rx_bytes = 0
        self._verified_channel_settings: dict[str, dict[str, Any] | None] | None = None

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise HpProtocolError("cancelled", "扫描已停止")
        if time.perf_counter() >= self._scan_deadline:
            raise HpProtocolError("scan_timeout", "整轮扫描超过截止时间")

    def _read_registers(self, start: int, count: int) -> tuple[int, ...]:
        self._check_cancel()
        port = self.serial
        if port is None:
            raise HpProtocolError("port_closed", "串口尚未打开")
        # Keep a quiet interval between responses and the next request.
        gap = max(0.003, 3.5 * 10 / self.baudrate)
        delay = self._last_response_time + gap - time.perf_counter()
        if delay > 0:
            self._cancel.wait(delay)
        self._check_cancel()
        if getattr(port, "in_waiting", 0):
            raise HpProtocolError("residual_data", "请求前存在残留帧，需重新连接")
        port.reset_input_buffer()
        request = read_request(self.slave_id, start, count)
        self._diagnostic = {"start_register": start, "register_count": count,
                            "channel": start // 20 + 1, "request_hex": request.hex(" "),
                            "response_hex": "", "tx_bytes": 0, "rx_bytes": 0}
        written = port.write(request)
        self._tx_bytes += written
        self._diagnostic["tx_bytes"] = written
        if written != len(request):
            raise HpProtocolError("write_incomplete", "读取请求未完整写入串口")
        # Allow the on-wire duration at lower configured baud rates as well.
        wire_bytes = len(request) + count * 2 + 5
        deadline = min(
            self._scan_deadline,
            time.perf_counter() + self.timeout + wire_bytes * 10 / self.baudrate,
        )
        buffer = bytearray()
        while time.perf_counter() < deadline:
            self._check_cancel()
            chunk = port.read(256)
            if not chunk:
                time.sleep(0.001)
                continue
            buffer.extend(chunk)
            self._rx_bytes += len(chunk)
            self._diagnostic.update(rx_bytes=len(buffer), response_hex=bytes(buffer[:256]).hex(" "))
            if len(buffer) < 2:
                continue
            if buffer[0] != self.slave_id:
                raise HpProtocolError("address", "收到其他地址的数据；请检查接线和通信模式")
            if buffer[1] == 0x83:
                # A valid standard exception can be accepted after five bytes;
                # the documented two-byte vendor exception needs six bytes.
                if len(buffer) >= 5 and append_crc(bytes(buffer[:3])) == bytes(buffer[:5]):
                    if len(buffer) != 5:
                        raise HpProtocolError("trailing_data", "异常响应附带额外数据")
                    self._verify_silence(buffer)
                    parse_response(bytes(buffer), self.slave_id, count)
                if len(buffer) >= 6:
                    if len(buffer) != 6:
                        raise HpProtocolError("trailing_data", "异常响应附带额外数据")
                    self._verify_silence(buffer)
                    parse_response(bytes(buffer), self.slave_id, count)
                continue
            if buffer[1] != 3:
                raise HpProtocolError("function", "仪表未返回FC03，请确认仪表已切换至Modbus模式")
            if len(buffer) < 3:
                continue
            if buffer[2] != count * 2:
                raise HpProtocolError("byte_count", "收到不属于本次请求的数据长度")
            expected = count * 2 + 5
            if len(buffer) >= expected:
                if len(buffer) != expected:
                    raise HpProtocolError("trailing_data", "响应附带额外数据，拒绝混合报文")
                result = parse_response(bytes(buffer), self.slave_id, count)
                self._verify_silence(buffer)
                return result
        if len(buffer) in (5, 6) and buffer[1] == 0x83:
            parse_response(bytes(buffer), self.slave_id, count)
        raise HpProtocolError("timeout", f"读取寄存器{start}超时（收到{len(buffer)}字节）")

    def _verify_silence(self, response: bytes) -> None:
        self._last_response_time = time.perf_counter()
        deadline = self._last_response_time + max(0.003, 35 / self.baudrate)
        while time.perf_counter() < deadline:
            self._check_cancel()
            extra = self.serial.read(256)
            if extra:
                self._rx_bytes += len(extra)
                self._diagnostic.update(rx_bytes=len(response) + len(extra), response_hex=(bytes(response) + extra)[:256].hex(" "))
                raise HpProtocolError("trailing_data", "响应附带额外数据")
            time.sleep(min(0.001, max(0.0, deadline - time.perf_counter())))

    def _settings(self, index: int) -> dict | None:
        try:
            unit, sensor_type = self._read_registers(index * 20 + 1, 2)
            switch, = self._read_registers(index * 20 + 14, 1)
            return {"unit": unit, "input_type": sensor_type, "switch": switch, "source": "device_readback"}
        except HpProtocolError as exc:
            if exc.code != "device_exception" or exc.exception_code not in (1, 2, 3, 0x0206, 0x0209, 0x0210):
                raise
            confirmation = self.channel_settings
            if (self._open_count == 1 and confirmation.get("confirmed") is True
                    and confirmation.get("binding") == settings_binding(
                        self.endpoint, self.baudrate, self.slave_id, self.read_profile, self.device_identity)):
                settings = (confirmation.get("channels") or {}).get(THERMOCOUPLE64_CHANNELS[index])
                if isinstance(settings, dict) and all(k in settings for k in ("unit", "input_type", "switch")):
                    return {**settings, "source": "operator_confirmed"}
            return None

    def _metadata(self) -> dict[str, Any]:
        return {"driver": THERMOCOUPLE64_DRIVER, "protocol": "modbus_rtu",
                "endpoint": self.endpoint, "baudrate": self.baudrate, "bytesize": 8,
                "parity": "N", "stopbits": 1, "slave_id": self.slave_id,
                "timeout": self.timeout, "read_profile": self.read_profile,
                "scan_timeout_seconds": self.scan_timeout_seconds,
                "poll_interval_seconds": getattr(self, "poll_interval_seconds", 0.1),
                "protocol_verified": self.protocol_verified,
                "last_transaction": dict(self._diagnostic),
                "tx_bytes": self._tx_bytes, "rx_bytes": self._rx_bytes}

    def _scan(self) -> dict[str, float | None]:
        started = time.perf_counter()
        started_monotonic = monotonic()
        started_wall = time.time()
        self._scan_deadline = started + self.scan_timeout_seconds
        values: dict[str, float | None] = {}
        errors: dict[str, str] = {}
        settings_evidence: dict[str, Any] = {}
        verified_settings: dict[str, dict[str, Any] | None] = {}
        refresh_settings = self.read_profile == "channel_temperature" and self._verified_channel_settings is None
        try:
            if self.read_profile == "channel_temperature":
                for index, name in enumerate(THERMOCOUPLE64_CHANNELS):
                    settings = (
                        self._settings(index)
                        if refresh_settings
                        else deepcopy(self._verified_channel_settings.get(name))
                    )
                    verified_settings[name] = deepcopy(settings)
                    settings_evidence[name] = settings or {"source": "unconfirmed"}
                    words = self._read_registers(index * 20 + 18, 2)
                    value, error = decode_temperature(words, settings)
                    values[name] = value
                    if error:
                        errors[name] = error
            for first in (range(0, 64, 6) if self.read_profile == "full_record_block" else []):
                size = min(6, 64 - first)
                registers = self._read_registers(first * 20, size * 20)
                for index in range(size):
                    name = THERMOCOUPLE64_CHANNELS[first + index]
                    value, error = decode_channel(registers[index * 20:(index + 1) * 20])
                    record = registers[index * 20:(index + 1) * 20]
                    settings_evidence[name] = {"unit": record[1], "input_type": record[2], "switch": record[14], "source": "device_readback"}
                    values[name] = value
                    if error:
                        errors[name] = error
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            self._verified_channel_settings = None
            self.error_counts[code] = self.error_counts.get(code, 0) + 1
            self.last_error = str(exc)
            self.protocol_verified = False
            self.quality_metadata = {
                **self.quality_metadata, "last_error": self.last_error,
                **self._metadata(), "error_code": code,
                "scan_duration_seconds": time.perf_counter() - started,
                "protocol_error_counts": dict(self.error_counts),
            }
            raise
        self.observation_count += 1
        if refresh_settings:
            self._verified_channel_settings = verified_settings
        self.protocol_verified = True
        self.last_error = ""
        self.quality_metadata = {
            **self._metadata(),
            "driver": THERMOCOUPLE64_DRIVER, "protocol": "modbus_rtu",
            "protocol_version": "TCP8-TCP64-A-20210802",
            "slave_id": self.slave_id, "channel_errors": errors,
            "observation_count": self.observation_count,
            "valid_temperature_count": sum(v is not None for v in values.values()),
            "channel_settings": settings_evidence,
            "scan_started_wall_time": started_wall,
            "scan_started_monotonic": started_monotonic,
            "received_monotonic": monotonic(),
            "scan_duration_seconds": time.perf_counter() - started,
            "received_wall_time": time.time(),
            "timestamp_origin": "host_scan_completion",
            "last_error": "", "protocol_error_counts": dict(self.error_counts),
            "error_code": "",
        }
        return values

    def open(self) -> None:
        self._cancel.clear()
        if self.serial is not None:
            self.close()
            self._cancel.clear()
        if self.device_identity.get("serial_number") or (self._open_count and self.observation_count):
            try:
                self._resolve_same_device()
            except HpProtocolError as exc:
                self.protocol_verified = False
                self.quality_metadata = {**self._metadata(), "error_code": exc.code, "last_error": str(exc)}
                raise
        self._open_count += 1
        if not self.endpoint:
            raise ValueError("慧谱64路热电偶必须选择实际串口")
        if self.serial_factory is None:
            import serial
            factory = serial.Serial
        else:
            factory = self.serial_factory
        self._last_response_time = 0.0
        try:
            self.serial = factory(
                port=self.endpoint, baudrate=self.baudrate, bytesize=8,
                parity="N", stopbits=1, timeout=0, write_timeout=self.timeout,
            )
            self._pending = self._scan()
        except Exception as exc:
            if not isinstance(exc, HpProtocolError):
                text = str(exc).lower()
                code = "port_busy" if isinstance(exc, PermissionError) or any(s in text for s in ("access is denied", "permission", "拒绝访问", "占用")) else "port_open_failed"
                self.quality_metadata = {**self._metadata(), "error_code": code, "last_error": str(exc)}
            self.close()
            raise

    def _resolve_same_device(self) -> None:
        identity = self.device_identity
        serial_number = identity.get("serial_number")
        if not serial_number:
            raise HpProtocolError("binding_unconfirmed", "重连缺少稳定设备身份，请重新选择并检查接口")
        if self.port_provider is None:
            from serial.tools import list_ports
            ports = list_ports.comports()
        else:
            ports = self.port_provider()
        matches = [p for p in ports if p.serial_number == serial_number
                   and p.vid == identity.get("vid") and p.pid == identity.get("pid")]
        if len(matches) != 1:
            raise HpProtocolError("device_missing", "无法唯一定位已确认设备，等待重枚举或重新选择")
        port = matches[0]
        self.endpoint = str(port.device)
        self.device_identity["location"] = getattr(port, "location", None)
        self.protocol_verified = False

    def read_sample(self) -> dict[str, float | None]:
        if self._pending is not None:
            sample, self._pending = self._pending, None
            return sample
        return self._scan()

    def close(self) -> None:
        self._cancel.set()
        port, self.serial = self.serial, None
        self._pending = None
        self._verified_channel_settings = None
        self.protocol_verified = False
        if port is not None:
            port.close()
