from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol


PBKDF2_ITERATIONS = 600_000
SESSION_TOKEN_BYTES = 32
MINIMUM_PASSWORD_LENGTH = 12


class AuthenticationError(ValueError):
    """Raised when owner authentication cannot be completed."""


class SecretProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDpapiProtector:
    """Protect secrets for the current Windows user without extra packages."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    @staticmethod
    def _configure_function(function) -> None:
        """Bind this module's DATA_BLOB type before every DPAPI call.

        ``ctypes.windll`` caches function objects process-wide.  The packaged
        local helper defines its own equivalent structure, so leaving argtypes
        from that module in place can make a later web-auth call reject a
        perfectly valid blob solely because the Python class identity differs.
        """
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL

    @staticmethod
    def _input_blob(value: bytes) -> tuple[_DataBlob, Any]:
        raw = bytes(value)
        buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        blob = _DataBlob(
            len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        )
        return blob, buffer

    @staticmethod
    def _consume_output(blob: _DataBlob) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob.pbData)

    def protect(self, value: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI is available only on Windows")
        input_blob, input_buffer = self._input_blob(value)
        output_blob = _DataBlob()
        crypt_protect = ctypes.windll.crypt32.CryptProtectData
        self._configure_function(crypt_protect)
        ok = crypt_protect(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        del input_buffer
        if not ok:
            raise ctypes.WinError()
        return self._consume_output(output_blob)

    def unprotect(self, value: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI is available only on Windows")
        input_blob, input_buffer = self._input_blob(value)
        output_blob = _DataBlob()
        crypt_unprotect = ctypes.windll.crypt32.CryptUnprotectData
        self._configure_function(crypt_unprotect)
        ok = crypt_unprotect(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        del input_buffer
        if not ok:
            raise ctypes.WinError()
        return self._consume_output(output_blob)


class PasswordHasher:
    @staticmethod
    def hash_password(password: str) -> str:
        clean = str(password)
        if len(clean) < MINIMUM_PASSWORD_LENGTH:
            raise ValueError(f"密码至少需要 {MINIMUM_PASSWORD_LENGTH} 个字符")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", clean.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        return "pbkdf2_sha256$%d$%s$%s" % (
            PBKDF2_ITERATIONS,
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, count, salt_text, digest_text = str(encoded).split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
            actual = hashlib.pbkdf2_hmac(
                "sha256", str(password).encode("utf-8"), salt, int(count)
            )
        except (TypeError, ValueError):
            return False
        return hmac.compare_digest(actual, expected)


@dataclass(frozen=True, slots=True)
class AuthSession:
    session_id: str
    created_at: float
    last_seen_at: float
    user_agent: str
    remote_label: str


class SecurityStore:
    """Persistent owner credentials and revocable sessions.

    Server session records intentionally contain no expiry column. Browser
    retention is separate and a user can always revoke tokens from the local
    administration surface.
    """

    def __init__(self, path: Path, protector: SecretProtector | None = None) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.protector = protector or WindowsDpapiProtector()
        self._lock = threading.RLock()
        self._initialize()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner_settings (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    password_hash TEXT NOT NULL,
                    protected_api_key BLOB NOT NULL,
                    model_name TEXT NOT NULL,
                    auth_version INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    auth_version INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    user_agent TEXT NOT NULL,
                    remote_label TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    session_id TEXT,
                    remote_label TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    details_json TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def append_audit(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        remote_label: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        safe_details = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_type, session_id, remote_label, created_at, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(event_type),
                    str(session_id) if session_id else None,
                    str(remote_label)[:200],
                    time.time(),
                    safe_details,
                ),
            )

    def configure_owner(self, password: str, api_key: str, model_name: str) -> None:
        password_hash = PasswordHasher.hash_password(password)
        clean_model = str(model_name or "").strip()
        if not clean_model:
            raise ValueError("模型名称不能为空")
        protected_key = (
            self.protector.protect(str(api_key).encode("utf-8")) if api_key else b""
        )
        now = time.time()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT auth_version FROM owner_settings WHERE singleton = 1"
            ).fetchone()
            auth_version = (int(row["auth_version"]) if row else 0) + 1
            connection.execute(
                """
                INSERT INTO owner_settings
                    (singleton, password_hash, protected_api_key, model_name,
                     auth_version, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    protected_api_key = excluded.protected_api_key,
                    model_name = excluded.model_name,
                    auth_version = excluded.auth_version,
                    updated_at = excluded.updated_at
                """,
                (password_hash, protected_key, clean_model, auth_version, now),
            )
            connection.execute("UPDATE sessions SET revoked = 1 WHERE revoked = 0")
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_type, session_id, remote_label, created_at, details_json)
                VALUES ('security_configured', NULL, 'local-admin', ?, ?)
                """,
                (now, json.dumps({"model_name": clean_model}, ensure_ascii=False)),
            )

    def authenticate(
        self, password: str, user_agent: str, remote_label: str
    ) -> tuple[str, AuthSession]:
        with self._lock, self._connect() as connection:
            owner = connection.execute(
                "SELECT password_hash, auth_version FROM owner_settings WHERE singleton = 1"
            ).fetchone()
            if owner is None or not PasswordHasher.verify_password(
                password, owner["password_hash"]
            ):
                connection.execute(
                    """
                    INSERT INTO audit_events
                        (event_type, session_id, remote_label, created_at, details_json)
                    VALUES ('login_failure', NULL, ?, ?, '{}')
                    """,
                    (str(remote_label)[:200], time.time()),
                )
                raise AuthenticationError("密码错误或公网授权尚未配置")

            token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
            session_id = uuid.uuid4().hex
            now = time.time()
            session = AuthSession(
                session_id=session_id,
                created_at=now,
                last_seen_at=now,
                user_agent=str(user_agent)[:500],
                remote_label=str(remote_label)[:200],
            )
            connection.execute(
                """
                INSERT INTO sessions
                    (session_id, token_hash, auth_version, created_at, last_seen_at,
                     user_agent, remote_label, revoked)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    session.session_id,
                    self._token_hash(token),
                    int(owner["auth_version"]),
                    session.created_at,
                    session.last_seen_at,
                    session.user_agent,
                    session.remote_label,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_type, session_id, remote_label, created_at, details_json)
                VALUES ('login_success', ?, ?, ?, '{}')
                """,
                (session.session_id, session.remote_label, now),
            )
        return token, session

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> AuthSession:
        return AuthSession(
            session_id=str(row["session_id"]),
            created_at=float(row["created_at"]),
            last_seen_at=float(row["last_seen_at"]),
            user_agent=str(row["user_agent"]),
            remote_label=str(row["remote_label"]),
        )

    def resolve_session(self, token: str) -> AuthSession | None:
        if not token:
            return None
        token_hash = self._token_hash(token)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.* FROM sessions AS s
                JOIN owner_settings AS o ON o.singleton = 1
                WHERE s.token_hash = ? AND s.revoked = 0
                  AND s.auth_version = o.auth_version
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            connection.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE session_id = ?",
                (now, row["session_id"]),
            )
            refreshed = dict(row)
            refreshed["last_seen_at"] = now
            return self._session_from_row(refreshed)  # type: ignore[arg-type]

    def logout(self, token: str) -> None:
        if not token:
            return
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT session_id, remote_label FROM sessions WHERE token_hash = ?",
                (self._token_hash(token),),
            ).fetchone()
            connection.execute(
                "UPDATE sessions SET revoked = 1 WHERE token_hash = ?",
                (self._token_hash(token),),
            )
            if row:
                connection.execute(
                    """
                    INSERT INTO audit_events
                        (event_type, session_id, remote_label, created_at, details_json)
                    VALUES ('logout', ?, ?, ?, '{}')
                    """,
                    (row["session_id"], row["remote_label"], time.time()),
                )

    def revoke(self, session_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET revoked = 1 WHERE session_id = ?",
                (str(session_id),),
            )
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_type, session_id, remote_label, created_at, details_json)
                VALUES ('session_revoked', ?, 'local-admin', ?, '{}')
                """,
                (str(session_id), time.time()),
            )

    def revoke_all(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("UPDATE sessions SET revoked = 1 WHERE revoked = 0")
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_type, session_id, remote_label, created_at, details_json)
                VALUES ('all_sessions_revoked', NULL, 'local-admin', ?, '{}')
                """,
                (time.time(),),
            )

    def model_credentials(self) -> tuple[str, str]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT protected_api_key, model_name
                FROM owner_settings WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            return "", ""
        protected = bytes(row["protected_api_key"] or b"")
        api_key = self.protector.unprotect(protected).decode("utf-8") if protected else ""
        return api_key, str(row["model_name"])

    def safe_status(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            owner = connection.execute(
                """
                SELECT model_name, protected_api_key, auth_version
                FROM owner_settings WHERE singleton = 1
                """
            ).fetchone()
            sessions = []
            if owner is not None:
                rows = connection.execute(
                    """
                    SELECT session_id, created_at, last_seen_at, user_agent, remote_label
                    FROM sessions
                    WHERE revoked = 0 AND auth_version = ?
                    ORDER BY created_at DESC
                    """,
                    (int(owner["auth_version"]),),
                ).fetchall()
                sessions = [
                    {
                        "session_id": str(row["session_id"]),
                        "created_at": float(row["created_at"]),
                        "last_seen_at": float(row["last_seen_at"]),
                        "user_agent": str(row["user_agent"]),
                        "remote_label": str(row["remote_label"]),
                    }
                    for row in rows
                ]
            return {
                "configured": owner is not None,
                "model_configured": bool(
                    owner is not None and bytes(owner["protected_api_key"] or b"")
                ),
                "model_name": str(owner["model_name"]) if owner else "",
                "authorized_sessions": sessions,
            }
