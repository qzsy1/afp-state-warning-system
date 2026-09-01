from __future__ import annotations

import importlib
from typing import Any

from contracts import ModuleRegistration


class TrainingService:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.manager_module = importlib.import_module("web_training")
        self.pipeline = importlib.import_module("web_training_pipeline")

    def manager(self) -> Any:
        return self.manager_module.WebTrainingManager()

    def default_columns(self, data_mode: str) -> dict[str, Any]:
        return dict(self.pipeline.default_columns(data_mode))

    def health_check(self) -> dict[str, Any]:
        return {
            "ok": hasattr(self.manager_module, "WebTrainingManager"),
            "data_modes": ["old", "new", "custom"],
            "training_types": ["prediction", "prediction_warning"],
        }


def register(context: Any) -> ModuleRegistration:
    return ModuleRegistration("training", "2.0.0", "2.0", ("csv_import", "mysql_import", "prediction_training", "warning_training", "stop_and_save"), TrainingService(context))

