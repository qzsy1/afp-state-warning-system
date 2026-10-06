from __future__ import annotations

import csv
import math
from pathlib import Path

from acquisition import NEW_COLLECTION_SENSOR_COLUMNS, PROCESS_PARAMETER_COLUMNS


PACKAGE_ROOT = Path(__file__).resolve().parent / "simulation_packages"


def _row(index: int) -> dict[str, object]:
    phase = index / 20.0
    values: dict[str, object] = {
        "时间": round(index / 10.0, 3),
        "温度": round(356.0 + 2.5 * math.sin(phase), 4),
        "压力": round(400.0 + 8.0 * math.sin(phase / 2.0), 4),
        "薄膜压力": round(0.42 + 0.03 * math.cos(phase), 5),
        "ROI平均温度": round(352.0 + 2.0 * math.sin(phase + 0.2), 4),
        "张力": round(82.0 + 1.5 * math.cos(phase / 3.0), 4),
        "线速度": round(80.0 + 0.8 * math.sin(phase / 4.0), 4),
        "ABB_X": round(100.0 + index * 0.02, 4),
        "ABB_Y": round(30.0 + 0.5 * math.sin(phase / 5.0), 4),
        "ABB_Z": round(220.0 + 0.5 * math.cos(phase / 5.0), 4),
        "initial_compaction_force_N": 400.0,
        "placement_speed_mm_s": 80.0,
        "pid_angle_deg": 5.0,
        "temperature_setpoint_C": 360.0,
        "run_id": "SYNTHETIC_VALIDATION",
        "specimen_id": "SYNTHETIC_ONLY",
        "condition_id": "SYNTHETIC_10HZ",
        "replicate": 1,
        "layer_id": 0,
    }
    for sensor_index in range(1, 9):
        values[f"温度{sensor_index}"] = round(
            350.0 + sensor_index + 1.2 * math.sin(phase + sensor_index / 10.0),
            4,
        )
    return values


def generate() -> None:
    PACKAGE_ROOT.mkdir(parents=True, exist_ok=True)
    columns = [
        "时间",
        *NEW_COLLECTION_SENSOR_COLUMNS,
        *PROCESS_PARAMETER_COLUMNS,
        "run_id",
        "specimen_id",
        "condition_id",
        "replicate",
        "layer_id",
    ]
    for filename, points in (
        ("afp_synthetic_quick_240.csv", 240),
        ("afp_synthetic_endurance_6000.csv", 6000),
    ):
        path = PACKAGE_ROOT / filename
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for index in range(points):
                writer.writerow(_row(index))


if __name__ == "__main__":
    generate()
