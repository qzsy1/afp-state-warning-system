from __future__ import annotations

import io
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acquisition import AcquisitionConfig, AcquisitionManager


DEFAULT_PER_SESSION_BYTES = 256 * 1024 * 1024
DEFAULT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_RUNNING = 4
_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


class GuestSimulationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


@dataclass(slots=True)
class GuestSimulationSession:
    session_id: str
    acquisition: AcquisitionManager
    save_root: Path
    created_at: float
    last_access_at: float


class GuestSimulationManager:
    """Own one simulation acquisition manager and directory per browser."""

    _SAFE_CONFIG_FIELDS = {
        "processing_mode",
        "dataset_schema",
        "use_best_prediction_override",
        "sample_rate_hz",
        "selected_sensors",
        "prediction_sensors",
        "model_input_sensors",
        "model_output_sensors",
        "prediction_model_type",
        "health_indicator",
        "run_id",
        "specimen_id",
        "condition_id",
        "layer",
        "cycle",
        "p",
        "v",
        "pr",
        "root",
        "initial_compaction_force_N",
        "placement_speed_mm_s",
        "pid_angle_deg",
        "temperature_setpoint_C",
        "replicate",
    }

    def __init__(
        self,
        root: Path,
        source_profiles: dict[str, dict[str, Any]],
        per_session_bytes: int = DEFAULT_PER_SESSION_BYTES,
        total_bytes: int = DEFAULT_TOTAL_BYTES,
        max_running: int = DEFAULT_MAX_RUNNING,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.source_profiles = {
            str(name): dict(profile)
            for name, profile in source_profiles.items()
            if str(name).strip() and isinstance(profile, dict)
        }
        if not self.source_profiles:
            raise ValueError("至少需要一个管理员批准的模拟数据源")
        self.per_session_bytes = max(1, int(per_session_bytes))
        self.total_bytes = max(self.per_session_bytes, int(total_bytes))
        self.max_running = max(1, int(max_running))
        self._sessions: dict[str, GuestSimulationSession] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _validate_session_id(session_id: str) -> str:
        clean = str(session_id or "").strip()
        if not _SESSION_PATTERN.fullmatch(clean):
            raise GuestSimulationError(
                "invalid_guest_session", "访客模拟会话编号无效"
            )
        return clean

    @staticmethod
    def _directory_bytes(path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += int(item.stat().st_size)
            except OSError:
                continue
        return total

    def _assert_quota(self, session: GuestSimulationSession) -> None:
        if self._directory_bytes(session.save_root) > self.per_session_bytes:
            raise GuestSimulationError(
                "guest_quota_exceeded", "当前访客模拟保存空间已达到上限"
            )
        if self._directory_bytes(self.root) > self.total_bytes:
            raise GuestSimulationError(
                "guest_total_quota_exceeded", "公共模拟保存总空间已达到上限"
            )

    def ensure_session(self, session_id: str) -> GuestSimulationSession:
        clean = self._validate_session_id(session_id)
        with self._lock:
            existing = self._sessions.get(clean)
            if existing is not None:
                existing.last_access_at = time.time()
                return existing
            save_root = (self.root / clean).resolve()
            if self.root not in save_root.parents:
                raise GuestSimulationError(
                    "invalid_guest_session", "访客模拟目录越过允许范围"
                )
            save_root.mkdir(parents=True, exist_ok=True)
            now = time.time()
            session = GuestSimulationSession(
                session_id=clean,
                acquisition=AcquisitionManager(capture_root=save_root),
                save_root=save_root,
                created_at=now,
                last_access_at=now,
            )
            self._sessions[clean] = session
            return session

    def _profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested = str(payload.get("source_profile") or "").strip()
        if not requested:
            requested = "builtin" if "builtin" in self.source_profiles else next(
                iter(self.source_profiles)
            )
        profile = self.source_profiles.get(requested)
        if profile is None:
            raise GuestSimulationError(
                "guest_source_not_allowed", "该模拟数据源没有经过本机管理员批准"
            )
        return profile

    def safe_config(self, session_id: str, payload: dict[str, Any]) -> AcquisitionConfig:
        session = self.ensure_session(session_id)
        profile = self._profile(payload)
        source_type = str(profile.get("source_type") or "single_csv").lower()
        if source_type not in {"single_csv", "folder_csv", "mysql"}:
            raise GuestSimulationError(
                "guest_source_not_allowed", "管理员配置的模拟数据源类型无效"
            )
        values = {
            key: value
            for key, value in dict(payload or {}).items()
            if key in self._SAFE_CONFIG_FIELDS
        }
        source_path = str(profile.get("path") or "").strip()
        values.update(
            {
                "acquisition_mode": "simulation",
                "driver": "simulator",
                "endpoint": source_path,
                "interfaces": [
                    {
                        "id": "guest_simulator",
                        "enabled": True,
                        "role": "custom",
                        "driver": "simulator",
                        "endpoint": source_path,
                        "channel_map": {},
                    }
                ],
                "interface_channel_assignments": {},
                "save_root": str(session.save_root),
                "source_file": source_path,
                "simulation_source_type": source_type,
                "simulation_source_path": source_path,
                "simulation_mysql_query": str(profile.get("query") or ""),
                "simulation_mysql_host": str(profile.get("host") or "127.0.0.1"),
                "simulation_mysql_port": int(profile.get("port") or 3306),
                "simulation_mysql_user": str(profile.get("user") or ""),
                "simulation_mysql_password": str(profile.get("password") or ""),
                "simulation_mysql_database": str(
                    profile.get("database") or "afp_state_warning"
                ),
                "mysql_enabled": False,
                "mysql_local_enabled": False,
                "mysql_password": "",
                "mysql_local_password": "",
            }
        )
        return AcquisitionConfig(**values)

    def _running_count(self) -> int:
        return sum(
            bool(session.acquisition.status().get("running"))
            for session in self._sessions.values()
        )

    def start(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self.ensure_session(session_id)
        with self._lock:
            self._assert_quota(session)
            if (
                not session.acquisition.status().get("running")
                and self._running_count() >= self.max_running
            ):
                raise GuestSimulationError(
                    "guest_capacity_reached", "公共模拟任务数量已达到上限"
                )
            config = self.safe_config(session_id, payload)
            result = session.acquisition.start(config)
            session.last_access_at = time.time()
            return self._public_status(session, result)

    def stop(self, session_id: str) -> dict[str, Any]:
        session = self.ensure_session(session_id)
        result = session.acquisition.stop()
        session.last_access_at = time.time()
        payload = self._public_status(session, result)
        try:
            self._assert_quota(session)
        except GuestSimulationError as exc:
            payload["quota_warning"] = exc.code
        return payload

    def status(self, session_id: str) -> dict[str, Any]:
        session = self.ensure_session(session_id)
        session.last_access_at = time.time()
        return self._public_status(session, session.acquisition.status())

    @staticmethod
    def _public_status(
        session: GuestSimulationSession, status: dict[str, Any]
    ) -> dict[str, Any]:
        payload = dict(status)
        payload["guest_session_id"] = session.session_id
        payload["server_save_scope"] = f"public_simulation/{session.session_id}"
        config = payload.get("config")
        if isinstance(config, dict):
            safe_config = dict(config)
            safe_config["save_root"] = payload["server_save_scope"]
            safe_config["source_file"] = "管理员批准的模拟数据源"
            safe_config["simulation_source_path"] = "管理员批准的模拟数据源"
            safe_config["simulation_mysql_host"] = ""
            safe_config["simulation_mysql_user"] = ""
            safe_config["simulation_mysql_password"] = ""
            payload["config"] = safe_config
        return payload

    def numeric_matrix(
        self, session_id: str
    ) -> tuple[list[dict[str, Any]], list[float]]:
        session = self.ensure_session(session_id)
        session.last_access_at = time.time()
        return session.acquisition.numeric_matrix()

    def acquisition(self, session_id: str) -> AcquisitionManager:
        return self.ensure_session(session_id).acquisition

    def download_archive(self, session_id: str) -> tuple[bytes, str]:
        session = self.ensure_session(session_id)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(session.save_root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                resolved = path.resolve()
                if session.save_root not in resolved.parents:
                    continue
                archive.write(resolved, resolved.relative_to(session.save_root).as_posix())
        session.last_access_at = time.time()
        return output.getvalue(), f"afp_simulation_{session.session_id}.zip"

    def cleanup_stopped(self) -> dict[str, int]:
        removed_sessions = 0
        removed_files = 0
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if session.acquisition.status().get("running"):
                    continue
                for path in sorted(
                    session.save_root.rglob("*"), key=lambda item: len(item.parts), reverse=True
                ):
                    if path.is_file() and not path.is_symlink():
                        path.unlink(missing_ok=True)
                        removed_files += 1
                    elif path.is_dir():
                        try:
                            path.rmdir()
                        except OSError:
                            pass
                try:
                    session.save_root.rmdir()
                except OSError:
                    pass
                self._sessions.pop(session_id, None)
                removed_sessions += 1
        return {"removed_sessions": removed_sessions, "removed_files": removed_files}
