"""In-memory authorization and command contract for local capture helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


ALLOWED_HELPER_COMMANDS = frozenset(
    {"discover", "check_capture", "mysql_preflight", "mysql_relation_map", "start_capture", "stop_capture", "status"}
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_pairing_code(value: str) -> str:
    """Accept the raw code or the complete label copied from the webpage."""

    clean = str(value or "").strip()
    for prefix in ("配对码：", "配对码:", "Pairing code:", "Pairing code："):
        if clean.lower().startswith(prefix.lower()):
            return clean[len(prefix):].strip()
    return clean


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

    def __init__(
        self,
        *,
        challenge_ttl_seconds: int = 300,
        heartbeat_ttl_seconds: int = 60,
        persistence_path: str | Path | None = None,
    ) -> None:
        self.challenge_ttl_seconds = max(30, int(challenge_ttl_seconds))
        self.heartbeat_ttl_seconds = max(1, int(heartbeat_ttl_seconds))
        self.persistence_path = Path(persistence_path).resolve() if persistence_path else None
        self._lock = threading.RLock()
        self._pairings: dict[str, _Pairing] = {}
        self._helpers: dict[str, _Helper] = {}
        self._load_persisted_helpers()

    def _load_persisted_helpers(self) -> None:
        path = self.persistence_path
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            helpers = payload.get("helpers", {}) if isinstance(payload, dict) else {}
            if not isinstance(helpers, dict):
                return
            with self._lock:
                for session_id, item in helpers.items():
                    if not isinstance(item, dict):
                        continue
                    device_id = str(item.get("device_id") or "").strip()
                    token_hash = str(item.get("token_hash") or "").strip()
                    if not device_id or len(token_hash) != 64:
                        continue
                    self._helpers[str(session_id)] = _Helper(
                        session_id=str(session_id),
                        device_id=device_id,
                        token_hash=token_hash,
                        capabilities=dict(item.get("capabilities") or {}),
                        online=False,
                        last_seen=None,
                    )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _persist_helpers_locked(self) -> None:
        path = self.persistence_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "helpers": {
                    session_id: {
                        "device_id": helper.device_id,
                        "token_hash": helper.token_hash,
                        "capabilities": dict(helper.capabilities),
                    }
                    for session_id, helper in self._helpers.items()
                },
            }
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError:
            # Pairing remains valid for the running process; a filesystem
            # failure must not interrupt an active acquisition session.
            return

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
        challenge = normalize_pairing_code(challenge)
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
            self._persist_helpers_locked()
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
            if helper.online and helper.last_seen is not None:
                if time.time() - helper.last_seen > self.heartbeat_ttl_seconds:
                    helper.online = False
                    helper.sender = None
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
            if helper.online and helper.last_seen is not None:
                if time.time() - helper.last_seen > self.heartbeat_ttl_seconds:
                    helper.online = False
                    helper.sender = None
            if not helper.online:
                return {"ok": False, "queued": False, "error": "helper_offline"}
            helper.last_seen = time.time()
            request = {
                "type": "command",
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
