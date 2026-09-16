"""Executable entry point for the local capture helper."""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wintypes
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

from acquisition import AcquisitionConfig, MySQLSettings
from helper_relay import normalize_pairing_code
from local_capture_agent import HelperTransport, LocalCaptureAgent


class PairingRequiredError(RuntimeError):
    """Raised when the saved helper credential is no longer accepted."""


def default_runtime_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "AFP_Local_Capture_Helper" / "config.json"


def default_server_hint_path() -> Path:
    configured = os.environ.get("AFP_PUBLIC_TUNNEL_URL_FILE")
    if configured:
        return Path(configured)
    return Path("F:/softwawre/cloudflared/quick-tunnel-url.txt")


def load_current_server_hint(*, path: str | Path | None = None) -> str:
    hint_path = Path(path) if path is not None else default_server_hint_path()
    try:
        server = hint_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if server.startswith(("https://", "http://")):
        return server
    return ""


def preferred_setup_server(saved_server: str = "", *, server_hint_path: str | Path | None = None) -> str:
    current = load_current_server_hint(path=server_hint_path)
    if current:
        return current
    return str(saved_server or "")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect_secret(value: str) -> str:
    """Encrypt a pairing token with the current Windows user account."""

    raw = str(value).encode("utf-8")
    if os.name != "nt":
        return "b64:" + base64.urlsafe_b64encode(raw).decode("ascii")
    source = _DataBlob(len(raw), ctypes.cast(ctypes.create_string_buffer(raw), ctypes.POINTER(ctypes.c_byte)))
    target = _DataBlob()
    crypt_protect = ctypes.windll.crypt32.CryptProtectData
    crypt_protect.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt_protect.restype = wintypes.BOOL
    if not crypt_protect(ctypes.byref(source), "AFP Local Capture Helper", None, None, None, 0, ctypes.byref(target)):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI 加密失败")
    try:
        encrypted = ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
    return "dpapi:" + base64.urlsafe_b64encode(encrypted).decode("ascii")


def _unprotect_secret(value: str) -> str:
    encoded = str(value or "")
    if encoded.startswith("b64:"):
        return base64.urlsafe_b64decode(encoded[4:].encode("ascii")).decode("utf-8")
    if not encoded.startswith("dpapi:") or os.name != "nt":
        return ""
    encrypted = base64.urlsafe_b64decode(encoded[6:].encode("ascii"))
    source = _DataBlob(len(encrypted), ctypes.cast(ctypes.create_string_buffer(encrypted), ctypes.POINTER(ctypes.c_byte)))
    target = _DataBlob()
    crypt_unprotect = ctypes.windll.crypt32.CryptUnprotectData
    crypt_unprotect.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt_unprotect.restype = wintypes.BOOL
    description = wintypes.LPWSTR()
    if not crypt_unprotect(ctypes.byref(source), ctypes.byref(description), None, None, None, 0, ctypes.byref(target)):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI 解密失败")
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def save_runtime_config(
    server: str,
    pairing_token: str,
    device_id: str = "local-helper",
    transport: str = "auto",
    *,
    path: str | Path | None = None,
) -> Path:
    config_path = Path(path) if path is not None else default_runtime_config_path()
    config_path = config_path.expanduser().resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "server": str(server).strip().rstrip("/"),
        "pairing_token_encrypted": _protect_secret(pairing_token),
        "device_id": str(device_id or "local-helper"),
        "transport": str(transport or "auto"),
    }
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, config_path)
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass
    return config_path


def load_saved_runtime_config(*, path: str | Path | None = None) -> dict[str, str] | None:
    config_path = Path(path) if path is not None else default_runtime_config_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        token = _unprotect_secret(payload.get("pairing_token_encrypted", ""))
        server = str(payload.get("server") or "").strip()
        if not server or not token:
            return None
        return {
            "server": server,
            "pairing_token": token,
            "device_id": str(payload.get("device_id") or "local-helper"),
            # Old helpers stored ``https`` while the resilient default now
            # means WSS-first with HTTPS polling fallback.
            "transport": (
                "auto"
                if str(payload.get("transport") or "https").lower() == "https"
                else str(payload.get("transport") or "auto")
            ),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
        return None


def authentication_failure_message() -> str:
    return "配对已失效，请在网页端重新生成配对码；辅助程序不会直接退出。"


def acquire_single_instance_lock(name: str = "AFP_Local_Capture_Helper") -> int | None:
    """Return a process-wide Windows mutex handle, or None if already running."""

    if os.name != "nt":
        return 1
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if not handle:
        return None
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def release_single_instance_lock(handle: int | None) -> None:
    if not handle or os.name != "nt":
        return
    ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))


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


def _dispatch_and_report(
    agent: LocalCaptureAgent,
    transport: HelperTransport,
    device_id: str,
    command: dict[str, Any],
) -> None:
    """Run one hardware command without blocking the heartbeat poller."""

    response = dispatch_command(agent, json.dumps(command, ensure_ascii=False))
    result_payload = {
        "device_id": device_id,
        "request_id": response["request_id"],
        "payload": response["payload"],
    }
    delay = 0.5
    for attempt in range(3):
        try:
            transport.http_json("api/helper/result", result_payload)
            return
        except Exception:
            if attempt >= 2:
                return
            time.sleep(delay)
            delay = min(delay * 2.0, 4.0)


def run_http_forever(server_url: str, pairing_token: str, device_id: str) -> None:
    agent = LocalCaptureAgent()
    transport = HelperTransport(server_url, pairing_token, device_id=device_id)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="afp-helper-command")
    delay = 1.0
    try:
        while True:
            try:
                polled = transport.http_json("api/helper/poll", {"device_id": device_id})
                if not polled.get("ok"):
                    raise RuntimeError(polled.get("error") or "辅助服务拒绝连接")
                command = polled.get("command")
                if isinstance(command, dict):
                    # Hardware probes can block on a missing serial/USB/UVC
                    # endpoint.  Keep the poller free so the server still sees
                    # a heartbeat while the single command worker finishes.
                    executor.submit(_dispatch_and_report, agent, transport, device_id, command)
                delay = 1.0
                time.sleep(0.25)
            except KeyboardInterrupt:
                return
            except Exception as exc:
                if should_repair_pairing(exc):
                    raise PairingRequiredError(authentication_failure_message()) from exc
                time.sleep(delay)
                delay = min(delay * 2.0, 30.0)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def run_forever(
    server_url: str,
    pairing_token: str,
    device_id: str,
    *,
    max_consecutive_failures: int | None = None,
) -> bool:
    agent = LocalCaptureAgent()
    transport = HelperTransport(server_url, pairing_token, device_id=device_id)
    delay = 1.0
    failures = 0
    while True:
        connection = None
        try:
            connection = transport.connect_once()
            failures = 0
            connection.send(
                json.dumps(
                    transport.hello(
                        capabilities=agent.discover().get("capabilities", {})
                    ),
                    ensure_ascii=False,
                )
            )
            delay = 1.0
            try:
                connection.settimeout(10)
            except Exception:
                pass
            while True:
                try:
                    raw = connection.recv()
                except Exception as exc:
                    # websocket-client exposes a dedicated timeout exception;
                    # avoid importing its private class and keep the helper
                    # heartbeat portable across package versions.
                    if exc.__class__.__name__ == "WebSocketTimeoutException":
                        connection.send(json.dumps({"type": "heartbeat"}))
                        continue
                    raise
                if raw is None:
                    break
                try:
                    message = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                except (TypeError, ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("type") in {"hello_ack", "heartbeat_ack", "result_ack"}:
                    continue
                if message.get("type") == "error":
                    if str(message.get("error") or "") == "helper_authentication_failed":
                        raise PairingRequiredError(authentication_failure_message())
                    continue
                response = dispatch_command(agent, json.dumps(message, ensure_ascii=False))
                connection.send(json.dumps(response, ensure_ascii=False))
        except KeyboardInterrupt:
            return True
        except PairingRequiredError:
            raise
        except Exception:
            failures += 1
            if max_consecutive_failures and failures >= max_consecutive_failures:
                return False
            time.sleep(delay)
            delay = min(delay * 2.0, 30.0)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def run_auto_forever(server_url: str, pairing_token: str, device_id: str) -> None:
    """Prefer WSS, then fall back to the proven HTTPS poller.

    Three consecutive WSS connection failures are enough to identify an
    unavailable endpoint or missing optional dependency.  Once the fallback
    poller is active, all existing pairing and command semantics remain in
    place, so a public tunnel outage cannot make the helper exit.
    """
    try:
        connected = run_forever(
            server_url,
            pairing_token,
            device_id,
            max_consecutive_failures=3,
        )
    except PairingRequiredError:
        raise
    if not connected:
        run_http_forever(server_url, pairing_token, device_id)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AFP本地采集辅助程序")
    parser.add_argument("--server", default="", help="网页服务地址，例如 https://afp.example.com")
    parser.add_argument("--pairing-token", default="")
    parser.add_argument("--pairing-challenge", default="")
    parser.add_argument("--device-id", default="local-helper")
    parser.add_argument("--transport", choices=("auto", "https", "wss"), default="auto")
    parser.add_argument(
        "--background",
        action="store_true",
        help="复用已保存配置并在后台运行；双击启动时不使用此模式。",
    )
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
        transport="auto",
    )


def resolve_runtime_args(
    argv: list[str] | None = None,
    *,
    input_fn=input,
    output_fn=print,
    gui_fn=None,
    config_path: str | Path | None = None,
    server_hint_path: str | Path | None = None,
) -> argparse.Namespace | None:
    """Resolve CLI arguments without silently exiting when double-clicked.

    A console EXE launched from Explorer has no command-line arguments.  In
    that case we provide a small pairing prompt instead of letting argparse
    terminate the process before the user can read the reason.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    saved = None
    if not args.server and not args.pairing_token and not args.pairing_challenge:
        saved = load_saved_runtime_config(path=config_path)
        if saved and args.background:
            args.server = saved["server"]
            args.pairing_token = saved["pairing_token"]
            args.device_id = saved["device_id"]
            args.transport = saved["transport"]
            return args
        if saved:
            return (gui_fn or _prompt_setup_gui)(
                preferred_setup_server(saved["server"], server_hint_path=server_hint_path)
            )
    if not args.server:
        output_fn("本地采集辅助程序需要网页地址和配对码。")
        output_fn("网页地址示例：http://127.0.0.1:8770 或 https://你的域名")
        # Explorer launches may have no usable stdin.  Open the setup dialog
        # immediately in that case instead of waiting for an input handle
        # that will be closed and making the helper silently disappear.
        if input_fn is input:
            try:
                if not sys.stdin.isatty():
                    return (gui_fn or _prompt_setup_gui)(
                        preferred_setup_server(saved["server"] if saved else "", server_hint_path=server_hint_path)
                    )
            except (AttributeError, OSError):
                return (gui_fn or _prompt_setup_gui)(
                    preferred_setup_server(saved["server"] if saved else "", server_hint_path=server_hint_path)
                )
        try:
            args.server = str(input_fn("请输入网页地址：")).strip()
        except (EOFError, OSError):
            return (gui_fn or _prompt_setup_gui)(
                preferred_setup_server(saved["server"] if saved else "", server_hint_path=server_hint_path)
            )
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


def _run_main() -> None:
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
    save_runtime_config(args.server, args.pairing_token, args.device_id, args.transport)
    while True:
        try:
            if args.transport == "https":
                run_http_forever(args.server, args.pairing_token, args.device_id)
            elif args.transport == "wss":
                run_forever(args.server, args.pairing_token, args.device_id)
            else:
                run_auto_forever(args.server, args.pairing_token, args.device_id)
            return
        except PairingRequiredError as exc:
            print(str(exc), flush=True)
            repaired = resolve_runtime_args(
                ["--server", args.server],
                output_fn=print,
            )
            if repaired is None or not repaired.pairing_challenge:
                return
            bootstrap = HelperTransport(repaired.server, "", device_id=repaired.device_id)
            paired = bootstrap.http_json(
                "api/helper/pair/complete",
                {"challenge": repaired.pairing_challenge, "device_id": repaired.device_id, "capabilities": {
                    "hardware_discovery": True, "real_capture": True, "local_csv_save": True, "local_mysql_save": True,
                }},
                authorized=False,
            )
            if not paired.get("ok"):
                print(str(paired.get("error") or "重新配对失败"), flush=True)
                return
            args = repaired
            args.pairing_token = str(paired.get("pairing_token") or "")
            save_runtime_config(args.server, args.pairing_token, args.device_id, args.transport)


def main() -> None:
    lock = acquire_single_instance_lock()
    if lock is None:
        print("本地采集辅助程序已经在运行，未重复启动。", flush=True)
        return
    try:
        _run_main()
    finally:
        release_single_instance_lock(lock)


if __name__ == "__main__":
    main()
