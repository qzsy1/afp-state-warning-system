from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any


APP_VERSION = "2.0.0-dev"
API_VERSION = "2.0"


def _prepare_imports(root: Path) -> Path:
    app_dir = root / "app"
    core_dir = app_dir / "core"
    for path in (core_dir, app_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return core_dir


def initialize(root: Path):
    _prepare_imports(root)
    from context import RuntimeContext
    from module_loader import ModuleManager

    context = RuntimeContext.load(root)
    context.prepare()
    context.export_environment()
    for path in (
        context.paths.legacy_dir,
        context.paths.legacy_dir / "model_runtime",
        context.paths.legacy_dir / "model_runtime" / "modern_TCN_models",
    ):
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    manager = ModuleManager(context, str(context.config.get("api_version", API_VERSION)))
    manager.load_all()
    status = manager.status()
    status.update(
        application_version=str(context.config.get("application_version", APP_VERSION)),
        root=str(root),
    )
    (context.paths.runtime_dir / "module_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return context, manager


def _legacy_module(name: str, context: Any):
    if str(context.paths.legacy_dir) not in sys.path:
        sys.path.insert(0, str(context.paths.legacy_dir))
    return __import__(name)


def _emit_command_result(context: Any, command: str, result: dict[str, Any]) -> None:
    """Persist CLI diagnostics because the packaged Windows EXE has no console."""
    payload = {
        "command": command,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "result": result,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    safe_name = command.strip("-").replace("-", "_") or "command"
    (context.paths.runtime_dir / f"{safe_name}_result.json").write_text(rendered, encoding="utf-8")
    (context.paths.runtime_dir / "last_command_result.json").write_text(rendered, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def self_test(context: Any, manager: Any) -> dict[str, Any]:
    required = [
        context.paths.legacy_dir / "app.py",
        context.paths.ui_dir / "index.html",
        context.paths.modules_dir,
        context.paths.data_dir / "dashboard_sequences.npz",
        context.paths.native_dll_dir / "BsvUvcNative.dll",
        context.paths.native_dll_dir / "libusb-1.0.dll",
    ]
    missing = [str(path) for path in required if not path.exists()]
    status = manager.status()
    result = {
        "ok": not missing and bool(status["all_healthy"]),
        "version": str(context.config.get("application_version", APP_VERSION)),
        "missing": missing,
        "modules": status,
    }
    if not result["ok"]:
        raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def verify_files(context: Any) -> dict[str, Any]:
    """Verify immutable delivery files against the build-time SHA-256 list."""
    sums_path = context.root / "SHA256SUMS.txt"
    if not sums_path.exists():
        raise FileNotFoundError(f"integrity manifest is missing: {sums_path}")
    checked = 0
    missing: list[str] = []
    mismatched: list[str] = []
    malformed: list[str] = []
    for number, raw_line in enumerate(sums_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            expected, relative = line.split(maxsplit=1)
            relative = relative.lstrip("*")
        except ValueError:
            malformed.append(f"line {number}")
            continue
        target = (context.root / relative).resolve()
        if target != context.root and context.root not in target.parents:
            malformed.append(f"line {number}: {relative}")
            continue
        if not target.is_file():
            missing.append(relative)
            continue
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest().lower() != expected.lower():
            mismatched.append(relative)
        checked += 1
    result = {
        "ok": not missing and not mismatched and not malformed,
        "checked": checked,
        "missing": missing,
        "mismatched": mismatched,
        "malformed": malformed,
    }
    if not result["ok"]:
        raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def integration_smoke(context: Any, manager: Any) -> dict[str, Any]:
    self_test(context, manager)
    legacy_app = _legacy_module("app", context)
    server = legacy_app.create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/health"
        with urllib.request.urlopen(url, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("status") != "ok":
            raise RuntimeError(f"legacy health endpoint failed: {payload}")
        return {"ok": True, "health": payload, "modules": manager.status()}
    finally:
        server.shutdown()
        server.server_close()


def functional_smoke(context: Any, manager: Any) -> dict[str, Any]:
    """Exercise simulation acquisition, prediction/warning and one-epoch training."""
    self_test(context, manager)
    legacy_app = _legacy_module("app", context)
    acquisition = _legacy_module("acquisition", context)
    training = _legacy_module("web_training_pipeline", context)
    report_root = context.root / "verification" / "functional_smoke"
    report_root.mkdir(parents=True, exist_ok=True)

    dashboard = legacy_app.DashboardData()
    bootstrap_payload = dashboard.bootstrap()
    schema = "new_collection_v11_3"
    sensors = list(acquisition.ACQUISITION_SCHEMAS[schema]["sensors"])
    profile = dashboard.best_prediction_profile(schema)
    inputs = [name for name in profile["input_sensors"] if name in sensors]
    outputs = [name for name in profile["output_sensors"] if name in sensors]
    source_file = bootstrap_payload["acquisition"]["new_collection_demo"]["source_file"]
    config = acquisition.AcquisitionConfig(
        processing_mode="prediction_warning",
        dataset_schema=schema,
        use_best_prediction_override=True,
        driver="simulator",
        source_file=source_file,
        endpoint=source_file,
        sample_rate_hz=500.0,
        selected_sensors=sensors,
        model_input_sensors=inputs,
        model_output_sensors=outputs,
        prediction_sensors=outputs,
        health_indicator="TC-HI",
        specimen_id="MODULAR_SMOKE",
        condition_id="SMOKE",
        layer=1,
        replicate=1,
        save_root=str(report_root / "captured"),
    )
    dashboard.validate_prediction_setup(config, load_model=True)
    dashboard.acquisition.start(config)
    deadline = time.time() + 45.0
    while dashboard.acquisition.status()["sample_count"] < 56 and time.time() < deadline:
        time.sleep(0.05)
    live = dashboard.live(
        sensor_id=0, history=120, step=1, threshold=0.5, rho=0.5,
        indicator="TC-HI", model_kind="random_forest",
        prediction_horizon=24, use_optimized_warning=True,
        prediction_sensors=outputs, processing_mode="prediction_warning",
        forecast_lead=1,
    )
    stopped = dashboard.acquisition.stop()

    columns = training.default_columns("new")
    unified = training.import_unified(
        {"kind": "csv", "path": str(context.paths.legacy_dir / "new_collection_demo_v11_3")},
        {
            "data_mode": "new",
            "condition_columns": ",".join(columns["condition_columns"]),
            "input_columns": ",".join(columns["input_columns"]),
            "output_columns": ",".join(columns["output_columns"]),
            "history_length": 24,
            "prediction_length": 24,
            "stride": 192,
        },
    )
    training_result = training.train_models(
        unified,
        {
            "training_type": "prediction_warning",
            "epochs": 1,
            "patience": 1,
            "batch_size": 256,
            "learning_rate": 8e-4,
            "weight_decay": 1e-4,
            "dropout": 0.05,
            "min_delta": 1e-6,
            "device": "cpu",
            "seed": 20260901,
            "model_type": "TCN",
            "task_name": "modular_functional_smoke",
        },
        report_root / "training",
        threading.Event(),
        lambda _event, **_payload: None,
    )
    report = {
        "ok": True,
        "acquisition_samples": int(stopped["sample_count"]),
        "window_state": live["window"]["state_label"],
        "layer_state": (live.get("layer") or {}).get("state_label"),
        "specimen_state": (live.get("specimen") or {}).get("state_label"),
        "training_validation": unified.validation,
        "training": training_result,
        "modules": manager.status(),
    }
    (report_root / "functional_smoke_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def launch(context: Any, manager: Any) -> None:
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("桌面界面组件缺失，请使用已打包的软件或安装 pywebview。") from exc
    legacy_app = _legacy_module("app", context)
    server = legacy_app.create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, name="AFP-local-ui", daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while not thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    try:
        ui_config = context.config.get("ui", {})
        webview.create_window(
            str(ui_config.get("title", "AFP 实时预测、状态预警与模型训练系统（模块化版）")),
            f"http://127.0.0.1:{server.server_port}/",
            width=int(ui_config.get("width", 1660)),
            height=int(ui_config.get("height", 1040)),
            min_size=(1180, 760),
            text_select=True,
        )
        webview.start(debug=False)
    finally:
        server.shutdown()
        server.server_close()


def main(root: Path, arguments: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AFP external modular runtime")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--verify-files", action="store_true")
    parser.add_argument("--integration-smoke", action="store_true")
    parser.add_argument("--functional-smoke", action="store_true")
    parser.add_argument("--module-status", action="store_true")
    parser.add_argument("--reload-module", default="")
    parser.add_argument("--install-patch", default="")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args(arguments)
    context, manager = initialize(root)

    if args.install_patch or args.rollback:
        from update_manager import UpdateManager

        updater = UpdateManager(context)
        result = updater.install(Path(args.install_patch)) if args.install_patch else updater.rollback()
        _emit_command_result(context, "install-patch" if args.install_patch else "rollback", result)
        return
    if args.reload_module:
        manager.reload(args.reload_module)
        _emit_command_result(context, "reload-module", manager.status())
        return
    if args.self_test:
        _emit_command_result(context, "self-test", self_test(context, manager))
        return
    if args.verify_files:
        _emit_command_result(context, "verify-files", verify_files(context))
        return
    if args.integration_smoke:
        _emit_command_result(context, "integration-smoke", integration_smoke(context, manager))
        return
    if args.functional_smoke:
        _emit_command_result(context, "functional-smoke", functional_smoke(context, manager))
        return
    if args.module_status:
        _emit_command_result(context, "module-status", manager.status())
        return
    launch(context, manager)
