"""Native desktop entry point using the AFP web workbench in an embedded window.

The window reuses the matching fixed local AFP service when available.  If no
matching service exists, it starts one on the fixed loopback port and falls back
to an ephemeral loopback port only when that port belongs to another service.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np

from app import create_server, health_payload


SHARED_UI_PORT = 8771
SHARED_UI_URL = os.environ.get(
    "AFP_LOCAL_UI_URL", f"http://127.0.0.1:{SHARED_UI_PORT}/"
).rstrip("/") + "/"


def discover_shared_ui_url(
    base_url: str = SHARED_UI_URL,
    *,
    expected_health: dict | None = None,
    opener=urlopen,
) -> str | None:
    """Return the fixed local AFP backend only when its release identity matches."""
    normalized = str(base_url or "").rstrip("/") + "/"
    expected = dict(expected_health or health_payload())
    try:
        request = Request(normalized + "api/health", headers={"Accept": "application/json"})
        with opener(request, timeout=0.75) as response:
            payload = json.loads(response.read(4097).decode("utf-8"))
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None
    identity_fields = ("status", "version", "build_id", "runtime_revision")
    if all(str(payload.get(key, "")) == str(expected.get(key, "")) for key in identity_fields):
        return normalized
    return None


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

    server = None
    thread = None
    url = discover_shared_ui_url()
    if url is None:
        try:
            server = create_server("127.0.0.1", SHARED_UI_PORT)
        except OSError:
            # Another matching launcher may have won the fixed-port race.
            deadline = time.time() + 1.0
            while url is None and time.time() < deadline:
                url = discover_shared_ui_url()
                if url is None:
                    time.sleep(0.05)
            if url is None:
                server = create_server("127.0.0.1", 0)
        if server is not None:
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
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
