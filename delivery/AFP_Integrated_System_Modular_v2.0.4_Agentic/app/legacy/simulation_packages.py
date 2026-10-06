from __future__ import annotations

import hashlib
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from acquisition import NEW_COLLECTION_SENSOR_COLUMNS
from simulation_source_transfer import inspect_csv_file


PACKAGE_ROOT = (Path(__file__).resolve().parent / "simulation_packages").resolve()
PACKAGE_SPECS = (
    {
        "package_id": "quick_240",
        "filename": "afp_synthetic_quick_240.csv",
        "label": "快速验证数据（240点 / 24秒）",
        "points": 240,
    },
    {
        "package_id": "endurance_6000",
        "filename": "afp_synthetic_endurance_6000.csv",
        "label": "十分钟耐久数据（6000点 / 600秒）",
        "points": 6000,
    },
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_simulation_package(package_id: str) -> Path:
    clean = str(package_id or "").strip()
    spec = next((item for item in PACKAGE_SPECS if item["package_id"] == clean), None)
    if spec is None:
        raise ValueError("模拟数据包不存在")
    path = (PACKAGE_ROOT / str(spec["filename"])).resolve()
    if PACKAGE_ROOT not in path.parents or not path.is_file():
        raise ValueError("模拟数据包尚未生成或已丢失")
    return path


@lru_cache(maxsize=1)
def _catalog_cached() -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for spec in PACKAGE_SPECS:
        path = resolve_simulation_package(str(spec["package_id"]))
        inspected = inspect_csv_file(path)
        points = int(spec["points"])
        if int(inspected["valid_rows"]) != points:
            raise ValueError(f"模拟数据包点数不一致：{spec['package_id']}")
        missing = [
            channel
            for channel in NEW_COLLECTION_SENSOR_COLUMNS
            if channel not in inspected["numeric_channels"]
        ]
        if missing:
            raise ValueError(f"模拟数据包缺少新采集通道：{missing}")
        result.append(
            {
                "package_id": str(spec["package_id"]),
                "filename": str(spec["filename"]),
                "label": str(spec["label"]),
                "synthetic": True,
                "sample_rate_hz": 10.0,
                "points": points,
                "duration_seconds": points / 10.0,
                "channels": list(NEW_COLLECTION_SENSOR_COLUMNS),
                "bytes": int(path.stat().st_size),
                "sha256": _sha256(path),
            }
        )
    return tuple(result)


def simulation_package_catalog() -> list[dict[str, Any]]:
    return deepcopy(list(_catalog_cached()))
