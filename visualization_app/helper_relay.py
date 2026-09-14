"""In-memory authorization and command contract for local capture helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


ALLOWED_HELPER_COMMANDS = frozenset(
    {"discover", "check_capture", "mysql_preflight", "mysql_relation_map", "start_capture", "stop_capture", "status"}
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class _Pairing:
    session_id: str
    challenge_hash: str
    created_at: float
    expires_at: float


@dataclass
class _Helper:
    session_id: str
    device_id: str
    token_hash: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    online: bool = False
    last_seen: float | None = None
    sender: Callable[[dict[str, Any]], None] | None = None
    queue: deque[dict[str, Any]] = field(default_factory=deque)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)


class HelperRegistry:
    """Authorize one helper per web session without persisting secrets."""

    def __init__(self, *, challenge_ttl_seconds: int = 300) -> None:
        self.challenge_ttl_seconds = max(30, int(challenge_ttl_seconds))
        self._lock = threading.RLock()
        self._pairings: dict[str, _Pairing] = {}
        self._helpers: dict[str, _Helper] = {}

    def start_pairing(self, session_id: str) -> dict[str, Any]:
        challenge = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._pairings[session_id] = _Pairing(
                session_id=session_id,
                challenge_hash=_hash(challenge),
                created_at=now,
                expires_at=now + self.challenge_ttl_seconds,
            )
        return {
            "ok": True,
            "challenge": challenge,
            "expires_in_seconds": self.challenge_ttl_seconds,
        }

    def complete_pairing(
        self,
        challenge: str,
        device_id: str,
        capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            matched_session = None
            for session_id, pairing in self._pairings.items():
                if pairing.expires_at >= now and hmac.compare_digest(
                    pairing.challenge_hash, _hash(challenge)
                ):
                    matched_session = session_id
                    break
            if matched_session is None:
                return {"ok": False, "error": "pairing_invalid_or_expired"}
            self._pairings.pop(matched_session, None)
            token = secrets.token_urlsafe(32)
            self._helpers[matched_session] = _Helper(
                session_id=matched_session,
                device_id=str(device_id),
                token_hash=_hash(token),
                capabilities=dict(capabilities or {}),
                online=True,
                last_seen=now,
            )
            return {
                "ok": True,
                "session_id": matched_session,
                "device_id": str(device_id),
                "pairing_token": token,
            }

    def attach(
        self,
        session_id: str,
        device_id: str,
        pairing_token: str,
        sender: Callable[[dict[str, Any]], None],
    ) -> bool:
        with self._lock:
            helper = self._helpers.get(session_id)
            if helper is None or helper.device_id != str(device_id):
                return False
            if not hmac.compare_digest(helper.token_hash, _hash(pairing_token)):
                return False
            helper.sender = sender
            helper.online = True
            helper.last_seen = time.time()
            return True

    def detach(self, session_id: str) -> None:
        with self._lock:
            helper = self._helpers.get(session_id)
            if helper:
                helper.online = False
                helper.sender = None

    def authenticate(self, device_id: str, pairing_token: str) -> str | None:
        """Return the owning session for a helper bearer token."""
        with self._lock:
            for session_id, helper in self._helpers.items():
                if helper.device_id == str(device_id) and hmac.compare_digest(
                    helper.token_hash, _hash(pairing_token)
                ):
                    helper.online = True
                    helper.last_seen = time.time()
                    return session_id
        return None

    def poll(self, device_id: str, pairing_token: str) -> dict[str, Any]:
        session_id = self.authenticate(device_id, pairing_token)
        if session_id is None:
            return {"ok": False, "error": "helper_authentication_failed"}
        with self._lock:
            helper = self._helpers[session_id]
            if helper.queue:
                return {"ok": True, "command": helper.queue.popleft()}
            return {"ok": True, "command": None, "heartbeat": True}

    def accept_result(
        self,
        device_id: str,
        pairing_token: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        session_id = self.authenticate(device_id, pairing_token)
        if session_id is None:
            return {"ok": False, "error": "helper_authentication_failed"}
        with self._lock:
            helper = self._helpers[session_id]
            helper.results[str(request_id)] = dict(payload)
        return {"ok": True, "request_id": str(request_id)}

    def pop_result(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            helper = self._helpers.get(session_id)
            if helper is None:
                return None
            return helper.results.pop(str(request_id), None)

    def status(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            helper = self._helpers.get(session_id)
            if helper is None:
                return {"paired": False, "online": False, "capabilities": {}}
            return {
                "paired": True,
                "online": bool(helper.online),
                "device_id": helper.device_id,
                "capabilities": dict(helper.capabilities),
                "last_seen": helper.last_seen,
            }

    def command(
        self,
        session_id: str,
        command: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = str(command or "").strip().lower()
        if command not in ALLOWED_HELPER_COMMANDS:
            return {"ok": False, "error": "command_not_allowed"}
        with self._lock:
            helper = self._helpers.get(session_id)
            if helper is None:
                return {"ok": False, "error": "helper_not_paired"}
            helper.last_seen = time.time()
            request = {
                "ok": True,
                "request_id": uuid.uuid4().hex,
                "command": command,
                "payload": dict(payload or {}),
            }
            if helper.sender is not None:
                helper.sender(request)
                request["queued"] = True
            else:
                if not helper.online:
                    request["queued"] = False
                    request["error"] = "helper_offline"
                else:
                    helper.queue.append(request)
                    request["queued"] = True
            return request
