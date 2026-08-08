from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np


APP_DIR = Path(__file__).resolve().parent
DEFAULT_ARTIFACT = (
    APP_DIR
    / "new_collection_demo_v11_3"
    / "models"
    / "new_collection_hi_artifacts.joblib"
)

SENSOR_COLUMNS = [
    "温度", "压力", "ROI平均温度", "张力", "线速度",
    "ABB_X", "ABB_Y", "ABB_Z",
    *[f"温度{index}" for index in range(1, 9)],
    "转速", "位移", "振动",
]
PROCESS_COLUMNS = [
    "initial_compaction_force_N",
    "placement_speed_mm_s",
    "pid_angle_deg",
    "temperature_setpoint_C",
]
THERMAL = [
    "温度", "ROI平均温度", *[f"温度{index}" for index in range(1, 9)]
]
COMPACTION = ["压力", "张力"]
MOTION = ["线速度", "ABB_X", "ABB_Y", "ABB_Z", "转速", "位移"]
VIBRATION = ["振动"]

NEW_STATE_LABELS = {
    "normal": "正常",
    "underheat": "温度设定过低",
    "high_speed_underheat": "高速－温度设定过低",
    "underheat_low_pressure": "低温－低压实力",
    "overheat": "温度设定过高",
    "severe_overheat": "温度设定严重过高",
    "overheat_high_pressure": "高温－高压实力",
    "low_speed_overheat": "低速－温度设定过高",
    "low_pressure": "初始压实力过低",
    "high_speed_low_pressure": "高速－低压实力",
    "high_pressure": "初始压实力过高",
    "severe_high_pressure": "初始压实力严重过高",
    "angle_low": "铺放角度过低",
    "angle_high": "铺放角度过高",
    "angle_low_underheat": "低角度－温度设定过低",
    "angle_high_overheat": "高角度－温度设定过高",
}
NEW_ABNORMAL_STATES = [
    state for state in NEW_STATE_LABELS if state != "normal"
]

MASTER_FEATURE_NAMES = [
    "thermal_residual_rms",
    "thermal_residual_p95",
    "thermal_bias",
    "thermal_uniformity_error",
    "thermal_ramp_error",
    "pressure_residual_rms",
    "pressure_residual_peak",
    "pressure_impulse_error",
    "contact_fraction_error",
    "tension_residual_rms",
    "motion_residual_rms",
    "speed_tracking_error",
    "trajectory_residual_rms",
    "vibration_residual_rms",
    "global_residual_p95",
    "cross_channel_residual_coherence",
    "thermo_compaction_coupling_error",
    "process_manifold_distance",
    "response_thermal_distance",
    "response_compaction_distance",
    "response_motion_distance",
    "pca_spe",
    "keca_distance",
    "mcfs_spatial_error",
    "temporal_autoencoding_error",
    "wasserstein_distance",
    "robust_mahalanobis_distance",
]

INDICATOR_FEATURES = {
    "T-HI": [
        "thermal_residual_rms", "thermal_residual_p95", "thermal_bias",
        "thermal_uniformity_error", "thermal_ramp_error",
        "response_thermal_distance",
    ],
    "C-HI": [
        "pressure_residual_rms", "pressure_residual_peak",
        "pressure_impulse_error", "contact_fraction_error",
        "tension_residual_rms", "response_compaction_distance",
    ],
    "TC-HI": [
        "thermal_residual_rms", "thermal_residual_p95", "thermal_bias",
        "thermal_uniformity_error", "pressure_residual_rms",
        "pressure_residual_peak", "pressure_impulse_error",
        "contact_fraction_error", "tension_residual_rms",
        "thermo_compaction_coupling_error", "process_manifold_distance",
    ],
    "RFHI": [
        "thermal_residual_rms", "pressure_residual_rms",
        "tension_residual_rms", "motion_residual_rms",
        "trajectory_residual_rms", "vibration_residual_rms",
        "global_residual_p95", "cross_channel_residual_coherence",
    ],
    "PR-HI": [
        "process_manifold_distance", "response_thermal_distance",
        "response_compaction_distance", "response_motion_distance",
        "speed_tracking_error", "thermo_compaction_coupling_error",
    ],
    "MPRF-HI": MASTER_FEATURE_NAMES[:21],
    "PCA-SPE-HI": ["pca_spe", "global_residual_p95"],
    "KECA-SPE-HI": [
        "keca_distance", "process_manifold_distance",
        "global_residual_p95",
    ],
    "McFS-AVAE-HI": [
        "mcfs_spatial_error", "temporal_autoencoding_error",
        "response_thermal_distance", "response_compaction_distance",
        "response_motion_distance",
    ],
    "CNN-LSTM-AE-HI": [
        "temporal_autoencoding_error", "thermal_ramp_error",
        "motion_residual_rms", "vibration_residual_rms",
        "global_residual_p95",
    ],
    "W-HI": [
        "wasserstein_distance", "process_manifold_distance",
        "global_residual_p95",
    ],
    "RMD-HI": [
        "robust_mahalanobis_distance", "process_manifold_distance",
        "global_residual_p95",
    ],
}

INDICATOR_LABELS = {
    "T-HI": "热响应健康指标（10路温度）",
    "C-HI": "压实接触健康指标（压力＋张力）",
    "TC-HI": "热－压实耦合健康指标",
    "RFHI": "19通道预测残差融合指标",
    "PR-HI": "工艺参数－传感器响应指标",
    "MPRF-HI": "多物理工艺－响应－残差融合指标",
    "PCA-SPE-HI": "正常子空间PCA-SPE指标",
    "KECA-SPE-HI": "核熵成分距离指标",
    "McFS-AVAE-HI": "多通道空间－时序重构指标",
    "CNN-LSTM-AE-HI": "时序自编码重构对比指标",
    "W-HI": "健康分布Wasserstein距离指标",
    "RMD-HI": "稳健马氏距离指标",
}

INDICATOR_REQUIRED_OUTPUTS = {
    "T-HI": THERMAL,
    "C-HI": COMPACTION,
    "TC-HI": [*THERMAL, *COMPACTION],
    "RFHI": SENSOR_COLUMNS,
    "PR-HI": SENSOR_COLUMNS,
    "MPRF-HI": SENSOR_COLUMNS,
    "PCA-SPE-HI": SENSOR_COLUMNS,
    "KECA-SPE-HI": SENSOR_COLUMNS,
    "McFS-AVAE-HI": SENSOR_COLUMNS,
    "CNN-LSTM-AE-HI": SENSOR_COLUMNS,
    "W-HI": SENSOR_COLUMNS,
    "RMD-HI": SENSOR_COLUMNS,
}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _integral(values: np.ndarray) -> float:
    trapezoid = getattr(np, "trapezoid", np.trapz)
    return float(trapezoid(values))


def _indices(names: list[str]) -> list[int]:
    return [SENSOR_COLUMNS.index(name) for name in names]


def summarize_window(values: np.ndarray) -> np.ndarray:
    """Five interpretable statistics per sensor: mean/std/range/slope/energy."""
    values = np.asarray(values, dtype=float)
    if values.shape != (24, len(SENSOR_COLUMNS)):
        raise ValueError(
            f"新数据集健康指标要求窗口形状为(24,{len(SENSOR_COLUMNS)})"
        )
    time = np.arange(24, dtype=float)
    centered_time = time - time.mean()
    slope = centered_time @ values / np.sum(centered_time**2)
    return np.concatenate(
        [
            np.mean(values, axis=0),
            np.std(values, axis=0),
            np.ptp(values, axis=0),
            slope,
            np.sqrt(np.mean(np.square(values), axis=0)),
        ]
    )


def build_calibration(
    healthy_actual: np.ndarray,
    healthy_prediction: np.ndarray,
    healthy_process_points: np.ndarray,
) -> dict[str, np.ndarray]:
    """Fit normal-only calibration without using validation or test labels."""
    actual = np.asarray(healthy_actual, dtype=float)
    prediction = np.asarray(healthy_prediction, dtype=float)
    residual = actual - prediction
    residual_center = np.median(residual, axis=(0, 1))
    residual_scale = 1.4826 * np.median(
        np.abs(residual - residual_center[None, None, :]), axis=(0, 1)
    )
    residual_scale = np.maximum(residual_scale, 1e-4)

    summaries = np.stack([summarize_window(window) for window in actual])
    summary_center = np.median(summaries, axis=0)
    summary_scale = 1.4826 * np.median(
        np.abs(summaries - summary_center[None, :]), axis=0
    )
    summary_scale = np.maximum(summary_scale, 1e-4)
    standardized = (summaries - summary_center[None, :]) / summary_scale[None, :]

    _, singular, right = np.linalg.svd(standardized, full_matrices=False)
    variance = np.square(singular)
    cumulative = np.cumsum(variance) / max(float(np.sum(variance)), 1e-12)
    retained = int(np.searchsorted(cumulative, 0.95) + 1)
    retained = int(np.clip(retained, 1, max(1, len(right) - 1)))
    pca_components = right[:retained]

    covariance = np.cov(standardized, rowvar=False)
    shrinkage = 0.10
    diagonal = np.diag(np.diag(covariance))
    robust_precision = np.linalg.pinv(
        (1.0 - shrinkage) * covariance + shrinkage * diagonal
        + np.eye(covariance.shape[0]) * 1e-5
    )
    prototype_count = min(64, len(standardized))
    prototype_indices = np.linspace(
        0, len(standardized) - 1, prototype_count, dtype=int
    )
    quantile_levels = np.linspace(0.1, 0.9, 9)
    healthy_quantiles = np.quantile(actual, quantile_levels, axis=(0, 1)).T
    process_points = np.asarray(healthy_process_points, dtype=float)
    process_scale = 1.4826 * np.median(
        np.abs(process_points - np.median(process_points, axis=0)), axis=0
    )
    process_scale = np.maximum(process_scale, np.asarray([25.0, 5.0, 1.0, 5.0]))
    return {
        "residual_center": residual_center,
        "residual_scale": residual_scale,
        "summary_center": summary_center,
        "summary_scale": summary_scale,
        "pca_components": pca_components,
        "robust_precision": robust_precision,
        "healthy_prototypes": standardized[prototype_indices],
        "healthy_quantiles": healthy_quantiles,
        "quantile_levels": quantile_levels,
        "healthy_process_points": process_points,
        "process_scale": process_scale,
    }


def _group_rms(values: np.ndarray, names: list[str]) -> float:
    return float(np.sqrt(np.mean(np.square(values[:, _indices(names)]))))


def _robust_group_distance(summary_z: np.ndarray, names: list[str]) -> float:
    channel_indices = _indices(names)
    feature_indices = []
    width = len(SENSOR_COLUMNS)
    for statistic in range(5):
        feature_indices.extend(statistic * width + index for index in channel_indices)
    return float(np.sqrt(np.mean(np.square(summary_z[feature_indices]))))


def process_manifold_distance(
    process: dict[str, Any],
    healthy_process_points: np.ndarray,
    process_scale: np.ndarray,
) -> float:
    vector = np.asarray(
        [_finite(process.get(name)) for name in PROCESS_COLUMNS], dtype=float
    )
    normalized = (
        vector[None, :] - np.asarray(healthy_process_points, dtype=float)
    ) / np.asarray(process_scale, dtype=float)[None, :]
    return float(np.min(np.sqrt(np.mean(np.square(normalized), axis=1))))


def build_master_feature_vector(
    actual: np.ndarray,
    prediction: np.ndarray,
    process: dict[str, Any],
    artifact: dict[str, Any],
) -> np.ndarray:
    actual = np.asarray(actual, dtype=float)
    prediction = np.asarray(prediction, dtype=float)
    required_shape = (24, len(SENSOR_COLUMNS))
    if actual.shape != required_shape or prediction.shape != required_shape:
        raise ValueError(f"实际/预测窗口必须为{required_shape}")

    center = np.asarray(artifact["residual_center"], dtype=float)
    scale = np.asarray(artifact["residual_scale"], dtype=float)
    residual = actual - prediction
    residual_z = (residual - center[None, :]) / scale[None, :]
    absolute_z = np.abs(residual_z)
    thermal_indices = _indices(THERMAL)
    pressure_index = SENSOR_COLUMNS.index("压力")
    tension_index = SENSOR_COLUMNS.index("张力")
    speed_index = SENSOR_COLUMNS.index("线速度")
    trajectory_indices = _indices(["ABB_X", "ABB_Y", "ABB_Z", "位移"])

    summary = summarize_window(actual)
    summary_z = (
        summary - np.asarray(artifact["summary_center"], dtype=float)
    ) / np.asarray(artifact["summary_scale"], dtype=float)
    components = np.asarray(artifact["pca_components"], dtype=float)
    reconstruction = (summary_z @ components.T) @ components
    pca_spe = float(np.mean(np.square(summary_z - reconstruction)))

    prototypes = np.asarray(artifact["healthy_prototypes"], dtype=float)
    squared_distance = np.mean(
        np.square(prototypes - summary_z[None, :]), axis=1
    )
    gamma = 1.0 / max(float(np.median(squared_distance)), 1e-6)
    keca_distance = float(1.0 - np.max(np.exp(-gamma * squared_distance)))

    current_quantiles = np.quantile(
        actual, np.asarray(artifact["quantile_levels"], dtype=float), axis=0
    ).T
    healthy_quantiles = np.asarray(artifact["healthy_quantiles"], dtype=float)
    wasserstein = float(
        np.mean(np.abs(current_quantiles - healthy_quantiles) / scale[:, None])
    )
    robust_distance = float(
        np.sqrt(max(summary_z @ np.asarray(artifact["robust_precision"]) @ summary_z, 0.0)
                / len(summary_z))
    )

    thermal_spread_actual = np.std(actual[:, thermal_indices], axis=1)
    thermal_spread_prediction = np.std(prediction[:, thermal_indices], axis=1)
    thermal_uniformity_error = float(
        np.mean(np.abs(thermal_spread_actual - thermal_spread_prediction))
        / max(float(np.mean(scale[thermal_indices])), 1e-6)
    )
    thermal_ramp_error = float(
        np.mean(np.abs(np.diff(residual_z[:, thermal_indices], axis=0)))
    )
    pressure_impulse_error = float(
        abs(
            _integral(actual[:, pressure_index])
            - _integral(prediction[:, pressure_index])
        )
        / max(24.0 * scale[pressure_index], 1e-6)
    )
    contact_threshold = max(10.0, 0.10 * _finite(process.get("initial_compaction_force_N"), 400.0))
    contact_fraction_error = float(
        abs(
            np.mean(actual[:, pressure_index] >= contact_threshold)
            - np.mean(prediction[:, pressure_index] >= contact_threshold)
        )
    )
    nominal_speed = _finite(process.get("placement_speed_mm_s"), 80.0)
    speed_tracking_error = float(
        np.mean(np.abs(actual[:, speed_index] - nominal_speed))
        / max(abs(nominal_speed), 1.0)
    )
    residual_channel_rms = np.sqrt(np.mean(np.square(residual_z), axis=0))
    coherence = float(np.std(residual_channel_rms) / max(np.mean(residual_channel_rms), 1e-6))
    thermo_compaction_coupling_error = float(
        abs(
            np.corrcoef(
                np.mean(actual[:, thermal_indices], axis=1),
                actual[:, pressure_index],
            )[0, 1]
            - np.corrcoef(
                np.mean(prediction[:, thermal_indices], axis=1),
                prediction[:, pressure_index],
            )[0, 1]
        )
    )
    if not math.isfinite(thermo_compaction_coupling_error):
        thermo_compaction_coupling_error = 0.0

    mcfs_spatial_error = float(
        np.mean(np.std(residual_z, axis=1))
        + np.mean(np.abs(np.diff(residual_z[:, thermal_indices], axis=1)))
    )
    temporal_autoencoding_error = float(
        np.mean(np.abs(np.diff(residual_z, axis=0)))
        + 0.5 * np.mean(np.abs(residual_z[1:] - residual_z[:-1]))
    )
    values = {
        "thermal_residual_rms": _group_rms(residual_z, THERMAL),
        "thermal_residual_p95": float(np.quantile(absolute_z[:, thermal_indices], 0.95)),
        "thermal_bias": float(np.mean(np.abs(np.mean(residual_z[:, thermal_indices], axis=0)))),
        "thermal_uniformity_error": thermal_uniformity_error,
        "thermal_ramp_error": thermal_ramp_error,
        "pressure_residual_rms": float(np.sqrt(np.mean(np.square(residual_z[:, pressure_index])))),
        "pressure_residual_peak": float(np.max(absolute_z[:, pressure_index])),
        "pressure_impulse_error": pressure_impulse_error,
        "contact_fraction_error": contact_fraction_error,
        "tension_residual_rms": float(np.sqrt(np.mean(np.square(residual_z[:, tension_index])))),
        "motion_residual_rms": _group_rms(residual_z, MOTION),
        "speed_tracking_error": speed_tracking_error,
        "trajectory_residual_rms": float(np.sqrt(np.mean(np.square(residual_z[:, trajectory_indices])))),
        "vibration_residual_rms": _group_rms(residual_z, VIBRATION),
        "global_residual_p95": float(np.quantile(absolute_z, 0.95)),
        "cross_channel_residual_coherence": coherence,
        "thermo_compaction_coupling_error": thermo_compaction_coupling_error,
        "process_manifold_distance": process_manifold_distance(
            process,
            np.asarray(artifact["healthy_process_points"], dtype=float),
            np.asarray(artifact["process_scale"], dtype=float),
        ),
        "response_thermal_distance": _robust_group_distance(summary_z, THERMAL),
        "response_compaction_distance": _robust_group_distance(summary_z, COMPACTION),
        "response_motion_distance": _robust_group_distance(summary_z, [*MOTION, *VIBRATION]),
        "pca_spe": pca_spe,
        "keca_distance": keca_distance,
        "mcfs_spatial_error": mcfs_spatial_error,
        "temporal_autoencoding_error": temporal_autoencoding_error,
        "wasserstein_distance": wasserstein,
        "robust_mahalanobis_distance": robust_distance,
    }
    return np.asarray([values[name] for name in MASTER_FEATURE_NAMES], dtype=float)


def build_feature_vector(
    indicator: str,
    actual: np.ndarray,
    prediction: np.ndarray,
    process: dict[str, Any],
    artifact: dict[str, Any],
) -> np.ndarray:
    if indicator not in INDICATOR_FEATURES:
        raise KeyError(f"未知新数据集健康指标：{indicator}")
    master = build_master_feature_vector(actual, prediction, process, artifact)
    lookup = dict(zip(MASTER_FEATURE_NAMES, master))
    return np.asarray([lookup[name] for name in INDICATOR_FEATURES[indicator]])


def cause_probabilities(process: dict[str, Any]) -> dict[str, float]:
    force = _finite(process.get("initial_compaction_force_N"), 400.0)
    speed = _finite(process.get("placement_speed_mm_s"), 80.0)
    angle = _finite(process.get("pid_angle_deg"), 5.0)
    setpoint = _finite(process.get("temperature_setpoint_C"), 360.0)
    underheat = max(0.0, (350.0 - setpoint) / 20.0)
    overheat = max(0.0, (setpoint - 380.0) / 20.0)
    severe_overheat = max(0.0, (setpoint - 400.0) / 10.0)
    pressure_low = max(0.0, (350.0 - force) / 150.0)
    pressure_high = max(0.0, (force - 450.0) / 250.0)
    severe_high_pressure = max(0.0, (force - 700.0) / 100.0)
    angle_low = max(0.0, (3.0 - angle) / 3.0)
    angle_high = max(0.0, (angle - 7.0) / 5.0)
    speed_low = max(0.0, (70.0 - speed) / 20.0)
    speed_high = max(0.0, (speed - 90.0) / 20.0)
    raw_by_state = {
        "underheat": underheat,
        "high_speed_underheat": speed_high * underheat,
        "underheat_low_pressure": underheat * pressure_low,
        "overheat": overheat,
        "severe_overheat": severe_overheat,
        "overheat_high_pressure": overheat * pressure_high,
        "low_speed_overheat": speed_low * overheat,
        "low_pressure": pressure_low,
        "high_speed_low_pressure": speed_high * pressure_low,
        "high_pressure": pressure_high,
        "severe_high_pressure": severe_high_pressure,
        "angle_low": angle_low,
        "angle_high": angle_high,
        "angle_low_underheat": angle_low * underheat,
        "angle_high_overheat": angle_high * overheat,
    }
    raw = np.asarray(
        [raw_by_state[state] for state in NEW_ABNORMAL_STATES], dtype=float
    )
    if float(raw.sum()) <= 1e-12:
        raw[:] = 1.0
    raw /= raw.sum()
    return {state: float(raw[index]) for index, state in enumerate(NEW_ABNORMAL_STATES)}


class NewCollectionHealthEngine:
    def __init__(self, artifact_path: Path = DEFAULT_ARTIFACT) -> None:
        import joblib

        self.artifact_path = Path(artifact_path)
        if not self.artifact_path.exists():
            raise FileNotFoundError(
                f"新数据集健康指标文件不存在：{self.artifact_path}；"
                "请先运行 fit_new_collection_health.py"
            )
        self.artifact = joblib.load(self.artifact_path)

    @property
    def catalog(self) -> list[dict[str, Any]]:
        return list(self.artifact["catalog"])

    def candidate(self, indicator: str, model_kind: str) -> dict[str, Any]:
        candidates = [
            row for row in self.artifact["catalog"]
            if row["indicator"] == indicator
        ]
        selected = [row for row in candidates if row["model"] == model_kind]
        if not selected:
            selected = [row for row in candidates if row["recommended"]]
        if not selected:
            raise KeyError(f"没有可用的新数据集候选组合：{indicator}/{model_kind}")
        return dict(selected[0])

    def predict(
        self,
        indicator: str,
        actual: np.ndarray,
        prediction: np.ndarray,
        process: dict[str, Any],
        model_kind: str,
    ) -> tuple[float, dict[str, float], np.ndarray]:
        features = build_feature_vector(
            indicator, actual, prediction, process, self.artifact
        )
        candidate = self.candidate(indicator, model_kind)
        model = self.artifact["models"][(indicator, candidate["model"])]
        raw = np.asarray(model["binary_model"].predict_proba(features[None, :]))
        classes = np.asarray(model["binary_model"].classes_)
        positive = np.flatnonzero(classes == 1)
        score = float(raw[0, int(positive[0])]) if len(positive) else 0.0
        return score, cause_probabilities(process), features
