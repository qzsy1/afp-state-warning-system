from __future__ import annotations

import argparse
import json
import math
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import joblib

from online_inference import (
    DEFAULT_CHECKPOINT,
    OnlineIModernTCN,
    inspect_prediction_model,
    model_catalog,
    normalize_model_type,
)
from acquisition import (
    ALL_SENSOR_COLUMNS,
    ACQUISITION_SCHEMAS,
    AcquisitionConfig,
    AcquisitionManager,
    NEW_COLLECTION_SENSOR_COLUMNS,
    NEW_EXCLUDED_SENSOR_COLUMNS,
    SENSOR_COLUMNS,
    select_capture_folder,
)
from mysql_storage import MySQLCaptureStore, mysql_settings_from_mapping
from online_health_features import OnlineWindowFeatureEngine
from causal_online_runtime import CausalOnlineConsistency
from runtime_scaler import FeatureScaler
from new_collection_health import (
    INDICATOR_REQUIRED_OUTPUTS as NEW_INDICATOR_REQUIRED_OUTPUTS,
    SENSOR_COLUMNS as NEW_HEALTH_SENSOR_COLUMNS,
    NEW_ABNORMAL_STATES,
    NEW_STATE_LABELS,
    NewCollectionHealthEngine,
)
from web_training import WebTrainingManager


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
_PACKAGED_REPLAY_DIR = DATA_DIR / "legacy_replay"
OUTPUT_DIR = (
    _PACKAGED_REPLAY_DIR
    if (_PACKAGED_REPLAY_DIR / "TC_HI_soft_window_results.csv").exists()
    else APP_DIR.parent / "outputs_tc_hi_soft_consistency_v13_8"
)
CAUSAL_OUTPUT_DIR = APP_DIR.parent / "outputs_causal_online_consistency_v13_9"

LEGACY_STATE_LABELS = {
    "normal": "正常",
    "power_low": "功率过低",
    "power_high": "功率过高",
    "speed_low": "速度过低",
    "speed_high": "速度过高",
    "compaction_low": "压实力过低",
    "compaction_high": "压实力过高",
}
STATE_LABELS = {**LEGACY_STATE_LABELS, **NEW_STATE_LABELS}
ABNORMAL_STATES = [state for state in LEGACY_STATE_LABELS if state != "normal"]
THERMAL_SENSORS = [f"温度{index}" for index in range(1, 9)]
INDICATOR_REQUIRED_OUTPUTS = {
    "T-HI": THERMAL_SENSORS,
    "C-HI": ["压力"],
    "TC-HI": [*THERMAL_SENSORS, "压力"],
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
LIVE_SENSOR_UNITS = {
    "转速": "device unit",
    "位移": "mm",
    "压力": "N / device unit",
    "振动": "device unit",
    "温度": "°C",
    "ROI平均温度": "°C",
    "张力": "N",
    "线速度": "mm/s",
    "ABB_X": "mm",
    "ABB_Y": "mm",
    "ABB_Z": "mm",
    **{f"温度{index}": "°C" for index in range(1, 9)},
}
NEW_DEMO_ROOT = APP_DIR / "new_collection_demo_v11_3"
NEW_DEMO_SOURCE = NEW_DEMO_ROOT / "simulator_stream.csv"
NEW_DEMO_CHECKPOINT = (
    NEW_DEMO_ROOT
    / "models"
    / "i_modern_tcn_new_collection_v11_3.pth"
)
NEW_DEMO_METRICS = NEW_DEMO_ROOT / "models" / "test_metrics.json"


def select_prediction_model_file(initial_path: str = "") -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    initial = Path(initial_path).expanduser() if initial_path else DEFAULT_CHECKPOINT
    try:
        selected = filedialog.askopenfilename(
            parent=root,
            title="选择已训练的I-ModernTCN预测模型",
            initialdir=str(initial.parent),
            initialfile=initial.name,
            filetypes=[
                ("PyTorch模型", "*.pth *.pt"),
                ("所有文件", "*.*"),
            ],
        )
        return str(Path(selected).resolve()) if selected else ""
    finally:
        root.destroy()


def select_training_file(initial_path: str = "", kind: str = "csv") -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    initial = Path(initial_path).expanduser() if initial_path else Path.home()
    filetypes = (
        [("已整合训练CSV", "*.csv"), ("所有文件", "*.*")]
        if kind == "csv"
        else [("PyTorch模型", "*.pth *.pt"), ("所有文件", "*.*")]
    )
    try:
        selected = filedialog.askopenfilename(
            parent=root,
            title="选择已整合训练CSV" if kind == "csv" else "选择用于继续训练的模型",
            initialdir=str(initial.parent if initial.suffix else initial),
            initialfile=initial.name if initial.suffix else "",
            filetypes=filetypes,
        )
        return str(Path(selected).resolve()) if selected else ""
    finally:
        root.destroy()


def _finite(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


PROCESS_PARAMETER_SCHEMAS = {
    "legacy_original": [
        {"key": "p", "current_key": "current_p", "label": "功率", "unit": "W"},
        {"key": "v", "current_key": "current_v", "label": "铺放速度", "unit": "mm/s"},
        {"key": "pr", "current_key": "current_pr", "label": "压实力", "unit": "N"},
    ],
    "new_collection_v11_3": [
        {"key": "initial_compaction_force_N", "label": "初始压实力", "unit": "N"},
        {"key": "placement_speed_mm_s", "label": "铺放速度", "unit": "mm/s"},
        {"key": "pid_angle_deg", "label": "PID角度", "unit": "°"},
        {"key": "temperature_setpoint_C", "label": "设定温度", "unit": "°C"},
    ],
}

INDICATOR_VARIANTS = {
    "legacy_original": {
        "TC-HI": {
            "variant_id": "TC-HI-Legacy-8T1C",
            "label": "旧数据热－压实耦合指标（8路温度＋压力）",
            "construction": "8路温度响应与压力响应的热－压实耦合特征",
            "required_outputs": INDICATOR_REQUIRED_OUTPUTS["TC-HI"],
            "required_process_parameters": [],
        }
    },
    "new_collection_v11_3": {
        "TC-HI": {
            "variant_id": "TC-HI-New-10T2C4P",
            "label": "新数据热－压实－工艺耦合指标（10路温度＋压力/张力＋4工艺参数）",
            "construction": "10路热响应、压力/张力压实响应与4项工艺流形距离联合构建",
            "required_outputs": NEW_INDICATOR_REQUIRED_OUTPUTS["TC-HI"],
            "required_process_parameters": [
                "initial_compaction_force_N",
                "placement_speed_mm_s",
                "pid_angle_deg",
                "temperature_setpoint_C",
            ],
        }
    },
}


def _finite_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _source_value(sources, *keys):
    for source in sources:
        if source is None:
            continue
        for key in keys:
            value = source.get(key)
            number = _finite_or_none(value)
            if number is not None:
                return number
    return None


def build_process_payload(
    declared_schema: str,
    observed=None,
    fallback=None,
    injection_severity=0.0,
) -> dict:
    """Extract process parameters from the received row, then fall back to UI config."""
    observed = observed if observed is not None else {}
    fallback = fallback if fallback is not None else {}
    observed_new = any(
        _finite_or_none(observed.get(item["key"])) is not None
        for item in PROCESS_PARAMETER_SCHEMAS["new_collection_v11_3"]
    )
    observed_legacy = any(
        _finite_or_none(observed.get(item["key"])) is not None
        for item in PROCESS_PARAMETER_SCHEMAS["legacy_original"]
    )
    schema_id = (
        "new_collection_v11_3"
        if observed_new
        else "legacy_original"
        if observed_legacy
        else declared_schema
        if declared_schema in PROCESS_PARAMETER_SCHEMAS
        else "legacy_original"
    )
    definitions = PROCESS_PARAMETER_SCHEMAS[schema_id]
    display_parameters = []
    observed_count = 0
    payload = {
        "schema_id": schema_id,
        "injection_severity": _finite(injection_severity, 0.0),
    }
    for item in definitions:
        nominal = _source_value([observed, fallback], item["key"])
        current_key = item.get("current_key", item["key"])
        current = _source_value([observed], current_key, item["key"])
        if current is not None:
            observed_count += 1
        if current is None:
            current = _source_value([fallback], current_key, item["key"])
        if nominal is None:
            nominal = current
        payload[item["key"]] = nominal
        payload[current_key] = current
        display_parameters.append(
            {
                "key": item["key"],
                "label": item["label"],
                "unit": item["unit"],
                "value": current,
                "nominal": nominal,
            }
        )
    payload["display_parameters"] = display_parameters
    payload["parameter_source"] = (
        "input_data"
        if observed_count == len(definitions)
        else "input_data_with_config_fallback"
        if observed_count
        else "configuration"
    )
    return payload


def indicator_variant(dataset_schema: str, indicator: str) -> dict:
    variant = INDICATOR_VARIANTS.get(dataset_schema, {}).get(indicator)
    if variant is not None:
        return dict(variant)
    outputs = (
        NEW_INDICATOR_REQUIRED_OUTPUTS
        if dataset_schema == "new_collection_v11_3"
        else INDICATOR_REQUIRED_OUTPUTS
    ).get(indicator, [])
    return {
        "variant_id": f"{indicator}-{dataset_schema}",
        "label": indicator,
        "construction": "按当前输入数据方案提取对应响应与残差特征",
        "required_outputs": list(outputs),
        "required_process_parameters": [],
    }


def _json_value(value):
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if pd.isna(value):
        return None
    return value


def _record(series: pd.Series, fields: list[str]) -> dict:
    return {field: _json_value(series.get(field)) for field in fields}


def cap_pool(scores: np.ndarray, rho: float) -> tuple[float, np.ndarray]:
    scores = np.asarray(scores, dtype=float)
    if len(scores) == 0:
        return 0.0, np.asarray([], dtype=float)
    alpha = float(np.clip(rho, 0.0, 1.0)) * math.log(max(len(scores) - 1, 1))
    logits = alpha * scores
    logits -= np.max(logits)
    weights = np.exp(logits)
    weights /= np.sum(weights)
    return float(np.dot(weights, scores)), weights


class DashboardData:
    def __init__(self) -> None:
        required = [
            DATA_DIR / "dashboard_sequences.npz",
            DATA_DIR / "dashboard_window_index.csv",
            DATA_DIR / "dashboard_manifest.json",
            OUTPUT_DIR / "TC_HI_soft_window_results.csv",
            OUTPUT_DIR / "TC_HI_soft_layer_results_130.csv",
            OUTPUT_DIR / "TC_HI_soft_specimen_results_26.csv",
            DATA_DIR / "dashboard_candidate_scores.npz",
            DATA_DIR / "dashboard_candidate_catalog.csv",
            DATA_DIR / "dashboard_candidate_features.npz",
            DATA_DIR / "candidate_models",
            DATA_DIR / "online_feature_artifacts.joblib",
            (
                DATA_DIR / "causal_online_consistency_artifact.joblib"
                if (DATA_DIR / "causal_online_consistency_artifact.joblib").exists()
                else CAUSAL_OUTPUT_DIR / "causal_online_consistency_artifact.joblib"
            ),
        ]
        missing = [path for path in required if not path.exists()]
        if missing:
            raise FileNotFoundError(
                "缺少可视化数据，请先运行 prepare_dashboard_data.py：\n"
                + "\n".join(str(path) for path in missing)
            )

        arrays = np.load(DATA_DIR / "dashboard_sequences.npz", allow_pickle=False)
        self.actual = arrays["actual"]
        self.prediction = arrays["prediction"]
        self.model_input = arrays["model_input"]
        self.model_true = arrays["model_true"]
        self.scaler_mean = arrays["scaler_mean"].astype(float)
        self.scaler_scale = arrays["scaler_scale"].astype(float)
        self.sensor_model_indices = arrays["sensor_model_indices"].astype(int)
        self.index = pd.read_csv(DATA_DIR / "dashboard_window_index.csv")
        self.windows = pd.read_csv(OUTPUT_DIR / "TC_HI_soft_window_results.csv")
        self.layers = pd.read_csv(OUTPUT_DIR / "TC_HI_soft_layer_results_130.csv")
        self.specimens = pd.read_csv(OUTPUT_DIR / "TC_HI_soft_specimen_results_26.csv")
        self.manifest = json.loads(
            (DATA_DIR / "dashboard_manifest.json").read_text(encoding="utf-8")
        )
        candidate_arrays = np.load(
            DATA_DIR / "dashboard_candidate_scores.npz", allow_pickle=False
        )
        self.candidate_scores = candidate_arrays["anomaly_scores"]
        self.candidate_type_probabilities = candidate_arrays["type_probabilities"]
        self.candidate_catalog = pd.read_csv(
            DATA_DIR / "dashboard_candidate_catalog.csv"
        )
        feature_arrays = np.load(
            DATA_DIR / "dashboard_candidate_features.npz", allow_pickle=False
        )
        self.candidate_features = {
            name: feature_arrays[name] for name in feature_arrays.files
        }
        self.candidate_model_dir = DATA_DIR / "candidate_models"
        self.candidate_model_cache: dict[int, dict] = {}
        self.candidate_score_cache: dict[
            tuple[int, int, str], tuple[float, np.ndarray]
        ] = {}
        self.candidate_lock = threading.RLock()
        self.online_predictor = OnlineIModernTCN()
        # PyTorch and scikit-learn ship separate OpenMP runtimes on Windows.
        # Load the forecasting runtime before lazily opening any sklearn model.
        self.online_predictor.warmup()
        online_artifact = joblib.load(
            DATA_DIR / "online_feature_artifacts.joblib"
        )
        feature_scaler = FeatureScaler(
            mean=self.scaler_mean.copy(),
            scale=self.scaler_scale.copy(),
            source=DATA_DIR / "dashboard_sequences.npz",
        )
        self.online_feature_engine = OnlineWindowFeatureEngine(
            scaler=feature_scaler,
            ambient=_finite(
                self.manifest.get("ambient_temperature_reference"), 0.0
            ),
            coherence_floor=online_artifact["coherence_floor"],
            transformers=online_artifact["transformers"],
        )
        self.acquisition = AcquisitionManager()
        self.causal_online_optimizer = CausalOnlineConsistency(
                (
                    DATA_DIR / "causal_online_consistency_artifact.joblib"
                    if (DATA_DIR / "causal_online_consistency_artifact.joblib").exists()
                    else CAUSAL_OUTPUT_DIR / "causal_online_consistency_artifact.joblib"
                )
        )
        try:
            self.new_collection_health_engine = NewCollectionHealthEngine()
        except FileNotFoundError:
            self.new_collection_health_engine = None
        self.live_prediction_cache: dict[tuple[str, int], np.ndarray] = {}
        self.live_window_result_cache: dict[tuple[str, int, int], dict] = {}
        self.live_forecast_cache: dict[tuple[str, int, int], tuple[np.ndarray, str]] = {}
        # Causal, target-aligned predictions are keyed by (target_index, lead).
        # A target is frozen only for the lead at which it was originally
        # forecast.  This is separate from the latest rolling forecast shown in
        # the UI, so changing the display horizon cannot create a fixed lag.
        self.live_causal_prediction_cache: dict[
            str, dict[tuple[int, int], np.ndarray]
        ] = {}
        # Backward-compatible diagnostic cache containing only the latest
        # rolling forecast.  It is never used for historical alignment.
        self.live_rolling_prediction_cache: dict[str, dict[int, np.ndarray]] = {}
        self.live_cache_lock = threading.RLock()
        self.replay_prediction_cache: dict[int, np.ndarray] = {}
        self.live_layer_health_path = (
            self.acquisition.capture_root / "specimen_layer_health.json"
        )
        self.web_training = WebTrainingManager(Path(r"F:\AFP_Training_Models"))
        self.live_layer_health = self._load_live_layer_health()

        if len(self.windows) != len(self.actual) or len(self.index) != len(self.actual):
            raise RuntimeError("窗口结果、窗口索引与序列数据数量不一致。")
        if self.candidate_scores.shape != (
            len(self.candidate_catalog),
            len(self.actual),
        ):
            raise RuntimeError(
                "候选模型异常分数形状与候选目录/窗口数量不一致。"
            )
        if self.candidate_type_probabilities.shape[:2] != self.candidate_scores.shape:
            raise RuntimeError("候选模型异常类型概率形状不一致。")

        self.windows = self.windows.copy()
        self.windows["visual_index"] = self.index["visual_index"].to_numpy()
        self.windows["full_specimen_id"] = self.windows["full_specimen_id"].astype(str)
        self.windows["layer_sample_id"] = self.windows["layer_sample_id"].astype(str)
        self.layers["full_specimen_id"] = self.layers["full_specimen_id"].astype(str)
        self.layers["layer_sample_id"] = self.layers["layer_sample_id"].astype(str)
        self.specimens["full_specimen_id"] = self.specimens["full_specimen_id"].astype(str)

        self.sensors = self.manifest["sensors"]
        self.specimen_ids = self.specimens["full_specimen_id"].tolist()
        self.window_groups = {
            key: group.sort_values("window_sample_id").reset_index(drop=True)
            for key, group in self.windows.groupby("layer_sample_id", sort=False)
        }
        self.layer_groups = {
            key: group.sort_values("layer").reset_index(drop=True)
            for key, group in self.layers.groupby("full_specimen_id", sort=False)
        }

    def bootstrap(self) -> dict:
        specimens = []
        for _, row in self.specimens.iterrows():
            specimens.append(
                {
                    "id": str(row["full_specimen_id"]),
                    "split": str(row["dataset_split"]),
                    "true_state": str(row["true_state"]),
                    "true_state_label": STATE_LABELS.get(str(row["true_state"]), str(row["true_state"])),
                    "predicted_state": str(row["soft_predicted_state"]),
                    "predicted_state_label": STATE_LABELS.get(str(row["soft_predicted_state"]), str(row["soft_predicted_state"])),
                    "correct": bool(row["soft_prediction_correct"]),
                }
            )
        first = self.specimen_ids[0]
        first_layer = str(self.layer_groups[first].iloc[0]["layer_sample_id"])
        indicators = []
        for indicator, rows in self.candidate_catalog.groupby(
            "indicator_family", sort=False
        ):
            variant_info = indicator_variant("legacy_original", str(indicator))
            recommended = rows.loc[
                rows["recommended_for_indicator"].astype(bool)
            ].iloc[0]
            models = []
            for _, candidate in rows.sort_values(
                "validation_selection_score", ascending=False
            ).iterrows():
                models.append(
                    {
                        "id": str(candidate["model_kind"]),
                        "candidate_index": int(candidate["candidate_index"]),
                        "recommended": bool(candidate["recommended_for_indicator"]),
                        "validation_selection_score": _finite(
                            candidate["validation_selection_score"]
                        ),
                        "validation_window_balanced_accuracy": _finite(
                            candidate["validation_window_balanced_accuracy"]
                        ),
                        "validation_layer_balanced_accuracy": _finite(
                            candidate["validation_layer_balanced_accuracy"]
                        ),
                        "validation_specimen_balanced_accuracy": _finite(
                            candidate["validation_specimen_balanced_accuracy"]
                        ),
                        "test_window_balanced_accuracy": _finite(
                            candidate["test_window_balanced_accuracy"]
                        ),
                        "test_layer_balanced_accuracy": _finite(
                            candidate["test_layer_balanced_accuracy"]
                        ),
                        "test_specimen_balanced_accuracy": _finite(
                            candidate["test_specimen_balanced_accuracy"]
                        ),
                        "window_threshold": _finite(candidate["window_threshold"]),
                        "layer_threshold": _finite(candidate["layer_threshold"]),
                        "specimen_threshold": _finite(candidate["specimen_threshold"]),
                        "cap_rho": _finite(candidate["cap_rho"]),
                    }
                )
            indicators.append(
                {
                    "id": str(indicator),
                    "label": variant_info["label"],
                    "variant": variant_info,
                    "required_outputs": variant_info["required_outputs"],
                    "recommended_model": str(recommended["model_kind"]),
                    "models": models,
                }
            )
        new_indicators = []
        if self.new_collection_health_engine is not None:
            new_catalog = pd.DataFrame(
                self.new_collection_health_engine.catalog
            )
            for indicator, rows in new_catalog.groupby("indicator", sort=False):
                variant_info = indicator_variant(
                    "new_collection_v11_3", str(indicator)
                )
                recommended = rows.loc[rows["recommended"].astype(bool)].iloc[0]
                models = []
                for _, candidate in rows.sort_values(
                    "validation_selection_score", ascending=False
                ).iterrows():
                    models.append(
                        {
                            "id": str(candidate["model"]),
                            "recommended": bool(candidate["recommended"]),
                            "validation_selection_score": _finite(
                                candidate["validation_selection_score"]
                            ),
                            "validation_window_balanced_accuracy": _finite(
                                candidate["validation_window_balanced_accuracy"]
                            ),
                            "validation_layer_balanced_accuracy": _finite(
                                candidate["validation_layer_balanced_accuracy"]
                            ),
                            "validation_specimen_balanced_accuracy": _finite(
                                candidate["validation_specimen_balanced_accuracy"]
                            ),
                            "window_threshold": _finite(
                                candidate["window_threshold"]
                            ),
                            "layer_threshold": _finite(
                                candidate["layer_threshold"]
                            ),
                            "specimen_threshold": _finite(
                                candidate["specimen_threshold"]
                            ),
                            "cap_rho": _finite(candidate["cap_rho"]),
                        }
                    )
                new_indicators.append(
                    {
                        "id": str(indicator),
                        "label": (
                            variant_info["label"]
                            if str(indicator) == "TC-HI"
                            else str(recommended["indicator_label"])
                        ),
                        "variant": variant_info,
                        "required_outputs": variant_info["required_outputs"],
                        "recommended_model": str(recommended["model"]),
                        "models": models,
                    }
                )
        return {
            "manifest": self.manifest,
            "state_labels": STATE_LABELS,
            "sensors": self.sensors,
            "indicators": indicators,
            "indicator_schemas": {
                "legacy_original": indicators,
                "new_collection_v11_3": new_indicators,
            },
            "indicator_variants": INDICATOR_VARIANTS,
            "specimens": specimens,
            "defaults": {
                "specimen": first,
                "layer": first_layer,
                "sensor": 2,
                "cursor": 24,
                "history": 240,
                "stream_step": 1,
                "distance": 0,
                "length": 480,
                "step": 1,
                "threshold": 0.5,
                "rho": 0.5,
                "score_mode": "soft",
                "indicator": "TC-HI",
                "model": "random_forest",
                "prediction_horizon": 24,
                "prediction_model_type": "i_T_G",
                "forecast_lead": 1,
                # Replay defaults to the causal checkpoint path so the
                # displayed historical curve uses the selected forecast lead
                # instead of the fixed archived target sequence.
                "realtime_prediction": True,
                "use_optimized_warning": True,
                "data_mode": "replay",
            },
            "acquisition": {
                "drivers": self.acquisition.available_drivers(),
                "schemas": self.acquisition.available_schemas(),
                "sensors": SENSOR_COLUMNS,
                "prediction_model": self.online_predictor.profile,
                "prediction_models": model_catalog(),
                "best_prediction_models": {
                    schema_id: self.best_prediction_profile(schema_id)
                    for schema_id in ACQUISITION_SCHEMAS
                },
                "new_collection_demo": {
                    "source_file": (
                        str(NEW_DEMO_SOURCE.resolve())
                        if NEW_DEMO_SOURCE.exists()
                        else ""
                    ),
                    "prediction_model": (
                        inspect_prediction_model(
                            NEW_DEMO_CHECKPOINT, schema_mode="new_collection_v11_3"
                        )
                        if NEW_DEMO_CHECKPOINT.exists()
                        else None
                    ),
                },
                "indicator_required_outputs": INDICATOR_REQUIRED_OUTPUTS,
                "indicator_required_outputs_by_schema": {
                    "legacy_original": INDICATOR_REQUIRED_OUTPUTS,
                    "new_collection_v11_3": NEW_INDICATOR_REQUIRED_OUTPUTS,
                },
                "new_collection_health_ready": bool(
                    self.new_collection_health_engine is not None
                ),
                "default_save_root": str(
                    self.acquisition.capture_root.resolve()
                ),
                "original_columns": [
                    "振动", "转速", "位移",
                    *[f"温度{index}" for index in range(1, 9)],
                    "压力", "cycle", "file", "root",
                    "p", "v", "pr", "l", "试件",
                ],
            },
        }

    def inspect_prediction_model(
        self, checkpoint: str = "", model_type: str = "", architecture: str = "",
        schema_mode: str = "",
    ) -> dict:
        return inspect_prediction_model(
            checkpoint, model_type=model_type, architecture=architecture,
            schema_mode=schema_mode,
        )

    def best_prediction_profile(self, dataset_schema: str) -> dict:
        if (
            dataset_schema == "new_collection_v11_3"
            and NEW_DEMO_CHECKPOINT.exists()
        ):
            profile = inspect_prediction_model(
                NEW_DEMO_CHECKPOINT, schema_mode="new_collection_v11_3"
            )
            metric_value = None
            if NEW_DEMO_METRICS.exists():
                metrics = json.loads(
                    NEW_DEMO_METRICS.read_text(encoding="utf-8")
                )
                metric_value = _finite(
                    metrics.get("best_validation_mse_standardized"),
                    None,
                )
            return {
                **profile,
                "selection_metric": "validation_mse_standardized",
                "selection_metric_value": metric_value,
                "selection_basis": (
                    "仅使用冻结验证集误差；未使用内推/外推测试集"
                ),
                "registry_scope": "new_collection_v11_3",
            }
        profile = inspect_prediction_model(
            DEFAULT_CHECKPOINT, schema_mode="legacy_original"
        )
        return {
            **profile,
            "selection_metric": "registered_default",
            "selection_metric_value": None,
            "selection_basis": (
                "旧方案当前登记的唯一兼容论文检查点；未用测试集重选"
            ),
            "registry_scope": "legacy_original",
        }

    def validate_prediction_setup(
        self,
        config: AcquisitionConfig,
        *,
        load_model: bool,
    ) -> dict:
        if config.processing_mode == "capture_only":
            return {
                "processing_mode": "capture_only",
                "compatible": True,
                "model_required": False,
                "checkpoint": "",
                "selected_input_sensors": [],
                "selected_output_sensors": [],
                "health_indicator": None,
                "health_required_outputs": [],
            }
        profile = (
            self.best_prediction_profile(config.dataset_schema)
            if config.use_best_prediction_override
            else inspect_prediction_model(
                config.prediction_model_file,
                model_type=getattr(config, "prediction_model_type", "i_T_G"),
                schema_mode=config.dataset_schema,
            )
        )
        auto_corrected = False
        schema_sensors = list(
            ACQUISITION_SCHEMAS.get(
                config.dataset_schema, ACQUISITION_SCHEMAS["legacy_original"]
            )["sensors"]
        )
        if config.use_best_prediction_override:
            config.prediction_model_file = profile["checkpoint"]
        acquired_inputs = list(config.model_input_sensors or [])
        model_inputs = list(profile["input_sensors"])
        missing_inputs = [
            name for name in model_inputs if name not in acquired_inputs
        ]
        # The new collection plan intentionally omits rotation speed,
        # displacement and vibration.  Older demo checkpoints may still list
        # these as model columns; they are virtual baseline-filled inputs, not
        # required physical channels.
        virtual_missing_inputs = [
            name for name in missing_inputs
            if name in NEW_EXCLUDED_SENSOR_COLUMNS
            and config.dataset_schema == "new_collection_v11_3"
        ]
        effective_missing_inputs = [
            name for name in missing_inputs if name not in virtual_missing_inputs
        ]
        unexpected_inputs = [
            name for name in acquired_inputs if name not in model_inputs
        ]
        schema_conflict = bool(
            unexpected_inputs
            or any(name not in schema_sensors for name in acquired_inputs)
            or any(
                name not in schema_sensors
                and name not in NEW_EXCLUDED_SENSOR_COLUMNS
                for name in model_inputs
            )
        )
        if (effective_missing_inputs or unexpected_inputs) and schema_conflict:
            profile = self.best_prediction_profile(config.dataset_schema)
            config.prediction_model_file = profile["checkpoint"]
            config.model_input_sensors = list(profile["input_sensors"])
            config.model_output_sensors = list(profile["output_sensors"])
            config.prediction_sensors = list(profile["output_sensors"])
            config.selected_sensors = list(schema_sensors)
            acquired_inputs = list(config.model_input_sensors)
            model_inputs = list(profile["input_sensors"])
            missing_inputs = []
            unexpected_inputs = []
            auto_corrected = True
        if effective_missing_inputs or unexpected_inputs:
            raise ValueError(
                "当前采集传感器与所选预测模型输入不一致。"
                f"缺少：{effective_missing_inputs or '无'}；"
                f"模型未声明：{unexpected_inputs or '无'}"
            )
        if not set(acquired_inputs).issubset(
            set(config.selected_sensors or [])
        ):
            raise ValueError(
                "模型输入通道必须全部包含在连接/保存通道中"
            )
        selected_outputs = list(config.model_output_sensors or [])
        if not selected_outputs:
            raise ValueError("至少选择一个预测模型输出通道")
        if not set(acquired_inputs).issubset(set(config.selected_sensors or [])):
            config.selected_sensors = list(
                dict.fromkeys([*(config.selected_sensors or []), *acquired_inputs])
            )
            auto_corrected = True
        unsupported_outputs = [
            name
            for name in selected_outputs
            if name not in profile["output_sensors"]
        ]
        if unsupported_outputs:
            selected_outputs = [
                name for name in selected_outputs
                if name in profile["output_sensors"]
            ] or list(profile["output_sensors"])
            config.model_output_sensors = list(selected_outputs)
            config.prediction_sensors = list(selected_outputs)
            unsupported_outputs = []
            auto_corrected = True
        if unsupported_outputs:
            raise ValueError(
                f"所选模型不提供这些输出通道：{unsupported_outputs}"
            )
        indicator_outputs = (
            NEW_INDICATOR_REQUIRED_OUTPUTS
            if config.dataset_schema == "new_collection_v11_3"
            else INDICATOR_REQUIRED_OUTPUTS
        )
        required_outputs = indicator_outputs.get(
            config.health_indicator,
            (
                NEW_COLLECTION_SENSOR_COLUMNS
                if config.dataset_schema == "new_collection_v11_3"
                else SENSOR_COLUMNS
            ),
        )
        missing_health_outputs = [
            name for name in required_outputs if name not in selected_outputs
        ]
        if missing_health_outputs:
            compatible_indicator = next(
                (
                    name
                    for name in ("TC-HI", "C-HI", "T-HI", "RFHI", "PR-HI", "MPRF-HI")
                    if name in indicator_outputs
                    and set(indicator_outputs[name]).issubset(set(selected_outputs))
                ),
                None,
            )
            if compatible_indicator is not None:
                config.health_indicator = compatible_indicator
                required_outputs = indicator_outputs[compatible_indicator]
                missing_health_outputs = []
                auto_corrected = True
        if missing_health_outputs:
            raise ValueError(
                f"{config.health_indicator}健康指标需要模型输出："
                f"{missing_health_outputs}"
            )
        if load_model:
            profile = self.online_predictor.configure(
                profile["checkpoint"],
                model_type=getattr(config, "prediction_model_type", profile.get("model_type", "i_T_G")),
                schema_mode=config.dataset_schema,
            )
        return {
            **profile,
            "selected_input_sensors": acquired_inputs,
            "selected_output_sensors": selected_outputs,
            "health_indicator": config.health_indicator,
            "health_required_outputs": required_outputs,
            "auto_corrected_schema_profile": auto_corrected,
            "compatible": True,
            "best_prediction_override": bool(
                config.use_best_prediction_override
            ),
        }

    def candidate(self, indicator: str, model_kind: str) -> pd.Series:
        rows = self.candidate_catalog.loc[
            self.candidate_catalog["indicator_family"].astype(str).eq(indicator)
        ]
        if rows.empty:
            rows = self.candidate_catalog.loc[
                self.candidate_catalog["indicator_family"].astype(str).eq("TC-HI")
            ]
        selected = rows.loc[rows["model_kind"].astype(str).eq(model_kind)]
        if selected.empty:
            selected = rows.loc[rows["recommended_for_indicator"].astype(bool)]
        return selected.iloc[0]

    def new_collection_candidate(
        self, indicator: str, model_kind: str
    ) -> pd.Series:
        if self.new_collection_health_engine is None:
            raise RuntimeError(
                "新数据集健康指标尚未生成，请先运行"
                " fit_new_collection_health.py"
            )
        candidate = self.new_collection_health_engine.candidate(
            indicator, model_kind
        )
        catalog = self.new_collection_health_engine.catalog
        candidate_index = next(
            index for index, row in enumerate(catalog)
            if row["indicator"] == candidate["indicator"]
            and row["model"] == candidate["model"]
        )
        return pd.Series(
            {
                **candidate,
                "candidate_index": -1 - candidate_index,
                "indicator_family": candidate["indicator"],
                "model_kind": candidate["model"],
                "feature_key": "new_collection_multiphysics_v2",
                "recommended_for_indicator": candidate["recommended"],
            }
        )

    @staticmethod
    def _score_columns(score_mode: str) -> tuple[str, dict[str, str]]:
        if score_mode == "raw":
            score_col = "window_health_index"
            state_cols = {
                state: f"probability_{state}"
                for state in ABNORMAL_STATES
            }
        else:
            score_col = "soft_window_anomaly_probability"
            state_cols = {
                state: f"soft_window_probability_{state}"
                for state in ABNORMAL_STATES
            }
        return score_col, state_cols

    def _aggregate_layer(
        self,
        layer_id: str,
        score_mode: str,
        threshold: float,
        rho: float,
    ) -> dict:
        group = self.window_groups[layer_id]
        score_col, state_cols = self._score_columns(score_mode)
        scores = group[score_col].to_numpy(dtype=float)
        health, weights = cap_pool(scores, rho)
        type_probs = {
            state: float(np.dot(weights, group[col].to_numpy(dtype=float)))
            for state, col in state_cols.items()
        }
        if health < threshold:
            state = "normal"
        else:
            state = max(type_probs, key=type_probs.get)
        return {
            "health": health,
            "state": state,
            "state_label": STATE_LABELS[state],
            "type_probabilities": type_probs,
            "max_weight": float(np.max(weights)) if len(weights) else 0.0,
            "effective_windows": float(1.0 / np.sum(weights**2)) if len(weights) else 0.0,
        }

    def _aggregate_window_group(
        self,
        group: pd.DataFrame,
        score_mode: str,
        threshold: float,
        rho: float,
    ) -> dict | None:
        if group.empty:
            return None
        score_col, state_cols = self._score_columns(score_mode)
        scores = group[score_col].to_numpy(dtype=float)
        health, weights = cap_pool(scores, rho)
        type_probs = {
            state: float(np.dot(weights, group[column].to_numpy(dtype=float)))
            for state, column in state_cols.items()
        }
        predicted_state = (
            "normal" if health < threshold else max(type_probs, key=type_probs.get)
        )
        return {
            "health": health,
            "state": predicted_state,
            "state_label": STATE_LABELS[predicted_state],
            "type_probabilities": type_probs,
            "evidence_count": int(len(group)),
            "maximum_weight": float(np.max(weights)),
            "effective_count": float(1.0 / np.sum(weights**2)),
        }

    def _infer_candidate(
        self,
        candidate_index: int,
        visual_indices: np.ndarray,
        realtime_prediction: bool = False,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Run classifiers for newly completed windows and cache that evidence."""
        visual_indices = np.asarray(visual_indices, dtype=int)
        source = "live_checkpoint" if realtime_prediction else "archived"
        with self.candidate_lock:
            missing = [
                int(index)
                for index in dict.fromkeys(visual_indices.tolist())
                if (candidate_index, int(index), source)
                not in self.candidate_score_cache
            ]
            if missing:
                artifact = self._candidate_artifact(candidate_index)
                feature_key = str(artifact["feature_key"])
                predictions = (
                    self._replay_live_predictions(np.asarray(missing, dtype=int))
                    if realtime_prediction
                    else self.prediction[missing]
                )
                generated = self.online_feature_engine.transform(
                    self.actual[missing],
                    predictions,
                    requested_key=feature_key,
                )
                values = generated[feature_key]
                new_scores, aligned = self._predict_feature_values(
                    candidate_index, values
                )
                for row_index, visual_index in enumerate(missing):
                    self.candidate_score_cache[
                        (candidate_index, visual_index, source)
                    ] = (
                        float(new_scores[row_index]),
                        aligned[row_index].copy(),
                    )
            cached = [
                self.candidate_score_cache[
                    (candidate_index, int(index), source)
                ]
                for index in visual_indices
            ]
        scores = np.asarray([item[0] for item in cached], dtype=float)
        types = np.stack([item[1] for item in cached], axis=0)
        return scores, types

    def _replay_live_predictions(
        self, visual_indices: np.ndarray
    ) -> np.ndarray:
        outputs = []
        for visual_index in np.asarray(visual_indices, dtype=int):
            cached = self.replay_prediction_cache.get(int(visual_index))
            if cached is None:
                standardized, _ = self.online_predictor.predict(
                    self.model_input[int(visual_index)], 24
                )
                cached = (
                    standardized[:, self.sensor_model_indices]
                    * self.scaler_scale[self.sensor_model_indices]
                    + self.scaler_mean[self.sensor_model_indices]
                )
                self.replay_prediction_cache[int(visual_index)] = cached
            outputs.append(cached)
        return np.stack(outputs, axis=0)

    def _load_live_layer_health(self) -> dict[str, dict]:
        if not self.live_layer_health_path.exists():
            return {}
        try:
            payload = json.loads(
                self.live_layer_health_path.read_text(encoding="utf-8")
            )
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_live_layer_health(self) -> None:
        self.live_layer_health_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.live_layer_health_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.live_layer_health, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.live_layer_health_path)

    @staticmethod
    def _live_layer_key(
        specimen_id: str,
        layer: int,
        indicator: str,
        model_kind: str,
        prediction_model_signature: str = "",
    ) -> str:
        return (
            f"{specimen_id}|{indicator}|{model_kind}|"
            f"{prediction_model_signature}|{int(layer)}"
        )

    @staticmethod
    def _live_scope_signature(config: dict) -> str:
        """Return the physical-specimen/condition scope for live evidence.

        Layer evidence must accumulate only within one independent specimen
        and one fixed process condition.  The previous key used the specimen
        name and model only, so reusing a specimen name while changing the
        process parameters could display evidence from the preceding run.
        Keep the signature deterministic and exclude the layer number so the
        layers of one specimen still accumulate together.
        """
        schema = str(config.get("dataset_schema", "legacy_original"))
        common = {
            "schema": schema,
            "specimen_id": str(config.get("specimen_id", "LIVE_SPECIMEN")),
            "run_id": str(config.get("run_id", "LIVE_RUN")),
            "condition_id": str(config.get("condition_id", "LIVE")),
            "replicate": int(config.get("replicate", 1) or 1),
        }
        if schema == "new_collection_v11_3":
            common.update(
                {
                    "initial_force": float(config.get("initial_compaction_force_N", 0) or 0),
                    "placement_speed": float(config.get("placement_speed_mm_s", 0) or 0),
                    "pid_angle": float(config.get("pid_angle_deg", 0) or 0),
                    "temperature_setpoint": float(config.get("temperature_setpoint_C", 0) or 0),
                }
            )
        else:
            common.update(
                {
                    "power": float(config.get("p", 0) or 0),
                    "speed": float(config.get("v", 0) or 0),
                    "compaction": float(config.get("pr", 0) or 0),
                }
            )
        return json.dumps(common, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _stored_live_layers(
        self,
        specimen_id: str,
        indicator: str,
        model_kind: str,
        prediction_model_signature: str,
    ) -> dict[int, dict]:
        """Return all persisted layers for the active specimen/model.

        The original implementation enumerated ``range(5)`` because the
        first data-collection plan used five layers.  Live acquisition now
        accepts any non-negative layer number, so discover the layer index
        from the persisted key suffix instead of imposing that limit.
        """
        prefix = (
            f"{specimen_id}|{indicator}|{model_kind}|"
            f"{prediction_model_signature}|"
        )
        stored: dict[int, dict] = {}
        for key, value in self.live_layer_health.items():
            if not str(key).startswith(prefix):
                continue
            try:
                layer_index = int(str(key)[len(prefix) :])
            except (TypeError, ValueError):
                continue
            if layer_index >= 0 and isinstance(value, dict):
                stored[layer_index] = value
        return stored

    def _candidate_artifact(self, candidate_index: int) -> dict:
        artifact = self.candidate_model_cache.get(candidate_index)
        if artifact is None:
            row = self.candidate_catalog.iloc[candidate_index]
            artifact = joblib.load(
                self.candidate_model_dir / str(row["model_file"])
            )
            self.candidate_model_cache[candidate_index] = artifact
        return artifact

    def _predict_feature_values(
        self, candidate_index: int, values: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Calculate anomaly/type probabilities from freshly generated HI features."""
        artifact = self._candidate_artifact(candidate_index)
        values = np.asarray(values, dtype=float)
        binary_model = artifact["binary_model"]
        raw_binary = np.asarray(binary_model.predict_proba(values), dtype=float)
        binary_classes = np.asarray(binary_model.classes_)
        positive = np.flatnonzero(binary_classes == 1)
        scores = (
            raw_binary[:, int(positive[0])]
            if len(positive)
            else np.zeros(len(values), dtype=float)
        )
        type_model = artifact["type_model"]
        raw_type = np.asarray(type_model.predict_proba(values), dtype=float)
        aligned = np.zeros((len(values), len(ABNORMAL_STATES)), dtype=float)
        for source_index, label in enumerate(type_model.classes_):
            if str(label) in ABNORMAL_STATES:
                aligned[:, ABNORMAL_STATES.index(str(label))] = raw_type[
                    :, source_index
                ]
        row_sum = aligned.sum(axis=1, keepdims=True)
        aligned = np.divide(
            aligned,
            row_sum,
            out=np.full_like(aligned, 1.0 / len(ABNORMAL_STATES)),
            where=row_sum > 0,
        )
        return scores, aligned

    def _decision_scores(
        self,
        group: pd.DataFrame,
        candidate: pd.Series,
        use_optimized_warning: bool,
        realtime_prediction: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, str, bool]:
        visual_indices = group["visual_index"].to_numpy(dtype=int)
        candidate_index = int(candidate["candidate_index"])
        realtime_scores, realtime_types = self._infer_candidate(
            candidate_index, visual_indices, realtime_prediction
        )
        optimized_available = (
            str(candidate["indicator_family"]) == "TC-HI"
            and str(candidate["model_kind"]) == "random_forest"
        )
        if use_optimized_warning and optimized_available:
            scores = group["soft_window_anomaly_probability"].to_numpy(dtype=float)
            type_matrix = group[
                [f"soft_window_probability_{state}" for state in ABNORMAL_STATES]
            ].to_numpy(dtype=float)
            return scores, type_matrix, "optimized_v13_8_soft_consistency", True
        return (
            realtime_scores,
            realtime_types,
            "realtime_features_live_checkpoint"
            if realtime_prediction
            else "realtime_features_archived_prediction",
            False,
        )

    def _aggregate_candidate_group(
        self,
        group: pd.DataFrame,
        candidate: pd.Series,
        threshold: float,
        rho: float,
        use_optimized_warning: bool,
        realtime_prediction: bool = False,
    ) -> dict | None:
        if group.empty:
            return None
        scores, type_matrix, decision_mode, optimized_applied = self._decision_scores(
            group, candidate, use_optimized_warning, realtime_prediction
        )
        health, weights = cap_pool(scores, rho)
        type_probs = {
            state: float(np.dot(weights, type_matrix[:, index]))
            for index, state in enumerate(ABNORMAL_STATES)
        }
        predicted_state = (
            "normal" if health < threshold else max(type_probs, key=type_probs.get)
        )
        return {
            "health": health,
            "state": predicted_state,
            "state_label": STATE_LABELS[predicted_state],
            "type_probabilities": type_probs,
            "evidence_count": int(len(group)),
            "maximum_weight": float(np.max(weights)),
            "effective_count": float(1.0 / np.sum(weights**2)),
            "decision_mode": decision_mode,
            "optimized_warning_applied": optimized_applied,
        }

    def realtime(
        self,
        specimen_id: str,
        sensor_id: int,
        cursor: int,
        history: int,
        step: int,
        threshold: float,
        rho: float,
        score_mode: str,
        indicator: str = "TC-HI",
        model_kind: str = "random_forest",
        prediction_horizon: int = 24,
        realtime_prediction: bool = False,
        use_optimized_warning: bool = True,
        forecast_lead: int = 1,
    ) -> dict:
        if specimen_id not in self.layer_groups:
            specimen_id = self.specimen_ids[0]
        sensor_id = int(np.clip(sensor_id, 0, len(self.sensors) - 1))
        history = int(np.clip(history, 48, 2400))
        step = int(np.clip(step, 1, 24))
        threshold = float(np.clip(threshold, 0.0, 1.0))
        rho = float(np.clip(rho, 0.0, 1.0))
        score_mode = "raw" if score_mode == "raw" else "soft"
        prediction_horizon = int(np.clip(prediction_horizon, 1, 600))
        forecast_lead = int(np.clip(forecast_lead, 1, 24))
        candidate = self.candidate(indicator, model_kind)
        indicator = str(candidate["indicator_family"])
        model_kind = str(candidate["model_kind"])
        candidate_index = int(candidate["candidate_index"])
        window_threshold = float(np.clip(threshold, 0.0, 1.0))
        layer_threshold = _finite(candidate["layer_threshold"], threshold)
        specimen_threshold = _finite(candidate["specimen_threshold"], threshold)

        specimen_layers = self.layer_groups[specimen_id]
        layer_blocks: list[dict] = []
        total_points = 0
        for _, layer_row in specimen_layers.iterrows():
            layer_id = str(layer_row["layer_sample_id"])
            group = self.window_groups[layer_id]
            visual_indices = group["visual_index"].to_numpy(dtype=int)
            point_count = int(len(group) * self.actual.shape[1])
            layer_blocks.append(
                {
                    "id": layer_id,
                    "layer": int(layer_row["layer"]),
                    "group": group,
                    "visual_indices": visual_indices,
                    "start": total_points,
                    "end": total_points + point_count,
                    "point_count": point_count,
                }
            )
            total_points += point_count

        cursor = int(np.clip(cursor, 1, total_points))
        current_layer_index = next(
            (
                index
                for index, block in enumerate(layer_blocks)
                if cursor <= int(block["end"])
            ),
            len(layer_blocks) - 1,
        )
        current_block = layer_blocks[current_layer_index]
        local_cursor = cursor - int(current_block["start"])
        local_cursor = int(np.clip(local_cursor, 1, int(current_block["point_count"])))
        completed_current_windows = local_cursor // int(self.actual.shape[1])
        if local_cursor == int(current_block["point_count"]):
            completed_current_windows = len(current_block["group"])

        actual_parts = [
            self.actual[block["visual_indices"]].reshape(-1, self.actual.shape[-1])
            for block in layer_blocks
        ]
        prediction_parts = [
            self.prediction[block["visual_indices"]].reshape(-1, self.prediction.shape[-1])
            for block in layer_blocks
        ]
        specimen_actual = np.concatenate(actual_parts, axis=0)
        specimen_prediction = np.concatenate(prediction_parts, axis=0)
        full_true_parts = [
            self.model_true[block["visual_indices"]].reshape(-1, self.model_true.shape[-1])
            for block in layer_blocks
        ]
        specimen_model_true = np.concatenate(full_true_parts, axis=0)

        history_start = max(0, cursor - history)
        observed_indices = np.arange(history_start, cursor, step, dtype=int)
        future_end = min(total_points, cursor + prediction_horizon)
        future_indices = np.arange(cursor, future_end, dtype=int)
        historical_prediction_matrix = specimen_prediction
        prediction_source = "archived_prediction"
        if realtime_prediction:
            first_visual_index = int(layer_blocks[0]["visual_indices"][0])
            model_history_stream = np.concatenate(
                [self.model_input[first_visual_index], specimen_model_true[:cursor]],
                axis=0,
            )
            online_standardized, forecast_mode = self.online_predictor.predict(
                model_history_stream[-24:], prediction_horizon
            )
            online_physical = (
                online_standardized[:, self.sensor_model_indices]
                * self.scaler_scale[self.sensor_model_indices]
                + self.scaler_mean[self.sensor_model_indices]
            )
            future_prediction_matrix = online_physical
            future_time = (np.arange(prediction_horizon, dtype=int) + 1) / _finite(
                self.manifest.get("sampling_hz"), 10.0
            )
            # Use the same causal origin for the historical curve.  Previously
            # the checkbox only changed the future curve while the observed
            # curve continued to use the archived target sequence, so its
            # apparent lag never changed when the forecast lead was changed.
            causal_indices = np.arange(
                max(0, history_start), cursor, dtype=int
            )
            causal_matrix = self._replay_causal_prediction_matrix(
                model_history_stream=model_history_stream,
                target_indices=causal_indices,
                forecast_lead=forecast_lead,
                profile=self.online_predictor.profile,
                sensor_columns=[str(item["name"]) for item in self.sensors],
            )
            # Start with an empty historical prediction series.  The first
            # input context is not a prediction target, so it must remain
            # blank until a causal model origin exists.
            historical_prediction_matrix = np.full_like(
                specimen_prediction, np.nan, dtype=float
            )
            if len(causal_indices):
                finite_rows = np.isfinite(causal_matrix).all(axis=1)
                valid_targets = causal_indices[finite_rows]
                if len(valid_targets):
                    historical_prediction_matrix[valid_targets] = causal_matrix[
                        finite_rows
                    ]
            # Keep the legacy source label for clients that already consume it;
            # the precise causal alignment is exposed separately through
            # historical_prediction_mode below.
            prediction_source = "live_checkpoint"
        else:
            future_prediction_matrix = specimen_prediction[future_indices]
            forecast_mode = (
                "archived_direct_24"
                if prediction_horizon <= int(self.actual.shape[1])
                else "archived_rolling_windows"
            )
        # The first model input window is context only.  Do not draw archived
        # predictions over it; the prediction curve begins after this window.
        input_context_points = min(
            len(historical_prediction_matrix),
            int(self.online_predictor.profile.get("seq_len", 24)),
        )
        if input_context_points:
            historical_prediction_matrix[:input_context_points] = np.nan
        sampling_hz = _finite(self.manifest.get("sampling_hz"), 10.0)
        observed_time = (observed_indices - cursor) / sampling_hz
        if not realtime_prediction:
            future_time = (future_indices - cursor + 1) / sampling_hz

        # Chart-only smoothing keeps causal predictions visually comparable to
        # the original overlapping-window curve.  Warning calculations above
        # continue to use the unsmoothed model outputs.
        historical_prediction_matrix = self._smooth_prediction_for_display(
            historical_prediction_matrix
        )
        future_prediction_matrix = self._smooth_prediction_for_display(
            future_prediction_matrix
        )

        channels = []
        for sensor in self.sensors:
            index = int(sensor["id"])
            observed_actual = specimen_actual[observed_indices, index]
            observed_prediction = historical_prediction_matrix[
                observed_indices, index
            ]
            future_prediction = future_prediction_matrix[:, index]
            valid_residual = np.isfinite(observed_actual) & np.isfinite(
                observed_prediction
            )
            residual = observed_actual[valid_residual] - observed_prediction[
                valid_residual
            ]
            channels.append(
                {
                    **sensor,
                    "prediction_enabled": True,
                    "x_observed": observed_time.tolist(),
                    "actual": observed_actual.tolist(),
                    "prediction_observed": [
                        float(value) if math.isfinite(value) else None
                        for value in observed_prediction
                    ],
                    "x_future": future_time.tolist(),
                    "prediction_future": future_prediction.tolist(),
                    "actual_current": float(observed_actual[-1]),
                    "prediction_current": (
                        float(observed_prediction[-1])
                        if math.isfinite(observed_prediction[-1])
                        else None
                    ),
                    "rmse": (
                        float(np.sqrt(np.mean(residual**2)))
                        if len(residual)
                        else None
                    ),
                }
            )

        layer_evidence: list[dict] = []
        for index, block in enumerate(layer_blocks):
            if index < current_layer_index:
                evidence_count = len(block["group"])
            elif index == current_layer_index:
                evidence_count = int(completed_current_windows)
            else:
                evidence_count = 0
            evidence = block["group"].iloc[:evidence_count]
            aggregate = self._aggregate_candidate_group(
                evidence,
                candidate,
                layer_threshold,
                rho,
                use_optimized_warning,
                realtime_prediction,
            )
            layer_evidence.append(
                {
                    "id": block["id"],
                    "layer": int(block["layer"]),
                    "display_layer": int(block["layer"]) + 1,
                    "completed_windows": evidence_count,
                    "total_windows": int(len(block["group"])),
                    "status": "complete"
                    if evidence_count == len(block["group"])
                    else "active"
                    if index == current_layer_index
                    else "waiting",
                    "aggregate": aggregate,
                }
            )

        available_layers = [
            item for item in layer_evidence if item["aggregate"] is not None
        ]
        if available_layers:
            layer_scores = np.asarray(
                [item["aggregate"]["health"] for item in available_layers], dtype=float
            )
            specimen_health, layer_weights = cap_pool(layer_scores, rho)
            specimen_type_probs = {
                anomaly_state: float(
                    np.dot(
                        layer_weights,
                        np.asarray(
                            [
                                item["aggregate"]["type_probabilities"][anomaly_state]
                                for item in available_layers
                            ],
                            dtype=float,
                        ),
                    )
                )
                for anomaly_state in ABNORMAL_STATES
            }
            specimen_state = (
                "normal"
                if specimen_health < specimen_threshold
                else max(specimen_type_probs, key=specimen_type_probs.get)
            )
            specimen_realtime = {
                "health": specimen_health,
                "state": specimen_state,
                "state_label": STATE_LABELS[specimen_state],
                "type_probabilities": specimen_type_probs,
                "evidence_layers": len(available_layers),
                "effective_layers": float(1.0 / np.sum(layer_weights**2)),
            }
        else:
            specimen_realtime = None

        current_group = current_block["group"]
        completed_group = current_group.iloc[:completed_current_windows]
        current_visual_indices = completed_group["visual_index"].to_numpy(dtype=int)
        realtime_scores, realtime_type_matrix = self._infer_candidate(
            candidate_index, current_visual_indices, realtime_prediction
        ) if len(completed_group) else (
            np.asarray([], dtype=float),
            np.empty((0, len(ABNORMAL_STATES)), dtype=float),
        )
        (
            current_scores,
            current_type_matrix,
            current_decision_mode,
            optimized_applied,
        ) = (
            self._decision_scores(
                completed_group,
                candidate,
                use_optimized_warning,
                realtime_prediction,
            )
            if len(completed_group)
            else (
                np.asarray([], dtype=float),
                np.empty((0, len(ABNORMAL_STATES)), dtype=float),
                "waiting_for_complete_window",
                False,
            )
        )
        if completed_current_windows > 0:
            latest_window = current_group.iloc[completed_current_windows - 1]
            latest_score = float(current_scores[completed_current_windows - 1])
            latest_type_probs = {
                anomaly_state: float(
                    current_type_matrix[
                        completed_current_windows - 1, anomaly_index
                    ]
                )
                for anomaly_index, anomaly_state in enumerate(ABNORMAL_STATES)
            }
            latest_state = (
                "normal"
                if latest_score < window_threshold
                else max(latest_type_probs, key=latest_type_probs.get)
            )
            realtime_window = {
                "id": str(latest_window["window_sample_id"]),
                "score": latest_score,
                "raw_realtime_score": float(
                    realtime_scores[completed_current_windows - 1]
                ),
                "state": latest_state,
                "state_label": STATE_LABELS[latest_state],
                "type_probabilities": latest_type_probs,
                "complete": True,
                "decision_mode": current_decision_mode,
                "optimized_warning_applied": optimized_applied,
            }
        else:
            realtime_window = {
                "id": f"{current_block['id']}_W000",
                "score": None,
                "state": "pending",
                "state_label": "等待首个完整窗口",
                "type_probabilities": {state: 0.0 for state in ABNORMAL_STATES},
                "complete": False,
                "decision_mode": "waiting_for_complete_window",
                "optimized_warning_applied": False,
            }

        active_window_index = min(
            max((local_cursor - 1) // int(self.actual.shape[1]), 0),
            len(current_group) - 1,
        )
        process_row = current_group.iloc[int(active_window_index)]
        visible_count = int(completed_current_windows)
        timeline = [
            float(current_scores[index]) if index < visible_count else None
            for index in range(len(current_group))
        ]
        current_layer_aggregate = layer_evidence[current_layer_index]["aggregate"]
        official_specimen = self.specimens.loc[
            self.specimens["full_specimen_id"].astype(str).eq(specimen_id)
        ].iloc[0]

        return {
            "mode": "realtime_replay",
            "selection": {
                "specimen": specimen_id,
                "sensor": sensor_id,
                "cursor": cursor,
                "history": history,
                "step": step,
                "threshold": threshold,
                "rho": rho,
                "score_mode": score_mode,
                "indicator": indicator,
                "model": model_kind,
                "prediction_horizon": prediction_horizon,
                "forecast_lead": forecast_lead,
                "realtime_prediction": bool(realtime_prediction),
                "use_optimized_warning": bool(use_optimized_warning),
            },
            "candidate": {
                "indicator": indicator,
                "model": model_kind,
                "recommended": bool(candidate["recommended_for_indicator"]),
                "validation_selection_score": _finite(
                    candidate["validation_selection_score"]
                ),
                "validation_window_balanced_accuracy": _finite(
                    candidate["validation_window_balanced_accuracy"]
                ),
                "validation_layer_balanced_accuracy": _finite(
                    candidate["validation_layer_balanced_accuracy"]
                ),
                "validation_specimen_balanced_accuracy": _finite(
                    candidate["validation_specimen_balanced_accuracy"]
                ),
                "test_window_balanced_accuracy": _finite(
                    candidate.get("test_window_balanced_accuracy"), None
                ),
                "test_layer_balanced_accuracy": _finite(
                    candidate.get("test_layer_balanced_accuracy"), None
                ),
                "test_specimen_balanced_accuracy": _finite(
                    candidate.get("test_specimen_balanced_accuracy"), None
                ),
                "window_threshold": window_threshold,
                "layer_threshold": layer_threshold,
                "specimen_threshold": specimen_threshold,
                "cap_rho": rho,
            },
            "forecast": {
                "requested_horizon": prediction_horizon,
                "forecast_lead": forecast_lead,
                "lead_semantics": "历史曲线使用已冻结的因果提前量；回放数据使用窗口起点对齐预测",
                "returned_horizon": int(len(future_prediction_matrix)),
                "native_horizon": int(self.actual.shape[1]),
                "mode": forecast_mode,
                "realtime": bool(realtime_prediction),
                "checkpoint": (
                    self.online_predictor.checkpoint if realtime_prediction else None
                ),
            },
            "progress": {
                "cursor": cursor,
                "total_points": total_points,
                "percent": float(cursor / total_points),
                "current_layer_index": current_layer_index,
                "current_layer": int(current_block["layer"]) + 1,
                "current_layer_id": str(current_block["id"]),
                "layer_point": local_cursor,
                "layer_total_points": int(current_block["point_count"]),
                "current_window": int(active_window_index) + 1,
                "total_windows_in_layer": int(len(current_group)),
                "sample_in_window": int((local_cursor - 1) % int(self.actual.shape[1])) + 1,
                "window_length": int(self.actual.shape[1]),
                "finished": cursor >= total_points,
            },
            "channels": channels,
            "selected_channel": channels[sensor_id],
            "window": realtime_window,
            "layer": current_layer_aggregate,
            "specimen": specimen_realtime,
            "layers": layer_evidence,
            "timeline": {
                "scores": timeline,
                "threshold": window_threshold,
                "active_index": int(active_window_index),
                "completed_count": visible_count,
            },
            "process": build_process_payload(
                "legacy_original",
                observed=process_row,
                injection_severity=process_row.get("injection_severity", 0.0),
            ),
            "official_final": {
                "true_state": str(official_specimen["true_state"]),
                "true_state_label": STATE_LABELS.get(
                    str(official_specimen["true_state"]), str(official_specimen["true_state"])
                ),
                "predicted_state": str(official_specimen["soft_predicted_state"]),
                "predicted_state_label": STATE_LABELS.get(
                    str(official_specimen["soft_predicted_state"]),
                    str(official_specimen["soft_predicted_state"]),
                ),
            },
            "feature_generation": {
                "mode": "realtime_from_current_replay_window",
                "feature_key": str(candidate["feature_key"]),
                "indicator_variant": indicator_variant(
                    "legacy_original", indicator
                ),
                "prediction_source": prediction_source,
                "historical_prediction_mode": (
                    f"causal_lead_{forecast_lead}"
                    if realtime_prediction
                    else "archived_target_sequence"
                ),
                "all_12_indicators_supported": True,
            },
        }

    def _prediction_model_scaler(
        self, profile: dict
    ) -> tuple[np.ndarray, np.ndarray]:
        if profile.get("uses_dashboard_scaler"):
            return self.scaler_mean.copy(), self.scaler_scale.copy()
        mean = np.asarray(profile.get("scaler_mean"), dtype=float)
        scale = np.asarray(profile.get("scaler_scale"), dtype=float)
        if mean.shape != (int(profile["enc_in"]),) or scale.shape != mean.shape:
            raise ValueError("预测模型标准化参数与enc_in不一致")
        return mean, scale

    def _live_model_tensor(
        self,
        rows: list[dict],
        sensors: np.ndarray,
        profile: dict,
        sensor_columns: list[str] | None = None,
    ) -> np.ndarray:
        sensor_columns = sensor_columns or SENSOR_COLUMNS
        mean, scale = self._prediction_model_scaler(profile)
        model_columns = list(profile["model_columns"])
        column_index = {
            name: index for index, name in enumerate(model_columns)
        }
        full = np.empty((len(rows), len(model_columns)), dtype=np.float32)
        full[:] = mean
        for sensor_name in profile["input_sensors"]:
            if sensor_name not in sensor_columns:
                # New collection data intentionally does not acquire the
                # legacy rotation/displacement/vibration channels.  Their
                # model columns remain at the scaler baseline for backward
                # compatibility with the existing checkpoint.
                continue
            full[:, column_index[sensor_name]] = sensors[
                :, sensor_columns.index(sensor_name)
            ]
        for context_name in (
            "cycle",
            "v",
            "p",
            "pr",
            "l",
            "initial_compaction_force_N",
            "placement_speed_mm_s",
            "pid_angle_deg",
            "temperature_setpoint_C",
        ):
            if context_name not in column_index:
                continue
            index = column_index[context_name]
            full[:, index] = [
                _finite(row.get(context_name), mean[index])
                for row in rows
            ]
        return ((full - mean) / scale).astype(np.float32)

    def _prediction_to_sensor_matrix(
        self,
        standardized: np.ndarray,
        profile: dict,
        sensor_columns: list[str] | None = None,
    ) -> np.ndarray:
        sensor_columns = sensor_columns or SENSOR_COLUMNS
        standardized = np.asarray(standardized, dtype=float)
        mean, scale = self._prediction_model_scaler(profile)
        model_columns = list(profile["model_columns"])
        converted = np.full(
            (len(standardized), len(sensor_columns)),
            np.nan,
            dtype=float,
        )
        for sensor_name in profile["output_sensors"]:
            model_index = model_columns.index(sensor_name)
            display_name = sensor_name
            if display_name not in sensor_columns:
                # The archived dashboard calls the force channel 压实力 while
                # the acquisition/model schema uses 压力.  Treat them as the
                # same physical channel at the conversion boundary.
                display_name = {
                    "压力": "压实力",
                    "压实力": "压力",
                }.get(display_name, display_name)
            if display_name not in sensor_columns:
                continue
            sensor_index = sensor_columns.index(display_name)
            converted[:, sensor_index] = (
                standardized[:, model_index] * scale[model_index]
                + mean[model_index]
            )
        return converted

    @staticmethod
    def _smooth_prediction_for_display(values: np.ndarray) -> np.ndarray:
        """Reduce point-to-point display jitter without changing warning data.

        The archived dashboard curve is produced from overlapping 24-point
        forecasts and is consequently smoother than a point-by-point causal
        replay.  Apply a causal three-point median followed by a light low-pass
        blend only to the values sent to the chart.  Raw model predictions used
        by health features, thresholds and warning aggregation remain intact.
        """
        source = np.asarray(values, dtype=float)
        if source.ndim != 2 or len(source) < 2:
            return source.copy()
        result = np.full_like(source, np.nan, dtype=float)
        for column in range(source.shape[1]):
            source_finite_rows = np.flatnonzero(np.isfinite(source[:, column]))
            leading_blank = (
                int(source_finite_rows[0])
                if len(source_finite_rows)
                else len(source)
            )
            previous = np.nan
            for row in range(len(source)):
                window = source[max(0, row - 2) : row + 1, column]
                finite = window[np.isfinite(window)]
                if not len(finite):
                    continue
                median = float(np.median(finite))
                if math.isfinite(previous):
                    value = 0.65 * median + 0.35 * previous
                else:
                    value = median
                result[row, column] = value
                previous = value
            finite_rows = np.flatnonzero(np.isfinite(result[:, column]))
            if len(finite_rows):
                first = int(finite_rows[0])
                # Preserve the intentional blank model-input context.  The
                # smoothing routine may fill leading gaps for ordinary
                # display data, but it must not invent predictions before the
                # first forecast target exists.
                result[:max(first, leading_blank), column] = np.nan
                for row in range(first + 1, len(result)):
                    if not np.isfinite(result[row, column]):
                        result[row, column] = result[row - 1, column]
        return result

    def _replay_causal_prediction_matrix(
        self,
        *,
        model_history_stream: np.ndarray,
        target_indices: np.ndarray,
        forecast_lead: int,
        profile: dict,
        sensor_columns: list[str],
    ) -> np.ndarray:
        """Recompute replay predictions at their causal forecast origins.

        The archived dashboard prediction is a window-level target sequence and
        is useful for reproducing the original benchmark.  It is not, however,
        a prediction made at a selectable lead for every displayed point.  The
        online model can predict a batch of causal origins, so replay mode uses
        this helper when the realtime-prediction option is enabled.  A target at
        index ``t`` is taken from the ``forecast_lead``-th output of the model
        origin immediately before it, which makes changing the lead change the
        historical alignment rather than only the future panel.
        """
        targets = np.asarray(target_indices, dtype=int)
        output = np.full(
            (len(targets), len(sensor_columns)), np.nan, dtype=float
        )
        if not len(targets):
            return output
        lead = int(np.clip(forecast_lead, 1, 24))
        stream = np.asarray(model_history_stream, dtype=np.float32)
        seq_len = int(profile.get("seq_len", 24))
        if stream.ndim != 2 or stream.shape[1] != int(profile["enc_in"]):
            return output
        valid_targets: list[int] = []
        histories: list[np.ndarray] = []
        for position, target in enumerate(targets):
            # The stream begins with one 24-point context block.  The target
            # t is generated from the origin t-lead+1.
            origin = int(target) - lead + 1
            stream_end = seq_len + origin
            if origin < 0 or stream_end < seq_len or stream_end > len(stream):
                continue
            history = stream[stream_end - seq_len : stream_end]
            if history.shape == (seq_len, int(profile["enc_in"])) and np.isfinite(history).all():
                valid_targets.append(position)
                histories.append(history)
        if not histories:
            return output
        batch = np.stack(histories, axis=0)
        standardized, _ = self.online_predictor.predict_batch(batch, lead)
        rows = standardized[:, lead - 1, :]
        physical = self._prediction_to_sensor_matrix(
            rows, profile, sensor_columns
        )
        output[np.asarray(valid_targets, dtype=int)] = physical
        return output

    def _health_feature_arrays(
        self,
        actual: np.ndarray,
        prediction: np.ndarray,
        selected_outputs: set[str],
        sensor_columns: list[str] | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        sensor_columns = sensor_columns or SENSOR_COLUMNS
        actual = np.asarray(actual, dtype=float)
        prediction = np.asarray(prediction, dtype=float)
        baseline = self.scaler_mean[self.sensor_model_indices]
        health_actual = np.broadcast_to(
            baseline, (len(actual), len(SENSOR_COLUMNS))
        ).astype(float).copy()
        health_prediction = health_actual.copy()
        for sensor_name in selected_outputs:
            source_index = sensor_columns.index(sensor_name)
            health_index = SENSOR_COLUMNS.index(sensor_name)
            if (
                not np.isfinite(actual[:, source_index]).all()
                or not np.isfinite(prediction[:, source_index]).all()
            ):
                raise ValueError(
                    f"健康指标所需输出通道没有完整实测/预测：{sensor_name}"
                )
            health_actual[:, health_index] = actual[:, source_index]
            health_prediction[:, health_index] = prediction[:, source_index]
        return health_actual, health_prediction

    def _new_collection_health_arrays(
        self,
        actual: np.ndarray,
        prediction: np.ndarray,
        selected_outputs: set[str],
        sensor_columns: list[str],
    ) -> tuple[np.ndarray, np.ndarray]:
        if self.new_collection_health_engine is None:
            raise RuntimeError("新数据集健康指标模型尚未生成")
        artifact = self.new_collection_health_engine.artifact
        health_sensor_count = len(NEW_HEALTH_SENSOR_COLUMNS)
        baseline = np.asarray(
            artifact["summary_center"], dtype=float
        )[:health_sensor_count]
        health_actual = np.broadcast_to(
            baseline, (len(actual), health_sensor_count)
        ).copy()
        health_prediction = health_actual.copy()
        for sensor_name in selected_outputs:
            source_index = sensor_columns.index(sensor_name)
            target_index = NEW_HEALTH_SENSOR_COLUMNS.index(sensor_name)
            if (
                not np.isfinite(actual[:, source_index]).all()
                or not np.isfinite(prediction[:, source_index]).all()
            ):
                raise ValueError(
                    f"健康指标所需输出通道没有完整实测/预测：{sensor_name}"
                )
            health_actual[:, target_index] = actual[:, source_index]
            health_prediction[:, target_index] = prediction[:, source_index]
        return health_actual, health_prediction

    def _capture_only_live(
        self,
        *,
        status: dict,
        rows: list[dict],
        sensor_columns: list[str],
        sensor_id: int,
        history: int,
        step: int,
        prediction_horizon: int,
    ) -> dict:
        config = status.get("config") or {}
        sample_rate = _finite(config.get("sample_rate_hz"), 10.0)
        observed_start = max(0, len(rows) - history)
        indices = np.arange(observed_start, len(rows), step, dtype=int)
        observed_time = (
            (indices - max(len(rows), 1)) / sample_rate
            if len(indices)
            else np.asarray([], dtype=float)
        )
        channels: list[dict] = []
        for index, name in enumerate(sensor_columns):
            if name in SENSOR_COLUMNS:
                legacy_index = SENSOR_COLUMNS.index(name)
                sensor = {**self.sensors[legacy_index], "id": index}
            else:
                sensor = {
                    "id": index,
                    "name": name,
                    "unit": LIVE_SENSOR_UNITS.get(name, "device unit"),
                }
            values: list[float | None] = []
            for row_index in indices:
                value = _finite(rows[int(row_index)].get(name), float("nan"))
                values.append(float(value) if math.isfinite(value) else None)
            channels.append(
                {
                    **sensor,
                    "prediction_source_name": name,
                    "prediction_enabled": False,
                    "x_observed": observed_time.tolist(),
                    "actual": values,
                    "prediction_observed": [],
                    "x_future": [],
                    "prediction_future": [],
                    "actual_current": values[-1] if values else None,
                    "prediction_current": None,
                    "rmse": None,
                }
            )
        if not channels:
            raise ValueError("仅采集模式至少需要一个传感器通道")
        current_layer = max(0, int(config.get("layer", 0) or 0))
        completed_file_layers = {
            max(0, int(layer) - 1)
            for layer in (status.get("completed_layers") or [])
        }
        live_layer_indices = sorted(completed_file_layers | {current_layer})
        layers = [
            {
                "id": f"{config.get('specimen_id', 'LIVE')}_L{layer}",
                "layer": layer,
                "display_layer": layer + 1,
                "completed_windows": 0,
                "total_windows": 1,
                "status": "active" if layer == current_layer else "waiting",
                "aggregate": None,
            }
            for layer in live_layer_indices
        ]
        process = build_process_payload(
            str(config.get("dataset_schema", "legacy_original")),
            observed=rows[-1] if rows else {},
            fallback=config,
        )
        return {
            "mode": "capture_only",
            "selection": {
                "specimen": str(config.get("specimen_id", "LIVE_SPECIMEN")),
                "sensor": sensor_id,
                "cursor": len(rows),
                "history": history,
                "step": step,
                "threshold": 0.5,
                "rho": 0.5,
                "score_mode": "disabled",
                "indicator": "disabled",
                "model": "disabled",
                "prediction_horizon": prediction_horizon,
                "realtime_prediction": False,
                "prediction_sensors": [],
                "prediction_model": "",
                "use_optimized_warning": False,
            },
            "candidate": {
                "indicator": "仅采集",
                "model": "未启用预测/预警",
                "recommended": False,
                "validation_selection_score": None,
                "validation_window_balanced_accuracy": None,
                "validation_layer_balanced_accuracy": None,
                "validation_specimen_balanced_accuracy": None,
            },
            "forecast": {
                "requested_horizon": prediction_horizon,
                "returned_horizon": 0,
                "native_horizon": 24,
                "mode": "capture_only",
                "realtime": False,
                "checkpoint": "",
                "model_name": "未加载",
                "input_sensors": [],
                "available_output_sensors": [],
                "selected_output_sensors": [],
                "display_consistency": "not_applicable",
            },
            "progress": {
                "cursor": len(rows),
                "total_points": max(len(rows) + 1, 1),
                "percent": 0.0,
                "current_layer_index": current_layer,
                "current_layer": current_layer + 1,
                "current_layer_id": f"LIVE_L{current_layer}",
                "layer_point": len(rows),
                "layer_total_points": max(len(rows) + 1, 1),
                "current_window": 1,
                "total_windows_in_layer": 1,
                "sample_in_window": ((max(len(rows), 1) - 1) % 24) + 1,
                "window_length": 24,
                "finished": False,
            },
            "channels": channels,
            "selected_channel": channels[sensor_id],
            "window": {
                "id": "DISABLED",
                "complete": False,
                "score": None,
                "raw_realtime_score": None,
                "state": "pending",
                "state_label": "仅采集，不判定",
                "type_probabilities": {},
                "optimized_warning_applied": False,
            },
            "layer": None,
            "specimen": None,
            "layers": layers,
            "timeline": {
                "scores": [],
                "threshold": 0.5,
                "active_index": 0,
                "completed_count": 0,
            },
            "process": process,
            "official_final": {
                "true_state": "unknown",
                "true_state_label": "仅采集：未设置真值",
                "predicted_state": "disabled",
                "predicted_state_label": "未启用预测预警",
            },
            "acquisition": status,
            "feature_generation": {
                "mode": "capture_only",
                "feature_key": None,
                "all_12_indicators_supported": False,
                "completed_windows_reused": 0,
                "incremental_cache": False,
                "historical_prediction_mode": "disabled",
                "health_indicator_output_sensors": [],
                "selected_model_output_sensors": [],
                "warning_optimization": "disabled",
            },
        }

    def live(
        self,
        sensor_id: int,
        history: int,
        step: int,
        threshold: float,
        rho: float,
        indicator: str,
        model_kind: str,
        prediction_horizon: int,
        use_optimized_warning: bool = True,
        prediction_sensors: list[str] | None = None,
        processing_mode: str | None = None,
        forecast_lead: int = 1,
    ) -> dict:
        """Real acquisition -> live model -> live HI features -> warning."""
        status = self.acquisition.status()
        rows, timestamps = self.acquisition.numeric_matrix()
        config = status.get("config") or {}
        if processing_mode in {"capture_only", "prediction_warning"}:
            config = {**config, "processing_mode": processing_mode}
        active_sensor_columns = list(
            config.get("selected_sensors") or SENSOR_COLUMNS
        )
        sensor_id = int(
            np.clip(sensor_id, 0, max(len(active_sensor_columns) - 1, 0))
        )
        history = int(np.clip(history, 48, 2400))
        step = int(np.clip(step, 1, 24))
        threshold = float(np.clip(threshold, 0.0, 1.0))
        rho = float(np.clip(rho, 0.0, 1.0))
        prediction_horizon = int(np.clip(prediction_horizon, 1, 600))
        forecast_lead = int(np.clip(forecast_lead, 1, 24))
        if config.get("processing_mode") == "capture_only":
            return self._capture_only_live(
                status=status,
                rows=rows,
                sensor_columns=active_sensor_columns,
                sensor_id=sensor_id,
                history=history,
                step=step,
                prediction_horizon=prediction_horizon,
            )
        new_schema = config.get("dataset_schema") == "new_collection_v11_3"
        abnormal_states = (
            NEW_ABNORMAL_STATES if new_schema else ABNORMAL_STATES
        )
        candidate = (
            self.new_collection_candidate(indicator, model_kind)
            if new_schema
            else self.candidate(indicator, model_kind)
        )
        candidate_index = int(candidate["candidate_index"])
        feature_key = str(candidate["feature_key"])
        indicator = str(candidate["indicator_family"])
        model_kind = str(candidate["model_kind"])
        causal_optimized = bool(
            use_optimized_warning
            and not new_schema
            and indicator == "TC-HI"
            and model_kind == "random_forest"
        )
        active_profile = self.online_predictor.profile
        # A schema switch can arrive while an acquisition session is being
        # reused.  Reload the registered checkpoint compatible with the active
        # sensor set before constructing health features; otherwise an old
        # in-memory profile is reported as a health-indicator mismatch.
        active_sensor_names = set(active_sensor_columns)
        if any(
            str(name) not in active_sensor_names
            and not (
                new_schema
                and str(name) in NEW_EXCLUDED_SENSOR_COLUMNS
            )
            for name in active_profile.get("input_sensors", [])
        ):
            compatible_profile = self.best_prediction_profile(
                "new_collection_v11_3" if new_schema else "legacy_original"
            )
            self.online_predictor.configure(
                compatible_profile["checkpoint"],
                model_type=compatible_profile.get("model_type", "i_T_G"),
                schema_mode="new_collection_v11_3" if new_schema else "legacy_original",
            )
            active_profile = self.online_predictor.profile
        configured_prediction_sensors = (
            prediction_sensors
            if prediction_sensors is not None
            else config.get("model_output_sensors")
            or config.get("prediction_sensors")
        )
        prediction_sensor_names = set(
            active_profile["output_sensors"]
            if configured_prediction_sensors is None
            else configured_prediction_sensors
        )
        prediction_sensor_names.intersection_update(
            config.get("model_input_sensors")
            or config.get("selected_sensors")
            or active_sensor_columns
        )
        indicator_output_catalog = (
            NEW_INDICATOR_REQUIRED_OUTPUTS
            if new_schema
            else INDICATOR_REQUIRED_OUTPUTS
        )
        required_health_outputs = indicator_output_catalog.get(
            indicator,
            NEW_COLLECTION_SENSOR_COLUMNS if new_schema else SENSOR_COLUMNS,
        )
        missing_health_outputs = [
            name
            for name in required_health_outputs
            if name not in prediction_sensor_names
        ]
        if missing_health_outputs:
            # A schema switch may leave the previous indicator/output checklist
            # in the browser.  Select a compatible family instead of making
            # every live refresh fail until the page is manually reloaded.
            compatible_indicator = next(
                (
                    name
                    for name in ("TC-HI", "C-HI", "T-HI", "RFHI", "PR-HI", "MPRF-HI")
                    if name in indicator_output_catalog
                    and set(indicator_output_catalog[name]).issubset(
                        prediction_sensor_names
                    )
                ),
                None,
            )
            if compatible_indicator is None:
                raise ValueError(
                    f"{indicator}健康指标缺少模型输出通道："
                    f"{missing_health_outputs}；当前可用输出："
                    f"{sorted(prediction_sensor_names)}"
                )
            indicator = compatible_indicator
            required_health_outputs = indicator_output_catalog[indicator]
        prediction_model_signature = (
            f"{active_profile['checkpoint']}|"
            f"{','.join(sorted(prediction_sensor_names))}"
        )
        latest_process_row = rows[-1] if rows else {}
        process_payload = build_process_payload(
            "new_collection_v11_3" if new_schema else "legacy_original",
            observed=latest_process_row,
            fallback=config,
        )
        process_parameters = {
            name: _finite(process_payload.get(name), 0.0)
            for name in (
                "initial_compaction_force_N",
                "placement_speed_mm_s",
                "pid_angle_deg",
                "temperature_setpoint_C",
            )
        }

        sensors = np.full(
            (len(rows), len(active_sensor_columns)), np.nan, dtype=float
        )
        for row_index, row in enumerate(rows):
            for column_index, name in enumerate(active_sensor_columns):
                value = row.get(name)
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number):
                    sensors[row_index, column_index] = number
        acquired_model_inputs = [
            name
            for name in active_profile["input_sensors"]
            if name in active_sensor_columns
        ]
        input_indices = [
            active_sensor_columns.index(name)
            for name in acquired_model_inputs
        ]
        all_channels_ready = (
            len(rows) >= 24
            and bool(input_indices)
            and np.isfinite(sensors[:, input_indices]).all()
        )
        model_tensor = (
            self._live_model_tensor(
                rows, sensors, active_profile, active_sensor_columns
            )
            if all_channels_ready
            else None
        )

        # A stable folder can be reused when a layer is collected again. Include
        # started_at so cached predictions/results can never leak into a new run.
        session_key = (
            f"{status.get('session_dir') or 'no-session'}|"
            f"{status.get('started_at') or 0.0}"
        )
        completed: list[dict] = []
        # Keep the native 24-point window prediction for warning scores.  The
        # chart uses a separate target-aligned causal prediction series below.
        warning_prediction = np.full_like(sensors, np.nan)
        historical_prediction = np.full_like(sensors, np.nan)
        if all_channels_ready and len(rows) >= 48:
            for target_start in range(24, len(rows) - 23, 24):
                cache_key = (session_key, target_start)
                with self.live_cache_lock:
                    prediction = self.live_prediction_cache.get(cache_key)
                if prediction is None:
                    predicted_standardized, _ = self.online_predictor.predict(
                        model_tensor[target_start - 24 : target_start], 24
                    )
                    prediction = self._prediction_to_sensor_matrix(
                        predicted_standardized,
                        active_profile,
                        active_sensor_columns,
                    )
                    with self.live_cache_lock:
                        self.live_prediction_cache[cache_key] = prediction
                actual_window = sensors[target_start : target_start + 24]
                warning_prediction[
                    target_start : target_start + 24
                ] = prediction
                result_key = (session_key, candidate_index, target_start)
                with self.live_cache_lock:
                    cached_result = self.live_window_result_cache.get(result_key)
                if cached_result is None:
                    if new_schema:
                        health_actual, health_prediction = (
                            self._new_collection_health_arrays(
                                actual_window,
                                prediction,
                                set(required_health_outputs),
                                active_sensor_columns,
                            )
                        )
                        score, type_probabilities, feature_values = (
                            self.new_collection_health_engine.predict(
                                indicator,
                                health_actual,
                                health_prediction,
                                process_parameters,
                                model_kind,
                            )
                        )
                        cached_result = {
                            "score": float(score),
                            "type_probabilities": type_probabilities,
                            "feature_values": feature_values.tolist(),
                            "contact_observed": bool(
                                np.max(
                                    actual_window[
                                        :, active_sensor_columns.index("压力")
                                    ]
                                ) >= 10.0
                            ),
                        }
                    else:
                        health_actual, health_prediction = (
                            self._health_feature_arrays(
                                actual_window,
                                prediction,
                                set(required_health_outputs),
                                active_sensor_columns,
                            )
                        )
                        feature_sets = self.online_feature_engine.transform(
                            health_actual,
                            health_prediction,
                            requested_key=feature_key,
                        )
                        feature_values = feature_sets[feature_key]
                        with self.candidate_lock:
                            scores, type_matrix = self._predict_feature_values(
                                candidate_index, feature_values
                            )
                        cached_result = {
                            "score": float(scores[0]),
                            "type_probabilities": {
                                state: float(type_matrix[0, index])
                                for index, state in enumerate(abnormal_states)
                            },
                            "contact_observed": bool(
                                np.max(
                                    actual_window[
                                        :, active_sensor_columns.index("压力")
                                    ]
                                ) >= 10.0
                            ),
                        }
                    with self.live_cache_lock:
                        self.live_window_result_cache[result_key] = cached_result
                score = float(cached_result["score"])
                type_probs = dict(cached_result["type_probabilities"])
                predicted_state = (
                    "normal"
                    if score < threshold
                    else max(type_probs, key=type_probs.get)
                )
                completed.append(
                    {
                        "id": f"LIVE_W{len(completed):03d}",
                        "score": score,
                        "state": predicted_state,
                        "state_label": STATE_LABELS[predicted_state],
                        "type_probabilities": type_probs,
                        "target_start": target_start,
                        "feature_mode": "realtime_from_raw_window",
                        "contact_observed": bool(
                            cached_result["contact_observed"]
                        ),
                    }
                )

        future_prediction = np.empty(
            (0, len(active_sensor_columns)), dtype=float
        )
        rolling_forecast_prediction = np.empty(
            (0, len(active_sensor_columns)), dtype=float
        )
        forecast_mode = "waiting_for_24_points"
        if all_channels_ready:
            # Always retain at least the native 24-step forecast. Even when the
            # UI requests only 1 future point, this lets the next refresh align
            # every newly arrived observation with a prediction made before it
            # arrived instead of waiting for a complete 24-point target block.
            inference_horizon = max(24, prediction_horizon, forecast_lead)
            forecast_key = (session_key, len(rows), inference_horizon)
            with self.live_cache_lock:
                cached_forecast = self.live_forecast_cache.get(forecast_key)
            if cached_forecast is None:
                forecast_standardized, forecast_mode = self.online_predictor.predict(
                    model_tensor[-24:], inference_horizon
                )
                rolling_forecast_prediction = self._prediction_to_sensor_matrix(
                    forecast_standardized,
                    active_profile,
                    active_sensor_columns,
                )
                cached_forecast = (rolling_forecast_prediction, forecast_mode)
                with self.live_cache_lock:
                    self.live_forecast_cache[forecast_key] = cached_forecast
                    # Only one forecast is useful for a given session/horizon.
                    stale_keys = [
                        key
                        for key in self.live_forecast_cache
                        if key[0] == session_key
                        and key[2] == inference_horizon
                        and key != forecast_key
                    ]
                    for key in stale_keys:
                        self.live_forecast_cache.pop(key, None)
            else:
                rolling_forecast_prediction, forecast_mode = cached_forecast

            # The future curve is always the latest rolling forecast.  For the
            # historical curve, freeze only the prediction made with the
            # selected causal lead.  This prevents an old 24-step forecast
            # from being mistaken for a current prediction after the UI
            # horizon is changed.
            with self.live_cache_lock:
                latest_predictions = self.live_rolling_prediction_cache.setdefault(
                    session_key, {}
                )
                latest_predictions.clear()
                latest_predictions.update(
                    {
                        len(rows) + offset: np.asarray(predicted_row, dtype=float).copy()
                        for offset, predicted_row in enumerate(
                            rolling_forecast_prediction
                        )
                    }
                )
                causal_predictions = self.live_causal_prediction_cache.setdefault(
                    session_key, {}
                )
                max_causal_lead = min(24, len(rolling_forecast_prediction))
                for lead in range(1, max_causal_lead + 1):
                    target_index = len(rows) + lead - 1
                    causal_predictions.setdefault(
                        (target_index, lead),
                        np.asarray(
                            rolling_forecast_prediction[lead - 1],
                            dtype=float,
                        ).copy(),
                    )
                oldest_required = max(0, len(rows) - 2400)
                stale_keys = [
                    key for key in causal_predictions if key[0] < oldest_required
                ]
                for key in stale_keys:
                    causal_predictions.pop(key, None)

                historical_prediction = np.full_like(sensors, np.nan)
                for target_index in range(len(rows)):
                    predicted_row = causal_predictions.get(
                        (target_index, forecast_lead)
                    )
                    if predicted_row is not None:
                        historical_prediction[target_index] = predicted_row

                # Do not show stale values from a previous origin in the
                # future region.  Every refresh uses the newest model output.
                future_prediction = np.asarray(
                    rolling_forecast_prediction[:prediction_horizon],
                    dtype=float,
                )

        evidence_scores = np.asarray(
            [item["score"] for item in completed], dtype=float
        )
        specimen_id = str(config.get("specimen_id", "LIVE_SPECIMEN"))
        # Include specimen/process condition in the persistence namespace so
        # a new independent specimen or changed parameters starts at layer 0
        # and cannot inherit evidence from a previous condition.
        live_scope_signature = self._live_scope_signature(config)
        evidence_namespace = f"{prediction_model_signature}|{live_scope_signature}"
        current_layer = max(0, int(config.get("layer", 0) or 0))
        layer_aggregate = None
        specimen_aggregate = None
        persist_layer_health = False
        if len(evidence_scores):
            layer_health, weights = cap_pool(evidence_scores, rho)
            layer_type_probs = {
                state: float(
                    np.dot(
                        weights,
                        np.asarray(
                            [
                                item["type_probabilities"][state]
                                for item in completed
                            ],
                            dtype=float,
                        ),
                    )
                )
                for state in abnormal_states
            }
            layer_threshold = _finite(candidate["layer_threshold"], threshold)
            layer_state = (
                "normal"
                if layer_health < layer_threshold
                else max(layer_type_probs, key=layer_type_probs.get)
            )
            layer_aggregate = {
                "health": layer_health,
                "state": layer_state,
                "state_label": STATE_LABELS[layer_state],
                "type_probabilities": layer_type_probs,
                "evidence_count": len(completed),
                "maximum_weight": float(np.max(weights)),
                "effective_count": float(1.0 / np.sum(weights**2)),
                "decision_mode": "live_features_and_classifier",
                "optimized_warning_applied": False,
            }
            causal_summary = None
            if not new_schema:
                causal_summary = self.causal_online_optimizer.build_layer_summary(
                    evidence_scores,
                    np.asarray(
                        [
                            [
                                item["type_probabilities"][state]
                                for state in abnormal_states
                            ]
                            for item in completed
                        ],
                        dtype=float,
                    ),
                    np.asarray(
                        [item["contact_observed"] for item in completed],
                        dtype=bool,
                    ),
                )
            layer_key = self._live_layer_key(
                specimen_id,
                current_layer,
                indicator,
                model_kind,
                evidence_namespace,
            )
            previous_layer = self.live_layer_health.get(layer_key)
            layer_complete = not bool(status.get("running"))
            persist_layer_health = (
                previous_layer is None
                or int(previous_layer.get("completed_window_count", -1))
                != len(completed)
                or bool(previous_layer.get("layer_complete", False))
                != layer_complete
            )
            self.live_layer_health[layer_key] = {
                **layer_aggregate,
                **(
                    {"causal_summary": causal_summary}
                    if causal_summary is not None
                    else {}
                ),
                "specimen_id": specimen_id,
                "layer": current_layer,
                "indicator": indicator,
                "model_kind": model_kind,
                "prediction_model": active_profile["checkpoint"],
                "model_output_sensors": sorted(prediction_sensor_names),
                "run_id": str(config.get("run_id", "")),
                "updated_sample_count": len(rows),
                "completed_window_count": len(completed),
                "layer_complete": layer_complete,
            }
            if persist_layer_health and not causal_optimized:
                self._save_live_layer_health()

        stored_layers = self._stored_live_layers(
            specimen_id,
            indicator,
            model_kind,
            evidence_namespace,
        )
        completed_file_layers = {
            max(0, int(layer) - 1)
            for layer in (status.get("completed_layers") or [])
        }
        live_layer_indices = sorted(
            set(stored_layers) | completed_file_layers | {max(0, current_layer)}
        )
        if stored_layers:
            layer_items = [stored_layers[index] for index in sorted(stored_layers)]
            layer_scores = np.asarray(
                [item["health"] for item in layer_items], dtype=float
            )
            specimen_health, specimen_weights = cap_pool(layer_scores, rho)
            specimen_type_probs = {
                state: float(
                    np.dot(
                        specimen_weights,
                        np.asarray(
                            [item["type_probabilities"][state] for item in layer_items],
                            dtype=float,
                        ),
                    )
                )
                for state in abnormal_states
            }
            specimen_threshold = _finite(
                candidate["specimen_threshold"], threshold
            )
            specimen_state = (
                "normal"
                if specimen_health < specimen_threshold
                else max(specimen_type_probs, key=specimen_type_probs.get)
            )
            actual_layer_count = len(layer_items)
            specimen_complete = bool(layer_items) and all(
                bool(item.get("layer_complete", True)) for item in layer_items
            )
            specimen_aggregate = {
                "health": specimen_health,
                "state": specimen_state,
                "state_label": STATE_LABELS[specimen_state],
                "type_probabilities": specimen_type_probs,
                "evidence_layers": actual_layer_count,
                "actual_layer_count": actual_layer_count,
                "effective_layers": float(
                    1.0 / np.sum(specimen_weights**2)
                ),
                "complete": specimen_complete,
                "aggregation": "CAP pooling across all available physical layers",
            }

        latest = completed[-1] if completed else None
        if latest is None:
            message = (
                f"需要全部{len(active_profile['input_sensors'])}个模型输入"
                "通道连续收到24点才能预测"
                if not all_channels_ready
                else "首次窗口预警需要24点历史＋24点实测"
            )
            realtime_window = {
                "id": "LIVE_W000",
                "score": None,
                "raw_realtime_score": None,
                "state": "pending",
                "state_label": message,
                "type_probabilities": {
                    state: 0.0 for state in abnormal_states
                },
                "complete": False,
                "decision_mode": "waiting_for_live_evidence",
                "optimized_warning_applied": False,
            }
        else:
            realtime_window = {
                **latest,
                "raw_realtime_score": latest["score"],
                "complete": True,
                "decision_mode": "live_features_and_classifier",
                "optimized_warning_applied": False,
            }

        if causal_optimized and latest is not None and layer_aggregate is not None:
            causal_items = [
                stored_layers[index]
                for index in sorted(stored_layers)
                if index <= current_layer
                and "causal_summary" in stored_layers[index]
            ]
            if causal_items:
                causal_result = self.causal_online_optimizer.predict(
                    [item["causal_summary"] for item in causal_items],
                    window_health=float(latest["score"]),
                    window_types=np.asarray(
                        [
                            latest["type_probabilities"][state]
                            for state in abnormal_states
                        ],
                        dtype=float,
                    ),
                    current_layer_complete=not bool(status.get("running")),
                    current_window_fraction=min(
                        1.0, len(completed) / 24.0
                    ),
                )

                def posterior_values(posterior: np.ndarray) -> tuple:
                    posterior = np.asarray(posterior, dtype=float)
                    state = self.causal_online_optimizer.states[
                        int(np.argmax(posterior))
                    ]
                    abnormal = float(1.0 - posterior[0])
                    anomaly = posterior[1:]
                    total = float(anomaly.sum())
                    type_probs = {
                        name: float(anomaly[index] / total)
                        if total > 0
                        else 1.0 / len(abnormal_states)
                        for index, name in enumerate(abnormal_states)
                    }
                    return state, abnormal, type_probs

                window_state, window_health, window_types = posterior_values(
                    causal_result["window_posterior"]
                )
                layer_state, optimized_layer_health, optimized_layer_types = (
                    posterior_values(causal_result["layer_posterior"])
                )
                (
                    specimen_state,
                    optimized_specimen_health,
                    optimized_specimen_types,
                ) = posterior_values(causal_result["specimen_posterior"])
                realtime_window.update(
                    {
                        "score": window_health,
                        "state": window_state,
                        "state_label": STATE_LABELS[window_state],
                        "type_probabilities": window_types,
                        "decision_mode": causal_result["method"],
                        "optimized_warning_applied": True,
                    }
                )
                layer_aggregate.update(
                    {
                        "raw_health": layer_aggregate["health"],
                        "health": optimized_layer_health,
                        "state": layer_state,
                        "state_label": STATE_LABELS[layer_state],
                        "type_probabilities": optimized_layer_types,
                        "decision_mode": causal_result["method"],
                        "optimized_warning_applied": True,
                    }
                )
                specimen_aggregate = {
                    "health": optimized_specimen_health,
                    "state": specimen_state,
                    "state_label": STATE_LABELS[specimen_state],
                    "type_probabilities": optimized_specimen_types,
                    "evidence_layers": len(causal_items),
                    "effective_layers": (
                        specimen_aggregate["effective_layers"]
                        if specimen_aggregate
                        else float(len(causal_items))
                    ),
                    "complete": bool(causal_result["all_five_complete"]),
                    "aggregation": causal_result["method"],
                    "optimized_warning_applied": True,
                }
                current_key = self._live_layer_key(
                    specimen_id,
                    current_layer,
                    indicator,
                    model_kind,
                    evidence_namespace,
                )
                self.live_layer_health[current_key][
                    "optimized_aggregate"
                ] = layer_aggregate
                if persist_layer_health:
                    self._save_live_layer_health()

        observed_start = max(0, len(rows) - history)
        observed_indices = np.arange(observed_start, len(rows), step, dtype=int)
        sampling_hz = (
            _finite(status.get("config", {}).get("sample_rate_hz"), 10.0)
            if status.get("config")
            else 10.0
        )
        observed_time = (
            (observed_indices - max(len(rows), 1)) / sampling_hz
            if len(observed_indices)
            else np.asarray([], dtype=float)
        )
        future_time = (
            np.arange(len(future_prediction), dtype=float) + 1
        ) / sampling_hz
        # Smooth only the visual series.  The raw matrices were already used
        # for the current window, layer and specimen health decisions.
        historical_prediction = self._smooth_prediction_for_display(
            historical_prediction
        )
        future_prediction = self._smooth_prediction_for_display(
            future_prediction
        )
        channels = []
        for index, source_sensor_name in enumerate(active_sensor_columns):
            if source_sensor_name in SENSOR_COLUMNS:
                legacy_index = SENSOR_COLUMNS.index(source_sensor_name)
                sensor = {**self.sensors[legacy_index], "id": index}
            else:
                sensor = {
                    "id": index,
                    "name": source_sensor_name,
                    "unit": LIVE_SENSOR_UNITS.get(
                        source_sensor_name, "device unit"
                    ),
                }
            prediction_enabled = source_sensor_name in prediction_sensor_names
            actual_values = (
                sensors[observed_indices, index]
                if len(observed_indices)
                else np.asarray([], dtype=float)
            )
            prediction_values = (
                historical_prediction[observed_indices, index]
                if len(observed_indices)
                else np.asarray([], dtype=float)
            )
            valid_residual = np.isfinite(actual_values) & np.isfinite(
                prediction_values
            )
            rmse = (
                float(
                    np.sqrt(
                        np.mean(
                            np.square(
                                actual_values[valid_residual]
                                - prediction_values[valid_residual]
                            )
                        )
                    )
                )
                if np.any(valid_residual)
                else None
            )
            channels.append(
                {
                    **sensor,
                    "prediction_source_name": source_sensor_name,
                    "prediction_enabled": prediction_enabled,
                    "x_observed": observed_time.tolist(),
                    "actual": [
                        float(value) if math.isfinite(value) else None
                        for value in actual_values
                    ],
                    "prediction_observed": [
                        float(value) if math.isfinite(value) else None
                        for value in prediction_values
                    ] if prediction_enabled else [],
                    "x_future": (
                        future_time.tolist() if prediction_enabled else []
                    ),
                    "prediction_future": (
                        future_prediction[:, index].tolist()
                        if prediction_enabled and len(future_prediction)
                        else []
                    ),
                    "actual_current": (
                        float(actual_values[-1])
                        if len(actual_values) and math.isfinite(actual_values[-1])
                        else None
                    ),
                    "prediction_current": (
                        float(prediction_values[-1])
                        if prediction_enabled
                        and len(prediction_values)
                        and math.isfinite(prediction_values[-1])
                        else (
                            float(future_prediction[0, index])
                            if prediction_enabled and len(future_prediction)
                            else None
                        )
                    ),
                    "rmse": rmse if prediction_enabled else None,
                }
            )

        layers = []
        for layer_index in live_layer_indices:
            is_current = layer_index == current_layer
            stored = stored_layers.get(layer_index)
            current_layer_complete = bool(
                is_current
                and layer_aggregate is not None
                and not status.get("running")
            )
            completed_windows = (
                len(completed)
                if is_current
                else int(stored.get("evidence_count", 0))
                if stored
                else 0
            )
            total_windows = (
                max(
                    len(completed)
                    if current_layer_complete
                    else len(completed) + 1,
                    1,
                )
                if is_current
                else max(completed_windows, 1)
            )
            layers.append(
                {
                    "id": f"{specimen_id}_L{layer_index}",
                    "layer": layer_index,
                    "display_layer": layer_index + 1,
                    "completed_windows": completed_windows,
                    "total_windows": total_windows,
                    "status": (
                        "complete"
                        if current_layer_complete
                        else "active"
                        if is_current
                        else "complete"
                        if stored
                        else "waiting"
                    ),
                    "aggregate": (
                        layer_aggregate
                        if is_current
                        else stored.get("optimized_aggregate", stored)
                        if stored and causal_optimized
                        else stored
                    ),
                }
            )

        process = process_payload
        return {
            "mode": "live_acquisition",
            "selection": {
                "specimen": str(config.get("specimen_id", "LIVE_SPECIMEN")),
                "sensor": sensor_id,
                "cursor": len(rows),
                "history": history,
                "step": step,
                "threshold": threshold,
                "rho": rho,
                "score_mode": "live",
                "indicator": indicator,
                "model": model_kind,
                "prediction_horizon": prediction_horizon,
                "forecast_lead": forecast_lead,
                "realtime_prediction": True,
                "prediction_sensors": sorted(prediction_sensor_names),
                "prediction_model": active_profile["checkpoint"],
                "best_prediction_override": bool(
                    config.get("use_best_prediction_override", False)
                ),
                "use_optimized_warning": causal_optimized,
            },
            "candidate": {
                "indicator": indicator,
                "model": model_kind,
                "recommended": bool(candidate["recommended_for_indicator"]),
                "validation_selection_score": _finite(
                    candidate["validation_selection_score"]
                ),
                "validation_window_balanced_accuracy": _finite(
                    candidate["validation_window_balanced_accuracy"]
                ),
                "validation_layer_balanced_accuracy": _finite(
                    candidate["validation_layer_balanced_accuracy"]
                ),
                "validation_specimen_balanced_accuracy": _finite(
                    candidate["validation_specimen_balanced_accuracy"]
                ),
                "test_window_balanced_accuracy": _finite(
                    candidate.get("test_window_balanced_accuracy"), None
                ),
                "test_layer_balanced_accuracy": _finite(
                    candidate.get("test_layer_balanced_accuracy"), None
                ),
                "test_specimen_balanced_accuracy": _finite(
                    candidate.get("test_specimen_balanced_accuracy"), None
                ),
                "window_threshold": threshold,
                "layer_threshold": _finite(
                    candidate["layer_threshold"], threshold
                ),
                "specimen_threshold": _finite(
                    candidate["specimen_threshold"], threshold
                ),
                "cap_rho": rho,
            },
                "forecast": {
                    "requested_horizon": prediction_horizon,
                    "forecast_lead": forecast_lead,
                    "lead_semantics": "历史曲线使用冻结的因果提前量；未来曲线每次刷新使用最新滚动预测",
                "returned_horizon": len(future_prediction),
                "native_horizon": 24,
                "mode": forecast_mode,
                "realtime": True,
                "checkpoint": self.online_predictor.checkpoint,
                "model_name": active_profile.get("name", "I-ModernTCN"),
                "input_sensors": active_profile["input_sensors"],
                "available_output_sensors": active_profile["output_sensors"],
                "selected_output_sensors": sorted(prediction_sensor_names),
                "best_prediction_override": bool(
                    config.get("use_best_prediction_override", False)
                ),
                "display_consistency": "latest_rolling_future_target_aligned_history",
                "warning_prediction_alignment": "native_24_window_origin",
            },
            "progress": {
                "cursor": len(rows),
                "total_points": max(len(rows) + 1, 1),
                "percent": 0.0,
                "current_layer_index": current_layer,
                "current_layer": current_layer + 1,
                "current_layer_id": f"LIVE_L{current_layer}",
                "layer_point": len(rows),
                "layer_total_points": max(len(rows) + 1, 48),
                "current_window": len(completed) + 1,
                "total_windows_in_layer": max(len(completed) + 1, 1),
                "sample_in_window": ((max(len(rows), 1) - 1) % 24) + 1,
                "window_length": 24,
                "finished": False,
            },
            "channels": channels,
            "selected_channel": channels[sensor_id],
            "window": realtime_window,
            "layer": layer_aggregate,
            "specimen": specimen_aggregate,
            "layers": layers,
            "timeline": {
                "scores": [item["score"] for item in completed] + [None],
                "threshold": threshold,
                "active_index": len(completed),
                "completed_count": len(completed),
            },
            "process": process,
            "official_final": {
                "true_state": "unknown",
                "true_state_label": "真实采集：未知",
                "predicted_state": (
                    specimen_aggregate["state"]
                    if specimen_aggregate
                    else "pending"
                ),
                "predicted_state_label": (
                    specimen_aggregate["state_label"]
                    if specimen_aggregate
                    else "等待数据"
                ),
            },
            "acquisition": status,
            "feature_generation": {
                "mode": "realtime_from_actual_and_live_prediction",
                "feature_key": feature_key,
                "indicator_variant": indicator_variant(
                    "new_collection_v11_3" if new_schema else "legacy_original",
                    indicator,
                ),
                "all_12_indicators_supported": True,
                "completed_windows_reused": len(completed),
                "incremental_cache": True,
                "historical_prediction_mode": (
                    f"target_aligned_causal_lead_{forecast_lead}"
                ),
                "health_indicator_output_sensors": required_health_outputs,
                "selected_model_output_sensors": sorted(
                    prediction_sensor_names
                ),
                "warning_optimization": (
                    "causal_online_v13_9"
                    if causal_optimized
                    else "none"
                ),
            },
        }

    def view(
        self,
        specimen_id: str,
        layer_id: str | None,
        sensor_id: int,
        distance: int,
        length: int,
        step: int,
        threshold: float,
        rho: float,
        score_mode: str,
    ) -> dict:
        if specimen_id not in self.layer_groups:
            specimen_id = self.specimen_ids[0]
        specimen_layers = self.layer_groups[specimen_id]
        valid_layers = specimen_layers["layer_sample_id"].astype(str).tolist()
        if layer_id not in valid_layers:
            layer_id = valid_layers[0]

        sensor_id = int(np.clip(sensor_id, 0, len(self.sensors) - 1))
        threshold = float(np.clip(threshold, 0.0, 1.0))
        rho = float(np.clip(rho, 0.0, 1.0))
        score_mode = "raw" if score_mode == "raw" else "soft"

        window_group = self.window_groups[layer_id]
        visual_indices = window_group["visual_index"].to_numpy(dtype=int)
        layer_actual = self.actual[visual_indices]
        layer_prediction = self.prediction[visual_indices]
        point_count = int(layer_actual.shape[0] * layer_actual.shape[1])

        distance = int(np.clip(distance, 0, max(point_count - 1, 0)))
        length = int(np.clip(length, 24, point_count))
        step = int(np.clip(step, 1, 24))
        end = max(1, point_count - distance)
        start = max(0, end - length)
        selection = np.arange(start, end, step, dtype=int)

        actual_flat = layer_actual.reshape(point_count, layer_actual.shape[-1])
        prediction_flat = layer_prediction.reshape(point_count, layer_prediction.shape[-1])
        sampling_hz = _finite(self.manifest.get("sampling_hz"), 10.0)
        x_seconds = (selection - (end - 1)) / sampling_hz

        selected_actual = actual_flat[selection, sensor_id]
        selected_prediction = prediction_flat[selection, sensor_id]
        selected_residual = selected_actual - selected_prediction
        current_window_local = min((end - 1) // 24, len(window_group) - 1)
        current_window = window_group.iloc[int(current_window_local)]

        layer_official = specimen_layers.loc[
            specimen_layers["layer_sample_id"].astype(str).eq(layer_id)
        ].iloc[0]
        specimen_official = self.specimens.loc[
            self.specimens["full_specimen_id"].astype(str).eq(specimen_id)
        ].iloc[0]

        layer_preview = self._aggregate_layer(layer_id, score_mode, threshold, rho)
        all_layer_previews = [
            self._aggregate_layer(other_layer, score_mode, threshold, rho)
            for other_layer in valid_layers
        ]
        layer_scores = np.asarray([item["health"] for item in all_layer_previews], dtype=float)
        specimen_health, layer_weights = cap_pool(layer_scores, rho)
        specimen_type_probs = {
            state: float(
                np.dot(
                    layer_weights,
                    np.asarray(
                        [item["type_probabilities"][state] for item in all_layer_previews],
                        dtype=float,
                    ),
                )
            )
            for state in ABNORMAL_STATES
        }
        if specimen_health < threshold:
            specimen_preview_state = "normal"
        else:
            specimen_preview_state = max(specimen_type_probs, key=specimen_type_probs.get)

        score_col, state_cols = self._score_columns(score_mode)
        timeline_scores = window_group[score_col].to_numpy(dtype=float)
        current_type_probs = {
            state: _finite(current_window[col])
            for state, col in state_cols.items()
        }
        current_score = _finite(current_window[score_col])
        current_preview_state = (
            "normal"
            if current_score < threshold
            else max(current_type_probs, key=current_type_probs.get)
        )

        sensor_stats = []
        selected_actual_all = actual_flat[selection]
        selected_prediction_all = prediction_flat[selection]
        for sensor in self.sensors:
            idx = int(sensor["id"])
            residual = selected_actual_all[:, idx] - selected_prediction_all[:, idx]
            sensor_stats.append(
                {
                    "id": idx,
                    "name": sensor["name"],
                    "unit": sensor["unit"],
                    "actual_mean": float(np.mean(selected_actual_all[:, idx])),
                    "prediction_mean": float(np.mean(selected_prediction_all[:, idx])),
                    "rmse": float(np.sqrt(np.mean(residual**2))),
                    "actual_last": float(selected_actual_all[-1, idx]),
                    "prediction_last": float(selected_prediction_all[-1, idx]),
                }
            )

        official_fields = [
            "true_state",
            "soft_predicted_state",
            "soft_prediction_correct",
            "dataset_split",
        ]
        layer_fields = [
            "layer_sample_id",
            "layer",
            "true_state",
            "soft_predicted_state",
            "soft_prediction_correct",
            "soft_layer_anomaly_probability",
            "layer_health_index",
        ]
        process_fields = [
            "p",
            "v",
            "pr",
            "current_p",
            "current_v",
            "current_pr",
            "injection_severity",
        ]
        return {
            "selection": {
                "specimen": specimen_id,
                "layer": layer_id,
                "sensor": sensor_id,
                "distance": distance,
                "length": length,
                "step": step,
                "threshold": threshold,
                "rho": rho,
                "score_mode": score_mode,
                "point_count": point_count,
                "displayed_points": int(len(selection)),
            },
            "available_layers": [
                {
                    "id": str(row["layer_sample_id"]),
                    "layer": int(row["layer"]),
                    "label": f"第 {int(row['layer']) + 1} 层（内部索引 {int(row['layer'])}）",
                }
                for _, row in specimen_layers.iterrows()
            ],
            "series": {
                "x_seconds": x_seconds.tolist(),
                "actual": selected_actual.tolist(),
                "prediction": selected_prediction.tolist(),
                "residual": selected_residual.tolist(),
                "sensor": self.sensors[sensor_id],
            },
            "sensor_stats": sensor_stats,
            "window_timeline": {
                "labels": window_group["window_sample_id"].astype(str).tolist(),
                "scores": timeline_scores.tolist(),
                "threshold": threshold,
                "current_index": int(current_window_local),
            },
            "current_window": {
                "id": str(current_window["window_sample_id"]),
                "official_state": str(
                    current_window[
                        "soft_predicted_state" if score_mode == "soft" else "predicted_window_state"
                    ]
                ),
                "official_state_label": STATE_LABELS.get(
                    str(
                        current_window[
                            "soft_predicted_state" if score_mode == "soft" else "predicted_window_state"
                        ]
                    ),
                    str(current_window.get("predicted_window_state", "")),
                ),
                "preview_state": current_preview_state,
                "preview_state_label": STATE_LABELS[current_preview_state],
                "score": current_score,
                "type_probabilities": current_type_probs,
            },
            "layer_official": {
                **_record(layer_official, layer_fields),
                "true_state_label": STATE_LABELS.get(str(layer_official["true_state"]), ""),
                "predicted_state_label": STATE_LABELS.get(str(layer_official["soft_predicted_state"]), ""),
            },
            "specimen_official": {
                **_record(specimen_official, official_fields),
                "true_state_label": STATE_LABELS.get(str(specimen_official["true_state"]), ""),
                "predicted_state_label": STATE_LABELS.get(str(specimen_official["soft_predicted_state"]), ""),
            },
            "preview": {
                "layer": layer_preview,
                "specimen": {
                    "health": specimen_health,
                    "state": specimen_preview_state,
                    "state_label": STATE_LABELS[specimen_preview_state],
                    "type_probabilities": specimen_type_probs,
                    "layer_weights": layer_weights.tolist(),
                },
            },
            "process": build_process_payload(
                "legacy_original",
                observed=current_window,
                injection_severity=current_window.get("injection_severity", 0.0),
            ),
        }


class AppHandler(BaseHTTPRequestHandler):
    dashboard: DashboardData

    def log_message(self, fmt: str, *args) -> None:
        # 实时回放可达到10 Hz；逐请求打印会淹没终端并影响长时间运行。
        return

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        raw = path.read_bytes()
        mime, _ = mimetypes.guess_type(path.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @staticmethod
    def _one(query: dict[str, list[str]], key: str, default: str) -> str:
        return query.get(key, [default])[0]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json({"status": "ok", "version": "1.11.0"})
            return
        if parsed.path == "/api/training/status":
            query = parse_qs(parsed.query)
            self._send_json(self.dashboard.web_training.status(int(self._one(query, "after_seq", "0"))))
            return
        if parsed.path == "/api/training/defaults":
            query = parse_qs(parsed.query)
            self._send_json(self.dashboard.web_training.defaults(self._one(query, "mode", "new")))
            return
        if parsed.path == "/api/bootstrap":
            self._send_json(self.dashboard.bootstrap())
            return
        if parsed.path == "/api/acquisition/status":
            self._send_json(self.dashboard.acquisition.status())
            return
        if parsed.path == "/api/live":
            try:
                query = parse_qs(parsed.query)
                payload = self.dashboard.live(
                    sensor_id=int(self._one(query, "sensor", "2")),
                    history=int(self._one(query, "history", "240")),
                    step=int(self._one(query, "step", "1")),
                    threshold=float(self._one(query, "threshold", "0.5")),
                    rho=float(self._one(query, "rho", "0.5")),
                    indicator=self._one(query, "indicator", "TC-HI"),
                    model_kind=self._one(
                        query, "model", "random_forest"
                    ),
                    prediction_horizon=int(
                        self._one(query, "prediction_horizon", "24")
                    ),
                    forecast_lead=int(
                        self._one(query, "forecast_lead", "1")
                    ),
                    use_optimized_warning=self._one(
                        query, "use_optimized_warning", "true"
                    ).lower() in {"1", "true", "yes", "on"},
                    prediction_sensors=(
                        (
                            []
                            if self._one(
                                query, "prediction_sensors", "__none__"
                            ) == "__none__"
                            else [
                                name
                                for name in self._one(
                                    query, "prediction_sensors", ""
                                ).split(",")
                                if name in ALL_SENSOR_COLUMNS
                            ]
                        )
                        if "prediction_sensors" in query
                        else None
                    ),
                    processing_mode=self._one(
                        query, "processing_mode", "prediction_warning"
                    ),
                )
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/view":
            try:
                query = parse_qs(parsed.query)
                payload = self.dashboard.view(
                    specimen_id=self._one(query, "specimen", ""),
                    layer_id=self._one(query, "layer", "") or None,
                    sensor_id=int(self._one(query, "sensor", "2")),
                    distance=int(self._one(query, "distance", "0")),
                    length=int(self._one(query, "length", "480")),
                    step=int(self._one(query, "step", "1")),
                    threshold=float(self._one(query, "threshold", "0.5")),
                    rho=float(self._one(query, "rho", "0.5")),
                    score_mode=self._one(query, "score_mode", "soft"),
                )
                self._send_json(payload)
            except Exception as exc:  # pragma: no cover - returned to browser
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/realtime":
            try:
                query = parse_qs(parsed.query)
                payload = self.dashboard.realtime(
                    specimen_id=self._one(query, "specimen", ""),
                    sensor_id=int(self._one(query, "sensor", "2")),
                    cursor=int(self._one(query, "cursor", "24")),
                    history=int(self._one(query, "history", "240")),
                    step=int(self._one(query, "step", "1")),
                    threshold=float(self._one(query, "threshold", "0.5")),
                    rho=float(self._one(query, "rho", "0.5")),
                    score_mode=self._one(query, "score_mode", "soft"),
                    indicator=self._one(query, "indicator", "TC-HI"),
                    model_kind=self._one(query, "model", "random_forest"),
                    prediction_horizon=int(
                        self._one(query, "prediction_horizon", "24")
                    ),
                    realtime_prediction=self._one(
                        query, "realtime_prediction", "false"
                    ).lower() in {"1", "true", "yes", "on"},
                    forecast_lead=int(
                        self._one(query, "forecast_lead", "1")
                    ),
                    use_optimized_warning=self._one(
                        query, "use_optimized_warning", "true"
                    ).lower() in {"1", "true", "yes", "on"},
                )
                self._send_json(payload)
            except Exception as exc:  # pragma: no cover - returned to browser
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        relative = "index.html" if parsed.path in ("", "/") else parsed.path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self._send_file(target)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是JSON对象")
            if parsed.path == "/api/acquisition/test":
                config = AcquisitionConfig(**payload)
                model_validation = self.dashboard.validate_prediction_setup(
                    config, load_model=False
                )
                result = self.dashboard.acquisition.test_connection(config)
                result["prediction_model"] = model_validation
                self._send_json(result)
                return
            if parsed.path == "/api/training/import":
                self._send_json(self.dashboard.web_training.import_source(payload))
                return
            if parsed.path == "/api/training/start":
                self._send_json(self.dashboard.web_training.start(payload))
                return
            if parsed.path == "/api/training/stop":
                self._send_json(self.dashboard.web_training.stop())
                return
            if parsed.path == "/api/training/select-file":
                selected = select_training_file(
                    str(payload.get("initial_path", "")),
                    str(payload.get("kind", "csv")),
                )
                self._send_json({"selected": bool(selected), "path": selected})
                return
            if parsed.path == "/api/mysql/test":
                settings = mysql_settings_from_mapping(payload)
                self._send_json(MySQLCaptureStore(settings).test_connection())
                return
            if parsed.path == "/api/mysql/relation-map":
                settings = mysql_settings_from_mapping(payload)
                result = MySQLCaptureStore(settings).relation_map(
                    int(payload.get("limit", 1000))
                )
                self._send_json(result)
                return
            if parsed.path == "/api/prediction-model/select-file":
                selected = select_prediction_model_file(
                    str(payload.get("initial_path", ""))
                )
                self._send_json(
                    {
                        "selected": bool(selected),
                        "path": selected,
                        "model": (
                            self.dashboard.inspect_prediction_model(
                                selected, str(payload.get("model_type", "")),
                                schema_mode=str(payload.get("schema_mode", "")),
                            )
                            if selected
                            else None
                        ),
                    }
                )
                return
            if parsed.path == "/api/prediction-model/inspect":
                self._send_json(
                    self.dashboard.inspect_prediction_model(
                        str(payload.get("path", "")),
                        str(payload.get("model_type", "")),
                        schema_mode=str(payload.get("schema_mode", "")),
                    )
                )
                return
            if parsed.path == "/api/acquisition/select-folder":
                selected = select_capture_folder(
                    str(payload.get("initial_path", ""))
                )
                self._send_json(
                    {"selected": bool(selected), "path": selected}
                )
                return
            if parsed.path == "/api/acquisition/start":
                config = AcquisitionConfig(**payload)
                model_validation = self.dashboard.validate_prediction_setup(
                    config, load_model=True
                )
                result = self.dashboard.acquisition.start(config)
                result["prediction_model"] = model_validation
                self._send_json(result)
                return
            if parsed.path == "/api/acquisition/stop":
                self._send_json(self.dashboard.acquisition.stop())
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    dashboard = DashboardData()
    handler = type("ConfiguredAppHandler", (AppHandler,), {"dashboard": dashboard})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="AFP 状态预警可视化界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    url = f"http://{args.host}:{args.port}"
    print(f"AFP 状态预警可视化界面已启动：{url}")
    print("按 Ctrl+C 停止服务。")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
