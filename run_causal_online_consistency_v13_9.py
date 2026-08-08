# -*- coding: utf-8 -*-
"""Validation-selected causal online consistency for AFP state warning.

Only windows and layers already acquired at a decision instant are used.
Training specimens fit the context model, validation specimens select the
model/fusion parameters, and interpolation/extrapolation test specimens remain
locked until the final evaluation.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from rerun_tc_hi_soft_consistency_v13_8 import (
    _fit_specimen_type_calibrator,
    _select_gamma,
    _specimen_feature_table,
    _specimen_probabilities,
)

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_DIR = SCRIPT_DIR / "outputs_tc_hi_fixed_split_v13_7"
OFFLINE_DIR = SCRIPT_DIR / "outputs_tc_hi_soft_consistency_v13_8"
OUTPUT_DIR = SCRIPT_DIR / "outputs_causal_online_consistency_v13_9"
VERSION = "13.9.0"

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
SUMMARY_COLUMNS = [
    "layer_health_index",
    "window_score_q95",
    "top_window_score_mean",
    "abnormal_window_fraction",
    "compaction_low_event_count",
    "compaction_high_event_count",
    *PROBABILITY_COLUMNS,
]
WINDOW_THRESHOLD = 0.366197916666667
CAP_RHO = 0.50
TOP_WINDOW_FRACTION = 0.08
MIN_TOP_WINDOWS = 2


def _normalise(values: np.ndarray, floor: float = 1e-10) -> np.ndarray:
    values = np.clip(np.asarray(values, dtype=float), floor, None)
    return values / values.sum(axis=1, keepdims=True)


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -50.0, 50.0)))


def _cap_pool(values: np.ndarray, rho: float = CAP_RHO) -> tuple[float, np.ndarray]:
    values = np.asarray(values, dtype=float)
    if len(values) == 1:
        return float(values[0]), np.ones(1, dtype=float)
    alpha = float(rho) * math.log(max(len(values) - 1, 1))
    logits = alpha * values
    logits -= logits.max()
    weights = np.exp(logits)
    weights /= weights.sum()
    return float(np.dot(weights, values)), weights


def _partial_layer_summary(group: pd.DataFrame) -> dict[str, float]:
    scores = group["window_health_index"].to_numpy(dtype=float)
    type_matrix = _normalise(group[PROBABILITY_COLUMNS].to_numpy(dtype=float))
    top_count = min(
        len(scores),
        max(MIN_TOP_WINDOWS, int(np.ceil(TOP_WINDOW_FRACTION * len(scores)))),
    )
    top_indices = np.argsort(scores)[-top_count:]
    tail_weights = np.maximum(scores[top_indices], 1e-6)
    tail_weights /= tail_weights.sum()
    type_vector = np.average(
        type_matrix[top_indices], axis=0, weights=tail_weights
    )
    type_vector /= max(float(type_vector.sum()), 1e-12)
    health, _ = _cap_pool(scores)
    compaction = group["predicted_compaction_event"].astype(bool).to_numpy()
    predicted_type = group["predicted_anomaly_type"].astype(str).to_numpy()
    result = {
        "layer_health_index": health,
        "window_score_q95": float(np.percentile(scores, 95)),
        "top_window_score_mean": float(scores[top_indices].mean()),
        "abnormal_window_fraction": float(np.mean(scores >= WINDOW_THRESHOLD)),
        "compaction_low_event_count": int(
            np.sum(compaction & (predicted_type == "compaction_low"))
        ),
        "compaction_high_event_count": int(
            np.sum(compaction & (predicted_type == "compaction_high"))
        ),
    }
    result.update(
        {
            column: float(type_vector[index])
            for index, column in enumerate(PROBABILITY_COLUMNS)
        }
    )
    result["legacy_fixed_tail_health_index"] = (
        0.35 * result["window_score_q95"]
        + 0.45 * result["top_window_score_mean"]
        + 0.20 * result["abnormal_window_fraction"]
    )
    return result


def context_feature_names() -> list[str]:
    names = []
    for column in SUMMARY_COLUMNS:
        names.extend(
            [
                f"{column}_mean",
                f"{column}_std",
                f"{column}_max",
                f"{column}_min",
            ]
        )
    return [
        *names,
        "observed_layer_fraction",
        "completed_layer_fraction",
        "current_layer_window_fraction",
    ]


def context_features(
    layer_summaries: list[dict[str, float]],
    completed_layers: int,
    current_window_fraction: float,
) -> np.ndarray:
    values = np.asarray(
        [
            [float(summary[column]) for column in SUMMARY_COLUMNS]
            for summary in layer_summaries
        ],
        dtype=float,
    )
    output: list[float] = []
    for column in range(values.shape[1]):
        vector = values[:, column]
        output.extend(
            [
                float(vector.mean()),
                float(vector.std(ddof=0)),
                float(vector.max()),
                float(vector.min()),
            ]
        )
    output.extend(
        [
            len(layer_summaries) / 5.0,
            completed_layers / 5.0,
            float(np.clip(current_window_fraction, 0.0, 1.0)),
        ]
    )
    return np.asarray(output, dtype=float)


def build_causal_stream(windows: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    feature_names = context_feature_names()
    for specimen_id, specimen_windows in windows.groupby(
        "full_specimen_id", sort=False
    ):
        specimen_windows = specimen_windows.sort_values(
            ["layer", "window_sample_id"]
        )
        completed_summaries: list[dict[str, float]] = []
        layer_groups = list(specimen_windows.groupby("layer", sort=True))
        for layer_position, (layer, layer_windows) in enumerate(layer_groups):
            layer_windows = layer_windows.reset_index(drop=True)
            for local_index in range(len(layer_windows)):
                observed = layer_windows.iloc[: local_index + 1]
                current_summary = _partial_layer_summary(observed)
                summaries = [*completed_summaries, current_summary]
                feature_vector = context_features(
                    summaries,
                    completed_layers=layer_position,
                    current_window_fraction=(local_index + 1) / len(layer_windows),
                )
                latest = observed.iloc[-1]
                layer_health = float(current_summary["layer_health_index"])
                layer_types = np.asarray(
                    [current_summary[column] for column in PROBABILITY_COLUMNS],
                    dtype=float,
                )
                row = {
                    "full_specimen_id": str(specimen_id),
                    "layer_sample_id": str(latest["layer_sample_id"]),
                    "window_sample_id": str(latest["window_sample_id"]),
                    "layer": int(layer),
                    "dataset_split": str(latest["dataset_split"]),
                    "true_state": str(latest["true_specimen_state"]),
                    "true_id": STATE_TO_ID[str(latest["true_specimen_state"])],
                    "window_training_eligible": bool(
                        latest["window_training_eligible"]
                    ),
                    "window_health": float(latest["window_health_index"]),
                    "layer_health": layer_health,
                    "is_layer_end": local_index + 1 == len(layer_windows),
                    "is_specimen_end": (
                        layer_position + 1 == len(layer_groups)
                        and local_index + 1 == len(layer_windows)
                    ),
                    "observed_layers": len(summaries),
                    "completed_layers": layer_position,
                    "current_window_fraction": (local_index + 1)
                    / len(layer_windows),
                }
                layer_health_values = np.asarray(
                    [summary["layer_health_index"] for summary in summaries],
                    dtype=float,
                )
                specimen_health, _ = _cap_pool(layer_health_values)
                specimen_type_weights = np.maximum(
                    np.asarray(
                        [
                            summary["legacy_fixed_tail_health_index"]
                            for summary in summaries
                        ],
                        dtype=float,
                    ),
                    1e-6,
                )
                specimen_type_matrix = np.asarray(
                    [
                        [summary[column] for column in PROBABILITY_COLUMNS]
                        for summary in summaries
                    ],
                    dtype=float,
                )
                specimen_types = np.average(
                    specimen_type_matrix,
                    axis=0,
                    weights=specimen_type_weights,
                )
                specimen_types /= max(float(specimen_types.sum()), 1e-12)
                low_events = int(
                    sum(
                        summary["compaction_low_event_count"]
                        for summary in summaries
                    )
                )
                high_events = int(
                    sum(
                        summary["compaction_high_event_count"]
                        for summary in summaries
                    )
                )
                row["specimen_health"] = specimen_health
                row["specimen_compaction_override"] = (
                    low_events + high_events >= 2
                )
                row["specimen_compaction_state"] = (
                    "compaction_high"
                    if high_events > low_events
                    else "compaction_low"
                )
                for index, state in enumerate(ANOMALY_STATES):
                    row[f"window_type_{state}"] = float(
                        latest[f"probability_{state}"]
                    )
                    row[f"layer_type_{state}"] = float(layer_types[index])
                    row[f"specimen_type_{state}"] = float(
                        specimen_types[index]
                    )
                row.update(
                    {
                        name: float(value)
                        for name, value in zip(feature_names, feature_vector)
                    }
                )
                rows.append(row)
            completed_summaries.append(_partial_layer_summary(layer_windows))
    return pd.DataFrame(rows)


def _candidate_models(seed: int) -> list[tuple[str, object]]:
    candidates: list[tuple[str, object]] = []
    for c in [0.03, 0.1, 0.3, 1.0, 3.0]:
        candidates.append(
            (
                f"logistic_C{c:g}",
                make_pipeline(
                    StandardScaler(),
                    LogisticRegression(
                        C=c,
                        class_weight="balanced",
                        max_iter=5000,
                        random_state=seed,
                    ),
                ),
            )
        )
    for depth in [4, 6, 10, None]:
        label = "none" if depth is None else str(depth)
        candidates.append(
            (
                f"random_forest_depth{label}",
                RandomForestClassifier(
                    n_estimators=500,
                    max_depth=depth,
                    min_samples_leaf=2,
                    max_features="sqrt",
                    class_weight="balanced_subsample",
                    random_state=seed,
                    n_jobs=-1,
                ),
            )
        )
        candidates.append(
            (
                f"extra_trees_depth{label}",
                ExtraTreesClassifier(
                    n_estimators=500,
                    max_depth=depth,
                    min_samples_leaf=2,
                    max_features="sqrt",
                    class_weight="balanced",
                    random_state=seed,
                    n_jobs=-1,
                ),
            )
        )
    for c in [0.3, 1.0, 3.0, 10.0]:
        candidates.append(
            (
                f"svm_rbf_C{c:g}",
                make_pipeline(
                    StandardScaler(),
                    SVC(
                        C=c,
                        gamma="scale",
                        class_weight="balanced",
                        probability=True,
                        random_state=seed,
                    ),
                ),
            )
        )
    return candidates


def _aligned_probabilities(model: object, values: np.ndarray) -> np.ndarray:
    probabilities = np.asarray(model.predict_proba(values), dtype=float)
    classes = (
        model[-1].classes_
        if hasattr(model, "__getitem__")
        else model.classes_
    )
    aligned = np.full((len(values), len(STATES)), 1e-10, dtype=float)
    for source, label in enumerate(classes):
        aligned[:, int(label)] = probabilities[:, source]
    return _normalise(aligned)


def _local_posterior(
    health: np.ndarray,
    types: np.ndarray,
    threshold: float,
    scale: float,
) -> np.ndarray:
    abnormal = _sigmoid((np.asarray(health, dtype=float) - threshold) / scale)
    types = _normalise(types)
    return _normalise(
        np.column_stack([1.0 - abnormal, abnormal[:, None] * types])
    )


def _logit(values: np.ndarray) -> np.ndarray:
    values = np.clip(np.asarray(values, dtype=float), 1e-8, 1.0 - 1e-8)
    return np.log(values / (1.0 - values))


def _soft_fusion(
    health: np.ndarray,
    types: np.ndarray,
    context: np.ndarray,
    threshold: float,
    scale: float,
    binary_context_weight: float,
    type_context_weight: float,
    temperature: float,
) -> np.ndarray:
    local_abnormal = _sigmoid(
        (np.asarray(health, dtype=float) - threshold) / scale
    )
    context_abnormal = 1.0 - context[:, 0]
    fused_abnormal = _sigmoid(
        (1.0 - binary_context_weight) * _logit(local_abnormal)
        + binary_context_weight * _logit(context_abnormal)
    )
    local_types = _normalise(types)
    context_types = _normalise(context[:, 1:])
    context_logits = (
        np.log(np.clip(context_types, 1e-10, None)) / temperature
    )
    context_logits -= context_logits.max(axis=1, keepdims=True)
    tempered_types = _normalise(np.exp(context_logits))
    type_logits = (
        (1.0 - type_context_weight)
        * np.log(np.clip(local_types, 1e-10, None))
        + type_context_weight
        * np.log(np.clip(tempered_types, 1e-10, None))
    )
    type_logits -= type_logits.max(axis=1, keepdims=True)
    fused_types = _normalise(np.exp(type_logits))
    return _normalise(
        np.column_stack(
            [1.0 - fused_abnormal, fused_abnormal[:, None] * fused_types]
        )
    )


def _metrics(true_ids: np.ndarray, predicted_ids: np.ndarray) -> dict[str, float]:
    true_binary = (true_ids > 0).astype(int)
    predicted_binary = (predicted_ids > 0).astype(int)
    return {
        "binary_accuracy": float(accuracy_score(true_binary, predicted_binary)),
        "balanced_accuracy": float(
            balanced_accuracy_score(true_binary, predicted_binary)
        ),
        "seven_state_accuracy": float(accuracy_score(true_ids, predicted_ids)),
    }


def _evaluate(
    stream: pd.DataFrame,
    context_probability: np.ndarray,
    parameters: dict[str, float],
    mask: np.ndarray,
) -> dict[str, dict[str, float]]:
    window_types = stream[
        [f"window_type_{state}" for state in ANOMALY_STATES]
    ].to_numpy(dtype=float)
    layer_types = stream[
        [f"layer_type_{state}" for state in ANOMALY_STATES]
    ].to_numpy(dtype=float)
    window_posterior = _soft_fusion(
        stream["window_health"].to_numpy(dtype=float),
        window_types,
        context_probability,
        WINDOW_THRESHOLD,
        parameters["window_scale"],
        parameters["window_binary_weight"],
        parameters["window_type_weight"],
        parameters["window_temperature"],
    )
    layer_posterior = _soft_fusion(
        stream["layer_health"].to_numpy(dtype=float),
        layer_types,
        context_probability,
        0.27023794,
        parameters["layer_scale"],
        parameters["layer_binary_weight"],
        parameters["layer_type_weight"],
        parameters["layer_temperature"],
    )
    specimen_types = stream[
        [f"specimen_type_{state}" for state in ANOMALY_STATES]
    ].to_numpy(dtype=float)
    specimen_posterior = _soft_fusion(
        stream["specimen_health"].to_numpy(dtype=float),
        specimen_types,
        context_probability,
        0.403721,
        parameters["specimen_scale"],
        parameters["specimen_binary_weight"],
        parameters["specimen_type_weight"],
        parameters["specimen_temperature"],
    )
    override = stream["specimen_compaction_override"].to_numpy(dtype=bool)
    for row_index in np.flatnonzero(override):
        state = str(stream.iloc[row_index]["specimen_compaction_state"])
        specimen_posterior[row_index] = 1e-10
        specimen_posterior[row_index, STATE_TO_ID[state]] = 1.0
    # At the final instant context_probability is replaced by the proven
    # v13.8 full-five-layer posterior. It is causal because all five layers
    # have physically arrived by then.
    complete = stream["is_specimen_end"].to_numpy(dtype=bool)
    specimen_posterior[complete] = context_probability[complete]
    for row_index in np.flatnonzero(complete & override):
        state = str(stream.iloc[row_index]["specimen_compaction_state"])
        specimen_posterior[row_index] = 1e-10
        specimen_posterior[row_index, STATE_TO_ID[state]] = 1.0
    true_ids = stream["true_id"].to_numpy(dtype=int)
    window_mask = mask & stream["window_training_eligible"].to_numpy(dtype=bool)
    layer_mask = mask & stream["is_layer_end"].to_numpy(dtype=bool)
    specimen_mask = mask & stream["is_specimen_end"].to_numpy(dtype=bool)
    snapshot_mask = mask & stream["is_layer_end"].to_numpy(dtype=bool)
    return {
        "window": _metrics(
            true_ids[window_mask], window_posterior.argmax(axis=1)[window_mask]
        ),
        "layer": _metrics(
            true_ids[layer_mask], layer_posterior.argmax(axis=1)[layer_mask]
        ),
        "specimen": _metrics(
            true_ids[specimen_mask],
            specimen_posterior.argmax(axis=1)[specimen_mask],
        ),
        "specimen_prefix_snapshots": _metrics(
            true_ids[snapshot_mask],
            specimen_posterior.argmax(axis=1)[snapshot_mask],
        ),
        "_posterior": {
            "window": window_posterior,
            "layer": layer_posterior,
            "specimen": specimen_posterior,
        },
    }


def _selection_value(metrics: dict[str, dict[str, float]]) -> tuple[float, ...]:
    primary = [
        metrics["window"]["balanced_accuracy"],
        metrics["window"]["seven_state_accuracy"],
        metrics["layer"]["balanced_accuracy"],
        metrics["layer"]["seven_state_accuracy"],
        metrics["specimen"]["balanced_accuracy"],
        metrics["specimen"]["seven_state_accuracy"],
        metrics["specimen_prefix_snapshots"]["seven_state_accuracy"],
    ]
    return (
        min(primary),
        float(np.mean(primary)),
        metrics["specimen_prefix_snapshots"]["seven_state_accuracy"],
    )


def run(seed: int = 2026, output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    windows = pd.read_csv(INPUT_DIR / "hierarchical_window_state_results.csv")
    layers = pd.read_csv(INPUT_DIR / "hierarchical_layer_state_results_130.csv")
    specimens = pd.read_csv(
        INPUT_DIR / "hierarchical_specimen_state_results_26.csv"
    )
    offline_metrics = pd.read_csv(OFFLINE_DIR / "TC_HI_soft_level_metrics.csv")
    full_specimen_features = _specimen_feature_table(layers)
    full_calibrator, calibrated_types = _fit_specimen_type_calibrator(
        full_specimen_features, seed
    )
    full_gamma, _ = _select_gamma(
        specimens, full_specimen_features, calibrated_types
    )
    full_anomaly_probability, full_type_probability = _specimen_probabilities(
        specimens,
        full_specimen_features,
        calibrated_types,
        full_gamma,
    )
    full_posterior = _normalise(
        np.column_stack(
            [
                1.0 - full_anomaly_probability,
                full_anomaly_probability[:, None] * full_type_probability,
            ]
        )
    )
    full_posterior_map = dict(
        zip(
            full_specimen_features["full_specimen_id"].astype(str),
            full_posterior,
        )
    )
    stream = build_causal_stream(windows)
    feature_names = context_feature_names()
    values = stream[feature_names].to_numpy(dtype=float)
    train = stream["dataset_split"].eq("train").to_numpy()
    validation = stream["dataset_split"].eq("validation").to_numpy()
    test = stream["dataset_split"].astype(str).str.startswith("test_").to_numpy()
    specimen_counts = (
        stream.loc[train, "full_specimen_id"].value_counts().to_dict()
    )
    sample_weight = np.asarray(
        [
            1.0 / specimen_counts[str(specimen)]
            for specimen in stream.loc[train, "full_specimen_id"]
        ],
        dtype=float,
    )
    sample_weight *= len(sample_weight) / sample_weight.sum()

    model_rows = []
    fitted_models: list[tuple[str, object, np.ndarray]] = []
    y_train = stream.loc[train, "true_id"].to_numpy(dtype=int)
    for model_name, model in _candidate_models(seed):
        fit_kwargs = (
            {"logisticregression__sample_weight": sample_weight}
            if model_name.startswith("logistic")
            else {"svc__sample_weight": sample_weight}
            if model_name.startswith("svm")
            else {"sample_weight": sample_weight}
        )
        model.fit(values[train], y_train, **fit_kwargs)
        context_probability = _aligned_probabilities(model, values)
        complete_indices = np.flatnonzero(
            stream["is_specimen_end"].to_numpy(dtype=bool)
        )
        for row_index in complete_indices:
            context_probability[row_index] = full_posterior_map[
                str(stream.iloc[row_index]["full_specimen_id"])
            ]
        predicted = context_probability.argmax(axis=1)
        val_all = _metrics(
            stream.loc[validation, "true_id"].to_numpy(dtype=int),
            predicted[validation],
        )
        val_layer_end = validation & stream["is_layer_end"].to_numpy(dtype=bool)
        val_prefix = _metrics(
            stream.loc[val_layer_end, "true_id"].to_numpy(dtype=int),
            predicted[val_layer_end],
        )
        model_rows.append(
            {
                "model": model_name,
                "validation_stream_balanced_accuracy": val_all[
                    "balanced_accuracy"
                ],
                "validation_stream_seven_state_accuracy": val_all[
                    "seven_state_accuracy"
                ],
                "validation_prefix_balanced_accuracy": val_prefix[
                    "balanced_accuracy"
                ],
                "validation_prefix_seven_state_accuracy": val_prefix[
                    "seven_state_accuracy"
                ],
            }
        )
        fitted_models.append((model_name, model, context_probability))
    model_table = pd.DataFrame(model_rows).sort_values(
        [
            "validation_prefix_seven_state_accuracy",
            "validation_stream_seven_state_accuracy",
            "validation_prefix_balanced_accuracy",
        ],
        ascending=False,
    )
    selected_model_name = str(model_table.iloc[0]["model"])
    _, selected_model, context_probability = next(
        item for item in fitted_models if item[0] == selected_model_name
    )

    parameter_rows = []
    best_parameters: dict[str, float] = {
        "window_scale": 0.05,
        "window_binary_weight": 0.40,
        "window_type_weight": 0.75,
        "window_temperature": 0.20,
        "layer_scale": 0.05,
        "layer_binary_weight": 0.40,
        "layer_type_weight": 0.75,
        "layer_temperature": 0.20,
        "specimen_scale": 0.05,
        "specimen_binary_weight": 0.40,
        "specimen_type_weight": 0.75,
        "specimen_temperature": 0.20,
    }
    target_map = {
        "window": "window",
        "layer": "layer",
        "specimen": "specimen_prefix_snapshots",
    }
    for prefix, metric_level in target_map.items():
        best_level_key: tuple[float, ...] | None = None
        selected_level: dict[str, float] | None = None
        for temperature in [0.05, 0.20, 0.50, 1.0]:
            for scale in [0.03, 0.05, 0.10]:
                for binary_weight in [0.0, 0.40, 0.80, 1.0]:
                    for type_weight in [0.0, 0.50, 0.75, 1.0]:
                        trial = best_parameters.copy()
                        trial[f"{prefix}_temperature"] = temperature
                        trial[f"{prefix}_scale"] = scale
                        trial[f"{prefix}_binary_weight"] = binary_weight
                        trial[f"{prefix}_type_weight"] = type_weight
                        metrics = _evaluate(
                            stream,
                            context_probability,
                            trial,
                            validation,
                        )
                        target = metrics[metric_level]
                        minimum = min(
                            target["balanced_accuracy"],
                            target["seven_state_accuracy"],
                        )
                        mean_score = 0.5 * (
                            target["balanced_accuracy"]
                            + target["seven_state_accuracy"]
                        )
                        key = (
                            minimum,
                            mean_score,
                            -max(binary_weight, type_weight),
                            -(binary_weight + type_weight),
                            -abs(temperature - 0.20),
                        )
                        parameter_rows.append(
                            {
                                "target_level": metric_level,
                                "temperature": temperature,
                                "scale": scale,
                                "binary_context_weight": binary_weight,
                                "type_context_weight": type_weight,
                                "validation_balanced_accuracy": target[
                                    "balanced_accuracy"
                                ],
                                "validation_seven_state_accuracy": target[
                                    "seven_state_accuracy"
                                ],
                                "validation_minimum_metric": minimum,
                            }
                        )
                        if best_level_key is None or key > best_level_key:
                            best_level_key = key
                            selected_level = {
                                f"{prefix}_temperature": temperature,
                                f"{prefix}_scale": scale,
                                f"{prefix}_binary_weight": binary_weight,
                                f"{prefix}_type_weight": type_weight,
                            }
        assert selected_level is not None
        best_parameters.update(selected_level)

    level_rows = []
    predictions = {}
    for dataset, mask in [
        ("validation", validation),
        ("test_all", test),
    ]:
        metrics = _evaluate(
            stream, context_probability, best_parameters, mask
        )
        predictions[dataset] = metrics
        for level in [
            "window",
            "layer",
            "specimen",
            "specimen_prefix_snapshots",
        ]:
            level_rows.append(
                {
                    "dataset": dataset,
                    "level": level,
                    **metrics[level],
                    "method": "causal online context fusion v13.9",
                    "future_layers_used": False,
                }
            )
    level_table = pd.DataFrame(level_rows)

    test_specimen_posterior = predictions["test_all"]["_posterior"][
        "specimen"
    ]
    prefix_rows = []
    for prefix in range(1, 6):
        mask = (
            test
            & stream["is_layer_end"].to_numpy(dtype=bool)
            & stream["observed_layers"].eq(prefix).to_numpy()
        )
        metric = _metrics(
            stream.loc[mask, "true_id"].to_numpy(dtype=int),
            test_specimen_posterior.argmax(axis=1)[mask],
        )
        prefix_rows.append(
            {
                "observed_layers": prefix,
                "n_test_specimens": int(mask.sum()),
                **metric,
            }
        )
    prefix_table = pd.DataFrame(prefix_rows)

    test_eval = predictions["test_all"]
    window_posterior = test_eval["_posterior"]["window"]
    layer_posterior = test_eval["_posterior"]["layer"]
    result = stream.copy()
    result["causal_context_state"] = [
        STATES[index] for index in context_probability.argmax(axis=1)
    ]
    result["causal_specimen_state"] = [
        STATES[index]
        for index in test_eval["_posterior"]["specimen"].argmax(axis=1)
    ]
    result["causal_window_state"] = [
        STATES[index] for index in window_posterior.argmax(axis=1)
    ]
    result["causal_layer_state"] = [
        STATES[index] for index in layer_posterior.argmax(axis=1)
    ]
    result["causal_context_confidence"] = context_probability.max(axis=1)
    result.to_csv(
        output_dir / "causal_online_stream_results.csv",
        index=False,
        encoding="utf-8-sig",
    )
    level_table.to_csv(
        output_dir / "causal_online_level_metrics.csv",
        index=False,
        encoding="utf-8-sig",
    )
    prefix_table.to_csv(
        output_dir / "causal_online_prefix_metrics.csv",
        index=False,
        encoding="utf-8-sig",
    )
    model_table.to_csv(
        output_dir / "causal_online_model_validation.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(parameter_rows).sort_values(
        [
            "target_level",
            "validation_minimum_metric",
            "validation_seven_state_accuracy",
        ],
        ascending=[True, False, False],
    ).to_csv(
        output_dir / "causal_online_parameter_validation.csv",
        index=False,
        encoding="utf-8-sig",
    )
    artifact = {
        "version": VERSION,
        "model": selected_model,
        "model_name": selected_model_name,
        "feature_names": feature_names,
        "summary_columns": SUMMARY_COLUMNS,
        "states": STATES,
        "anomaly_states": ANOMALY_STATES,
        "window_threshold": WINDOW_THRESHOLD,
        "cap_rho": CAP_RHO,
        "parameters": best_parameters,
        "full_five_layer_calibrator": full_calibrator,
        "full_five_layer_gamma": full_gamma,
        "full_five_layer_feature_names": [
            column
            for column in full_specimen_features.columns
            if column
            not in {"full_specimen_id", "dataset_split", "true_state"}
        ],
    }
    joblib.dump(
        artifact,
        output_dir / "causal_online_consistency_artifact.joblib",
        compress=3,
    )

    offline_test = offline_metrics.loc[
        offline_metrics["dataset"].eq("test_all")
    ].set_index("level")
    causal_test = level_table.loc[
        level_table["dataset"].eq("test_all")
        & level_table["level"].isin(["window", "layer", "specimen"])
    ].set_index("level")
    levels = ["window", "layer", "specimen"]
    x = np.arange(len(levels))
    width = 0.36
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4))
    axes[0].bar(
        x - width / 2,
        [offline_test.loc[level, "balanced_accuracy"] for level in levels],
        width,
        label="Offline five-layer v13.8",
        color="#476F95",
    )
    axes[0].bar(
        x + width / 2,
        [causal_test.loc[level, "balanced_accuracy"] for level in levels],
        width,
        label="Causal online v13.9",
        color="#D88750",
    )
    axes[0].set_xticks(x, ["Window", "Layer", "Specimen"])
    axes[0].set_ylim(0.0, 1.05)
    axes[0].set_ylabel("Balanced accuracy")
    axes[0].legend(frameon=False)
    axes[0].grid(axis="y", alpha=0.2)
    axes[1].plot(
        prefix_table["observed_layers"],
        prefix_table["seven_state_accuracy"],
        marker="o",
        linewidth=2,
        color="#2B8C82",
    )
    axes[1].axhline(
        float(offline_test.loc["specimen", "seven_state_accuracy"]),
        linestyle="--",
        color="#476F95",
        label="Offline final specimen",
    )
    axes[1].set_xticks(range(1, 6))
    axes[1].set_ylim(0.0, 1.05)
    axes[1].set_xlabel("Observed layers")
    axes[1].set_ylabel("Test specimen seven-state accuracy")
    axes[1].grid(alpha=0.2)
    axes[1].legend(frameon=False)
    fig.tight_layout()
    figure_dir = output_dir / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        figure_dir / "Fig22_Causal_Online_vs_Offline.png",
        dpi=300,
        bbox_inches="tight",
    )
    fig.savefig(
        figure_dir / "Fig22_Causal_Online_vs_Offline.pdf",
        bbox_inches="tight",
    )
    plt.close(fig)

    summary = {
        "version": VERSION,
        "method": "causal prefix context model + local posterior log-opinion fusion",
        "selection_split": "validation_only",
        "locked_test": True,
        "future_layers_used": False,
        "selected_model": selected_model_name,
        "final_model_refit": "none; matched v13.8 train-only protocol",
        "selected_parameters": best_parameters,
        "validation_metrics": level_table.loc[
            level_table["dataset"].eq("validation")
        ].to_dict(orient="records"),
        "test_metrics": level_table.loc[
            level_table["dataset"].eq("test_all")
        ].to_dict(orient="records"),
        "prefix_test_metrics": prefix_table.to_dict(orient="records"),
        "offline_v13_8_test_metrics": offline_test.reset_index().to_dict(
            orient="records"
        ),
        "scientific_boundary": (
            "Synthetic/process-state labels and a seven-specimen locked test do "
            "not establish stable real-world defect-warning accuracy."
        ),
    }
    (output_dir / "causal_online_method_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AFP causal online soft consistency"
    )
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    summary = run(args.seed, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
