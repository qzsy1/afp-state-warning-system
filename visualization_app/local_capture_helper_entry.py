"""Executable entry point for the local capture helper."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import fields
from typing import Any

from acquisition import AcquisitionConfig, MySQLSettings
from helper_relay import normalize_pairing_code
from local_capture_agent import HelperTransport, LocalCaptureAgent


def _config_from_payload(payload: dict[str, Any]) -> AcquisitionConfig:
    allowed = {field.name for field in fields(AcquisitionConfig)}
    return AcquisitionConfig(**{key: value for key, value in payload.items() if key in allowed})


def dispatch_command(agent: LocalCaptureAgent, raw: str | bytes) -> dict[str, Any]:
    command = HelperTransport.decode_command(raw)
    name = command["command"]
    payload = command["payload"]
    try:
        if name == "discover":
            result = agent.discover()
        elif name == "status":
            result = agent.status()
        elif name == "mysql_preflight":
            result = agent.mysql_preflight(
                MySQLSettings.from_mapping(payload),
                write_test=bool(payload.get("write_test", False)),
            )
        elif name == "check_capture":
            result = agent.check_capture(_config_from_payload(payload))
        elif name == "mysql_relation_map":
            result = agent.mysql_relation_map(
                MySQLSettings.from_mapping(payload),
                limit=int(payload.get("limit", 1000)),
            )
        elif name == "start_capture":
            result = agent.start_capture(_config_from_payload(payload))
        elif name == "stop_capture":
            result = agent.stop_capture()
        else:  # decode_command already guards this; retain a defensive branch.
            raise ValueError("helper命令不在允许列表")
    except Exception as exc:
        # The command has already been removed from the relay queue.  Always
        # return its failure to the browser; otherwise the page can only wait
        # for a result that will never arrive and report a misleading timeout.
        result = {"ok": False, "error": str(exc) or exc.__class__.__name__}
    return {
        "type": "result",
        "request_id": command["request_id"],
        "payload": result if isinstance(result, dict) else {"result": result},
    }


def should_repair_pairing(error: BaseException) -> bool:
    """Return true when the server rejected the helper credentials."""

    text = str(error or "").lower()
    return "401" in text or "authentication_failed" in text or "配对失效" in text


def run_http_forever(server_url: str, pairing_token: str, device_id: str) -> None:
    agent = LocalCaptureAgent()
    transport = HelperTransport(server_url, pairing_token, device_id=device_id)
    delay = 1.0
    while True:
        try:
            polled = transport.http_json("api/helper/poll", {"device_id": device_id})
            if not polled.get("ok"):
                raise RuntimeError(polled.get("error") or "辅助服务拒绝连接")
            command = polled.get("command")
            if command:
                response = dispatch_command(agent, json.dumps(command, ensure_ascii=False))
                transport.http_json(
                    "api/helper/result",
                    {
                        "device_id": device_id,
                        "request_id": response["request_id"],
                        "payload": response["payload"],
                    },
                )
            delay = 1.0
            time.sleep(0.25)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            if should_repair_pairing(exc):
                print("配对已失效，请重新生成配对码并重启本地采集辅助程序。", flush=True)
                return
            time.sleep(delay)
            delay = min(delay * 2.0, 30.0)


def run_forever(server_url: str, pairing_token: str, device_id: str) -> None:
    agent = LocalCaptureAgent()
    transport = HelperTransport(server_url, pairing_token, device_id=device_id)
    delay = 1.0
    while True:
        connection = None
        try:
            connection = transport.connect_once()
            connection.send(
                json.dumps(
                    transport.hello(
                        capabilities=agent.discover().get("capabilities", {})
                    ),
                    ensure_ascii=False,
                )
            )
            delay = 1.0
            while True:
                raw = connection.recv()
                if raw is None:
                    break
                response = dispatch_command(agent, raw)
                connection.send(json.dumps(response, ensure_ascii=False))
        except KeyboardInterrupt:
            return
        except Exception:
            time.sleep(delay)
            delay = min(delay * 2.0, 30.0)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AFP本地采集辅助程序")
    parser.add_argument("--server", default="", help="网页服务地址，例如 https://afp.example.com")
    parser.add_argument("--pairing-token", default="")
    parser.add_argument("--pairing-challenge", default="")
    parser.add_argument("--device-id", default="local-helper")
    parser.add_argument("--transport", choices=("https", "wss"), default="https")
    return parser


def resolve_runtime_args(
    argv: list[str] | None = None,
    *,
    input_fn=input,
    output_fn=print,
) -> argparse.Namespace | None:
    """Resolve CLI arguments without silently exiting when double-clicked.

    A console EXE launched from Explorer has no command-line arguments.  In
    that case we provide a small pairing prompt instead of letting argparse
    terminate the process before the user can read the reason.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.server:
        output_fn("本地采集辅助程序需要网页地址和配对码。")
        output_fn("网页地址示例：http://127.0.0.1:8770 或 https://你的域名")
        try:
            args.server = str(input_fn("请输入网页地址：")).strip()
        except (EOFError, OSError):
            return None
        if not args.server:
            output_fn("未输入网页地址，辅助程序未启动。")
            return None
    if not args.pairing_token and not args.pairing_challenge:
        try:
            args.pairing_challenge = str(input_fn("请输入网页端生成的配对码：")).strip()
        except (EOFError, OSError):
            return None
        if not args.pairing_challenge:
            output_fn("未输入配对码，辅助程序未启动。")
            return None
    args.pairing_challenge = normalize_pairing_code(args.pairing_challenge)
    return args


def main() -> None:
    args = resolve_runtime_args()
    if args is None:
        try:
            input("按回车关闭窗口……")
        except (EOFError, OSError):
            pass
        return
    if args.pairing_challenge:
        bootstrap = HelperTransport(args.server, "", device_id=args.device_id)
        paired = bootstrap.http_json(
            "api/helper/pair/complete",
            {"challenge": args.pairing_challenge, "device_id": args.device_id, "capabilities": {
                "hardware_discovery": True, "real_capture": True, "local_csv_save": True, "local_mysql_save": True,
            }},
            authorized=False,
        )
        if not paired.get("ok"):
            error_code = str(paired.get("error") or "")
            if error_code == "pairing_invalid_or_expired":
                raise RuntimeError(
                    "配对码无效或已过期：请确认辅助程序填写的是生成配对码的同一个网页地址，"
                    "并在5分钟内重新生成后输入。可直接粘贴网页显示的“配对码：xxxx”。"
                )
            raise RuntimeError(error_code or "辅助程序配对失败")
        args.pairing_token = str(paired.get("pairing_token") or "")
    if args.transport == "https":
        run_http_forever(args.server, args.pairing_token, args.device_id)
    else:
        run_forever(args.server, args.pairing_token, args.device_id)


if __name__ == "__main__":
    main()
