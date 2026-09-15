"""Executable entry point for the local capture helper."""

from __future__ import annotations

import argparse
import json
import sys
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


def _prompt_setup_gui(existing_server: str = "") -> argparse.Namespace | None:
    """Show a setup dialog when Explorer did not provide a usable console.

    A console EXE launched by double-click can have stdin redirected to an
    already-closed handle.  The previous implementation treated that as a
    normal cancellation and exited, leaving the web page with no heartbeat.
    The small Tk dialog keeps the same one-time URL/code contract without
    requiring a terminal window.
    """

    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception:
        return None

    result: dict[str, str] = {}
    root = tk.Tk()
    root.title("AFP 本地采集辅助程序")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=16)
    frame.grid()
    ttk.Label(frame, text="连接网页端并识别本机接口", font=("Microsoft YaHei UI", 11, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
    )
    ttk.Label(frame, text="网页地址").grid(row=1, column=0, sticky="w", pady=4)
    server_var = tk.StringVar(value=existing_server)
    server_entry = ttk.Entry(frame, textvariable=server_var, width=54)
    server_entry.grid(row=1, column=1, pady=4)
    ttk.Label(frame, text="配对码").grid(row=2, column=0, sticky="w", pady=4)
    challenge_var = tk.StringVar()
    challenge_entry = ttk.Entry(frame, textvariable=challenge_var, width=54)
    challenge_entry.grid(row=2, column=1, pady=4)
    ttk.Label(
        frame,
        text="请先在网页端解锁真实模式并生成配对码；配对码 5 分钟内有效。",
    ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 12))

    def submit() -> None:
        server = server_var.get().strip()
        challenge = normalize_pairing_code(challenge_var.get())
        if not server or not challenge:
            messagebox.showwarning("信息不完整", "请输入网页地址和配对码。", parent=root)
            return
        result.update(server=server, pairing_challenge=challenge)
        root.destroy()

    def cancel() -> None:
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(2, 0))
    ttk.Button(buttons, text="连接", command=submit).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text="取消", command=cancel).grid(row=0, column=1)
    root.protocol("WM_DELETE_WINDOW", cancel)
    server_entry.focus_set()
    root.mainloop()
    if not result:
        return None
    return argparse.Namespace(
        server=result["server"],
        pairing_token="",
        pairing_challenge=result["pairing_challenge"],
        device_id="local-helper",
        transport="https",
    )


def resolve_runtime_args(
    argv: list[str] | None = None,
    *,
    input_fn=input,
    output_fn=print,
    gui_fn=None,
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
        # Explorer launches may have no usable stdin.  Open the setup dialog
        # immediately in that case instead of waiting for an input handle
        # that will be closed and making the helper silently disappear.
        if input_fn is input:
            try:
                if not sys.stdin.isatty():
                    return (gui_fn or _prompt_setup_gui)()
            except (AttributeError, OSError):
                return (gui_fn or _prompt_setup_gui)()
        try:
            args.server = str(input_fn("请输入网页地址：")).strip()
        except (EOFError, OSError):
            return (gui_fn or _prompt_setup_gui)()
        if not args.server:
            output_fn("未输入网页地址，辅助程序未启动。")
            return None
    if not args.pairing_token and not args.pairing_challenge:
        try:
            args.pairing_challenge = str(input_fn("请输入网页端生成的配对码：")).strip()
        except (EOFError, OSError):
            return (gui_fn or _prompt_setup_gui)(args.server)
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
