from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_native_entry_is_server_free() -> None:
    source = (ROOT / "native_integrated_app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "http.server" not in imported
    assert "webbrowser" not in imported
    assert "webview" not in imported


def test_native_entry_contains_both_workflows() -> None:
    source = (ROOT / "native_integrated_app.py").read_text(encoding="utf-8")
    assert "class AcquisitionPanel" in source
    assert "class TrainingPanel" in source
    assert "DashboardData" in source
    assert "WebTrainingManager" in source


def test_packaged_training_runtime_is_supported() -> None:
    source = (ROOT / "web_training_pipeline.py").read_text(encoding="utf-8")
    assert 'Path(getattr(sys, "_MEIPASS")) / "model_runtime"' in source


def test_integrated_smoke_covers_both_workflows() -> None:
    source = (ROOT / "native_integrated_app.py").read_text(encoding="utf-8")
    assert '"--integration-smoke"' in source
    assert 'processing_mode="prediction_warning"' in source
    assert '"training_type": "prediction_warning"' in source
