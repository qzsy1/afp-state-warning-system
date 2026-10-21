"""HIK UVC 温度载荷校验；换算依据由已核验设备档显式指定。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct


class TemperatureFrameError(ValueError):
    pass


def validate_roi(roi, width: int, height: int) -> tuple[int, int, int, int]:
    if roi is None:
        return 0, 0, width, height
    if not isinstance(roi, (tuple, list)) or len(roi) != 4:
        raise TemperatureFrameError("invalid_roi")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in roi):
        raise TemperatureFrameError("invalid_roi")
    x1, y1, x2, y2 = roi
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise TemperatureFrameError("invalid_roi")
    return x1, y1, x2, y2


@dataclass(frozen=True)
class TemperatureFrame:
    source_sequence: int
    width: int
    height: int
    matrix: tuple[float, ...]
    raw_matrix: bytes
    evidence: dict

    def roi_mean(self, roi=None) -> float:
        x1, y1, x2, y2 = validate_roi(roi, self.width, self.height)
        return math.fsum(self.matrix[y * self.width + x]
                         for y in range(y1, y2) for x in range(x1, x2)) / ((x2-x1)*(y2-y1))


def decode_temperature_matrix(raw: bytes, *, width: int, height: int,
                              encoding: str, conversion: str,
                              scale: float | None = None, offset: float | None = None):
    if not (0 < width <= 2048 and 0 < height <= 2048):
        raise TemperatureFrameError("invalid_dimensions")
    size = {"uint16_le": 2, "float32_le": 4}.get(encoding)
    if size is None or len(raw) != width * height * size:
        raise TemperatureFrameError("invalid_matrix_length_or_encoding")
    if not any(raw):
        raise TemperatureFrameError("uninitialized_zero_matrix")
    values = struct.unpack("<" + str(width * height) + ("H" if size == 2 else "f"), raw)
    if encoding == "float32_le" and conversion == "celsius_float32":
        matrix = values
    elif encoding == "uint16_le" and conversion == "raw64_minus50":
        matrix = tuple(v / 64.0 - 50.0 for v in values)
    elif encoding == "uint16_le" and conversion == "header_kelvin":
        if scale is None or offset is None or not math.isfinite(scale) or scale <= 0 or not math.isfinite(offset):
            raise TemperatureFrameError("invalid_conversion_parameters")
        matrix = tuple(v / scale + offset - 273.15 for v in values)
    else:
        raise TemperatureFrameError("unconfirmed_conversion")
    if not all(math.isfinite(v) and v >= -273.15 for v in matrix):
        raise TemperatureFrameError("invalid_temperature_pixel")
    return matrix


def parse_temperature_yuv(payload: bytes, *, conversion: str, expected_magic: int,
                          expected_dimensions: tuple[int, int]) -> TemperatureFrame:
    """解析指南 B.1.3；不搜索偏移、不修补数据、不推测换算公式。"""
    payload = bytes(payload)
    if len(payload) < 120:
        raise TemperatureFrameError("truncated_header")
    magic, header_size, stream_type, stream_len, has_yuv = struct.unpack_from("<5I", payload)
    if magic != expected_magic or stream_type != 8 or has_yuv != 1:
        raise TemperatureFrameError("not_temperature_yuv_stream")
    if header_size < 4636 or header_size > 65536 or len(payload) != header_size + stream_len:
        raise TemperatureFrameError("invalid_frame_length")
    mode, scale, offset, frozen = struct.unpack_from("<4I", payload, 20)
    data_type, sequence = struct.unpack_from("<2I", payload, 36)
    width, height, matrix_len, fps, channel = struct.unpack_from("<5I", payload, 64)
    yuv_sequence, yuv_width, yuv_height, yuv_len = struct.unpack_from("<4I", payload, 84)
    if frozen != 0:
        raise TemperatureFrameError("frozen_frame")
    if data_type != 2 or mode not in (0, 1):
        raise TemperatureFrameError("not_temperature_matrix")
    if (width, height) != expected_dimensions or (yuv_width, yuv_height) != (width, height):
        raise TemperatureFrameError("unexpected_dimensions")
    expected_len = width * height * (2 if mode == 1 else 4)
    if matrix_len != expected_len or yuv_len != width * height * 2 or stream_len != 4 + matrix_len + yuv_len:
        raise TemperatureFrameError("inconsistent_payload_lengths")
    if sequence != yuv_sequence:
        raise TemperatureFrameError("inconsistent_source_sequences")
    raw = payload[header_size + 4:header_size + 4 + matrix_len]
    encoding = "uint16_le" if mode == 1 else "float32_le"
    matrix = decode_temperature_matrix(raw, width=width, height=height, encoding=encoding,
                                       conversion=conversion, scale=scale, offset=offset)
    return TemperatureFrame(sequence, width, height, matrix, raw, {
        "frame_kind": "temperature_matrix", "stream_type": stream_type,
        "header_size": header_size, "payload_length": len(payload), "encoding": encoding,
        "conversion": conversion, "scale": scale, "offset": offset,
        "frozen": False, "source_frame_counter": sequence, "declared_fps": fps,
        "channel": channel, "matrix_sha256": hashlib.sha256(raw).hexdigest(),
        "payload_sha256": hashlib.sha256(payload).hexdigest(), "header_crc_verified": False,
    })


class SourceFrameTracker:
    """同一连接代次内仅接受向前源序号；不假定设备每帧递增一。"""
    def __init__(self):
        self.last_sequence = None
        self.observations = 0
        self.duplicates = 0

    def accept(self, sequence: int) -> bool:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 0xFFFFFFFF:
            raise TemperatureFrameError("invalid_source_sequence")
        if self.last_sequence is not None:
            delta = (sequence - self.last_sequence) & 0xFFFFFFFF
            if delta == 0:
                self.duplicates += 1
                return False
            if delta >= 0x80000000:
                raise TemperatureFrameError("source_sequence_reset_or_out_of_order")
        self.last_sequence = sequence
        self.observations += 1
        return True
