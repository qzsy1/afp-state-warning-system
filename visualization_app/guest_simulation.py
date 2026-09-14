from __future__ import annotations

import base64
import binascii
import csv
import io
import re
import shutil
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
DEFAULT_MAX_UPLOAD_BYTES = 64 * 1024 * 1024
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
    selected_source: dict[str, Any] | None = None


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

    def select_source(self, session_id: str, source_type: str = "single_csv", initial_path: str = "") -> dict[str, Any]:
        """Open the same local chooser used by the desktop UI and approve it as the guest source.

        The chooser runs on the acquisition host; the browser receives only the
        selected filename.  The selected source remains simulator-only.
        """
        self.ensure_session(session_id)
        from acquisition import select_simulation_source

        clean_type = str(source_type or "single_csv").strip().lower()
        if clean_type not in {"single_csv", "folder_csv"}:
            raise GuestSimulationError("guest_source_not_allowed", "只允许选择 CSV 文件或采集数据文件夹")
        selected = str(select_simulation_source(clean_type, str(initial_path or "")) or "").strip()
        if not selected:
            return {"selected": False, "path": "", "name": ""}
        path = Path(selected).resolve()
        if clean_type == "single_csv" and path.suffix.lower() != ".csv":
            raise GuestSimulationError("guest_source_not_allowed", "模拟数据必须是 CSV 文件")
        if clean_type == "folder_csv" and not path.is_dir():
            raise GuestSimulationError("guest_source_not_allowed", "模拟数据文件夹无效")
        self.ensure_session(session_id).selected_source = {
            **self.source_profiles.get("builtin", {}),
            "source_type": clean_type,
            "path": str(path),
        }
        return {"selected": True, "path": "", "name": path.name}

    @staticmethod
    def _safe_upload_name(name: str) -> Path:
        raw = str(name or "").replace("\\", "/").strip()
        relative = Path(raw)
        if not raw or relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError("上传文件名无效")
        if relative.suffix.lower() != ".csv":
            raise ValueError("模拟数据只支持 CSV 文件")
        return relative

    def upload_source(
        self,
        session_id: str,
        source_type: str,
        files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Store browser-selected CSV files in the current guest session."""
        session = self.ensure_session(session_id)
        clean_type = str(source_type or "single_csv").strip().lower()
        if clean_type not in {"single_csv", "folder_csv"}:
            raise ValueError("访客模式只支持单 CSV 或 CSV 文件夹上传")
        if not isinstance(files, list) or not files:
            raise ValueError("请选择至少一个 CSV 文件")
        if clean_type == "single_csv" and len(files) != 1:
            raise ValueError("单 CSV 模式只能选择一个文件")

        source_root = (session.save_root / ".simulation_source").resolve()
        if session.save_root not in source_root.parents:
            raise GuestSimulationError(
                "invalid_guest_session", "模拟数据目录越过允许范围"
            )
        shutil.rmtree(source_root, ignore_errors=True)
        source_root.mkdir(parents=True, exist_ok=True)
        total = 0
        names: list[str] = []
        try:
            for item in files:
                if not isinstance(item, dict):
                    raise ValueError("上传文件描述无效")
                relative = self._safe_upload_name(str(item.get("name") or ""))
                destination = (source_root / relative).resolve()
                if source_root not in destination.parents:
                    raise ValueError("上传文件路径无效")
                if relative.as_posix() in names:
                    raise ValueError("上传文件名重复")
                encoded = str(item.get("data") or "")
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ValueError("上传文件内容不是有效的 Base64") from exc
                total += len(content)
                if total > DEFAULT_MAX_UPLOAD_BYTES:
                    raise ValueError("上传文件总大小超过 64 MB 限制")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                names.append(relative.as_posix())
        except Exception:
            shutil.rmtree(source_root, ignore_errors=True)
            raise
        selected_path = (
            source_root if clean_type == "folder_csv"
            else source_root / Path(names[0])
        ).resolve()
        session.selected_source = {
            "source_type": clean_type,
            "path": str(selected_path),
        }
        status = self.source_status(session_id)
        return {
            "selected": True,
            "source_type": clean_type,
            "path": "",
            "name": names[0] if clean_type == "single_csv" else f"已上传 {len(names)} 个 CSV",
            "files": names,
            "bytes": total,
            "channels": status.get("channels", []),
        }

    def source_status(self, session_id: str) -> dict[str, Any]:
        session = self.ensure_session(session_id)
        profile = session.selected_source or self._profile({})
        path = Path(str(profile.get("path") or ""))
        source_type = str(profile.get("source_type") or "single_csv")
        channels: list[str] = []
        if source_type == "single_csv" and path.is_file():
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    channels = list(next(csv.reader(handle), []))
            except (OSError, UnicodeDecodeError):
                try:
                    with path.open("r", encoding="gb18030", newline="") as handle:
                        channels = list(next(csv.reader(handle), []))
                except (OSError, UnicodeDecodeError):
                    channels = []
        elif source_type == "folder_csv" and path.is_dir():
            for file in sorted(path.rglob("*.csv")):
                try:
                    with file.open("r", encoding="utf-8-sig", newline="") as handle:
                        channels.extend(next(csv.reader(handle), []))
                except (OSError, UnicodeDecodeError):
                    continue
            channels = list(dict.fromkeys(channels))
        return {
            "source_type": source_type,
            "name": (
                f"已上传 {len(list(path.rglob('*.csv')))} 个 CSV"
                if source_type == "folder_csv" and path.is_dir()
                else path.name
            ),
            "channels": channels,
        }

    def safe_config(self, session_id: str, payload: dict[str, Any]) -> AcquisitionConfig:
        session = self.ensure_session(session_id)
        profile = session.selected_source or self._profile(payload)
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

    def export_manifest(self, session_id: str) -> dict[str, Any]:
        session = self.ensure_session(session_id)
        session.last_access_at = time.time()
        return session.acquisition.export_manifest()

    def export_file(self, session_id: str, relative_path: str) -> tuple[bytes, str]:
        session = self.ensure_session(session_id)
        session.last_access_at = time.time()
        return session.acquisition.export_file(relative_path)

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
