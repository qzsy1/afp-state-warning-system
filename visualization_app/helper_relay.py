"""Authorization, persistence, and command contracts for local capture helpers."""

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
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from public_ingress_state import PublicIngressState, load_public_ingress_state
from release_contract import (
    helper_capability_mismatches,
    load_release_contract,
)


MINIMUM_HELPER_PROTOCOL_VERSION = 2
REQUIRED_COMMAND_LIFECYCLE = "isolated_hardware_check"
RELEASE_CONTRACT = load_release_contract(start=__file__)


ALLOWED_HELPER_COMMANDS = frozenset(
    {
        "discover",
        "check_capture",
        "read_process_parameters",
        "mysql_preflight",
        "mysql_relation_map",
        "mysql_profile_save",
        "mysql_profile_status",
        "check_save_root",
        "select_folder",
        "prepare_simulation_source",
        "start_capture",
        "stop_capture",
        "esr_numeric",
        "status",
    }
)


def select_simulation_execution(helper_status: dict[str, Any] | None) -> dict[str, str]:
    """Choose a remote simulation host before capture starts."""

    status = helper_status if isinstance(helper_status, dict) else {}
    if not bool(status.get("online")):
        return {"execution_host": "server", "fallback_reason": "helper_offline"}
    if not bool(status.get("protocol_compatible")):
        return {
            "execution_host": "server",
            "fallback_reason": "helper_protocol_incompatible",
        }
    capabilities = status.get("capabilities")
    if not isinstance(capabilities, dict) or not bool(
        capabilities.get("simulation_replay_v1")
    ):
        return {
            "execution_host": "server",
            "fallback_reason": "helper_simulation_replay_unsupported",
        }
    return {"execution_host": "helper_local", "fallback_reason": ""}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_pairing_code(value: str) -> str:
    """Accept the raw code or the complete label copied from the webpage."""

    clean = str(value or "").strip()
    for prefix in ("配对码：", "配对码:", "Pairing code:", "Pairing code："):
        if clean.lower().startswith(prefix.lower()):
            return clean[len(prefix):].strip()
    return clean


def _normalize_server_url(value: Any) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return ""
        host = parsed.hostname.lower()
        port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
        path = parsed.path.rstrip("/")
        return urlunsplit(("https", host + port, path, parsed.query, ""))
    except (TypeError, ValueError):
        return ""


@dataclass
class _Pairing:
    session_id: str
    challenge_hash: str
    created_at: float
    expires_at: float
    generation: int


@dataclass
class _Helper:
    session_id: str
    device_id: str
    token_hash: str
    generation: int
    capabilities: dict[str, Any] = field(default_factory=dict)
    online: bool = False
    last_seen: float | None = None
    sender: Callable[[dict[str, Any]], None] | None = None
    queue: deque[dict[str, Any]] = field(default_factory=deque)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class _CommandRecord:
    request_id: str
    session_id: str
    command: str
    generation: int
    delivery_state: str
    terminal_error: str = ""


class HelperRegistry:
    """Authorize one helper per web session without persisting secrets."""

    def __init__(
        self,
        *,
        challenge_ttl_seconds: int = 300,
        heartbeat_ttl_seconds: int = 60,
        persistence_path: str | Path | None = None,
        ingress_state_path: str | Path | None = None,
        expected_candidate_id: str = "",
        ingress_state_loader: Callable[..., PublicIngressState] | None = None,
    ) -> None:
        self.challenge_ttl_seconds = max(30, int(challenge_ttl_seconds))
        self.heartbeat_ttl_seconds = max(1, int(heartbeat_ttl_seconds))
        self.persistence_path = Path(persistence_path).resolve() if persistence_path else None
        self.ingress_state_path = Path(ingress_state_path).resolve() if ingress_state_path else None
        self.cutover_reservation_path = (
            self.ingress_state_path.with_name("public-ingress-cutover.json")
            if self.ingress_state_path is not None
            else None
        )
        self.expected_candidate_id = str(expected_candidate_id or "")
        self.ingress_state_loader = ingress_state_loader or load_public_ingress_state
        self._lock = threading.RLock()
        self._pairings: dict[str, _Pairing] = {}
        self._helpers: dict[str, _Helper] = {}
        self._commands: dict[str, _CommandRecord] = {}
        self._terminal_results: dict[tuple[str, str], dict[str, Any]] = {}
        self._active_captures: dict[str, str] = {}
        self._ingress_state: PublicIngressState | None = None
        self._ingress_generation = 0
        self._ingress_error = ""
        self._persistence_error = ""
        self._cutover_reservation_id = ""
        self._cutover_reservation_deadline = 0.0
        self._capture_starts_in_progress = 0
        with self._lock:
            self._synchronize_ingress_locked()
            self._load_persisted_helpers_locked()

    def _terminalize_generation_locked(self, generation: int) -> None:
        terminal = {
            "ok": False,
            "error": "ingress_generation_changed",
            "terminal": True,
        }
        for record in self._commands.values():
            if record.generation <= generation and record.delivery_state not in {"terminal", "completed"}:
                record.delivery_state = "terminal"
                record.terminal_error = "ingress_generation_changed"
                self._terminal_results[(record.session_id, record.request_id)] = dict(terminal)
        self._pairings.clear()
        for helper in self._helpers.values():
            helper.sender = None
            helper.online = False
        self._helpers.clear()
        self._persist_helpers_locked()

    @contextmanager
    def _cutover_file_lock_locked(self):
        path = self.cutover_reservation_path
        if path is None:
            yield
            return
        lock_path = path.with_name(path.name + ".lock")
        deadline = time.monotonic() + 5.0
        descriptor: int | None = None
        while descriptor is None:
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 30.0:
                        lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("cutover reservation lock unavailable")
                time.sleep(0.025)
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _read_persisted_cutover_locked(self) -> dict[str, Any] | None:
        path = self.cutover_reservation_path
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            token_hash = str(payload.get("reservation_hash") or "")
            expires_at = float(payload.get("expires_at") or 0.0)
            if len(token_hash) != 64:
                raise ValueError("invalid reservation hash")
            if time.time() >= expires_at:
                return None
            return {"reservation_hash": token_hash, "expires_at": expires_at}
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            # An unreadable lease is treated as active so capture cannot start
            # while cutover ownership is uncertain.
            return {"reservation_hash": "", "expires_at": float("inf")}

    def _write_persisted_cutover_locked(
        self,
        reservation_id: str,
        expires_at: float,
    ) -> None:
        path = self.cutover_reservation_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "reservation_hash": _hash(reservation_id),
                        "expires_at": expires_at,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _cutover_reserved_locked(self) -> bool:
        persisted = self._read_persisted_cutover_locked()
        if persisted is not None:
            self._cutover_reservation_deadline = float(persisted["expires_at"])
            return True
        if self._cutover_reservation_id and time.time() >= self._cutover_reservation_deadline:
            self._cutover_reservation_id = ""
            self._cutover_reservation_deadline = 0.0
        return bool(self._cutover_reservation_id)

    def _owns_cutover_reservation_locked(self, reservation_id: str) -> bool:
        candidate = str(reservation_id or "")
        if not candidate:
            return False
        persisted = self._read_persisted_cutover_locked()
        if persisted is not None:
            token_hash = str(persisted.get("reservation_hash") or "")
            return bool(token_hash) and hmac.compare_digest(token_hash, _hash(candidate))
        return bool(self._cutover_reservation_id) and hmac.compare_digest(
            self._cutover_reservation_id,
            candidate,
        )

    def _synchronize_ingress_locked(self) -> bool:
        if self.ingress_state_path is None:
            self._ingress_error = ""
            self._ingress_generation = 0
            return True
        try:
            state = self.ingress_state_loader(
                self.ingress_state_path,
                self.expected_candidate_id,
            )
            if not isinstance(state, PublicIngressState):
                raise ValueError("ingress state loader returned an invalid value")
        except Exception as exc:
            self._ingress_error = str(exc) or exc.__class__.__name__
            return False

        previous = self._ingress_state
        if previous is not None:
            if state.generation < previous.generation:
                self._ingress_error = "public ingress generation rollback detected"
                return False
            if state.generation == previous.generation and (
                state.public_url != previous.public_url
                or state.provider != previous.provider
                or state.recovery_owner != previous.recovery_owner
                or state.candidate_id != previous.candidate_id
            ):
                self._ingress_error = "public ingress identity changed without a new generation"
                return False
            if state.generation > previous.generation:
                self._terminalize_generation_locked(previous.generation)
        self._ingress_state = state
        self._ingress_generation = state.generation
        self._ingress_error = ""
        return True

    def _load_persisted_helpers_locked(self) -> None:
        path = self.persistence_path
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("helper registry root must be an object")
            helpers = payload.get("helpers", {})
            if not isinstance(helpers, dict):
                raise ValueError("helper registry helpers must be an object")
            for session_id, item in helpers.items():
                if not str(session_id).strip() or not isinstance(item, dict):
                    raise ValueError("helper registry contains an invalid helper")
                device_id = str(item.get("device_id") or "").strip()
                token_hash = str(item.get("token_hash") or "").strip()
                try:
                    generation = int(item.get("generation", 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("helper registry contains an invalid generation") from exc
                if (
                    not device_id
                    or len(token_hash) != 64
                ):
                    raise ValueError("helper registry contains an invalid identity")
                if generation != self._ingress_generation or self._ingress_error:
                    continue
                capabilities = item.get("capabilities") or {}
                if not isinstance(capabilities, dict):
                    raise ValueError("helper registry contains invalid capabilities")
                self._helpers[str(session_id)] = _Helper(
                    session_id=str(session_id),
                    device_id=device_id,
                    token_hash=token_hash,
                    generation=generation,
                    capabilities=dict(capabilities),
                    online=False,
                    last_seen=None,
                )
            commands = payload.get("commands", {}) if isinstance(payload, dict) else {}
            if not isinstance(commands, dict):
                raise ValueError("helper registry commands must be an object")
            for request_id, item in commands.items():
                if not str(request_id).strip() or not isinstance(item, dict):
                    raise ValueError("helper registry contains an invalid command")
                try:
                    generation = int(item.get("generation", -1))
                except (TypeError, ValueError) as exc:
                    raise ValueError("helper registry contains an invalid command generation") from exc
                record = _CommandRecord(
                    request_id=str(request_id),
                    session_id=str(item.get("session_id") or ""),
                    command=str(item.get("command") or ""),
                    generation=generation,
                    delivery_state=str(item.get("delivery_state") or "terminal"),
                    terminal_error=str(item.get("terminal_error") or ""),
                )
                if not record.session_id or not record.command:
                    raise ValueError("helper registry contains an incomplete command")
                if record.delivery_state not in {"terminal", "completed"}:
                    previous_delivery_state = record.delivery_state
                    record.delivery_state = "terminal"
                    record.terminal_error = (
                        "ingress_generation_changed"
                        if generation != self._ingress_generation
                        else "command_state_lost"
                    )
                    if (
                        record.command == "start_capture"
                        and previous_delivery_state == "delivered"
                    ):
                        self._active_captures[
                            f"unconfirmed-{record.request_id}"
                        ] = record.session_id
                self._commands[record.request_id] = record
                if record.delivery_state == "terminal" and record.terminal_error:
                    self._terminal_results[(record.session_id, record.request_id)] = {
                        "ok": False,
                        "error": record.terminal_error,
                        "terminal": True,
                    }
            try:
                persisted_generation = int(payload.get("ingress_generation", -1))
            except (TypeError, ValueError) as exc:
                raise ValueError("helper registry ingress_generation is invalid") from exc
            active_captures = payload.get("active_captures", {})
            if not isinstance(active_captures, dict):
                raise ValueError("helper registry active_captures must be an object")
            if persisted_generation == self._ingress_generation:
                if any(
                    not str(capture_uuid).strip() or not str(session_id).strip()
                    for capture_uuid, session_id in active_captures.items()
                ):
                    raise ValueError("helper registry contains an invalid active capture")
                self._active_captures.update(
                    {
                        str(capture_uuid): str(session_id)
                        for capture_uuid, session_id in active_captures.items()
                        if str(capture_uuid).strip() and str(session_id).strip()
                    }
                )
            self._persist_helpers_locked()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._persistence_error = str(exc) or "helper registry unavailable"
            return

    def _persist_helpers_locked(self) -> None:
        path = self.persistence_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 2,
                "ingress_generation": self._ingress_generation,
                "helpers": {
                    session_id: {
                        "device_id": helper.device_id,
                        "token_hash": helper.token_hash,
                        "generation": helper.generation,
                        "capabilities": dict(helper.capabilities),
                    }
                    for session_id, helper in self._helpers.items()
                },
                "commands": {
                    request_id: {
                        "session_id": record.session_id,
                        "command": record.command,
                        "generation": record.generation,
                        "delivery_state": record.delivery_state,
                        "terminal_error": record.terminal_error,
                    }
                    for request_id, record in self._commands.items()
                },
                "active_captures": dict(self._active_captures),
            }
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, path)
            self._persistence_error = ""
        except (OSError, TypeError, ValueError) as exc:
            self._persistence_error = str(exc) or "helper registry unavailable"
            return

    def start_pairing(self, session_id: str) -> dict[str, Any]:
        challenge = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            if not self._synchronize_ingress_locked():
                return {"ok": False, "error": "ingress_state_unavailable"}
            self._pairings[session_id] = _Pairing(
                session_id=session_id,
                challenge_hash=_hash(challenge),
                created_at=now,
                expires_at=now + self.challenge_ttl_seconds,
                generation=self._ingress_generation,
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
            if not self._synchronize_ingress_locked():
                return {"ok": False, "error": "ingress_state_unavailable"}
            matched_session = None
            for session_id, pairing in self._pairings.items():
                if pairing.generation == self._ingress_generation and pairing.expires_at >= now and hmac.compare_digest(
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
                generation=self._ingress_generation,
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
                "ingress_generation": self._ingress_generation,
            }

    def attach(
        self,
        session_id: str,
        device_id: str,
        pairing_token: str,
        sender: Callable[[dict[str, Any]], None],
        capabilities: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock:
            if not self._synchronize_ingress_locked():
                return False
            helper = self._helpers.get(session_id)
            if (
                helper is None
                or helper.generation != self._ingress_generation
                or helper.device_id != str(device_id)
            ):
                return False
            if not hmac.compare_digest(helper.token_hash, _hash(pairing_token)):
                return False
            helper.sender = sender
            helper.online = True
            helper.last_seen = time.time()
            if isinstance(capabilities, dict):
                helper.capabilities = dict(capabilities)
                self._persist_helpers_locked()
            return True

    def detach(
        self,
        session_id: str,
        *,
        sender: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Detach only the connection that is actually closing.

        A reconnect can attach a new WebSocket before the old request handler
        reaches its ``finally`` block.  The stale handler must not clear the
        new sender or mark the replacement connection offline.
        """
        with self._lock:
            helper = self._helpers.get(session_id)
            if helper:
                if sender is not None and helper.sender is not sender:
                    return
                helper.online = False
                helper.sender = None

    def _authenticate_locked(self, device_id: str, pairing_token: str) -> str | None:
        for session_id, helper in self._helpers.items():
            if (
                helper.generation == self._ingress_generation
                and helper.device_id == str(device_id)
                and hmac.compare_digest(helper.token_hash, _hash(pairing_token))
            ):
                helper.online = True
                helper.last_seen = time.time()
                return session_id
        return None

    def authenticate(self, device_id: str, pairing_token: str) -> str | None:
        """Return the owning session for a helper bearer token."""
        with self._lock:
            if not self._synchronize_ingress_locked():
                return None
            return self._authenticate_locked(device_id, pairing_token)

    def poll(self, device_id: str, pairing_token: str) -> dict[str, Any]:
        with self._lock:
            if not self._synchronize_ingress_locked():
                return {"ok": False, "error": "ingress_state_unavailable"}
            session_id = self._authenticate_locked(device_id, pairing_token)
            if session_id is None:
                return {"ok": False, "error": "helper_authentication_failed"}
            helper = self._helpers[session_id]
            if helper.queue:
                command = helper.queue.popleft()
                record = self._commands.get(str(command.get("request_id") or ""))
                if record is None or record.generation != self._ingress_generation:
                    return {"ok": False, "error": "ingress_generation_changed"}
                record.delivery_state = "delivered"
                self._persist_helpers_locked()
                return {"ok": True, "command": command, "ingress_generation": self._ingress_generation}
            return {"ok": True, "command": None, "heartbeat": True, "ingress_generation": self._ingress_generation}

    def accept_result(
        self,
        device_id: str,
        pairing_token: str,
        request_id: str,
        payload: dict[str, Any],
        ingress_generation: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if not self._synchronize_ingress_locked():
                return {"ok": False, "error": "ingress_state_unavailable"}
            session_id = self._authenticate_locked(device_id, pairing_token)
            if session_id is None:
                return {"ok": False, "error": "helper_authentication_failed"}
            helper = self._helpers[session_id]
            record = self._commands.get(str(request_id))
            if record is None or record.session_id != session_id:
                return {"ok": False, "error": "helper_request_unknown"}
            if self.ingress_state_path is not None and ingress_generation is None:
                return {"ok": False, "error": "ingress_generation_mismatch"}
            try:
                received_generation = (
                    helper.generation
                    if ingress_generation is None
                    else int(ingress_generation)
                )
            except (TypeError, ValueError):
                return {"ok": False, "error": "ingress_generation_mismatch"}
            if received_generation != self._ingress_generation or record.generation != self._ingress_generation:
                return {"ok": False, "error": "ingress_generation_mismatch"}
            if record.delivery_state == "terminal":
                return {
                    "ok": False,
                    "error": record.terminal_error or "command_already_terminal",
                    "terminal": True,
                }
            if record.delivery_state == "completed":
                return {"ok": True, "request_id": str(request_id)}
            result = dict(payload)
            helper.results[str(request_id)] = result
            record.delivery_state = "completed"
            record.terminal_error = ""
            self._active_captures.pop(f"unconfirmed-{record.request_id}", None)
            capture_uuid = str(result.get("capture_uuid") or "").strip()
            start_state = str((result.get("start_operation") or {}).get("state") or "")
            starting = start_state in {"starting", "cancelling"}
            if record.command == "status" and isinstance(result.get("running"), bool):
                self._active_captures = {
                    capture_id: owner
                    for capture_id, owner in self._active_captures.items()
                    if owner != session_id
                }
            if record.command in {"start_capture", "status"} and (result.get("running") is True or starting) and capture_uuid:
                self._active_captures[capture_uuid] = session_id
            if result.get("running") is False and not starting and capture_uuid:
                self._active_captures.pop(capture_uuid, None)
            self._persist_helpers_locked()
        return {"ok": True, "request_id": str(request_id)}

    def pop_result(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._synchronize_ingress_locked()
            terminal = self._terminal_results.pop((session_id, str(request_id)), None)
            if terminal is not None:
                record = self._commands.get(str(request_id))
                if record is not None:
                    record.delivery_state = "completed"
                    record.terminal_error = ""
                    self._persist_helpers_locked()
                return terminal
            helper = self._helpers.get(session_id)
            if helper is None:
                return None
            return helper.results.pop(str(request_id), None)

    def status(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            ready = self._synchronize_ingress_locked()
            helper = self._helpers.get(session_id)
            if helper is None:
                return {
                    "paired": False,
                    "online": False,
                    "capabilities": {},
                    "protocol_compatible": False,
                    "ingress_generation": self._ingress_generation,
                    "ingress_state_ready": ready,
                    "ingress_error": "" if ready else "ingress_state_unavailable",
                }
            if helper.online and helper.last_seen is not None:
                if time.time() - helper.last_seen > self.heartbeat_ttl_seconds:
                    helper.online = False
                    helper.sender = None
            capabilities = dict(helper.capabilities)
            try:
                protocol_version = int(capabilities.get("protocol_version") or 0)
            except (TypeError, ValueError):
                protocol_version = 0
            protocol_compatible = (
                protocol_version >= MINIMUM_HELPER_PROTOCOL_VERSION
                and capabilities.get("command_lifecycle") == REQUIRED_COMMAND_LIFECYCLE
            )
            return {
                "paired": True,
                "online": bool(helper.online),
                "device_id": helper.device_id,
                "capabilities": capabilities,
                "protocol_version": protocol_version,
                "protocol_compatible": protocol_compatible,
                "last_seen": helper.last_seen,
                "ingress_generation": self._ingress_generation,
                "ingress_state_ready": ready,
                "ingress_error": "" if ready else "ingress_state_unavailable",
            }

    def cutover_readiness(self, reservation_id: str = "") -> dict[str, Any]:
        with self._lock:
            ready = self._synchronize_ingress_locked()
            blocking = sorted(
                record.request_id
                for record in self._commands.values()
                if record.command in {"start_capture", "stop_capture"}
                and record.delivery_state not in {"completed", "terminal"}
            )
            captures = sorted(self._active_captures)
            if self._capture_starts_in_progress:
                blocking.append("capture_start_in_progress")
            if self._persistence_error:
                blocking.append("helper_state_reconciliation_required")
            reservation_active = self._cutover_reserved_locked()
            owns_reservation = bool(
                reservation_active
                and self._owns_cutover_reservation_locked(reservation_id)
            )
            return {
                "allowed": bool(
                    ready
                    and not captures
                    and not blocking
                    and (not reservation_active or owns_reservation)
                ),
                "generation": self._ingress_generation,
                "active_capture_uuids": captures,
                "blocking_request_ids": blocking,
                "reservation_active": reservation_active,
            }

    def reserve_cutover(self, ttl_seconds: int = 180) -> dict[str, Any]:
        with self._lock:
            try:
                with self._cutover_file_lock_locked():
                    readiness = self.cutover_readiness()
                    if not readiness["allowed"]:
                        return {**readiness, "reservation_id": ""}
                    reservation_id = secrets.token_urlsafe(24)
                    deadline = time.time() + max(30, min(int(ttl_seconds), 600))
                    self._write_persisted_cutover_locked(reservation_id, deadline)
                    self._cutover_reservation_id = reservation_id
                    self._cutover_reservation_deadline = deadline
                    return {
                        **readiness,
                        "allowed": True,
                        "reservation_active": True,
                        "reservation_id": reservation_id,
                    }
            except (OSError, TimeoutError):
                readiness = self.cutover_readiness()
                return {
                    **readiness,
                    "allowed": False,
                    "reservation_active": True,
                    "reservation_id": "",
                    "error": "cutover_reservation_unavailable",
                }

    def release_cutover(self, reservation_id: str) -> bool:
        with self._lock:
            try:
                with self._cutover_file_lock_locked():
                    if not self._cutover_reserved_locked():
                        return False
                    if not self._owns_cutover_reservation_locked(reservation_id):
                        return False
                    if self.cutover_reservation_path is not None:
                        try:
                            self.cutover_reservation_path.unlink()
                        except FileNotFoundError:
                            return False
                    self._cutover_reservation_id = ""
                    self._cutover_reservation_deadline = 0.0
                    return True
            except (OSError, TimeoutError):
                return False

    def cutover_reserved(self) -> bool:
        with self._lock:
            return self._cutover_reserved_locked()

    def begin_capture_start(
        self,
        *,
        require_ingress_ready: bool = True,
        require_cutover_clear: bool = True,
    ) -> bool:
        with self._lock:
            if require_cutover_clear and self._cutover_reserved_locked():
                return False
            if require_ingress_ready and (
                not self._synchronize_ingress_locked()
                or bool(self._persistence_error)
                or (
                    self.ingress_state_path is not None
                    and (
                        self._ingress_state is None
                        or self._ingress_state.status != "online"
                    )
                )
            ):
                return False
            self._capture_starts_in_progress += 1
            return True

    def end_capture_start(self) -> None:
        with self._lock:
            self._capture_starts_in_progress = max(
                0, self._capture_starts_in_progress - 1
            )

    def deployment_readiness(self) -> dict[str, Any]:
        with self._lock:
            state_ready = self._synchronize_ingress_locked()
            now = time.time()
            paired = 0
            online = 0
            compatible = 0
            route_mismatches = 0
            expected_route = _normalize_server_url(
                self._ingress_state.public_url if self._ingress_state else ""
            )
            for helper in self._helpers.values():
                if helper.generation != self._ingress_generation:
                    continue
                paired += 1
                is_online = bool(
                    helper.online
                    and helper.last_seen is not None
                    and now - helper.last_seen <= self.heartbeat_ttl_seconds
                )
                if not is_online:
                    continue
                online += 1
                helper_route = _normalize_server_url(
                    helper.capabilities.get("paired_server_url")
                )
                route_matches = bool(
                    not self.ingress_state_path
                    or (expected_route and helper_route == expected_route)
                )
                if not route_matches:
                    route_mismatches += 1
                if (
                    route_matches
                    and helper.capabilities.get("command_lifecycle")
                    == REQUIRED_COMMAND_LIFECYCLE
                    and not helper_capability_mismatches(
                        RELEASE_CONTRACT, helper.capabilities
                    )
                ):
                    compatible += 1
            return {
                "ready": bool(
                    state_ready and not self._persistence_error and compatible > 0
                ),
                "generation": self._ingress_generation,
                "candidate_id": self.expected_candidate_id,
                "paired_helpers": paired,
                "online_helpers": online,
                "compatible_helpers": compatible,
                "non_sensitive_error": (
                    ""
                    if state_ready and not self._persistence_error and compatible > 0
                    else "helper_state_reconciliation_required"
                    if self._persistence_error
                    else "helper_route_mismatch"
                    if state_ready and route_mismatches > 0
                    else "helper_not_ready"
                    if state_ready
                    else "ingress_state_unavailable"
                ),
            }

    def authorize_generation(self, session_id: str, generation: Any) -> bool:
        with self._lock:
            if not self._synchronize_ingress_locked():
                return False
            helper = self._helpers.get(str(session_id))
            try:
                supplied = int(generation)
            except (TypeError, ValueError):
                supplied = 0 if self.ingress_state_path is None else -1
            return bool(
                helper is not None
                and helper.generation == self._ingress_generation
                and supplied == self._ingress_generation
            )

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
            if not self._synchronize_ingress_locked():
                return {"ok": False, "queued": False, "error": "ingress_state_unavailable"}
            if command == "start_capture":
                if self._persistence_error:
                    return {
                        "ok": False,
                        "queued": False,
                        "error": "helper_state_reconciliation_required",
                    }
                if self._cutover_reserved_locked():
                    return {"ok": False, "queued": False, "error": "ingress_cutover_in_progress"}
                if (
                    self.ingress_state_path is not None
                    and (
                        self._ingress_state is None
                        or self._ingress_state.status != "online"
                    )
                ):
                    return {"ok": False, "queued": False, "error": "ingress_not_online"}
            helper = self._helpers.get(session_id)
            if helper is None or helper.generation != self._ingress_generation:
                return {"ok": False, "error": "helper_not_paired"}
            if helper.online and helper.last_seen is not None:
                if time.time() - helper.last_seen > self.heartbeat_ttl_seconds:
                    helper.online = False
                    helper.sender = None
            if not helper.online:
                return {"ok": False, "queued": False, "error": "helper_offline"}
            if command == "start_capture":
                mismatches = helper_capability_mismatches(
                    RELEASE_CONTRACT, helper.capabilities
                )
                if mismatches:
                    return {
                        "ok": False,
                        "queued": False,
                        "error": "release_contract_mismatch",
                        "mismatches": list(mismatches),
                    }
            helper.last_seen = time.time()
            request = {
                "type": "command",
                "ok": True,
                "request_id": uuid.uuid4().hex,
                "command": command,
                "payload": dict(payload or {}),
                "ingress_generation": self._ingress_generation,
            }
            record = _CommandRecord(
                request_id=request["request_id"],
                session_id=session_id,
                command=command,
                generation=self._ingress_generation,
                delivery_state="queued",
            )
            self._commands[record.request_id] = record
            if helper.sender is not None:
                sender = helper.sender
                try:
                    sender(request)
                    request["queued"] = True
                    record.delivery_state = "delivered"
                except (ConnectionError, OSError, TimeoutError):
                    if helper.sender is sender:
                        helper.sender = None
                        helper.online = False
                    request["ok"] = False
                    request["queued"] = False
                    request["error"] = "helper_transport_send_failed"
                    record.delivery_state = "terminal"
                    record.terminal_error = "helper_transport_send_failed"
            else:
                if not helper.online:
                    request["queued"] = False
                    request["error"] = "helper_offline"
                else:
                    helper.queue.append(request)
                    request["queued"] = True
            self._persist_helpers_locked()
            return request
