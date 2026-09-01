from __future__ import annotations

import importlib
from typing import Any

from contracts import ModuleRegistration


class StorageService:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.backend = importlib.import_module("mysql_storage")

    def settings(self, values: dict[str, Any]) -> Any:
        return self.backend.mysql_settings_from_mapping(values)

    def store(self, values: dict[str, Any]) -> Any:
        return self.backend.MySQLCaptureStore(self.settings(values))

    def health_check(self) -> dict[str, Any]:
        drivers: list[str] = []
        for name in ("mysql.connector", "pymysql"):
            try:
                importlib.import_module(name)
                drivers.append(name)
            except ImportError:
                pass
        return {
            "ok": bool(drivers),
            "available_python_drivers": drivers,
            "note": "health check does not open a user database connection",
        }


def register(context: Any) -> ModuleRegistration:
    return ModuleRegistration("storage", "2.0.0", "2.0", ("csv", "mysql", "foreign_keys", "flat_views"), StorageService(context))

