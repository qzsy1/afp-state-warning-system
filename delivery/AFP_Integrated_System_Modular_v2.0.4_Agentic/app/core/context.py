from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    app_dir: Path
    legacy_dir: Path
    modules_dir: Path
    ui_dir: Path
    data_dir: Path
    models_dir: Path
    native_dll_dir: Path
    config_dir: Path
    logs_dir: Path
    updates_dir: Path
    rollback_dir: Path
    runtime_dir: Path


class RuntimeContext:
    def __init__(self, root: Path, config: dict[str, Any]) -> None:
        self.root = root.resolve()
        self.config = config
        self.paths = self._make_paths(config.get("paths", {}))
        self.services: dict[str, Any] = {}
        self.module_manager: Any = None

    @classmethod
    def load(cls, root: Path) -> "RuntimeContext":
        root = root.resolve()
        config_path = root / "config" / "runtime.json"
        if not config_path.exists():
            raise FileNotFoundError(f"runtime configuration is missing: {config_path}")
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        return cls(root, config)

    def _resolve(self, value: str, default: str) -> Path:
        path = Path(value or default).expanduser()
        return (self.root / path).resolve() if not path.is_absolute() else path.resolve()

    def _make_paths(self, values: dict[str, Any]) -> RuntimePaths:
        app_dir = self._resolve(values.get("app_dir", "app"), "app")
        return RuntimePaths(
            root=self.root,
            app_dir=app_dir,
            legacy_dir=self._resolve(values.get("legacy_dir", "app/legacy"), "app/legacy"),
            modules_dir=self._resolve(values.get("modules_dir", "app/modules"), "app/modules"),
            ui_dir=self._resolve(values.get("ui_dir", "app/ui"), "app/ui"),
            data_dir=self._resolve(values.get("data_dir", "app/legacy/data"), "app/legacy/data"),
            models_dir=self._resolve(values.get("models_dir", "models"), "models"),
            native_dll_dir=self._resolve(values.get("native_dll_dir", "native_dll"), "native_dll"),
            config_dir=self._resolve(values.get("config_dir", "config"), "config"),
            logs_dir=self._resolve(values.get("logs_dir", "logs"), "logs"),
            updates_dir=self._resolve(values.get("updates_dir", "updates"), "updates"),
            rollback_dir=self._resolve(values.get("rollback_dir", "rollback"), "rollback"),
            runtime_dir=self._resolve(values.get("runtime_dir", "runtime"), "runtime"),
        )

    def prepare(self) -> None:
        for path in (
            self.paths.logs_dir,
            self.paths.updates_dir,
            self.paths.rollback_dir,
            self.paths.runtime_dir,
            self.paths.models_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def export_environment(self) -> None:
        values = {
            "AFP_MODULAR_ROOT": self.root,
            "AFP_LEGACY_APP_DIR": self.paths.legacy_dir,
            "AFP_UI_DIR": self.paths.ui_dir,
            "AFP_DATA_DIR": self.paths.data_dir,
            "AFP_MODELS_DIR": self.paths.models_dir,
            "AFP_NATIVE_DLL_DIR": self.paths.native_dll_dir,
            "AFP_RUNTIME_DIR": self.paths.runtime_dir,
            "AFP_NEW_DEMO_DIR": self.paths.legacy_dir / "new_collection_demo_v11_3",
            "AFP_NEW_HEALTH_ARTIFACT": (
                self.paths.legacy_dir
                / "new_collection_demo_v11_3"
                / "models"
                / "new_collection_hi_artifacts.joblib"
            ),
        }
        for name, value in values.items():
            os.environ[name] = str(value)

    def register_service(self, module_id: str, service: Any) -> None:
        if module_id in self.services:
            raise ValueError(f"service already registered: {module_id}")
        self.services[module_id] = service

    def service(self, module_id: str) -> Any:
        if module_id not in self.services:
            raise KeyError(f"service is not loaded: {module_id}")
        return self.services[module_id]
