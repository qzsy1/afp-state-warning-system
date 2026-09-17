from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any


APP_VERSION = "2.0.1-dev"
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


def _persist_lan_web_status(context: Any, status: dict[str, Any]) -> None:
    path = context.paths.runtime_dir / "lan_web_status.json"
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def _afp_server_available(url: str, timeout: float = 1.5) -> bool:
    """Return true only when an existing listener is this AFP application."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("status") == "ok" and bool(payload.get("version"))
    except Exception:
        return False


def _show_desktop_window(context: Any, url: str) -> None:
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("桌面界面组件缺失，请使用已打包的软件或安装 pywebview。") from exc
    ui_config = context.config.get("ui", {})
    webview.create_window(
        str(ui_config.get("title", "AFP 实时预测、状态预警与模型训练系统（模块化版）")),
        url,
        width=int(ui_config.get("width", 1660)),
        height=int(ui_config.get("height", 1040)),
        min_size=(1180, 760),
        text_select=True,
    )
    webview.start(debug=False)


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
    dependency_status: dict[str, dict[str, Any]] = {}
    for name in ("tkinter", "mysql.connector", "openpyxl", "xlrd"):
        try:
            module = importlib.import_module(name)
            if name == "tkinter":
                # Importing _tkinter alone is not sufficient in a frozen build:
                # Tcl/Tk data files can still be absent.  Creating a hidden root
                # verifies the same runtime path used by the file/folder pickers.
                root = module.Tk()
                root.withdraw()
                root.update_idletasks()
                root.destroy()
            dependency_status[name] = {
                "ok": True,
                "version": str(
                    getattr(module, "__version__", "")
                    or getattr(module, "TkVersion", "")
                ),
            }
        except Exception as exc:
            dependency_status[name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    status = manager.status()
    result = {
        "ok": (
            not missing
            and bool(status["all_healthy"])
            and all(item["ok"] for item in dependency_status.values())
        ),
        "version": str(context.config.get("application_version", APP_VERSION)),
        "missing": missing,
        "dependencies": dependency_status,
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


def mysql_smoke(
    context: Any,
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> dict[str, Any]:
    """Create schema, write one layer and read its relation row using packaged dependencies."""
    mysql_storage = _legacy_module("mysql_storage", context)
    settings = mysql_storage.MySQLSettings(
        enabled=True,
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        connect_timeout=5,
    )
    store = mysql_storage.MySQLCaptureStore(settings)
    connection = store.test_connection()
    if not connection.get("ok"):
        raise RuntimeError(str(connection.get("error") or "MySQL connection test failed"))
    config = SimpleNamespace(
        p=600.0,
        v=100.0,
        pr=600.0,
        initial_compaction_force_N=600.0,
        placement_speed_mm_s=100.0,
        pid_angle_deg=0.0,
        temperature_setpoint_C=360.0,
        layer=0,
        specimen_id="MODULAR_MYSQL_SMOKE",
        replicate=1,
        dataset_schema="new_collection_v11_3",
        run_id="MODULAR_MYSQL_SMOKE_RUN",
        schema_sensors=["温度1", "压力"],
        process_columns=["p", "v", "pr"],
    )
    rows = [
        {
            "时间": "2026-09-01T00:00:00.000",
            "timestamp_unix": 0.0,
            "温度1": 235.5,
            "压力": 600.0,
            "p": 600.0,
            "v": 100.0,
            "pr": 600.0,
        },
        {
            "时间": "2026-09-01T00:00:00.010",
            "timestamp_unix": 0.01,
            "温度1": 236.0,
            "压力": 601.0,
            "p": 600.0,
            "v": 100.0,
            "pr": 600.0,
        },
    ]
    saved = store.save_layer(
        config,
        rows=rows,
        layer_file="mysql_smoke_layer.csv",
        full_specimen_file="mysql_smoke_complete.csv",
        timestamp_file="mysql_smoke_timestamp.csv",
        folder_path=str(context.paths.runtime_dir / "mysql_smoke"),
        summary={"completed_layers": [1], "diagnostic": True},
    )
    if not saved.get("ok"):
        raise RuntimeError(str(saved.get("error") or "MySQL write test failed"))
    relation = store.relation_map(limit=10)
    if not relation.get("ok") or not relation.get("rows"):
        raise RuntimeError(str(relation.get("error") or "MySQL read-back test failed"))
    return {
        "ok": True,
        "database": database,
        "driver": connection.get("driver"),
        "saved_rows": saved.get("saved_rows"),
        "relation_rows": relation.get("count"),
        "specimen_key": saved.get("specimen_key"),
    }


def spreadsheet_smoke(context: Any) -> dict[str, Any]:
    """Verify packaged Excel engines by writing and reading a small workbook."""
    pandas = importlib.import_module("pandas")
    output_dir = context.root / "verification" / "spreadsheet_smoke"
    output_dir.mkdir(parents=True, exist_ok=True)
    workbook = output_dir / "excel_roundtrip.xlsx"
    expected = pandas.DataFrame(
        {
            "工况": ["p600_v100_pr600", "p700_v100_pr600"],
            "铺层": [1, 2],
            "温度1": [235.5, 241.0],
        }
    )
    expected.to_excel(workbook, index=False, engine="openpyxl")
    actual = pandas.read_excel(workbook, engine="openpyxl")
    if list(actual.columns) != list(expected.columns) or len(actual) != len(expected):
        raise RuntimeError("Excel round-trip columns or row count did not match")
    return {
        "ok": True,
        "file": str(workbook),
        "rows": int(len(actual)),
        "columns": list(actual.columns),
    }


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


def launch(context: Any, manager: Any, *, server_only: bool = False) -> None:
    from lan_web import LanWebConfig, discover_lan_urls, safe_network_status

    # The public-web deployment uses one shared dashboard and two explicitly
    # separated listeners: guest/public on 8770 and loopback administration on
    # 8771.  Keep the legacy single-listener path below for older configs and
    # for rollback compatibility.
    if context.config.get("public_web"):
        return _launch_public_web(context, manager, server_only=server_only)

    config = LanWebConfig.from_mapping(context.config.get("lan_web", {}))
    bind_host = config.bind_host if config.enabled else "127.0.0.1"
    urls = discover_lan_urls(config.port, bind_host) if config.enabled else []
    legacy_app = _legacy_module("app", context)
    initial_status = safe_network_status(config, urls, started_at=None)
    try:
        server = legacy_app.create_server(bind_host, config.port, initial_status)
    except OSError as exc:
        failed_status = safe_network_status(
            config,
            urls,
            started_at=None,
            error=f"无法绑定局域网端口 {config.port}：{exc}",
        )
        _persist_lan_web_status(context, failed_status)
        raise RuntimeError(
            f"局域网服务启动失败：端口 {config.port} 可能已被占用，请关闭占用程序后重试。"
        ) from exc
    _persist_lan_web_status(
        context,
        safe_network_status(
            config,
            urls,
            started_at=getattr(server, "service_started_at", time.time()),
        ),
    )
    thread = threading.Thread(target=server.serve_forever, name="AFP-local-ui", daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while not thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    try:
        if not config.open_desktop_window:
            thread.join()
            return
        try:
            import webview
        except ImportError as exc:
            raise RuntimeError("桌面界面组件缺失，请使用已打包的软件或安装 pywebview。") from exc
        ui_config = context.config.get("ui", {})
        webview.create_window(
            str(ui_config.get("title", "AFP 实时预测、状态预警与模型训练系统（模块化版）")),
            f"http://127.0.0.1:{config.port}/",
            width=int(ui_config.get("width", 1660)),
            height=int(ui_config.get("height", 1040)),
            min_size=(1180, 760),
            text_select=True,
        )
        webview.start(debug=False)
    finally:
        server.shutdown()
        server.server_close()


def _launch_public_web(
    context: Any, manager: Any, *, server_only: bool = False
) -> None:
    from lan_web import discover_lan_urls
    from public_web import PublicWebConfig, public_web_urls

    config = PublicWebConfig.from_mapping(context.config.get("public_web", {}))
    if not config.enabled:
        return
    existing_admin_url = f"http://127.0.0.1:{config.local_admin_port}/"
    if _afp_server_available(existing_admin_url + "api/health"):
        # The watchdog may already own the shared acquisition server.  A
        # second desktop launch reuses it instead of creating another server
        # process that can disagree about capture state or fail on the port.
        if not server_only and config.open_desktop_window:
            _show_desktop_window(context, existing_admin_url)
        return
    legacy_app = _legacy_module("app", context)
    dashboard = legacy_app.DashboardData()
    security_store = legacy_app.SecurityStore(
        context.paths.runtime_dir / "public_web_security.sqlite3"
    )
    capture_root = Path(getattr(dashboard.acquisition, "capture_root", context.paths.runtime_dir / "capture"))
    source_candidates = [
        Path(r"F:\AFP_Capture\simulation_m3232_new_collection\SIM_PRESSURE_M3232_new_collection.csv"),
        context.paths.legacy_dir / "new_collection_demo_v11_3" / "simulator_stream.csv",
        context.paths.data_dir / "dashboard_candidate_catalog.csv",
    ]
    source = next((item for item in source_candidates if item.is_file()), source_candidates[-1])
    guest_manager = legacy_app.GuestSimulationManager(
        capture_root / "public_simulation",
        {"builtin": {"source_type": "single_csv", "path": str(source)}},
    )
    lease = legacy_app.RealControlLease()
    addresses = discover_lan_urls(config.public_port, config.public_bind_host)
    urls = public_web_urls(config, [url.split("://", 1)[-1].rsplit(":", 1)[0] for url in addresses])
    common = {
        "dashboard": dashboard,
        "security_store": security_store,
        "guest_manager": guest_manager,
        "control_lease": lease,
    }
    public_server = legacy_app.create_server(
        config.public_bind_host,
        config.public_port,
        {"enabled": True, "urls": urls["public"], "public": True},
        **common,
        access_context="public",
        public_web_config=context.config.get("public_web", {}),
    )
    admin_server = legacy_app.create_server(
        config.local_admin_bind_host,
        config.local_admin_port,
        {"enabled": True, "urls": [urls["local_admin"]], "local_admin": True},
        **common,
        access_context="local_admin",
        public_web_config=context.config.get("public_web", {}),
    )
    servers = (public_server, admin_server)
    threads = [
        threading.Thread(
            target=server.serve_forever,
            name=name,
            daemon=True,
        )
        for server, name in zip(servers, ("AFP-public-web", "AFP-local-admin"))
    ]
    for thread in threads:
        thread.start()
    status = {
        "enabled": True,
        "public_urls": urls["public"],
        "local_admin_url": urls["local_admin"],
        "public_port": config.public_port,
        "local_admin_port": config.local_admin_port,
        "cloudflare_tunnel_enabled": config.cloudflare_tunnel_enabled,
        "domain": config.domain,
        "application_version": str(context.config.get("application_version", APP_VERSION)),
    }
    _persist_lan_web_status(context, status)
    try:
        if server_only or not config.open_desktop_window:
            threads[1].join()
            return
        _show_desktop_window(context, urls["local_admin"])
    finally:
        # Closing the desktop webview must not tear down the HTTP origin.  In
        # public mode that origin is also the target of the Cloudflare Tunnel;
        # shutting it down leaves the tunnel URL alive but unusable.  Join the
        # public server thread to keep this process alive until it is stopped.
        if config.keep_alive_after_window_close and sys.exc_info()[0] is None:
            threads[0].join()
        else:
            for server in servers:
                server.shutdown()
                server.server_close()


def main(root: Path, arguments: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AFP external modular runtime")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--verify-files", action="store_true")
    parser.add_argument("--integration-smoke", action="store_true")
    parser.add_argument("--functional-smoke", action="store_true")
    parser.add_argument("--mysql-smoke", action="store_true")
    parser.add_argument("--spreadsheet-smoke", action="store_true")
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--mysql-user", default="root")
    parser.add_argument("--mysql-password", default=os.environ.get("AFP_MYSQL_PASSWORD", ""))
    parser.add_argument("--mysql-database", default="afp_modular_smoke")
    parser.add_argument("--module-status", action="store_true")
    parser.add_argument("--reload-module", default="")
    parser.add_argument("--install-patch", default="")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--server-only", action="store_true")
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
    if args.mysql_smoke:
        _emit_command_result(
            context,
            "mysql-smoke",
            mysql_smoke(
                context,
                host=args.mysql_host,
                port=args.mysql_port,
                user=args.mysql_user,
                password=args.mysql_password,
                database=args.mysql_database,
            ),
        )
        return
    if args.spreadsheet_smoke:
        _emit_command_result(context, "spreadsheet-smoke", spreadsheet_smoke(context))
        return
    if args.module_status:
        _emit_command_result(context, "module-status", manager.status())
        return
    launch(context, manager, server_only=args.server_only)
