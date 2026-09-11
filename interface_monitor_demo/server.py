from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .agent_flow import AgentGateError, run_diagnosis
from .interface_catalog import build_interface_catalog
from .monitor import InterfaceMonitor, SCENARIOS


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
MAX_REQUEST_BYTES = 64 * 1024
_FORBIDDEN_FIELDS = {
    "api_key",
    "apikey",
    "token",
    "access_token",
    "secret",
    "authorization",
    "password",
}


def _contains_secret_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_FIELDS:
                return True
            if _contains_secret_field(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_field(item) for item in value)
    return False


class DemoRequestHandler(BaseHTTPRequestHandler):
    monitor: InterfaceMonitor
    catalog: list[dict[str, object]]

    server_version = "AFPInterfaceMonitorDemo/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, object]:
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ValueError("请求长度无效") from exc
        if length <= 0:
            return {}
        if length > MAX_REQUEST_BYTES:
            raise ValueError("请求内容超过64 KiB限制")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求不是有效的UTF-8 JSON对象") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求必须是JSON对象")
        if _contains_secret_field(payload):
            raise ValueError("请求不得包含 API Key、密码或令牌原文")
        return payload

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else request_path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send_json({"error": "资源路径无效"}, HTTPStatus.BAD_REQUEST)
            return
        if not candidate.is_file():
            self._send_json({"error": "资源不存在"}, HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/bootstrap":
            snapshot = self.monitor.snapshot()
            self._send_json(
                {
                    "application": {
                        "name": "AFP接口状态监控与LangChain诊断Demo",
                        "version": "demo-1.0",
                        "baseline": "AFP Integrated System M3232 v2.0.6",
                    },
                    **snapshot,
                    "scenarios": SCENARIOS,
                    "langchain": {
                        "mode": "local_runnable_simulation",
                        "network_calls": False,
                        "key_transport": False,
                    },
                }
            )
            return
        if path == "/api/status":
            self._send_json(self.monitor.snapshot())
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/scenario":
                result = self.monitor.apply_scenario(
                    str(payload.get("interface_id", "")),
                    str(payload.get("scenario", "")),
                )
                self._send_json(result)
                return
            if path == "/api/diagnose":
                event_id = str(payload.get("event_id", ""))
                event = self.monitor.get_event(event_id)
                if event is None:
                    self._send_json({"error": "未找到可诊断的异常事件"}, HTTPStatus.NOT_FOUND)
                    return
                result = run_diagnosis(
                    event,
                    self.catalog,
                    api_key_present=payload.get("api_key_present") is True,
                    model_name=str(payload.get("model_name", "")),
                )
                self._send_json(result)
                return
            if path == "/api/reset":
                self._send_json(self.monitor.reset())
                return
            self._send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except AgentGateError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            self._send_json(
                {"error": "本地Demo处理失败，请查看接口状态后重试"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def create_server(
    host: str = "127.0.0.1", port: int = 8770
) -> ThreadingHTTPServer:
    catalog = build_interface_catalog()
    monitor = InterfaceMonitor(catalog)
    handler = type(
        "ConfiguredDemoRequestHandler",
        (DemoRequestHandler,),
        {"monitor": monitor, "catalog": catalog},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="AFP接口状态监控与LangChain诊断Demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    print(f"Demo已启动：http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
