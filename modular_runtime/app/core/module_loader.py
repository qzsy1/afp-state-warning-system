from __future__ import annotations

import importlib.util
import json
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from contracts import HealthCheckable, ModuleRegistration


@dataclass(slots=True)
class LoadedModule:
    manifest: dict[str, Any]
    python_module: ModuleType
    registration: ModuleRegistration
    path: Path


class ModuleManager:
    def __init__(self, context: Any, api_version: str) -> None:
        self.context = context
        self.api_version = api_version
        self._loaded: dict[str, LoadedModule] = {}
        self._lock = threading.RLock()

    def discover(self) -> dict[str, tuple[dict[str, Any], Path]]:
        found: dict[str, tuple[dict[str, Any], Path]] = {}
        for manifest_path in sorted(self.context.paths.modules_dir.glob("*/module.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            module_id = str(manifest.get("id", "")).strip()
            if not module_id:
                raise ValueError(f"module id is missing: {manifest_path}")
            if module_id in found:
                raise ValueError(f"duplicate module id: {module_id}")
            found[module_id] = (manifest, manifest_path.parent)
        return found

    @staticmethod
    def _ordered(discovered: dict[str, tuple[dict[str, Any], Path]]) -> list[str]:
        visiting: set[str] = set()
        visited: set[str] = set()
        order: list[str] = []

        def visit(module_id: str) -> None:
            if module_id in visited:
                return
            if module_id in visiting:
                raise ValueError(f"cyclic module dependency includes: {module_id}")
            if module_id not in discovered:
                raise ValueError(f"module dependency is missing: {module_id}")
            visiting.add(module_id)
            manifest = discovered[module_id][0]
            for dependency in manifest.get("dependencies", []):
                visit(str(dependency))
            visiting.remove(module_id)
            visited.add(module_id)
            order.append(module_id)

        for module_id in discovered:
            visit(module_id)
        return order

    def load_all(self) -> dict[str, LoadedModule]:
        discovered = self.discover()
        for module_id in self._ordered(discovered):
            manifest, path = discovered[module_id]
            if manifest.get("enabled", True):
                self._load(module_id, manifest, path)
        self.context.module_manager = self
        return dict(self._loaded)

    def _load(self, module_id: str, manifest: dict[str, Any], path: Path) -> LoadedModule:
        declared_api = str(manifest.get("api_version", ""))
        if declared_api != self.api_version:
            raise RuntimeError(
                f"module {module_id} API {declared_api} is incompatible with runtime {self.api_version}"
            )
        entrypoint = path / str(manifest.get("entrypoint", "plugin.py"))
        if not entrypoint.exists():
            raise FileNotFoundError(f"module entrypoint is missing: {entrypoint}")
        import_name = f"afp_module_{module_id.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(import_name, entrypoint)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot create module spec: {entrypoint}")
        python_module = importlib.util.module_from_spec(spec)
        sys.modules[import_name] = python_module
        spec.loader.exec_module(python_module)
        register = getattr(python_module, "register", None)
        if not callable(register):
            raise TypeError(f"module {module_id} does not expose register(context)")
        registration = register(self.context)
        if not isinstance(registration, ModuleRegistration):
            raise TypeError(f"module {module_id} returned an invalid registration")
        if registration.module_id != module_id or registration.api_version != self.api_version:
            raise ValueError(f"module registration does not match its manifest: {module_id}")
        loaded = LoadedModule(manifest, python_module, registration, path)
        self._loaded[module_id] = loaded
        self.context.register_service(module_id, registration.service)
        return loaded

    def reload(self, module_id: str) -> LoadedModule:
        with self._lock:
            if module_id not in self._loaded:
                raise KeyError(f"module is not loaded: {module_id}")
            old = self._loaded.pop(module_id)
            service = old.registration.service
            shutdown = getattr(service, "shutdown", None)
            if callable(shutdown):
                shutdown()
            self.context.services.pop(module_id, None)
            sys.modules.pop(old.python_module.__name__, None)
            return self._load(module_id, old.manifest, old.path)

    def health(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for module_id, loaded in self._loaded.items():
            service = loaded.registration.service
            try:
                detail = dict(service.health_check()) if isinstance(service, HealthCheckable) else {"ok": True}
                detail.setdefault("ok", True)
            except Exception as exc:
                detail = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            result[module_id] = {
                "version": loaded.registration.version,
                "api_version": loaded.registration.api_version,
                "capabilities": list(loaded.registration.capabilities),
                "health": detail,
            }
        return result

    def status(self) -> dict[str, Any]:
        health = self.health()
        return {
            "api_version": self.api_version,
            "module_count": len(self._loaded),
            "all_healthy": all(item["health"].get("ok", False) for item in health.values()),
            "modules": health,
        }
