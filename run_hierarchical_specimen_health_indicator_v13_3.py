# -*- coding: utf-8 -*-
"""Hierarchical AFP warning: specimen-consistent injection -> window -> layer -> specimen.

The physical specimen is the independent experimental unit.  All five layers
share one state, while only physically observable windows are used as positive
window evidence.  Binary warning and six-type diagnosis are fitted in two
stages so the final state is logically consistent.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    from .run_layer_specimen_health_indicator_v13_2 import (
        MAX_WINDOWS_PER_LAYER,
        NORMAL_STATE,
        STATE_ORDER,
        TARGET_STATE_COUNTS,
        _make_classifier,
        _state_from_parameters,
        allocate_specimen_splits_and_states,
        build_layer_ledger,
        build_window_bank,
    )
    from .run_physics_guided_health_indicator_v13 import (
        ANOMALY_TYPES,
        OUTPUT_ENCODING,
        PRESSURE,
        TEMP,
        FeatureScaler,
        abnormal_params,
        anomaly_observability,
        apply_physics_response,
        binary_metrics,
        build_residual_features,
        choose_threshold,
        configure_matplotlib,
        deterministic_severity,
        response_feature_names,
        response_features,
        save_figure,
    )
except ImportError:  # Direct script execution.
    from run_layer_specimen_health_indicator_v13_2 import (
        MAX_WINDOWS_PER_LAYER,
        NORMAL_STATE,
        STATE_ORDER,
        TARGET_STATE_COUNTS,
        _make_classifier,
        _state_from_parameters,
        allocate_specimen_splits_and_states,
        build_layer_ledger,
        build_window_bank,
    )
    from run_physics_guided_health_indicator_v13 import (
        ANOMALY_TYPES,
        OUTPUT_ENCODING,
        PRESSURE,
        TEMP,
        FeatureScaler,
        abnormal_params,
        anomaly_observability,
        apply_physics_response,
        binary_metrics,
        build_residual_features,
        choose_threshold,
        configure_matplotlib,
        deterministic_severity,
        response_feature_names,
        response_features,
        save_figure,
    )

from run_full_prediction_to_warning_v11_5 import fit_coherence_scale_floor

try:
    from .literature_health_indicators_v13_5 import build_literature_feature_sets
except ImportError:
    from literature_health_indicators_v13_5 import build_literature_feature_sets


VERSION = "13.7.0"
TOP_WINDOW_FRACTION = 0.08
MIN_TOP_WINDOWS = 2
COMPACTION_EVENT_TYPE_PROBABILITY = 0.40
COMPACTION_EVENTS_TO_TRIGGER = 2
CAP_RHO_GRID = (0.0, 0.25, 0.50, 0.75, 1.0)
DEFAULT_CAP_RHO = 0.50
MODEL_ORDER = ["logistic", "svm_rbf", "random_forest", "extra_trees"]
# build_window_bank already returns the 12-channel sensor view
# (rotation, displacement, 8 temperatures, pressure, vibration).
SENSOR_CHANNELS = np.arange(12, dtype=int)
INDICATOR_SPECS = [
    ("T-HI", "thermal_response"),
    ("C-HI", "compaction_response"),
    ("TC-HI", "thermomechanical_response"),
    ("RFHI", "residual"),
    ("PR-HI", "response_plus_residual"),
    ("MPRF-HI", "response_plus_residual"),
    ("PCA-SPE-HI", "pca_spe"),
    ("KECA-SPE-HI", "keca_spe"),
    ("McFS-AVAE-HI", "mcfs_avae"),
    ("CNN-LSTM-AE-HI", "cnn_lstm_ae"),
    ("W-HI", "wasserstein"),
    ("RMD-HI", "robust_mahalanobis"),
]
PRIMARY_AFP_INDICATOR_FAMILIES = {
    "T-HI", "C-HI", "TC-HI", "RFHI", "PR-HI", "MPRF-HI",
}


@dataclass(frozen=True)
class AggregationConfig:
    """Window/layer pooling definition used throughout one evaluation.

    ``cap`` is Constrained AutoPool.  The same dimensionless ``rho`` is used
    at both hierarchy levels, while the actual concentration parameter adapts
    to the bag size: alpha=rho*log(M-1).  This caps the largest possible
    instance weight at 0.5 for scores in [0, 1].
    """

    method: str = "original_fixed_tail"
    rho: float = DEFAULT_CAP_RHO

    @property
    def label(self) -> str:
        if self.method == "cap":
            return f"CAP-MIL (rho={self.rho:.2f})"
        return {
            "original_fixed_tail": "Original Q95+Top8%+fraction",
            "mean": "Mean pooling",
            "max": "Max pooling",
            "top10_cvar": "Top-10%/Top-2 tail pooling",
        }.get(self.method, self.method)


@dataclass
class HierarchicalBenchmarkResult:
    ledger: pd.DataFrame
    predictions: pd.DataFrame
    long_results: pd.DataFrame
    window_results: pd.DataFrame
    layer_results: pd.DataFrame
    specimen_results: pd.DataFrame
    candidate_metrics: pd.DataFrame
    pooling_comparison: pd.DataFrame
    indicator_metrics: pd.DataFrame
    level_metrics: pd.DataFrame
    split_summary: pd.DataFrame
    state_balance: pd.DataFrame
    fixed_split_stability: pd.DataFrame
    literature_indicator_audit: pd.DataFrame
    selected_sensor_indicator: str
    selected_sensor_candidate: str
    figures: List[str]
    summary: dict


def _fit_with_specimen_weights(
    model,
    values: np.ndarray,
    labels: np.ndarray,
    specimen_ids: np.ndarray,
):
    specimen_ids = np.asarray(specimen_ids, dtype=object)
    counts = pd.Series(specimen_ids).value_counts().to_dict()
    weights = np.asarray([1.0 / counts[item] for item in specimen_ids], dtype=float)
    weights *= len(weights) / max(float(weights.sum()), 1e-12)
    if hasattr(model, "steps"):
        final_step = str(model.steps[-1][0])
        model.fit(values, labels, **{f"{final_step}__sample_weight": weights})
    else:
        model.fit(values, labels, sample_weight=weights)
    return model


def _aligned_probabilities(model, values: np.ndarray, classes: Sequence[str]) -> np.ndarray:
    raw = np.asarray(model.predict_proba(values), dtype=float)
    output = np.zeros((len(values), len(classes)), dtype=float)
    observed = [str(item) for item in np.asarray(model.classes_)]
    for column, label in enumerate(classes):
        if label in observed:
            output[:, column] = raw[:, observed.index(label)]
    row_sum = output.sum(axis=1, keepdims=True)
    return np.divide(output, row_sum, out=np.zeros_like(output), where=row_sum > 0)


def _binary_probability(model, values: np.ndarray) -> np.ndarray:
    raw = np.asarray(model.predict_proba(values), dtype=float)
    classes = np.asarray(model.classes_)
    match = np.flatnonzero(classes == 1)
    if not len(match):
        return np.zeros(len(values), dtype=float)
    return raw[:, int(match[0])]


def build_hierarchical_window_features(
    ledger: pd.DataFrame,
    selected_windows: Mapping[str, np.ndarray],
    actual_bank: np.ndarray,
    prediction_bank: np.ndarray,
    scaler: FeatureScaler,
    bounds: Mapping[str, Tuple[float, float]],
    ambient: float,
    seed: int,
) -> Tuple[Dict[str, np.ndarray], pd.DataFrame, pd.DataFrame]:
    normal_actual = []
    normal_prediction = []
    normal_rows = ledger[
        ledger["dataset_split"].eq("train") & ledger["health_state"].eq(NORMAL_STATE)
    ]
    for row in normal_rows.itertuples(index=False):
        indices = selected_windows[str(row.layer_sample_id)]
        normal_actual.append(actual_bank[indices])
        normal_prediction.append(prediction_bank[indices])
    coherence_floor = fit_coherence_scale_floor(
        scaler.transform_sensors(np.concatenate(normal_actual, axis=0)),
        scaler.transform_sensors(np.concatenate(normal_prediction, axis=0)),
    )

    _, groups = response_feature_names()
    feature_parts: Dict[str, List[np.ndarray]] = {
        "thermal_response": [],
        "compaction_response": [],
        "thermomechanical_response": [],
        "residual": [],
        "response_plus_residual": [],
    }
    rows: List[dict] = []
    sensor_sequence_parts: List[np.ndarray] = []
    offset = 0
    for layer_row in ledger.itertuples(index=False):
        indices = selected_windows[str(layer_row.layer_sample_id)]
        baseline = np.asarray(actual_bank[indices], dtype=float).copy()
        prediction = np.asarray(prediction_bank[indices], dtype=float)
        nominal = np.asarray(
            [layer_row.p, layer_row.v, layer_row.pr, layer_row.layer], dtype=float
        )
        state = str(layer_row.health_state)
        severity = 0.0
        current = nominal.copy()
        if state != NORMAL_STATE:
            severity = deterministic_severity(
                str(layer_row.full_specimen_id), state, seed
            )
            current = abnormal_params(nominal, state, severity, bounds)
            actual = np.stack([
                apply_physics_response(window, nominal, current, state, ambient)
                for window in baseline
            ])
        else:
            actual = baseline.copy()

        response = response_features(actual, prediction, ambient)
        residual = build_residual_features(
            scaler.transform_sensors(actual),
            scaler.transform_sensors(prediction),
            coherence_floor,
        )
        thermal = response[:, groups["thermal"]]
        compaction = response[:, groups["compaction"]]
        all_response = response[:, groups["all"]]
        feature_parts["thermal_response"].append(thermal)
        feature_parts["compaction_response"].append(compaction)
        feature_parts["thermomechanical_response"].append(all_response)
        feature_parts["residual"].append(residual)
        feature_parts["response_plus_residual"].append(
            np.concatenate([all_response, residual], axis=1)
        )
        sensor_sequence_parts.append(actual[:, :, SENSOR_CHANNELS])

        for local_index, bank_index in enumerate(indices):
            thermal_observed = bool(np.max(baseline[local_index, :, TEMP]) >= ambient + 10.0)
            contact_observed = bool(np.max(baseline[local_index, :, PRESSURE]) >= 10.0)
            if state == NORMAL_STATE:
                evidence_eligible = True
                reason = "normal_reference_window"
                observable_state = NORMAL_STATE
            else:
                reason = anomaly_observability(state, baseline[local_index], ambient)
                evidence_eligible = not reason.startswith("parameter_only")
                observable_state = state if evidence_eligible else "unobservable"
            rows.append({
                "window_sample_id": f"{layer_row.layer_sample_id}_W{local_index:03d}",
                "layer_sample_id": str(layer_row.layer_sample_id),
                "full_specimen_id": str(layer_row.full_specimen_id),
                "layer": int(layer_row.layer),
                "dataset_split": str(layer_row.dataset_split),
                "true_specimen_state": state,
                "true_binary_label": int(state != NORMAL_STATE),
                "observable_window_state": observable_state,
                "window_training_eligible": bool(evidence_eligible),
                "evidence_reason": reason,
                "thermal_observed": thermal_observed,
                "contact_observed": contact_observed,
                "source_bank_index": int(bank_index),
                "raw_layer_present": bool(layer_row.raw_layer_present),
                "imputed_layer": bool(layer_row.imputed_from_same_condition_other_specimen),
                "p": float(layer_row.p), "v": float(layer_row.v),
                "pr": float(layer_row.pr),
                "current_p": float(current[0]), "current_v": float(current[1]),
                "current_pr": float(current[2]),
                "injection_severity": float(severity),
                "feature_row": int(offset + local_index),
            })
        offset += len(indices)
    metadata = pd.DataFrame(rows)
    features = {name: np.concatenate(parts, axis=0) for name, parts in feature_parts.items()}
    sensor_sequences = np.concatenate(sensor_sequence_parts, axis=0)
    literature = build_literature_feature_sets(
        features["response_plus_residual"], sensor_sequences, metadata, seed
    )
    features.update(literature.features)
    if any(len(values) != len(metadata) for values in features.values()):
        raise RuntimeError("Hierarchical window features are not aligned")
    return features, metadata, literature.audit


def _candidate_specs(
    indicator_families: Sequence[str] | None = None,
) -> List[Tuple[str, str, str]]:
    allowed = None if indicator_families is None else set(indicator_families)
    specs: List[Tuple[str, str, str]] = []
    for family, feature_key in INDICATOR_SPECS:
        if allowed is not None and family not in allowed:
            continue
        for model_kind in MODEL_ORDER:
            specs.append((family, feature_key, model_kind))
    if not specs:
        raise ValueError("No indicator/model candidates remain after filtering")
    return specs


def _fit_two_stage(
    feature_values: np.ndarray,
    metadata: pd.DataFrame,
    fit_mask: np.ndarray,
    model_kind: str,
    seed: int,
) -> Tuple[object, object]:
    eligible = fit_mask & metadata["window_training_eligible"].to_numpy(dtype=bool)
    binary_labels = metadata["true_binary_label"].to_numpy(dtype=int)
    specimen_ids = metadata["full_specimen_id"].astype(str).to_numpy()
    binary_model = _make_classifier(model_kind, seed)
    _fit_with_specimen_weights(
        binary_model, feature_values[eligible], binary_labels[eligible],
        specimen_ids[eligible],
    )
    abnormal = eligible & (binary_labels == 1)
    type_model = _make_classifier(model_kind, seed + 1)
    _fit_with_specimen_weights(
        type_model, feature_values[abnormal],
        metadata.loc[abnormal, "true_specimen_state"].astype(str).to_numpy(),
        specimen_ids[abnormal],
    )
    return binary_model, type_model


def _predict_two_stage(
    binary_model,
    type_model,
    feature_values: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    return (
        _binary_probability(binary_model, feature_values),
        _aligned_probabilities(type_model, feature_values, ANOMALY_TYPES),
    )


def _choose_threshold_with_override(
    labels: np.ndarray,
    scores: np.ndarray,
    override: np.ndarray,
) -> float:
    labels = np.asarray(labels, dtype=int)
    scores = np.asarray(scores, dtype=float)
    override = np.asarray(override, dtype=bool)
    candidates = np.unique(np.concatenate([[0.0], scores, [1.0]]))
    best_threshold = choose_threshold(labels, scores)
    best_key = (-np.inf, -np.inf, np.inf)
    for threshold in candidates:
        predicted = (scores >= threshold) | override
        metrics = binary_metrics(labels, predicted)
        key = (
            metrics["balanced_accuracy"], metrics["accuracy"],
            -abs(float(threshold) - 0.5),
        )
        if key > best_key:
            best_key = key
            best_threshold = float(threshold)
    return float(best_threshold)


def _type_evidence(type_probabilities: np.ndarray, metadata: pd.DataFrame) -> np.ndarray:
    output = np.asarray(type_probabilities, dtype=float).copy()
    thermal = metadata["thermal_observed"].to_numpy(dtype=bool)
    contact = metadata["contact_observed"].to_numpy(dtype=bool)
    for column, state in enumerate(ANOMALY_TYPES):
        if state.startswith("power"):
            output[:, column] *= thermal
        elif state.startswith("compaction"):
            output[:, column] *= contact
    row_sum = output.sum(axis=1, keepdims=True)
    fallback = np.asarray(type_probabilities, dtype=float)
    return np.divide(output, row_sum, out=fallback.copy(), where=row_sum > 0)


def _cap_weights(scores: np.ndarray, rho: float) -> Tuple[np.ndarray, float]:
    """Return numerically stable Constrained AutoPool weights.

    For probabilities in [0, 1], alpha <= log(M-1) ensures that even the most
    extreme single window cannot receive more than half of the bag weight.
    """

    values = np.clip(np.asarray(scores, dtype=float), 0.0, 1.0)
    count = len(values)
    if count == 0:
        raise ValueError("Cannot pool an empty score bag")
    if count == 1:
        return np.ones(1, dtype=float), 0.0
    rho = float(np.clip(rho, 0.0, 1.0))
    alpha = rho * float(np.log(count - 1.0))
    logits = alpha * values
    logits -= float(np.max(logits))
    weights = np.exp(logits)
    weights /= float(np.sum(weights))
    return weights, alpha


def _pool_scores(
    scores: np.ndarray,
    aggregation: AggregationConfig,
    level: str,
) -> Tuple[float, np.ndarray, float]:
    """Pool one layer/window bag and expose weights for type aggregation."""

    values = np.asarray(scores, dtype=float)
    if not len(values):
        raise ValueError("Cannot pool an empty score bag")
    method = str(aggregation.method)
    if method == "cap":
        weights, alpha = _cap_weights(values, aggregation.rho)
    elif method == "mean":
        weights = np.full(len(values), 1.0 / len(values), dtype=float)
        alpha = 0.0
    elif method == "max":
        weights = np.zeros(len(values), dtype=float)
        weights[int(np.argmax(values))] = 1.0
        alpha = float("nan")
    elif method == "top10_cvar":
        # Local AFP defects can occupy a short segment of a layer.  Use the
        # empirical upper 10% tail at window level and the upper two of five
        # layers at specimen level as the transparent fixed-tail baseline.
        if level == "layer":
            top_count = max(1, int(np.ceil(0.10 * len(values))))
        elif level == "specimen":
            top_count = min(2, len(values))
        else:
            raise ValueError(f"Unsupported hierarchy level: {level}")
        selected = np.argsort(values)[-top_count:]
        weights = np.zeros(len(values), dtype=float)
        weights[selected] = 1.0 / top_count
        alpha = float("nan")
    else:
        raise ValueError(f"Unsupported pooling method: {method}")
    return float(np.dot(weights, values)), weights, float(alpha)


def _pool_class_evidence(
    instance_scores: np.ndarray,
    type_evidence: np.ndarray,
    aggregation: AggregationConfig,
    level: str,
    original_weights: np.ndarray,
) -> np.ndarray:
    """Aggregate mechanism-gated evidence separately for each anomaly class.

    A shared attention vector can dilute a sparse compaction/power event with
    windows supporting another class.  Class-specific MIL instead pools the
    joint evidence P(abnormal)*P(type|abnormal) for every anomaly mechanism.
    """

    instance_scores = np.asarray(instance_scores, dtype=float)
    type_evidence = np.asarray(type_evidence, dtype=float)
    if aggregation.method == "original_fixed_tail":
        vector = np.average(type_evidence, axis=0, weights=original_weights)
    else:
        class_values = []
        for column in range(type_evidence.shape[1]):
            joint = np.clip(
                instance_scores * type_evidence[:, column], 0.0, 1.0
            )
            pooled, _, _ = _pool_scores(joint, aggregation, level)
            class_values.append(pooled)
        vector = np.asarray(class_values, dtype=float)
    total = float(np.sum(vector))
    if total <= 1e-12:
        vector = np.mean(type_evidence, axis=0)
        total = float(np.sum(vector))
    return np.divide(vector, total, out=np.zeros_like(vector), where=total > 0)


def aggregate_window_predictions(
    metadata: pd.DataFrame,
    binary_scores: np.ndarray,
    type_probabilities: np.ndarray,
    window_threshold: float,
    layer_threshold: float | None = None,
    specimen_threshold: float | None = None,
    aggregation: AggregationConfig | None = None,
    apply_specimen_consistency: bool = True,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    aggregation = aggregation or AggregationConfig()
    window = metadata.copy().reset_index(drop=True)
    window["window_health_index"] = np.asarray(binary_scores, dtype=float)
    evidence = _type_evidence(type_probabilities, window)
    window_type_index = np.argmax(evidence, axis=1)
    window["predicted_anomaly_type"] = [ANOMALY_TYPES[index] for index in window_type_index]
    window["predicted_binary_label"] = (
        window["window_health_index"].to_numpy() >= window_threshold
    ).astype(int)
    window["predicted_window_state"] = np.where(
        window["predicted_binary_label"].eq(1),
        window["predicted_anomaly_type"], NORMAL_STATE,
    )
    observable = window["observable_window_state"].ne("unobservable")
    window["window_prediction_correct"] = np.where(
        ~observable, "not_evaluable",
        np.where(
            window["predicted_window_state"].eq(window["observable_window_state"]),
            "yes", "no",
        ),
    )
    compaction_columns = [
        ANOMALY_TYPES.index("compaction_low"),
        ANOMALY_TYPES.index("compaction_high"),
    ]
    window["compaction_event_probability"] = (
        window["window_health_index"].to_numpy()
        * np.max(evidence[:, compaction_columns], axis=1)
    )
    window["predicted_compaction_event"] = (
        window["contact_observed"].to_numpy(dtype=bool)
        & (window["window_health_index"].to_numpy() >= window_threshold)
        & (np.max(evidence[:, compaction_columns], axis=1) >= COMPACTION_EVENT_TYPE_PROBABILITY)
    )
    for column, state in enumerate(ANOMALY_TYPES):
        window[f"probability_{state}"] = evidence[:, column]

    layer_rows: List[dict] = []
    for layer_id, group in window.groupby("layer_sample_id", sort=False):
        indices = group.index.to_numpy(dtype=int)
        scores = window.loc[indices, "window_health_index"].to_numpy(dtype=float)
        top_count = min(len(scores), max(MIN_TOP_WINDOWS, int(np.ceil(TOP_WINDOW_FRACTION * len(scores)))))
        top_indices_local = np.argsort(scores)[-top_count:]
        q95 = float(np.percentile(scores, 95))
        top_mean = float(np.mean(scores[top_indices_local]))
        abnormal_fraction = float(np.mean(scores >= window_threshold))
        legacy_layer_score = 0.35 * q95 + 0.45 * top_mean + 0.20 * abnormal_fraction
        legacy_type_weights = np.zeros(len(scores), dtype=float)
        tail_weights = np.maximum(scores[top_indices_local], 1e-6)
        legacy_type_weights[top_indices_local] = tail_weights / float(np.sum(tail_weights))
        if aggregation.method == "original_fixed_tail":
            layer_score = legacy_layer_score
            pooling_weights = legacy_type_weights.copy()
            pooling_alpha = float("nan")
        else:
            layer_score, pooling_weights, pooling_alpha = _pool_scores(
                scores, aggregation, "layer"
            )
        type_matrix = evidence[indices]
        if aggregation.method == "cap":
            # Keep the proven sparse, mechanism-gated tail attribution in the
            # second stage.  CAP changes the binary HI only; it must not dilute
            # the anomaly type with many normal windows.
            type_vector = np.average(
                type_matrix, axis=0, weights=legacy_type_weights
            )
            type_vector /= max(float(type_vector.sum()), 1e-12)
        else:
            type_vector = _pool_class_evidence(
                scores, type_matrix, aggregation, "layer", pooling_weights
            )
        comp_events = group["predicted_compaction_event"].to_numpy(dtype=bool)
        low_events = int(np.sum(
            comp_events & group["predicted_anomaly_type"].eq("compaction_low").to_numpy()
        ))
        high_events = int(np.sum(
            comp_events & group["predicted_anomaly_type"].eq("compaction_high").to_numpy()
        ))
        first = group.iloc[0]
        row = {
            "layer_sample_id": layer_id,
            "full_specimen_id": first["full_specimen_id"],
            "layer": int(first["layer"]),
            "dataset_split": first["dataset_split"],
            "true_state": first["true_specimen_state"],
            "true_binary_label": int(first["true_binary_label"]),
            "window_count": int(len(group)),
            "evaluable_window_count": int(group["window_training_eligible"].sum()),
            "predicted_abnormal_window_count": int(np.sum(scores >= window_threshold)),
            "layer_health_index": float(layer_score),
            "legacy_fixed_tail_health_index": float(legacy_layer_score),
            "aggregation_method": aggregation.method,
            "aggregation_label": aggregation.label,
            "type_evidence_aggregation": (
                "mechanism_gated_original_tail" if aggregation.method == "cap"
                else f"class_specific_{aggregation.method}"
            ),
            "cap_rho": float(aggregation.rho) if aggregation.method == "cap" else np.nan,
            "pooling_alpha": pooling_alpha,
            "maximum_pooling_weight": float(np.max(pooling_weights)),
            "effective_window_count": float(1.0 / np.sum(pooling_weights ** 2)),
            "window_score_q95": q95,
            "top_window_score_mean": top_mean,
            "abnormal_window_fraction": abnormal_fraction,
            "compaction_low_event_count": low_events,
            "compaction_high_event_count": high_events,
            "compaction_event_count": low_events + high_events,
            "raw_layer_present": bool(first["raw_layer_present"]),
            "imputed_layer": bool(first["imputed_layer"]),
        }
        for column, state in enumerate(ANOMALY_TYPES):
            row[f"probability_{state}"] = float(type_vector[column])
        layer_rows.append(row)
    layer = pd.DataFrame(layer_rows)

    specimen_rows: List[dict] = []
    for specimen_id, group in layer.groupby("full_specimen_id", sort=False):
        scores = group["layer_health_index"].to_numpy(dtype=float)
        legacy_scores = group["legacy_fixed_tail_health_index"].to_numpy(dtype=float)
        top_two = np.sort(scores)[-min(2, len(scores)):]
        if aggregation.method == "original_fixed_tail":
            specimen_score = 0.60 * float(np.max(scores)) + 0.40 * float(np.mean(top_two))
            pooling_weights = np.maximum(scores, 1e-6)
            pooling_weights /= float(np.sum(pooling_weights))
            pooling_alpha = float("nan")
        else:
            specimen_score, pooling_weights, pooling_alpha = _pool_scores(
                scores, aggregation, "specimen"
            )
        type_matrix = group[[f"probability_{state}" for state in ANOMALY_TYPES]].to_numpy(dtype=float)
        if aggregation.method == "cap":
            type_vector = np.average(
                type_matrix, axis=0, weights=np.maximum(legacy_scores, 1e-6)
            )
            type_vector /= max(float(type_vector.sum()), 1e-12)
        else:
            type_vector = _pool_class_evidence(
                scores, type_matrix, aggregation, "specimen", pooling_weights
            )
        first = group.iloc[0]
        low_events = int(group["compaction_low_event_count"].sum())
        high_events = int(group["compaction_high_event_count"].sum())
        row = {
            "full_specimen_id": specimen_id,
            "dataset_split": first["dataset_split"],
            "true_state": first["true_state"],
            "true_binary_label": int(first["true_binary_label"]),
            "layer_count": int(len(group)),
            "abnormal_layer_count_at_0_5": int(np.sum(scores >= 0.5)),
            "maximum_layer_health_index": float(np.max(scores)),
            "top2_layer_health_index_mean": float(np.mean(top_two)),
            "specimen_health_index": specimen_score,
            "type_evidence_aggregation": (
                "mechanism_gated_original_tail" if aggregation.method == "cap"
                else f"class_specific_{aggregation.method}"
            ),
            "aggregation_method": aggregation.method,
            "aggregation_label": aggregation.label,
            "cap_rho": float(aggregation.rho) if aggregation.method == "cap" else np.nan,
            "pooling_alpha": pooling_alpha,
            "maximum_pooling_weight": float(np.max(pooling_weights)),
            "effective_layer_count": float(1.0 / np.sum(pooling_weights ** 2)),
            "compaction_low_event_count": low_events,
            "compaction_high_event_count": high_events,
            "compaction_event_count": low_events + high_events,
        }
        for column, state in enumerate(ANOMALY_TYPES):
            row[f"probability_{state}"] = float(type_vector[column])
        specimen_rows.append(row)
    specimen = pd.DataFrame(specimen_rows)

    if layer_threshold is not None:
        _finalize_hierarchical_states(layer, float(layer_threshold), "layer")
    if specimen_threshold is not None:
        _finalize_hierarchical_states(specimen, float(specimen_threshold), "specimen")
    if (
        apply_specimen_consistency
        and layer_threshold is not None
        and specimen_threshold is not None
    ):
        _apply_specimen_consistency(window, layer, specimen)
    return window, layer, specimen


def _apply_specimen_consistency(
    window: pd.DataFrame,
    layer: pd.DataFrame,
    specimen: pd.DataFrame,
) -> None:
    """Back-project the five-layer specimen decision as the final warning state.

    A physical AFP specimen has one shared process state across its five
    layers.  Sparse contact/thermal events may be absent from an individual
    layer even though the specimen is abnormal.  The local window/layer
    decisions are therefore retained as diagnostic evidence, while the final
    deployed state is constrained to the specimen decision.  This is a
    structured-prediction consistency step, not an independent layer model.
    """

    specimen_index = specimen.set_index("full_specimen_id")
    specimen_state = specimen_index["predicted_state"].astype(str).to_dict()
    specimen_type = specimen_index["predicted_anomaly_type"].astype(str).to_dict()
    specimen_binary = specimen_index["predicted_binary_label"].astype(int).to_dict()

    layer["local_predicted_binary_label"] = layer["predicted_binary_label"].astype(int)
    layer["local_predicted_anomaly_type"] = layer["predicted_anomaly_type"].astype(str)
    layer["local_predicted_state"] = layer["predicted_state"].astype(str)
    layer["local_prediction_correct"] = layer["prediction_correct"].astype(str)
    layer["predicted_binary_label"] = (
        layer["full_specimen_id"].map(specimen_binary).astype(int)
    )
    layer["predicted_anomaly_type"] = (
        layer["full_specimen_id"].map(specimen_type).astype(str)
    )
    layer["predicted_state"] = layer["full_specimen_id"].map(specimen_state).astype(str)
    layer["prediction_correct"] = np.where(
        layer["predicted_state"].eq(layer["true_state"]), "yes", "no"
    )
    layer["final_decision_source"] = "five_layer_specimen_consistency"

    # Window outputs retain their local binary evidence.  For evaluable
    # windows, the final seven-state warning is aligned with the completed
    # specimen decision; both columns remain available for audit.
    window["local_predicted_binary_label"] = window["predicted_binary_label"].astype(int)
    window["local_predicted_anomaly_type"] = window["predicted_anomaly_type"].astype(str)
    window["local_predicted_window_state"] = window["predicted_window_state"].astype(str)
    window["local_window_prediction_correct"] = window["window_prediction_correct"].astype(str)
    evaluable = window["window_training_eligible"].to_numpy(dtype=bool)
    mapped_state = window["full_specimen_id"].map(specimen_state).astype(str)
    mapped_type = window["full_specimen_id"].map(specimen_type).astype(str)
    mapped_binary = window["full_specimen_id"].map(specimen_binary).astype(int)
    window.loc[evaluable, "predicted_binary_label"] = mapped_binary[evaluable]
    window.loc[evaluable, "predicted_window_state"] = mapped_state[evaluable]
    window.loc[evaluable, "predicted_anomaly_type"] = mapped_type[evaluable]
    window.loc[evaluable, "window_prediction_correct"] = np.where(
        window.loc[evaluable, "predicted_window_state"].astype(str).to_numpy()
        == window.loc[evaluable, "observable_window_state"].astype(str).to_numpy(),
        "yes", "no",
    )
    window["final_state_decision_source"] = np.where(
        evaluable, "five_layer_specimen_consistency", "not_evaluable"
    )


def _finalize_hierarchical_states(table: pd.DataFrame, threshold: float, level: str) -> None:
    score_column = f"{level}_health_index"
    compaction_override = table["compaction_event_count"].to_numpy(dtype=int) >= COMPACTION_EVENTS_TO_TRIGGER
    predicted_binary = (
        (table[score_column].to_numpy(dtype=float) >= threshold) | compaction_override
    ).astype(int)
    type_matrix = table[[f"probability_{state}" for state in ANOMALY_TYPES]].to_numpy(dtype=float)
    predicted_type = np.asarray([ANOMALY_TYPES[index] for index in np.argmax(type_matrix, axis=1)], dtype=object)
    force_compaction = compaction_override
    predicted_type[force_compaction] = np.where(
        table.loc[force_compaction, "compaction_high_event_count"].to_numpy(dtype=int)
        > table.loc[force_compaction, "compaction_low_event_count"].to_numpy(dtype=int),
        "compaction_high", "compaction_low",
    )
    table["decision_threshold"] = float(threshold)
    table["compaction_event_override"] = compaction_override
    table["predicted_binary_label"] = predicted_binary
    table["predicted_anomaly_type"] = predicted_type
    table["predicted_state"] = np.where(
        predicted_binary == 1, predicted_type, NORMAL_STATE
    )
    table["prediction_correct"] = np.where(
        table["predicted_state"].eq(table["true_state"]), "yes", "no"
    )


def _thresholds_from_validation(
    metadata: pd.DataFrame,
    binary_scores: np.ndarray,
    type_probabilities: np.ndarray,
    aggregation: AggregationConfig | None = None,
) -> Tuple[float, float, float]:
    aggregation = aggregation or AggregationConfig()
    validation = metadata["dataset_split"].eq("validation").to_numpy()
    observable = metadata["window_training_eligible"].to_numpy(dtype=bool)
    window_threshold = choose_threshold(
        metadata.loc[validation & observable, "true_binary_label"].to_numpy(dtype=int),
        binary_scores[validation & observable],
    )
    _, layer, specimen = aggregate_window_predictions(
        metadata[validation].reset_index(drop=True), binary_scores[validation],
        type_probabilities[validation], window_threshold,
        aggregation=aggregation,
    )
    layer_override = layer["compaction_event_count"].to_numpy(dtype=int) >= COMPACTION_EVENTS_TO_TRIGGER
    specimen_override = specimen["compaction_event_count"].to_numpy(dtype=int) >= COMPACTION_EVENTS_TO_TRIGGER
    layer_threshold = _choose_threshold_with_override(
        layer["true_binary_label"].to_numpy(dtype=int),
        layer["layer_health_index"].to_numpy(dtype=float), layer_override,
    )
    specimen_threshold = _choose_threshold_with_override(
        specimen["true_binary_label"].to_numpy(dtype=int),
        specimen["specimen_health_index"].to_numpy(dtype=float), specimen_override,
    )
    return float(window_threshold), float(layer_threshold), float(specimen_threshold)


def _state_accuracy(table: pd.DataFrame) -> float:
    if "predicted_state" in table.columns:
        predicted = table["predicted_state"].astype(str)
        truth = table["true_state"].astype(str)
    else:
        predicted = table["predicted_window_state"].astype(str)
        truth = table["observable_window_state"].astype(str)
    return float(np.mean(predicted == truth))


def _evaluate_candidate(
    metadata: pd.DataFrame,
    binary_scores: np.ndarray,
    type_probabilities: np.ndarray,
    thresholds: Tuple[float, float, float],
    dataset_mask: np.ndarray,
    aggregation: AggregationConfig | None = None,
) -> Tuple[dict, dict, dict, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    aggregation = aggregation or AggregationConfig()
    window_threshold, layer_threshold, specimen_threshold = thresholds
    window, layer, specimen = aggregate_window_predictions(
        metadata[dataset_mask].reset_index(drop=True), binary_scores[dataset_mask],
        type_probabilities[dataset_mask], window_threshold,
        layer_threshold, specimen_threshold, aggregation=aggregation,
    )
    observable = window["window_training_eligible"].to_numpy(dtype=bool)
    window_metrics = binary_metrics(
        window.loc[observable, "true_binary_label"].to_numpy(dtype=int),
        window.loc[observable, "predicted_binary_label"].to_numpy(dtype=int),
    )
    layer_metrics = binary_metrics(
        layer["true_binary_label"].to_numpy(dtype=int),
        layer["predicted_binary_label"].to_numpy(dtype=int),
    )
    specimen_metrics = binary_metrics(
        specimen["true_binary_label"].to_numpy(dtype=int),
        specimen["predicted_binary_label"].to_numpy(dtype=int),
    )
    for metrics, table in [
        (window_metrics, window.loc[observable]),
        (layer_metrics, layer),
        (specimen_metrics, specimen),
    ]:
        metrics["state_accuracy"] = _state_accuracy(table)
    return window_metrics, layer_metrics, specimen_metrics, window, layer, specimen


def _hierarchical_selection_score(layer_metrics: Mapping[str, float], specimen_metrics: Mapping[str, float]) -> float:
    return float(
        0.70 * specimen_metrics["balanced_accuracy"]
        + 0.20 * layer_metrics["balanced_accuracy"]
        + 0.10 * specimen_metrics["state_accuracy"]
    )


def _select_cap_configuration(
    metadata: pd.DataFrame,
    binary_scores: np.ndarray,
    type_probabilities: np.ndarray,
) -> Tuple[AggregationConfig, Tuple[float, float, float], dict, dict, dict]:
    """Choose CAP concentration only on the fixed validation specimens."""

    validation = metadata["dataset_split"].eq("validation").to_numpy()
    candidates = []
    for rho in CAP_RHO_GRID:
        aggregation = AggregationConfig("cap", float(rho))
        thresholds = _thresholds_from_validation(
            metadata, binary_scores, type_probabilities, aggregation
        )
        window_metrics, layer_metrics, specimen_metrics, *_ = _evaluate_candidate(
            metadata, binary_scores, type_probabilities, thresholds,
            validation, aggregation,
        )
        selection_score = _hierarchical_selection_score(layer_metrics, specimen_metrics)
        # A validation tie is resolved toward rho=0.5: it is neither pure mean
        # nor the most concentrated admissible pooling and was pre-registered
        # before examining the locked test specimens.
        key = (
            selection_score,
            specimen_metrics["balanced_accuracy"],
            layer_metrics["balanced_accuracy"],
            -abs(float(rho) - DEFAULT_CAP_RHO),
        )
        candidates.append((key, aggregation, thresholds, window_metrics,
                           layer_metrics, specimen_metrics))
    _, aggregation, thresholds, window_metrics, layer_metrics, specimen_metrics = max(
        candidates, key=lambda item: item[0]
    )
    return aggregation, thresholds, window_metrics, layer_metrics, specimen_metrics


def fit_hierarchical_candidates(
    features: Mapping[str, np.ndarray],
    metadata: pd.DataFrame,
    seed: int,
    indicator_families: Sequence[str] | None = None,
) -> Tuple[pd.DataFrame, dict]:
    split = metadata["dataset_split"].astype(str).to_numpy()
    train = split == "train"
    validation = split == "validation"
    test = np.char.startswith(split.astype(str), "test_")
    rows: List[dict] = []
    fitted: List[dict] = []
    for family, feature_key, model_kind in _candidate_specs(indicator_families):
        values = features[feature_key]
        # A fair factorial comparison uses the same random state for the same
        # classifier across every indicator family.  The seed must not depend
        # on candidate ordering, otherwise identical feature/model pairs can
        # differ only because they appeared at different rows in the grid.
        model_seed = seed + 1009 * MODEL_ORDER.index(model_kind)
        binary_model, type_model = _fit_two_stage(
            values, metadata, train, model_kind, model_seed
        )
        binary_scores, type_probabilities = _predict_two_stage(
            binary_model, type_model, values
        )
        aggregation, thresholds, val_window, val_layer, val_specimen = _select_cap_configuration(
            metadata, binary_scores, type_probabilities
        )
        test_window, test_layer, test_specimen, *_ = _evaluate_candidate(
            metadata, binary_scores, type_probabilities, thresholds, test,
            aggregation,
        )
        selection_score = _hierarchical_selection_score(val_layer, val_specimen)
        rows.append({
            "indicator_family": family,
            "candidate": f"{family} | {model_kind}",
            "feature_key": feature_key,
            "model_kind": model_kind,
            "aggregation_method": aggregation.method,
            "aggregation_label": aggregation.label,
            "cap_rho": aggregation.rho,
            "uses_process_parameter_combination": False,
            "eligible_for_primary_selection": family in PRIMARY_AFP_INDICATOR_FAMILIES,
            "window_threshold": thresholds[0],
            "layer_threshold": thresholds[1],
            "specimen_threshold": thresholds[2],
            "validation_selection_score": float(selection_score),
            "validation_window_balanced_accuracy": val_window["balanced_accuracy"],
            "validation_layer_balanced_accuracy": val_layer["balanced_accuracy"],
            "validation_specimen_accuracy": val_specimen["accuracy"],
            "validation_specimen_balanced_accuracy": val_specimen["balanced_accuracy"],
            "validation_specimen_state_accuracy": val_specimen["state_accuracy"],
            "test_window_balanced_accuracy": test_window["balanced_accuracy"],
            "test_layer_accuracy": test_layer["accuracy"],
            "test_layer_balanced_accuracy": test_layer["balanced_accuracy"],
            "test_layer_state_accuracy": test_layer["state_accuracy"],
            "test_specimen_accuracy": test_specimen["accuracy"],
            "test_specimen_balanced_accuracy": test_specimen["balanced_accuracy"],
            "test_specimen_state_accuracy": test_specimen["state_accuracy"],
        })
        fitted.append({
            "binary_model": binary_model,
            "type_model": type_model,
            "feature_key": feature_key,
            "model_kind": model_kind,
            "candidate": f"{family} | {model_kind}",
            "indicator_family": family,
            "thresholds": thresholds,
            "aggregation": aggregation,
            "binary_scores": binary_scores,
            "type_probabilities": type_probabilities,
        })
    table = pd.DataFrame(rows)
    eligible_indices = table.index[table["eligible_for_primary_selection"]].to_numpy(dtype=int)
    selected_index = int(
        table.loc[eligible_indices, "validation_selection_score"].idxmax()
    )
    comparison_winner_index = int(table["validation_selection_score"].idxmax())
    table["selected_hierarchical_indicator"] = table.index == selected_index
    table["validation_winner_all_families"] = table.index == comparison_winner_index
    table = table.sort_values(
        ["validation_selection_score", "validation_specimen_balanced_accuracy"],
        ascending=False,
    ).reset_index(drop=True)
    return table, fitted[selected_index]


def fixed_split_tc_hi_stability(
    features: Mapping[str, np.ndarray],
    metadata: pd.DataFrame,
    base_seed: int,
    repeats: int,
    model_kind: str = "random_forest",
) -> pd.DataFrame:
    """Repeat only model randomness on one locked specimen split.

    The data/state assignment, injected response, training specimens,
    validation specimens and test specimens remain byte-identical.  Each run
    refits TC-HI with a different estimator seed; CAP concentration and all
    thresholds are re-selected from the fixed validation specimens only.
    """

    if repeats < 1:
        raise ValueError("repeats must be at least one")
    values = np.asarray(features["thermomechanical_response"], dtype=float)
    split = metadata["dataset_split"].astype(str).to_numpy()
    train = split == "train"
    validation = split == "validation"
    test = np.char.startswith(split.astype(str), "test_")
    rows: List[dict] = []
    for repeat in range(int(repeats)):
        model_seed = int(base_seed + 1009 * repeat)
        binary_model, type_model = _fit_two_stage(
            values, metadata, train, model_kind, model_seed
        )
        binary_scores, type_probabilities = _predict_two_stage(
            binary_model, type_model, values
        )
        aggregation, thresholds, val_window, val_layer, val_specimen = (
            _select_cap_configuration(metadata, binary_scores, type_probabilities)
        )
        (
            test_window_metrics,
            test_layer_metrics,
            test_specimen_metrics,
            test_window,
            test_layer,
            _,
        ) = _evaluate_candidate(
            metadata, binary_scores, type_probabilities, thresholds, test,
            aggregation,
        )
        observable = test_window["window_training_eligible"].to_numpy(dtype=bool)
        local_window = binary_metrics(
            test_window.loc[observable, "true_binary_label"].to_numpy(dtype=int),
            test_window.loc[observable, "local_predicted_binary_label"].to_numpy(dtype=int),
        )
        local_window_state = float(np.mean(
            test_window.loc[observable, "local_predicted_window_state"].astype(str)
            == test_window.loc[observable, "observable_window_state"].astype(str)
        ))
        local_layer = binary_metrics(
            test_layer["true_binary_label"].to_numpy(dtype=int),
            test_layer["local_predicted_binary_label"].to_numpy(dtype=int),
        )
        local_layer_state = float(np.mean(
            test_layer["local_predicted_state"].astype(str)
            == test_layer["true_state"].astype(str)
        ))
        final_test_metrics = [
            test_window_metrics["balanced_accuracy"],
            test_window_metrics["state_accuracy"],
            test_layer_metrics["balanced_accuracy"],
            test_layer_metrics["state_accuracy"],
            test_specimen_metrics["balanced_accuracy"],
            test_specimen_metrics["state_accuracy"],
        ]
        local_test_metrics = [
            local_window["balanced_accuracy"],
            local_window_state,
            local_layer["balanced_accuracy"],
            local_layer_state,
            test_specimen_metrics["balanced_accuracy"],
            test_specimen_metrics["state_accuracy"],
        ]
        rows.append({
            "repeat": repeat + 1,
            "data_split_seed": int(base_seed),
            "model_seed": model_seed,
            "indicator": "TC-HI",
            "model": model_kind,
            "aggregation": aggregation.label,
            "cap_rho": float(aggregation.rho),
            "window_threshold": float(thresholds[0]),
            "layer_threshold": float(thresholds[1]),
            "specimen_threshold": float(thresholds[2]),
            "validation_specimen_balanced_accuracy": val_specimen["balanced_accuracy"],
            "validation_specimen_state_accuracy": val_specimen["state_accuracy"],
            "test_local_window_balanced_accuracy": local_window["balanced_accuracy"],
            "test_local_window_state_accuracy": local_window_state,
            "test_final_window_balanced_accuracy": test_window_metrics["balanced_accuracy"],
            "test_final_window_state_accuracy": test_window_metrics["state_accuracy"],
            "test_local_layer_balanced_accuracy": local_layer["balanced_accuracy"],
            "test_local_layer_state_accuracy": local_layer_state,
            "test_final_layer_balanced_accuracy": test_layer_metrics["balanced_accuracy"],
            "test_final_layer_state_accuracy": test_layer_metrics["state_accuracy"],
            "test_specimen_balanced_accuracy": test_specimen_metrics["balanced_accuracy"],
            "test_specimen_state_accuracy": test_specimen_metrics["state_accuracy"],
            "minimum_test_final_warning_accuracy": float(min(final_test_metrics)),
            "all_test_final_warning_accuracies_at_least_90_percent": bool(
                min(final_test_metrics) >= 0.90
            ),
            "minimum_test_local_warning_accuracy": float(min(local_test_metrics)),
            "all_test_local_warning_accuracies_at_least_90_percent": bool(
                min(local_test_metrics) >= 0.90
            ),
        })
    return pd.DataFrame(rows)


def compare_aggregation_methods(
    selected: Mapping[str, object],
    metadata: pd.DataFrame,
) -> pd.DataFrame:
    """Fair pooling ablation for one fixed window model.

    Every method receives exactly the same window scores and type
    probabilities.  Its thresholds are estimated on validation specimens;
    the locked test is evaluated only after those choices are fixed.
    """

    binary_scores = np.asarray(selected["binary_scores"], dtype=float)
    type_probabilities = np.asarray(selected["type_probabilities"], dtype=float)
    split = metadata["dataset_split"].astype(str).to_numpy()
    validation = split == "validation"
    test = np.char.startswith(split.astype(str), "test_")
    configs = [
        AggregationConfig("original_fixed_tail", DEFAULT_CAP_RHO),
        AggregationConfig("mean", DEFAULT_CAP_RHO),
        AggregationConfig("max", DEFAULT_CAP_RHO),
        AggregationConfig("top10_cvar", DEFAULT_CAP_RHO),
        *[AggregationConfig("cap", float(rho)) for rho in CAP_RHO_GRID],
    ]
    rows: List[dict] = []
    selected_aggregation = selected["aggregation"]
    for aggregation in configs:
        thresholds = _thresholds_from_validation(
            metadata, binary_scores, type_probabilities, aggregation
        )
        val_window, val_layer, val_specimen, *_ = _evaluate_candidate(
            metadata, binary_scores, type_probabilities, thresholds,
            validation, aggregation,
        )
        test_window, test_layer, test_specimen, *_ = _evaluate_candidate(
            metadata, binary_scores, type_probabilities, thresholds,
            test, aggregation,
        )
        rows.append({
            "window_model": str(selected["candidate"]),
            "aggregation_method": aggregation.method,
            "aggregation_label": aggregation.label,
            "cap_rho": aggregation.rho if aggregation.method == "cap" else np.nan,
            "window_threshold": thresholds[0],
            "layer_threshold": thresholds[1],
            "specimen_threshold": thresholds[2],
            "validation_selection_score": _hierarchical_selection_score(val_layer, val_specimen),
            "validation_window_balanced_accuracy": val_window["balanced_accuracy"],
            "validation_layer_balanced_accuracy": val_layer["balanced_accuracy"],
            "validation_layer_state_accuracy": val_layer["state_accuracy"],
            "validation_specimen_balanced_accuracy": val_specimen["balanced_accuracy"],
            "validation_specimen_state_accuracy": val_specimen["state_accuracy"],
            "test_window_balanced_accuracy": test_window["balanced_accuracy"],
            "test_layer_accuracy": test_layer["accuracy"],
            "test_layer_balanced_accuracy": test_layer["balanced_accuracy"],
            "test_layer_state_accuracy": test_layer["state_accuracy"],
            "test_specimen_accuracy": test_specimen["accuracy"],
            "test_specimen_balanced_accuracy": test_specimen["balanced_accuracy"],
            "test_specimen_state_accuracy": test_specimen["state_accuracy"],
            "selected_primary_cap": (
                aggregation.method == selected_aggregation.method
                and abs(float(aggregation.rho) - float(selected_aggregation.rho)) < 1e-12
            ),
        })
    table = pd.DataFrame(rows)
    best_index = table.sort_values(
        ["validation_selection_score", "validation_specimen_balanced_accuracy",
         "validation_layer_balanced_accuracy"],
        ascending=False, kind="mergesort",
    ).index[0]
    table["validation_winner_all_pooling_methods"] = table.index == best_index
    return table


def _metrics_rows(
    indicator: str,
    window: pd.DataFrame,
    layer: pd.DataFrame,
    specimen: pd.DataFrame,
    dataset: str,
) -> List[dict]:
    rows: List[dict] = []
    observable = window["window_training_eligible"].to_numpy(dtype=bool)
    for level, table, mask in [
        ("window", window, observable),
        ("layer", layer, np.ones(len(layer), dtype=bool)),
        ("specimen", specimen, np.ones(len(specimen), dtype=bool)),
    ]:
        subset = table.loc[mask]
        metrics = binary_metrics(
            subset["true_binary_label"].to_numpy(dtype=int),
            subset["predicted_binary_label"].to_numpy(dtype=int),
        )
        rows.append({
            "dataset": dataset,
            "level": level,
            "indicator_used": indicator,
            "uses_process_parameter_combination": False,
            **metrics,
            "state_accuracy": _state_accuracy(subset),
        })
    return rows


def create_hierarchical_figures(
    output: Path,
    candidate_metrics: pd.DataFrame,
    pooling_comparison: pd.DataFrame,
    level_metrics: pd.DataFrame,
    specimen_results: pd.DataFrame,
) -> List[str]:
    plt = configure_matplotlib()
    figure_dir = output / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    saved: List[str] = []
    blue, orange, green, red = "#2F5597", "#D97A2B", "#3A7D44", "#B33A3A"

    present_families = set(candidate_metrics["indicator_family"].astype(str))
    family_order = [
        family for family, _ in INDICATOR_SPECS if family in present_families
    ]
    model_labels = {
        "logistic": "LR", "svm_rbf": "RBF-SVM",
        "random_forest": "RF", "extra_trees": "ET",
    }
    model_colors = {
        "logistic": blue, "svm_rbf": orange,
        "random_forest": green, "extra_trees": "#7A5195",
    }
    all_candidates = candidate_metrics.copy()
    all_candidates["family_order"] = all_candidates["indicator_family"].map(
        {name: index for index, name in enumerate(family_order)}
    )
    all_candidates["model_order"] = all_candidates["model_kind"].map(
        {name: index for index, name in enumerate(MODEL_ORDER)}
    )
    all_candidates = all_candidates.sort_values(
        ["family_order", "model_order"], kind="mergesort"
    ).reset_index(drop=True)
    labels = [
        f"{row.indicator_family} | {model_labels[str(row.model_kind)]}"
        for row in all_candidates.itertuples(index=False)
    ]
    colors = [model_colors[str(model)] for model in all_candidates["model_kind"]]
    metrics = [
        ("validation_specimen_balanced_accuracy", "Validation\nspecimen BA"),
        ("test_window_balanced_accuracy", "Test window\nBA"),
        ("test_layer_balanced_accuracy", "Test layer\nBA"),
        ("test_specimen_balanced_accuracy", "Test specimen\nBA"),
        ("test_specimen_state_accuracy", "Test seven-state\naccuracy"),
    ]
    y = np.arange(len(all_candidates))
    combination_count = len(all_candidates)
    fig, axes = plt.subplots(
        1, len(metrics), figsize=(15.8, max(9.2, 0.29 * combination_count + 2.2)), sharey=True,
        gridspec_kw={"wspace": 0.08},
    )
    selected_mask = all_candidates["selected_hierarchical_indicator"].to_numpy(dtype=bool)
    comparison_winner_mask = all_candidates["validation_winner_all_families"].to_numpy(dtype=bool)
    for axis, (column, title) in zip(axes, metrics):
        bars = axis.barh(
            y, all_candidates[column].to_numpy(dtype=float),
            color=colors, height=0.68,
        )
        for bar, selected_flag, comparison_flag in zip(
            bars, selected_mask, comparison_winner_mask
        ):
            if selected_flag:
                bar.set_edgecolor(red)
                bar.set_linewidth(1.8)
            elif comparison_flag:
                bar.set_edgecolor("#7A5195")
                bar.set_linewidth(1.8)
        axis.axvline(0.90, color=red, linestyle="--", linewidth=0.9)
        axis.set_xlim(0, 1.03)
        axis.set_title(title, fontsize=10)
        axis.set_xlabel("Score")
        axis.grid(axis="x", color="#E5E7EB", linewidth=0.6)
        axis.tick_params(axis="x", labelsize=8)
    axes[0].set_yticks(y, labels, fontsize=8)
    axes[0].invert_yaxis()
    from matplotlib.patches import Patch
    legend_handles = [
        Patch(facecolor=model_colors[model], label=model_labels[model])
        for model in MODEL_ORDER
    ]
    legend_handles.append(Patch(facecolor="white", edgecolor=red, linewidth=1.8,
                                label="Selected AFP primary"))
    legend_handles.append(Patch(facecolor="white", edgecolor="#7A5195", linewidth=1.8,
                                label="All-family validation winner"))
    fig.suptitle(
        f"All {combination_count} indicator-model combinations across the AFP hierarchy",
        y=0.995,
    )
    fig.legend(handles=legend_handles, frameon=False, ncol=6,
               loc="lower center", bbox_to_anchor=(0.5, 0.005))
    fig.subplots_adjust(left=0.19, right=0.995, top=0.94, bottom=0.07)
    saved += save_figure(
        fig, figure_dir,
        f"Fig13_All_{combination_count}_Indicator_Model_Combinations",
    )
    plt.close(fig)

    # The two summary figures contain Chinese labels.  Use an installed CJK
    # font explicitly so that saved PNG/PDF/SVG files do not contain tofu
    # boxes when the global SCI style defaults to Times New Roman.
    plt.rcParams["font.family"] = "Microsoft YaHei"
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    # Best validation-selected model from each non-duplicate indicator family.
    # MPRF-HI is retained in the factorial table for historical compatibility,
    # but omitted here because it is mathematically identical to PR-HI.
    summary_order = [family for family in family_order if family != "MPRF-HI"]
    best_by_family = (
        candidate_metrics.sort_values(
            ["validation_selection_score", "validation_specimen_balanced_accuracy"],
            ascending=False,
        )
        .drop_duplicates("indicator_family")
        .set_index("indicator_family")
        .loc[summary_order]
        .reset_index()
    )
    six_metrics = [
        ("validation_selection_score", "验证选择分数"),
        ("validation_specimen_balanced_accuracy", "验证试样平衡准确率"),
        ("test_window_balanced_accuracy", "测试窗口平衡准确率"),
        ("test_layer_balanced_accuracy", "测试层平衡准确率"),
        ("test_specimen_balanced_accuracy", "测试试样平衡准确率"),
        ("test_specimen_state_accuracy", "测试试样七状态准确率"),
    ]
    y_summary = np.arange(len(best_by_family))
    fig, axes = plt.subplots(2, 3, figsize=(16.2, max(9.0, 0.45 * len(best_by_family) + 4.2)))
    for axis, (column, title) in zip(axes.ravel(), six_metrics):
        values = best_by_family[column].to_numpy(dtype=float)
        bars = axis.barh(y_summary, values, color="#2A78B0", height=0.68)
        axis.set_yticks(y_summary, best_by_family["indicator_family"].astype(str), fontsize=9)
        axis.invert_yaxis()
        axis.set_xlim(0.0, 1.08)
        axis.set_title(title, fontsize=12)
        axis.grid(axis="x", color="#E5E7EB", linewidth=0.6)
        for bar, value in zip(bars, values):
            axis.text(
                min(float(value) + 0.012, 1.035),
                bar.get_y() + bar.get_height() / 2,
                f"{value:.3f}", va="center", ha="left", fontsize=8,
            )
    fig.tight_layout(w_pad=2.0, h_pad=2.2)
    saved += save_figure(fig, figure_dir, "Fig16_Best_Indicator_Families_Six_Metrics")
    plt.close(fig)

    marker_styles = ["o", "s", "^", "D", "P", "X"]
    metric_colors = ["#1F77B4", "#FF7F0E", "#2CA02C", "#D62728", "#9467BD", "#8C564B"]
    model_labels_cn = {
        "logistic": "逻辑回归", "svm_rbf": "RBF-SVM",
        "random_forest": "随机森林", "extra_trees": "极端随机树",
    }
    dot_labels = [
        f"{row.indicator_family} | {model_labels_cn[str(row.model_kind)]}"
        for row in best_by_family.itertuples(index=False)
    ]
    metric_matrix = best_by_family[[item[0] for item in six_metrics]].to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(13.0, max(7.2, 0.64 * len(best_by_family) + 2.6)))
    for row_index in range(len(best_by_family)):
        row_values = metric_matrix[row_index]
        ax.hlines(row_index, float(row_values.min()), float(row_values.max()), color="#1F77B4", linewidth=1.2)
        for metric_index, value in enumerate(row_values):
            ax.scatter(
                value, row_index, s=62, marker=marker_styles[metric_index],
                color=metric_colors[metric_index], zorder=3,
                label=six_metrics[metric_index][1] if row_index == 0 else None,
            )
    ax.set_yticks(np.arange(len(dot_labels)), dot_labels, fontsize=9)
    ax.invert_yaxis()
    lower = max(0.0, float(metric_matrix.min()) - 0.08)
    ax.set_xlim(lower, 1.03)
    ax.set_xlabel("得分 / 准确率")
    ax.set_title("最优模型总览：AFP机理、文献复现与经典健康指标")
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.6)
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.10), fontsize=8)
    fig.subplots_adjust(left=0.25, right=0.98, top=0.92, bottom=0.18)
    saved += save_figure(fig, figure_dir, "Fig17_Best_Indicator_Families_Nature_Dotplot")
    plt.close(fig)

    test = level_metrics[level_metrics["dataset"].eq("test_all")]
    levels = ["window", "layer", "specimen"]
    values = [float(test.loc[test["level"].eq(level), "balanced_accuracy"].iloc[0]) for level in levels]
    state_values = [float(test.loc[test["level"].eq(level), "state_accuracy"].iloc[0]) for level in levels]
    x = np.arange(3)
    fig, ax = plt.subplots(figsize=(7.4, 4.8))
    ax.bar(x - 0.18, values, 0.36, color=blue, label="Binary balanced accuracy")
    ax.bar(x + 0.18, state_values, 0.36, color=green, label="State accuracy")
    ax.set_xticks(x, ["Window", "Layer", "Specimen"])
    ax.set_ylim(0, 1.03)
    ax.set_ylabel("Accuracy")
    ax.set_title("Evidence aggregation across the AFP hierarchy")
    ax.legend(frameon=False, loc="upper center", ncol=2, bbox_to_anchor=(0.5, -0.14))
    ax.grid(axis="y", color="#E5E7EB", linewidth=0.6)
    fig.subplots_adjust(bottom=0.22)
    saved += save_figure(fig, figure_dir, "Fig14_Window_Layer_Specimen_Performance")
    plt.close(fig)

    test_specimen = specimen_results[specimen_results["dataset_split"].str.startswith("test_")]
    matrix = pd.crosstab(
        pd.Categorical(test_specimen["true_state"], categories=STATE_ORDER),
        pd.Categorical(test_specimen["predicted_state"], categories=STATE_ORDER),
        dropna=False,
    ).to_numpy(dtype=int)
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    image = ax.imshow(matrix, cmap="Blues", vmin=0, vmax=max(1, int(matrix.max())))
    for row in range(matrix.shape[0]):
        for column in range(matrix.shape[1]):
            value = int(matrix[row, column])
            ax.text(column, row, str(value), ha="center", va="center",
                    color="white" if value > matrix.max() / 2 else "#111827", fontsize=9)
    ax.set_xticks(np.arange(len(STATE_ORDER)), STATE_ORDER, rotation=45, ha="right")
    ax.set_yticks(np.arange(len(STATE_ORDER)), STATE_ORDER)
    ax.set_xlabel("Predicted specimen state")
    ax.set_ylabel("True specimen state")
    ax.set_title("Locked-test specimen confusion matrix")
    fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04, label="Specimens")
    fig.tight_layout()
    saved += save_figure(fig, figure_dir, "Fig15_Hierarchical_Specimen_Confusion")
    plt.close(fig)

    # Pooling ablation: the window classifier is held fixed, so differences
    # isolate only window->layer->specimen evidence aggregation.
    pooling = pooling_comparison.reset_index(drop=True)
    pooling_labels = pooling["aggregation_label"].astype(str).tolist()
    pooling_metrics = [
        ("validation_selection_score", "验证选择分数"),
        ("test_layer_balanced_accuracy", "测试层平衡准确率"),
        ("test_specimen_balanced_accuracy", "测试试样平衡准确率"),
        ("test_specimen_state_accuracy", "测试试样七状态准确率"),
    ]
    y_pool = np.arange(len(pooling))
    selected_pool = pooling["selected_primary_cap"].to_numpy(dtype=bool)
    fig, axes = plt.subplots(
        2, 2, figsize=(13.2, max(7.6, 0.42 * len(pooling) + 4.6)),
        sharey=True,
    )
    for axis, (column, title) in zip(axes.ravel(), pooling_metrics):
        values = pooling[column].to_numpy(dtype=float)
        bars = axis.barh(y_pool, values, color="#2A78B0", height=0.66)
        for bar, selected_flag in zip(bars, selected_pool):
            if selected_flag:
                bar.set_edgecolor(red)
                bar.set_linewidth(2.0)
        axis.set_yticks(y_pool, pooling_labels, fontsize=8)
        axis.invert_yaxis()
        axis.set_xlim(0.0, 1.08)
        axis.set_title(title, fontsize=11)
        axis.grid(axis="x", color="#E5E7EB", linewidth=0.6)
        for bar, value in zip(bars, values):
            axis.text(
                min(float(value) + 0.012, 1.035),
                bar.get_y() + bar.get_height() / 2,
                f"{value:.3f}", va="center", ha="left", fontsize=8,
            )
    fig.suptitle("固定窗口模型下的层次聚合方法对比（红框：验证集选定CAP）", y=0.995)
    fig.tight_layout(w_pad=2.0, h_pad=2.0)
    saved += save_figure(fig, figure_dir, "Fig18_CAP_MIL_Aggregation_Comparison")
    plt.close(fig)
    return saved


def append_hierarchical_method_section(path: Path, result: HierarchicalBenchmarkResult) -> None:
    marker = "## 9. 窗口—层—完整试样三级状态预警（v13.6）"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    for previous_marker in [marker, "## 9. 窗口—层—完整试样三级状态预警（v13.5）"]:
        if previous_marker in existing:
            existing = existing.split(previous_marker, 1)[0].rstrip()
    q = result.summary
    indicator_label = str(q["selected_hierarchical_indicator"]).replace("|", "\\|")
    lines = [
        marker, "",
        "### 9.1 试样一致的反事实注入", "",
        "异常类型和严重度只在完整试样级分配，同一试样5层共享状态。功率异常只在有效加热窗口形成热响应，"
        "速度异常形成热驻留与转速响应，压实力异常只在原始接触窗口形成压力/振动响应；不可观测窗口不作为异常窗口训练样本。",
        "",
        "### 9.2 两阶段窗口模型与层次聚合", "",
        "窗口模型先输出异常概率，再仅对满足热激活/压辊接触门控的异常证据输出六类异常概率。"
        "层级和五层试样级均采用约束自适应池化CAP：a_i=softmax(alpha*p_i)，HI=sum(a_i*p_i)，"
        "alpha=rho*ln(M-1)，从而限制单个窗口/层的最大理论权重不超过0.5。第二阶段异常类型识别"
        "保留物理门控的稀疏尾部证据，避免大量正常窗口稀释局部异常类别。rho仅在验证试样上选择，"
        "相同得分优先rho=0.5；锁定测试集不参与选择。任一试样累计至少2个压实异常接触事件时启用覆盖。",
        "",
        "### 9.3 锁定测试结果", "",
        "| 使用指标 | 窗口平衡准确率 | 层平衡准确率 | 试样平衡准确率 | 试样七状态准确率 |",
        "|---|---:|---:|---:|---:|",
        (
            f"| {indicator_label} | "
            f"{q['test_window_balanced_accuracy']:.4f} | "
            f"{q['test_layer_balanced_accuracy']:.4f} | "
            f"{q['test_specimen_balanced_accuracy']:.4f} | "
            f"{q['test_specimen_state_accuracy']:.4f} |"
        ),
        "",
        (
            f"固定划分重复模型随机种子 {q['stability_repeats']} 次；"
            f"全部最终预警指标均不低于90%的运行是否覆盖全部重复："
            f"{q['stability_all_runs_final_test_pass_90_percent']}；"
            f"跨重复的最低最终准确率为 "
            f"{q['stability_minimum_final_test_warning_accuracy']:.4f}。"
        ),
        f"最终聚合方法为 {q['selected_aggregation_label']}。固定尾部、均值、最大值、Top-10%和CAP参数"
        "的完整对照见 hierarchical_pooling_method_comparison.csv。",
        "",
        "窗口和层用于定位证据，完整试样是主要评价单位。当前异常仍为正常实测数据上的机理约束反事实，"
        "真实部署性能必须由后续采集的独立异常试样验证。", "",
    ]
    path.write_text(existing + "\n\n" + "\n".join(lines), encoding="utf-8")


def run_hierarchical_specimen_benchmark(
    result_dir: Path,
    split_root: Path,
    scaler: FeatureScaler,
    bounds: Mapping[str, Tuple[float, float]],
    ambient: float,
    output: Path,
    seed: int,
    stride: int,
    make_plots: bool = True,
    indicator_families: Sequence[str] | None = None,
    stability_repeats: int = 20,
) -> HierarchicalBenchmarkResult:
    bank, actual_bank, prediction_bank, _ = build_window_bank(
        result_dir, split_root, scaler, stride
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
        for row in specimen_layers.itertuples(index=False) if int(row.layer) < 5
    ]
    assignment = allocate_specimen_splits_and_states(specimen_keys, incomplete_keys, seed)
    ledger, selected_windows = build_layer_ledger(bank, assignment)
    features, window_metadata, literature_indicator_audit = build_hierarchical_window_features(
        ledger, selected_windows, actual_bank, prediction_bank,
        scaler, bounds, ambient, seed,
    )
    candidate_metrics, selected = fit_hierarchical_candidates(
        features, window_metadata, seed, indicator_families=indicator_families
    )
    pooling_comparison = compare_aggregation_methods(selected, window_metadata)
    aggregation = selected["aggregation"]
    window_results, layer_results, specimen_results = aggregate_window_predictions(
        window_metadata,
        np.asarray(selected["binary_scores"], dtype=float),
        np.asarray(selected["type_probabilities"], dtype=float),
        *selected["thresholds"], aggregation=aggregation,
    )
    for table in [window_results, layer_results, specimen_results]:
        table["indicator_used"] = str(selected["candidate"])
        table["uses_process_parameter_combination"] = False

    split = window_metadata["dataset_split"].astype(str).to_numpy()
    validation_mask = split == "validation"
    test_mask = np.char.startswith(split.astype(str), "test_")
    level_rows: List[dict] = []
    for dataset, mask in [("validation", validation_mask), ("test_all", test_mask)]:
        w, l, s = aggregate_window_predictions(
            window_metadata[mask].reset_index(drop=True),
            np.asarray(selected["binary_scores"])[mask],
            np.asarray(selected["type_probabilities"])[mask],
            *selected["thresholds"], aggregation=aggregation,
        )
        level_rows.extend(_metrics_rows(str(selected["candidate"]), w, l, s, dataset))
    level_metrics = pd.DataFrame(level_rows)

    indicator_rows: List[dict] = []
    for dataset, layer_mask in [
        ("validation", layer_results["dataset_split"].eq("validation")),
        ("test_all", layer_results["dataset_split"].str.startswith("test_")),
        ("test_interpolation", layer_results["dataset_split"].eq("test_interpolation")),
        ("test_extrapolation", layer_results["dataset_split"].eq("test_extrapolation")),
    ]:
        subset = layer_results.loc[layer_mask]
        sensor_metrics = binary_metrics(
            subset["true_binary_label"].to_numpy(dtype=int),
            subset["predicted_binary_label"].to_numpy(dtype=int),
        )
        indicator_rows.append({
            "dataset": dataset,
            "indicator_used": str(selected["candidate"]),
            "uses_process_parameter_combination": False,
            **sensor_metrics,
            "seven_state_accuracy": _state_accuracy(subset),
        })
        current_rows = ledger.loc[layer_mask].copy()
        process_state = np.asarray([
            _state_from_parameters(
                abnormal_params(
                    np.asarray([row.p, row.v, row.pr, row.layer], dtype=float),
                    str(row.health_state),
                    deterministic_severity(str(row.full_specimen_id), str(row.health_state), seed),
                    bounds,
                )[0],
                abnormal_params(
                    np.asarray([row.p, row.v, row.pr, row.layer], dtype=float),
                    str(row.health_state),
                    deterministic_severity(str(row.full_specimen_id), str(row.health_state), seed),
                    bounds,
                )[1],
                abnormal_params(
                    np.asarray([row.p, row.v, row.pr, row.layer], dtype=float),
                    str(row.health_state),
                    deterministic_severity(str(row.full_specimen_id), str(row.health_state), seed),
                    bounds,
                )[2],
                bounds,
            ) if str(row.health_state) != NORMAL_STATE else NORMAL_STATE
            for row in current_rows.itertuples(index=False)
        ], dtype=object)
        process_binary = (process_state != NORMAL_STATE).astype(int)
        process_metrics = binary_metrics(
            current_rows["binary_health_label"].to_numpy(dtype=int), process_binary
        )
        indicator_rows.append({
            "dataset": dataset,
            "indicator_used": "PCHI parameter-boundary state rule",
            "uses_process_parameter_combination": True,
            **process_metrics,
            "seven_state_accuracy": float(np.mean(
                process_state == current_rows["health_state"].astype(str).to_numpy()
            )),
        })
    indicator_metrics = pd.DataFrame(indicator_rows)

    fixed_split_stability = fixed_split_tc_hi_stability(
        features,
        window_metadata,
        seed,
        stability_repeats,
        model_kind=str(selected["model_kind"]),
    )
    split_summary = ledger.groupby(
        ["dataset_split", "health_state"], as_index=False
    ).agg(
        full_specimens=("full_specimen_id", "nunique"),
        layer_samples=("layer_sample_id", "size"),
        imputed_layer_samples=("imputed_from_same_condition_other_specimen", "sum"),
    )
    state_balance = ledger.groupby("health_state", as_index=False).agg(
        full_specimens=("full_specimen_id", "nunique"),
        layer_samples=("layer_sample_id", "size"),
    )

    output.mkdir(parents=True, exist_ok=True)
    window_results.to_csv(output / "hierarchical_window_state_results.csv", index=False, encoding=OUTPUT_ENCODING)
    layer_results.to_csv(output / "hierarchical_layer_state_results_130.csv", index=False, encoding=OUTPUT_ENCODING)
    specimen_results.to_csv(output / "hierarchical_specimen_state_results_26.csv", index=False, encoding=OUTPUT_ENCODING)
    candidate_metrics.to_csv(output / "hierarchical_sensor_only_HI_candidates.csv", index=False, encoding=OUTPUT_ENCODING)
    pooling_comparison.to_csv(
        output / "hierarchical_pooling_method_comparison.csv",
        index=False, encoding=OUTPUT_ENCODING,
    )
    literature_indicator_audit.to_csv(
        output / "literature_health_indicator_reproducibility.csv",
        index=False, encoding=OUTPUT_ENCODING,
    )
    level_metrics.to_csv(output / "hierarchical_level_metrics.csv", index=False, encoding=OUTPUT_ENCODING)
    indicator_metrics.to_csv(output / "layer_indicator_metrics.csv", index=False, encoding=OUTPUT_ENCODING)
    fixed_split_stability.to_csv(
        output / "TC_HI_fixed_split_multiseed_stability.csv",
        index=False, encoding=OUTPUT_ENCODING,
    )
    ledger.to_csv(output / "layer_sample_ledger_130.csv", index=False, encoding=OUTPUT_ENCODING)
    assignment.to_csv(output / "specimen_split_state_assignment_26.csv", index=False, encoding=OUTPUT_ENCODING)
    split_summary.to_csv(output / "layer_split_state_summary.csv", index=False, encoding=OUTPUT_ENCODING)
    state_balance.to_csv(output / "layer_state_balance.csv", index=False, encoding=OUTPUT_ENCODING)

    dataset_dir = output.parent / "hierarchical_dataset_v13_3"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(dataset_dir / "all_130_layer_samples.csv", index=False, encoding=OUTPUT_ENCODING)
    assignment.to_csv(dataset_dir / "26_specimen_split_and_state_assignment.csv", index=False, encoding=OUTPUT_ENCODING)
    for split_name, file_name in [
        ("train", "train_75_layer_samples.csv"),
        ("validation", "validation_20_layer_samples.csv"),
        ("test_interpolation", "test_interpolation_15_layer_samples.csv"),
        ("test_extrapolation", "test_extrapolation_20_layer_samples.csv"),
    ]:
        ledger[ledger["dataset_split"].eq(split_name)].to_csv(
            dataset_dir / file_name, index=False, encoding=OUTPUT_ENCODING
        )

    figures = create_hierarchical_figures(
        output, candidate_metrics, pooling_comparison, level_metrics, specimen_results
    ) if make_plots else []
    selected_row = candidate_metrics[candidate_metrics["selected_hierarchical_indicator"]].iloc[0]
    comparison_winner_row = candidate_metrics[
        candidate_metrics["validation_winner_all_families"]
    ].iloc[0]
    test_levels = level_metrics[level_metrics["dataset"].eq("test_all")].set_index("level")
    test_layers = layer_results[
        layer_results["dataset_split"].str.startswith("test_")
    ]
    local_layer_metrics = binary_metrics(
        test_layers["true_binary_label"].to_numpy(dtype=int),
        test_layers["local_predicted_binary_label"].to_numpy(dtype=int),
    )
    local_layer_state_accuracy = float(np.mean(
        test_layers["local_predicted_state"].astype(str)
        == test_layers["true_state"].astype(str)
    ))
    summary = {
        "version": VERSION,
        "state_assignment_seed": int(seed),
        "sample_definition": "one specimen, five layers, one shared state; windows provide local evidence",
        "full_specimens": 26,
        "layer_samples": 130,
        "window_samples": int(len(window_results)),
        "indicator_families": int(candidate_metrics["indicator_family"].nunique()),
        "indicator_model_combinations": int(len(candidate_metrics)),
        "raw_layer_samples": int(ledger["raw_layer_present"].sum()),
        "imputed_training_only_layer_samples": int((~ledger["raw_layer_present"]).sum()),
        "train_full_specimens": int(assignment["dataset_split"].eq("train").sum()),
        "validation_full_specimens": int(assignment["dataset_split"].eq("validation").sum()),
        "test_full_specimens": int(assignment["dataset_split"].str.startswith("test_").sum()),
        "train_layer_samples": int(ledger["dataset_split"].eq("train").sum()),
        "validation_layer_samples": int(ledger["dataset_split"].eq("validation").sum()),
        "test_layer_samples": int(ledger["dataset_split"].str.startswith("test_").sum()),
        "state_counts_full_specimen": TARGET_STATE_COUNTS,
        "state_counts_layer_sample": {
            row.health_state: int(row.layer_samples)
            for row in state_balance.itertuples(index=False)
        },
        "selected_hierarchical_indicator": str(selected["candidate"]),
        "selected_aggregation_method": aggregation.method,
        "selected_aggregation_label": aggregation.label,
        "selected_cap_rho": float(aggregation.rho),
        "aggregation_selection_rule": (
            "CAP rho selected on validation specimens only; ties prefer rho=0.50; "
            "locked test is not used"
        ),
        "selected_sensor_only_indicator": str(selected["candidate"]),
        "selected_sensor_only_indicator_family": str(selected["indicator_family"]),
        "primary_selection_scope": "pre-registered AFP indicator families; literature/statistical methods are comparison baselines",
        "comparison_validation_winner": str(comparison_winner_row["candidate"]),
        "comparison_validation_winner_score": float(
            comparison_winner_row["validation_selection_score"]
        ),
        "comparison_validation_winner_test_specimen_balanced_accuracy": float(
            comparison_winner_row["test_specimen_balanced_accuracy"]
        ),
        "sensor_only_uses_process_parameter_combination": False,
        "window_threshold": float(selected["thresholds"][0]),
        "layer_threshold": float(selected["thresholds"][1]),
        "specimen_threshold": float(selected["thresholds"][2]),
        "validation_specimen_balanced_accuracy": float(selected_row["validation_specimen_balanced_accuracy"]),
        "test_window_balanced_accuracy": float(test_levels.loc["window", "balanced_accuracy"]),
        "test_layer_accuracy": float(test_levels.loc["layer", "accuracy"]),
        "test_layer_balanced_accuracy": float(test_levels.loc["layer", "balanced_accuracy"]),
        "test_layer_state_accuracy": float(test_levels.loc["layer", "state_accuracy"]),
        "test_local_layer_balanced_accuracy_before_consistency": float(
            local_layer_metrics["balanced_accuracy"]
        ),
        "test_local_layer_state_accuracy_before_consistency": (
            local_layer_state_accuracy
        ),
        "test_specimen_accuracy": float(test_levels.loc["specimen", "accuracy"]),
        "test_specimen_balanced_accuracy": float(test_levels.loc["specimen", "balanced_accuracy"]),
        "test_specimen_state_accuracy": float(test_levels.loc["specimen", "state_accuracy"]),
        "selected_sensor_validation_binary_balanced_accuracy": float(selected_row["validation_specimen_balanced_accuracy"]),
        "selected_sensor_test_binary_balanced_accuracy": float(test_levels.loc["layer", "balanced_accuracy"]),
        "selected_sensor_test_seven_state_accuracy": float(test_levels.loc["layer", "state_accuracy"]),
        "stability_protocol": (
            "fixed train/validation/interpolation-test/extrapolation-test specimens; "
            "repeat estimator seeds only; no grouped cross-validation"
        ),
        "stability_repeats": int(len(fixed_split_stability)),
        "stability_all_runs_final_test_pass_90_percent": bool(
            fixed_split_stability[
                "all_test_final_warning_accuracies_at_least_90_percent"
            ].all()
        ),
        "stability_all_runs_local_test_pass_90_percent": bool(
            fixed_split_stability[
                "all_test_local_warning_accuracies_at_least_90_percent"
            ].all()
        ),
        "stability_minimum_final_test_warning_accuracy": float(
            fixed_split_stability["minimum_test_final_warning_accuracy"].min()
        ),
        "stability_minimum_local_test_warning_accuracy": float(
            fixed_split_stability["minimum_test_local_warning_accuracy"].min()
        ),
        "process_rule_test_seven_state_accuracy": 1.0,
        "scientific_boundary": (
            "Window evidence is generated by AFP-constrained counterfactual response; "
            "the independent sample size remains 26 specimens."
        ),
        "hierarchical_consistency_note": (
            "Local window/layer decisions are retained in local_* columns; final "
            "layer/window warning states are back-projected from the five-layer "
            "specimen decision because all five layers share one physical state."
        ),
    }

    # Compatibility fields consumed by the complete v13 pipeline/workbook payload.
    predictions = layer_results.copy()
    long_results = layer_results[[
        "layer_sample_id", "full_specimen_id", "layer", "dataset_split",
        "true_state", "raw_layer_present", "imputed_layer", "indicator_used",
        "uses_process_parameter_combination", "predicted_state", "prediction_correct",
    ]].copy()
    long_results = long_results.rename(columns={"imputed_layer": "imputed"})
    return HierarchicalBenchmarkResult(
        ledger=ledger,
        predictions=predictions,
        long_results=long_results,
        window_results=window_results,
        layer_results=layer_results,
        specimen_results=specimen_results,
        candidate_metrics=candidate_metrics,
        pooling_comparison=pooling_comparison,
        indicator_metrics=indicator_metrics,
        level_metrics=level_metrics,
        split_summary=split_summary,
        state_balance=state_balance,
        fixed_split_stability=fixed_split_stability,
        literature_indicator_audit=literature_indicator_audit,
        selected_sensor_indicator=str(selected["indicator_family"]),
        selected_sensor_candidate=str(selected["candidate"]),
        figures=figures,
        summary=summary,
    )
