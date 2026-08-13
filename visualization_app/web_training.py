"""Browser-facing adapter for the native training-center workflow."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from training_center import TrainingCenter


class WebTrainingManager:
    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root).resolve()
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._thread: threading.Thread | None = None
        self.center = TrainingCenter(self.output_root, self._on_event)
        self.source_path: str = ""
        self.last_import: dict[str, Any] | None = None
        self.last_dataset: str = ""

    def _on_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._events.append(event)
            self._events = self._events[-100:]

    def import_data(self, path: str) -> dict[str, Any]:
        result = self.center.import_files(path)
        with self._lock:
            self.source_path = str(Path(path).resolve())
            self.last_import = result
            self._events.append({"event": "web_data_imported", **result})
        return result

    def start(self, path: str | None = None, epochs: int = 1, patience: int = 1,
              batch_size: int = 32, learning_rate: float = 8e-4,
              device: str = "auto") -> dict[str, Any]:
        if path:
            self.import_data(path)
        if self.center.last_frame is None:
            raise ValueError("请先选择 CSV 文件夹并点击导入预检")
        dataset = self.center.prepare_dataset(self.output_root / "dataset")
        self.last_dataset = str(dataset)
        self.center.start_training(dataset, epochs, patience, batch_size, learning_rate, device)
        return {"started": True, "dataset": self.last_dataset}

    def status(self) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)
            last = events[-1] if events else None
        running = bool(self.center._thread and self.center._thread.is_alive())
        return {
            "running": running,
            "source": self.source_path,
            "dataset": self.last_dataset,
            "last_import": self.last_import,
            "last_event": last,
            "events": events[-20:],
        }
