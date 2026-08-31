"""Native desktop entry point using the original AFP web workbench in an embedded window.

The HTML/CSS/JS frontend is rendered inside pywebview, so the user does not need
to open a browser or type a localhost address.  The local server is bound to an
ephemeral loopback port only for the embedded window and is stopped on exit.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

from app import create_server


def _run_legacy_check(arguments: list[str]) -> None:
    """Keep the existing deterministic smoke/self-test entry points available."""
    from native_integrated_app import main as legacy_main

    import sys

    old = sys.argv
    try:
        sys.argv = [old[0], *arguments]
        legacy_main()
    finally:
        sys.argv = old


def _run_model_smoke() -> None:
    """Load and execute every shipped schema/algorithm checkpoint once."""
    from online_inference import MODEL_REGISTRY, OnlineIModernTCN

    records: list[dict] = []
    for schema, width in (("legacy_original", 15), ("new_collection_v11_3", 20)):
        history = np.zeros((24, width), dtype=np.float32)
        for model_type in MODEL_REGISTRY:
            record = {"schema": schema, "model": model_type, "ok": False}
            try:
                runtime = OnlineIModernTCN(model_type=model_type, schema_mode=schema)
                prediction, mode = runtime.predict(history, 24)
                finite = bool(np.isfinite(prediction).all())
                expected = (24, width)
                if tuple(prediction.shape) != expected or not finite:
                    raise ValueError(
                        f"invalid output: shape={tuple(prediction.shape)}, finite={finite}"
                    )
                profile = runtime.profile
                record.update(
                    ok=True,
                    shape=list(prediction.shape),
                    mode=mode,
                    sha256=profile.get("checkpoint_sha256", ""),
                    atavn=bool((profile.get("atavn") or {}).get("enabled", False)),
                )
            except Exception as exc:  # keep the report complete across all models
                record["error"] = f"{type(exc).__name__}: {exc}"
            records.append(record)

    output_root = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    report_dir = output_root / "verification"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "all_prediction_models_smoke.json"
    report_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    failed = [item for item in records if not item["ok"]]
    if failed:
        raise RuntimeError(
            f"prediction-model smoke failed for {len(failed)}/{len(records)} combinations; "
            f"see {report_path}"
        )
    print(f"prediction-model-smoke: ok ({len(records)} combinations); {report_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AFP integrated native desktop application")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--integration-smoke", action="store_true")
    parser.add_argument("--model-smoke", action="store_true")
    args, unknown = parser.parse_known_args()
    if args.model_smoke:
        _run_model_smoke()
        return
    if args.self_test or args.integration_smoke:
        _run_legacy_check([*(["--self-test"] if args.self_test else []), *(["--integration-smoke"] if args.integration_smoke else []), *unknown])
        return

    try:
        import webview
    except ImportError as exc:
        raise RuntimeError(
            "桌面界面组件未安装。请安装 pywebview，或使用已打包的 EXE。"
        ) from exc

    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, name="AFP-local-ui", daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while not thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        webview.create_window(
            "AFP 实时预测与状态预警系统",
            url,
            width=1660,
            height=1040,
            min_size=(1180, 760),
            text_select=True,
        )
        webview.start(debug=False)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
