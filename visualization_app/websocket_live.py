"""Small dependency-free WebSocket helpers for live dashboard streaming.

The application already owns a threaded HTTP server, so pulling in a second
WebSocket framework would add packaging and deployment risk.  These helpers
cover the RFC 6455 handshake and server-to-client frames used by the live
dashboard; the browser does not need to send application messages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
from typing import Any


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class VersionedPayloadCache:
    """Build a live payload only when a version-aware source advances."""

    def __init__(self) -> None:
        self._version: int | None = None
        self._initialized = False

    def payload_for(self, acquisition: Any, builder) -> dict[str, Any] | None:
        version_reader = getattr(acquisition, "stream_version", None)
        if not callable(version_reader):
            return builder()
        version = int(version_reader())
        if self._initialized and version == self._version:
            return None
        payload = builder()
        self._version = version
        self._initialized = True
        return payload

    @property
    def version(self) -> int | None:
        return self._version


def websocket_accept_value(client_key: str) -> str:
    """Return the RFC 6455 ``Sec-WebSocket-Accept`` value."""
    if not str(client_key or "").strip():
        raise ValueError("缺少 Sec-WebSocket-Key")
    digest = hashlib.sha1(
        (str(client_key).strip() + WEBSOCKET_GUID).encode("ascii")
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_server_frame(payload: str | bytes, opcode: int = 0x1) -> bytes:
    """Encode one unmasked server frame (text, binary, ping or close)."""
    body = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
    first = 0x80 | (int(opcode) & 0x0F)
    length = len(body)
    if length < 126:
        header = bytes((first, length))
    elif length <= 0xFFFF:
        header = bytes((first, 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((first, 127)) + length.to_bytes(8, "big")
    return header + body


def encode_json_frame(payload: dict[str, Any]) -> bytes:
    """Serialize a dashboard payload as one compact WebSocket text frame."""
    return encode_server_frame(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def decode_client_frame(frame: bytes) -> tuple[int, bytes]:
    """Decode one complete masked client frame.

    Browser/helper clients must mask frames sent to a server.  The helper is
    deliberately limited to one complete frame because the HTTP handler reads
    the exact frame length from the socket before calling this function.
    """
    data = bytes(frame)
    if len(data) < 2:
        raise ValueError("WebSocket 帧不完整")
    first, second = data[0], data[1]
    if not first & 0x80:
        raise ValueError("不支持 WebSocket 分片帧")
    if not second & 0x80:
        raise ValueError("客户端 WebSocket 帧必须掩码")
    opcode = first & 0x0F
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(data) < offset + 2:
            raise ValueError("WebSocket 帧长度不完整")
        length = int.from_bytes(data[offset : offset + 2], "big")
        offset += 2
    elif length == 127:
        if len(data) < offset + 8:
            raise ValueError("WebSocket 帧长度不完整")
        length = int.from_bytes(data[offset : offset + 8], "big")
        offset += 8
    if len(data) < offset + 4 + length:
        raise ValueError("WebSocket 帧载荷不完整")
    if len(data) != offset + 4 + length:
        raise ValueError("WebSocket 帧包含多余数据")
    mask = data[offset : offset + 4]
    offset += 4
    payload = bytes(value ^ mask[index % 4] for index, value in enumerate(data[offset:]))
    return opcode, payload


def recv_client_frame(connection: socket.socket, max_bytes: int = 1_048_576) -> tuple[int, bytes]:
    """Read and decode one complete client frame from a connected socket."""
    def receive_exact(size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = connection.recv(remaining)
            if not chunk:
                raise ConnectionError("WebSocket 连接已关闭")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    head = receive_exact(2)
    length_code = head[1] & 0x7F
    extension = b""
    if length_code == 126:
        extension = receive_exact(2)
        length = int.from_bytes(extension, "big")
    elif length_code == 127:
        extension = receive_exact(8)
        length = int.from_bytes(extension, "big")
    else:
        length = length_code
    if length > max_bytes:
        raise ValueError("WebSocket 帧载荷过大")
    mask = receive_exact(4) if head[1] & 0x80 else b""
    payload = receive_exact(length)
    raw = head + extension + mask + payload
    return decode_client_frame(raw)


def websocket_handshake_headers(client_key: str) -> dict[str, str]:
    """Return the HTTP headers required for a successful protocol upgrade."""
    return {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Accept": websocket_accept_value(client_key),
    }
