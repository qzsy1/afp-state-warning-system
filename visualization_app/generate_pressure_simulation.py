"""Generate a deterministic, full-channel CSV for acquisition-chain testing.

This file intentionally contains the 16 channels of the current new
collection plan plus pressure-matrix diagnostics.  It is synthetic data for
software validation only, not evidence of a real AFP defect.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


SENSOR_COLUMNS = [
    "温度", "压力", "ROI平均温度", "张力", "线速度", "ABB_X", "ABB_Y", "ABB_Z",
    *[f"温度{i}" for i in range(1, 9)],
]
PROCESS_COLUMNS = [
    "initial_compaction_force_N", "placement_speed_mm_s", "pid_angle_deg",
    "temperature_setpoint_C",
]
METADATA_COLUMNS = [
    "run_id", "specimen_id", "condition_id", "replicate", "layer_id",
    "sample_index", "state_label", "abnormal_type", "source_type",
    "source_file", "schema_mode", "timestamp",
]
PRESSURE_COLUMNS = ["压力峰值", "压力接触面积", "压力有效像素比例", "压力矩阵JSON"]


def _pressure_matrix(t: float, layer: int, rng: np.random.Generator) -> list[list[float]]:
    y, x = np.mgrid[0:32, 0:32]
    cx = 15.5 + 1.4 * np.sin(t * 0.45 + layer * 0.3)
    cy = 15.5 + 1.0 * np.cos(t * 0.37)
    sigma = 6.0 + 0.4 * np.sin(t * 0.2)
    peak = 80.0 + 12.0 * np.sin(t * 0.8) + layer * 1.5
    matrix = peak * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2.0 * sigma**2))
    matrix += rng.normal(0.0, 0.8, size=(32, 32))
    matrix[matrix < 0.0] = 0.0
    return np.round(matrix, 3).tolist()


def generate(output: Path, rows: int = 240, seed: int = 20260910) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    t = np.arange(rows, dtype=float) * 0.1
    layer = 1 + (np.arange(rows) // max(1, rows // 5)).clip(0, 4)
    force, speed, angle, setpoint = 420.0, 80.0, 5.0, 240.0
    contact = 1.0 / (1.0 + np.exp(-(t - 1.0) * 4.5))
    pressure = force * contact * (1.0 + 0.02 * np.sin(t * 2.0)) + rng.normal(0, 3, rows)
    temperature = 24.0 + (setpoint - 24.0) * (1 - np.exp(-t / 4.0)) * 0.68 + rng.normal(0, 0.35, rows)
    speed_series = speed * (1 - np.exp(-t / 0.7)) + rng.normal(0, 0.25, rows)
    tension = 19 + 0.115 * speed_series + rng.normal(0, 0.18, rows)
    roi = temperature + 3.2 - 0.014 * speed + rng.normal(0, 0.25, rows)
    abb_x = np.cumsum(np.maximum(speed_series, 0)) * 0.1
    abb_y = 2.5 * np.sin(np.linspace(0, np.pi, rows))
    abb_z = (layer - 1) * 0.18
    matrices = [_pressure_matrix(float(value), int(layer[idx]), rng) for idx, value in enumerate(t)]
    peaks = [max(v for row in m for v in row) for m in matrices]
    areas = [sum(v > 5.0 for row in m for v in row) for m in matrices]
    valid = [sum(v > 0.0 for row in m for v in row) / 1024.0 for m in matrices]
    data: dict[str, object] = {
        "时间": t,
        "温度": temperature,
        "压力": pressure,
        "ROI平均温度": roi,
        "张力": tension,
        "线速度": speed_series,
        "ABB_X": abb_x,
        "ABB_Y": abb_y,
        "ABB_Z": abb_z,
        "initial_compaction_force_N": force,
        "placement_speed_mm_s": speed,
        "pid_angle_deg": angle,
        "temperature_setpoint_C": setpoint,
        "run_id": "SIM_M3232_R1",
        "specimen_id": "SIM_M3232",
        "condition_id": "SIM_NORMAL",
        "replicate": 1,
        "layer_id": layer,
        "sample_index": np.arange(rows),
        "state_label": 0,
        "abnormal_type": "正常",
        "source_type": "simulation",
        "source_file": output.name,
        "schema_mode": "new",
        "timestamp": pd.Timestamp("2026-09-10T08:00:00") + pd.to_timedelta(t, unit="s"),
        "压力峰值": peaks,
        "压力接触面积": areas,
        "压力有效像素比例": valid,
        "压力矩阵JSON": [json.dumps(m, ensure_ascii=False, separators=(",", ":")) for m in matrices],
    }
    for idx in range(1, 9):
        data[f"温度{idx}"] = temperature + (idx - 4.5) * 0.7 + rng.normal(0, 0.2, rows)
    frame = pd.DataFrame(data)
    ordered = ["时间", *SENSOR_COLUMNS, *PROCESS_COLUMNS, *METADATA_COLUMNS, *PRESSURE_COLUMNS]
    frame = frame.loc[:, ordered]
    frame.to_csv(output, index=False, encoding="utf-8-sig")
    manifest = {
        "purpose": "用于新软件采集/预测链路测试，不是真实传感器数据或缺陷证据",
        "schema_mode": "new",
        "sensor_columns": SENSOR_COLUMNS,
        "pressure_matrix_shape": [32, 32],
        "rows": rows,
        "file": output.name,
    }
    output.with_name("simulation_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output


if __name__ == "__main__":
    target = Path(r"F:\AFP_Capture\simulation_m3232_new_collection\SIM_PRESSURE_M3232_new_collection.csv")
    print(generate(target))
