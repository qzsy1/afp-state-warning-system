"""Native Tk training-center panel.

It is intentionally small and dependency-light so it can be bundled with the
existing Windows executable. The same panel can later be replaced by PySide6
without changing the TrainingCenter backend.
"""
from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from training_center import TrainingCenter


class TrainingCenterWindow:
    def __init__(self, parent: tk.Misc | None = None):
        self.window = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.window.title("AFP 模型训练中心")
        self.window.geometry("1080x760")
        self.center = TrainingCenter(Path.cwd() / "trained_models", self.on_event)
        self._build()

    def _build(self) -> None:
        top = ttk.Frame(self.window, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="数据来源").grid(row=0, column=0, sticky="w")
        self.source = tk.StringVar()
        ttk.Entry(top, textvariable=self.source, width=86).grid(row=0, column=1, padx=8, sticky="ew")
        ttk.Button(top, text="选择文件/文件夹", command=self.choose_source).grid(row=0, column=2)
        ttk.Button(top, text="导入并预检", command=self.import_source).grid(row=0, column=3, padx=6)
        top.columnconfigure(1, weight=1)

        config = ttk.LabelFrame(self.window, text="训练设置", padding=10)
        config.pack(fill="x", padx=12, pady=6)
        self.epochs = tk.IntVar(value=100)
        self.patience = tk.IntVar(value=10)
        self.batch = tk.IntVar(value=64)
        self.lr = tk.DoubleVar(value=8e-4)
        for col, (label, var) in enumerate((("Epoch", self.epochs), ("Patience", self.patience), ("Batch", self.batch), ("Learning rate", self.lr))):
            ttk.Label(config, text=label).grid(row=0, column=col * 2, padx=5)
            ttk.Entry(config, textvariable=var, width=12).grid(row=0, column=col * 2 + 1, padx=5)
        self.start_btn = ttk.Button(config, text="开始训练", command=self.start_training, state="disabled")
        self.start_btn.grid(row=0, column=8, padx=10)
        self.stop_btn = ttk.Button(config, text="停止", command=self.center.stop_training, state="disabled")
        self.stop_btn.grid(row=0, column=9)

        self.notebook = ttk.Notebook(self.window)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=6)
        self.data_tab = ttk.Frame(self.notebook, padding=8)
        self.metrics_tab = ttk.Frame(self.notebook, padding=8)
        self.log_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.data_tab, text="数据预检")
        self.notebook.add(self.metrics_tab, text="训练结果")
        self.notebook.add(self.log_tab, text="训练日志")
        self.data_text = tk.Text(self.data_tab, wrap="none")
        self.data_text.pack(fill="both", expand=True)
        self.metrics_text = tk.Text(self.metrics_tab, wrap="none")
        self.metrics_text.pack(fill="both", expand=True)
        self.log_text = tk.Text(self.log_tab, wrap="none")
        self.log_text.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="请选择 Excel/CSV 文件或文件夹")
        ttk.Label(self.window, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x", padx=12, pady=(0, 8))

    def choose_source(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("数据文件", "*.csv *.xlsx *.xls *.xlsm"), ("所有文件", "*.*")])
        if not path:
            path = filedialog.askdirectory(title="选择数据文件夹")
        if path:
            self.source.set(path)

    def import_source(self) -> None:
        if not self.source.get().strip():
            messagebox.showwarning("未选择数据", "请选择 Excel/CSV 文件或文件夹")
            return
        try:
            result = self.center.import_files(self.source.get().strip())
            self.data_text.delete("1.0", "end")
            self.data_text.insert("end", json.dumps(result, ensure_ascii=False, indent=2, default=str))
            if result["validation"]["ok"]:
                self.start_btn.config(state="normal")
                self.status.set("数据预检通过，可以开始训练")
            else:
                self.status.set("数据预检未通过，请修正字段或数据")
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))

    def start_training(self) -> None:
        try:
            dataset = self.center.prepare_dataset()
            self.center.start_training(dataset, self.epochs.get(), self.patience.get(), self.batch.get(), self.lr.get())
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.notebook.select(self.log_tab)
        except Exception as exc:
            messagebox.showerror("启动训练失败", str(exc))

    def on_event(self, event: dict) -> None:
        def update() -> None:
            self.log_text.insert("end", json.dumps(event, ensure_ascii=False, default=str) + "\n")
            self.log_text.see("end")
            self.status.set(str(event.get("event", "")))
            if event.get("event") == "training_finished":
                self.stop_btn.config(state="disabled")
                self.metrics_text.delete("1.0", "end")
                self.metrics_text.insert("end", json.dumps(event, ensure_ascii=False, indent=2, default=str))
                self.notebook.select(self.metrics_tab)
            elif event.get("event") == "training_failed":
                self.stop_btn.config(state="disabled")
                messagebox.showerror("训练失败", str(event.get("error", "未知错误")))
        self.window.after(0, update)


def open_training_center(parent: tk.Misc | None = None) -> TrainingCenterWindow:
    return TrainingCenterWindow(parent)


if __name__ == "__main__":
    TrainingCenterWindow().window.mainloop()
