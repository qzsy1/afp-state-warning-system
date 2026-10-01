from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from acquisition import NEW_COLLECTION_SENSOR_COLUMNS, SENSOR_CHANNEL_METADATA
from simulation_packages import PACKAGE_SPECS, resolve_simulation_package


SCHEMA_VERSION = "afp-public-demo-v1"
MANIFEST_SCHEMA_VERSION = "afp-public-demo-manifest-v1"
GENERATOR_VERSION = "synthetic-rule-20260930-v1"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "static" / "demo"

INTERFACES: tuple[dict[str, Any], ...] = (
    {
        "id": "thermocouple_8ch",
        "role": "thermocouple",
        "label": "八通道热电偶",
        "protocol": "smrf_hid",
        "channels": [f"温度{index}" for index in range(1, 9)],
    },
    {
        "id": "plc_process",
        "role": "plc",
        "label": "松下PLC过程传感器",
        "protocol": "modbus_tcp",
        "channels": ["温度", "压力", "张力"],
    },
    {
        "id": "uvc_temperature",
        "role": "thermal_uvc",
        "label": "BSV UVC测温热像仪",
        "protocol": "uvc",
        "channels": ["ROI平均温度"],
    },
    {
        "id": "abb_motion",
        "role": "robot",
        "label": "ABB机器人",
        "protocol": "abb_rws",
        "channels": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
    },
    {
        "id": "m3232_pressure",
        "role": "pressure",
        "label": "M3232薄膜压力",
        "protocol": "m3232_serial",
        "channels": ["薄膜压力"],
    },
)

CHANNEL_INTERFACE = {
    channel: interface["id"]
    for interface in INTERFACES
    for channel in interface["channels"]
}


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _package_spec(package_id: str) -> dict[str, Any]:
    clean = str(package_id or "").strip()
    for item in PACKAGE_SPECS:
        if item["package_id"] == clean:
            return dict(item)
    raise ValueError("公网演示数据包不存在")


def _read_rows(package_id: str) -> list[dict[str, float]]:
    source = resolve_simulation_package(package_id)
    rows: list[dict[str, float]] = []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for source_row in reader:
            parsed: dict[str, float] = {}
            for channel in NEW_COLLECTION_SENSOR_COLUMNS:
                value = float(source_row[channel])
                if not math.isfinite(value):
                    raise ValueError(f"演示数据通道包含非有限数值：{channel}")
                parsed[channel] = value
            rows.append(parsed)
    expected = int(_package_spec(package_id)["points"])
    if len(rows) != expected:
        raise ValueError(f"演示数据点数不一致：{package_id}")
    return rows


def _predicted(values: list[float], channel_index: int) -> list[float]:
    if not values:
        return []
    span = max(values) - min(values)
    amplitude = max(abs(span) * 0.015, max(abs(value) for value in values) * 0.0005, 0.0001)
    return [
        round(value + amplitude * math.sin((index + channel_index * 3) / 18.0), 6)
        for index, value in enumerate(values)
    ]


def _window_evidence(points: int) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    total = max(1, math.ceil(points / 24))
    for index in range(total):
        phase = index / max(total - 1, 1)
        anomaly = math.exp(-((phase - 0.66) ** 2) / 0.018)
        score = round(min(0.92, 0.12 + 0.68 * anomaly), 6)
        state = "abnormal" if score >= 0.65 else "warning" if score >= 0.35 else "normal"
        state_label = "异常" if state == "abnormal" else "预警" if state == "warning" else "正常"
        abnormal_probability = round(score, 6)
        warning_probability = round(min(1.0 - abnormal_probability, score * 0.35), 6)
        normal_probability = round(max(0.0, 1.0 - abnormal_probability - warning_probability), 6)
        windows.append(
            {
                "id": f"W{index + 1:04d}",
                "start_index": index * 24,
                "end_index": min(points, (index + 1) * 24) - 1,
                "complete": (index + 1) * 24 <= points,
                "score": score,
                "health": round(1.0 - score, 6),
                "state": state,
                "state_label": state_label,
                "type_probabilities": {
                    "normal": normal_probability,
                    "warning": warning_probability,
                    "abnormal": abnormal_probability,
                },
            }
        )
    return windows


def build_demo_bundle(package_id: str) -> dict[str, Any]:
    spec = _package_spec(package_id)
    rows = _read_rows(package_id)
    points = len(rows)
    channels: list[dict[str, Any]] = []
    for channel_index, name in enumerate(NEW_COLLECTION_SENSOR_COLUMNS):
        actual = [round(row[name], 6) for row in rows]
        metadata = SENSOR_CHANNEL_METADATA.get(name, {})
        channels.append(
            {
                "id": channel_index,
                "name": name,
                "unit": str(metadata.get("unit") or ""),
                "interface_id": CHANNEL_INTERFACE[name],
                "actual": actual,
                "predicted": _predicted(actual, channel_index),
            }
        )
    windows = _window_evidence(points)
    peak = max(windows, key=lambda item: item["score"])
    average_health = sum(float(item["health"]) for item in windows) / len(windows)
    return {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "package_id": str(spec["package_id"]),
        "label": str(spec["label"]),
        "synthetic": True,
        "precomputed": True,
        "evidence_scope": "demonstration_only",
        "sample_rate_hz": 10.0,
        "points": points,
        "duration_seconds": points / 10.0,
        "interfaces": [dict(item) for item in INTERFACES],
        "channels": channels,
        "window_evidence": windows,
        "aggregate": {
            "layer": {
                "health": round(average_health, 6),
                "state": peak["state"],
                "state_label": peak["state_label"],
                "evidence_count": len(windows),
            },
            "specimen": {
                "health": round(average_health, 6),
                "state": peak["state"],
                "state_label": peak["state_label"],
                "evidence_layers": 1,
            },
        },
        "process": {
            "schema_id": "new_collection_v11_3",
            "parameter_source": "precomputed_synthetic",
            "display_parameters": [
                {"label": "初始压实力", "unit": "N", "value": 400.0},
                {"label": "铺放速度", "unit": "mm/s", "value": 80.0},
                {"label": "PID角度", "unit": "°", "value": 5.0},
                {"label": "设定温度", "unit": "°C", "value": 360.0},
            ],
        },
        "diagnosis": {
            "precomputed": True,
            "title": "预计算 synthetic 演示诊断",
            "conclusion": "演示在中段注入可视化异常趋势，用于验证界面、预测和三级预警展示；不代表设备故障或真实缺陷。",
            "evidence_sources": [
                "批准的 synthetic 演示数据",
                f"{len(windows)} 个预计算 24 点窗口",
                "五类逻辑接口映射（未连接物理设备）",
            ],
        },
    }


def write_demo_assets(output_dir: Path | str = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    packages: list[dict[str, Any]] = []
    keep: set[str] = {"manifest-v1.json"}
    for spec in PACKAGE_SPECS:
        bundle = build_demo_bundle(str(spec["package_id"]))
        raw = _json_bytes(bundle)
        digest = hashlib.sha256(raw).hexdigest()
        filename = f"{spec['package_id']}.{digest[:16]}.json"
        (target / filename).write_bytes(raw)
        keep.add(filename)
        packages.append(
            {
                "package_id": str(spec["package_id"]),
                "label": str(spec["label"]),
                "schema_version": SCHEMA_VERSION,
                "synthetic": True,
                "precomputed": True,
                "sample_rate_hz": 10.0,
                "points": int(spec["points"]),
                "duration_seconds": int(spec["points"]) / 10.0,
                "channels": len(NEW_COLLECTION_SENSOR_COLUMNS),
                "interfaces": len(INTERFACES),
                "bytes": len(raw),
                "sha256": digest,
                "url": f"/demo/{filename}",
            }
        )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "synthetic": True,
        "packages": packages,
    }
    (target / "manifest-v1.json").write_bytes(_json_bytes(manifest))
    for old in target.glob("*.json"):
        if old.name not in keep:
            old.unlink()
    return manifest


if __name__ == "__main__":
    manifest = write_demo_assets()
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
