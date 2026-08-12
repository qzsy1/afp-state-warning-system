from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np


APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parents[4]
# The installed desktop application carries the I-ModernTCN implementation
# beside its runtime assets.  Retain the workspace location for development.
_PACKAGED_MODEL_RUNTIME = APP_DIR / "model_runtime"
XJU_ROOT = (
    _PACKAGED_MODEL_RUNTIME
    if (_PACKAGED_MODEL_RUNTIME / "shijie").exists()
    else Path(r"F:\program\XJUsorceopen")
)
_WORKSPACE_DEFAULT_CHECKPOINT = (
    PROJECT_ROOT
    / "checkpoints"
    / (
        "health_i_T_G_MyCustom_ftM_sl24_ll24_pl24_dm128_nh8_el2_dl1_"
        "df2048_fc1_ebtimeF_dtTrue_health_v9_conditional_normal_no_param_"
        "score_lr0.001_bs128"
    )
    / "checkpoint.pth"
)
_PACKAGED_DEFAULT_CHECKPOINT = APP_DIR / "models" / "checkpoint.pth"
DEFAULT_CHECKPOINT = (
    _PACKAGED_DEFAULT_CHECKPOINT
    if _PACKAGED_DEFAULT_CHECKPOINT.exists()
    else _WORKSPACE_DEFAULT_CHECKPOINT
)
CHECKPOINT = DEFAULT_CHECKPOINT

MODEL_SENSOR_COLUMNS = [
    "转速",
    "位移",
    *[f"温度{index}" for index in range(1, 9)],
    "压力",
    "振动",
]
NEW_MODEL_SENSOR_COLUMNS = [
    "温度",
    "压力",
    "ROI平均温度",
    "张力",
    "线速度",
    "ABB_X",
    "ABB_Y",
    "ABB_Z",
    *[f"温度{index}" for index in range(1, 9)],
    "转速",
    "位移",
    "振动",
]
SUPPORTED_SENSOR_COLUMNS = list(
    dict.fromkeys([*MODEL_SENSOR_COLUMNS, *NEW_MODEL_SENSOR_COLUMNS])
)
DEFAULT_MODEL_METADATA = {
    "name": "I-ModernTCN AFP 24→24（默认模型）",
    "architecture": "I-ModernTCN",
    "enc_in": 17,
    "seq_len": 24,
    "pred_len": 24,
    "input_sensors": MODEL_SENSOR_COLUMNS,
    "output_sensors": MODEL_SENSOR_COLUMNS,
    "model_columns": [
        "转速",
        "位移",
        *[f"温度{index}" for index in range(1, 9)],
        "压力",
        "cycle",
        "v",
        "p",
        "pr",
        "l",
        "振动",
    ],
    "normalization": "dashboard_sequences.npz",
}


def _metadata_candidates(checkpoint: Path) -> list[Path]:
    return [
        checkpoint.with_suffix(checkpoint.suffix + ".json"),
        checkpoint.with_suffix(".json"),
        checkpoint.parent / "model_metadata.json",
    ]


def inspect_prediction_model(checkpoint: str | Path = "") -> dict:
    path = Path(checkpoint).expanduser() if str(checkpoint).strip() else DEFAULT_CHECKPOINT
    path = path.resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"预测模型文件不存在：{path}")
    metadata_path = next(
        (candidate for candidate in _metadata_candidates(path) if candidate.exists()),
        None,
    )
    if metadata_path is None:
        if path != DEFAULT_CHECKPOINT.resolve():
            raise ValueError(
                "自定义预测模型缺少元数据文件。请在模型同目录提供"
                " checkpoint.pth.json、checkpoint.json 或 model_metadata.json。"
            )
        metadata = dict(DEFAULT_MODEL_METADATA)
        metadata_source = "built_in_default"
    else:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata_source = str(metadata_path)
    required = {
        "architecture",
        "enc_in",
        "seq_len",
        "pred_len",
        "input_sensors",
        "output_sensors",
        "model_columns",
    }
    missing = sorted(required.difference(metadata))
    if missing:
        raise ValueError(f"预测模型元数据缺少字段：{missing}")
    if str(metadata["architecture"]) != "I-ModernTCN":
        raise ValueError("当前在线运行器仅支持 architecture=I-ModernTCN")
    enc_in = int(metadata["enc_in"])
    if (
        enc_in < 1
        or int(metadata["seq_len"]) != 24
        or int(metadata["pred_len"]) != 24
    ):
        raise ValueError(
            "当前预警系统要求seq_len=24、pred_len=24，且enc_in必须为正整数"
        )
    input_sensors = [str(name) for name in metadata["input_sensors"]]
    output_sensors = [str(name) for name in metadata["output_sensors"]]
    model_columns = [str(name) for name in metadata["model_columns"]]
    if len(model_columns) != enc_in or len(set(model_columns)) != enc_in:
        raise ValueError(
            f"model_columns必须包含enc_in={enc_in}个不重复的模型列"
        )
    unknown_inputs = sorted(
        set(input_sensors).difference(SUPPORTED_SENSOR_COLUMNS)
    )
    unknown_outputs = sorted(
        set(output_sensors).difference(SUPPORTED_SENSOR_COLUMNS)
    )
    if unknown_inputs or unknown_outputs:
        raise ValueError(
            f"模型元数据包含未知传感器：输入{unknown_inputs}，输出{unknown_outputs}"
        )
    if not set(output_sensors).issubset(input_sensors):
        raise ValueError("预测模型输出传感器必须同时属于模型输入传感器")
    missing_input_columns = [
        name for name in input_sensors if name not in model_columns
    ]
    missing_output_columns = [
        name for name in output_sensors if name not in model_columns
    ]
    if missing_input_columns or missing_output_columns:
        raise ValueError(
            "模型输入/输出传感器必须出现在model_columns中："
            f"输入缺少{missing_input_columns}，输出缺少{missing_output_columns}"
        )
    scaler_mean = metadata.get("scaler_mean")
    scaler_scale = metadata.get("scaler_scale")
    scaler_file = metadata.get("scaler_file")
    uses_dashboard_scaler = (
        str(metadata.get("normalization", "")) == "dashboard_sequences.npz"
    )
    if scaler_file:
        scaler_path = Path(str(scaler_file))
        if not scaler_path.is_absolute():
            scaler_path = (
                (metadata_path.parent if metadata_path else path.parent)
                / scaler_path
            )
        if not scaler_path.exists():
            raise FileNotFoundError(f"模型标准化文件不存在：{scaler_path}")
        arrays = np.load(scaler_path, allow_pickle=False)
        scaler_mean = arrays["scaler_mean"].astype(float).tolist()
        scaler_scale = arrays["scaler_scale"].astype(float).tolist()
    if scaler_mean is not None or scaler_scale is not None:
        if scaler_mean is None or scaler_scale is None:
            raise ValueError("scaler_mean和scaler_scale必须同时提供")
        if len(scaler_mean) != enc_in or len(scaler_scale) != enc_in:
            raise ValueError("模型标准化参数长度必须等于enc_in")
        if any(float(value) == 0.0 for value in scaler_scale):
            raise ValueError("scaler_scale不能包含0")
    elif not uses_dashboard_scaler:
        raise ValueError(
            "自定义模型元数据必须提供scaler_mean/scaler_scale、"
            "scaler_file，或声明normalization=dashboard_sequences.npz"
        )
    if (
        uses_dashboard_scaler
        and model_columns != DEFAULT_MODEL_METADATA["model_columns"]
    ):
        raise ValueError(
            "使用dashboard_sequences.npz标准化器时，model_columns必须"
            "与默认17列顺序完全一致"
        )
    return {
        **metadata,
        "checkpoint": str(path),
        "metadata_source": metadata_source,
        "input_sensors": input_sensors,
        "output_sensors": output_sensors,
        "model_columns": model_columns,
        "scaler_mean": scaler_mean,
        "scaler_scale": scaler_scale,
        "uses_dashboard_scaler": uses_dashboard_scaler,
    }


class OnlineIModernTCN:
    """Lazy, thread-safe I-ModernTCN inference for the dashboard."""

    def __init__(self, checkpoint: str | Path = "") -> None:
        self._lock = threading.Lock()
        self._model = None
        self._torch = None
        self._device = None
        self._profile = inspect_prediction_model(checkpoint)

    @property
    def checkpoint(self) -> str:
        return str(self._profile["checkpoint"])

    @property
    def profile(self) -> dict:
        return dict(self._profile)

    def configure(self, checkpoint: str | Path = "") -> dict:
        profile = inspect_prediction_model(checkpoint)
        with self._lock:
            if profile["checkpoint"] != self._profile["checkpoint"]:
                self._profile = profile
                self._model = None
                self._torch = None
                self._device = None
            else:
                self._profile = profile
            self._load()
        return dict(self._profile)

    def warmup(self) -> None:
        with self._lock:
            self._load()

    def _load(self) -> None:
        if self._model is not None:
            return
        checkpoint = Path(self._profile["checkpoint"])
        if not checkpoint.exists():
            raise FileNotFoundError(f"I-ModernTCN checkpoint not found: {checkpoint}")
        if str(XJU_ROOT) not in sys.path:
            sys.path.insert(0, str(XJU_ROOT))
        # Keep the local HTTP service deterministic and avoid contending with
        # the training process for GPU memory. The imported model module selects
        # its construction device at import time.
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        # scikit-learn is loaded by the anomaly service before PyTorch. Allow
        # both runtimes to coexist in the same Windows process.
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        import torch
        import shijie.model_mine.I_modernTCN_GAT_abalation as model_module

        device = torch.device("cpu")
        model_module.device = device
        Model = model_module.Model
        config = SimpleNamespace(
            enc_in=int(self._profile["enc_in"]),
            seq_len=int(self._profile["seq_len"]),
            pred_len=int(self._profile["pred_len"]),
            dropout=float(self._profile.get("dropout", 0.05)),
        )
        model = Model(config).to(device)
        state = torch.load(checkpoint, map_location="cpu", weights_only=False)
        model.load_state_dict(state)
        model.eval()
        self._torch = torch
        self._device = device
        self._model = model

    def predict(self, history: np.ndarray, horizon: int) -> tuple[np.ndarray, str]:
        history = np.asarray(history, dtype=np.float32)
        required_shape = (
            int(self._profile["seq_len"]),
            int(self._profile["enc_in"]),
        )
        if history.shape != required_shape:
            raise ValueError(
                f"online model requires {required_shape}, got {history.shape}"
            )
        horizon = int(np.clip(horizon, 1, 600))
        with self._lock:
            self._load()
            torch = self._torch
            rolling = history.copy()
            outputs: list[np.ndarray] = []
            remaining = horizon
            with torch.no_grad():
                while remaining > 0:
                    x = torch.from_numpy(rolling[None, ...]).to(self._device)
                    x_mark = torch.zeros((1, 24, 4), device=self._device)
                    decoder = torch.zeros(
                        (
                            1,
                            int(self._profile["seq_len"])
                            + int(self._profile["pred_len"]),
                            int(self._profile["enc_in"]),
                        ),
                        device=self._device,
                    )
                    y_mark = torch.zeros((1, 48, 4), device=self._device)
                    prediction = (
                        self._model(x, x_mark, decoder, y_mark)
                        .detach()
                        .cpu()
                        .numpy()[0]
                        .astype(np.float32)
                    )
                    take = min(remaining, 24)
                    outputs.append(prediction[:take])
                    remaining -= take
                    rolling = np.concatenate([rolling, prediction], axis=0)[-24:]
            mode = "live_checkpoint_direct_24" if horizon <= 24 else "live_checkpoint_recursive"
            return np.concatenate(outputs, axis=0), mode

    def predict_batch(
        self, histories: np.ndarray, horizon: int = 24
    ) -> tuple[np.ndarray, str]:
        """Predict a batch of causal origins in one forward pass.

        Replay alignment needs the prediction made at many historical origins.
        Calling ``predict`` once per point makes the browser appear stalled and
        also encourages callers to reuse a prediction from the wrong origin.
        The fixed-lead alignment used by the dashboard is within the native
        24-point checkpoint horizon, so a batched direct pass is both causal
        and substantially faster.
        """
        histories = np.asarray(histories, dtype=np.float32)
        required_tail = (
            int(self._profile["seq_len"]),
            int(self._profile["enc_in"]),
        )
        if histories.ndim != 3 or tuple(histories.shape[1:]) != required_tail:
            raise ValueError(
                f"online model requires a batch shaped (N,{required_tail[0]},{required_tail[1]}), "
                f"got {histories.shape}"
            )
        horizon = int(np.clip(horizon, 1, 24))
        if len(histories) == 0:
            return np.empty((0, horizon, required_tail[1]), dtype=np.float32), "batch_empty"
        with self._lock:
            self._load()
            torch = self._torch
            batch_size = int(histories.shape[0])
            with torch.no_grad():
                x = torch.from_numpy(histories).to(self._device)
                x_mark = torch.zeros((batch_size, 24, 4), device=self._device)
                decoder = torch.zeros(
                    (
                        batch_size,
                        int(self._profile["seq_len"])
                        + int(self._profile["pred_len"]),
                        int(self._profile["enc_in"]),
                    ),
                    device=self._device,
                )
                y_mark = torch.zeros((batch_size, 48, 4), device=self._device)
                prediction = (
                    self._model(x, x_mark, decoder, y_mark)
                    .detach()
                    .cpu()
                    .numpy()
                    .astype(np.float32)
                )
            return prediction[:, :horizon], "live_checkpoint_batch_direct_24"
