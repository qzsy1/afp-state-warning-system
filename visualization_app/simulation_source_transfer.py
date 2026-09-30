"""Session-bound transfer contract for browser-uploaded simulation sources."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import os
import secrets
import shutil
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


MAX_SOURCE_FILES = 500
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_TRANSFER_CHUNK_BYTES = 1024 * 1024


class SimulationSourceTransferError(ValueError):
    """Raised when a transfer violates its session or integrity contract."""


def _numeric_value(value: str) -> bool:
    try:
        return math.isfinite(float(str(value).strip()))
    except (TypeError, ValueError):
        return False


def inspect_csv_file(path: str | Path) -> dict[str, Any]:
    """Stream one CSV and prove that it contains a header and numeric samples."""

    source = Path(path)
    if not source.is_file() or int(source.stat().st_size) <= 0:
        raise SimulationSourceTransferError(f"模拟源 CSV 为空或为 0 字节：{source.name}")
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with source.open("r", encoding=encoding, newline="") as handle:
                reader = csv.reader(handle)
                header = [str(item).strip() for item in next(reader, [])]
                if not header or not any(header) or all(_numeric_value(item) for item in header if item):
                    raise SimulationSourceTransferError(
                        f"模拟源 CSV 缺少可解析表头：{source.name}"
                    )
                numeric_channels: set[str] = set()
                valid_rows = 0
                for row in reader:
                    row_has_numeric = False
                    for index, cell in enumerate(row[:len(header)]):
                        if _numeric_value(cell):
                            row_has_numeric = True
                            if header[index]:
                                numeric_channels.add(header[index])
                    if row_has_numeric:
                        valid_rows += 1
                if valid_rows <= 0:
                    raise SimulationSourceTransferError(
                        f"模拟源 CSV 没有有效数值数据行：{source.name}"
                    )
                return {
                    "channels": [item for item in header if item],
                    "numeric_channels": sorted(numeric_channels),
                    "valid_rows": valid_rows,
                }
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except csv.Error as exc:
            raise SimulationSourceTransferError(
                f"模拟源 CSV 格式无效：{source.name}"
            ) from exc
    raise SimulationSourceTransferError(
        f"模拟源 CSV 编码无法解析：{source.name}"
    ) from last_error


def inspect_csv_source(
    source_type: str,
    source_path: str | Path,
    *,
    required_channels: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Validate replay content without loading the complete source into memory."""

    clean_type = str(source_type or "").strip().lower()
    path = Path(source_path)
    if clean_type == "single_csv":
        files = [path]
    elif clean_type == "folder_csv":
        files = sorted(path.rglob("*.csv")) if path.is_dir() else []
    else:
        raise SimulationSourceTransferError("helper 模拟回放只支持 CSV 或 CSV 文件夹")
    if not files:
        raise SimulationSourceTransferError("模拟源中没有 CSV 文件")
    channels: set[str] = set()
    numeric_channels: set[str] = set()
    valid_rows = 0
    for file in files:
        result = inspect_csv_file(file)
        channels.update(result["channels"])
        numeric_channels.update(result["numeric_channels"])
        valid_rows += int(result["valid_rows"])
    required = [str(item).strip() for item in (required_channels or []) if str(item).strip()]
    missing = [item for item in required if item not in numeric_channels]
    if missing:
        raise SimulationSourceTransferError(f"模拟数据缺少有效数值通道：{missing}")
    return {
        "channels": sorted(channels),
        "numeric_channels": sorted(numeric_channels),
        "valid_rows": valid_rows,
    }


def _safe_relative_path(value: str) -> Path:
    raw = str(value or "").replace("\\", "/").strip()
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.lower() != ".csv"
    ):
        raise SimulationSourceTransferError("模拟源包含不安全的相对 CSV 路径")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(MAX_TRANSFER_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _content_sha256(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["relative_path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(int(item["size"])).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_source_manifest(
    source_id: str,
    source_type: str,
    source_path: str | Path,
) -> tuple[dict[str, Any], Path]:
    """Build a path-free manifest and retain the private source root separately."""

    clean_id = str(source_id or "").strip()
    clean_type = str(source_type or "").strip().lower()
    path = Path(source_path).resolve()
    if not clean_id:
        raise SimulationSourceTransferError("模拟源标识为空")
    if clean_type == "single_csv":
        if not path.is_file() or path.suffix.lower() != ".csv":
            raise SimulationSourceTransferError("单文件模拟源不是有效 CSV")
        source_root = path.parent
        candidates = [(path.name, path)]
    elif clean_type == "folder_csv":
        if not path.is_dir():
            raise SimulationSourceTransferError("模拟源文件夹不存在")
        source_root = path
        candidates = [
            (item.relative_to(source_root).as_posix(), item)
            for item in sorted(source_root.rglob("*.csv"))
            if item.is_file()
        ]
    else:
        raise SimulationSourceTransferError("helper 模拟回放只支持 CSV 或 CSV 文件夹")
    if not candidates:
        raise SimulationSourceTransferError("模拟源中没有 CSV 文件")
    if len(candidates) > MAX_SOURCE_FILES:
        raise SimulationSourceTransferError("模拟源文件数量超过 500 个限制")

    files: list[dict[str, Any]] = []
    total = 0
    source_channels: set[str] = set()
    numeric_channels: set[str] = set()
    valid_rows = 0
    for relative_name, candidate in candidates:
        relative = _safe_relative_path(relative_name)
        resolved = candidate.resolve()
        if source_root != resolved.parent and source_root not in resolved.parents:
            raise SimulationSourceTransferError("模拟源文件越过允许目录")
        size = int(resolved.stat().st_size)
        if size > MAX_SOURCE_FILE_BYTES:
            raise SimulationSourceTransferError("单个模拟源文件超过 16 MB 限制")
        total += size
        if total > MAX_SOURCE_TOTAL_BYTES:
            raise SimulationSourceTransferError("模拟源总大小超过 64 MB 限制")
        inspected = inspect_csv_file(resolved)
        source_channels.update(inspected["channels"])
        numeric_channels.update(inspected["numeric_channels"])
        valid_rows += int(inspected["valid_rows"])
        files.append(
            {
                "relative_path": relative.as_posix(),
                "size": size,
                "sha256": _file_sha256(resolved),
            }
        )
    return (
        {
            "version": 1,
            "source_id": clean_id,
            "source_type": clean_type,
            "files": files,
            "file_count": len(files),
            "total_bytes": total,
            "content_sha256": _content_sha256(files),
            "channels": sorted(source_channels),
            "numeric_channels": sorted(numeric_channels),
            "valid_rows": valid_rows,
        },
        source_root,
    )


@dataclass
class _Ticket:
    web_session_id: str
    helper_session_id: str
    manifest: dict[str, Any]
    source_root: Path
    expires_at: float


class SimulationSourceTicketStore:
    """Issue short-lived tickets that can only be used by one paired helper."""

    def __init__(self, *, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = max(30.0, float(ttl_seconds))
        self._lock = threading.RLock()
        self._tickets: dict[str, _Ticket] = {}

    def issue(
        self,
        *,
        web_session_id: str,
        helper_session_id: str,
        manifest: dict[str, Any],
        source_root: str | Path,
    ) -> dict[str, Any]:
        web_session = str(web_session_id or "").strip()
        helper_session = str(helper_session_id or "").strip()
        if not web_session or not helper_session:
            raise SimulationSourceTransferError("模拟源交付缺少绑定会话")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tickets[token] = _Ticket(
                web_session_id=web_session,
                helper_session_id=helper_session,
                manifest=deepcopy(manifest),
                source_root=Path(source_root).resolve(),
                expires_at=time.time() + self.ttl_seconds,
            )
        return {"ticket": token, "manifest": deepcopy(manifest)}

    def _authorized(self, token: str, helper_session_id: str) -> _Ticket:
        with self._lock:
            ticket = self._tickets.get(str(token or ""))
            if ticket is None or ticket.expires_at < time.time():
                self._tickets.pop(str(token or ""), None)
                raise SimulationSourceTransferError("模拟源交付票据不存在或已过期")
            if not secrets.compare_digest(
                ticket.helper_session_id, str(helper_session_id or "")
            ):
                raise SimulationSourceTransferError("模拟源交付票据不属于当前 helper 会话")
            return ticket

    def read_chunk(
        self,
        token: str,
        helper_session_id: str,
        *,
        file_index: int,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        ticket = self._authorized(token, helper_session_id)
        files = ticket.manifest.get("files")
        if not isinstance(files, list) or not 0 <= int(file_index) < len(files):
            raise SimulationSourceTransferError("模拟源文件索引无效")
        item = files[int(file_index)]
        relative = _safe_relative_path(str(item.get("relative_path") or ""))
        target = (ticket.source_root / relative).resolve()
        if ticket.source_root != target.parent and ticket.source_root not in target.parents:
            raise SimulationSourceTransferError("模拟源路径越过允许目录")
        expected_size = int(item.get("size") or 0)
        start = max(0, int(offset))
        if start > expected_size:
            raise SimulationSourceTransferError("模拟源分块偏移超过文件大小")
        size = max(1, min(int(limit), MAX_TRANSFER_CHUNK_BYTES))
        with target.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(size)
        next_offset = start + len(raw)
        return {
            "ok": True,
            "relative_path": relative.as_posix(),
            "file_index": int(file_index),
            "offset": start,
            "next_offset": next_offset,
            "size": expected_size,
            "eof": next_offset >= expected_size,
            "data": base64.b64encode(raw).decode("ascii"),
        }


class SimulationSourceCache:
    """Download and atomically publish a verified content-addressed source."""

    def __init__(
        self, root: str | Path, *, chunk_bytes: int = MAX_TRANSFER_CHUNK_BYTES
    ) -> None:
        self.root = Path(root).resolve()
        self.chunk_bytes = max(1, min(int(chunk_bytes), MAX_TRANSFER_CHUNK_BYTES))

    @staticmethod
    def _content_identity(manifest: dict[str, Any]) -> dict[str, Any]:
        """Return the stable fields that identify cached bytes.

        ``source_id`` belongs to the browser upload session and can change when
        the same source is selected again.  It must not invalidate an already
        verified content-addressed cache entry.
        """

        return {
            key: deepcopy(manifest.get(key))
            for key in (
                "version",
                "source_type",
                "files",
                "file_count",
                "total_bytes",
                "content_sha256",
            )
        }

    @staticmethod
    def _validate_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
        files = manifest.get("files")
        if not isinstance(files, list) or not files or len(files) > MAX_SOURCE_FILES:
            raise SimulationSourceTransferError("模拟源清单文件数量无效")
        total = 0
        clean_files: list[dict[str, Any]] = []
        for item in files:
            if not isinstance(item, dict):
                raise SimulationSourceTransferError("模拟源清单文件项无效")
            relative = _safe_relative_path(str(item.get("relative_path") or ""))
            size = int(item.get("size") or 0)
            digest = str(item.get("sha256") or "").lower()
            if size <= 0 or size > MAX_SOURCE_FILE_BYTES:
                raise SimulationSourceTransferError("模拟源清单文件大小无效")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise SimulationSourceTransferError("模拟源清单 SHA-256 无效")
            total += size
            if total > MAX_SOURCE_TOTAL_BYTES:
                raise SimulationSourceTransferError("模拟源清单总大小超过限制")
            clean_files.append(
                {"relative_path": relative.as_posix(), "size": size, "sha256": digest}
            )
        if total != int(manifest.get("total_bytes") or 0):
            raise SimulationSourceTransferError("模拟源清单总大小不一致")
        if _content_sha256(clean_files) != str(manifest.get("content_sha256") or ""):
            raise SimulationSourceTransferError("模拟源清单内容 SHA-256 不一致")
        return clean_files

    def is_ready(self, transfer: dict[str, Any]) -> bool:
        """Return whether the transfer already has a verified cache entry."""

        manifest = deepcopy(transfer.get("manifest")) if isinstance(transfer, dict) else None
        if not isinstance(manifest, dict):
            return False
        try:
            self._validate_manifest(manifest)
        except (TypeError, ValueError, SimulationSourceTransferError):
            return False
        content_digest = str(manifest.get("content_sha256") or "")
        marker = (self.root / content_digest / ".manifest.json").resolve()
        if not marker.is_file():
            return False
        try:
            cached_manifest = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return self._content_identity(cached_manifest) == self._content_identity(manifest)

    def materialize(
        self,
        transfer: dict[str, Any],
        fetch_chunk: Callable[[str, int, int, int], dict[str, Any]],
    ) -> Path:
        ticket = str(transfer.get("ticket") or "")
        manifest = deepcopy(transfer.get("manifest"))
        if not ticket or not isinstance(manifest, dict):
            raise SimulationSourceTransferError("模拟源交付描述不完整")
        files = self._validate_manifest(manifest)
        content_digest = str(manifest["content_sha256"])
        destination = (self.root / content_digest).resolve()
        marker = destination / ".manifest.json"
        if self.is_ready(transfer):
            return destination

        self.root.mkdir(parents=True, exist_ok=True)
        temporary = (self.root / f".{content_digest}.{secrets.token_hex(8)}.partial").resolve()
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            for file_index, item in enumerate(files):
                relative = _safe_relative_path(item["relative_path"])
                target = (temporary / relative).resolve()
                if temporary not in target.parents:
                    raise SimulationSourceTransferError("模拟源缓存路径越过允许目录")
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                offset = 0
                with target.open("wb") as handle:
                    while offset < int(item["size"]):
                        chunk = fetch_chunk(ticket, file_index, offset, self.chunk_bytes)
                        if not isinstance(chunk, dict) or not chunk.get("ok"):
                            raise SimulationSourceTransferError("模拟源分块下载中断")
                        if int(chunk.get("offset", -1)) != offset:
                            raise SimulationSourceTransferError("模拟源分块偏移不连续")
                        if str(chunk.get("relative_path") or "") != relative.as_posix():
                            raise SimulationSourceTransferError("模拟源分块路径不一致")
                        try:
                            raw = base64.b64decode(str(chunk.get("data") or ""), validate=True)
                        except ValueError as exc:
                            raise SimulationSourceTransferError("模拟源分块不是有效 Base64") from exc
                        if not raw or offset + len(raw) > int(item["size"]):
                            raise SimulationSourceTransferError("模拟源分块大小无效")
                        handle.write(raw)
                        digest.update(raw)
                        offset += len(raw)
                if digest.hexdigest() != item["sha256"]:
                    raise SimulationSourceTransferError("模拟源文件 SHA-256 校验失败")
            marker = temporary / ".manifest.json"
            marker.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(temporary, destination)
            return destination
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
