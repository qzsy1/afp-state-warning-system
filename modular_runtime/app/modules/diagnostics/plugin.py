from __future__ import annotations

from typing import Any

from contracts import ModuleRegistration


class DiagnosticsService:
    def __init__(self, context: Any) -> None:
        self.context = context

    def report(self) -> dict[str, Any]:
        manager = self.context.module_manager
        return manager.status() if manager is not None else {"all_healthy": False, "modules": {}}

    def health_check(self) -> dict[str, Any]:
        return {"ok": True, "runtime_root": str(self.context.root)}


def register(context: Any) -> ModuleRegistration:
    return ModuleRegistration("diagnostics", "2.0.0", "2.0", ("module_health", "compatibility_report", "runtime_status"), DiagnosticsService(context))

