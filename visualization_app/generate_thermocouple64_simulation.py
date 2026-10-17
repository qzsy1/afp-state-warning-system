from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import sys

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from acquisition import ACQUISITION_SCHEMAS, PROCESS_PARAMETER_COLUMNS  # noqa: E402
from thermocouple64 import THERMOCOUPLE64_CHANNELS  # noqa: E402


PACKAGE_ROOT = Path(__file__).resolve().parent / "simulation_packages" / "thermocouple64_v13_0"
CORE_CHANNELS = [
    name for name in ACQUISITION_SCHEMAS["new_collection_v13_0"]["sensors"]
    if name not in THERMOCOUPLE64_CHANNELS
]
CHANNELS = [*CORE_CHANNELS, *THERMOCOUPLE64_CHANNELS]
IDENTITY_COLUMNS = ["run_id", "specimen_id", "condition_id", "replicate", "layer_id"]


def _row(index: int, *, layer: int = 1, missing_group: int | None = None) -> dict[str, object]:
    time_s = index / 10.0
    phase = index / 20.0
    process = {
        "initial_compaction_force_N": 400.0,
        "placement_speed_mm_s": 80.0,
        "pid_angle_deg": 5.0,
        "temperature_setpoint_C": 360.0,
    }
    row: dict[str, object] = {
        "时间": round(time_s, 3),
        "温度": round(356.0 + 2.2 * math.sin(phase), 5),
        "压力": round(400.0 + 8.0 * math.sin(phase / 2), 5),
        "薄膜压力": round(0.42 + 0.03 * math.cos(phase), 5),
        "ROI平均温度": round(352.0 + 1.8 * math.sin(phase + 0.2), 5),
        "张力": round(82.0 + 1.5 * math.cos(phase / 3), 5),
        "线速度": round(80.0 + 0.8 * math.sin(phase / 4), 5),
        "ABB_X": round(100.0 + index * 0.02, 5),
        "ABB_Y": round(30.0 + 0.5 * math.sin(phase / 5), 5),
        "ABB_Z": round(220.0 + layer * 0.18 + 0.1 * math.cos(phase / 5), 5),
        "应变1": round(450.0 + 35.0 * math.sin(phase), 5),
        "应变2": round(900.0 + 35.0 * math.sin(phase + 0.2), 5),
        "应变3": round(1350.0 + 35.0 * math.sin(phase + 0.4), 5),
        "应变4": round(1800.0 + 35.0 * math.sin(phase + 0.6), 5),
        **process,
        "run_id": f"TC64_SIM_L{layer}",
        "specimen_id": "TC64_SIM_SPECIMEN",
        "condition_id": "TC64_SIM_10HZ",
        "replicate": 1,
        "layer_id": layer,
    }
    for channel_index, name in enumerate(THERMOCOUPLE64_CHANNELS, start=1):
        group = (channel_index - 1) // 8 + 1
        if group == missing_group:
            row[name] = ""
            continue
        spatial_offset = (channel_index - 32.5) * 0.11
        row[name] = round(
            348.0 + 0.82 * group + spatial_offset
            + 1.3 * math.sin(phase + channel_index * 0.018)
            + (layer - 1) * 0.7,
            5,
        )
    return row


def generate(output_root: Path = PACKAGE_ROOT) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    fieldnames = ["时间", *CHANNELS, *PROCESS_PARAMETER_COLUMNS, *IDENTITY_COLUMNS]
    for filename, count in (("quick_240.csv", 240), ("endurance_6000.csv", 6000)):
        with (output_root / filename).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(_row(index) for index in range(count))

    layer_root = output_root / "heterogeneous_layers"
    layer_root.mkdir(parents=True, exist_ok=True)
    for layer, missing_group in ((1, None), (2, 3), (3, 6)):
        layer_path = layer_root / f"layer_{layer}.csv"
        layer_fields = [
            "时间", *CORE_CHANNELS,
            *[name for name in THERMOCOUPLE64_CHANNELS
              if missing_group is None or (int(name.removeprefix("温度")) - 1) // 8 + 1 != missing_group],
            *PROCESS_PARAMETER_COLUMNS, *IDENTITY_COLUMNS,
        ]
        with layer_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=layer_fields)
            writer.writeheader()
            for index in range(240):
                row = _row(index, layer=layer, missing_group=missing_group)
                writer.writerow({key: row[key] for key in layer_fields})

    manifest = {
        "dataset_schema": "new_collection_v13_0",
        "simulation_only": True,
        "raw_channels": CHANNELS,
        "raw_channel_count": len(CHANNELS),
        "samples": {"quick_240.csv": 240, "endurance_6000.csv": 6000},
        "heterogeneous_layers": {
            "layer_1.csv": "all channels present",
            "layer_2.csv": "温度17 through 温度24 absent",
            "layer_3.csv": "温度41 through 温度48 absent",
        },
        "sampling_rate_hz": 10,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    generate()
