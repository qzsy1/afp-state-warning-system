from __future__ import annotations

import importlib
from typing import Any

from contracts import ModuleRegistration


class AcquisitionService:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.backend = importlib.import_module("acquisition")

    @property
    def schemas(self) -> dict[str, Any]:
        return dict(self.backend.ACQUISITION_SCHEMAS)

    def config(self, **values: Any) -> Any:
        return self.backend.AcquisitionConfig(**values)

    def manager(self, *args: Any, **kwargs: Any) -> Any:
        return self.backend.AcquisitionManager(*args, **kwargs)

    def health_check(self) -> dict[str, Any]:
        schemas = self.schemas
        return {
            "ok": bool(schemas) and hasattr(self.backend, "AcquisitionManager"),
            "schemas": sorted(schemas),
            "supports_multi_interface": hasattr(self.backend, "MultiInterfaceDriver"),
        }


def register(context: Any) -> ModuleRegistration:
    return ModuleRegistration(
        "acquisition", "2.1.1", "2.0",
        (
            "real_capture", "simulation_replay", "multi_interface", "layer_save",
            "capture_identity", "atomic_save", "quality_summary",
            "transactional_replace",
        ),
        AcquisitionService(context),
    )
