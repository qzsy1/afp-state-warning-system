from __future__ import annotations

import json
import importlib
import os
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np


APP_DIR = Path(__file__).resolve().parent
EXECUTABLE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else APP_DIR
)
# In the source tree this module lives four levels below the workspace root.
# PyInstaller places it below the extracted `_internal` directory, whose
# parent chain is much shorter.  Never index a fixed parent level here: doing
# so makes the frozen desktop application fail before the web server starts.
_SOURCE_PATH = Path(__file__).resolve()
_SOURCE_PARENTS = _SOURCE_PATH.parents
PROJECT_ROOT = (
    _SOURCE_PARENTS[4]
    if len(_SOURCE_PARENTS) > 4 and (_SOURCE_PARENTS[4] / "checkpoints").exists()
    else APP_DIR.parent
)
# The installed desktop application carries the I-ModernTCN implementation
# beside its runtime assets.  Retain the workspace location for development.
_PACKAGED_MODEL_RUNTIME = APP_DIR / "model_runtime"
XJU_ROOT = (
    _PACKAGED_MODEL_RUNTIME
    if (_PACKAGED_MODEL_RUNTIME / "shijie").exists()
    else Path(r"F:\program\XJUsorceopen")
)
# The comparison models in the thesis live in the main project tree, while
# I-ModernTCN lives in XJUsorceopen (or in the packaged model_runtime folder).
# Keep all candidate roots available so selecting TCN/Transformer/FNN does not
# accidentally import a package from a different Python installation.
MODEL_IMPORT_ROOTS = []
for _root in (XJU_ROOT, PROJECT_ROOT, PROJECT_ROOT.parent):
    _root = Path(_root)
    if _root.exists() and _root not in MODEL_IMPORT_ROOTS:
        MODEL_IMPORT_ROOTS.append(_root)
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

# Models implemented in the original thesis code base.  The registry is kept
# independent from the warning/health-indicator code: a forecast model only
# needs to satisfy the common (history, horizon) -> future sequence contract.
MODEL_REGISTRY = {
    "i_T_G": {
        "label": "I-ModernTCN-GAT（默认）",
        "architecture": "I-ModernTCN",
        "module": "shijie.model_mine.I_modernTCN_GAT_abalation",
        "class_name": "Model",
        "aliases": ["I-ModernTCN", "i_T_G"],
    },
    "TCN": {"label": "TCN", "architecture": "TCN", "module": "models.TCN", "class_name": "Model", "aliases": ["TCN"]},
    "Transformer": {"label": "Transformer", "architecture": "Transformer", "module": "models.Transformer", "class_name": "Model", "aliases": ["Transformer"]},
    "Informer": {"label": "Informer", "architecture": "Informer", "module": "models.Informer", "class_name": "Model", "aliases": ["Informer"]},
    "DeepVAR": {"label": "DeepVAR", "architecture": "DeepVAR", "module": "models.DeepVAR", "class_name": "Model", "aliases": ["DeepVAR"]},
    "DLinear": {"label": "DLinear", "architecture": "DLinear", "module": "models.DLinear", "class_name": "Model", "aliases": ["DLinear"]},
    "NLinear": {"label": "NLinear", "architecture": "NLinear", "module": "models.NLinear", "class_name": "Model", "aliases": ["NLinear"]},
    "Linear": {"label": "Linear", "architecture": "Linear", "module": "models.Linear", "class_name": "Model", "aliases": ["Linear"]},
    "MLP": {"label": "MLP", "architecture": "MLP", "module": "models.MLP", "class_name": "Model", "aliases": ["MLP"]},
    "NHITS": {"label": "N-HiTS（当前版本仅作离线对比）", "architecture": "NHITS", "module": "models.NHITS", "class_name": "Model", "aliases": ["NHITS", "N-HiTS"], "training_only": True},
    "FNN_2024": {"label": "FNN 2024", "architecture": "FNN_2024", "module": "models.FNN", "class_name": "FNN", "aliases": ["FNN_2024", "FNN"]},
    "FNN_2025_Base": {"label": "FNN 2025 Base", "architecture": "FNN_2025_Base", "module": "models.FNN", "class_name": "TgNN_Base_Concordia", "aliases": ["FNN_2025_Base"]},
    "GBRT": {"label": "GBRT（需专用模型文件）", "architecture": "GBRT", "module": "models.GBRT", "class_name": "Model", "aliases": ["GBRT"], "training_only": True},
}
# The thesis comparison set does not contain the previously prototyped NHITS
# and GBRT entries.  Keep their implementation files available for training
# experiments, but do not expose them as selectable runtime algorithms.
MODEL_REGISTRY = {
    key: value for key, value in MODEL_REGISTRY.items()
    if key not in {"NHITS", "GBRT"}
}
MODEL_TYPE_ALIASES = {
    alias.lower(): model_type
    for model_type, definition in MODEL_REGISTRY.items()
    for alias in definition.get("aliases", [])
}


def normalize_model_type(value: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return "i_T_G"
    return MODEL_TYPE_ALIASES.get(text.lower(), text if text in MODEL_REGISTRY else "i_T_G")


def model_catalog() -> list[dict]:
    """Return a UI-safe model list without exposing local checkpoint paths."""
    return [
        {
            "id": model_type,
            "label": definition["label"],
            "architecture": definition["architecture"],
            "training_only": bool(definition.get("training_only", False)),
            "default_epochs": 100,
            "default_patience": 10,
        }
        for model_type, definition in MODEL_REGISTRY.items()
    ]


def _schema_key(schema_mode: str = "") -> str:
    return "new" if str(schema_mode or "").strip() == "new_collection_v11_3" else "legacy"


def _selection_history_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AFP_State_Warning_System"
    root.mkdir(parents=True, exist_ok=True)
    return root / "model_selection_history.json"


def _read_selection_history() -> dict:
    path = _selection_history_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _remember_model_selection(model_type: str, schema_mode: str, checkpoint: Path) -> None:
    history_path = _selection_history_path()
    history = _read_selection_history()
    history[f"{_schema_key(schema_mode)}::{normalize_model_type(model_type)}"] = str(checkpoint)
    try:
        history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _first_existing(paths) -> Path | None:
    for candidate in paths:
        candidate = Path(candidate)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _is_builtin_checkpoint_path(path: Path) -> bool:
    """Identify paths from an older bundled/source-tree installation.

    A persisted selection from a previous build must not shadow the matching
    checkpoint that is now shipped beside the EXE.  User-selected checkpoints
    outside these known application locations remain respected.
    """
    normalized = str(path).replace("\\", "/").lower()
    return any(marker in normalized for marker in (
        "/models/legacy/",
        "/models/new/",
        "/_internal/models/",
        "/checkpoints/test_",
        "/new_collection_demo_v11_3/models/",
    ))


def _registered_checkpoint(model_type: str, schema_mode: str = "") -> Path | None:
    model_type = normalize_model_type(model_type)
    scope = _schema_key(schema_mode)
    history = _read_selection_history()
    remembered = history.get(f"{scope}::{model_type}")
    if remembered:
        remembered_path = Path(str(remembered)).expanduser()
        if (remembered_path.exists() and remembered_path.is_file()
                and not _is_builtin_checkpoint_path(remembered_path)):
            return remembered_path

    # A model trained by the web/native training page is preferred over a
    # stale source-tree checkpoint, and is kept separate for old/new schemas.
    # In a frozen desktop build the authoritative user-editable weights are
    # beside the EXE.  Search that directory first; the extracted _internal
    # copy remains a fallback for older packages.
    app_roots = list(dict.fromkeys(
        ([EXECUTABLE_DIR, APP_DIR] if getattr(sys, "frozen", False)
         else [APP_DIR, EXECUTABLE_DIR])
    ))
    trained_roots = [
        root / "trained_models_web" for root in app_roots
    ] + [
        root / "trained_models" for root in app_roots
    ] + [
        root / "models" for root in app_roots
    ]
    if scope == "new":
        new_roots = [root / "new_collection_demo_v11_3" / "models" for root in app_roots]
        candidates = []
        if model_type == "i_T_G":
            candidates.extend([
                root / "i_modern_tcn_new_collection_final.pth"
                for root in new_roots
            ])
            candidates.extend([
                root / "i_modern_tcn_new_collection_v11_3.pth"
                for root in new_roots
            ])
        candidates.extend([
            root / "models" / "new" / model_type / "checkpoint.pth"
            for root in app_roots
        ])
        for root in trained_roots:
            if root.exists():
                candidates.extend(root.glob(f"**/{model_type}*.pth"))
                if model_type == "i_T_G":
                    candidates.extend(root.glob("**/i_modern_tcn*.pth"))
        return _first_existing(candidates)

    if model_type == "i_T_G":
        return _first_existing([
            root / "models" / "legacy" / model_type / "checkpoint.pth"
            for root in app_roots
        ] + [DEFAULT_CHECKPOINT])
    packaged = _first_existing([
        root / "models" / "legacy" / model_type / "checkpoint.pth"
        for root in app_roots
    ] + [
        root / "models" / model_type / "checkpoint.pth" for root in app_roots
    ])
    if packaged is not None:
        return packaged
    patterns = {
        "TCN": "test_TCN_*sl24_ll24_pl24*",
        "Transformer": "test_Transformer_*sl24_ll24_pl24*",
        "FNN_2024": "test_FNN_2024_*sl24_ll24_pl24*",
        "FNN_2025_Base": "test_FNN_2025_Base_*sl24_ll24_pl24*",
        "Informer": "test_Informer_*sl24*pl24*",
        "DeepVAR": "test_DeepVAR_*sl24*pl24*",
        "DLinear": "test_DLinear_*sl24*pl24*",
        "NLinear": "test_NLinear_*sl24*pl24*",
        "Linear": "test_Linear_*sl24*pl24*",
        "MLP": "test_MLP_*sl24*pl24*",
    }
    pattern = patterns.get(model_type)
    checkpoints_root = PROJECT_ROOT / "checkpoints"
    if pattern and checkpoints_root.exists():
        candidates = sorted(checkpoints_root.glob(f"{pattern}/checkpoint.pth"))
        return candidates[0] if candidates else None
    return None


def _infer_model_type(path: Path, architecture: str = "") -> str:
    explicit = normalize_model_type(architecture)
    if architecture:
        return explicit
    text = path.as_posix().lower()
    for model_type, definition in MODEL_REGISTRY.items():
        if model_type.lower() in text or any(alias.lower() in text for alias in definition.get("aliases", [])):
            return model_type
    return "i_T_G"


def _builtin_model_metadata(model_type: str, checkpoint: Path) -> dict:
    model_type = normalize_model_type(model_type)
    definition = MODEL_REGISTRY[model_type]
    return {
        **DEFAULT_MODEL_METADATA,
        "name": definition["label"],
        "architecture": definition["architecture"],
        "model_type": model_type,
        "model_module": definition["module"],
        "model_class": definition["class_name"],
        "checkpoint": str(checkpoint),
    }


def _metadata_candidates(checkpoint: Path) -> list[Path]:
    return [
        checkpoint.with_suffix(checkpoint.suffix + ".json"),
        checkpoint.with_suffix(".json"),
        checkpoint.parent / "model_metadata.json",
    ]


def inspect_prediction_model(
    checkpoint: str | Path = "", architecture: str = "", model_type: str = "",
    schema_mode: str = "",
) -> dict:
    selected_type = normalize_model_type(model_type or architecture)
    if not str(checkpoint).strip():
        registered = _registered_checkpoint(selected_type, schema_mode)
        if registered is None:
            raise FileNotFoundError(
                f"算法 {MODEL_REGISTRY[selected_type]['label']} 当前没有匹配的已训练权重。"
                "请在‘已训练预测模型文件’中选择该算法的 checkpoint.pth。"
            )
        checkpoint = registered
    path = Path(checkpoint).expanduser() if str(checkpoint).strip() else DEFAULT_CHECKPOINT
    path = path.resolve()
    if str(checkpoint).strip() and selected_type == "i_T_G":
        inferred_type = _infer_model_type(path)
        if inferred_type != "i_T_G":
            selected_type = inferred_type
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"预测模型文件不存在：{path}")
    metadata_path = next(
        (candidate for candidate in _metadata_candidates(path) if candidate.exists()),
        None,
    )
    if metadata_path is None:
        registered_paths = {
            candidate.resolve() for candidate in (
                _registered_checkpoint(selected_type, schema_mode), DEFAULT_CHECKPOINT
            ) if candidate is not None and Path(candidate).exists()
        }
        if path not in registered_paths and not (model_type or architecture):
            raise ValueError(
                "自定义预测模型缺少元数据文件。请在模型同目录提供"
                " checkpoint.pth.json、checkpoint.json 或 model_metadata.json。"
            )
        metadata = dict(DEFAULT_MODEL_METADATA)
        metadata_source = "built_in_default"
    else:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata_source = str(metadata_path)
    if metadata_source == "built_in_default" and (model_type or architecture):
        # The default metadata describes I-ModernTCN.  When a registered
        # comparison checkpoint is selected, it must not overwrite the user's
        # explicit algorithm choice during this fallback path.
        metadata["model_type"] = selected_type
        metadata["architecture"] = MODEL_REGISTRY[selected_type]["architecture"]
    selected_type = normalize_model_type(
        metadata.get("model_type")
        or metadata.get("architecture")
        or model_type
        or architecture
    )
    definition = MODEL_REGISTRY.get(selected_type, MODEL_REGISTRY["i_T_G"])
    if metadata_source == "built_in_default":
        metadata["architecture"] = definition["architecture"]
    metadata.setdefault("model_type", selected_type)
    metadata.setdefault("model_module", definition["module"])
    metadata.setdefault("model_class", definition["class_name"])
    metadata.setdefault("name", definition["label"])
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
    if False and str(metadata["architecture"]) != "I-ModernTCN":
        raise ValueError("当前在线运行器仅支持 architecture=I-ModernTCN")
    enc_in = int(metadata["enc_in"])
    if (
        enc_in < 1
        or int(metadata["seq_len"]) < 1
        or int(metadata["pred_len"]) < 1
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
    result = {
        **metadata,
        "model_type": selected_type,
        "model_label": definition["label"],
        "supports_live": not bool(definition.get("training_only", False)),
        "checkpoint": str(path),
        "metadata_source": metadata_source,
        "input_sensors": input_sensors,
        "output_sensors": output_sensors,
        "model_columns": model_columns,
        "scaler_mean": scaler_mean,
        "scaler_scale": scaler_scale,
        "uses_dashboard_scaler": uses_dashboard_scaler,
        "schema_mode": schema_mode or "legacy_original",
    }
    _remember_model_selection(selected_type, schema_mode, path)
    return result


class OnlineIModernTCN:
    """Lazy, thread-safe I-ModernTCN inference for the dashboard."""

    def __init__(self, checkpoint: str | Path = "", model_type: str = "", schema_mode: str = "") -> None:
        self._lock = threading.Lock()
        self._model = None
        self._torch = None
        self._device = None
        self._profile = inspect_prediction_model(checkpoint, model_type=model_type, schema_mode=schema_mode)

    @property
    def checkpoint(self) -> str:
        return str(self._profile["checkpoint"])

    @property
    def profile(self) -> dict:
        return dict(self._profile)

    def configure(
        self,
        checkpoint: str | Path = "",
        model_type: str = "",
        architecture: str = "",
        schema_mode: str = "",
    ) -> dict:
        profile = inspect_prediction_model(
            checkpoint, architecture=architecture, model_type=model_type, schema_mode=schema_mode
        )
        with self._lock:
            if (
                profile["checkpoint"] != self._profile["checkpoint"]
                or profile.get("model_type") != self._profile.get("model_type")
            ):
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
            raise FileNotFoundError(f"预测模型文件不存在: {checkpoint}")
        if not self._profile.get("supports_live", True):
            raise ValueError("该算法需要专用的 GBRT 模型文件，当前检查点不能直接在线推理")
        for import_root in reversed(MODEL_IMPORT_ROOTS):
            if str(import_root) not in sys.path:
                sys.path.insert(0, str(import_root))
        # I-ModernTCN imports a top-level ``models`` package from its own
        # modern_TCN_models directory.  The thesis baselines also use the name
        # ``models`` but refer to the project-root package; clear the former
        # namespace before importing a baseline in the same process.
        if self._profile.get("model_type") != "i_T_G":
            for module_name in list(sys.modules):
                if (
                    module_name == "models" or module_name.startswith("models.")
                    or module_name == "layers" or module_name.startswith("layers.")
                    or module_name == "utilsaa" or module_name.startswith("utilsaa.")
                ):
                    del sys.modules[module_name]
            sys.path[:] = [
                item for item in sys.path
                if Path(item).name.lower() != "modern_tcn_models"
            ]
            project_root = str(PROJECT_ROOT)
            if project_root in sys.path:
                sys.path.remove(project_root)
            sys.path.insert(0, project_root)
        # Keep the local HTTP service deterministic and avoid contending with
        # the training process for GPU memory. The imported model module selects
        # its construction device at import time.
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        # scikit-learn is loaded by the anomaly service before PyTorch. Allow
        # both runtimes to coexist in the same Windows process.
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        import torch

        device = torch.device("cpu")
        model_module = importlib.import_module(str(self._profile["model_module"]))
        Model = getattr(model_module, str(self._profile["model_class"]))
        config = SimpleNamespace(
            enc_in=int(self._profile["enc_in"]),
            c_out=int(self._profile.get("c_out", self._profile["enc_in"])),
            dec_in=int(self._profile.get("dec_in", self._profile["enc_in"])),
            seq_len=int(self._profile["seq_len"]),
            label_len=int(self._profile.get("label_len", self._profile["seq_len"])),
            pred_len=int(self._profile["pred_len"]),
            dropout=float(self._profile.get("dropout", 0.05)),
            d_model=int(self._profile.get("d_model", 128)),
            n_heads=int(self._profile.get("n_heads", 8)),
            e_layers=int(self._profile.get("e_layers", 2)),
            d_layers=int(self._profile.get("d_layers", 1)),
            d_ff=int(self._profile.get("d_ff", 2048)),
            factor=int(self._profile.get("factor", 1)),
            embed=str(self._profile.get("embed", "timeF")),
            freq=str(self._profile.get("freq", "h")),
            activation=str(self._profile.get("activation", "gelu")),
            output_attention=False,
            distil=bool(self._profile.get("distil", True)),
            individual=bool(self._profile.get("individual", False)),
            flat_input=bool(self._profile.get("flat_input", False)),
            low_rank=bool(self._profile.get("low_rank", False)),
            rank_ratio=int(self._profile.get("rank_ratio", 4)),
            version=int(self._profile.get("version", 1)),
            model=str(self._profile.get("model_type", "")),
        )
        if hasattr(model_module, "device"):
            model_module.device = device
        model = Model(config).to(device)
        state = torch.load(checkpoint, map_location="cpu", weights_only=False)
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        model.load_state_dict(state)
        model.eval()
        self._torch = torch
        self._device = device
        self._model = model

    def _forward_tensor(self, x):
        """Run any registered thesis model and normalize its output shape."""
        torch = self._torch
        batch_size = int(x.shape[0])
        seq_len = int(self._profile["seq_len"])
        pred_len = int(self._profile["pred_len"])
        atavn = self._profile.get("atavn") or {}
        atavn_external = bool(atavn.get("enabled", False)) and str(atavn.get("mode", "external")) == "external"
        if atavn_external:
            from atavn import normalize as atavn_normalize, restore as atavn_restore
            model_input, terminal, scale = atavn_normalize(x, eps=float(atavn.get("epsilon", 1.0e-6)))
        else:
            model_input, terminal, scale = x, None, None
        x_mark = torch.zeros((batch_size, seq_len, 4), device=self._device)
        decoder = torch.zeros(
            (batch_size, seq_len + pred_len, int(self._profile["enc_in"])),
            device=self._device,
        )
        y_mark = torch.zeros((batch_size, seq_len + pred_len, 4), device=self._device)
        output = self._model(model_input, x_mark, decoder, y_mark)
        if isinstance(output, (tuple, list)):
            output = output[0]
        if not hasattr(output, "ndim") or output.ndim != 3:
            raise ValueError(f"模型输出必须为三维序列，实际为 {getattr(output, 'shape', None)}")
        if output.shape[-1] != int(self._profile["enc_in"]):
            raise ValueError(
                f"模型输出通道数 {output.shape[-1]} 与 enc_in={self._profile['enc_in']} 不一致"
            )
        if output.shape[1] < pred_len:
            output = torch.cat(
                [output, output[:, -1:, :].repeat(1, pred_len - output.shape[1], 1)],
                dim=1,
            )
        if atavn_external:
            output = atavn_restore(output, terminal, scale)
        return output[:, -pred_len:, :]

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
                    prediction = self._forward_tensor(x).detach().cpu().numpy()[0].astype(np.float32)
                    take = min(remaining, int(self._profile["pred_len"]))
                    outputs.append(prediction[:take])
                    remaining -= take
                    rolling = np.concatenate([rolling, prediction], axis=0)[-int(self._profile["seq_len"]):]
            mode = f"{self._profile['model_type']}_direct" if horizon <= int(self._profile["pred_len"]) else f"{self._profile['model_type']}_recursive"
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
        horizon = int(np.clip(horizon, 1, int(self._profile["pred_len"])))
        if len(histories) == 0:
            return np.empty((0, horizon, required_tail[1]), dtype=np.float32), "batch_empty"
        with self._lock:
            self._load()
            torch = self._torch
            batch_size = int(histories.shape[0])
            with torch.no_grad():
                x = torch.from_numpy(histories).to(self._device)
                prediction = self._forward_tensor(x).detach().cpu().numpy().astype(np.float32)
            return prediction[:, :horizon], f"{self._profile['model_type']}_batch_direct"
