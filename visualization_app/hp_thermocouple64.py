"""HuiPu TCP8–TCP64, Modbus protocol A (2021-08-02), read-only acquisition."""

from __future__ import annotations

import math
import struct
import time
from typing import Any

from thermocouple64 import THERMOCOUPLE64_CHANNELS, THERMOCOUPLE64_DRIVER


class HpProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
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
        raise HpProtocolError("device_exception", f"仪表返回异常0x{code:04X}")
    if frame[1] != 3:
        raise HpProtocolError("function", "期望FC03读取响应")
    if frame[2] != count * 2 or len(frame) != count * 2 + 5:
        raise HpProtocolError("byte_count", "响应字节数与本次请求不一致")
    return struct.unpack(f">{count}H", frame[3:-2])


def decode_channel(registers: tuple[int, ...]) -> tuple[float | None, str]:
    if len(registers) != 20:
        raise HpProtocolError("channel_length", "每个通道必须包含20个寄存器")
    if registers[14] == 1:
        return None, "channel_disabled"
    if registers[14] != 0:
        return None, "invalid_switch"
    if not 0 <= registers[2] <= 10:
        return None, "non_temperature_input"
    unit = registers[1]
    if unit not in (0, 1, 2):
        return None, "non_temperature_unit"
    # Each register is big endian, but the low 16-bit word precedes the high.
    value = struct.unpack(">f", struct.pack(">HH", registers[19], registers[18]))[0]
    if not math.isfinite(value):
        return None, "non_finite"
    if unit == 1:
        value -= 273.15
    elif unit == 2:
        value = (value - 32.0) * 5.0 / 9.0
    return value, ""


class HpThermocouple64Driver:
    """Read all 64 channel records in eleven bounded FC03 transactions.

    A scan is published only after every block passes validation. Channel
    faults remain explicit nulls; a transport failure rejects the entire scan.
    """

    def __init__(
        self, endpoint: str, baudrate: int = 115200, slave_id: int = 1,
        timeout: float = 0.5, *, serial_factory: Any = None,
    ) -> None:
        read_request(int(slave_id), 0, 1)
        self.endpoint = str(endpoint or "").strip()
        self.baudrate = int(baudrate)
        if self.baudrate not in (2400, 4800, 9600, 19200, 115200):
            raise ValueError("慧谱波特率应为2400、4800、9600、19200或115200")
        self.slave_id = int(slave_id)
        self.timeout = max(0.01, min(float(timeout), 2.0))
        self.open_timeout_seconds = max(2.5, 11 * self.timeout + 27000 / self.baudrate + 1)
        self.serial_factory = serial_factory
        self.serial = None
        self.protocol_verified = False
        self.observation_count = 0
        self.error_counts: dict[str, int] = {}
        self.last_error = ""
        self.quality_metadata: dict[str, Any] = {}
        self._pending: dict[str, float | None] | None = None
        self._last_response_time = 0.0

    def _read_registers(self, start: int, count: int) -> tuple[int, ...]:
        port = self.serial
        if port is None:
            raise HpProtocolError("port_closed", "串口尚未打开")
        # Keep a quiet interval between responses and the next request.
        gap = max(0.003, 3.5 * 10 / self.baudrate)
        delay = self._last_response_time + gap - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        port.reset_input_buffer()
        request = read_request(self.slave_id, start, count)
        if port.write(request) != len(request):
            raise HpProtocolError("write_incomplete", "读取请求未完整写入串口")
        port.flush()
        # Allow the on-wire duration at lower configured baud rates as well.
        deadline = time.monotonic() + self.timeout + (count * 2 + 5) * 10 / self.baudrate
        buffer = bytearray()
        while time.monotonic() < deadline:
            chunk = port.read(256)
            if not chunk:
                time.sleep(0.001)
                continue
            buffer.extend(chunk)
            if len(buffer) < 2:
                continue
            if buffer[0] != self.slave_id:
                raise HpProtocolError("address", "收到其他地址的数据；请检查接线和通信模式")
            if buffer[1] == 0x83:
                # A valid standard exception can be accepted after five bytes;
                # the documented two-byte vendor exception needs six bytes.
                if len(buffer) >= 5 and append_crc(bytes(buffer[:3])) == bytes(buffer[:5]):
                    parse_response(bytes(buffer[:5]), self.slave_id, count)
                if len(buffer) >= 6:
                    parse_response(bytes(buffer[:6]), self.slave_id, count)
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
                self._last_response_time = time.monotonic()
                return result
        raise HpProtocolError("timeout", f"读取寄存器{start}超时（收到{len(buffer)}字节）")

    def _scan(self) -> dict[str, float | None]:
        started = time.monotonic()
        values: dict[str, float | None] = {}
        errors: dict[str, str] = {}
        try:
            for first in range(0, 64, 6):
                size = min(6, 64 - first)
                registers = self._read_registers(first * 20, size * 20)
                for index in range(size):
                    name = THERMOCOUPLE64_CHANNELS[first + index]
                    value, error = decode_channel(registers[index * 20:(index + 1) * 20])
                    values[name] = value
                    if error:
                        errors[name] = error
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            self.error_counts[code] = self.error_counts.get(code, 0) + 1
            self.last_error = str(exc)
            self.protocol_verified = False
            self.quality_metadata = {
                **self.quality_metadata, "last_error": self.last_error,
                "protocol_error_counts": dict(self.error_counts),
            }
            raise
        self.observation_count += 1
        self.protocol_verified = True
        self.last_error = ""
        self.quality_metadata = {
            "driver": THERMOCOUPLE64_DRIVER, "protocol": "modbus_rtu",
            "protocol_version": "TCP8-TCP64-A-20210802",
            "slave_id": self.slave_id, "channel_errors": errors,
            "observation_count": self.observation_count,
            "scan_duration_seconds": time.monotonic() - started,
            "received_wall_time": time.time(),
            "timestamp_origin": "host_scan_completion",
            "last_error": "", "protocol_error_counts": dict(self.error_counts),
        }
        return values

    def open(self) -> None:
        if not self.endpoint:
            raise ValueError("慧谱64路热电偶必须选择实际串口")
        if self.serial_factory is None:
            import serial
            factory = serial.Serial
        else:
            factory = self.serial_factory
        self.serial = factory(
            port=self.endpoint, baudrate=self.baudrate, bytesize=8,
            parity="N", stopbits=1, timeout=0.02, write_timeout=self.timeout,
        )
        self._last_response_time = 0.0
        try:
            self._pending = self._scan()
        except Exception:
            self.close()
            raise

    def read_sample(self) -> dict[str, float | None]:
        if self._pending is not None:
            sample, self._pending = self._pending, None
            return sample
        return self._scan()

    def close(self) -> None:
        port, self.serial = self.serial, None
        self._pending = None
        self.protocol_verified = False
        if port is not None:
            port.close()
