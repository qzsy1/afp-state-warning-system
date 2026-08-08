"""Runtime-only feature primitives for the AFP online health indicators."""
from __future__ import annotations

import numpy as np

from runtime_scaler import FeatureScaler


SAMPLING_HZ = 10.0
DT_SECONDS = 1.0 / SAMPLING_HZ
TEMP = np.arange(2, 10, dtype=int)
ROTATION, DISPLACEMENT, PRESSURE, VIBRATION = 0, 1, 10, 11


def build_residual_features(actual: np.ndarray, prediction: np.ndarray, coherence_floor: np.ndarray) -> np.ndarray:
    residual = np.asarray(actual, dtype=float) - np.asarray(prediction, dtype=float)
    signed_mean = np.mean(residual, axis=1)
    mean_abs = np.mean(np.abs(residual), axis=1)
    rmse = np.sqrt(np.mean(np.square(residual), axis=1))
    max_abs = np.max(np.abs(residual), axis=1)
    std = np.std(residual, axis=1)
    slope = residual[:, -1, :] - residual[:, 0, :]
    summary = np.concatenate([
        signed_mean, mean_abs, rmse, max_abs, std, slope,
        np.column_stack([np.mean(mean_abs, axis=1), np.max(mean_abs, axis=1), np.mean(rmse, axis=1), np.max(max_abs, axis=1)]),
    ], axis=1)
    floor = np.asarray(coherence_floor, dtype=float).reshape(1, -1)
    roughness = np.mean(np.abs(np.diff(residual, axis=1)), axis=1)
    mean_to_std = np.abs(signed_mean) / (std + floor)
    mean_to_abs = np.abs(signed_mean) / (mean_abs + floor)
    mean_to_roughness = np.abs(signed_mean) / (roughness + floor)
    sign_coherence = np.abs(np.mean(np.sign(residual), axis=1))
    coherence = np.concatenate([
        mean_to_std, mean_to_abs, mean_to_roughness, sign_coherence,
        np.column_stack([np.mean(mean_to_std, axis=1), np.max(mean_to_std, axis=1), np.mean(mean_to_roughness, axis=1), np.max(sign_coherence, axis=1)]),
    ], axis=1)
    values = np.concatenate([summary, coherence], axis=1)
    if values.shape[1] != 128 or not np.isfinite(values).all():
        raise ValueError(f"Invalid residual feature matrix: {values.shape}")
    return values


def response_feature_names() -> tuple[list[str], dict[str, np.ndarray]]:
    names = ["thermal_exposure_residual", "thermal_peak_residual", "thermal_mean_residual", "thermal_spatial_p95_residual", "thermal_spatial_mean_residual", "thermal_roughness_residual", "heating_rate_max_residual", "cooling_rate_max_residual", "effective_heating_time_residual"]
    names += [f"temperature_exposure_residual_tc{i}" for i in range(1, 9)]
    names += [f"temperature_rmse_tc{i}" for i in range(1, 9)]
    names += ["pressure_impulse_residual", "pressure_peak_residual", "pressure_mean_residual", "pressure_contact_time_residual", "pressure_cv_residual", "pressure_roughness_residual", "rotation_mean_residual", "rotation_rmse", "rotation_cv_residual", "displacement_mean_residual", "displacement_std_residual", "displacement_jump_residual", "vibration_rms_residual", "vibration_p95_residual", "thermomechanical_dose_residual", "thermomechanical_peak_residual", "thermomechanical_normalized_residual"]
    thermal_end = 25
    coupling_start = len(names) - 3
    return names, {"thermal": np.arange(thermal_end), "compaction": np.arange(thermal_end, coupling_start), "coupling": np.arange(coupling_start, len(names)), "all": np.arange(len(names))}


def _window_physics(values: np.ndarray, ambient: float) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    temp, pressure = values[:, :, TEMP], np.maximum(values[:, :, PRESSURE], 0.0)
    rotation, displacement, vibration = values[:, :, ROTATION], values[:, :, DISPLACEMENT], values[:, :, VIBRATION]
    excess = np.maximum(temp - ambient, 0.0)
    mean_temp, mean_excess = np.mean(temp, axis=2), np.mean(excess, axis=2)
    thermal_exposure = DT_SECONDS * np.sum(mean_excess, axis=1)
    spatial = np.max(temp, axis=2) - np.min(temp, axis=2)
    rates = np.diff(mean_temp, axis=1) * SAMPLING_HZ
    tc_exposure = DT_SECONDS * np.sum(excess, axis=1)
    pressure_impulse = DT_SECONDS * np.sum(pressure, axis=1)
    pressure_mean = np.mean(pressure, axis=1)
    dose = DT_SECONDS * np.sum(mean_excess * pressure, axis=1)
    return np.column_stack([
        thermal_exposure, np.max(temp, axis=(1, 2)), np.mean(temp, axis=(1, 2)), np.percentile(spatial, 95, axis=1), np.mean(spatial, axis=1), np.mean(np.abs(np.diff(mean_temp, axis=1)), axis=1) * SAMPLING_HZ, np.max(rates, axis=1), np.max(-rates, axis=1), thermal_exposure / np.maximum(np.max(mean_excess, axis=1), 1e-8), tc_exposure,
        pressure_impulse, np.max(pressure, axis=1), pressure_mean, DT_SECONDS * np.sum(pressure > 10.0, axis=1), np.std(pressure, axis=1) / (pressure_mean + 1e-8), np.mean(np.abs(np.diff(pressure, axis=1)), axis=1) * SAMPLING_HZ,
        np.mean(rotation, axis=1), np.std(rotation, axis=1) / (np.abs(np.mean(rotation, axis=1)) + 1e-8), np.mean(displacement, axis=1), np.std(displacement, axis=1), np.max(np.abs(np.diff(displacement, axis=1)), axis=1), np.sqrt(np.mean(vibration**2, axis=1)), np.percentile(np.abs(vibration), 95, axis=1), dose, np.max(mean_excess * pressure, axis=1), dose / np.maximum(thermal_exposure * pressure_impulse, 1e-8),
    ])


def response_features(actual: np.ndarray, prediction: np.ndarray, ambient: float) -> np.ndarray:
    difference = _window_physics(actual, ambient) - _window_physics(prediction, ambient)
    tc_rmse = np.sqrt(np.mean(np.square(actual[:, :, TEMP] - prediction[:, :, TEMP]), axis=1))
    rotation_rmse = np.sqrt(np.mean(np.square(actual[:, :, ROTATION] - prediction[:, :, ROTATION]), axis=1))
    return np.column_stack([difference[:, :9], difference[:, 9:17], tc_rmse, difference[:, 17:23], difference[:, 23], rotation_rmse, difference[:, 24], difference[:, 25:28], difference[:, 28:30], difference[:, 30:33]])
