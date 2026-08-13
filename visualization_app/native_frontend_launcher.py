"""Native desktop entry point using the original AFP web workbench in an embedded window.

The HTML/CSS/JS frontend is rendered inside pywebview, so the user does not need
to open a browser or type a localhost address.  The local server is bound to an
ephemeral loopback port only for the embedded window and is stopped on exit.
"""

from __future__ import annotations

import argparse
import threading
import time

import webview

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


def main() -> None:
    parser = argparse.ArgumentParser(description="AFP integrated native desktop application")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--integration-smoke", action="store_true")
    args, unknown = parser.parse_known_args()
    if args.self_test or args.integration_smoke:
        _run_legacy_check([*(["--self-test"] if args.self_test else []), *(["--integration-smoke"] if args.integration_smoke else []), *unknown])
        return

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
