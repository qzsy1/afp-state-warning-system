from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class UpdateManager:
    def __init__(self, context: Any) -> None:
        self.context = context

    def _safe_target(self, relative: str) -> Path:
        if not relative or Path(relative).is_absolute():
            raise ValueError(f"invalid patch path: {relative!r}")
        target = (self.context.root / relative).resolve()
        if target != self.context.root and self.context.root not in target.parents:
            raise ValueError(f"patch path escapes application root: {relative}")
        return target

    def install(self, archive: Path) -> dict[str, Any]:
        archive = archive.resolve()
        if not archive.exists():
            raise FileNotFoundError(archive)
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1_000_000:06d}"
        backup_root = self.context.paths.rollback_dir / stamp
        staging_root = Path(tempfile.mkdtemp(prefix="afp_patch_", dir=self.context.paths.updates_dir))
        touched: list[Path] = []
        try:
            with zipfile.ZipFile(archive) as package:
                # Do not use extractall(): reject absolute/parent paths before a
                # patch is allowed to write anything outside its staging area.
                for member in package.infolist():
                    normalized = member.filename.replace("\\", "/")
                    relative = Path(normalized)
                    destination = (staging_root / relative).resolve()
                    if (
                        not normalized
                        or relative.is_absolute()
                        or ".." in relative.parts
                        or (destination != staging_root and staging_root not in destination.parents)
                    ):
                        raise ValueError(f"unsafe patch archive entry: {member.filename!r}")
                    if member.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as source_handle, destination.open("wb") as target_handle:
                        shutil.copyfileobj(source_handle, target_handle)
            manifest_path = staging_root / "patch_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if str(manifest.get("api_version")) != str(self.context.config.get("api_version")):
                raise RuntimeError("patch API version is incompatible with this application")
            files = manifest.get("files", [])
            if not files:
                raise ValueError("patch contains no files")
            for item in files:
                relative = str(item["path"]).replace("\\", "/")
                source = (staging_root / "payload" / relative).resolve()
                target = self._safe_target(relative)
                if not source.exists() or sha256(source).lower() != str(item["sha256"]).lower():
                    raise ValueError(f"patch hash validation failed: {relative}")
                if target.exists():
                    backup = backup_root / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".updating")
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
                touched.append(target)
            record = {
                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "archive": str(archive),
                "manifest": manifest,
                "backup": str(backup_root),
                "touched": [str(path.relative_to(self.context.root)) for path in touched],
            }
            backup_root.mkdir(parents=True, exist_ok=True)
            (backup_root / "rollback_manifest.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return record
        except Exception:
            # The rollback manifest is intentionally written only after a
            # successful install.  Restore the files already replaced during a
            # partially failed install directly from the in-memory touch list.
            for target in reversed(touched):
                relative = target.relative_to(self.context.root)
                backup = backup_root / relative
                if backup.exists():
                    temporary = target.with_name(target.name + ".rollback")
                    shutil.copy2(backup, temporary)
                    os.replace(temporary, target)
                elif target.exists():
                    target.unlink()
            raise
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    def rollback(self, backup: Path | None = None) -> dict[str, Any]:
        if backup is None:
            candidates = sorted(self.context.paths.rollback_dir.glob("*/rollback_manifest.json"))
            if not candidates:
                raise FileNotFoundError("no rollback snapshot is available")
            backup = candidates[-1].parent
        backup = backup.resolve()
        record_path = backup / "rollback_manifest.json"
        record = json.loads(record_path.read_text(encoding="utf-8-sig"))
        restored: list[str] = []
        for relative in record.get("touched", []):
            target = self._safe_target(relative)
            source = backup / relative
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".rollback")
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
                restored.append(relative)
            elif target.exists():
                target.unlink()
                restored.append(relative)
        return {"backup": str(backup), "restored": restored}
