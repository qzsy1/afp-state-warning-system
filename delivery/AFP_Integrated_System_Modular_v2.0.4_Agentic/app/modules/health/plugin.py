from __future__ import annotations

import importlib
from typing import Any

from contracts import ModuleRegistration


class HealthService:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.new_backend = importlib.import_module("new_collection_health")
        self.online_backend = importlib.import_module("online_health_features")

    def new_engine(self, artifact_path: str | None = None) -> Any:
        return self.new_backend.NewCollectionHealthEngine(artifact_path) if artifact_path else self.new_backend.NewCollectionHealthEngine()

    def online_features(self, *args: Any, **kwargs: Any) -> Any:
        return self.online_backend.OnlineWindowFeatureEngine(*args, **kwargs)

    def health_check(self) -> dict[str, Any]:
        artifact = self.new_backend.DEFAULT_ARTIFACT
        return {
            "ok": hasattr(self.new_backend, "NewCollectionHealthEngine"),
            "new_schema_artifact": str(artifact),
            "new_schema_artifact_exists": artifact.exists(),
            "sensor_count": len(self.new_backend.SENSOR_COLUMNS),
        }


def register(context: Any) -> ModuleRegistration:
    capabilities = ("T-HI", "C-HI", "TC-HI", "RFHI", "PR-HI", "MPRF-HI", "PCA-SPE-HI", "KECA-SPE-HI")
    return ModuleRegistration("health", "2.0.0", "2.0", capabilities, HealthService(context))

