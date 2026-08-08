# -*- coding: utf-8 -*-
"""TC-HI soft five-layer consistency without hard label back-projection.

This script starts from the audited v13.7 fixed-split TC-HI outputs.  A
specimen-level type calibrator is fitted on TRAIN specimens only.  Fusion
weights are selected on VALIDATION only.  The interpolation/extrapolation
TEST specimens are locked until final evaluation.

The window/layer posterior keeps its own evidence and is combined with the
five-layer specimen posterior through a log-opinion pool (the probabilistic
form of a soft Potts/CRF consistency term).  No specimen label is copied back
to windows or layers.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "outputs_tc_hi_fixed_split_v13_7"
DEFAULT_OUTPUT = SCRIPT_DIR / "outputs_tc_hi_soft_consistency_v13_8"
VERSION = "13.8.0"

STATES = [
    "normal",
    "power_low",
    "power_high",
    "speed_low",
    "speed_high",
    "compaction_low",
    "compaction_high",
]
ANOMALY_STATES = STATES[1:]
STATE_TO_ID = {state: index for index, state in enumerate(STATES)}
PROBABILITY_COLUMNS = [f"probability_{state}" for state in ANOMALY_STATES]
SPECIMEN_FEATURE_COLUMNS = [
    "layer_health_index",
    "window_score_q95",
    "top_window_score_mean",
    "abnormal_window_fraction",
    "compaction_low_event_count",
    "compaction_high_event_count",
    *PROBABILITY_COLUMNS,
]


def _normalise(values: np.ndarray, floor: float = 1e-12) -> np.ndarray:
    values = np.clip(np.asarray(values, dtype=float), floor, None)
    return values / values.sum(axis=1, keepdims=True)


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -50.0, 50.0)))


def _logit(values: np.ndarray) -> np.ndarray:
    values = np.clip(np.asarray(values, dtype=float), 1e-8, 1.0 - 1e-8)
    return np.log(values / (1.0 - values))


def _geometric_pool(
    first: np.ndarray, second: np.ndarray, second_weight: float
) -> np.ndarray:
    logits = (
        (1.0 - float(second_weight)) * np.log(np.clip(first, 1e-12, None))
        + float(second_weight) * np.log(np.clip(second, 1e-12, None))
    )
    logits -= logits.max(axis=1, keepdims=True)
    return _normalise(np.exp(logits))


def _specimen_feature_table(layers: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for specimen_id, group in layers.groupby("full_specimen_id", sort=False):
        row: dict[str, object] = {
            "full_specimen_id": str(specimen_id),
            "dataset_split": str(group["dataset_split"].iloc[0]),
            "true_state": str(group["true_state"].iloc[0]),
        }
        for column in SPECIMEN_FEATURE_COLUMNS:
            values = group[column].to_numpy(dtype=float)
            row[f"{column}_mean"] = float(values.mean())
            row[f"{column}_std"] = float(values.std(ddof=0))
            row[f"{column}_max"] = float(values.max())
            row[f"{column}_min"] = float(values.min())
        rows.append(row)
    return pd.DataFrame(rows)


def _fit_specimen_type_calibrator(
    specimen_features: pd.DataFrame, seed: int
) -> tuple[object, np.ndarray]:
    feature_columns = [
        column
        for column in specimen_features.columns
        if column not in {"full_specimen_id", "dataset_split", "true_state"}
    ]
    values = specimen_features[feature_columns].to_numpy(dtype=float)
    train = specimen_features["dataset_split"].eq("train").to_numpy()
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(
            C=0.1,
            class_weight="balanced",
            max_iter=5000,
            random_state=seed,
        ),
    )
    model.fit(values[train], specimen_features.loc[train, "true_state"])
    probabilities = model.predict_proba(values)
    classes = list(model.classes_)
    aligned = np.column_stack([
        probabilities[:, classes.index(state)]
        if state in classes else np.zeros(len(probabilities), dtype=float)
        for state in ANOMALY_STATES
    ])
    return model, _normalise(aligned, floor=1e-8)


def _specimen_probabilities(
    specimens: pd.DataFrame,
    specimen_features: pd.DataFrame,
    calibrated_types: np.ndarray,
    gamma: float,
) -> tuple[np.ndarray, np.ndarray]:
    aligned = specimens.set_index("full_specimen_id").loc[
        specimen_features["full_specimen_id"]
    ]
    cap_types = _normalise(
        aligned[PROBABILITY_COLUMNS].to_numpy(dtype=float), floor=1e-8
    )
    type_probabilities = _geometric_pool(cap_types, calibrated_types, gamma)
    anomaly_probability = _sigmoid(
        (
            aligned["specimen_health_index"].to_numpy(dtype=float)
            - aligned["decision_threshold"].to_numpy(dtype=float)
        )
        / 0.05
    )
    override = aligned["compaction_event_override"].astype(bool).to_numpy()
    anomaly_probability = np.where(override, 0.99, anomaly_probability)
    prior_binary = aligned["predicted_binary_label"].to_numpy(dtype=int)
    anomaly_probability = np.where(
        prior_binary == 1,
        np.maximum(anomaly_probability, 0.500001),
        np.minimum(anomaly_probability, 0.499999),
    )
    return anomaly_probability, type_probabilities


def _select_gamma(
    specimens: pd.DataFrame,
    specimen_features: pd.DataFrame,
    calibrated_types: np.ndarray,
) -> tuple[float, pd.DataFrame]:
    true_ids = specimen_features["true_state"].map(STATE_TO_ID).to_numpy(dtype=int)
    validation = specimen_features["dataset_split"].eq("validation").to_numpy()
    rows = []
    best_accuracy = -1.0
    selected = 0.0
    for gamma in np.arange(0.0, 1.0001, 0.05):
        anomaly_probability, type_probability = _specimen_probabilities(
            specimens, specimen_features, calibrated_types, float(gamma)
        )
        predicted = np.where(
            anomaly_probability >= 0.5,
            1 + type_probability.argmax(axis=1),
            0,
        )
        accuracy = float(accuracy_score(true_ids[validation], predicted[validation]))
        rows.append({
            "gamma": float(gamma),
            "validation_specimen_seven_state_accuracy": accuracy,
        })
        if accuracy > best_accuracy + 1e-12:
            best_accuracy = accuracy
            selected = float(gamma)
    return selected, pd.DataFrame(rows)


def _soft_predict(
    frame: pd.DataFrame,
    health_column: str,
    threshold: np.ndarray | float,
    specimen_probability: dict[str, float],
    specimen_type_probability: dict[str, np.ndarray],
    binary_context_weight: float,
    type_context_weight: float,
    type_temperature: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    local_probability = _sigmoid(
        (
            frame[health_column].to_numpy(dtype=float)
            - np.asarray(threshold, dtype=float)
        )
        / 0.05
    )
    local_type = _normalise(
        frame[PROBABILITY_COLUMNS].to_numpy(dtype=float), floor=1e-8
    )
    specimen_ids = frame["full_specimen_id"].astype(str)
    context_probability = np.asarray(
        [specimen_probability[value] for value in specimen_ids], dtype=float
    )
    context_type = np.stack([
        specimen_type_probability[value] for value in specimen_ids
    ])
    tempered_logits = np.log(np.clip(context_type, 1e-12, None)) / float(
        type_temperature
    )
    tempered_logits -= tempered_logits.max(axis=1, keepdims=True)
    tempered_context = _normalise(np.exp(tempered_logits))
    fused_probability = _sigmoid(
        (1.0 - binary_context_weight) * _logit(local_probability)
        + binary_context_weight * _logit(context_probability)
    )
    fused_type = _geometric_pool(
        local_type, tempered_context, type_context_weight
    )
    predicted_ids = np.where(
        fused_probability >= 0.5, 1 + fused_type.argmax(axis=1), 0
    )
    return predicted_ids, fused_probability, fused_type


def _scores(
    true_ids: np.ndarray, predicted_ids: np.ndarray
) -> dict[str, float]:
    true_binary = (true_ids > 0).astype(int)
    predicted_binary = (predicted_ids > 0).astype(int)
    return {
        "binary_accuracy": float(accuracy_score(true_binary, predicted_binary)),
        "balanced_accuracy": float(
            balanced_accuracy_score(true_binary, predicted_binary)
        ),
        "seven_state_accuracy": float(accuracy_score(true_ids, predicted_ids)),
    }


def _evaluate_candidate(
    windows: pd.DataFrame,
    layers: pd.DataFrame,
    specimens: pd.DataFrame,
    specimen_features: pd.DataFrame,
    specimen_anomaly_probability: np.ndarray,
    specimen_type_probability: np.ndarray,
    binary_context_weight: float,
    type_context_weight: float,
    type_temperature: float,
) -> dict[str, object]:
    probability_map = dict(zip(
        specimen_features["full_specimen_id"].astype(str),
        specimen_anomaly_probability,
    ))
    type_map = dict(zip(
        specimen_features["full_specimen_id"].astype(str),
        specimen_type_probability,
    ))
    window_ids, window_probability, window_types = _soft_predict(
        windows,
        "window_health_index",
        0.366197916666667,
        probability_map,
        type_map,
        binary_context_weight,
        type_context_weight,
        type_temperature,
    )
    layer_ids, layer_probability, layer_types = _soft_predict(
        layers,
        "layer_health_index",
        layers["decision_threshold"].to_numpy(dtype=float),
        probability_map,
        type_map,
        binary_context_weight,
        type_context_weight,
        type_temperature,
    )
    specimen_ids = np.where(
        specimen_anomaly_probability >= 0.5,
        1 + specimen_type_probability.argmax(axis=1),
        0,
    )
    return {
        "window_ids": window_ids,
        "window_probability": window_probability,
        "window_types": window_types,
        "layer_ids": layer_ids,
        "layer_probability": layer_probability,
        "layer_types": layer_types,
        "specimen_ids": specimen_ids,
    }


def _selection_scores(
    prediction: dict[str, object],
    windows: pd.DataFrame,
    layers: pd.DataFrame,
    specimen_features: pd.DataFrame,
) -> list[float]:
    window_mask = (
        windows["dataset_split"].eq("validation")
        & windows["window_training_eligible"].astype(bool)
    ).to_numpy()
    layer_mask = layers["dataset_split"].eq("validation").to_numpy()
    specimen_mask = specimen_features["dataset_split"].eq("validation").to_numpy()
    window_true = windows["true_specimen_state"].map(STATE_TO_ID).to_numpy(dtype=int)
    layer_true = layers["true_state"].map(STATE_TO_ID).to_numpy(dtype=int)
    specimen_true = specimen_features["true_state"].map(STATE_TO_ID).to_numpy(
        dtype=int
    )
    window_score = _scores(
        window_true[window_mask],
        np.asarray(prediction["window_ids"])[window_mask],
    )
    layer_score = _scores(
        layer_true[layer_mask],
        np.asarray(prediction["layer_ids"])[layer_mask],
    )
    specimen_score = _scores(
        specimen_true[specimen_mask],
        np.asarray(prediction["specimen_ids"])[specimen_mask],
    )
    return [
        window_score["balanced_accuracy"],
        window_score["seven_state_accuracy"],
        layer_score["balanced_accuracy"],
        layer_score["seven_state_accuracy"],
        specimen_score["balanced_accuracy"],
        specimen_score["seven_state_accuracy"],
    ]


def _select_soft_parameters(
    windows: pd.DataFrame,
    layers: pd.DataFrame,
    specimens: pd.DataFrame,
    specimen_features: pd.DataFrame,
    specimen_anomaly_probability: np.ndarray,
    specimen_type_probability: np.ndarray,
) -> tuple[dict[str, float], pd.DataFrame]:
    temperatures = [0.01, 0.02, 0.03, 0.05, 0.07, 0.1, 0.15, 0.2, 0.3, 0.5, 0.7, 1.0]
    weights = np.arange(0.0, 1.0001, 0.025)
    # Candidate selection is deliberately computed on validation rows only.
    # Besides making the lock auditable, this avoids repeatedly scoring the
    # much larger test/train tables during the grid search.
    validation_windows = windows.loc[
        windows["dataset_split"].eq("validation")
    ].reset_index(drop=True)
    validation_layers = layers.loc[
        layers["dataset_split"].eq("validation")
    ].reset_index(drop=True)
    validation_feature_mask = specimen_features["dataset_split"].eq(
        "validation"
    ).to_numpy()
    validation_features = specimen_features.loc[
        validation_feature_mask
    ].reset_index(drop=True)
    validation_specimen_probability = specimen_anomaly_probability[
        validation_feature_mask
    ]
    validation_specimen_types = specimen_type_probability[
        validation_feature_mask
    ]
    feasible: list[tuple[tuple[float, float], dict[str, float]]] = []
    rows = []
    for temperature in temperatures:
        for binary_weight in weights:
            for type_weight in weights:
                prediction = _evaluate_candidate(
                    validation_windows,
                    validation_layers,
                    specimens,
                    validation_features,
                    validation_specimen_probability,
                    validation_specimen_types,
                    float(binary_weight),
                    float(type_weight),
                    float(temperature),
                )
                scores = _selection_scores(
                    prediction,
                    validation_windows,
                    validation_layers,
                    validation_features,
                )
                row = {
                    "type_temperature": float(temperature),
                    "binary_context_weight": float(binary_weight),
                    "type_context_weight": float(type_weight),
                    "validation_window_balanced_accuracy": scores[0],
                    "validation_window_seven_state_accuracy": scores[1],
                    "validation_layer_balanced_accuracy": scores[2],
                    "validation_layer_seven_state_accuracy": scores[3],
                    "validation_specimen_balanced_accuracy": scores[4],
                    "validation_specimen_seven_state_accuracy": scores[5],
                    "validation_minimum_metric": min(scores),
                    "all_validation_metrics_above_90_percent": min(scores) > 0.90,
                }
                rows.append(row)
                if min(scores) > 0.90:
                    key = (
                        max(float(binary_weight), float(type_weight)),
                        float(binary_weight) + float(type_weight),
                    )
                    feasible.append((key, row))
    if not feasible:
        raise RuntimeError(
            "No validation-only soft-consistency configuration exceeded 90%."
        )
    selected = min(feasible, key=lambda item: item[0])[1].copy()
    return {
        "type_temperature": selected["type_temperature"],
        "binary_context_weight": selected["binary_context_weight"],
        "type_context_weight": selected["type_context_weight"],
    }, pd.DataFrame(rows)


def _attach_predictions(
    frame: pd.DataFrame,
    predicted_ids: np.ndarray,
    probability: np.ndarray,
    type_probability: np.ndarray,
    true_column: str,
    prefix: str,
) -> pd.DataFrame:
    output = frame.copy()
    output[f"{prefix}_anomaly_probability"] = probability
    for index, state in enumerate(ANOMALY_STATES):
        output[f"{prefix}_probability_{state}"] = type_probability[:, index]
    output["soft_predicted_binary_label"] = (predicted_ids > 0).astype(int)
    output["soft_predicted_state"] = [STATES[index] for index in predicted_ids]
    output["soft_prediction_correct"] = (
        output[true_column].astype(str).to_numpy()
        == output["soft_predicted_state"].to_numpy()
    )
    output["soft_decision_source"] = (
        "local_TC-HI_plus_soft_five-layer_log-opinion-pool"
    )
    return output


def _metric_rows(
    windows: pd.DataFrame,
    layers: pd.DataFrame,
    specimens: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    configurations: Iterable[
        tuple[str, pd.DataFrame, str, str, np.ndarray]
    ] = [
        (
            "window",
            windows,
            "true_specimen_state",
            "soft_predicted_state",
            windows["window_training_eligible"].astype(bool).to_numpy(),
        ),
        ("layer", layers, "true_state", "soft_predicted_state", np.ones(len(layers), bool)),
        (
            "specimen",
            specimens,
            "true_state",
            "soft_predicted_state",
            np.ones(len(specimens), bool),
        ),
    ]
    for dataset_name, split_names in [
        ("train_as_test", ["train"]),
        ("validation", ["validation"]),
        ("train_validation_as_test", ["train", "validation"]),
        ("test_all", ["test_interpolation", "test_extrapolation"]),
    ]:
        for level, frame, true_column, prediction_column, eligible in configurations:
            mask = frame["dataset_split"].isin(split_names).to_numpy() & eligible
            true_ids = frame.loc[mask, true_column].map(STATE_TO_ID).to_numpy(dtype=int)
            predicted_ids = frame.loc[mask, prediction_column].map(STATE_TO_ID).to_numpy(
                dtype=int
            )
            scores = _scores(true_ids, predicted_ids)
            rows.append({
                "dataset": dataset_name,
                "level": level,
                "n_samples": int(mask.sum()),
                "errors": int(np.sum(true_ids != predicted_ids)),
                **scores,
                "hard_specimen_label_back_projection": False,
                "method": "TC-HI + soft Potts/CRF log-opinion pool",
            })
    return pd.DataFrame(rows)


def _make_figures(output: Path, metrics: pd.DataFrame) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.family"] = "Microsoft YaHei"
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei", "SimHei", "DejaVu Sans"
    ]
    plt.rcParams["axes.unicode_minus"] = False
    figure_dir = output / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    test = metrics.loc[metrics["dataset"].eq("test_all")].set_index("level")
    levels = ["window", "layer", "specimen"]
    labels = ["窗口", "层", "试样"]
    binary = test.loc[levels, "balanced_accuracy"].to_numpy(dtype=float)
    state = test.loc[levels, "seven_state_accuracy"].to_numpy(dtype=float)
    x = np.arange(3)
    figure, axis = plt.subplots(figsize=(8.6, 5.2))
    axis.bar(x - 0.18, binary, 0.36, color="#2F5597", label="二分类平衡准确率")
    axis.bar(x + 0.18, state, 0.36, color="#D97A2B", label="七状态准确率")
    axis.axhline(0.90, color="#B33A3A", linestyle="--", linewidth=1.2,
                 label="90%目标线")
    axis.set_xticks(x, labels)
    axis.set_ylim(0.0, 1.08)
    axis.set_ylabel("准确率")
    axis.set_title("TC-HI软五层一致性：锁定测试集三级结果")
    axis.grid(axis="y", color="#E5E7EB", linewidth=0.7)
    axis.legend(frameon=False, ncol=3, loc="lower center")
    for offset, values in [(-0.18, binary), (0.18, state)]:
        for position, value in zip(x + offset, values):
            axis.text(position, value + 0.014, f"{value:.3f}",
                      ha="center", va="bottom", fontsize=10)
    figure.tight_layout()
    paths = []
    for extension, dpi in [("png", 300), ("pdf", 300)]:
        path = figure_dir / f"Fig21_TC_HI_Soft_Consistency_Accuracy.{extension}"
        figure.savefig(path, dpi=dpi, bbox_inches="tight")
        paths.append(str(path))
    plt.close(figure)
    return paths


def run(input_dir: Path, output: Path, seed: int) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    windows = pd.read_csv(input_dir / "hierarchical_window_state_results.csv")
    layers = pd.read_csv(input_dir / "hierarchical_layer_state_results_130.csv")
    specimens = pd.read_csv(input_dir / "hierarchical_specimen_state_results_26.csv")
    specimen_features = _specimen_feature_table(layers)
    _, calibrated_types = _fit_specimen_type_calibrator(specimen_features, seed)
    gamma, gamma_table = _select_gamma(
        specimens, specimen_features, calibrated_types
    )
    specimen_probability, specimen_types = _specimen_probabilities(
        specimens, specimen_features, calibrated_types, gamma
    )
    selected, candidates = _select_soft_parameters(
        windows,
        layers,
        specimens,
        specimen_features,
        specimen_probability,
        specimen_types,
    )
    prediction = _evaluate_candidate(
        windows,
        layers,
        specimens,
        specimen_features,
        specimen_probability,
        specimen_types,
        selected["binary_context_weight"],
        selected["type_context_weight"],
        selected["type_temperature"],
    )
    windows = _attach_predictions(
        windows,
        np.asarray(prediction["window_ids"]),
        np.asarray(prediction["window_probability"]),
        np.asarray(prediction["window_types"]),
        "true_specimen_state",
        "soft_window",
    )
    layers = _attach_predictions(
        layers,
        np.asarray(prediction["layer_ids"]),
        np.asarray(prediction["layer_probability"]),
        np.asarray(prediction["layer_types"]),
        "true_state",
        "soft_layer",
    )
    aligned_specimens = specimens.set_index("full_specimen_id").loc[
        specimen_features["full_specimen_id"]
    ].reset_index()
    specimens = _attach_predictions(
        aligned_specimens,
        np.asarray(prediction["specimen_ids"]),
        specimen_probability,
        specimen_types,
        "true_state",
        "soft_specimen",
    )
    metrics = _metric_rows(windows, layers, specimens)
    figures = _make_figures(output, metrics)

    windows.to_csv(
        output / "TC_HI_soft_window_results.csv", index=False, encoding="utf-8-sig"
    )
    layers.to_csv(
        output / "TC_HI_soft_layer_results_130.csv", index=False, encoding="utf-8-sig"
    )
    specimens.to_csv(
        output / "TC_HI_soft_specimen_results_26.csv",
        index=False,
        encoding="utf-8-sig",
    )
    metrics.to_csv(
        output / "TC_HI_soft_level_metrics.csv", index=False, encoding="utf-8-sig"
    )
    gamma_table.to_csv(
        output / "TC_HI_soft_gamma_validation.csv",
        index=False,
        encoding="utf-8-sig",
    )
    candidates.to_csv(
        output / "TC_HI_soft_parameter_validation.csv",
        index=False,
        encoding="utf-8-sig",
    )
    test_metrics = metrics.loc[metrics["dataset"].eq("test_all")]
    n_test_specimens = int(
        test_metrics.loc[test_metrics["level"].eq("specimen"), "n_samples"].iloc[0]
    )
    z_value = 1.959963984540054
    denominator = 1.0 + z_value**2 / n_test_specimens
    wilson_lower = (
        (1.0 + z_value**2 / (2.0 * n_test_specimens)) / denominator
        - z_value
        * math.sqrt(z_value**2 / (4.0 * n_test_specimens**2))
        / denominator
    )
    summary = {
        "version": VERSION,
        "method": "TC-HI + soft Potts/CRF log-opinion pool",
        "source_result": str(input_dir.resolve()),
        "cross_validation_used": False,
        "selection_split": "validation_only",
        "locked_test": True,
        "hard_specimen_label_back_projection": False,
        "specimen_type_calibration": (
            "48 specimen-level statistics; StandardScaler + multinomial "
            "LogisticRegression(C=0.1); fitted on 15 train specimens only"
        ),
        "selected_gamma": gamma,
        **selected,
        "all_locked_test_metrics_above_90_percent": bool(
            (test_metrics[[
                "binary_accuracy",
                "balanced_accuracy",
                "seven_state_accuracy",
            ]] > 0.90).to_numpy().all()
        ),
        "test_metrics": json.loads(test_metrics.to_json(orient="records")),
        "test_specimens": n_test_specimens,
        "seven_of_seven_wilson_95_lower": float(wilson_lower),
        "statistical_warning": (
            "Seven-state specimen test accuracy has increments of 1/7. "
            "Above 90% therefore requires 7/7; this is not evidence of a "
            "stable 100% real-world accuracy."
        ),
        "figures": figures,
    }
    (output / "TC_HI_soft_method_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TC-HI soft five-layer Potts/CRF consistency"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=2026)
    options = parser.parse_args()
    output = run(options.input.resolve(), options.output.resolve(), options.seed)
    summary = json.loads(
        (output / "TC_HI_soft_method_summary.json").read_text(encoding="utf-8")
    )
    print(f"Method: {summary['method']}")
    print("Cross-validation used: False")
    print("Hard specimen label back-projection: False")
    print(
        "All locked-test metrics > 90%: "
        f"{summary['all_locked_test_metrics_above_90_percent']}"
    )
    print(f"Output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
