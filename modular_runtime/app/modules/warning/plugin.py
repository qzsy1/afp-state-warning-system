from __future__ import annotations

import importlib
from typing import Any

from contracts import ModuleRegistration


class WarningService:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.causal = importlib.import_module("causal_online_runtime")
        self.primitives = importlib.import_module("runtime_health_primitives")

    def causal_consistency(self, *args: Any, **kwargs: Any) -> Any:
        return self.causal.CausalOnlineConsistency(*args, **kwargs)

    def health_check(self) -> dict[str, Any]:
        return {
            "ok": hasattr(self.causal, "CausalOnlineConsistency"),
            "levels": ["window", "layer", "specimen"],
            "causal_online": True,
        }


def register(context: Any) -> ModuleRegistration:
    return ModuleRegistration("warning", "2.0.0", "2.0", ("window", "layer", "specimen", "causal_online", "abnormal_type"), WarningService(context))

