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
from typing import Any


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


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


def websocket_handshake_headers(client_key: str) -> dict[str, str]:
    """Return the HTTP headers required for a successful protocol upgrade."""
    return {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Accept": websocket_accept_value(client_key),
    }
