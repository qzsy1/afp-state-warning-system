from __future__ import annotations

import importlib
from typing import Any

from contracts import ModuleRegistration


class DataIntegrationService:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.backend = importlib.import_module("acquisition")

    def integrate(self, *args: Any, **kwargs: Any) -> Any:
        return self.backend.integrate_capture_sources(*args, **kwargs)

    def health_check(self) -> dict[str, Any]:
        return {"ok": callable(getattr(self.backend, "integrate_capture_sources", None)), "sources": ["folder", "mysql"]}


def register(context: Any) -> ModuleRegistration:
    return ModuleRegistration("data_integration", "2.0.0", "2.0", ("capture_folder_to_csv", "mysql_to_csv", "schema_normalization"), DataIntegrationService(context))

