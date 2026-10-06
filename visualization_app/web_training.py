"""Thread-safe browser adapter for unified CSV/MySQL model training."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from web_training_pipeline import (
    UnifiedData,
    default_columns,
    default_mysql_query,
    import_unified,
    train_models,
)


class WebTrainingManager:
    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root).resolve()
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._event_seq = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._data: UnifiedData | None = None
        self._source: dict[str, Any] | None = None
        self._options: dict[str, Any] | None = None
        self._progress: dict[str, Any] = {
            "stage": "idle", "epoch": 0, "epochs": 0,
            "bad_epochs": 0, "patience": 0,
        }
        self._last_result: dict[str, Any] | None = None
        self._last_error = ""

    def defaults(self, mode: str = "new") -> dict[str, Any]:
        columns = default_columns("legacy" if mode == "legacy" else "new")
        return {
            **{key: ",".join(value) for key, value in columns.items()},
            "mysql_query": default_mysql_query(mode),
            "output_root": str(self.output_root),
        }

    def _emit(self, event: str, **payload: Any) -> None:
        with self._lock:
            self._event_seq += 1
            item = {
                "seq": self._event_seq,
                "time": time.strftime("%H:%M:%S"),
                "event": event,
                **payload,
            }
            self._events.append(item)
            self._events = self._events[-300:]
            if event == "training_started":
                self._progress.update({
                    "stage": "prediction_training", "epoch": 0,
                    "epochs": int(payload.get("epochs", 0)),
                    "bad_epochs": 0,
                    "patience": int(payload.get("patience", 0)),
                    "task_dir": payload.get("task_dir", ""),
                    "model_type": payload.get("model_type", "i_T_G"),
                    "model_label": payload.get("model_label", ""),
                })
            elif event == "epoch_progress":
                self._progress.update({
                    "stage": "prediction_training",
                    "epoch": int(payload.get("epoch", 0)),
                    "epochs": int(payload.get("epochs", 0)),
                    "bad_epochs": int(payload.get("bad_epochs", 0)),
                    "patience": int(payload.get("patience", 0)),
                    "train_loss": payload.get("train_loss"),
                    "validation_loss": payload.get("validation_loss"),
                    "best_validation_loss": payload.get("best_validation_loss"),
                })
            elif event == "warning_progress":
                self._progress.update({
                    "stage": "warning_training",
                    "warning_current": int(payload.get("current", 0)),
                    "warning_total": int(payload.get("total", 0)),
                    "warning_model": payload.get("model", ""),
                })

    def import_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.running:
            raise RuntimeError("训练正在运行，不能重新导入数据")
        source_kind = str(payload.get("source", "csv"))
        source = {
            "kind": source_kind,
            "path": str(payload.get("path", "")),
            "connection": payload.get("mysql", {}),
            "query": str(payload.get("query", "")) or default_mysql_query(
                str(payload.get("data_mode", "new"))
            ),
        }
        options = {
            "data_mode": str(payload.get("data_mode", "new")),
            "condition_columns": payload.get("condition_columns", ""),
            "input_columns": payload.get("input_columns", ""),
            "output_columns": payload.get("output_columns", ""),
            "history_length": int(payload.get("history_length", 24)),
            "prediction_length": int(payload.get("prediction_length", 24)),
            "stride": int(payload.get("stride", 24)),
        }
        data = import_unified(source, options)
        with self._lock:
            self._data = data
            self._source = source
            self._options = options
            self._last_error = ""
            self._last_result = None
        self._emit("data_imported", source=data.validation.get("source"), **{
            key: data.validation.get(key)
            for key in (
                "rows", "columns", "specimens", "conditions", "layers",
                "retained_files", "rejected_files", "warning_ready",
            )
        })
        return {
            "validation": data.validation,
            "config": data.config,
            "preview_columns": list(data.frame.columns),
            "preview": data.frame.head(8).fillna("").to_dict(orient="records"),
        }

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.running:
                raise RuntimeError("已有训练任务正在运行")
            if self._data is None:
                raise ValueError("请先导入并预检训练数据")
            if not self._data.validation.get("ok"):
                raise ValueError("当前数据未通过预检，不能开始训练")
            training_type = str(settings.get("training_type", "prediction"))
            if training_type == "prediction_warning" and not self._data.validation.get("warning_ready"):
                raise ValueError("预测预警模式需要 state_label 同时包含 0（正常）和 1（异常）")
            data = self._data
            self._stop_event.clear()
            self._last_error = ""
            self._last_result = None
            self._progress = {
                "stage": "preparing", "epoch": 0,
                "epochs": int(settings.get("epochs", 100)),
                "bad_epochs": 0,
                "patience": int(settings.get("patience", 10)),
            }
            raw_output = str(settings.get("output_root", "")).strip()
            output_root = Path(raw_output).expanduser() if raw_output else self.output_root
            if not output_root.is_absolute():
                raise ValueError("模型保存位置必须是盘符开头的绝对路径，例如 F:\\AFP_Training_Models")
            thread = threading.Thread(
                target=self._run,
                args=(data, dict(settings), output_root),
                daemon=True,
                name="afp-web-training",
            )
            self._thread = thread
            thread.start()
        self._emit("training_queued", output_root=str(output_root.resolve()))
        return {"started": True, "output_root": str(output_root.resolve())}

    def _run(self, data: UnifiedData, settings: dict[str, Any], output_root: Path) -> None:
        try:
            result = train_models(data, settings, output_root, self._stop_event, self._emit)
            with self._lock:
                self._last_result = result
                self._progress["stage"] = "stopped" if result.get("stopped") else "finished"
            self._emit("training_stopped" if result.get("stopped") else "training_finished", **result)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._progress["stage"] = "error"
            self._emit("training_error", error=str(exc))

    def stop(self) -> dict[str, Any]:
        if not self.running:
            return {"stop_requested": False, "message": "当前没有运行中的训练任务"}
        self._stop_event.set()
        self._emit("stop_requested", message="将在当前批次结束后保存模型并停止")
        return {"stop_requested": True}

    def status(self, after_seq: int = 0) -> dict[str, Any]:
        with self._lock:
            events = [item for item in self._events if int(item["seq"]) > int(after_seq)]
            return {
                "running": self.running,
                "imported": self._data is not None,
                "validation": self._data.validation if self._data is not None else None,
                "data_config": self._data.config if self._data is not None else None,
                "progress": dict(self._progress),
                "result": self._last_result,
                "error": self._last_error,
                "events": events[-100:],
                "last_seq": self._event_seq,
            }
