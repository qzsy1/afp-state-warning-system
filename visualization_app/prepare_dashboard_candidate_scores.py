from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import joblib


APP_DIR = Path(__file__).resolve().parent
STATE_DIR = APP_DIR.parent
WORKSPACE_DIR = STATE_DIR.parent
PROJECT_ROOT = STATE_DIR.parents[2]
for path in (WORKSPACE_DIR, STATE_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from run_hierarchical_specimen_health_indicator_v13_3 import (  # noqa: E402
    MODEL_ORDER,
    TEMP,
    _candidate_specs,
    _fit_two_stage,
    _evaluate_candidate,
    _hierarchical_selection_score,
    _predict_two_stage,
    _select_cap_configuration,
    build_hierarchical_window_features,
)
from run_layer_specimen_health_indicator_v13_2 import (  # noqa: E402
    allocate_specimen_splits_and_states,
    build_layer_ledger,
    build_window_bank,
)
from run_physics_guided_health_indicator_v13 import (  # noqa: E402
    load_feature_scaler,
    load_parameter_bounds,
)
from run_full_prediction_to_warning_v11_5 import (  # noqa: E402
    fit_coherence_scale_floor,
)
from online_health_features import (  # noqa: E402
    OnlineWindowFeatureEngine,
    fit_literature_transformers,
)


RESULT_DIR = PROJECT_ROOT / "results" / "3"
SPLIT_ROOT = WORKSPACE_DIR / "health_split_v3_accuracy"
COMPARISON_CSV = (
    STATE_DIR
    / "outputs_tc_hi_cap_mil_v13_6_final"
    / "hierarchical_sensor_only_HI_candidates.csv"
)
WINDOW_CSV = (
    STATE_DIR
    / "outputs_tc_hi_soft_consistency_v13_8"
    / "TC_HI_soft_window_results.csv"
)
DATA_DIR = APP_DIR / "data"


def build_metadata(seed: int, stride: int):
    scaler = load_feature_scaler(SPLIT_ROOT / "train_normal.csv")
    bounds = load_parameter_bounds(SPLIT_ROOT / "split_manifest.csv")
    bank, actual_bank, prediction_bank, _ = build_window_bank(
        RESULT_DIR, SPLIT_ROOT, scaler, stride
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
    features, metadata, _ = build_hierarchical_window_features(
        ledger,
        selected_windows,
        actual_bank,
        prediction_bank,
        scaler,
        bounds,
        ambient,
        seed,
    )
    return features, metadata


def prepare(seed: int = 2026, stride: int = 24) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    features, metadata = build_metadata(seed, stride)
    reference_windows = pd.read_csv(WINDOW_CSV)
    if metadata["window_sample_id"].astype(str).tolist() != reference_windows[
        "window_sample_id"
    ].astype(str).tolist():
        raise RuntimeError("候选模型窗口顺序与v13.8窗口顺序不一致。")

    sequence_arrays = np.load(
        DATA_DIR / "dashboard_sequences.npz", allow_pickle=False
    )
    actual_sequences = np.asarray(sequence_arrays["actual"], dtype=float)
    prediction_sequences = np.asarray(
        sequence_arrays["prediction"], dtype=float
    )
    scaler = load_feature_scaler(SPLIT_ROOT / "train_normal.csv")
    normal_mask = (
        metadata["dataset_split"].eq("train")
        & metadata["true_specimen_state"].eq("normal")
        & metadata["window_training_eligible"].astype(bool)
    ).to_numpy(dtype=bool)
    coherence_floor = fit_coherence_scale_floor(
        scaler.transform_sensors(actual_sequences[normal_mask]),
        scaler.transform_sensors(prediction_sequences[normal_mask]),
    )
    transformers = fit_literature_transformers(
        features["response_plus_residual"],
        actual_sequences,
        metadata,
        seed,
    )
    online_engine = OnlineWindowFeatureEngine(
        scaler=scaler,
        ambient=float(
            json.loads(
                (DATA_DIR / "dashboard_manifest.json").read_text(encoding="utf-8")
            )["ambient_temperature_reference"]
        ),
        coherence_floor=coherence_floor,
        transformers=transformers,
    )
    # Train and evaluate the warning models on the exact feature representation
    # used online. This prevents float-precision drift in low-variance channels
    # (notably Wasserstein distance features).
    features = online_engine.transform(actual_sequences, prediction_sequences)
    joblib.dump(
        {
            "version": "1.1.0",
            "coherence_floor": np.asarray(coherence_floor, dtype=float),
            "transformers": transformers,
        },
        DATA_DIR / "online_feature_artifacts.joblib",
        compress=3,
    )

    split = metadata["dataset_split"].astype(str).to_numpy()
    train = split == "train"
    test = np.char.startswith(split.astype(str), "test_")
    score_arrays: list[np.ndarray] = []
    type_arrays: list[np.ndarray] = []
    catalog_rows: list[dict] = []
    model_dir = DATA_DIR / "candidate_models"
    model_dir.mkdir(parents=True, exist_ok=True)

    for family, feature_key, model_kind in _candidate_specs():
        model_seed = seed + 1009 * MODEL_ORDER.index(model_kind)
        binary_model, type_model = _fit_two_stage(
            features[feature_key], metadata, train, model_kind, model_seed
        )
        scores, type_probabilities = _predict_two_stage(
            binary_model, type_model, features[feature_key]
        )
        aggregation, thresholds, val_window, val_layer, val_specimen = _select_cap_configuration(
            metadata, scores, type_probabilities
        )
        test_window, test_layer, test_specimen, *_ = _evaluate_candidate(
            metadata,
            scores,
            type_probabilities,
            thresholds,
            test,
            aggregation,
        )
        validation_selection_score = _hierarchical_selection_score(
            val_layer, val_specimen
        )
        score_arrays.append(np.asarray(scores, dtype=np.float32))
        type_arrays.append(np.asarray(type_probabilities, dtype=np.float32))
        candidate_index = len(catalog_rows)
        model_file = f"candidate_{candidate_index:02d}.joblib"
        joblib.dump(
            {
                "binary_model": binary_model,
                "type_model": type_model,
                "feature_key": feature_key,
            },
            model_dir / model_file,
            compress=3,
        )
        catalog_rows.append(
            {
                "candidate_index": len(catalog_rows),
                "model_file": model_file,
                "indicator_family": family,
                "feature_key": feature_key,
                "model_kind": model_kind,
                "candidate": f"{family} | {model_kind}",
                "window_threshold": float(thresholds[0]),
                "layer_threshold": float(thresholds[1]),
                "specimen_threshold": float(thresholds[2]),
                "cap_rho": float(aggregation.rho),
                "validation_selection_score": float(validation_selection_score),
                "validation_window_balanced_accuracy": float(
                    val_window["balanced_accuracy"]
                ),
                "validation_layer_balanced_accuracy": float(
                    val_layer["balanced_accuracy"]
                ),
                "validation_specimen_balanced_accuracy": float(
                    val_specimen["balanced_accuracy"]
                ),
                "test_window_balanced_accuracy": float(
                    test_window["balanced_accuracy"]
                ),
                "test_layer_balanced_accuracy": float(
                    test_layer["balanced_accuracy"]
                ),
                "test_specimen_balanced_accuracy": float(
                    test_specimen["balanced_accuracy"]
                ),
            }
        )

    catalog = pd.DataFrame(catalog_rows)
    catalog["recommended_for_indicator"] = False
    recommended_indices = (
        catalog.sort_values(
            ["indicator_family", "validation_selection_score"],
            ascending=[True, False],
        )
        .groupby("indicator_family", sort=False)
        .head(1)
        .index
    )
    catalog.loc[recommended_indices, "recommended_for_indicator"] = True
    catalog.to_csv(
        DATA_DIR / "dashboard_candidate_catalog.csv",
        index=False,
        encoding="utf-8-sig",
    )
    np.savez_compressed(
        DATA_DIR / "dashboard_candidate_scores.npz",
        anomaly_scores=np.stack(score_arrays, axis=0),
        type_probabilities=np.stack(type_arrays, axis=0),
    )
    np.savez_compressed(
        DATA_DIR / "dashboard_candidate_features.npz",
        **{
            key: np.asarray(values, dtype=np.float32)
            for key, values in features.items()
        },
    )
    summary = {
        "candidate_count": int(len(catalog)),
        "indicator_count": int(catalog["indicator_family"].nunique()),
        "model_count": int(catalog["model_kind"].nunique()),
        "window_count": int(len(metadata)),
        "recommendation_rule": "maximum validation_selection_score within indicator",
        "seed": seed,
        "stride": stride,
    }
    (DATA_DIR / "dashboard_candidate_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="生成48种健康指标×模型的窗口异常分数")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--stride", type=int, default=24)
    args = parser.parse_args()
    print(json.dumps(prepare(args.seed, args.stride), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
