from __future__ import annotations

import importlib
from typing import Any

from contracts import ModuleRegistration


class PredictionService:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.backend = importlib.import_module("online_inference")

    def catalog(self, schema_mode: str = "legacy_original") -> list[dict[str, Any]]:
        return list(self.backend.model_catalog(schema_mode))

    def inspect(self, path: str, schema_mode: str = "legacy_original") -> dict[str, Any]:
        return dict(self.backend.inspect_prediction_model(path, schema_mode=schema_mode))

    def runtime(self, **values: Any) -> Any:
        return self.backend.OnlineIModernTCN(**values)

    def health_check(self) -> dict[str, Any]:
        registry = dict(self.backend.MODEL_REGISTRY)
        model_root = self.context.paths.models_dir
        return {
            "ok": bool(registry),
            "registered_algorithms": sorted(registry),
            "model_root": str(model_root),
            "model_root_exists": model_root.exists(),
        }


def register(context: Any) -> ModuleRegistration:
    return ModuleRegistration(
        "prediction", "2.0.0", "2.0",
        ("model_catalog", "checkpoint_validation", "causal_forecast", "horizon_1_600"),
        PredictionService(context),
    )

