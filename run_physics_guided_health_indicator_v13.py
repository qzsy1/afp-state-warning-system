# -*- coding: utf-8 -*-
"""Physics-guided AFP health indicators built on I-ModernTCN forecasts.

The development benchmark uses original normal I-ModernTCN forecast windows
and a documented counterfactual process-response generator.  Every base window
produces a paired normal/abnormal sample.  The abnormal label is defined by one
out-of-envelope process parameter (laser power, placement speed, or compaction
force).  The sensor response is generated from AFP-inspired relations:

    line heat input      E_l = P / v
    heating residence    t_h = L_h / v
    compaction exposure  D_c proportional to F / v

The program deliberately reports two different questions:

1. Process compliance: can the measured/set process parameters identify the
   state that is *defined* by those parameters?  Near-perfect accuracy is
   expected and is not a defect-prediction claim.
2. Forming response: can measured thermo-mechanical response and forecast
   residuals identify the consequences without directly reading the parameter
   boundary flag?

Model/threshold selection uses validation data only.  Test data are locked for
selection.  Splits are group-safe at the base-window level.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np
# Load torch before downstream Scikit/MKL computations.  This avoids a Windows
# optree DLL access violation when the CNN-LSTM/AVAE baselines are imported
# lazily after large eigensolver workloads.
import torch  # noqa: F401
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = SCRIPT_DIR.parent
V11_DIR = WORKSPACE / "state_monitor_v11"
V12_DIR = WORKSPACE / "state_monitor_v12"
for import_dir in (WORKSPACE, V11_DIR, V12_DIR):
    if str(import_dir) not in sys.path:
        sys.path.insert(0, str(import_dir))

from autorun_prediction_and_state_warning_v11_6 import (  # noqa: E402
    _has_array_triplet,
    load_triplet,
    stable_subsample,
)
from run_full_prediction_to_warning_v11_5 import (  # noqa: E402
    fit_coherence_scale_floor,
)
from run_health_indicator_v12 import (  # noqa: E402
    build_residual_features,
    calibrate_health_index,
    calibration_scale,
    configure_matplotlib,
    feature_names as residual_feature_names,
    roc_pr,
    save_figure,
    stable_group_folds,
)
from run_original_injection_benchmark_v11_4 import (  # noqa: E402
    OUTPUT_ENCODING,
    RidgeScoreClassifier,
    binary_metrics,
    choose_threshold,
)


VERSION = "13.6.0"
SAMPLING_HZ = 10.0
DT_SECONDS = 1.0 / SAMPLING_HZ
CONTACT_FORCE_THRESHOLD_N = 10.0
CONTACT_MIN_CONSECUTIVE_POINTS = 2
SPECIMEN_MIN_COMPACTION_EVENT_WINDOWS = 2
COMPACTION_PARAMETER_TOLERANCE_N = 0.5
MODEL_FEATURE_COLUMNS = [
    "转速", "位移", "温度1", "温度2", "温度3", "温度4", "温度5", "温度6",
    "温度7", "温度8", "压力", "cycle", "v", "p", "pr", "l", "振动",
]
SENSOR_MODEL_INDICES = np.asarray([*range(0, 11), 16], dtype=int)
SENSOR_NAMES = [
    "rotation_speed", "displacement", "temperature_1", "temperature_2",
    "temperature_3", "temperature_4", "temperature_5", "temperature_6",
    "temperature_7", "temperature_8", "pressure", "vibration",
]
TEMP = np.arange(2, 10, dtype=int)
ROTATION, DISPLACEMENT, PRESSURE, VIBRATION = 0, 1, 10, 11
ANOMALY_TYPES = [
    "power_low", "power_high", "speed_low", "speed_high",
    "compaction_low", "compaction_high",
]


@dataclass
class FeatureScaler:
    mean: np.ndarray
    scale: np.ndarray
    source: Path

    def inverse_full(self, values: np.ndarray) -> np.ndarray:
        return np.asarray(values, dtype=float) * self.scale + self.mean

    def inverse_sensors(self, values: np.ndarray) -> np.ndarray:
        return (
            np.asarray(values, dtype=float) * self.scale[SENSOR_MODEL_INDICES]
            + self.mean[SENSOR_MODEL_INDICES]
        )

    def transform_sensors(self, values: np.ndarray) -> np.ndarray:
        return (
            np.asarray(values, dtype=float) - self.mean[SENSOR_MODEL_INDICES]
        ) / self.scale[SENSOR_MODEL_INDICES]

    def inverse_params(self, standardized_condition: np.ndarray) -> np.ndarray:
        indices = np.asarray([13, 12, 14, 15], dtype=int)  # p, v, pr, l
        return (
            np.asarray(standardized_condition, dtype=float) * self.scale[indices]
            + self.mean[indices]
        )


@dataclass
class PreparedSplit:
    name: str
    actual_standardized: np.ndarray
    prediction_standardized: np.ndarray
    actual_physical: np.ndarray
    prediction_physical: np.ndarray
    metadata: pd.DataFrame
    residual_features: np.ndarray
    response_features: np.ndarray
    process_features: np.ndarray

    @property
    def labels(self) -> np.ndarray:
        return self.metadata["true_label"].to_numpy(dtype=int)

    @property
    def groups(self) -> np.ndarray:
        return self.metadata["base_id"].astype(str).to_numpy()


@dataclass
class CandidateResult:
    name: str
    family: str
    input_source: str
    eligibility: str
    feature_key: str
    model_kind: str
    l2: float | None
    top_k: int | None
    model: object
    selected_indices: np.ndarray | None
    threshold: float
    calibration_scale: float
    validation_score: np.ndarray
    test_score: np.ndarray
    validation_metrics: dict
    test_metrics: dict


class FixedScoreModel:
    def __init__(self, feature_index: int):
        self.feature_index = int(feature_index)

    def fit(self, x: np.ndarray, y: np.ndarray) -> "FixedScoreModel":
        return self

    def predict_score(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(x[:, self.feature_index], dtype=float)


class SelectedRidgeModel:
    def __init__(self, l2: float, top_k: int | None, seed: int):
        self.l2 = float(l2)
        self.top_k = None if top_k is None else int(top_k)
        self.seed = int(seed)

    def fit(self, x: np.ndarray, y: np.ndarray, groups: Sequence[str]) -> "SelectedRidgeModel":
        if self.top_k is None or self.top_k >= x.shape[1]:
            self.selected_indices = np.arange(x.shape[1], dtype=int)
            self.feature_scores = np.ones(x.shape[1], dtype=float)
        else:
            self.feature_scores = multicriterion_scores(x, y, groups, self.seed)
            self.selected_indices = np.argsort(
                -self.feature_scores, kind="mergesort"
            )[: self.top_k]
        self.ridge = RidgeScoreClassifier(degree=1, l2=self.l2).fit(
            x[:, self.selected_indices], y
        )
        return self

    def predict_score(self, x: np.ndarray) -> np.ndarray:
        return self.ridge.predict_score(x[:, self.selected_indices])


class ForestHealthIndexModel:
    """Nonlinear scalar HI: mean abnormal vote probability over all trees."""

    def __init__(
        self,
        min_samples_leaf: int,
        seed: int,
        n_estimators: int = 500,
        anomaly_type_weight_cap: float | None = None,
    ):
        self.min_samples_leaf = int(min_samples_leaf)
        self.seed = int(seed)
        self.n_estimators = int(n_estimators)
        self.anomaly_type_weight_cap = anomaly_type_weight_cap

    def fit(
        self,
        x: np.ndarray,
        y: np.ndarray,
        groups: Sequence[str],
        anomaly_types: Sequence[str] | None = None,
    ) -> "ForestHealthIndexModel":
        from sklearn.ensemble import RandomForestClassifier

        self.selected_indices = np.arange(x.shape[1], dtype=int)
        sample_weight = None
        if self.anomaly_type_weight_cap is not None and anomaly_types is not None:
            type_array = np.asarray(anomaly_types, dtype=str)
            labels = np.asarray(y, dtype=int)
            abnormal_types = np.unique(type_array[labels == 1])
            counts = {kind: int(np.sum(type_array == kind)) for kind in abnormal_types}
            mean_count = sum(counts.values()) / max(len(counts), 1)
            sample_weight = np.ones(len(labels), dtype=float)
            for kind, count in counts.items():
                sample_weight[type_array == kind] = min(
                    float(self.anomaly_type_weight_cap), mean_count / max(count, 1)
                )
        self.model = RandomForestClassifier(
            n_estimators=self.n_estimators,
            min_samples_leaf=self.min_samples_leaf,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=self.seed,
            n_jobs=-1,
        ).fit(
            np.asarray(x, dtype=float), np.asarray(y, dtype=int),
            sample_weight=sample_weight,
        )
        self.feature_scores = np.asarray(self.model.feature_importances_, dtype=float)
        return self

    def predict_score(self, x: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(np.asarray(x, dtype=float))[:, 1]


def refit_candidate_model(
    selected: CandidateResult,
    x: np.ndarray,
    y: np.ndarray,
    groups: Sequence[str],
    seed: int,
    anomaly_types: Sequence[str] | None = None,
) -> object:
    if isinstance(selected.model, FixedScoreModel):
        return FixedScoreModel(selected.model.feature_index).fit(x, y)
    if isinstance(selected.model, ForestHealthIndexModel):
        return ForestHealthIndexModel(
            selected.model.min_samples_leaf, seed, selected.model.n_estimators,
            selected.model.anomaly_type_weight_cap,
        ).fit(x, y, groups, anomaly_types)
    return SelectedRidgeModel(
        selected.l2 or 0.1, selected.top_k, seed
    ).fit(x, y, groups)


def stable_int(*parts: object) -> int:
    text = "|".join(str(part) for part in parts)
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], 16)


def json_records(frame: pd.DataFrame) -> List[dict]:
    """Return strict-JSON records (no numpy scalars or NaN tokens)."""
    clean = frame.astype(object).where(pd.notna(frame), None)
    return json.loads(clean.to_json(orient="records", force_ascii=False))


def load_feature_scaler(train_csv: Path) -> FeatureScaler:
    frame = pd.read_csv(train_csv)
    missing = [name for name in MODEL_FEATURE_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError(f"{train_csv} missing model features: {missing}")
    values = frame[MODEL_FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce")
    if values.isna().any().any():
        raise ValueError(f"{train_csv} contains non-numeric model features")
    mean = values.mean(axis=0).to_numpy(dtype=float)
    scale = values.std(axis=0, ddof=0).to_numpy(dtype=float)
    scale = np.where(scale > 1e-12, scale, 1.0)
    return FeatureScaler(mean=mean, scale=scale, source=train_csv)


def load_parameter_bounds(manifest_csv: Path) -> Dict[str, Tuple[float, float]]:
    frame = pd.read_csv(manifest_csv)
    bounds = {}
    for name in ["p", "v", "pr"]:
        if name not in frame.columns:
            raise ValueError(f"{manifest_csv} missing {name}")
        values = pd.to_numeric(frame[name], errors="coerce").dropna()
        bounds[name] = (float(values.min()), float(values.max()))
    return bounds


def physical_params(arrays: Mapping[str, np.ndarray], scaler: FeatureScaler) -> np.ndarray:
    # Saved model order: channel 12=v, 13=p, 14=pr, 15=l.
    standardized = np.column_stack([
        arrays["input"][:, 0, 13],
        arrays["input"][:, 0, 12],
        arrays["input"][:, 0, 14],
        arrays["input"][:, 0, 15],
    ])
    return scaler.inverse_params(standardized)


def sensor_physical(full: np.ndarray, scaler: FeatureScaler) -> np.ndarray:
    return scaler.inverse_sensors(np.asarray(full)[:, :, SENSOR_MODEL_INDICES])


def contact_window_statistics(
    physical_windows: np.ndarray,
    force_threshold_n: float = CONTACT_FORCE_THRESHOLD_N,
    min_consecutive_points: int = CONTACT_MIN_CONSECUTIVE_POINTS,
) -> pd.DataFrame:
    """Return auditable roller-contact evidence for every 24-point window."""
    pressure = np.maximum(np.asarray(physical_windows, dtype=float)[:, :, PRESSURE], 0.0)
    active = pressure > float(force_threshold_n)
    longest_runs = np.zeros(len(active), dtype=int)
    for row_index, row in enumerate(active):
        current = 0
        best = 0
        for value in row:
            current = current + 1 if bool(value) else 0
            best = max(best, current)
        longest_runs[row_index] = best
    active_points = np.sum(active, axis=1).astype(int)
    return pd.DataFrame({
        "contact_pressure_peak_N": np.max(pressure, axis=1),
        "contact_active_points": active_points,
        "contact_longest_consecutive_points": longest_runs,
        "contact_event_eligible": longest_runs >= int(min_consecutive_points),
    })


def reconstruct_window_metadata(
    csv_path: Path,
    expected_windows: int,
    source_origin: str,
    seq_len: int = 24,
    pred_len: int = 24,
) -> pd.DataFrame:
    """Rebuild metadata in the exact shuffle=False order used by I-ModernTCN."""
    frame = pd.read_csv(csv_path, keep_default_na=False)
    specimen_column = "\u8bd5\u4ef6"
    block_key_columns = [
        column for column in [
            "segment_id", "source_block_id", specimen_column, "file", "root",
            "v", "p", "pr", "l",
        ] if column in frame.columns
    ]
    if not block_key_columns:
        raise ValueError(f"{csv_path} has no columns that can identify specimens")
    key_values = frame[block_key_columns].astype(str).to_numpy()
    boundary = np.ones(len(frame), dtype=bool)
    if len(frame) > 1:
        boundary[1:] = np.any(key_values[1:] != key_values[:-1], axis=1)
    starts = np.flatnonzero(boundary)
    ends = np.r_[starts[1:], len(frame)]
    pieces: List[pd.DataFrame] = []
    for group_id, (start, end) in enumerate(zip(starts, ends)):
        count = int(end - start - seq_len - pred_len + 1)
        if count <= 0:
            continue
        first = frame.iloc[int(start)]
        source_block_id = str(first.get("source_block_id", "")).strip()
        if not source_block_id:
            source_block_id = f"BLOCK_{stable_int(source_origin, group_id, *[first[c] for c in block_key_columns]):016x}"
        segment_id = str(first.get("segment_id", source_block_id))
        specimen_label = str(first.get(specimen_column, "unknown_specimen"))
        pieces.append(pd.DataFrame({
            "source_origin": np.repeat(str(source_origin), count),
            "source_block_id": np.repeat(source_block_id, count),
            "segment_id": np.repeat(segment_id, count),
            "specimen_label": np.repeat(specimen_label, count),
            "source_file": np.repeat(str(first.get("file", "")), count),
            "source_root": np.repeat(str(first.get("root", "")), count),
            "window_start_in_segment": np.arange(count, dtype=int),
            "metadata_group_id": np.repeat(int(group_id), count),
        }))
    metadata = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    if len(metadata) != int(expected_windows):
        raise ValueError(
            f"Window metadata mismatch for {csv_path}: reconstructed={len(metadata)}, "
            f"saved_arrays={expected_windows}"
        )
    metadata["specimen_id"] = (
        metadata["source_origin"].astype(str) + "|" + metadata["source_block_id"].astype(str)
    )
    return metadata


def metadata_for_saved_prefix(
    split_root: Path,
    prefix: str,
    expected_windows: int,
) -> pd.DataFrame:
    csv_names = {
        "train": "train_normal.csv",
        "val": "validation_normal.csv",
        "validation": "validation_normal.csv",
        "normal_test": "test_normal_id.csv",
        "ood": "test_ood_unlabeled.csv",
    }
    csv_name = csv_names.get(str(prefix))
    if csv_name is None or not (split_root / csv_name).exists():
        return pd.DataFrame({
            "source_origin": np.repeat(str(prefix), expected_windows),
            "source_block_id": [f"UNMAPPED_{prefix}_{index}" for index in range(expected_windows)],
            "segment_id": [f"UNMAPPED_{prefix}_{index}" for index in range(expected_windows)],
            "specimen_label": np.repeat("metadata_unavailable", expected_windows),
            "source_file": np.repeat("", expected_windows),
            "source_root": np.repeat("", expected_windows),
            "window_start_in_segment": np.zeros(expected_windows, dtype=int),
            "metadata_group_id": np.arange(expected_windows, dtype=int),
            "specimen_id": [f"{prefix}|UNMAPPED_{index}" for index in range(expected_windows)],
        })
    return reconstruct_window_metadata(
        split_root / csv_name, expected_windows, str(prefix)
    )


def active_mask(arrays: Mapping[str, np.ndarray], scaler: FeatureScaler, ambient: float) -> np.ndarray:
    true = sensor_physical(arrays["true"], scaler)
    temperature_peak = np.max(true[:, :, TEMP], axis=(1, 2))
    pressure_peak = np.max(np.maximum(true[:, :, PRESSURE], 0.0), axis=1)
    return (temperature_peak >= ambient + 10.0) | (pressure_peak >= 10.0)


def filter_arrays(
    arrays: Mapping[str, np.ndarray], indices: np.ndarray, mask: np.ndarray
) -> Tuple[Dict[str, np.ndarray], np.ndarray]:
    return (
        {name: np.asarray(values[mask]) for name, values in arrays.items()},
        np.asarray(indices[mask], dtype=int),
    )


def append_array_sets(parts: Sequence[Tuple[Mapping[str, np.ndarray], np.ndarray, str]]) -> Tuple[dict, np.ndarray, np.ndarray]:
    arrays = {
        name: np.concatenate([np.asarray(part[0][name]) for part in parts], axis=0)
        for name in ["input", "prediction", "true"]
    }
    original_indices = np.concatenate([part[1] for part in parts])
    origins = np.concatenate([
        np.asarray([part[2]] * len(part[1]), dtype=object) for part in parts
    ])
    return arrays, original_indices, origins


def anomaly_observability(
    anomaly_type: str, normal_window: np.ndarray, ambient: float
) -> str:
    """Describe whether this short window can show the injected consequence."""
    thermal_observed = bool(np.max(normal_window[:, TEMP]) >= ambient + 10.0)
    contact_observed = bool(np.max(normal_window[:, PRESSURE]) >= 10.0)
    if anomaly_type.startswith("power"):
        return "observable_thermal_response" if thermal_observed else "parameter_only_no_thermal_activity"
    if anomaly_type.startswith("compaction"):
        return "observable_contact_response" if contact_observed else "parameter_only_no_roller_contact"
    return "observable_rotation_response"


def balanced_anomaly_schedule(n_samples: int, split_name: str, seed: int) -> np.ndarray:
    """Return a deterministic six-type schedule with counts differing by <= 1."""
    repeated = np.resize(np.asarray(ANOMALY_TYPES, dtype=object), int(n_samples))
    rng = np.random.default_rng(stable_int("balanced_schedule", split_name, seed) % (2**32 - 1))
    return repeated[rng.permutation(int(n_samples))]


def deterministic_severity(base_id: str, anomaly_type: str, seed: int) -> float:
    rng = np.random.default_rng(
        stable_int("severity", base_id, anomaly_type, seed) % (2**32 - 1)
    )
    return float(rng.uniform(0.25, 1.0))


def abnormal_params(
    nominal: np.ndarray,
    anomaly_type: str,
    severity: float,
    bounds: Mapping[str, Tuple[float, float]],
) -> np.ndarray:
    current = np.asarray(nominal, dtype=float).copy()
    if anomaly_type.startswith("power"):
        low, high = bounds["p"]
        margin = 25.0 + 50.0 * severity
        current[0] = low - margin if anomaly_type.endswith("low") else high + margin
    elif anomaly_type.startswith("speed"):
        low, high = bounds["v"]
        margin = 4.0 + 8.0 * severity
        current[1] = max(10.0, low - margin) if anomaly_type.endswith("low") else high + margin
    elif anomaly_type.startswith("compaction"):
        low, high = bounds["pr"]
        margin = 60.0 + 120.0 * severity
        current[2] = max(10.0, low - margin) if anomaly_type.endswith("low") else high + margin
    else:
        raise ValueError(anomaly_type)
    return current


def apply_physics_response(
    normal: np.ndarray,
    nominal_params: np.ndarray,
    current_params: np.ndarray,
    anomaly_type: str,
    ambient: float,
) -> np.ndarray:
    """Generate a counterfactual AFP response without additive channel shifts."""
    output = np.asarray(normal, dtype=float).copy()
    p0, v0, f0, _ = nominal_params
    p1, v1, f1, _ = current_params

    if anomaly_type.startswith("power") or anomaly_type.startswith("speed"):
        line_energy_ratio = np.clip(
            (p1 / max(v1, 1e-6)) / (p0 / max(v0, 1e-6)), 0.35, 2.0
        )
        original_temp = output[:, TEMP].copy()
        thermal_active = original_temp > ambient
        scaled_temp = ambient + (original_temp - ambient) * line_energy_ratio**0.85
        output[:, TEMP] = np.where(thermal_active, scaled_temp, original_temp)

    if anomaly_type.startswith("speed"):
        speed_ratio = np.clip(v1 / max(v0, 1e-6), 0.5, 1.8)
        output[:, ROTATION] *= speed_ratio

    if anomaly_type.startswith("compaction"):
        contact_active = output[:, PRESSURE] > 10.0
        if np.any(contact_active):
            force_ratio = np.clip(f1 / max(f0, 1e-6), 0.1, 3.0)
            output[contact_active, PRESSURE] *= force_ratio
            # Low force can reduce stability only while the roller is in contact.
            if anomaly_type == "compaction_low":
                centered = output[:, VIBRATION] - np.mean(output[:, VIBRATION])
                output[:, VIBRATION] = (
                    np.mean(output[:, VIBRATION]) + centered / math.sqrt(force_ratio)
                )
    return output


def process_feature_names() -> List[str]:
    return [
        "p_low_violation", "p_high_violation", "v_low_violation", "v_high_violation",
        "pr_low_violation", "pr_high_violation", "max_parameter_violation",
        "sum_parameter_violation", "line_heat_input_P_over_v",
        "compaction_exposure_pr_over_v", "thermomechanical_input_P_pr_over_v",
        "line_heat_low_violation", "line_heat_high_violation",
        "compaction_exposure_low_violation", "compaction_exposure_high_violation",
    ]


def process_features(
    params: np.ndarray,
    bounds: Mapping[str, Tuple[float, float]],
    derived_bounds: Mapping[str, Tuple[float, float]],
) -> np.ndarray:
    params = np.asarray(params, dtype=float)
    p, v, pr = params[:, 0], params[:, 1], params[:, 2]
    violations = []
    for values, name in [(p, "p"), (v, "v"), (pr, "pr")]:
        low, high = bounds[name]
        span = max(high - low, 1e-8)
        violations.extend([
            np.maximum(0.0, (low - values) / span),
            np.maximum(0.0, (values - high) / span),
        ])
    violation_matrix = np.column_stack(violations)
    line_heat = p / np.maximum(v, 1e-8)
    compaction = pr / np.maximum(v, 1e-8)
    coupled = p * pr / np.maximum(v, 1e-8)

    derived_violations = []
    for values, key in [(line_heat, "line_heat"), (compaction, "compaction")]:
        low, high = derived_bounds[key]
        span = max(high - low, 1e-8)
        derived_violations.extend([
            np.maximum(0.0, (low - values) / span),
            np.maximum(0.0, (values - high) / span),
        ])
    return np.column_stack([
        violation_matrix,
        np.max(violation_matrix, axis=1),
        np.sum(violation_matrix, axis=1),
        line_heat,
        compaction,
        coupled,
        *derived_violations,
    ])


def response_feature_names() -> Tuple[List[str], Dict[str, np.ndarray]]:
    names = [
        "thermal_exposure_residual", "thermal_peak_residual", "thermal_mean_residual",
        "thermal_spatial_p95_residual", "thermal_spatial_mean_residual",
        "thermal_roughness_residual", "heating_rate_max_residual",
        "cooling_rate_max_residual", "effective_heating_time_residual",
    ]
    names.extend([f"temperature_exposure_residual_tc{i}" for i in range(1, 9)])
    names.extend([f"temperature_rmse_tc{i}" for i in range(1, 9)])
    names.extend([
        "pressure_impulse_residual", "pressure_peak_residual", "pressure_mean_residual",
        "pressure_contact_time_residual", "pressure_cv_residual", "pressure_roughness_residual",
        "rotation_mean_residual", "rotation_rmse", "rotation_cv_residual",
        "displacement_mean_residual", "displacement_std_residual", "displacement_jump_residual",
        "vibration_rms_residual", "vibration_p95_residual",
        "thermomechanical_dose_residual", "thermomechanical_peak_residual",
        "thermomechanical_normalized_residual",
    ])
    thermal_end = 9 + 8 + 8
    compaction_start = thermal_end
    coupling_start = len(names) - 3
    groups = {
        "thermal": np.arange(0, thermal_end, dtype=int),
        "compaction": np.arange(compaction_start, coupling_start, dtype=int),
        "coupling": np.arange(coupling_start, len(names), dtype=int),
        "all": np.arange(0, len(names), dtype=int),
    }
    return names, groups


def _window_physics(values: np.ndarray, ambient: float) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    temp = values[:, :, TEMP]
    pressure = np.maximum(values[:, :, PRESSURE], 0.0)
    rotation = values[:, :, ROTATION]
    displacement = values[:, :, DISPLACEMENT]
    vibration = values[:, :, VIBRATION]

    temp_excess = np.maximum(temp - ambient, 0.0)
    temp_mean_t = np.mean(temp, axis=2)
    excess_mean_t = np.mean(temp_excess, axis=2)
    thermal_exposure = DT_SECONDS * np.sum(excess_mean_t, axis=1)
    thermal_peak = np.max(temp, axis=(1, 2))
    thermal_mean = np.mean(temp, axis=(1, 2))
    spatial_range = np.max(temp, axis=2) - np.min(temp, axis=2)
    spatial_p95 = np.percentile(spatial_range, 95, axis=1)
    spatial_mean = np.mean(spatial_range, axis=1)
    thermal_roughness = np.mean(np.abs(np.diff(temp_mean_t, axis=1)), axis=1) * SAMPLING_HZ
    rates = np.diff(temp_mean_t, axis=1) * SAMPLING_HZ
    heating_rate = np.max(rates, axis=1)
    cooling_rate = np.max(-rates, axis=1)
    effective_time = thermal_exposure / np.maximum(
        np.max(excess_mean_t, axis=1), 1e-8
    )
    tc_exposure = DT_SECONDS * np.sum(temp_excess, axis=1)

    pressure_impulse = DT_SECONDS * np.sum(pressure, axis=1)
    pressure_peak = np.max(pressure, axis=1)
    pressure_mean = np.mean(pressure, axis=1)
    pressure_contact_time = DT_SECONDS * np.sum(pressure > 10.0, axis=1)
    pressure_cv = np.std(pressure, axis=1) / (pressure_mean + 1e-8)
    pressure_roughness = np.mean(np.abs(np.diff(pressure, axis=1)), axis=1) * SAMPLING_HZ

    rotation_mean = np.mean(rotation, axis=1)
    rotation_cv = np.std(rotation, axis=1) / (np.abs(rotation_mean) + 1e-8)
    displacement_mean = np.mean(displacement, axis=1)
    displacement_std = np.std(displacement, axis=1)
    displacement_jump = np.max(np.abs(np.diff(displacement, axis=1)), axis=1)
    vibration_rms = np.sqrt(np.mean(np.square(vibration), axis=1))
    vibration_p95 = np.percentile(np.abs(vibration), 95, axis=1)

    dose = DT_SECONDS * np.sum(excess_mean_t * pressure, axis=1)
    dose_peak = np.max(excess_mean_t * pressure, axis=1)
    dose_normalized = dose / np.maximum(thermal_exposure * pressure_impulse, 1e-8)
    return np.column_stack([
        thermal_exposure, thermal_peak, thermal_mean, spatial_p95, spatial_mean,
        thermal_roughness, heating_rate, cooling_rate, effective_time,
        tc_exposure,
        pressure_impulse, pressure_peak, pressure_mean, pressure_contact_time,
        pressure_cv, pressure_roughness,
        rotation_mean, rotation_cv, displacement_mean, displacement_std,
        displacement_jump, vibration_rms, vibration_p95,
        dose, dose_peak, dose_normalized,
    ])


def response_features(actual: np.ndarray, prediction: np.ndarray, ambient: float) -> np.ndarray:
    actual_raw = _window_physics(actual, ambient)
    pred_raw = _window_physics(prediction, ambient)
    # Layout from _window_physics: first 9 thermal, 8 TC exposure, then mechanics.
    difference = actual_raw - pred_raw
    tc_actual = actual[:, :, TEMP]
    tc_pred = prediction[:, :, TEMP]
    tc_rmse = np.sqrt(np.mean(np.square(tc_actual - tc_pred), axis=1))
    rotation_rmse = np.sqrt(
        np.mean(np.square(actual[:, :, ROTATION] - prediction[:, :, ROTATION]), axis=1)
    )
    # Reorder to match response_feature_names.
    return np.column_stack([
        difference[:, :9],
        difference[:, 9:17],
        tc_rmse,
        difference[:, 17:23],
        difference[:, 23],
        rotation_rmse,
        difference[:, 24],
        difference[:, 25:28],
        difference[:, 28:30],
        difference[:, 30:33],
    ])


def derived_process_bounds(
    params: np.ndarray,
) -> Dict[str, Tuple[float, float]]:
    p, v, pr = params[:, 0], params[:, 1], params[:, 2]
    line = p / np.maximum(v, 1e-8)
    comp = pr / np.maximum(v, 1e-8)
    return {
        "line_heat": (float(np.min(line)), float(np.max(line))),
        "compaction": (float(np.min(comp)), float(np.max(comp))),
    }


def make_paired_split(
    name: str,
    arrays: Mapping[str, np.ndarray],
    original_indices: np.ndarray,
    origins: Sequence[str],
    source_metadata: pd.DataFrame,
    scaler: FeatureScaler,
    bounds: Mapping[str, Tuple[float, float]],
    derived_bounds_map: Mapping[str, Tuple[float, float]],
    ambient: float,
    seed: int,
    train_conditions: set,
) -> PreparedSplit:
    base_actual = sensor_physical(arrays["true"], scaler)
    base_prediction = sensor_physical(arrays["prediction"], scaler)
    base_params = physical_params(arrays, scaler)
    if len(source_metadata) != len(base_actual):
        raise ValueError(
            f"{name} source metadata length {len(source_metadata)} != windows {len(base_actual)}"
        )
    source_metadata = source_metadata.reset_index(drop=True)
    contact_stats = contact_window_statistics(base_actual)

    actual_physical: List[np.ndarray] = []
    prediction_physical: List[np.ndarray] = []
    current_params_list: List[np.ndarray] = []
    metadata: List[dict] = []
    anomaly_schedule = balanced_anomaly_schedule(len(base_actual), name, seed)
    for i in range(len(base_actual)):
        origin = str(origins[i])
        source_row = source_metadata.iloc[i]
        base_id = f"IMODERN|{name}|{origin}|idx{int(original_indices[i])}"
        nominal = base_params[i]
        condition = tuple(np.round(nominal[:3], 4))
        test_type = (
            "test_interpolation" if condition in train_conditions else "test_extrapolation"
        ) if name == "test" else name
        common = {
            "base_id": base_id,
            "source_origin": origin,
            "source_index": int(original_indices[i]),
            "source_block_id": str(source_row["source_block_id"]),
            "segment_id": str(source_row["segment_id"]),
            "specimen_label": str(source_row["specimen_label"]),
            "specimen_id": str(source_row["specimen_id"]),
            "source_file": str(source_row["source_file"]),
            "source_root": str(source_row["source_root"]),
            "window_start_in_segment": int(source_row["window_start_in_segment"]),
            "contact_reference_peak_N": float(contact_stats.iloc[i]["contact_pressure_peak_N"]),
            "contact_reference_active_points": int(contact_stats.iloc[i]["contact_active_points"]),
            "contact_reference_longest_run": int(contact_stats.iloc[i]["contact_longest_consecutive_points"]),
            "contact_event_eligible": bool(contact_stats.iloc[i]["contact_event_eligible"]),
            "dataset_split": test_type,
            "nominal_p": float(nominal[0]),
            "nominal_v": float(nominal[1]),
            "nominal_pr": float(nominal[2]),
            "layer": float(nominal[3]),
            "seed": int(seed),
        }
        actual_physical.append(base_actual[i])
        prediction_physical.append(base_prediction[i])
        current_params_list.append(nominal.copy())
        metadata.append({
            **common,
            "sample_id": f"{base_id}|normal",
            "true_label": 0,
            "anomaly_type": "none",
            "injected_parameter": "none",
            "severity": 0.0,
            "response_observability": "not_applicable_normal",
            "current_p": float(nominal[0]),
            "current_v": float(nominal[1]),
            "current_pr": float(nominal[2]),
            "label_source": "original_normal_I_ModernTCN_target",
        })

        anomaly_type = str(anomaly_schedule[i])
        severity = deterministic_severity(base_id, anomaly_type, seed)
        response_observability = anomaly_observability(
            anomaly_type, base_actual[i], ambient
        )
        current = abnormal_params(nominal, anomaly_type, severity, bounds)
        abnormal = apply_physics_response(
            base_actual[i], nominal, current, anomaly_type, ambient
        )
        actual_physical.append(abnormal)
        prediction_physical.append(base_prediction[i])
        current_params_list.append(current)
        parameter = "p" if anomaly_type.startswith("power") else "v" if anomaly_type.startswith("speed") else "pr"
        metadata.append({
            **common,
            "sample_id": f"{base_id}|{anomaly_type}",
            "true_label": 1,
            "anomaly_type": anomaly_type,
            "injected_parameter": parameter,
            "severity": float(severity),
            "response_observability": response_observability,
            "current_p": float(current[0]),
            "current_v": float(current[1]),
            "current_pr": float(current[2]),
            "label_source": "balanced_single_out_of_envelope_parameter_with_physics_response_if_observable",
        })

    actual_physical_array = np.stack(actual_physical)
    prediction_physical_array = np.stack(prediction_physical)
    actual_standardized = scaler.transform_sensors(actual_physical_array)
    prediction_standardized = scaler.transform_sensors(prediction_physical_array)
    metadata_frame = pd.DataFrame(metadata)
    current_params_array = np.stack(current_params_list)

    return PreparedSplit(
        name=name,
        actual_standardized=actual_standardized,
        prediction_standardized=prediction_standardized,
        actual_physical=actual_physical_array,
        prediction_physical=prediction_physical_array,
        metadata=metadata_frame,
        residual_features=np.empty((len(metadata_frame), 0)),
        response_features=response_features(
            actual_physical_array, prediction_physical_array, ambient
        ),
        process_features=process_features(
            current_params_array, bounds, derived_bounds_map
        ),
    )


def _normalized_compaction_violation(
    force_value: float,
    bounds: Mapping[str, Tuple[float, float]],
) -> Tuple[float, float, float]:
    low, high = bounds["pr"]
    span = max(float(high - low), 1e-8)
    # Saved standardized arrays can invert 300 N to 299.999923 N.  A 0.5 N
    # engineering tolerance is far below the 60--180 N injected margins and
    # prevents such float32 round-off from becoming a false process alarm.
    low_violation = max(
        0.0, (float(low) - COMPACTION_PARAMETER_TOLERANCE_N - float(force_value)) / span
    )
    high_violation = max(
        0.0, (float(force_value) - float(high) - COMPACTION_PARAMETER_TOLERANCE_N) / span
    )
    return low_violation, high_violation, max(low_violation, high_violation)


def build_sparse_compaction_event_benchmark(
    split: PreparedSplit,
    bounds: Mapping[str, Tuple[float, float]],
    ambient: float,
    seed: int,
    min_event_windows: int = SPECIMEN_MIN_COMPACTION_EVENT_WINDOWS,
    force_threshold_n: float = CONTACT_FORCE_THRESHOLD_N,
    min_consecutive_points: int = CONTACT_MIN_CONSECUTIVE_POINTS,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """Create normal/low/high specimen scenarios with only a few event windows.

    For each real specimen, the highest-pressure eligible contact windows are
    selected.  Only those windows receive a compaction-force counterfactual;
    all remaining windows stay equal to the original normal measurement.
    """
    normal_indices = np.flatnonzero(split.labels == 0)
    normal_metadata = split.metadata.iloc[normal_indices].copy()
    normal_metadata["prepared_row_index"] = normal_indices
    event_contact_stats = contact_window_statistics(
        split.actual_physical[normal_indices],
        force_threshold_n=force_threshold_n,
        min_consecutive_points=min_consecutive_points,
    )
    normal_metadata["contact_reference_peak_N"] = event_contact_stats[
        "contact_pressure_peak_N"
    ].to_numpy()
    normal_metadata["contact_reference_active_points"] = event_contact_stats[
        "contact_active_points"
    ].to_numpy()
    normal_metadata["contact_reference_longest_run"] = event_contact_stats[
        "contact_longest_consecutive_points"
    ].to_numpy()
    normal_metadata["contact_event_eligible"] = event_contact_stats[
        "contact_event_eligible"
    ].to_numpy()
    event_window_rows: List[dict] = []
    specimen_rows: List[dict] = []
    eligibility_rows: List[dict] = []
    scenarios = ["normal", "compaction_low", "compaction_high"]

    for specimen_id, specimen in normal_metadata.groupby("specimen_id", sort=True):
        specimen = specimen.sort_values(
            ["source_index", "window_start_in_segment", "base_id"], kind="mergesort"
        ).reset_index(drop=True)
        eligible = specimen[specimen["contact_event_eligible"].astype(bool)].copy()
        eligible = eligible.sort_values(
            ["contact_reference_peak_N", "source_index"],
            ascending=[False, True], kind="mergesort",
        )
        selected_base_ids = set(
            eligible.head(int(min_event_windows))["base_id"].astype(str)
        )
        sufficient = len(selected_base_ids) >= int(min_event_windows)
        first = specimen.iloc[0]
        eligibility_rows.append({
            "split": split.name,
            "specimen_id": str(specimen_id),
            "specimen_label": str(first["specimen_label"]),
            "source_origin": str(first["source_origin"]),
            "dataset_split": str(first["dataset_split"]),
            "source_file": str(first["source_file"]),
            "n_windows": int(len(specimen)),
            "n_contact_event_eligible_windows": int(len(eligible)),
            "minimum_required_event_windows": int(min_event_windows),
            "included_in_event_benchmark": bool(sufficient),
            "exclusion_reason": "" if sufficient else "fewer_than_required_contact_windows",
        })
        if not sufficient:
            continue

        for scenario in scenarios:
            scenario_id = f"{specimen_id}|{scenario}"
            scenario_window_rows: List[dict] = []
            for _, base_row in specimen.iterrows():
                prepared_index = int(base_row["prepared_row_index"])
                nominal = np.asarray([
                    base_row["nominal_p"], base_row["nominal_v"],
                    base_row["nominal_pr"], base_row["layer"],
                ], dtype=float)
                current = nominal.copy()
                actual = np.asarray(split.actual_physical[prepared_index], dtype=float).copy()
                selected_event_window = (
                    scenario != "normal" and str(base_row["base_id"]) in selected_base_ids
                )
                if selected_event_window:
                    severity = deterministic_severity(
                        str(specimen_id), scenario, seed
                    )
                    current = abnormal_params(nominal, scenario, severity, bounds)
                    actual = apply_physics_response(
                        actual, nominal, current, scenario, ambient
                    )
                else:
                    severity = 0.0

                observed_contact = contact_window_statistics(
                    actual[None, :, :], force_threshold_n, min_consecutive_points
                ).iloc[0]
                low_v, high_v, event_score = _normalized_compaction_violation(
                    current[2], bounds
                )
                reference_gate = bool(base_row["contact_event_eligible"])
                observed_gate = bool(observed_contact["contact_event_eligible"])
                predicted_event = int(reference_gate and event_score > 0.0)
                observed_gate_predicted_event = int(observed_gate and event_score > 0.0)
                true_event = int(selected_event_window)
                row = {
                    "split": split.name,
                    "dataset_split": str(base_row["dataset_split"]),
                    "source_origin": str(base_row["source_origin"]),
                    "specimen_id": str(specimen_id),
                    "specimen_scenario_id": scenario_id,
                    "scenario": scenario,
                    "specimen_label": str(base_row["specimen_label"]),
                    "source_file": str(base_row["source_file"]),
                    "segment_id": str(base_row["segment_id"]),
                    "base_id": str(base_row["base_id"]),
                    "source_index": int(base_row["source_index"]),
                    "window_start_in_segment": int(base_row["window_start_in_segment"]),
                    "contact_reference_peak_N": float(base_row["contact_reference_peak_N"]),
                    "contact_reference_active_points": int(base_row["contact_reference_active_points"]),
                    "contact_reference_longest_run": int(base_row["contact_reference_longest_run"]),
                    "reference_contact_gate": reference_gate,
                    "observed_pressure_peak_N": float(observed_contact["contact_pressure_peak_N"]),
                    "observed_contact_active_points": int(observed_contact["contact_active_points"]),
                    "observed_contact_longest_run": int(observed_contact["contact_longest_consecutive_points"]),
                    "observed_contact_gate": observed_gate,
                    "selected_top_contact_window": bool(str(base_row["base_id"]) in selected_base_ids),
                    "true_window_event": true_event,
                    "predicted_window_event": predicted_event,
                    "observed_gate_predicted_window_event": observed_gate_predicted_event,
                    "window_prediction_correct": int(true_event == predicted_event),
                    "current_pr_N": float(current[2]),
                    "pr_low_violation": float(low_v),
                    "pr_high_violation": float(high_v),
                    "contact_event_health_index": float(event_score if reference_gate else 0.0),
                    "severity": float(severity),
                }
                event_window_rows.append(row)
                scenario_window_rows.append(row)

            scenario_frame = pd.DataFrame(scenario_window_rows)
            true_event_count = int(scenario_frame["true_window_event"].sum())
            predicted_event_count = int(scenario_frame["predicted_window_event"].sum())
            observed_event_count = int(
                scenario_frame["observed_gate_predicted_window_event"].sum()
            )
            true_label = int(true_event_count >= int(min_event_windows))
            predicted_label = int(predicted_event_count >= int(min_event_windows))
            observed_predicted_label = int(observed_event_count >= int(min_event_windows))
            low_count = int(
                ((scenario_frame["predicted_window_event"] == 1) &
                 (scenario_frame["pr_low_violation"] > 0)).sum()
            )
            high_count = int(
                ((scenario_frame["predicted_window_event"] == 1) &
                 (scenario_frame["pr_high_violation"] > 0)).sum()
            )
            predicted_type = (
                "compaction_low" if low_count >= int(min_event_windows)
                else "compaction_high" if high_count >= int(min_event_windows)
                else "normal"
            )
            true_state = "abnormal" if true_label else "normal"
            predicted_state = "abnormal" if predicted_label else "normal"
            specimen_rows.append({
                "split": split.name,
                "dataset_split": str(first["dataset_split"]),
                "source_origin": str(first["source_origin"]),
                "specimen_id": str(specimen_id),
                "specimen_scenario_id": scenario_id,
                "specimen_label": str(first["specimen_label"]),
                "source_file": str(first["source_file"]),
                "true_anomaly_type": scenario,
                "predicted_anomaly_type": predicted_type,
                "n_windows": int(len(scenario_frame)),
                "n_contact_event_eligible_windows": int(
                    scenario_frame["reference_contact_gate"].sum()
                ),
                "minimum_required_event_windows": int(min_event_windows),
                "true_event_window_count": true_event_count,
                "predicted_event_window_count": predicted_event_count,
                "observed_gate_predicted_event_window_count": observed_event_count,
                "specimen_compaction_health_index": float(
                    min(1.0, predicted_event_count / max(int(min_event_windows), 1))
                ),
                "true_label": true_label,
                "predicted_label": predicted_label,
                "observed_gate_predicted_label": observed_predicted_label,
                "true_state": true_state,
                "predicted_state": predicted_state,
                "prediction_correct": "yes" if true_label == predicted_label else "no",
                "observed_gate_prediction_correct": (
                    "yes" if true_label == observed_predicted_label else "no"
                ),
                "\u771f\u5b9e\u72b6\u6001": "\u5f02\u5e38" if true_label else "\u6b63\u5e38",
                "\u9884\u6d4b\u72b6\u6001": "\u5f02\u5e38" if predicted_label else "\u6b63\u5e38",
                "\u9884\u6d4b\u6b63\u786e\u4e0e\u5426": "\u6b63\u786e" if true_label == predicted_label else "\u9519\u8bef",
            })

    window_table = pd.DataFrame(event_window_rows)
    specimen_table = pd.DataFrame(specimen_rows)
    eligibility_table = pd.DataFrame(eligibility_rows)
    if len(specimen_table):
        primary = binary_metrics(
            specimen_table["true_label"].to_numpy(dtype=int),
            specimen_table["predicted_label"].to_numpy(dtype=int),
        )
        observed = binary_metrics(
            specimen_table["true_label"].to_numpy(dtype=int),
            specimen_table["observed_gate_predicted_label"].to_numpy(dtype=int),
        )
        type_accuracy = float(np.mean(
            specimen_table["true_anomaly_type"].astype(str).to_numpy()
            == specimen_table["predicted_anomaly_type"].astype(str).to_numpy()
        ))
    else:
        primary, observed, type_accuracy = {}, {}, float("nan")
    metrics = {
        "split": split.name,
        "specimens_available": int(normal_metadata["specimen_id"].nunique()),
        "specimens_included": int(eligibility_table["included_in_event_benchmark"].sum()) if len(eligibility_table) else 0,
        "scenario_count": int(len(specimen_table)),
        "normal_scenario_count": int(np.sum(specimen_table.get("true_anomaly_type", pd.Series(dtype=str)) == "normal")),
        "compaction_low_scenario_count": int(np.sum(specimen_table.get("true_anomaly_type", pd.Series(dtype=str)) == "compaction_low")),
        "compaction_high_scenario_count": int(np.sum(specimen_table.get("true_anomaly_type", pd.Series(dtype=str)) == "compaction_high")),
        "minimum_event_windows": int(min_event_windows),
        "contact_force_threshold_N": float(force_threshold_n),
        "minimum_consecutive_contact_points": int(min_consecutive_points),
        "compaction_parameter_tolerance_N": float(COMPACTION_PARAMETER_TOLERANCE_N),
        "type_accuracy": type_accuracy,
        **{f"primary_{key}": value for key, value in primary.items()},
        **{f"observed_gate_{key}": value for key, value in observed.items()},
    }
    return window_table, specimen_table, eligibility_table, metrics


def specimen_event_grouped_tenfold(
    specimen_table: pd.DataFrame,
    seed: int,
) -> pd.DataFrame:
    """Group-safe ten-fold audit of the locked deterministic specimen rule."""
    if specimen_table.empty:
        return pd.DataFrame()
    specimen_ids = specimen_table["specimen_id"].astype(str).to_numpy()
    folds = stable_group_folds(specimen_ids, 10, seed)
    rows = []
    for fold in range(10):
        part = specimen_table.loc[folds == fold]
        if part.empty:
            continue
        rows.append({
            "fold": fold + 1,
            "n_specimen_scenarios": int(len(part)),
            "n_unique_specimens": int(part["specimen_id"].nunique()),
            **binary_metrics(
                part["true_label"].to_numpy(dtype=int),
                part["predicted_label"].to_numpy(dtype=int),
            ),
        })
    return pd.DataFrame(rows)


def prepare_benchmark(
    result_dir: Path,
    train_csv: Path,
    manifest_csv: Path,
    stride: int,
    seed: int,
) -> Tuple[PreparedSplit, PreparedSplit, PreparedSplit, dict, FeatureScaler, dict]:
    scaler = load_feature_scaler(train_csv)
    bounds = load_parameter_bounds(manifest_csv)
    split_root = train_csv.parent

    train_raw = load_triplet(result_dir, "train")
    val_prefix = "val" if _has_array_triplet(result_dir, "val") else "validation"
    val_raw = load_triplet(result_dir, val_prefix)
    internal_prefix = "normal_test" if _has_array_triplet(result_dir, "normal_test") else "test"
    internal_raw = load_triplet(result_dir, internal_prefix)

    train_source_metadata_raw = metadata_for_saved_prefix(
        split_root, "train", len(train_raw["true"])
    )
    val_source_metadata_raw = metadata_for_saved_prefix(
        split_root, val_prefix, len(val_raw["true"])
    )
    internal_source_metadata_raw = metadata_for_saved_prefix(
        split_root, internal_prefix, len(internal_raw["true"])
    )

    train, train_indices = stable_subsample(train_raw, stride)
    validation, val_indices = stable_subsample(val_raw, stride)
    internal, internal_indices = stable_subsample(internal_raw, stride)
    train_source_metadata = train_source_metadata_raw.iloc[train_indices].reset_index(drop=True)
    val_source_metadata = val_source_metadata_raw.iloc[val_indices].reset_index(drop=True)
    internal_source_metadata = internal_source_metadata_raw.iloc[internal_indices].reset_index(drop=True)

    train_phys = sensor_physical(train["true"], scaler)
    ambient = float(np.percentile(train_phys[:, :, TEMP], 5.0))
    train_activity = active_mask(train, scaler, ambient)
    train, train_indices = filter_arrays(train, train_indices, train_activity)
    train_source_metadata = train_source_metadata.loc[train_activity].reset_index(drop=True)
    validation_activity = active_mask(validation, scaler, ambient)
    validation, val_indices = filter_arrays(
        validation, val_indices, validation_activity
    )
    val_source_metadata = val_source_metadata.loc[validation_activity].reset_index(drop=True)
    internal_activity = active_mask(internal, scaler, ambient)
    internal, internal_indices = filter_arrays(
        internal, internal_indices, internal_activity
    )
    internal_source_metadata = internal_source_metadata.loc[internal_activity].reset_index(drop=True)

    target_validation = max(1, int(round(len(train["true"]) * 0.25)))
    if len(validation["true"]) > target_validation:
        rng = np.random.default_rng(seed)
        chosen = np.sort(rng.choice(len(validation["true"]), target_validation, replace=False))
        validation = {name: np.asarray(values[chosen]) for name, values in validation.items()}
        val_indices = val_indices[chosen]
        val_source_metadata = val_source_metadata.iloc[chosen].reset_index(drop=True)

    test_parts: List[Tuple[Mapping[str, np.ndarray], np.ndarray, str]] = [
        (internal, internal_indices, internal_prefix)
    ]
    test_metadata_parts: List[pd.DataFrame] = [internal_source_metadata]
    if _has_array_triplet(result_dir, "ood"):
        ood_raw = load_triplet(result_dir, "ood")
        ood_source_metadata_raw = metadata_for_saved_prefix(
            split_root, "ood", len(ood_raw["true"])
        )
        ood, ood_indices = stable_subsample(ood_raw, stride)
        ood_source_metadata = ood_source_metadata_raw.iloc[ood_indices].reset_index(drop=True)
        ood_activity = active_mask(ood, scaler, ambient)
        ood, ood_indices = filter_arrays(ood, ood_indices, ood_activity)
        ood_source_metadata = ood_source_metadata.loc[ood_activity].reset_index(drop=True)
        if len(ood["true"]) > len(internal["true"]):
            rng = np.random.default_rng(seed + 41)
            chosen = np.sort(rng.choice(len(ood["true"]), len(internal["true"]), replace=False))
            ood = {name: np.asarray(values[chosen]) for name, values in ood.items()}
            ood_indices = ood_indices[chosen]
            ood_source_metadata = ood_source_metadata.iloc[chosen].reset_index(drop=True)
        test_parts.append((ood, ood_indices, "ood"))
        test_metadata_parts.append(ood_source_metadata)
    test, test_indices, test_origins = append_array_sets(test_parts)
    test_source_metadata = pd.concat(test_metadata_parts, ignore_index=True)

    train_params = physical_params(train, scaler)
    train_conditions = {tuple(np.round(row[:3], 4)) for row in train_params}
    derived_bounds_map = derived_process_bounds(train_params)
    train_origins = np.asarray(["train"] * len(train_indices), dtype=object)
    val_origins = np.asarray([val_prefix] * len(val_indices), dtype=object)

    train_split = make_paired_split(
        "train", train, train_indices, train_origins, train_source_metadata, scaler, bounds,
        derived_bounds_map, ambient, seed, train_conditions,
    )
    val_split = make_paired_split(
        "validation", validation, val_indices, val_origins, val_source_metadata, scaler, bounds,
        derived_bounds_map, ambient, seed, train_conditions,
    )
    test_split = make_paired_split(
        "test", test, test_indices, test_origins, test_source_metadata, scaler, bounds,
        derived_bounds_map, ambient, seed, train_conditions,
    )

    normal_mask = train_split.labels == 0
    coherence_floor = fit_coherence_scale_floor(
        train_split.actual_standardized[normal_mask],
        train_split.prediction_standardized[normal_mask],
    )
    for split in [train_split, val_split, test_split]:
        split.residual_features = build_residual_features(
            split.actual_standardized, split.prediction_standardized, coherence_floor
        )

    audit = {
        "data_origin": "original_I_ModernTCN_forecasts_plus_AFP_physics_counterfactual",
        "result_directory": str(result_dir),
        "train_csv_for_scaler": str(train_csv),
        "parameter_manifest": str(manifest_csv),
        "stride": int(stride),
        "sampling_hz": SAMPLING_HZ,
        "ambient_reference_C": ambient,
        "activity_rule": "temperature_peak >= ambient+10C OR pressure_peak >= 10N",
        "train_base_windows": int(len(train["true"])),
        "validation_base_windows": int(len(validation["true"])),
        "test_base_windows": int(len(test["true"])),
        "train_specimens": int(train_source_metadata["specimen_id"].nunique()),
        "validation_specimens": int(val_source_metadata["specimen_id"].nunique()),
        "test_specimens": int(test_source_metadata["specimen_id"].nunique()),
        "specimen_id_source": "source_block_id reconstructed in the exact I-ModernTCN window order",
        "train_samples_after_pairing": int(len(train_split.labels)),
        "validation_samples_after_pairing": int(len(val_split.labels)),
        "test_samples_after_pairing": int(len(test_split.labels)),
        "test_interpolation_samples": int(np.sum(test_split.metadata["dataset_split"] == "test_interpolation")),
        "test_extrapolation_samples": int(np.sum(test_split.metadata["dataset_split"] == "test_extrapolation")),
        "parameter_bounds": {k: list(v) for k, v in bounds.items()},
        "derived_training_bounds": {k: list(v) for k, v in derived_bounds_map.items()},
        "coherence_floor": coherence_floor.tolist(),
        "test_abnormal_response_observability_counts": {
            str(key): int(value) for key, value in test_split.metadata.loc[
                test_split.metadata["true_label"] == 1, "response_observability"
            ].value_counts().items()
        },
        "test_abnormal_type_counts": {
            str(key): int(value) for key, value in test_split.metadata.loc[
                test_split.metadata["true_label"] == 1, "anomaly_type"
            ].value_counts().items()
        },
    }
    return train_split, val_split, test_split, audit, scaler, {
        "bounds": bounds,
        "derived_bounds": derived_bounds_map,
        "ambient": ambient,
    }


def multicriterion_scores(
    x: np.ndarray, y: np.ndarray, groups: Sequence[str], seed: int
) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=int)
    median0 = np.median(x[y == 0], axis=0)
    median1 = np.median(x[y == 1], axis=0)
    pooled = np.sqrt(0.5 * (np.var(x[y == 0], axis=0) + np.var(x[y == 1], axis=0)))
    effect = np.abs(median1 - median0) / np.maximum(pooled, 1e-8)
    q0 = np.subtract(*np.percentile(x[y == 0], [75, 25], axis=0))
    qa = np.subtract(*np.percentile(x, [75, 25], axis=0))
    robustness = 1.0 / (1.0 + q0 / np.maximum(qa, 1e-8))
    folds = stable_group_folds(groups, 5, seed)
    global_sign = np.sign(median1 - median0)
    consistent = np.zeros(x.shape[1], dtype=float)
    for fold in range(5):
        mask = folds != fold
        delta = np.median(x[mask & (y == 1)], axis=0) - np.median(x[mask & (y == 0)], axis=0)
        consistent += (np.sign(delta) == global_sign).astype(float)
    stability = consistent / 5.0
    normalized_effect = effect / max(float(np.max(effect)), 1e-8)
    return normalized_effect * (0.5 + 0.5 * robustness) * (0.5 + 0.5 * stability)


def feature_sets(split: PreparedSplit, response_groups: Mapping[str, np.ndarray]) -> Dict[str, np.ndarray]:
    response = split.response_features
    process = split.process_features
    residual = split.residual_features
    return {
        "thermal_response": response[:, response_groups["thermal"]],
        "compaction_response": response[:, response_groups["compaction"]],
        "thermomechanical_response": response[:, response_groups["all"]],
        "residual": residual,
        "response_plus_residual": np.concatenate([response, residual], axis=1),
        "process_mechanism": process,
        "full_physics_guided": np.concatenate([process, response, residual], axis=1),
    }


def paired_sensor_identifiability(
    split: PreparedSplit, response_groups: Mapping[str, np.ndarray]
) -> dict:
    """Upper bound caused by normal/abnormal pairs with identical sensor features."""
    values = feature_sets(split, response_groups)["response_plus_residual"]
    normal_indices = np.arange(0, len(split.labels), 2, dtype=int)
    abnormal_indices = normal_indices + 1
    if not (
        np.all(split.labels[normal_indices] == 0)
        and np.all(split.labels[abnormal_indices] == 1)
    ):
        raise ValueError("Paired split order must be normal then abnormal")
    identical = np.all(
        np.isclose(
            values[normal_indices], values[abnormal_indices],
            rtol=0.0, atol=1e-12, equal_nan=True,
        ),
        axis=1,
    )
    anomaly_types = split.metadata.iloc[abnormal_indices]["anomaly_type"].astype(str).to_numpy()
    per_type = {
        kind: {
            "n_samples": int(np.sum(anomaly_types == kind)),
            "identical_sensor_pairs": int(np.sum(identical & (anomaly_types == kind))),
        }
        for kind in ANOMALY_TYPES
    }
    n_pairs = len(normal_indices)
    n_identical = int(np.sum(identical))
    return {
        "n_base_pairs": int(n_pairs),
        "identical_sensor_pairs": n_identical,
        "maximum_possible_balanced_accuracy_for_deterministic_sensor_classifier": float(
            1.0 - n_identical / max(2 * n_pairs, 1)
        ),
        "per_anomaly_type": per_type,
    }


def feature_names_by_key(response_names: Sequence[str], response_groups: Mapping[str, np.ndarray]) -> Dict[str, List[str]]:
    residual_names = residual_feature_names()
    process_names = process_feature_names()
    response_names = list(response_names)
    return {
        "thermal_response": [response_names[i] for i in response_groups["thermal"]],
        "compaction_response": [response_names[i] for i in response_groups["compaction"]],
        "thermomechanical_response": response_names,
        "residual": residual_names,
        "response_plus_residual": response_names + residual_names,
        "process_mechanism": process_names,
        "full_physics_guided": process_names + response_names + residual_names,
    }


def candidate_specs() -> List[dict]:
    specs = [
        {
            "name": "PCHI parameter-compliance index",
            "family": "process_compliance",
            "input_source": "p/v/pr boundary distance plus P/v and pr/v",
            "eligibility": "process_state_primary",
            "feature_key": "process_mechanism",
            "fixed_index": 6,
        }
    ]
    definitions = [
        ("T-HI", "thermal_response", "thermal_response", "sensor_response_primary"),
        ("C-HI", "compaction_response", "compaction_response", "sensor_response_primary"),
        ("TC-HI", "thermomechanical_response", "thermomechanical_response", "sensor_response_primary"),
        ("RFHI", "residual", "I-ModernTCN residual magnitude/coherence", "sensor_response_primary"),
        ("PR-HI", "response_plus_residual", "physics response plus forecast residual", "sensor_response_primary"),
        ("PM-HI", "process_mechanism", "process-mechanism features", "process_state_primary"),
        ("PG-RFHI", "full_physics_guided", "process mechanism plus response plus residual", "process_state_primary"),
    ]
    for label, key, source, eligibility in definitions:
        for l2 in [0.01, 0.1, 1.0]:
            top_k = 96 if key == "residual" else 128 if key in {"response_plus_residual", "full_physics_guided"} else None
            specs.append({
                "name": f"{label} (lambda={l2:g})",
                "family": label,
                "input_source": source,
                "eligibility": eligibility,
                "feature_key": key,
                "l2": l2,
                "top_k": top_k,
                "model_kind": "ridge",
            })
    for min_leaf in [1, 2, 4]:
        specs.append({
            "name": f"MPRF-HI (random forest, min_leaf={min_leaf})",
            "family": "MPRF-HI",
            "input_source": "AFP thermal/compaction response plus I-ModernTCN residual",
            "eligibility": "sensor_response_primary",
            "feature_key": "response_plus_residual",
            "model_kind": "random_forest",
            "min_samples_leaf": min_leaf,
        })
    return specs


def orient_scores(validation_labels: np.ndarray, val_score: np.ndarray, test_score: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float]:
    orientation = 1.0
    if np.median(val_score[validation_labels == 1]) < np.median(val_score[validation_labels == 0]):
        orientation = -1.0
    return orientation * val_score, orientation * test_score, orientation


def fit_candidates(
    train: PreparedSplit,
    validation: PreparedSplit,
    test: PreparedSplit,
    response_groups: Mapping[str, np.ndarray],
    seed: int,
) -> Tuple[List[CandidateResult], pd.DataFrame, CandidateResult, CandidateResult]:
    train_sets = feature_sets(train, response_groups)
    val_sets = feature_sets(validation, response_groups)
    test_sets = feature_sets(test, response_groups)
    rows: List[dict] = []
    results: List[CandidateResult] = []
    for spec in candidate_specs():
        key = spec["feature_key"]
        if "fixed_index" in spec:
            model = FixedScoreModel(spec["fixed_index"]).fit(train_sets[key], train.labels)
            selected = np.asarray([spec["fixed_index"]], dtype=int)
        elif spec.get("model_kind") == "random_forest":
            model = ForestHealthIndexModel(
                spec["min_samples_leaf"], seed,
                anomaly_type_weight_cap=spec.get("anomaly_type_weight_cap"),
            ).fit(
                train_sets[key], train.labels, train.groups,
                train.metadata["anomaly_type"].astype(str).to_numpy(),
            )
            selected = model.selected_indices
        else:
            model = SelectedRidgeModel(spec["l2"], spec["top_k"], seed).fit(
                train_sets[key], train.labels, train.groups
            )
            selected = model.selected_indices
        val_raw = model.predict_score(val_sets[key])
        test_raw = model.predict_score(test_sets[key])
        val_score, test_score, _ = orient_scores(validation.labels, val_raw, test_raw)
        threshold = choose_threshold(validation.labels, val_score)
        scale = calibration_scale(validation.labels, val_score)
        val_metrics = binary_metrics(validation.labels, val_score >= threshold)
        test_metrics = binary_metrics(test.labels, test_score >= threshold)
        result = CandidateResult(
            name=spec["name"], family=spec["family"],
            input_source=spec["input_source"], eligibility=spec["eligibility"],
            feature_key=key, model_kind=spec.get("model_kind", "fixed"),
            l2=spec.get("l2"), top_k=spec.get("top_k"),
            model=model, selected_indices=selected, threshold=threshold,
            calibration_scale=scale, validation_score=val_score, test_score=test_score,
            validation_metrics=val_metrics, test_metrics=test_metrics,
        )
        results.append(result)
        rows.append({
            "candidate": result.name,
            "family": result.family,
            "input_source": result.input_source,
            "eligibility": result.eligibility,
            "feature_key": result.feature_key,
            "model_kind": result.model_kind,
            "l2": result.l2,
            "top_k": result.top_k,
            "threshold_selected_on_validation": threshold,
            **{f"validation_{k}": v for k, v in val_metrics.items()},
            **{f"test_{k}": v for k, v in test_metrics.items()},
        })
    table = pd.DataFrame(rows)
    table["validation_rank_all"] = table["validation_balanced_accuracy"].rank(
        method="min", ascending=False
    ).astype(int)
    sensor_mask = table["eligibility"] == "sensor_response_primary"
    table["validation_rank_sensor_only"] = np.nan
    table.loc[sensor_mask, "validation_rank_sensor_only"] = table.loc[
        sensor_mask, "validation_balanced_accuracy"
    ].rank(method="min", ascending=False)

    process_eligible = table["eligibility"] == "process_state_primary"
    process_index = table.loc[process_eligible, "validation_balanced_accuracy"].idxmax()
    sensor_index = table.loc[sensor_mask, "validation_balanced_accuracy"].idxmax()
    selected_process = results[int(process_index)]
    selected_sensor = results[int(sensor_index)]
    table["selected_process_state_HI"] = table.index == process_index
    table["selected_sensor_response_HI"] = table.index == sensor_index
    table = table.sort_values(
        ["validation_balanced_accuracy", "test_balanced_accuracy"], ascending=False
    ).reset_index(drop=True)
    return results, table, selected_process, selected_sensor


def component_candidate(results: Sequence[CandidateResult], family: str) -> CandidateResult:
    candidates = [item for item in results if item.family == family]
    return max(candidates, key=lambda item: item.validation_metrics["balanced_accuracy"])


def result_table(
    split: PreparedSplit,
    selected: CandidateResult,
    components: Mapping[str, CandidateResult],
    use_validation_scores: bool,
) -> pd.DataFrame:
    score = selected.validation_score if use_validation_scores else selected.test_score
    prediction = (score >= selected.threshold).astype(int)
    table = split.metadata.copy()
    table["selected_method"] = selected.name
    table["raw_score"] = score
    table["raw_threshold"] = selected.threshold
    table["health_index"] = calibrate_health_index(
        score, selected.threshold, selected.calibration_scale
    )
    table["health_threshold"] = 0.5
    table["predicted_label"] = prediction
    table["真实状态"] = np.where(split.labels == 1, "异常", "正常")
    table["预测状态"] = np.where(prediction == 1, "异常", "正常")
    table["状态预警"] = np.where(prediction == 1, "报警", "不报警")
    table["预测正确与否"] = np.where(split.labels == prediction, "正确", "错误")
    for label, candidate in components.items():
        component_score = candidate.validation_score if use_validation_scores else candidate.test_score
        table[f"HI_{label}"] = calibrate_health_index(
            component_score, candidate.threshold, candidate.calibration_scale
        )
    table["line_heat_input_P_over_v"] = table["current_p"] / table["current_v"]
    table["compaction_exposure_pr_over_v"] = table["current_pr"] / table["current_v"]
    table["thermomechanical_input_P_pr_over_v"] = (
        table["current_p"] * table["current_pr"] / table["current_v"]
    )
    return table


def subgroup_metrics(table: pd.DataFrame) -> pd.DataFrame:
    rows: List[dict] = []
    for group_name, group in table.groupby("dataset_split", dropna=False):
        metrics = binary_metrics(
            group["true_label"].to_numpy(dtype=int),
            group["predicted_label"].to_numpy(dtype=int),
        )
        rows.append({"group_type": "test_split", "group": group_name, **metrics})
    abnormal = table[table["true_label"] == 1]
    for anomaly, group in abnormal.groupby("anomaly_type"):
        recall = float(np.mean(group["predicted_label"].to_numpy(dtype=int) == 1))
        rows.append({
            "group_type": "anomaly_type", "group": anomaly,
            "n_samples": int(len(group)), "recall": recall, "accuracy": recall,
        })
    if "response_observability" in abnormal.columns:
        for status, group in abnormal.groupby("response_observability"):
            recall = float(np.mean(group["predicted_label"].to_numpy(dtype=int) == 1))
            rows.append({
                "group_type": "response_observability", "group": status,
                "n_samples": int(len(group)), "recall": recall, "accuracy": recall,
            })
    return pd.DataFrame(rows)


def grouped_tenfold(
    selected: CandidateResult,
    train: PreparedSplit,
    validation: PreparedSplit,
    response_groups: Mapping[str, np.ndarray],
    seed: int,
) -> pd.DataFrame:
    train_x = feature_sets(train, response_groups)[selected.feature_key]
    val_x = feature_sets(validation, response_groups)[selected.feature_key]
    fold_ids = stable_group_folds(train.groups, 10, seed)
    rows = []
    for fold in range(10):
        fit_mask = fold_ids != fold
        hold_mask = fold_ids == fold
        model = refit_candidate_model(
            selected, train_x[fit_mask], train.labels[fit_mask],
            train.groups[fit_mask], seed + fold,
            train.metadata.loc[fit_mask, "anomaly_type"].astype(str).to_numpy(),
        )
        val_raw = model.predict_score(val_x)
        hold_raw = model.predict_score(train_x[hold_mask])
        val_score, hold_score, _ = orient_scores(validation.labels, val_raw, hold_raw)
        threshold = choose_threshold(validation.labels, val_score)
        metrics = binary_metrics(train.labels[hold_mask], hold_score >= threshold)
        rows.append({"fold": fold + 1, "threshold_from_fixed_validation": threshold, **metrics})
    return pd.DataFrame(rows)


def shuffled_test_tenfold(table: pd.DataFrame, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(table))
    folds = np.array_split(order, 10)
    rows = []
    for index, fold in enumerate(folds, start=1):
        part = table.iloc[fold]
        rows.append({
            "fold": index,
            **binary_metrics(
                part["true_label"].to_numpy(dtype=int),
                part["predicted_label"].to_numpy(dtype=int),
            ),
        })
    return pd.DataFrame(rows)


def selected_feature_table(
    selected: CandidateResult, names: Mapping[str, Sequence[str]]
) -> pd.DataFrame:
    feature_names = np.asarray(names[selected.feature_key], dtype=object)
    selected_indices = np.asarray(selected.selected_indices, dtype=int)
    if isinstance(selected.model, FixedScoreModel):
        weights = np.ones(len(selected_indices), dtype=float)
        scores = np.ones(len(selected_indices), dtype=float)
        contribution_type = "fixed_feature"
    elif isinstance(selected.model, ForestHealthIndexModel):
        weights = np.asarray(selected.model.model.feature_importances_, dtype=float)
        scores = weights.copy()
        contribution_type = "random_forest_impurity_importance"
    else:
        # RidgeScoreClassifier stores [feature weights..., intercept].
        weights = np.asarray(selected.model.ridge.weight[:-1], dtype=float)
        scores = np.asarray(selected.model.feature_scores[selected_indices], dtype=float)
        contribution_type = "standardized_ridge_weight"
    table = pd.DataFrame({
        "feature_index": selected_indices,
        "feature_name": feature_names[selected_indices],
        "selection_score": scores,
        "model_contribution": weights,
        "absolute_weight": np.abs(weights),
        "contribution_type": contribution_type,
    })
    return table.sort_values("absolute_weight", ascending=False).reset_index(drop=True)


def add_sensor_feature_domains(
    table: pd.DataFrame,
    selected: CandidateResult,
    response_groups: Mapping[str, np.ndarray],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Separate forecast-residual and AFP response contributions."""
    output = table.copy()
    thermal_end = len(response_groups["thermal"])
    compaction_end = thermal_end + len(response_groups["compaction"])
    response_end = len(response_groups["all"])

    def domain(index: int) -> str:
        index = int(index)
        if selected.feature_key == "residual":
            return "forecast_residual"
        if selected.feature_key == "thermal_response":
            return "thermal_response"
        if selected.feature_key == "compaction_response":
            return "compaction_response"
        if selected.feature_key in {"thermomechanical_response", "response_plus_residual"}:
            if selected.feature_key == "response_plus_residual" and index >= response_end:
                return "forecast_residual"
            if index < thermal_end:
                return "thermal_response"
            if index < compaction_end:
                return "compaction_response"
            return "thermomechanical_coupling"
        return "other"

    output["feature_domain"] = [domain(index) for index in output["feature_index"]]
    grouped = output.groupby("feature_domain", as_index=False)["absolute_weight"].sum()
    total = max(float(grouped["absolute_weight"].sum()), 1e-12)
    grouped["contribution_fraction"] = grouped["absolute_weight"] / total
    grouped = grouped.sort_values("contribution_fraction", ascending=False).reset_index(drop=True)
    return output, grouped


def literature_rows() -> List[dict]:
    reference_root = Path(r"F:\基于数字孪生的故障诊断\自动铺丝\状态预警")
    manuscript = Path(r"F:\基于数字孪生的故障诊断\自动铺丝\常用资料\An Interactive Spatiotemporal Graph Attention Network for Thermal-mechanical Forecasting in Automated Fiber Placement.docx")
    return [
        {
            "编号": "R1", "文献": "Zhao et al. (2024), Simulation and on-line monitoring using optical fiber Bragg grating sensors of temperature history during laser-assisted AFP",
            "期刊": "Journal of Composite Materials, 58(18), 2079-2092",
            "DOI": "10.1177/00219983241259849",
            "在线链接": "https://doi.org/10.1177/00219983241259849",
            "本研究引用内容": "各向异性三维瞬态热传导方程、对流/辊面/工装边界条件、速度影响温度峰值、AFP热压驻留时间很短",
            "对应健康指标": "T-HI thermal exposure/peak/spatial gradient/rate; active short-window rule",
            "公式或变量": "Kx*T_xx + Ky*T_yy + Kz*T_zz = rho*c*T_t; t_h=L_h/v",
            "本地文件": str(reference_root / "自动铺丝刀具温度监测.pdf"),
            "证据页": "3-8",
        },
        {
            "编号": "R2", "文献": "Song et al. (2022), Research on Void Dynamics during In Situ Consolidation of CF/High-Performance Thermoplastic Composite",
            "期刊": "Polymers, 14, 1401", "DOI": "10.3390/polym14071401",
            "在线链接": "https://doi.org/10.3390/polym14071401",
            "本研究引用内容": "辊-预浸料接触长度、压力积分、二维牛顿流体挤压流、速度和压力对孔隙率的影响",
            "对应健康指标": "C-HI pressure impulse/contact time; pr/v compaction exposure; TC coupling dose",
            "公式或变量": "Lc=sqrt(Rr^2-(Rr-hi+hf)^2); Fc=2*int int P(x,y)dxdy",
            "本地文件": str(reference_root / "3区空隙受工艺参数影响.pdf"),
            "证据页": "4-8",
        },
        {
            "编号": "R3", "文献": "Venkatesan et al. (2020), Effect of process parameters on polyamide-6 carbon fibre prepreg laminated by IR-assisted AFP",
            "期刊": "International Journal of Advanced Manufacturing Technology, 108, 1275-1284",
            "DOI": "10.1007/s00170-020-05230-z",
            "在线链接": "https://doi.org/10.1007/s00170-020-05230-z",
            "本研究引用内容": "IR功率与铺放速度的交互；高功率低速度导致过热，低功率高速度导致熔融/压实不足；压实力同样重要",
            "对应健康指标": "P/v line heat input; two-sided parameter risk; thermo-mechanical interaction",
            "公式或变量": "E_l=P/v; higher v requires coordinated higher P",
            "本地文件": str(reference_root / "工艺参数对红外辅助自动纤维铺放层压聚酰胺-6碳纤维预浸料的影响.pdf"),
            "证据页": "1, 4-9",
        },
        {
            "编号": "R4", "文献": "Godbold et al. (2026), Data-driven surface temperature prediction for variable tool geometries in AFP",
            "期刊": "Composites Part B, 309, 113047", "DOI": "10.1016/j.compositesb.2025.113047",
            "在线链接": "https://doi.org/10.1016/j.compositesb.2025.113047",
            "本研究引用内容": "功率、速度、p-angle、热源距离影响表面温差；二阶多项式回归和10折交叉验证",
            "对应健康指标": "thermal mechanism feature design; validation-only selection; grouped ten-fold evaluation",
            "公式或变量": "DeltaT=Tapplied-Tambient; second-degree polynomial response surface",
            "本地文件": str(reference_root / "一区partB-多项式回归-铺放速度角度表面距离-AFP温度差值预测.pdf"),
            "证据页": "5-9",
        },
        {
            "编号": "R5", "文献": "Francis et al. (2024), A digital environment for simulating the automated fiber placement manufacturing process",
            "期刊": "Manufacturing Letters, 40, 146-149", "DOI": "10.1016/j.mfglet.2024.03.008",
            "在线链接": "https://doi.org/10.1016/j.mfglet.2024.03.008",
            "本研究引用内容": "Hertz接触理论压力模型与二阶多项式热应用模型在统一AFP仿真环境中组合",
            "对应健康指标": "separate process/thermal/compaction components and physics-data fusion",
            "公式或变量": "Hertzian contact model + data-driven heating model",
            "本地文件": str(reference_root / "自动铺丝仿真软件.pdf"),
            "证据页": "1-3",
        },
        {
            "编号": "R6", "文献": "Li et al. (manuscript), An Interactive Spatiotemporal Graph Attention Network for Thermal-mechanical Forecasting in AFP",
            "期刊": "User manuscript", "DOI": "",
            "在线链接": "",
            "本研究引用内容": "10 Hz异构传感器、24点输入/预测、ATAVN、Interactive Attention、ModernTCN与GAT；热-机械预测残差",
            "对应健康指标": "RFHI residual branch and short heterogeneous multichannel windows",
            "公式或变量": "I-ModernTCN-GAT forecast residual e(t)=y(t)-yhat(t)",
            "本地文件": str(manuscript), "证据页": "manuscript sections 2-3",
        },
        {
            "编号": "R7", "文献": "Fontes & Shadmehri (2024), Data-driven thermal modeling of in-situ AFP",
            "期刊": "Composites Part A, 186, 108379", "DOI": "10.1016/j.compositesa.2024.108379",
            "在线链接": "https://doi.org/10.1016/j.compositesa.2024.108379",
            "本研究引用内容": "AFP热历史的数据驱动预测作为热响应建模依据",
            "对应健康指标": "forecast-versus-observed thermal response deviation",
            "公式或变量": "data-driven thermal response",
            "本地文件": "Referenced in user manuscript", "证据页": "reference [9]",
        },
        {
            "编号": "R8", "文献": "Fontes et al. (2025), Theory-guided machine learning for thermal modeling of in-situ AFP of thermoplastic composites",
            "期刊": "Composites Science and Technology, 260, 110987", "DOI": "10.1016/j.compscitech.2025.110987",
            "在线链接": "https://doi.org/10.1016/j.compscitech.2025.110987",
            "本研究引用内容": "理论引导变换与数据驱动热模型融合思路",
            "对应健康指标": "PG-RFHI physics-guided feature branch",
            "公式或变量": "theory-guided thermal gradients",
            "本地文件": "Referenced in user manuscript", "证据页": "reference [10]",
        },
    ]


def create_figures(
    output: Path,
    comparison: pd.DataFrame,
    test_table: pd.DataFrame,
    selected_process: CandidateResult,
    selected_sensor: CandidateResult,
    subgroup: pd.DataFrame,
    sensor_subgroup: pd.DataFrame,
    feature_table: pd.DataFrame,
    sensor_feature_table: pd.DataFrame,
    bounds: Mapping[str, Tuple[float, float]],
) -> List[str]:
    plt = configure_matplotlib()
    colors = {"blue": "#2F5597", "orange": "#D97A2B", "green": "#3A7D44", "gray": "#6B7280", "red": "#B33A3A"}
    saved: List[str] = []
    figure_dir = output / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)

    best_family = comparison.sort_values("validation_balanced_accuracy", ascending=False).drop_duplicates("family")
    best_family = best_family.sort_values("validation_balanced_accuracy")
    best_family = best_family.copy()
    best_family["family"] = best_family["family"].replace({"process_compliance": "PCHI"})
    fig, ax = plt.subplots(figsize=(8.2, 4.8))
    y = np.arange(len(best_family))
    ax.barh(y - 0.18, best_family["validation_balanced_accuracy"], 0.34, color=colors["blue"], label="Validation")
    ax.barh(y + 0.18, best_family["test_balanced_accuracy"], 0.34, color=colors["orange"], label="Locked test")
    ax.set_yticks(y, best_family["family"])
    ax.set_xlim(0.5, 1.01)
    ax.set_xlabel("Balanced accuracy")
    ax.set_title("AFP health-index candidate comparison")
    ax.legend(frameon=False, loc="lower right")
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.7)
    saved += save_figure(fig, figure_dir, "Fig1_HI_Candidate_Comparison")
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(9.2, 4.0), sharey=True)
    for ax, method, column, title in [
        (axes[0], selected_process, "health_index", "Selected process-state HI"),
        (axes[1], selected_sensor, "HI_sensor_response", "Selected sensor-response HI"),
    ]:
        for label, color, text_label in [(0, colors["blue"], "Normal"), (1, colors["orange"], "Abnormal")]:
            values = test_table.loc[test_table["true_label"] == label, column].to_numpy(dtype=float)
            ax.hist(values, bins=30, density=True, alpha=0.62, color=color, label=text_label)
        ax.axvline(0.5, color=colors["red"], linestyle="--", linewidth=1.1)
        ax.set_xlabel("Health index (abnormality probability scale)")
        ax.set_title(title)
        ax.grid(axis="y", color="#E5E7EB", linewidth=0.6)
    axes[0].set_ylabel("Density")
    axes[0].legend(frameon=False)
    saved += save_figure(fig, figure_dir, "Fig2_HI_Distributions")
    plt.close(fig)

    p_low, p_high = bounds["p"]
    v_low, v_high = bounds["v"]
    p_grid = np.linspace(p_low - 100, p_high + 100, 160)
    v_grid = np.linspace(max(1, v_low - 20), v_high + 20, 140)
    pp, vv = np.meshgrid(p_grid, v_grid)
    fig, ax = plt.subplots(figsize=(6.8, 5.0))
    contour = ax.contourf(pp, vv, pp / vv, levels=18, cmap="viridis")
    ax.plot([p_low, p_high, p_high, p_low, p_low], [v_low, v_low, v_high, v_high, v_low], color="white", linewidth=1.8, label="Accepted parameter envelope")
    abnormal = test_table[test_table["true_label"] == 1].sample(min(220, int((test_table["true_label"] == 1).sum())), random_state=7)
    ax.scatter(abnormal["current_p"], abnormal["current_v"], s=10, c=colors["red"], alpha=0.45, label="Injected out-of-envelope state")
    ax.set_xlabel("Laser power P (W)")
    ax.set_ylabel("Placement speed v (mm/s)")
    ax.set_title("Line heat input mechanism: $E_l=P/v$")
    fig.colorbar(contour, ax=ax, label="P/v (J/mm, proportional)")
    ax.legend(frameon=False, fontsize=8)
    saved += save_figure(fig, figure_dir, "Fig3_Line_Heat_Input_Map")
    plt.close(fig)

    pr_low, pr_high = bounds["pr"]
    pr_grid = np.linspace(max(1, pr_low - 220), pr_high + 220, 160)
    vv2, ff = np.meshgrid(v_grid, pr_grid)
    fig, ax = plt.subplots(figsize=(6.8, 5.0))
    contour = ax.contourf(vv2, ff, ff / vv2, levels=18, cmap="magma")
    ax.plot([v_low, v_high, v_high, v_low, v_low], [pr_low, pr_low, pr_high, pr_high, pr_low], color="white", linewidth=1.8, label="Accepted parameter envelope")
    ax.scatter(abnormal["current_v"], abnormal["current_pr"], s=10, c="#2DD4BF", alpha=0.5, label="Injected out-of-envelope state")
    ax.set_xlabel("Placement speed v (mm/s)")
    ax.set_ylabel("Compaction force pr (N)")
    ax.set_title(r"Compaction exposure proxy: $D_c\propto pr/v$")
    fig.colorbar(contour, ax=ax, label="pr/v (N s/mm, proportional)")
    ax.legend(frameon=False, fontsize=8)
    saved += save_figure(fig, figure_dir, "Fig4_Compaction_Exposure_Map")
    plt.close(fig)

    process_rows = subgroup[subgroup["group_type"] == "anomaly_type"][["group", "recall"]].rename(columns={"recall": "process_recall"})
    sensor_rows = sensor_subgroup[sensor_subgroup["group_type"] == "anomaly_type"][["group", "recall"]].rename(columns={"recall": "sensor_recall"})
    anomaly_rows = process_rows.merge(sensor_rows, on="group", how="outer").sort_values("sensor_recall")
    fig, ax = plt.subplots(figsize=(7.3, 4.4))
    y = np.arange(len(anomaly_rows))
    ax.barh(y - 0.18, anomaly_rows["process_recall"], 0.34, color=colors["green"], label="Process-state HI")
    ax.barh(y + 0.18, anomaly_rows["sensor_recall"], 0.34, color=colors["orange"], label="Sensor-response HI")
    ax.set_yticks(y, anomaly_rows["group"])
    ax.axvline(0.90, color=colors["red"], linestyle="--", linewidth=1.0, label="90% target")
    ax.set_xlim(0, 1.01)
    ax.set_xlabel("Recall")
    ax.set_title("Selected health indicators by anomaly mechanism")
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.6)
    ax.legend(
        frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.10),
        ncol=3,
    )
    fig.subplots_adjust(bottom=0.20)
    saved += save_figure(fig, figure_dir, "Fig5_Anomaly_Type_Recall")
    plt.close(fig)

    top = feature_table.head(15).sort_values("absolute_weight")
    fig, ax = plt.subplots(figsize=(8.4, 5.4))
    ax.barh(top["feature_name"], top["absolute_weight"], color=colors["blue"])
    ax.set_xlabel("Model contribution (absolute ridge weight or tree importance)")
    ax.set_title("Top contributions in selected process-state HI")
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.6)
    saved += save_figure(fig, figure_dir, "Fig6_Selected_HI_Feature_Contributions")
    plt.close(fig)

    top_sensor = sensor_feature_table.head(15).sort_values("absolute_weight")
    fig, ax = plt.subplots(figsize=(8.4, 5.4))
    ax.barh(top_sensor["feature_name"], top_sensor["absolute_weight"], color=colors["orange"])
    ax.set_xlabel("Random-forest impurity importance")
    ax.set_title("Top contributions in selected sensor-response HI")
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.6)
    saved += save_figure(fig, figure_dir, "Fig7_Selected_Sensor_HI_Feature_Contributions")
    plt.close(fig)

    domain = sensor_feature_table.groupby("feature_domain", as_index=False)["absolute_weight"].sum()
    domain["contribution_fraction"] = domain["absolute_weight"] / domain["absolute_weight"].sum()
    domain = domain.sort_values("contribution_fraction")
    fig, ax = plt.subplots(figsize=(6.8, 4.3))
    palette = [colors["gray"], colors["green"], colors["orange"], colors["blue"]]
    ax.barh(domain["feature_domain"], domain["contribution_fraction"], color=palette[:len(domain)])
    ax.set_xlabel("Fraction of total random-forest importance")
    ax.set_title("Data and AFP-mechanism contributions in MPRF-HI")
    ax.xaxis.set_major_formatter(lambda value, _: f"{100 * value:.0f}%")
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.6)
    saved += save_figure(fig, figure_dir, "Fig8_Data_Mechanism_Contribution_Composition")
    plt.close(fig)
    return saved


def create_compaction_event_figure(
    output: Path,
    window_table: pd.DataFrame,
    specimen_table: pd.DataFrame,
    force_threshold_n: float,
    min_event_windows: int,
) -> List[str]:
    """Create an SCI-style figure for sparse contact-event aggregation."""
    if window_table.empty or specimen_table.empty:
        return []
    plt = configure_matplotlib()
    colors = {
        "normal": "#2F5597", "low": "#3A7D44", "high": "#B33A3A",
        "gray": "#A7AFBA", "orange": "#D97A2B",
    }
    figure_dir = output / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.2))

    high = window_table[window_table["scenario"] == "compaction_high"]
    example_id = high.groupby("specimen_scenario_id")["n_windows"].size().idxmax() if "n_windows" in high.columns else high["specimen_scenario_id"].iloc[0]
    example = high[high["specimen_scenario_id"] == example_id].sort_values(
        ["source_index", "window_start_in_segment"], kind="mergesort"
    ).reset_index(drop=True)
    x = np.arange(len(example))
    event_mask = example["true_window_event"].to_numpy(dtype=int) == 1
    bar_colors = np.where(event_mask, colors["high"], colors["gray"])
    axes[0].bar(x, example["observed_pressure_peak_N"], color=bar_colors, width=0.82)
    axes[0].axhline(
        float(force_threshold_n), color="#111827", linestyle="--", linewidth=1.0,
        label=f"contact gate ({force_threshold_n:g} N)",
    )
    axes[0].set_xlabel("Window order within specimen")
    axes[0].set_ylabel("Pressure peak (N)")
    axes[0].set_title("(a) Sparse high-force events")
    axes[0].legend(frameon=False, fontsize=8)
    axes[0].grid(axis="y", color="#E5E7EB", linewidth=0.6)

    order = ["normal", "compaction_low", "compaction_high"]
    labels = ["Normal", "Low force", "High force"]
    palette = [colors["normal"], colors["low"], colors["high"]]
    rng = np.random.default_rng(20260714)
    for position, (scenario, color) in enumerate(zip(order, palette)):
        values = specimen_table.loc[
            specimen_table["true_anomaly_type"] == scenario,
            "predicted_event_window_count",
        ].to_numpy(dtype=float)
        jitter = rng.uniform(-0.08, 0.08, size=len(values))
        axes[1].scatter(
            np.repeat(position, len(values)) + jitter, values,
            s=24, alpha=0.72, color=color, edgecolors="white", linewidths=0.35,
        )
        if len(values):
            axes[1].plot(
                [position - 0.22, position + 0.22],
                [np.median(values), np.median(values)], color="#111827", linewidth=1.3,
            )
    axes[1].axhline(
        int(min_event_windows), color=colors["orange"], linestyle="--", linewidth=1.0,
        label=f"specimen alarm threshold = {min_event_windows} windows",
    )
    axes[1].set_xticks(range(3), labels)
    axes[1].set_ylabel("Detected abnormal contact windows")
    axes[1].set_title("(b) Event aggregation by specimen")
    axes[1].legend(frameon=False, fontsize=8)
    axes[1].grid(axis="y", color="#E5E7EB", linewidth=0.6)

    true = specimen_table["true_label"].to_numpy(dtype=int)
    pred = specimen_table["predicted_label"].to_numpy(dtype=int)
    matrix = np.asarray([
        [np.sum((true == 0) & (pred == 0)), np.sum((true == 0) & (pred == 1))],
        [np.sum((true == 1) & (pred == 0)), np.sum((true == 1) & (pred == 1))],
    ], dtype=int)
    axes[2].imshow(matrix, cmap="Blues", vmin=0, vmax=max(int(matrix.max()), 1))
    for row in range(2):
        for column in range(2):
            axes[2].text(
                column, row, str(matrix[row, column]), ha="center", va="center",
                color="white" if matrix[row, column] > matrix.max() / 2 else "#111827",
                fontsize=12,
            )
    axes[2].set_xticks([0, 1], ["Normal", "Abnormal"])
    axes[2].set_yticks([0, 1], ["Normal", "Abnormal"])
    axes[2].set_xlabel("Predicted specimen state")
    axes[2].set_ylabel("True specimen state")
    axes[2].set_title("(c) Locked-test confusion matrix")

    fig.suptitle("Contact-event compaction health indicator (CE-C-HI)", y=1.01, fontsize=12)
    fig.tight_layout()
    saved = save_figure(fig, figure_dir, "Fig9_Specimen_Compaction_Event_Aggregation")
    plt.close(fig)
    return saved


def write_method_report(
    path: Path,
    audit: Mapping[str, object],
    selected_process: CandidateResult,
    selected_sensor: CandidateResult,
    comparison: pd.DataFrame,
    cv: pd.DataFrame,
    sensor_cv: pd.DataFrame,
    sensor_subgroup: pd.DataFrame,
    sensor_domain: pd.DataFrame,
    seed_metrics: pd.DataFrame,
) -> None:
    best_rows = comparison.sort_values("validation_balanced_accuracy", ascending=False).drop_duplicates("family")
    lines = [
        "# AFP机理-数据融合健康指标构造与实验说明（v13）",
        "",
        "## 1. 结论",
        "",
        f"- 工艺状态主指标：**{selected_process.name}**；验证集平衡准确率 {selected_process.validation_metrics['balanced_accuracy']:.4f}，锁定测试集 {selected_process.test_metrics['balanced_accuracy']:.4f}。",
        f"- 不直接使用工艺参数越界信息的传感响应主指标：**{selected_sensor.name}**；验证集平衡准确率 {selected_sensor.validation_metrics['balanced_accuracy']:.4f}，锁定测试集 {selected_sensor.test_metrics['balanced_accuracy']:.4f}。",
        f"- 测试集中有 {audit['test_sensor_identifiability']['identical_sensor_pairs']} 对正常/异常样本的传感特征完全相同，主要来自无辊接触的压实力异常；因此任何确定性纯传感分类器的样本级平衡准确率理论上限约为 {audit['test_sensor_identifiability']['maximum_possible_balanced_accuracy_for_deterministic_sensor_classifier']:.4f}。",
        "- 两项结果必须分开解释：工艺状态本身由 p/v/pr 是否越界定义，所以含参数合规项的指标高准确率是定义一致性；传感响应指标才检验热-机械响应及预测残差能否识别异常后果。",
        "",
        "## 2. 数据与划分",
        "",
        f"- 使用原I-ModernTCN保存的预测、真实值和输入数组；只保留工艺参数在24+24点内稳定的窗口，并按每 {audit['stride']} 个窗口抽样。",
        f"- 采样率10 Hz；活动窗口规则为最高温度不低于环境基准+10 C，或压力峰值不低于10 N。训练/验证/测试基础窗口分别为 {audit['train_base_windows']}/{audit['validation_base_windows']}/{audit['test_base_windows']}。",
        "- 每个基础窗口生成配对正常与异常样本；同一基础窗口的两类样本始终在同一数据集和同一交叉验证折，避免配对泄漏。",
        "- 验证集约为训练基础窗口的四分之一；模型和阈值均只用训练/验证集，测试集不参与选择。",
        "",
        "## 3. 状态与异常响应",
        "",
        "异常状态由单个工艺参数越过原实验工艺包络定义。异常传感响应不是固定平移，而由下列AFP关系生成：",
        "",
        r"$$E_l=P/v,\qquad t_h=L_h/v,\qquad D_c\propto pr/v$$",
        "",
        "温度相对环境的升温幅值按线热输入比 $(P_1/v_1)/(P_0/v_0)$ 调整；转速响应按 $v_1/v_0$ 调整；压力响应按 $pr_1/pr_0$ 调整。预测值保持为异常发生前由I-ModernTCN给出的名义预测，因此其残差表示异常工艺参数诱发的偏离。",
        "六种异常按基础窗口确定性均衡分配，各类型数量最多相差1，并保持每个基础窗口一个正常样本和一个异常样本。不再把不可观测的压实力异常重分配为功率异常：无压辊接触的窗口仍保留压实力越界标签，同时标记为 parameter_only_no_roller_contact；它可由工艺参数健康指标识别，但不应假定压力传感响应一定可识别。",
        "",
        "## 4. 构造的健康指标",
        "",
        "### 4.1 PCHI：工艺参数合规健康指标",
        "",
        r"对 $p,v,pr$ 分别计算上下界归一化越界距离，并计算 $P/v$、$pr/v$ 和 $P\,pr/v$。PCHI采用最大参数越界距离。它回答工艺参数是否处于规定范围，不等价于缺陷预测。",
        "",
        "### 4.2 T-HI：热状态指标",
        "",
        "由8个温度通道构造温度暴露积分、峰值、均值、空间温差P95、时序粗糙度、最大升/降温率、有效受热时间、各热电偶暴露量和预测RMSE。所有量使用真实响应与I-ModernTCN预测响应之差。",
        "",
        r"$$I_T=\Delta t\sum_t\frac{1}{8}\sum_{i=1}^{8}\max(T_i(t)-T_{amb},0)$$",
        "",
        "### 4.3 C-HI：压实状态指标",
        "",
        "由压力冲量、峰值、接触时间、压力波动、转速、位移和振动响应构造：",
        "",
        r"$$I_F=\Delta t\sum_t\max(F(t),0),\qquad \tau_c=\Delta t\sum_t\mathbf{1}[F(t)>10\,N]$$",
        "",
        "### 4.4 TC-HI：热-压实耦合指标",
        "",
        "使用热压耦合剂量及全部热/压实响应特征：",
        "",
        r"$$D_{TF}=\Delta t\sum_t \overline{(T(t)-T_{amb})_+}\,F_+(t)$$",
        "",
        "### 4.5 RFHI与PG-RFHI",
        "",
        "RFHI使用I-ModernTCN残差幅值、RMSE、斜率和残差符号一致性。PG-RFHI将参数机理、热-压实响应和RFHI连接，经多准则特征选择和岭模型融合为标量健康指标。阈值在验证集上确定，并通过Logistic映射使健康指标阈值固定为0.5。",
        "",
        "### 4.6 MPRF-HI：机理-预测随机森林健康指标",
        "",
        "MPRF-HI不读取p/v/pr越界距离，将42项热-压实机理响应特征与I-ModernTCN残差特征输入随机森林。标量指标定义为所有树给出异常类别概率的平均：",
        "",
        "$$HI_{MPRF}(x)=\\frac{1}{B}\\sum_{b=1}^{B}p_b(y=1\\mid x)$$",
        "",
        "树数固定为500；候选叶节点最小样本数为1/2/4，仍仅按验证集平衡准确率选取。由于异常类型已经在注入阶段均衡，不再进行异常机理类别重加权。最终原始概率以验证阈值校准为0.5预警界限。",
        "",
        "## 5. 多指标实验",
        "",
        "| 指标族 | 最佳候选 | 验证平衡准确率 | 测试平衡准确率 | 输入性质 |",
        "|---|---|---:|---:|---|",
    ]
    for _, row in best_rows.iterrows():
        lines.append(
            f"| {row['family']} | {row['candidate']} | {row['validation_balanced_accuracy']:.4f} | {row['test_balanced_accuracy']:.4f} | {row['input_source']} |"
        )
    lines.extend([
        "",
        "### 5.1 传感响应指标的分异常机理结果",
        "",
        "| 异常类型 | 样本数 | 召回率 |",
        "|---|---:|---:|",
    ])
    for _, row in sensor_subgroup.loc[
        sensor_subgroup["group_type"] == "anomaly_type"
    ].iterrows():
        lines.append(f"| {row['group']} | {int(row['n_samples'])} | {row['recall']:.4f} |")
    lines.extend([
        "",
        "### 5.2 传感响应可观测性",
        "",
        "| 可观测状态 | 样本数 | 召回率 |",
        "|---|---:|---:|",
    ])
    for _, row in sensor_subgroup.loc[
        sensor_subgroup["group_type"] == "response_observability"
    ].iterrows():
        lines.append(f"| {row['group']} | {int(row['n_samples'])} | {row['recall']:.4f} |")
    lines.extend([
        "",
        "无辊接触或无热活动时，工艺参数仍然异常，但相应传感后果在该短窗口中不可观测；因此传感响应HI低于参数合规HI是合理结果。",
        "",
        "### 5.3 MPRF-HI的数据与AFP机理贡献",
        "",
        "| 特征域 | 随机森林重要性占比 |",
        "|---|---:|",
    ])
    for _, row in sensor_domain.iterrows():
        lines.append(f"| {row['feature_domain']} | {row['contribution_fraction']:.4f} |")
    lines.append(
        "当前模型的重要性主要来自I-ModernTCN预测残差；热、压实和热压耦合特征提供AFP机理约束与可解释补充。该比例是模型重要性，不是因果贡献。"
    )
    lines.extend([
        "",
        f"选定工艺状态指标的分组10折准确率为 {cv['accuracy'].mean():.4f} +/- {cv['accuracy'].std(ddof=0):.4f}。",
        f"选定传感响应指标的分组10折准确率为 {sensor_cv['accuracy'].mean():.4f} +/- {sensor_cv['accuracy'].std(ddof=0):.4f}。",
        f"传感响应指标在不同异常分配种子下的测试准确率为 {seed_metrics['test_accuracy'].mean():.4f} +/- {seed_metrics['test_accuracy'].std(ddof=0):.4f}。" if len(seed_metrics) else "未执行多种子重复。",
        "",
        "## 6. 文献与方法对应",
        "",
        "完整表格见 `文献与健康指标对应表_v13.xlsx` 和输出目录中的 `literature_reference_table.csv`。主要依据包括瞬态热传导、P/v热输入、辊压接触/孔隙动力学、AFP温度响应面和I-ModernTCN热-机械预测。",
        "",
        "## 7. 科学边界",
        "",
        "1. 当前异常响应仍是基于正常实测窗口的机理约束反事实生成，不是真实异常试件；准确率只能用于方法开发。",
        "2. PCHI/PM-HI/PG-RFHI读取当前工艺参数，因此高准确率表示参数合规识别，不表示成型缺陷识别。",
        "3. T-HI/C-HI/TC-HI/RFHI/PR-HI/MPRF-HI不直接使用参数越界距离，其结果更接近真实传感状态监测能力。",
        "4. 当前数据缺少铺放头p-angle、热源-表面距离、辊半径、实际接触宽度、层厚与CF/PEEK黏度-温度曲线，因此没有伪造完整PDE或孔隙率数值解；收集这些量后才可加入严格物理残差。",
        "5. 当前残差健康指标需要观察到24点预测区间后才能确认状态，属于短时预测后确认，而非预测区间开始前的提前报警。",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def append_compaction_event_method_section(
    path: Path,
    validation_metrics: Mapping[str, object],
    test_metrics: Mapping[str, object],
) -> None:
    marker = "## 8. CE-C-HI试样级压实力接触事件判据"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if marker in existing:
        existing = existing.split(marker, 1)[0].rstrip()
    lines = [
        marker,
        "",
        "### 8.1 为什么改为试样级事件聚合",
        "",
        "铺放过程短且非连续，不能把长时间无接触解释为压实力故障。压实力只在压辊实际接触的短窗口内有物理意义，因此无接触窗口不参与压实力判别；少量可靠的异常接触事件即可把整件试样判为异常。",
        "",
        "### 8.2 接触门控、窗口指标与试样指标",
        "",
        "1. 接触门控：$g_w=1[\\max\\,run(1[F_t>10\\,N])\\ge2]$。即24点窗口内压力超过10 N且至少连续2点（10 Hz下为0.2 s），避免单点噪声触发。",
        "2. 压实力越界：$z_L=\\max(0,(pr_L-\\delta-pr)/(pr_H-pr_L))$，$z_H=\\max(0,(pr-pr_H-\\delta)/(pr_H-pr_L))$，固定工程容差$\\delta=0.5\\,N$，用于消除float32反标准化边界误差。",
        "3. 窗口CE-C-HI：$e_w=g_w\\max(z_L,z_H)$。无接触时强制为0。",
        "4. 试样CE-C-HI：$HI_s=\\min(1,\\sum_w1[e_w>0]/2)$；当至少2个接触窗口异常时，$HI_s=1$并报警。",
        "",
        "### 8.3 本次反事实注入",
        "",
        "试样编号直接采用原分割文件中的`source_block_id`，同一试样的多个`segment_id`合并。对每个至少含2个有效接触窗口的试样，按原始压力峰值排序，只在最高的2个接触窗口分别构造低压实力和高压实力场景，其余全部窗口保持原始正常数据。每个试样生成正常、压实力偏低、压实力偏高三种场景，因此高/低异常试样数严格相同。",
        "",
        "这里的接触门控在合成开发实验中取自注入前原始实测窗口，以防低压实力注入把接触证据本身削弱；真实部署应优先使用压辊接触开关/压辊位姿，再用压力连续点作交叉确认。",
        "",
        "### 8.4 结果",
        "",
        "| 数据集 | 纳入试样 | 正常场景 | 低压异常场景 | 高压异常场景 | 准确率 | 平衡准确率 | 类型准确率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| 验证集 | {int(validation_metrics.get('specimens_included', 0))} | "
            f"{int(validation_metrics.get('normal_scenario_count', 0))} | "
            f"{int(validation_metrics.get('compaction_low_scenario_count', 0))} | "
            f"{int(validation_metrics.get('compaction_high_scenario_count', 0))} | "
            f"{float(validation_metrics.get('primary_accuracy', float('nan'))):.4f} | "
            f"{float(validation_metrics.get('primary_balanced_accuracy', float('nan'))):.4f} | "
            f"{float(validation_metrics.get('type_accuracy', float('nan'))):.4f} |"
        ),
        (
            f"| 锁定测试集 | {int(test_metrics.get('specimens_included', 0))} | "
            f"{int(test_metrics.get('normal_scenario_count', 0))} | "
            f"{int(test_metrics.get('compaction_low_scenario_count', 0))} | "
            f"{int(test_metrics.get('compaction_high_scenario_count', 0))} | "
            f"{float(test_metrics.get('primary_accuracy', float('nan'))):.4f} | "
            f"{float(test_metrics.get('primary_balanced_accuracy', float('nan'))):.4f} | "
            f"{float(test_metrics.get('type_accuracy', float('nan'))):.4f} |"
        ),
        "",
        "该准确率验证的是“接触门控后的压实力工艺参数越界规则”，不是缺陷检测准确率。真实异常试样必须重新标定$pr_L,pr_H$、接触门控和2窗口阈值。",
    ]
    path.write_text(existing + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")


def repeat_selected_seed(
    result_dir: Path,
    train_csv: Path,
    manifest_csv: Path,
    stride: int,
    seeds: Iterable[int],
    selected: CandidateResult,
    response_groups: Mapping[str, np.ndarray],
) -> pd.DataFrame:
    rows = []
    for seed in seeds:
        train, val, test, _, _, _ = prepare_benchmark(
            result_dir, train_csv, manifest_csv, stride, int(seed)
        )
        sets_train = feature_sets(train, response_groups)
        sets_val = feature_sets(val, response_groups)
        sets_test = feature_sets(test, response_groups)
        model = refit_candidate_model(
            selected, sets_train[selected.feature_key], train.labels,
            train.groups, int(seed),
            train.metadata["anomaly_type"].astype(str).to_numpy(),
        )
        val_raw = model.predict_score(sets_val[selected.feature_key])
        test_raw = model.predict_score(sets_test[selected.feature_key])
        val_score, test_score, _ = orient_scores(val.labels, val_raw, test_raw)
        threshold = choose_threshold(val.labels, val_score)
        val_metrics = binary_metrics(val.labels, val_score >= threshold)
        test_metrics = binary_metrics(test.labels, test_score >= threshold)
        rows.append({
            "seed": int(seed), "validation_accuracy": val_metrics["accuracy"],
            "validation_balanced_accuracy": val_metrics["balanced_accuracy"],
            "test_accuracy": test_metrics["accuracy"],
            "test_balanced_accuracy": test_metrics["balanced_accuracy"],
        })
    return pd.DataFrame(rows)


def locate_default_result(project_root: Path) -> Path:
    result_root = project_root / "results"
    candidates = [
        path for path in result_root.glob("health_i_T_G_MyCustom_ftM_sl24_ll24_pl24_*")
        if _has_array_triplet(path, "train") and _has_array_triplet(path, "val")
    ]
    if not candidates:
        raise FileNotFoundError(f"No compatible I-ModernTCN results under {result_root}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def run(args: argparse.Namespace) -> Path:
    project_root = Path(args.project_root).resolve()
    result_dir = Path(args.result_dir).resolve() if args.result_dir else locate_default_result(project_root)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    train_csv = Path(args.train_csv).resolve()
    manifest_csv = Path(args.manifest_csv).resolve()

    train, validation, test, audit, scaler, physics = prepare_benchmark(
        result_dir, train_csv, manifest_csv, args.stride, args.seed
    )
    response_names, response_groups = response_feature_names()
    identifiability = paired_sensor_identifiability(test, response_groups)
    audit["test_sensor_identifiability"] = identifiability
    names = feature_names_by_key(response_names, response_groups)
    results, comparison, selected_process, selected_sensor = fit_candidates(
        train, validation, test, response_groups, args.seed
    )

    components = {
        "process": component_candidate(results, "PM-HI"),
        "thermal": component_candidate(results, "T-HI"),
        "compaction": component_candidate(results, "C-HI"),
        "thermomechanical": component_candidate(results, "TC-HI"),
        "residual": component_candidate(results, "RFHI"),
        "sensor_response": selected_sensor,
    }
    validation_table = result_table(
        validation, selected_process, components, use_validation_scores=True
    )
    test_table = result_table(test, selected_process, components, use_validation_scores=False)
    validation_sensor_table = result_table(
        validation, selected_sensor, components, use_validation_scores=True
    )
    test_sensor_table = result_table(
        test, selected_sensor, components, use_validation_scores=False
    )
    subgroup = subgroup_metrics(test_table)
    sensor_subgroup = subgroup_metrics(test_sensor_table)
    cv = grouped_tenfold(selected_process, train, validation, response_groups, args.seed)
    sensor_cv = grouped_tenfold(selected_sensor, train, validation, response_groups, args.seed)
    shuffled = shuffled_test_tenfold(test_table, args.seed)
    feature_table = selected_feature_table(selected_process, names)
    sensor_feature_table = selected_feature_table(selected_sensor, names)
    sensor_feature_table, sensor_domain = add_sensor_feature_domains(
        sensor_feature_table, selected_sensor, response_groups
    )

    seed_values = [args.seed + 1009 * index for index in range(max(1, args.seed_repeats))]
    seed_metrics = repeat_selected_seed(
        result_dir, train_csv, manifest_csv, args.stride, seed_values,
        selected_sensor, response_groups,
    ) if args.seed_repeats > 0 else pd.DataFrame()

    contact_force_threshold_n = float(getattr(
        args, "contact_force_threshold_n", CONTACT_FORCE_THRESHOLD_N
    ))
    contact_min_consecutive_points = int(getattr(
        args, "contact_min_consecutive_points", CONTACT_MIN_CONSECUTIVE_POINTS
    ))
    specimen_min_event_windows = int(getattr(
        args, "specimen_min_event_windows", SPECIMEN_MIN_COMPACTION_EVENT_WINDOWS
    ))
    (
        validation_event_windows, validation_specimen_events,
        validation_event_eligibility, validation_event_metrics,
    ) = build_sparse_compaction_event_benchmark(
        validation, physics["bounds"], physics["ambient"], args.seed,
        specimen_min_event_windows, contact_force_threshold_n,
        contact_min_consecutive_points,
    )
    (
        test_event_windows, test_specimen_events,
        test_event_eligibility, test_event_metrics,
    ) = build_sparse_compaction_event_benchmark(
        test, physics["bounds"], physics["ambient"], args.seed,
        specimen_min_event_windows, contact_force_threshold_n,
        contact_min_consecutive_points,
    )
    specimen_event_metrics = pd.DataFrame([
        validation_event_metrics, test_event_metrics,
    ])
    specimen_event_cv = specimen_event_grouped_tenfold(
        test_specimen_events, args.seed
    )
    audit["specimen_compaction_event_monitor"] = {
        "decision_rule": (
            "only contact-event windows are evaluated; a specimen is abnormal "
            f"when at least {specimen_min_event_windows} windows have out-of-envelope compaction force"
        ),
        "contact_gate": (
            f"pressure > {contact_force_threshold_n:g} N for at least "
            f"{contact_min_consecutive_points} consecutive samples"
        ),
        "compaction_parameter_tolerance_N": COMPACTION_PARAMETER_TOLERANCE_N,
        "injection_rule": (
            f"inject exactly {specimen_min_event_windows} highest-pressure eligible windows "
            "per low/high counterfactual specimen; all other windows remain original normal"
        ),
        "specimen_grouping": "source_origin + source_block_id; multiple segment_id values are merged",
        "validation": json_records(pd.DataFrame([validation_event_metrics]))[0],
        "locked_test": json_records(pd.DataFrame([test_event_metrics]))[0],
    }

    try:
        from .run_hierarchical_specimen_health_indicator_v13_3 import (
            append_hierarchical_method_section,
            run_hierarchical_specimen_benchmark,
        )
    except ImportError:  # Direct script execution.
        from run_hierarchical_specimen_health_indicator_v13_3 import (
            append_hierarchical_method_section,
            run_hierarchical_specimen_benchmark,
        )
    layer_result = run_hierarchical_specimen_benchmark(
        result_dir=result_dir,
        split_root=train_csv.parent,
        scaler=scaler,
        bounds=physics["bounds"],
        ambient=physics["ambient"],
        output=output,
        seed=args.seed,
        stride=args.stride,
        make_plots=not args.no_plots,
    )
    audit["hierarchical_specimen_benchmark"] = layer_result.summary

    comparison.to_csv(output / "candidate_health_indicator_comparison.csv", index=False, encoding=OUTPUT_ENCODING)
    validation_table.to_csv(output / "validation_health_index_results.csv", index=False, encoding=OUTPUT_ENCODING)
    test_table.to_csv(output / "test_health_index_results.csv", index=False, encoding=OUTPUT_ENCODING)
    validation_sensor_table.to_csv(output / "validation_sensor_response_HI_results.csv", index=False, encoding=OUTPUT_ENCODING)
    test_sensor_table.to_csv(output / "test_sensor_response_HI_results.csv", index=False, encoding=OUTPUT_ENCODING)
    test_table.sample(frac=1.0, random_state=args.seed).to_csv(
        output / "test_health_index_results_shuffled.csv", index=False, encoding=OUTPUT_ENCODING
    )
    subgroup.to_csv(output / "test_subgroup_metrics.csv", index=False, encoding=OUTPUT_ENCODING)
    sensor_subgroup.to_csv(output / "test_sensor_response_HI_subgroup_metrics.csv", index=False, encoding=OUTPUT_ENCODING)
    cv.to_csv(output / "selected_HI_grouped_10fold_metrics.csv", index=False, encoding=OUTPUT_ENCODING)
    sensor_cv.to_csv(output / "selected_sensor_HI_grouped_10fold_metrics.csv", index=False, encoding=OUTPUT_ENCODING)
    shuffled.to_csv(output / "shuffled_test_10fold_metrics.csv", index=False, encoding=OUTPUT_ENCODING)
    seed_metrics.to_csv(output / "selected_sensor_HI_multiseed_metrics.csv", index=False, encoding=OUTPUT_ENCODING)
    feature_table.to_csv(output / "selected_process_HI_feature_contributions.csv", index=False, encoding=OUTPUT_ENCODING)
    sensor_feature_table.to_csv(output / "selected_sensor_HI_feature_contributions.csv", index=False, encoding=OUTPUT_ENCODING)
    sensor_domain.to_csv(output / "selected_sensor_HI_domain_contributions.csv", index=False, encoding=OUTPUT_ENCODING)
    validation_event_windows.to_csv(
        output / "validation_compaction_event_window_results.csv", index=False,
        encoding=OUTPUT_ENCODING,
    )
    validation_specimen_events.to_csv(
        output / "validation_specimen_compaction_state_results.csv", index=False,
        encoding=OUTPUT_ENCODING,
    )
    test_event_windows.to_csv(
        output / "test_compaction_event_window_results.csv", index=False,
        encoding=OUTPUT_ENCODING,
    )
    test_specimen_events.to_csv(
        output / "test_specimen_compaction_state_results.csv", index=False,
        encoding=OUTPUT_ENCODING,
    )
    pd.concat(
        [validation_event_eligibility, test_event_eligibility], ignore_index=True
    ).to_csv(
        output / "specimen_compaction_event_eligibility.csv", index=False,
        encoding=OUTPUT_ENCODING,
    )
    specimen_event_metrics.to_csv(
        output / "specimen_compaction_event_metrics.csv", index=False,
        encoding=OUTPUT_ENCODING,
    )
    specimen_event_cv.to_csv(
        output / "specimen_compaction_grouped_10fold_metrics.csv", index=False,
        encoding=OUTPUT_ENCODING,
    )
    literature = pd.DataFrame(literature_rows())
    literature.to_csv(output / "literature_reference_table.csv", index=False, encoding=OUTPUT_ENCODING)

    figures = list(layer_result.figures)
    if not args.no_plots:
        figures += create_figures(
            output, comparison, test_table, selected_process, selected_sensor,
            subgroup, sensor_subgroup, feature_table, sensor_feature_table,
            physics["bounds"],
        )
        figures += create_compaction_event_figure(
            output, test_event_windows, test_specimen_events,
            contact_force_threshold_n, specimen_min_event_windows,
        )

    roc_process = roc_pr(test.labels, selected_process.test_score)
    roc_sensor = roc_pr(test.labels, selected_sensor.test_score)
    summary = {
        "selected_process_state_HI": selected_process.name,
        "selected_sensor_response_HI": selected_sensor.name,
        "selection_rule": "highest validation balanced accuracy within each eligibility class; process-state ties prefer the deterministic PCHI listed first; locked test not used",
        "process_validation_metrics": selected_process.validation_metrics,
        "process_test_metrics": selected_process.test_metrics,
        "sensor_validation_metrics": selected_sensor.validation_metrics,
        "sensor_test_metrics": selected_sensor.test_metrics,
        "process_test_roc_auc": roc_process["roc_auc"],
        "process_test_pr_auc": roc_process["pr_auc"],
        "sensor_test_roc_auc": roc_sensor["roc_auc"],
        "sensor_test_pr_auc": roc_sensor["pr_auc"],
        "sensor_identical_test_pairs": identifiability["identical_sensor_pairs"],
        "sensor_identifiability_upper_bound": identifiability[
            "maximum_possible_balanced_accuracy_for_deterministic_sensor_classifier"
        ],
        "grouped_10fold_accuracy_mean": float(cv["accuracy"].mean()),
        "grouped_10fold_accuracy_std": float(cv["accuracy"].std(ddof=0)),
        "sensor_grouped_10fold_accuracy_mean": float(sensor_cv["accuracy"].mean()),
        "sensor_grouped_10fold_accuracy_std": float(sensor_cv["accuracy"].std(ddof=0)),
        "sensor_multiseed_test_accuracy_mean": float(seed_metrics["test_accuracy"].mean()) if len(seed_metrics) else None,
        "sensor_multiseed_test_accuracy_std": float(seed_metrics["test_accuracy"].std(ddof=0)) if len(seed_metrics) else None,
        "specimen_compaction_event_HI": "CE-C-HI",
        "specimen_compaction_validation_metrics": json_records(
            pd.DataFrame([validation_event_metrics])
        )[0],
        "specimen_compaction_test_metrics": json_records(
            pd.DataFrame([test_event_metrics])
        )[0],
        "specimen_compaction_grouped_10fold_accuracy_mean": (
            float(specimen_event_cv["accuracy"].mean()) if len(specimen_event_cv) else None
        ),
        "specimen_compaction_grouped_10fold_accuracy_std": (
            float(specimen_event_cv["accuracy"].std(ddof=0)) if len(specimen_event_cv) else None
        ),
        "layer_sample_definition": layer_result.summary["sample_definition"],
        "layer_full_specimens": layer_result.summary["full_specimens"],
        "layer_samples": layer_result.summary["layer_samples"],
        "layer_train_samples": layer_result.summary["train_layer_samples"],
        "layer_validation_samples": layer_result.summary["validation_layer_samples"],
        "layer_test_samples": layer_result.summary["test_layer_samples"],
        "layer_state_counts_full_specimen": layer_result.summary["state_counts_full_specimen"],
        "layer_state_counts_layer_sample": layer_result.summary["state_counts_layer_sample"],
        "layer_selected_sensor_only_HI": layer_result.selected_sensor_candidate,
        "layer_selected_sensor_only_uses_process_parameter_combination": False,
        "layer_selected_sensor_validation_balanced_accuracy": layer_result.summary[
            "selected_sensor_validation_binary_balanced_accuracy"
        ],
        "layer_selected_sensor_test_accuracy": float(
            layer_result.indicator_metrics.loc[
                (layer_result.indicator_metrics["dataset"] == "test_all") &
                (~layer_result.indicator_metrics["uses_process_parameter_combination"]),
                "accuracy",
            ].iloc[0]
        ),
        "layer_selected_sensor_test_balanced_accuracy": layer_result.summary[
            "selected_sensor_test_binary_balanced_accuracy"
        ],
        "layer_selected_sensor_test_seven_state_accuracy": layer_result.summary[
            "selected_sensor_test_seven_state_accuracy"
        ],
        "layer_selected_sensor_grouped_10fold_accuracy_mean": layer_result.summary[
            "selected_sensor_grouped_10fold_accuracy_mean"
        ],
        "layer_selected_sensor_grouped_10fold_accuracy_std": layer_result.summary[
            "selected_sensor_grouped_10fold_accuracy_std"
        ],
        "layer_process_rule_test_seven_state_accuracy": layer_result.summary[
            "process_rule_test_seven_state_accuracy"
        ],
        "hierarchical_selected_HI": layer_result.summary[
            "selected_hierarchical_indicator"
        ],
        "hierarchical_selected_aggregation": layer_result.summary[
            "selected_aggregation_label"
        ],
        "hierarchical_selected_cap_rho": layer_result.summary[
            "selected_cap_rho"
        ],
        "hierarchical_test_window_balanced_accuracy": layer_result.summary[
            "test_window_balanced_accuracy"
        ],
        "hierarchical_test_layer_balanced_accuracy": layer_result.summary[
            "test_layer_balanced_accuracy"
        ],
        "hierarchical_test_layer_state_accuracy": layer_result.summary[
            "test_layer_state_accuracy"
        ],
        "hierarchical_test_specimen_balanced_accuracy": layer_result.summary[
            "test_specimen_balanced_accuracy"
        ],
        "hierarchical_test_specimen_state_accuracy": layer_result.summary[
            "test_specimen_state_accuracy"
        ],
        "hierarchical_grouped_10fold_specimen_accuracy_mean": layer_result.summary[
            "grouped_10fold_specimen_accuracy_mean"
        ],
        "hierarchical_stratified_5fold_specimen_accuracy_mean": layer_result.summary[
            "stratified_5fold_specimen_accuracy_mean"
        ],
    }
    (output / "selected_method_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    indicator_construction = pd.DataFrame([
        {"健康指标": "PCHI", "类别": "参数合规", "构造公式": "max normalized boundary violation of p, v, pr", "输入": "current p, v, pr", "AFP机理": "工艺包络", "直接读取参数越界": "是", "解释": "参数合规检查，不等同缺陷预测"},
        {"健康指标": "T-HI", "类别": "传感响应", "构造公式": "I_T=dt*sum(mean_i(max(T_i-Tamb,0))) plus spatial/rate/residual features", "输入": "8-channel temperature and I-ModernTCN forecast", "AFP机理": "瞬态热传导、P/v线热输入、Lh/v驻留时间", "直接读取参数越界": "否", "解释": "过热或热输入不足导致的热响应偏离"},
        {"健康指标": "C-HI", "类别": "传感响应", "构造公式": "I_F=dt*sum(max(F,0)); tau_c=dt*sum(1[F>10N])", "输入": "pressure, rotation, displacement, vibration and forecast", "AFP机理": "辊压接触、pr/v压实暴露", "直接读取参数越界": "否", "解释": "压力冲量、接触和机械稳定性偏离"},
        {"健康指标": "TC-HI", "类别": "传感响应", "构造公式": "D_TF=dt*sum(mean_i((T_i-Tamb)+)*F+)", "输入": "thermal and compaction response", "AFP机理": "短时热-压实协同", "直接读取参数越界": "否", "解释": "熔融温度与辊压载荷的同步暴露"},
        {"健康指标": "RFHI", "类别": "传感响应", "构造公式": "multi-channel magnitude/RMSE/slope/sign-coherence of y-yhat", "输入": "I-ModernTCN prediction residual", "AFP机理": "异构短序列预测一致性", "直接读取参数越界": "否", "解释": "相对正常预测轨迹的综合偏离"},
        {"健康指标": "PR-HI", "类别": "传感响应融合", "构造公式": "HI_m=P_m(abnormal|x_response+residual), m in {LR,RBF-SVM,RF,ET}", "输入": "42 physics-response features plus I-ModernTCN residual", "AFP机理": "热、压实与预测残差融合", "直接读取参数越界": "否", "解释": "在6×4全因子实验中由四种分类器分别输出异常概率"},
        {"健康指标": "MPRF-HI", "类别": "传感响应融合（旧名称）", "构造公式": "HI_m=P_m(abnormal|x_response+residual), m in {LR,RBF-SVM,RF,ET}", "输入": "same response_plus_residual vector as PR-HI", "AFP机理": "P/v热响应、pr/v接触响应、热压剂量和预测一致性", "直接读取参数越界": "否", "解释": "保留旧名称用于版本对照；当前与PR-HI共享特征，同一模型结果应一致"},
        {"健康指标": "PM-HI", "类别": "参数机理", "构造公式": "violations plus P/v, pr/v and P*pr/v", "输入": "current p, v, pr", "AFP机理": "线热输入、压实暴露与热压耦合输入", "直接读取参数越界": "是", "解释": "工艺状态分类"},
        {"健康指标": "PG-RFHI", "类别": "机理-数据融合", "构造公式": "selected ridge fusion of PM-HI, response features and RFHI", "输入": "parameters, sensors and prediction residual", "AFP机理": "参数-热-压实-预测全链路", "直接读取参数越界": "是", "解释": "工艺状态主指标候选"},
    ])
    indicator_construction.loc[len(indicator_construction)] = [
        "CE-C-HI", "specimen-level contact event",
        (
            f"delta={COMPACTION_PARAMETER_TOLERANCE_N:g}N; "
            f"g_w=1[max consecutive(F>{contact_force_threshold_n:g}N)>="
            f"{contact_min_consecutive_points}]; "
            "e_w=g_w*max(z_pr_low(delta),z_pr_high(delta)); "
            f"HI_spec=min(1,sum(1[e_w>0])/{specimen_min_event_windows})"
        ),
        "contact pressure sequence and current compaction-force parameter pr",
        "roller-contact event gating and sparse high-load windows",
        "yes",
        "Two abnormal contact windows trigger the whole-specimen compaction alarm; no-contact windows are excluded.",
    ]
    indicator_construction.loc[len(indicator_construction)] = [
        "CAP-MIL", "hierarchical evidence aggregation",
        "a_i=exp(alpha*p_i)/sum_j exp(alpha*p_j); HI=sum_i a_i*p_i; alpha=rho*ln(M-1), 0<=rho<=1",
        "calibrated window anomaly probabilities and five layer health indices",
        "mechanism-gated local AFP evidence; one window/layer cannot exceed 50% theoretical weight",
        "no",
        "The validation-selected rho pools binary window-to-layer and five-layer-to-specimen evidence; the second-stage anomaly type retains mechanism-gated sparse tail attribution. Mean, max, fixed tail and original aggregation are retained as ablations.",
    ]
    workbook_payload = {
        "summary": summary,
        "audit": audit,
        "indicator_construction": json_records(indicator_construction),
        "candidate_results": json_records(comparison),
        "literature": json_records(literature),
        "process_grouped_cv": json_records(cv),
        "sensor_grouped_cv": json_records(sensor_cv),
        "sensor_subgroup_metrics": json_records(sensor_subgroup),
        "sensor_domain_contributions": json_records(sensor_domain),
        "specimen_compaction_metrics": json_records(specimen_event_metrics),
        "specimen_compaction_grouped_cv": json_records(specimen_event_cv),
        "specimen_compaction_results": json_records(test_specimen_events),
        "specimen_compaction_eligibility": json_records(pd.concat(
            [validation_event_eligibility, test_event_eligibility], ignore_index=True
        )),
        "layer_summary": layer_result.summary,
        "layer_specimen_assignment": json_records(
            layer_result.ledger[
                [
                    "full_specimen_id", "p", "v", "pr", "specimen_label",
                    "dataset_split", "process_condition_in_training",
                    "health_state", "binary_health_label",
                ]
            ].drop_duplicates("full_specimen_id")
        ),
        "layer_split_summary": json_records(layer_result.split_summary),
        "layer_state_balance": json_records(layer_result.state_balance),
        "layer_indicator_metrics": json_records(layer_result.indicator_metrics),
        "layer_sensor_only_candidates": json_records(layer_result.candidate_metrics),
        "layer_results": json_records(layer_result.long_results),
        "layer_grouped_cv": json_records(layer_result.grouped_cv),
        "hierarchical_summary": layer_result.summary,
        "hierarchical_level_metrics": json_records(layer_result.level_metrics),
        "hierarchical_candidates": json_records(layer_result.candidate_metrics),
        "hierarchical_pooling_comparison": json_records(
            layer_result.pooling_comparison
        ),
        "literature_indicator_audit": json_records(
            layer_result.literature_indicator_audit
        ),
        "hierarchical_window_results": json_records(
            layer_result.window_results[
                layer_result.window_results["dataset_split"].str.startswith("test_")
            ]
        ),
        "hierarchical_layer_results": json_records(layer_result.layer_results),
        "hierarchical_specimen_results": json_records(layer_result.specimen_results),
        "hierarchical_grouped_10fold": json_records(layer_result.grouped_cv),
        "hierarchical_stratified_5fold": json_records(
            layer_result.stratified_fivefold_cv
        ),
    }
    (output / "workbook_payload_v13.json").write_text(
        json.dumps(workbook_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    write_method_report(
        WORKSPACE / "AFP机理数据融合健康指标_构造与实验说明_v13.md",
        audit, selected_process, selected_sensor, comparison, cv, sensor_cv,
        sensor_subgroup, sensor_domain, seed_metrics,
    )
    append_compaction_event_method_section(
        WORKSPACE / "AFP机理数据融合健康指标_构造与实验说明_v13.md",
        validation_event_metrics, test_event_metrics,
    )
    append_hierarchical_method_section(
        WORKSPACE / "AFP机理数据融合健康指标_构造与实验说明_v13.md",
        layer_result,
    )
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "audit": audit,
        "selected_methods": summary,
        "figures": figures,
        "literature_rows": literature_rows(),
        "scientific_boundary": {
            "current_data": "normal measured I-ModernTCN windows plus physics-constrained counterfactual abnormal response",
            "process_state_accuracy": "parameter-envelope compliance, not defect prediction",
            "sensor_response_accuracy": "forecast residual/thermo-mechanical response, excludes direct boundary-distance features",
            "real_validation_required": True,
            "timing": "state confirmation after the 24-point forecast horizon is observed",
            "compaction_event_rule": (
                "no-contact windows are excluded; two abnormal contact windows trigger "
                "the specimen-level compaction alarm"
            ),
            "compaction_counterfactual_density": (
                "exactly two highest-pressure contact windows per abnormal specimen scenario"
            ),
            "layer_sample_definition": (
                "one physical specimen contributes exactly five layer samples; "
                "all five layers share one health state"
            ),
            "layer_state_balance": (
                "eight normal specimens and three specimens for each of six anomaly types; "
                "each anomaly type therefore has exactly fifteen layer samples"
            ),
            "layer_missing_records": (
                "five absent original layer records are imputed from the same-condition "
                "other specimen, explicitly marked, and restricted to training"
            ),
            "hierarchical_decision": (
                "specimen-consistent injection, mechanism-gated observable-window evidence, "
                "validation-selected constrained AutoPool at layer and specimen levels, and "
                "a logically consistent two-stage state decision"
            ),
        },
    }
    (output / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"v13 selected process-state HI: {selected_process.name}")
    print(f"  validation balanced accuracy: {selected_process.validation_metrics['balanced_accuracy']:.6f}")
    print(f"  locked test balanced accuracy: {selected_process.test_metrics['balanced_accuracy']:.6f}")
    print(f"v13 selected sensor-response HI: {selected_sensor.name}")
    print(f"  validation balanced accuracy: {selected_sensor.validation_metrics['balanced_accuracy']:.6f}")
    print(f"  locked test balanced accuracy: {selected_sensor.test_metrics['balanced_accuracy']:.6f}")
    print("v13.1 specimen compaction event HI: CE-C-HI")
    print(f"  validation specimen accuracy: {validation_event_metrics.get('primary_accuracy', float('nan')):.6f}")
    print(f"  locked test specimen accuracy: {test_event_metrics.get('primary_accuracy', float('nan')):.6f}")
    print(
        "v13.6 hierarchical benchmark: "
        f"{len(layer_result.candidate_metrics)} candidates, window -> layer -> specimen"
    )
    print(f"  selected sensor-only HI: {layer_result.selected_sensor_candidate}")
    print(
        "  selected hierarchical aggregation: "
        f"{layer_result.summary['selected_aggregation_label']}"
    )
    print(
        "  locked test binary accuracy: "
        f"{summary['layer_selected_sensor_test_accuracy']:.6f}"
    )
    print(
        "  locked test seven-state accuracy: "
        f"{summary['layer_selected_sensor_test_seven_state_accuracy']:.6f}"
    )
    print(
        "  locked test specimen balanced accuracy: "
        f"{layer_result.summary['test_specimen_balanced_accuracy']:.6f}"
    )
    print(
        "  locked test specimen state accuracy: "
        f"{layer_result.summary['test_specimen_state_accuracy']:.6f}"
    )
    print(f"Outputs: {output}")
    return output


def main() -> int:
    project_root = WORKSPACE.parent.parent
    parser = argparse.ArgumentParser(
        description="AFP physics-guided multiple-health-index experiment v13"
    )
    parser.add_argument("--project-root", type=Path, default=project_root)
    parser.add_argument("--result-dir", type=Path, default=None)
    parser.add_argument(
        "--train-csv", type=Path,
        default=WORKSPACE / "health_split_v3_accuracy" / "train_normal.csv",
    )
    parser.add_argument(
        "--manifest-csv", type=Path,
        default=WORKSPACE / "health_split_v3_accuracy" / "split_manifest.csv",
    )
    parser.add_argument(
        "--output", type=Path,
        default=SCRIPT_DIR / "outputs_physics_guided_hi_v13",
    )
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--seed-repeats", type=int, default=5)
    parser.add_argument("--stride", type=int, default=24)
    parser.add_argument(
        "--contact-force-threshold-n", type=float,
        default=CONTACT_FORCE_THRESHOLD_N,
    )
    parser.add_argument(
        "--contact-min-consecutive-points", type=int,
        default=CONTACT_MIN_CONSECUTIVE_POINTS,
    )
    parser.add_argument(
        "--specimen-min-event-windows", type=int,
        default=SPECIMEN_MIN_COMPACTION_EVENT_WINDOWS,
    )
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
