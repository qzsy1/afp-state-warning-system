"""Server-only target MySQL profile resolution for authorized sessions."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from mysql_storage import MySQLSettings, mysql_settings_from_mapping


@dataclass(frozen=True)
class TargetSelection:
    config_id: str
    settings: MySQLSettings
    source: str

    def public(self) -> dict[str, Any]:
        return {
            "config_id": self.config_id,
            "scope": "server_target",
            "execution_host": "server",
            "host": self.settings.host,
            "port": self.settings.port,
            "user": self.settings.user,
            "database": self.settings.database,
            "credential_source": self.source,
        }


class ServerTargetProfiles:
    """Keep explicit target secrets in server memory, never in a client identifier."""

    def __init__(self, profile_loader: Callable[[], dict], *, ttl_seconds: int = 3600) -> None:
        self._profile_loader = profile_loader
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._selections: dict[tuple[str, str], tuple[float, TargetSelection]] = {}

    def resolve(self, session_id: str, payload: dict[str, Any]) -> TargetSelection:
        if not str(session_id).strip():
            raise ValueError("目标 MySQL 要求已授权会话")
        requested = mysql_settings_from_mapping({**payload, "mysql_enabled": True})
        stored = self._profile_loader().get("target") or {}
        exact = (
            str(stored.get("host") or "").strip() == requested.host
            and int(stored.get("port") or 3306) == requested.port
            and str(stored.get("user") or "").strip() == requested.user
            and str(stored.get("database") or "").strip() == requested.database
        )
        explicit = str(payload.get("mysql_password") or "")
        if explicit:
            password, source = explicit, "explicit"
        elif exact and stored.get("password"):
            password, source = str(stored["password"]), "server_profile"
        else:
            raise ValueError("目标 MySQL 无匹配已存凭据，请输入此目标的密码后重新预检")
        settings = MySQLSettings(**{**requested.__dict__, "password": password})
        selection = TargetSelection(secrets.token_urlsafe(24), settings, source)
        with self._lock:
            self._selections[(session_id, selection.config_id)] = (time.monotonic(), selection)
        return selection

    def resolve_id(self, session_id: str, config_id: str) -> TargetSelection:
        with self._lock:
            entry = self._selections.get((session_id, config_id))
            if not entry or time.monotonic() - entry[0] > self._ttl:
                raise ValueError("目标 MySQL 配置已失效，请重新预检")
            return entry[1]

    def for_request(self, session_id: str, payload: dict[str, Any]) -> TargetSelection:
        config_id = str(payload.get("mysql_target_config_id") or "")
        if not config_id:
            return self.resolve(session_id, payload)
        selection = self.resolve_id(session_id, config_id)
        requested = mysql_settings_from_mapping({**payload, "mysql_enabled": True})
        current = selection.settings
        if (requested.host, requested.port, requested.user, requested.database) != (
            current.host, current.port, current.user, current.database
        ) or (requested.password and requested.password != current.password):
            raise ValueError("目标 MySQL 配置已修改，请重新预检")
        return selection
