# -*- coding: utf-8 -*-
"""Twenty-six-specimen, five-layer AFP health-state benchmark.

One physical specimen is defined by (p, v, pr, specimen label).  Each physical
specimen contributes exactly five layer samples and has one state shared by all
five layers.  Splits are performed at physical-specimen level.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    from .run_physics_guided_health_indicator_v13 import (
        ANOMALY_TYPES,
        OUTPUT_ENCODING,
        FeatureScaler,
        abnormal_params,
        apply_physics_response,
        binary_metrics,
        build_residual_features,
        choose_threshold,
        configure_matplotlib,
        deterministic_severity,
        load_triplet,
        metadata_for_saved_prefix,
        physical_params,
        response_feature_names,
        response_features,
        save_figure,
        sensor_physical,
        stable_group_folds,
        stable_int,
        stable_subsample,
    )
except ImportError:  # Direct script execution.
    from run_physics_guided_health_indicator_v13 import (
        ANOMALY_TYPES,
        OUTPUT_ENCODING,
        FeatureScaler,
        abnormal_params,
        apply_physics_response,
        binary_metrics,
        build_residual_features,
        choose_threshold,
        configure_matplotlib,
        deterministic_severity,
        load_triplet,
        metadata_for_saved_prefix,
        physical_params,
        response_feature_names,
        response_features,
        save_figure,
        sensor_physical,
        stable_group_folds,
        stable_int,
        stable_subsample,
    )
from run_full_prediction_to_warning_v11_5 import fit_coherence_scale_floor


VERSION = "13.2.0"
NORMAL_STATE = "normal"
STATE_ORDER = [NORMAL_STATE, *ANOMALY_TYPES]
TARGET_STATE_COUNTS = {
    NORMAL_STATE: 8,
    "power_low": 3,
    "power_high": 3,
    "speed_low": 3,
    "speed_high": 3,
    "compaction_low": 3,
    "compaction_high": 3,
}
MAX_WINDOWS_PER_LAYER = 64


@dataclass
class LayerBenchmarkResult:
    ledger: pd.DataFrame
    predictions: pd.DataFrame
    long_results: pd.DataFrame
    candidate_metrics: pd.DataFrame
    indicator_metrics: pd.DataFrame
    split_summary: pd.DataFrame
    state_balance: pd.DataFrame
    grouped_cv: pd.DataFrame
    selected_sensor_indicator: str
    selected_sensor_candidate: str
    figures: List[str]
    summary: dict


def _specimen_key(p: float, v: float, pr: float, specimen: str) -> Tuple[int, int, int, str]:
    return int(round(p)), int(round(v)), int(round(pr)), str(specimen)


def _specimen_id(key: Tuple[int, int, int, str]) -> str:
    p, v, pr, specimen = key
    return f"P{p}_V{v}_PR{pr}_{specimen}"


def _layer_id(key: Tuple[int, int, int, str], layer: int) -> str:
    return f"{_specimen_id(key)}_L{int(layer)}"


def build_window_bank(
    result_dir: Path,
    split_root: Path,
    scaler: FeatureScaler,
    stride: int,
) -> Tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray]:
    frames: List[pd.DataFrame] = []
    actual_parts: List[np.ndarray] = []
    prediction_parts: List[np.ndarray] = []
    parameter_parts: List[np.ndarray] = []
    prefix_map = [
        ("train", "train"),
        ("val", "val"),
        ("normal_test", "normal_test"),
        ("ood", "ood"),
    ]
    offset = 0
    for prefix, metadata_prefix in prefix_map:
        arrays_raw = load_triplet(result_dir, prefix)
        metadata_raw = metadata_for_saved_prefix(
            split_root, metadata_prefix, len(arrays_raw["true"])
        )
        arrays, indices = stable_subsample(arrays_raw, stride)
        metadata = metadata_raw.iloc[indices].reset_index(drop=True)
        parameters = physical_params(arrays, scaler)
        actual = sensor_physical(arrays["true"], scaler)
        prediction = sensor_physical(arrays["prediction"], scaler)
        rows = []
        for index in range(len(metadata)):
            p, v, pr, layer = parameters[index]
            specimen = str(metadata.iloc[index]["specimen_label"])
            key = _specimen_key(p, v, pr, specimen)
            rows.append({
                "bank_index": offset + index,
                "source_origin": prefix,
                "source_index": int(indices[index]),
                "source_block_id": str(metadata.iloc[index]["source_block_id"]),
                "segment_id": str(metadata.iloc[index]["segment_id"]),
                "full_specimen_id": _specimen_id(key),
                "specimen_label": specimen,
                "p": key[0], "v": key[1], "pr": key[2],
                "layer": int(round(layer)),
            })
        frames.append(pd.DataFrame(rows))
        actual_parts.append(actual)
        prediction_parts.append(prediction)
        parameter_parts.append(parameters)
        offset += len(metadata)
    bank = pd.concat(frames, ignore_index=True)
    actual_all = np.concatenate(actual_parts, axis=0)
    prediction_all = np.concatenate(prediction_parts, axis=0)
    parameter_all = np.concatenate(parameter_parts, axis=0)
    if not (
        len(bank) == len(actual_all) == len(prediction_all) == len(parameter_all)
    ):
        raise RuntimeError("Window bank arrays are not aligned")
    return bank, actual_all, prediction_all, parameter_all


def allocate_specimen_splits_and_states(
    specimen_keys: Sequence[Tuple[int, int, int, str]],
    incomplete_keys: Sequence[Tuple[int, int, int, str]],
    seed: int,
) -> pd.DataFrame:
    keys = set(specimen_keys)
    external_conditions = {(600, 100, 900), (750, 110, 600)}
    test_external = {
        key for key in keys if key[:3] in external_conditions
    }
    test_internal = {
        (600, 100, 600, "试件2"),
        (650, 100, 450, "试件2"),
        (700, 110, 450, "试件2"),
    }
    validation = {
        (600, 100, 750, "试件2"),
        (650, 110, 600, "试件2"),
        (700, 100, 600, "试件2"),
        (750, 100, 450, "试件2"),
    }
    required = test_external | test_internal | validation
    missing_required = required - keys
    if missing_required:
        raise ValueError(f"Fixed layer split references missing specimens: {missing_required}")
    train = keys - required
    if (len(train), len(validation), len(test_internal), len(test_external)) != (15, 4, 3, 4):
        raise RuntimeError("Expected split counts 15/4/3/4 at full-specimen level")
    if not set(incomplete_keys).issubset(train):
        raise RuntimeError("Incomplete original specimens must remain training-only")

    fixed_states: Dict[Tuple[int, int, int, str], str] = {
        (600, 100, 900, "试件1"): "power_low",
        (600, 100, 900, "试件2"): "power_high",
        (750, 110, 600, "试件1"): "speed_low",
        (750, 110, 600, "试件2"): "speed_high",
        (600, 100, 600, "试件2"): "compaction_low",
        (650, 100, 450, "试件2"): "compaction_high",
        (700, 110, 450, "试件2"): NORMAL_STATE,
        (650, 110, 600, "试件2"): NORMAL_STATE,
        (700, 100, 600, "试件2"): NORMAL_STATE,
        (600, 100, 750, "试件2"): "power_low",
        (750, 100, 450, "试件2"): "compaction_low",
    }
    counts = {state: 0 for state in STATE_ORDER}
    for state in fixed_states.values():
        counts[state] += 1
    remaining = {
        state: TARGET_STATE_COUNTS[state] - counts[state] for state in STATE_ORDER
    }
    for key in incomplete_keys:
        fixed_states[key] = NORMAL_STATE
        remaining[NORMAL_STATE] -= 1
    if any(value < 0 for value in remaining.values()):
        raise RuntimeError(f"Negative remaining state allocation: {remaining}")
    train_unassigned = sorted(
        train - set(fixed_states),
        key=lambda key: stable_int("layer_state_assignment", _specimen_id(key), seed),
    )
    state_tokens = [
        (state, occurrence)
        for state in STATE_ORDER
        for occurrence in range(int(remaining[state]))
    ]
    state_tokens = sorted(
        state_tokens,
        key=lambda token: stable_int(
            "layer_state_schedule", token[0], token[1], seed
        ),
    )
    state_schedule = [state for state, _ in state_tokens]
    if len(train_unassigned) != len(state_schedule):
        raise RuntimeError(
            f"Unassigned train specimens={len(train_unassigned)} != states={len(state_schedule)}"
        )
    for key, state in zip(train_unassigned, state_schedule):
        fixed_states[key] = state

    rows = []
    train_conditions = {key[:3] for key in train}
    for key in sorted(keys):
        if key in train:
            split = "train"
        elif key in validation:
            split = "validation"
        elif key in test_internal:
            split = "test_interpolation"
        else:
            split = "test_extrapolation"
        rows.append({
            "full_specimen_id": _specimen_id(key),
            "p": key[0], "v": key[1], "pr": key[2],
            "specimen_label": key[3],
            "dataset_split": split,
            "process_condition_in_training": bool(key[:3] in train_conditions),
            "health_state": fixed_states[key],
            "binary_health_label": int(fixed_states[key] != NORMAL_STATE),
            "original_layers_available": 5 if key not in incomplete_keys else np.nan,
        })
    assignment = pd.DataFrame(rows)
    observed_counts = assignment["health_state"].value_counts().to_dict()
    if observed_counts != TARGET_STATE_COUNTS:
        raise RuntimeError(
            f"State counts are not the designed counts: {observed_counts}"
        )
    return assignment


def _select_window_indices(indices: np.ndarray, maximum: int) -> np.ndarray:
    indices = np.asarray(indices, dtype=int)
    if len(indices) <= maximum:
        return indices
    positions = np.unique(np.linspace(0, len(indices) - 1, maximum).astype(int))
    return indices[positions]


def build_layer_ledger(
    bank: pd.DataFrame,
    assignment: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict[str, np.ndarray]]:
    assignment_by_id = assignment.set_index("full_specimen_id")
    bank_groups = {
        (int(p), int(v), int(pr), str(specimen), int(layer)): group["bank_index"].to_numpy(dtype=int)
        for (p, v, pr, specimen, layer), group in bank.groupby(
            ["p", "v", "pr", "specimen_label", "layer"], sort=False
        )
    }
    rows = []
    selected_windows: Dict[str, np.ndarray] = {}
    for specimen_id, specimen_row in assignment_by_id.iterrows():
        p = int(specimen_row["p"])
        v = int(specimen_row["v"])
        pr = int(specimen_row["pr"])
        specimen = str(specimen_row["specimen_label"])
        donor = "试件2" if specimen == "试件1" else "试件1"
        for layer in range(5):
            layer_sample_id = f"{specimen_id}_L{layer}"
            key = (p, v, pr, specimen, layer)
            source_indices = bank_groups.get(key, np.asarray([], dtype=int))
            imputed = len(source_indices) == 0
            donor_id = ""
            if imputed:
                donor_key = (p, v, pr, donor, layer)
                source_indices = bank_groups.get(donor_key, np.asarray([], dtype=int))
                donor_id = _specimen_id((p, v, pr, donor))
                if len(source_indices) == 0:
                    raise RuntimeError(f"No same-condition donor for {layer_sample_id}")
                donor_split = str(assignment_by_id.loc[donor_id, "dataset_split"])
                if donor_split != "train":
                    raise RuntimeError(
                        f"Imputation donor {donor_id} for {layer_sample_id} is not training-only"
                    )
            selected = _select_window_indices(source_indices, MAX_WINDOWS_PER_LAYER)
            selected_windows[layer_sample_id] = selected
            rows.append({
                "layer_sample_id": layer_sample_id,
                "full_specimen_id": specimen_id,
                "layer": layer,
                "p": p, "v": v, "pr": pr,
                "specimen_label": specimen,
                "dataset_split": str(specimen_row["dataset_split"]),
                "process_condition_in_training": bool(
                    specimen_row["process_condition_in_training"]
                ),
                "health_state": str(specimen_row["health_state"]),
                "binary_health_label": int(specimen_row["binary_health_label"]),
                "raw_layer_present": not imputed,
                "imputed_from_same_condition_other_specimen": imputed,
                "imputation_source_specimen_id": donor_id,
                "available_window_count": int(0 if imputed else len(source_indices)),
                "source_window_count": int(len(source_indices)),
                "selected_window_count": int(len(selected)),
            })
    ledger = pd.DataFrame(rows).sort_values(
        ["dataset_split", "full_specimen_id", "layer"], kind="mergesort"
    ).reset_index(drop=True)
    if len(ledger) != 130:
        raise RuntimeError(f"Expected 26 x 5 = 130 layer samples, got {len(ledger)}")
    if ledger.groupby("full_specimen_id")["health_state"].nunique().max() != 1:
        raise RuntimeError("A full specimen received more than one health state")
    return ledger, selected_windows


def _aggregate_window_features(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    return np.concatenate([
        np.mean(values, axis=0),
        np.std(values, axis=0),
        np.percentile(values, 5, axis=0),
        np.percentile(values, 95, axis=0),
        np.mean(np.abs(values), axis=0),
        np.max(np.abs(values), axis=0),
    ])


def build_layer_features(
    ledger: pd.DataFrame,
    selected_windows: Mapping[str, np.ndarray],
    actual_bank: np.ndarray,
    prediction_bank: np.ndarray,
    scaler: FeatureScaler,
    bounds: Mapping[str, Tuple[float, float]],
    ambient: float,
    seed: int,
) -> Tuple[Dict[str, np.ndarray], pd.DataFrame]:
    normal_train_actual = []
    normal_train_prediction = []
    for _, row in ledger[
        (ledger["dataset_split"] == "train") &
        (ledger["health_state"] == NORMAL_STATE)
    ].iterrows():
        indices = selected_windows[str(row["layer_sample_id"])]
        normal_train_actual.append(actual_bank[indices])
        normal_train_prediction.append(prediction_bank[indices])
    normal_actual = np.concatenate(normal_train_actual, axis=0)
    normal_prediction = np.concatenate(normal_train_prediction, axis=0)
    coherence_floor = fit_coherence_scale_floor(
        scaler.transform_sensors(normal_actual),
        scaler.transform_sensors(normal_prediction),
    )

    _, response_groups = response_feature_names()
    features: Dict[str, List[np.ndarray]] = {
        "thermal_response": [],
        "compaction_response": [],
        "thermomechanical_response": [],
        "residual": [],
        "response_plus_residual": [],
    }
    feature_audit_rows = []
    for _, row in ledger.iterrows():
        indices = selected_windows[str(row["layer_sample_id"])]
        actual = np.asarray(actual_bank[indices], dtype=float).copy()
        prediction = np.asarray(prediction_bank[indices], dtype=float)
        nominal = np.asarray(
            [row["p"], row["v"], row["pr"], row["layer"]], dtype=float
        )
        state = str(row["health_state"])
        current = nominal.copy()
        severity = 0.0
        if state != NORMAL_STATE:
            severity = deterministic_severity(
                str(row["full_specimen_id"]), state, seed
            )
            current = abnormal_params(nominal, state, severity, bounds)
            actual = np.stack([
                apply_physics_response(window, nominal, current, state, ambient)
                for window in actual
            ])
        response = response_features(actual, prediction, ambient)
        residual = build_residual_features(
            scaler.transform_sensors(actual),
            scaler.transform_sensors(prediction),
            coherence_floor,
        )
        thermal = response[:, response_groups["thermal"]]
        compaction = response[:, response_groups["compaction"]]
        all_response = response[:, response_groups["all"]]
        features["thermal_response"].append(_aggregate_window_features(thermal))
        features["compaction_response"].append(_aggregate_window_features(compaction))
        features["thermomechanical_response"].append(
            _aggregate_window_features(all_response)
        )
        features["residual"].append(_aggregate_window_features(residual))
        features["response_plus_residual"].append(np.concatenate([
            _aggregate_window_features(all_response),
            _aggregate_window_features(residual),
        ]))
        feature_audit_rows.append({
            "layer_sample_id": row["layer_sample_id"],
            "current_p": float(current[0]),
            "current_v": float(current[1]),
            "current_pr": float(current[2]),
            "injection_severity": float(severity),
            "window_count_used": int(len(indices)),
        })
    return (
        {key: np.stack(value) for key, value in features.items()},
        pd.DataFrame(feature_audit_rows),
    )


def _make_classifier(kind: str, seed: int):
    from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC

    if kind == "random_forest":
        return RandomForestClassifier(
            n_estimators=600, min_samples_leaf=1, max_features="sqrt",
            class_weight="balanced_subsample", random_state=seed, n_jobs=-1,
        )
    if kind == "extra_trees":
        return ExtraTreesClassifier(
            n_estimators=600, min_samples_leaf=1, max_features="sqrt",
            class_weight="balanced", random_state=seed, n_jobs=-1,
        )
    if kind == "svm_rbf":
        return make_pipeline(
            StandardScaler(),
            SVC(C=10.0, gamma="scale", class_weight="balanced", probability=True,
                random_state=seed),
        )
    if kind == "logistic":
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(
                C=1.0, class_weight="balanced", max_iter=5000,
                random_state=seed,
            ),
        )
    raise ValueError(kind)


def _abnormal_probability(model, values: np.ndarray) -> np.ndarray:
    probabilities = model.predict_proba(values)
    classes = np.asarray(model.classes_)
    index = int(np.flatnonzero(classes == 1)[0])
    return probabilities[:, index]


def fit_sensor_only_candidates(
    features: Mapping[str, np.ndarray],
    ledger: pd.DataFrame,
    seed: int,
) -> Tuple[pd.DataFrame, dict]:
    from sklearn.metrics import balanced_accuracy_score, recall_score

    split = ledger["dataset_split"].astype(str).to_numpy()
    train_mask = split == "train"
    validation_mask = split == "validation"
    test_mask = np.char.startswith(split.astype(str), "test_")
    binary_labels = ledger["binary_health_label"].to_numpy(dtype=int)
    state_labels = ledger["health_state"].astype(str).to_numpy()
    specs = []
    for family, feature_key in [
        ("T-HI", "thermal_response"),
        ("C-HI", "compaction_response"),
        ("TC-HI", "thermomechanical_response"),
        ("RFHI", "residual"),
    ]:
        for model_kind in ["logistic", "svm_rbf", "random_forest", "extra_trees"]:
            specs.append((family, feature_key, model_kind))
    for model_kind in ["logistic", "svm_rbf"]:
        specs.append(("PR-HI", "response_plus_residual", model_kind))
    for model_kind in ["random_forest", "extra_trees"]:
        specs.append(("MPRF-HI", "response_plus_residual", model_kind))

    rows = []
    fitted = []
    for index, (family, feature_key, model_kind) in enumerate(specs):
        values = features[feature_key]
        binary_model = _make_classifier(model_kind, seed + 101 * index)
        state_model = _make_classifier(model_kind, seed + 101 * index + 1)
        binary_model.fit(values[train_mask], binary_labels[train_mask])
        state_model.fit(values[train_mask], state_labels[train_mask])
        val_score = _abnormal_probability(binary_model, values[validation_mask])
        threshold = choose_threshold(binary_labels[validation_mask], val_score)
        test_score = _abnormal_probability(binary_model, values[test_mask])
        val_binary_pred = (val_score >= threshold).astype(int)
        test_binary_pred = (test_score >= threshold).astype(int)
        val_state_pred = state_model.predict(values[validation_mask]).astype(str)
        test_state_pred = state_model.predict(values[test_mask]).astype(str)
        val_binary = binary_metrics(binary_labels[validation_mask], val_binary_pred)
        test_binary = binary_metrics(binary_labels[test_mask], test_binary_pred)
        val_state_accuracy = float(np.mean(
            val_state_pred == state_labels[validation_mask]
        ))
        test_state_accuracy = float(np.mean(
            test_state_pred == state_labels[test_mask]
        ))
        validation_states = np.unique(state_labels[validation_mask])
        val_macro_recall = float(recall_score(
            state_labels[validation_mask], val_state_pred,
            labels=validation_states, average="macro", zero_division=0,
        ))
        test_macro_recall = float(recall_score(
            state_labels[test_mask], test_state_pred,
            labels=STATE_ORDER, average="macro", zero_division=0,
        ))
        selection_score = (
            0.70 * val_binary["balanced_accuracy"]
            + 0.20 * val_state_accuracy
            + 0.10 * val_macro_recall
        )
        rows.append({
            "indicator_family": family,
            "candidate": f"{family} | {model_kind}",
            "feature_key": feature_key,
            "model_kind": model_kind,
            "uses_process_parameter_combination": False,
            "threshold_selected_on_validation": float(threshold),
            "validation_selection_score": float(selection_score),
            "validation_binary_accuracy": val_binary["accuracy"],
            "validation_binary_balanced_accuracy": val_binary["balanced_accuracy"],
            "validation_state_accuracy": val_state_accuracy,
            "validation_state_macro_recall": val_macro_recall,
            "test_binary_accuracy": test_binary["accuracy"],
            "test_binary_balanced_accuracy": test_binary["balanced_accuracy"],
            "test_state_accuracy": test_state_accuracy,
            "test_state_macro_recall": test_macro_recall,
        })
        fitted.append({
            "binary_model": binary_model,
            "state_model": state_model,
            "threshold": float(threshold),
            "feature_key": feature_key,
            "candidate": f"{family} | {model_kind}",
            "indicator_family": family,
        })
    table = pd.DataFrame(rows)
    selected_index = int(table["validation_selection_score"].idxmax())
    table["selected_sensor_only_indicator"] = table.index == selected_index
    table = table.sort_values(
        ["validation_selection_score", "validation_binary_balanced_accuracy"],
        ascending=False,
    ).reset_index(drop=True)
    return table, fitted[selected_index]


def _state_from_parameters(
    current_p: float,
    current_v: float,
    current_pr: float,
    bounds: Mapping[str, Tuple[float, float]],
) -> str:
    tolerance = 0.5
    if current_p < bounds["p"][0] - tolerance:
        return "power_low"
    if current_p > bounds["p"][1] + tolerance:
        return "power_high"
    if current_v < bounds["v"][0] - tolerance:
        return "speed_low"
    if current_v > bounds["v"][1] + tolerance:
        return "speed_high"
    if current_pr < bounds["pr"][0] - tolerance:
        return "compaction_low"
    if current_pr > bounds["pr"][1] + tolerance:
        return "compaction_high"
    return NORMAL_STATE


def grouped_sensor_tenfold(
    selected: Mapping[str, object],
    features: Mapping[str, np.ndarray],
    ledger: pd.DataFrame,
    seed: int,
) -> pd.DataFrame:
    train_mask = ledger["dataset_split"].astype(str).to_numpy() == "train"
    validation_mask = ledger["dataset_split"].astype(str).to_numpy() == "validation"
    x = features[str(selected["feature_key"])]
    y = ledger["binary_health_label"].to_numpy(dtype=int)
    groups = ledger["full_specimen_id"].astype(str).to_numpy()
    train_indices = np.flatnonzero(train_mask)
    fold_ids = stable_group_folds(groups[train_mask], 10, seed)
    rows = []
    model_kind = str(selected["candidate"]).split(" | ")[-1]
    for fold in range(10):
        fit_indices = train_indices[fold_ids != fold]
        hold_indices = train_indices[fold_ids == fold]
        if len(hold_indices) == 0:
            continue
        model = _make_classifier(model_kind, seed + 1000 + fold)
        model.fit(x[fit_indices], y[fit_indices])
        val_score = _abnormal_probability(model, x[validation_mask])
        threshold = choose_threshold(y[validation_mask], val_score)
        hold_score = _abnormal_probability(model, x[hold_indices])
        rows.append({
            "fold": fold + 1,
            "held_out_specimens": int(len(np.unique(groups[hold_indices]))),
            "held_out_layer_samples": int(len(hold_indices)),
            "threshold_from_fixed_validation": float(threshold),
            **binary_metrics(y[hold_indices], hold_score >= threshold),
        })
    return pd.DataFrame(rows)


def create_layer_figures(
    output: Path,
    ledger: pd.DataFrame,
    candidate_metrics: pd.DataFrame,
    predictions: pd.DataFrame,
) -> List[str]:
    plt = configure_matplotlib()
    figure_dir = output / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    saved: List[str] = []
    colors = {
        "blue": "#2F5597", "orange": "#D97A2B", "green": "#3A7D44",
        "red": "#B33A3A", "gray": "#6B7280", "pale": "#CBD5E1",
    }

    allocation = ledger.groupby(
        ["dataset_split", "health_state"], as_index=False
    ).size().rename(columns={"size": "layer_samples"})
    pivot = allocation.pivot(
        index="dataset_split", columns="health_state", values="layer_samples"
    ).fillna(0).reindex(columns=STATE_ORDER, fill_value=0)
    fig, ax = plt.subplots(figsize=(9.4, 4.8))
    bottom = np.zeros(len(pivot))
    palette = [colors["gray"], "#4472C4", "#5B9BD5", "#70AD47", "#A5A5A5", "#ED7D31", "#C55A11"]
    for state, color in zip(pivot.columns, palette):
        values = pivot[state].to_numpy(dtype=float)
        ax.bar(pivot.index, values, bottom=bottom, label=state, color=color)
        bottom += values
    ax.set_ylabel("Layer samples (one specimen = five layers)")
    ax.set_title("26 specimens x 5 layers: leakage-safe split and state allocation")
    ax.legend(frameon=False, ncol=4, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.14))
    ax.grid(axis="y", color="#E5E7EB", linewidth=0.6)
    fig.subplots_adjust(bottom=0.25)
    saved += save_figure(fig, figure_dir, "Fig10_130_Layer_Sample_Allocation")
    plt.close(fig)

    best = candidate_metrics.sort_values(
        "validation_selection_score", ascending=False
    ).drop_duplicates("indicator_family")
    order = ["T-HI", "C-HI", "TC-HI", "RFHI", "PR-HI", "MPRF-HI"]
    best["order"] = best["indicator_family"].map({name: i for i, name in enumerate(order)})
    best = best.sort_values("order")
    x = np.arange(len(best))
    fig, ax = plt.subplots(figsize=(8.8, 4.8))
    ax.bar(
        x - 0.18, best["validation_binary_balanced_accuracy"], 0.36,
        color=colors["blue"], label="Validation",
    )
    ax.bar(
        x + 0.18, best["test_binary_balanced_accuracy"], 0.36,
        color=colors["orange"], label="Locked test",
    )
    ax.set_xticks(x, best["indicator_family"])
    ax.set_ylim(0, 1.03)
    ax.set_ylabel("Binary balanced accuracy")
    ax.set_title("Sensor-only health indicators (no process-parameter combinations)")
    ax.axhline(0.90, color=colors["red"], linestyle="--", linewidth=1.0, label="90% target")
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.14))
    ax.grid(axis="y", color="#E5E7EB", linewidth=0.6)
    fig.subplots_adjust(bottom=0.23)
    saved += save_figure(fig, figure_dir, "Fig11_Sensor_Only_HI_Bar_Chart")
    plt.close(fig)

    test = predictions[predictions["dataset_split"].str.startswith("test_")]
    matrix = pd.crosstab(
        pd.Categorical(test["health_state"], categories=STATE_ORDER),
        pd.Categorical(test["sensor_predicted_state"], categories=STATE_ORDER),
        dropna=False,
    ).to_numpy(dtype=int)
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    image = ax.imshow(matrix, cmap="Blues", vmin=0, vmax=max(1, int(matrix.max())))
    for row in range(matrix.shape[0]):
        for column in range(matrix.shape[1]):
            value = int(matrix[row, column])
            ax.text(
                column, row, str(value), ha="center", va="center",
                color="white" if value > matrix.max() / 2 else "#111827",
                fontsize=9,
            )
    ax.set_xticks(np.arange(len(STATE_ORDER)), STATE_ORDER, rotation=45, ha="right")
    ax.set_yticks(np.arange(len(STATE_ORDER)), STATE_ORDER)
    ax.set_xlabel("Predicted state")
    ax.set_ylabel("True state")
    ax.set_title("Selected sensor-only HI: locked-test confusion matrix")
    fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04, label="Layer samples")
    fig.tight_layout()
    saved += save_figure(fig, figure_dir, "Fig12_Layer_State_Classification")
    plt.close(fig)
    return saved


def append_layer_specimen_method_section(
    path: Path,
    result: LayerBenchmarkResult,
) -> None:
    """Append the auditable 26-specimen/130-layer definition to the report."""
    marker = "## 9. 26个完整试样×5层的状态预警口径（v13.2）"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if marker in existing:
        existing = existing.split(marker, 1)[0].rstrip()
    summary = result.summary
    test_metrics = result.indicator_metrics[
        result.indicator_metrics["dataset"].eq("test_all")
    ]
    process = test_metrics[
        test_metrics["uses_process_parameter_combination"]
    ].iloc[0]
    sensor = test_metrics[
        ~test_metrics["uses_process_parameter_combination"]
    ].iloc[0]
    lines = [
        marker,
        "",
        "### 9.1 样本定义与状态一致性",
        "",
        "- 一个完整试样由 `(p, v, pr, 试件编号)` 唯一确定；每个完整试样固定产生5个层样本（L0—L4）。",
        "- 同一完整试样的5层共享同一真实状态，禁止在层之间分配不同标签，也禁止把同一试样的层拆到不同数据集。",
        "- 26个完整试样共130个层样本。健康试样8个（40层）；六类异常各3个试样（各15层），因此不同异常状态严格等量。",
        "- 130不能被7种状态整除，所以健康状态不与每一种异常状态强行等量；这里优先满足六类异常等量并保留足够健康基线。",
        f"- 状态分配与模型随机种子固定为 `{summary['state_assignment_seed']}`，可由命令行 `--seed` 显式复现。",
        "",
        "### 9.2 数据集划分",
        "",
        "| 数据集 | 完整试样 | 层样本 | 说明 |",
        "|---|---:|---:|---|",
        "| 训练集 | 15 | 75 | 尽量扩大训练集 |",
        "| 验证集 | 4 | 20 | 为训练集的26.7%，接近四分之一 |",
        "| 内推测试集 | 3 | 15 | 工艺组合在训练集中出现 |",
        "| 外推测试集 | 4 | 20 | 工艺组合完全未进入训练集 |",
        "",
        "旧预测数组实际只有125个可对应层记录：`(600,100,450,试件2)` 缺L3，"
        "`(600,110,300,试件2)` 缺L0—L3。缺少的5层使用同工况另一试件的同层窗口补齐，"
        "并在账本中逐条标记；含补齐记录的试样仅进入训练集，验证集和锁定测试集不含补齐层。",
        "",
        "### 9.3 层级健康指标",
        "",
        "每层最多等间隔抽取64个稳定窗口。对窗口级热响应、压实响应、热—压实耦合响应及"
        "I-ModernTCN预测残差分别计算均值、标准差、5%/95%分位数、绝对均值和绝对最大值，"
        "形成T-HI、C-HI、TC-HI、RFHI、PR-HI与MPRF-HI候选。六类候选均不读取工艺参数组合或"
        "参数越界距离；模型和阈值只由训练集、验证集确定。PCHI单独读取当前p/v/pr与包络边界，"
        "只回答工艺参数状态是否越界。",
        "",
        "### 9.4 锁定测试结果",
        "",
        "| 使用指标 | 使用工艺参数组合 | 二分类准确率 | 二分类平衡准确率 | 七状态准确率 |",
        "|---|---|---:|---:|---:|",
        (
            f"| {process['indicator_used']} | 是 | {process['accuracy']:.4f} | "
            f"{process['balanced_accuracy']:.4f} | {process['seven_state_accuracy']:.4f} |"
        ),
        (
            f"| {sensor['indicator_used']} | 否 | {sensor['accuracy']:.4f} | "
            f"{sensor['balanced_accuracy']:.4f} | {sensor['seven_state_accuracy']:.4f} |"
        ),
        "",
        (
            "按完整试样分组的10折交叉验证准确率为 "
            f"{summary['selected_sensor_grouped_10fold_accuracy_mean']:.4f} ± "
            f"{summary['selected_sensor_grouped_10fold_accuracy_std']:.4f}。"
            "训练集只有15个完整试样且仅5个健康试样，因此10折中的部分折只有单一类别；"
            "折均平衡准确率不适合作为主要结论，应与锁定测试平衡准确率及逐折表一并报告。"
        ),
        "",
        "PCHI的高准确率是标签定义与参数边界规则一致的结果，不能解释为缺陷检测准确率。"
        "无工艺参数组合的传感指标更接近实际部署能力；当前异常仍由正常实测序列上的机理约束"
        "反事实生成，最终结论必须用后续采集的真实异常完整试样复核。",
        "",
    ]
    path.write_text(existing + "\n\n" + "\n".join(lines), encoding="utf-8")


def run_layer_specimen_benchmark(
    result_dir: Path,
    split_root: Path,
    scaler: FeatureScaler,
    bounds: Mapping[str, Tuple[float, float]],
    ambient: float,
    output: Path,
    seed: int,
    stride: int,
    make_plots: bool = True,
) -> LayerBenchmarkResult:
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
        _specimen_key(row.p, row.v, row.pr, row.specimen_label)
        for row in specimen_layers.itertuples(index=False)
    ]
    incomplete_keys = [
        _specimen_key(row.p, row.v, row.pr, row.specimen_label)
        for row in specimen_layers.itertuples(index=False)
        if int(row.layer) < 5
    ]
    assignment = allocate_specimen_splits_and_states(
        specimen_keys, incomplete_keys, seed
    )
    ledger, selected_windows = build_layer_ledger(bank, assignment)
    features, feature_audit = build_layer_features(
        ledger, selected_windows, actual_bank, prediction_bank,
        scaler, bounds, ambient, seed,
    )
    ledger = ledger.merge(feature_audit, on="layer_sample_id", how="left")
    candidate_metrics, selected = fit_sensor_only_candidates(
        features, ledger, seed
    )

    selected_values = features[str(selected["feature_key"])]
    sensor_score = _abnormal_probability(selected["binary_model"], selected_values)
    sensor_binary = (sensor_score >= float(selected["threshold"])).astype(int)
    sensor_state = selected["state_model"].predict(selected_values).astype(str)
    process_state = np.asarray([
        _state_from_parameters(row.current_p, row.current_v, row.current_pr, bounds)
        for row in ledger.itertuples(index=False)
    ], dtype=object)
    process_binary = (process_state != NORMAL_STATE).astype(int)
    predictions = ledger.copy()
    predictions["process_indicator_used"] = "PCHI parameter-boundary state rule"
    predictions["process_uses_parameter_combination"] = True
    predictions["process_predicted_state"] = process_state
    predictions["process_predicted_binary_label"] = process_binary
    predictions["process_prediction_correct"] = np.where(
        process_state == predictions["health_state"].astype(str).to_numpy(),
        "yes", "no",
    )
    predictions["sensor_indicator_used"] = str(selected["candidate"])
    predictions["sensor_uses_parameter_combination"] = False
    predictions["sensor_health_index"] = sensor_score
    predictions["sensor_predicted_binary_label"] = sensor_binary
    predictions["sensor_predicted_state"] = sensor_state
    predictions["sensor_prediction_correct"] = np.where(
        sensor_state == predictions["health_state"].astype(str).to_numpy(),
        "yes", "no",
    )
    predictions["真实状态"] = predictions["health_state"]
    predictions["工艺规则预测状态"] = predictions["process_predicted_state"]
    predictions["传感指标预测状态"] = predictions["sensor_predicted_state"]

    long_rows = []
    for _, row in predictions.iterrows():
        common = {
            "layer_sample_id": row["layer_sample_id"],
            "full_specimen_id": row["full_specimen_id"],
            "layer": int(row["layer"]),
            "dataset_split": row["dataset_split"],
            "true_state": row["health_state"],
            "raw_layer_present": bool(row["raw_layer_present"]),
            "imputed": bool(row["imputed_from_same_condition_other_specimen"]),
        }
        long_rows.extend([
            {
                **common,
                "indicator_used": row["process_indicator_used"],
                "uses_process_parameter_combination": True,
                "predicted_state": row["process_predicted_state"],
                "prediction_correct": row["process_prediction_correct"],
            },
            {
                **common,
                "indicator_used": row["sensor_indicator_used"],
                "uses_process_parameter_combination": False,
                "predicted_state": row["sensor_predicted_state"],
                "prediction_correct": row["sensor_prediction_correct"],
            },
        ])
    long_results = pd.DataFrame(long_rows)

    indicator_rows = []
    for split_name, mask in [
        ("validation", predictions["dataset_split"] == "validation"),
        ("test_all", predictions["dataset_split"].str.startswith("test_")),
        ("test_interpolation", predictions["dataset_split"] == "test_interpolation"),
        ("test_extrapolation", predictions["dataset_split"] == "test_extrapolation"),
    ]:
        true_binary = predictions.loc[mask, "binary_health_label"].to_numpy(dtype=int)
        process_pred = predictions.loc[mask, "process_predicted_binary_label"].to_numpy(dtype=int)
        sensor_pred = predictions.loc[mask, "sensor_predicted_binary_label"].to_numpy(dtype=int)
        true_state = predictions.loc[mask, "health_state"].astype(str).to_numpy()
        for indicator, uses_parameters, binary_pred, state_column in [
            ("PCHI parameter-boundary state rule", True, process_pred, "process_predicted_state"),
            (str(selected["candidate"]), False, sensor_pred, "sensor_predicted_state"),
        ]:
            state_pred = predictions.loc[mask, state_column].astype(str).to_numpy()
            indicator_rows.append({
                "dataset": split_name,
                "indicator_used": indicator,
                "uses_process_parameter_combination": uses_parameters,
                **binary_metrics(true_binary, binary_pred),
                "seven_state_accuracy": float(np.mean(true_state == state_pred)),
            })
    indicator_metrics = pd.DataFrame(indicator_rows)
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
    grouped_cv = grouped_sensor_tenfold(selected, features, ledger, seed)

    dataset_dir = output.parent / "layer_dataset_v13_2"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(dataset_dir / "all_130_layer_samples.csv", index=False, encoding=OUTPUT_ENCODING)
    assignment.to_csv(dataset_dir / "26_specimen_split_and_state_assignment.csv", index=False, encoding=OUTPUT_ENCODING)
    for split_name, file_name in [
        ("train", "train_75_layer_samples.csv"),
        ("validation", "validation_20_layer_samples.csv"),
        ("test_interpolation", "test_interpolation_layer_samples.csv"),
        ("test_extrapolation", "test_extrapolation_layer_samples.csv"),
    ]:
        ledger[ledger["dataset_split"] == split_name].to_csv(
            dataset_dir / file_name, index=False, encoding=OUTPUT_ENCODING
        )

    ledger.to_csv(output / "layer_sample_ledger_130.csv", index=False, encoding=OUTPUT_ENCODING)
    assignment.to_csv(output / "specimen_split_state_assignment_26.csv", index=False, encoding=OUTPUT_ENCODING)
    predictions.to_csv(output / "layer_sample_state_results.csv", index=False, encoding=OUTPUT_ENCODING)
    long_results.to_csv(output / "layer_sample_state_results_long.csv", index=False, encoding=OUTPUT_ENCODING)
    candidate_metrics.to_csv(output / "layer_sensor_only_HI_candidates.csv", index=False, encoding=OUTPUT_ENCODING)
    indicator_metrics.to_csv(output / "layer_indicator_metrics.csv", index=False, encoding=OUTPUT_ENCODING)
    split_summary.to_csv(output / "layer_split_state_summary.csv", index=False, encoding=OUTPUT_ENCODING)
    state_balance.to_csv(output / "layer_state_balance.csv", index=False, encoding=OUTPUT_ENCODING)
    grouped_cv.to_csv(output / "layer_selected_sensor_HI_grouped_10fold.csv", index=False, encoding=OUTPUT_ENCODING)
    figures = (
        create_layer_figures(output, ledger, candidate_metrics, predictions)
        if make_plots else []
    )

    selected_row = candidate_metrics[
        candidate_metrics["selected_sensor_only_indicator"]
    ].iloc[0]
    summary = {
        "version": VERSION,
        "state_assignment_seed": int(seed),
        "model_seed": int(seed),
        "sample_definition": "one full specimen has five layer samples; one state shared by all five layers",
        "full_specimens": 26,
        "layer_samples": 130,
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
        "selected_sensor_only_indicator": str(selected["candidate"]),
        "selected_sensor_only_indicator_family": str(selected["indicator_family"]),
        "sensor_only_uses_process_parameter_combination": False,
        "selected_sensor_validation_binary_balanced_accuracy": float(
            selected_row["validation_binary_balanced_accuracy"]
        ),
        "selected_sensor_test_binary_balanced_accuracy": float(
            selected_row["test_binary_balanced_accuracy"]
        ),
        "selected_sensor_test_seven_state_accuracy": float(
            selected_row["test_state_accuracy"]
        ),
        "selected_sensor_grouped_10fold_accuracy_mean": float(
            grouped_cv["accuracy"].mean()
        ),
        "selected_sensor_grouped_10fold_accuracy_std": float(
            grouped_cv["accuracy"].std(ddof=0)
        ),
        "selected_sensor_grouped_10fold_balanced_accuracy_mean": float(
            grouped_cv["balanced_accuracy"].mean()
        ),
        "process_rule_test_seven_state_accuracy": float(
            indicator_metrics.loc[
                (indicator_metrics["dataset"] == "test_all") &
                indicator_metrics["uses_process_parameter_combination"],
                "seven_state_accuracy",
            ].iloc[0]
        ),
        "scientific_boundary": (
            "Five absent raw layers are transparently imputed from the other specimen "
            "under the same process condition and used only in training; locked validation/test contain no imputed layers."
        ),
    }
    return LayerBenchmarkResult(
        ledger=ledger,
        predictions=predictions,
        long_results=long_results,
        candidate_metrics=candidate_metrics,
        indicator_metrics=indicator_metrics,
        split_summary=split_summary,
        state_balance=state_balance,
        grouped_cv=grouped_cv,
        selected_sensor_indicator=str(selected["indicator_family"]),
        selected_sensor_candidate=str(selected["candidate"]),
        figures=figures,
        summary=summary,
    )
