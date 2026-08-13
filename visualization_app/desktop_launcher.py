from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import Button, Label, Tk, messagebox


APP_DIR = (
    Path(getattr(sys, "_MEIPASS"))
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
APP_SCRIPT = APP_DIR / "app.py"
URL = "http://127.0.0.1:8765/"


def _write_server_trace(message: str) -> None:
    """Persist startup diagnostics for a frozen no-console application."""
    if not getattr(sys, "frozen", False):
        return
    try:
        (APP_DIR / "server_startup.log").write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def find_edge() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", ""))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft/Edge/Application/msedge.exe",
    ]
    return next((path for path in candidates if path.exists()), None)


class DesktopLauncher:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("AFP 实时预测与状态预警系统")
        self.root.geometry("520x250")
        self.root.resizable(False, False)
        self.process: subprocess.Popen | None = None
        self.status = Label(
            self.root,
            text="正在启动本地预测与采集服务…",
            font=("Microsoft YaHei UI", 12),
            wraplength=460,
            justify="left",
        )
        self.status.pack(padx=28, pady=(34, 22), anchor="w")
        Button(
            self.root,
            text="打开状态预警主界面",
            command=self.open_app,
            width=30,
            height=2,
        ).pack(pady=5)
        Button(
            self.root,
            text="打开采集数据目录",
            command=self.open_capture_folder,
            width=30,
        ).pack(pady=5)
        Button(
            self.root,
            text="打开模型训练中心",
            command=self.open_training_center,
            width=30,
        ).pack(pady=5)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self.start_server, daemon=True).start()

    def start_server(self) -> None:
        try:
            try:
                with urllib.request.urlopen(
                    URL + "api/health", timeout=0.5
                ) as response:
                    if response.status == 200:
                        self.root.after(
                            0,
                            lambda: self.status.config(
                                text="服务已运行。可以打开桌面主界面。"
                            ),
                        )
                        self.root.after(0, self.open_app)
                        return
            except Exception:
                pass
            creationflags = (
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            )
            command = (
                [str(Path(sys.executable).resolve()), "--server", "--no-browser"]
                if getattr(sys, "frozen", False)
                else [str(Path(sys.executable).resolve()), str(APP_SCRIPT), "--no-browser"]
            )
            self.process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                creationflags=creationflags,
            )
            # CPU-only PyTorch and the bundled checkpoint can take up to a
            # minute on first launch while DLLs and models are loaded.
            for _ in range(480):
                if self.process.poll() is not None:
                    raise RuntimeError("本地服务启动失败")
                try:
                    with urllib.request.urlopen(
                        URL + "api/health", timeout=0.4
                    ) as response:
                        if response.status == 200:
                            self.root.after(
                                0,
                                lambda: self.status.config(
                                    text="服务启动成功。真实采集数据会自动保存到 captured_data。"
                                ),
                            )
                            self.root.after(0, self.open_app)
                            return
                except Exception:
                    time.sleep(0.25)
            raise TimeoutError("等待本地服务启动超时")
        except Exception as exc:
            self.root.after(
                0,
                lambda: messagebox.showerror("启动失败", str(exc)),
            )

    def open_app(self) -> None:
        edge = find_edge()
        if edge is not None:
            subprocess.Popen(
                [
                    str(edge),
                    f"--app={URL}",
                    "--start-maximized",
                    "--disable-features=msEdgeSidebarV2",
                ]
            )
        else:
            webbrowser.open(URL)

    def open_capture_folder(self) -> None:
        path = APP_DIR / "captured_data"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_training_center(self) -> None:
        try:
            from training_center_ui import open_training_center

            open_training_center(self.root)
        except Exception as exc:
            messagebox.showerror("训练中心打开失败", str(exc))

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    if "--server" in sys.argv:
        try:
            _write_server_trace("server mode entered")
            sys.argv.remove("--server")
            from app import main

            _write_server_trace("application module imported")
            main()
        except Exception:
            _write_server_trace(traceback.format_exc())
            raise
        raise SystemExit(0)
    if "--self-test" in sys.argv:
        try:
            if not APP_SCRIPT.exists():
                raise FileNotFoundError(APP_SCRIPT)
        except Exception:
            raise SystemExit(1)
        raise SystemExit(0)
    DesktopLauncher().run()
