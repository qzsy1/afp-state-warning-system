from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


APP_DIR = Path(__file__).resolve().parent
STATE_DIR = APP_DIR.parent
WORKSPACE_DIR = STATE_DIR.parent
PROJECT_ROOT = STATE_DIR.parents[2]

for path in (WORKSPACE_DIR, STATE_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from run_hierarchical_specimen_health_indicator_v13_3 import (  # noqa: E402
    NORMAL_STATE,
    PRESSURE,
    TEMP,
    abnormal_params,
    apply_physics_response,
    deterministic_severity,
)
from run_layer_specimen_health_indicator_v13_2 import (  # noqa: E402
    allocate_specimen_splits_and_states,
    build_layer_ledger,
    build_window_bank,
)
from run_physics_guided_health_indicator_v13 import (  # noqa: E402
    SENSOR_MODEL_INDICES,
    load_feature_scaler,
    load_parameter_bounds,
)


DEFAULT_RESULT_DIR = PROJECT_ROOT / "results" / "3"
DEFAULT_SPLIT_ROOT = WORKSPACE_DIR / "health_split_v3_accuracy"
DEFAULT_OUTPUT_ROOT = STATE_DIR / "outputs_tc_hi_soft_consistency_v13_8"
DEFAULT_DATA_DIR = APP_DIR / "data"

SENSORS = [
    {"id": 0, "name": "转速", "unit": "物理单位"},
    {"id": 1, "name": "位移", "unit": "物理单位"},
    {"id": 2, "name": "温度1", "unit": "℃"},
    {"id": 3, "name": "温度2", "unit": "℃"},
    {"id": 4, "name": "温度3", "unit": "℃"},
    {"id": 5, "name": "温度4", "unit": "℃"},
    {"id": 6, "name": "温度5", "unit": "℃"},
    {"id": 7, "name": "温度6", "unit": "℃"},
    {"id": 8, "name": "温度7", "unit": "℃"},
    {"id": 9, "name": "温度8", "unit": "℃"},
    {"id": 10, "name": "压实力", "unit": "物理单位"},
    {"id": 11, "name": "振动", "unit": "物理单位"},
]


def prepare(
    result_dir: Path,
    split_root: Path,
    output_root: Path,
    data_dir: Path,
    *,
    stride: int = 24,
    seed: int = 2026,
) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)

    scaler = load_feature_scaler(split_root / "train_normal.csv")
    bounds = load_parameter_bounds(split_root / "split_manifest.csv")
    bank, actual_bank, prediction_bank, _ = build_window_bank(
        result_dir,
        split_root,
        scaler,
        stride,
    )

    available = bank.groupby(
        ["p", "v", "pr", "specimen_label", "layer"], as_index=False
    ).size()
    specimen_layers = available.groupby(
        ["p", "v", "pr", "specimen_label"], as_index=False
    )["layer"].nunique()
    specimen_keys = [
        (int(row.p), int(row.v), int(row.pr), str(row.specimen_label))
        for row in specimen_layers.itertuples(index=False)
    ]
    incomplete_keys = [
        (int(row.p), int(row.v), int(row.pr), str(row.specimen_label))
        for row in specimen_layers.itertuples(index=False)
        if int(row.layer) < 5
    ]
    assignment = allocate_specimen_splits_and_states(
        specimen_keys, incomplete_keys, seed
    )
    ledger, selected_windows = build_layer_ledger(bank, assignment)

    train_mask = bank["source_origin"].astype(str).eq("train").to_numpy()
    ambient = float(np.percentile(actual_bank[train_mask][:, :, TEMP], 5.0))

    actual_sequences: list[np.ndarray] = []
    prediction_sequences: list[np.ndarray] = []
    model_input_sequences: list[np.ndarray] = []
    model_true_sequences: list[np.ndarray] = []
    rows: list[dict] = []
    source_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    for layer_row in ledger.itertuples(index=False):
        layer_id = str(layer_row.layer_sample_id)
        indices = np.asarray(selected_windows[layer_id], dtype=int)
        baseline = np.asarray(actual_bank[indices], dtype=np.float32).copy()
        prediction = np.asarray(prediction_bank[indices], dtype=np.float32)
        nominal = np.asarray(
            [float(layer_row.p), float(layer_row.v), float(layer_row.pr), float(layer_row.layer)],
            dtype=float,
        )
        state = str(layer_row.health_state)
        current = nominal.copy()

        if state != NORMAL_STATE:
            severity = deterministic_severity(str(layer_row.full_specimen_id), state, seed)
            current = abnormal_params(nominal, state, severity, bounds)
            actual = np.stack(
                [
                    apply_physics_response(window, nominal, current, state, ambient)
                    for window in baseline
                ],
                axis=0,
            ).astype(np.float32)
        else:
            severity = 0.0
            actual = baseline

        for local_index, bank_index in enumerate(indices):
            bank_row = bank.iloc[int(bank_index)]
            source_origin = str(bank_row["source_origin"])
            if source_origin not in source_cache:
                source_cache[source_origin] = (
                    np.load(result_dir / f"{source_origin}_inputx.npy", mmap_mode="r"),
                    np.load(result_dir / f"{source_origin}_trues.npy", mmap_mode="r"),
                )
            source_index = int(bank_row["source_index"])
            source_input, source_true = source_cache[source_origin]
            model_input = np.asarray(source_input[source_index], dtype=np.float32).copy()
            model_true = np.asarray(source_true[source_index], dtype=np.float32).copy()
            # The replayed actual signal may contain specimen-consistent injected
            # responses. Keep the 17-channel model tensor synchronized with the
            # 12 displayed sensor channels while preserving process/context fields.
            model_true[:, SENSOR_MODEL_INDICES] = scaler.transform_sensors(
                actual[local_index][None, ...]
            )[0].astype(np.float32)
            visual_index = len(actual_sequences)
            actual_sequences.append(actual[local_index])
            prediction_sequences.append(prediction[local_index])
            model_input_sequences.append(model_input)
            model_true_sequences.append(model_true)
            rows.append(
                {
                    "visual_index": visual_index,
                    "window_sample_id": f"{layer_id}_W{local_index:03d}",
                    "layer_sample_id": layer_id,
                    "full_specimen_id": str(layer_row.full_specimen_id),
                    "layer": int(layer_row.layer),
                    "source_bank_index": int(bank_index),
                    "true_specimen_state": state,
                    "injection_severity": float(severity),
                    "p": float(nominal[0]),
                    "v": float(nominal[1]),
                    "pr": float(nominal[2]),
                    "current_p": float(current[0]),
                    "current_v": float(current[1]),
                    "current_pr": float(current[2]),
                }
            )

    index_df = pd.DataFrame(rows)
    result_windows = pd.read_csv(output_root / "TC_HI_soft_window_results.csv")
    expected_ids = result_windows["window_sample_id"].astype(str).tolist()
    generated_ids = index_df["window_sample_id"].astype(str).tolist()
    if expected_ids != generated_ids:
        mismatch = next(
            (
                i
                for i, (expected, generated) in enumerate(zip(expected_ids, generated_ids))
                if expected != generated
            ),
            None,
        )
        raise RuntimeError(
            "可视化序列与 v13.8 窗口结果顺序不一致。"
            f" first_mismatch={mismatch}, expected={len(expected_ids)}, generated={len(generated_ids)}"
        )

    if len(result_windows) != len(index_df):
        raise RuntimeError(
            f"窗口数量不一致：v13.8={len(result_windows)}, generated={len(index_df)}"
        )

    actual_array = np.asarray(actual_sequences, dtype=np.float32)
    prediction_array = np.asarray(prediction_sequences, dtype=np.float32)
    model_input_array = np.asarray(model_input_sequences, dtype=np.float32)
    model_true_array = np.asarray(model_true_sequences, dtype=np.float32)
    if actual_array.shape != prediction_array.shape or actual_array.shape[1:] != (24, 12):
        raise RuntimeError(
            f"序列形状异常：actual={actual_array.shape}, prediction={prediction_array.shape}"
        )

    np.savez_compressed(
        data_dir / "dashboard_sequences.npz",
        actual=actual_array,
        prediction=prediction_array,
        model_input=model_input_array,
        model_true=model_true_array,
        scaler_mean=np.asarray(scaler.mean, dtype=np.float32),
        scaler_scale=np.asarray(scaler.scale, dtype=np.float32),
        sensor_model_indices=np.asarray(SENSOR_MODEL_INDICES, dtype=np.int64),
    )
    index_df.to_csv(data_dir / "dashboard_window_index.csv", index=False, encoding="utf-8-sig")

    manifest = {
        "version": "1.1.0",
        "source_method": "TC-HI + Random Forest + soft Potts/CRF consistency (v13.8)",
        "result_dir": str(result_dir.resolve()),
        "split_root": str(split_root.resolve()),
        "output_root": str(output_root.resolve()),
        "sampling_hz": 10.0,
        "window_length": 24,
        "stride": stride,
        "seed": seed,
        "ambient_temperature_reference": ambient,
        "window_count": int(actual_array.shape[0]),
        "specimen_count": int(index_df["full_specimen_id"].nunique()),
        "layer_count": int(index_df["layer_sample_id"].nunique()),
        "sensors": SENSORS,
        "notes": [
            "actual 为旧数据及试样一致的机理引导异常响应；prediction 为原 I-ModernTCN 预测。",
            "model_input/model_true 为当前检查点实时前向推理所需的17通道标准化输入。",
            "合成异常只用于算法开发，不等同于真实缺陷真值。",
            "界面可调阈值与 CAP 参数仅用于交互预览，不覆盖正式实验结果。",
        ],
    }
    (data_dir / "dashboard_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="准备 AFP 状态预警可视化数据")
    parser.add_argument("--result-dir", type=Path, default=DEFAULT_RESULT_DIR)
    parser.add_argument("--split-root", type=Path, default=DEFAULT_SPLIT_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--stride", type=int, default=24)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    manifest = prepare(
        args.result_dir,
        args.split_root,
        args.output_root,
        args.data_dir,
        stride=args.stride,
        seed=args.seed,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
