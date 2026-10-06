"""Native training-center orchestration for the AFP desktop application."""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

try:
    from new_collection_health import PROCESS_COLUMNS, SENSOR_COLUMNS
except ImportError:  # source-module inspection fallback
    SENSOR_COLUMNS = ["温度", "压力", "ROI平均温度", "张力", "线速度", "ABB_X", "ABB_Y", "ABB_Z", *[f"温度{i}" for i in range(1, 9)], "转速", "位移", "振动"]
    PROCESS_COLUMNS = ["initial_compaction_force_N", "placement_speed_mm_s", "pid_angle_deg", "temperature_setpoint_C"]

from training_data import (
    ImportResult,
    guess_mapping,
    normalize_frame,
    read_excel_or_folder,
    read_mysql,
    validate_frame,
    write_manifest_dataset,
)


APP_DIR = Path(__file__).resolve().parent
DEMO_ROOT = APP_DIR / "new_collection_demo_v11_3"
TRAINING_CORE_DIR = APP_DIR.parent.parent / "final_training_packages" / "new_data_full_pipeline"
if getattr(sys, "frozen", False):
    _MEIPASS = Path(getattr(sys, "_MEIPASS"))
    _PACKAGED_TRAINING_CORE = _MEIPASS / "training_core"
    if _PACKAGED_TRAINING_CORE.exists():
        TRAINING_CORE_DIR = _PACKAGED_TRAINING_CORE


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    return value


class TrainingCenter:
    """Thread-safe native training workflow used by a future desktop panel."""

    def __init__(self, output_root: str | Path = "trained_models", callback: Callable[[dict], None] | None = None):
        self.output_root = Path(output_root).expanduser().resolve()
        self.callback = callback
        self.last_import: ImportResult | None = None
        self.last_frame: pd.DataFrame | None = None
        self.last_mapping: dict[str, str] = {}
        self.last_validation: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _emit(self, event: str, **payload: Any) -> None:
        message = {"event": event, **_jsonable(payload)}
        if self.callback:
            self.callback(message)

    def import_files(self, path: str | Path, mapping: dict[str, str] | None = None) -> dict[str, Any]:
        result = read_excel_or_folder(path)
        chosen = mapping or guess_mapping([str(c) for c in result.frame.columns])
        normalized = normalize_frame(result.frame, chosen)
        self.last_import, self.last_mapping, self.last_frame = result, chosen, normalized
        self.last_validation = validate_frame(normalized, SENSOR_COLUMNS, PROCESS_COLUMNS)
        self._emit("data_imported", source=result.source_description, rows=len(normalized), mapping=chosen, validation=self.last_validation, warnings=result.warnings)
        return {"source": result.source_description, "mapping": chosen, "validation": self.last_validation, "warnings": result.warnings}

    def import_mysql(self, connection: dict[str, Any], query: str, mapping: dict[str, str] | None = None) -> dict[str, Any]:
        result = read_mysql(connection, query)
        chosen = mapping or guess_mapping([str(c) for c in result.frame.columns])
        normalized = normalize_frame(result.frame, chosen)
        self.last_import, self.last_mapping, self.last_frame = result, chosen, normalized
        self.last_validation = validate_frame(normalized, SENSOR_COLUMNS, PROCESS_COLUMNS)
        self._emit("data_imported", source=result.source_description, rows=len(normalized), mapping=chosen, validation=self.last_validation, warnings=result.warnings)
        return {"source": result.source_description, "mapping": chosen, "validation": self.last_validation, "warnings": result.warnings}

    def validate(self) -> dict[str, Any]:
        if self.last_frame is None:
            raise RuntimeError("请先导入数据")
        self.last_validation = validate_frame(self.last_frame, SENSOR_COLUMNS, PROCESS_COLUMNS)
        self._emit("data_validated", validation=self.last_validation)
        return self.last_validation

    def prepare_dataset(self, output: str | Path | None = None) -> Path:
        if self.last_frame is None:
            raise RuntimeError("请先导入数据")
        validation = self.validate()
        missing = validation["missing_required"]
        if missing:
            raise ValueError(f"数据缺少训练字段：{missing}")
        root = write_manifest_dataset(self.last_frame, output or (self.output_root / "dataset"), SENSOR_COLUMNS, PROCESS_COLUMNS)
        self._emit("dataset_prepared", path=root, manifest=root / "manifest.csv")
        return root

    def start_training(self, dataset_root: str | Path, epochs: int = 100, patience: int = 10, batch_size: int = 64, learning_rate: float = 8e-4, device: str = "auto", output_root: str | Path | None = None) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("已有训练任务正在运行")
        self._stop.clear()
        if output_root is not None:
            self.output_root = Path(output_root).expanduser().resolve()
        self._thread = threading.Thread(target=self._run_training, args=(Path(dataset_root), epochs, patience, batch_size, learning_rate, device), daemon=True)
        self._thread.start()

    def stop_training(self) -> None:
        self._stop.set()
        self._emit("training_stop_requested")

    def _run_training(self, dataset_root: Path, epochs: int, patience: int, batch_size: int, learning_rate: float, device: str) -> None:
        started = time.time()
        try:
            # The packaged training core keeps the prediction runner at its
            # root and the health-model runner in ``core``.  Add both paths so
            # the same workflow works from source and from the frozen EXE.
            for _training_path in (TRAINING_CORE_DIR, TRAINING_CORE_DIR / "core"):
                if _training_path.exists() and str(_training_path) not in sys.path:
                    sys.path.insert(0, str(_training_path))
            if str(APP_DIR) not in sys.path:
                sys.path.insert(0, str(APP_DIR))
            self._emit("training_started", dataset_root=dataset_root, epochs=epochs, patience=patience)
            from train_predictor import TrainConfig, load_manifest, run as train_predictor
            manifest = load_manifest(dataset_root)
            self._emit("split_ready", counts=manifest.groupby("split")["specimen_id"].nunique().to_dict())
            if self._stop.is_set():
                return
            result = train_predictor(TrainConfig(
                data_root=dataset_root, epochs=epochs, patience=patience,
                batch_size=batch_size, learning_rate=learning_rate, device=device,
                stop_event=self._stop,
                progress_callback=lambda update: self._emit("epoch_progress", **update),
            ))
            if result.get("stopped"):
                self._emit("training_stopped", history=result.get("history", []))
                return
            self._emit("prediction_training_finished", result=result)
            checkpoint = Path(result["checkpoint"])
            health = self._fit_health_models(dataset_root, checkpoint)
            package = self._export_package(dataset_root, checkpoint, result, health)
            self._emit("training_finished", package=package, elapsed_seconds=round(time.time() - started, 2))
        except Exception as exc:
            self._emit("training_failed", error=str(exc))

    def _fit_health_models(self, dataset_root: Path, checkpoint: Path) -> dict[str, Any]:
        labels = pd.read_csv(dataset_root / "manifest.csv", encoding="utf-8-sig")["state_label"]
        if labels.nunique() < 2:
            raise ValueError(
                "预测模型可以使用单一状态数据训练，但健康指标预警模型至少需要正常和异常两类试样；"
                "请在 MySQL 查询中加入 state_label=1 的异常试样，并填写 abnormal_type。"
            )
        import fit_new_collection_health as health
        health.DATA_ROOT = dataset_root
        health.MODEL_DIR = dataset_root / "models"
        health.MANIFEST = dataset_root / "manifest.csv"
        health.CHECKPOINT = checkpoint
        health.METADATA = checkpoint.with_suffix(".pth.json")
        health.ARTIFACT = health.MODEL_DIR / "new_collection_hi_artifacts.joblib"
        health.METRICS_FILE = health.MODEL_DIR / "new_collection_hi_metrics.csv"
        health.CATALOG_FILE = health.MODEL_DIR / "new_collection_hi_catalog.csv"
        health.SUMMARY_FILE = health.MODEL_DIR / "new_collection_hi_summary.json"
        # The existing fitter reads module-level paths and writes the complete
        # 12-indicator x 4-classifier artifact used by the live warning engine.
        health.main()
        return json.loads(health.SUMMARY_FILE.read_text(encoding="utf-8"))

    def _export_package(self, dataset_root: Path, checkpoint: Path, prediction: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        package = self.output_root / stamp
        package.mkdir(parents=True, exist_ok=True)
        model_dir = dataset_root / "models"
        for path in (checkpoint, checkpoint.with_suffix(".pth.json"), model_dir / "new_collection_hi_artifacts.joblib", model_dir / "new_collection_hi_catalog.csv", model_dir / "new_collection_hi_metrics.csv", model_dir / "new_collection_hi_summary.json", model_dir / "prediction_metrics.json", model_dir / "training_history.csv"):
            if path.is_file():
                shutil.copy2(path, package / path.name)
        shutil.copy2(dataset_root / "manifest.csv", package / "split_manifest.csv")
        (package / "training_config.json").write_text(json.dumps({"prediction": prediction, "health_warning": health}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (package / "data_source_snapshot.json").write_text(json.dumps({"source": self.last_import.source_description if self.last_import else "unknown", "mapping": self.last_mapping, "validation": self.last_validation}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {"path": str(package), "files": sorted(p.name for p in package.iterdir())}


def create_demo_center(output_root: str | Path = "trained_models") -> TrainingCenter:
    return TrainingCenter(output_root)
