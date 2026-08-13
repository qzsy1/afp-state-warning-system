"""AFP acquisition, forecasting, warning and model training desktop app.

This entry point is deliberately server-free: Tk widgets call the existing
acquisition, I-ModernTCN inference, health-indicator and training backends in
the same process.  It is suitable both for source execution and PyInstaller.
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import traceback
import tempfile
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_DIR = (
    Path(getattr(sys, "_MEIPASS"))
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from acquisition import (  # noqa: E402
    ACQUISITION_SCHEMAS,
    DEFAULT_SIMULATOR_FILE,
    AcquisitionConfig,
)
from mysql_storage import MySQLCaptureStore, MySQLSettings  # noqa: E402
from web_training import WebTrainingManager  # noqa: E402
from web_training_pipeline import (  # noqa: E402
    default_columns,
    default_mysql_query,
)


COLORS = {
    "navy": "#123b5d",
    "blue": "#1979b7",
    "cyan": "#00aebd",
    "orange": "#ed8b2c",
    "red": "#cf3e3e",
    "green": "#1f8b62",
    "bg": "#eef4f8",
    "panel": "#ffffff",
    "line": "#c9d8e4",
    "muted": "#60798c",
}


def _fmt(value: Any, digits: int = 4) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}f}"


def _safe_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


class ScrollFrame(ttk.Frame):
    def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0, background=COLORS["bg"])
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, padding=8)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.body.bind("<Configure>", self._resize_scroll)
        self.canvas.bind("<Configure>", self._resize_width)
        self.canvas.bind("<Enter>", lambda _event: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _event: self.canvas.unbind_all("<MouseWheel>"))

    def _resize_scroll(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_width(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _wheel(self, event: tk.Event) -> None:
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget in {self, self.canvas, self.body}:
                self.canvas.yview_scroll(int(-event.delta / 120), "units")
                return
            widget = getattr(widget, "master", None)


class LineChart(tk.Canvas):
    def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
        super().__init__(parent, background="white", highlightthickness=1,
                         highlightbackground=COLORS["line"], **kwargs)
        self._payload: dict[str, Any] | None = None
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_channel(self, channel: dict[str, Any] | None) -> None:
        self._payload = channel
        self.redraw()

    @staticmethod
    def _pairs(xs: list[Any], ys: list[Any]) -> list[tuple[float, float]]:
        pairs: list[tuple[float, float]] = []
        for x, y in zip(xs, ys):
            try:
                xv, yv = float(x), float(y)
            except (TypeError, ValueError):
                continue
            if math.isfinite(xv) and math.isfinite(yv):
                pairs.append((xv, yv))
        return pairs

    def redraw(self) -> None:
        self.delete("all")
        width, height = max(self.winfo_width(), 320), max(self.winfo_height(), 220)
        left, top, right, bottom = 58, 25, width - 20, height - 44
        self.create_rectangle(left, top, right, bottom, outline=COLORS["line"])
        payload = self._payload
        if not payload:
            self.create_text(width / 2, height / 2, text="等待采集数据", fill=COLORS["muted"], font=("Microsoft YaHei UI", 13))
            return
        xo = payload.get("x_observed", [])
        xf = payload.get("x_future", [])
        actual = self._pairs(xo, payload.get("actual", []))
        predicted = self._pairs(xo, payload.get("prediction_observed", []))
        future = self._pairs(xf, payload.get("prediction_future", []))
        all_pairs = actual + predicted + future
        if not all_pairs:
            self.create_text(width / 2, height / 2, text="当前尚无可绘制数据", fill=COLORS["muted"])
            return
        xmin = min(x for x, _ in all_pairs); xmax = max(x for x, _ in all_pairs)
        ymin = min(y for _, y in all_pairs); ymax = max(y for _, y in all_pairs)
        if xmax <= xmin: xmax = xmin + 1.0
        if ymax <= ymin: ymax = ymin + 1.0
        pad = (ymax - ymin) * 0.08
        ymin -= pad; ymax += pad

        def point(pair: tuple[float, float]) -> tuple[float, float]:
            x, y = pair
            return (
                left + (x - xmin) / (xmax - xmin) * (right - left),
                bottom - (y - ymin) / (ymax - ymin) * (bottom - top),
            )

        for i in range(5):
            y = top + i * (bottom - top) / 4
            value = ymax - i * (ymax - ymin) / 4
            self.create_line(left, y, right, y, fill="#e7eef3")
            self.create_text(left - 7, y, text=f"{value:.2f}", anchor="e", fill=COLORS["muted"], font=("Segoe UI", 8))

        def draw(series: list[tuple[float, float]], color: str, width_px: int = 2) -> None:
            if len(series) < 2:
                return
            coords: list[float] = []
            for pair in series:
                coords.extend(point(pair))
            self.create_line(*coords, fill=color, width=width_px, smooth=False)

        draw(actual, COLORS["cyan"], 2)
        draw(predicted, COLORS["blue"], 2)
        draw(future, COLORS["orange"], 2)
        zero_x = left + (0.0 - xmin) / (xmax - xmin) * (right - left)
        if left <= zero_x <= right:
            self.create_line(zero_x, top, zero_x, bottom, fill=COLORS["orange"], dash=(4, 3))
        self.create_text(left, height - 20, text="实际", fill=COLORS["cyan"], anchor="w", font=("Microsoft YaHei UI", 9, "bold"))
        self.create_text(left + 55, height - 20, text="历史预测", fill=COLORS["blue"], anchor="w", font=("Microsoft YaHei UI", 9, "bold"))
        self.create_text(left + 135, height - 20, text="未来预测", fill=COLORS["orange"], anchor="w", font=("Microsoft YaHei UI", 9, "bold"))
        self.create_text(width / 2, 12, text=f"{payload.get('name', '')}  实际 {_fmt(payload.get('actual_current'), 2)}  预测 {_fmt(payload.get('prediction_current'), 2)}  RMSE {_fmt(payload.get('rmse'), 3)}", fill=COLORS["navy"], font=("Microsoft YaHei UI", 10, "bold"))


class LossChart(LineChart):
    def set_history(self, history: list[dict[str, Any]]) -> None:
        self.delete("all")
        payload = {
            "name": "训练损失 / 验证损失",
            "x_observed": [row.get("epoch") for row in history],
            "actual": [row.get("train_loss") for row in history],
            "prediction_observed": [row.get("validation_loss") for row in history],
            "x_future": [], "prediction_future": [],
            "actual_current": history[-1].get("train_loss") if history else None,
            "prediction_current": history[-1].get("validation_loss") if history else None,
            "rmse": None,
        }
        self._payload = payload
        self.redraw()


class AcquisitionPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: "IntegratedApp") -> None:
        super().__init__(parent, padding=8)
        self.app = app
        self.dashboard: Any = None
        self.bootstrap: dict[str, Any] = {}
        self._live_busy = False
        self._last_live: dict[str, Any] | None = None
        self.vars: dict[str, tk.Variable] = {}
        self._build()

    def _v(self, name: str, value: Any = "") -> tk.Variable:
        var: tk.Variable
        if isinstance(value, bool): var = tk.BooleanVar(value=value)
        elif isinstance(value, int): var = tk.IntVar(value=value)
        elif isinstance(value, float): var = tk.DoubleVar(value=value)
        else: var = tk.StringVar(value=value)
        self.vars[name] = var
        return var

    def _entry(self, parent: tk.Misc, label: str, name: str, value: Any, row: int, column: int, width: int = 12, show: str = "") -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=3, pady=3)
        entry = ttk.Entry(parent, textvariable=self._v(name, value), width=width, show=show)
        entry.grid(row=row, column=column + 1, sticky="ew", padx=3, pady=3)
        return entry

    def _build(self) -> None:
        self.columnconfigure(0, weight=0, minsize=390)
        self.columnconfigure(1, weight=3)
        self.columnconfigure(2, weight=1, minsize=285)
        self.rowconfigure(0, weight=1)
        left = ScrollFrame(self)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        center = ttk.Frame(self)
        center.grid(row=0, column=1, sticky="nsew", padx=4)
        right = ScrollFrame(self)
        right.grid(row=0, column=2, sticky="nsew", padx=(7, 0))
        self._build_controls(left.body)
        self._build_monitor(center)
        self._build_warning(right.body)

    def _build_controls(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        basic = ttk.LabelFrame(parent, text="采集与运行模式", padding=8)
        basic.grid(row=0, column=0, sticky="ew", pady=4)
        basic.columnconfigure(1, weight=1); basic.columnconfigure(3, weight=1)
        ttk.Label(basic, text="数据方案").grid(row=0, column=0, sticky="w", padx=3, pady=3)
        self.schema_combo = ttk.Combobox(basic, textvariable=self._v("dataset_schema", "new_collection_v11_3"), state="readonly", values=("new_collection_v11_3", "legacy_original"), width=20)
        self.schema_combo.grid(row=0, column=1, columnspan=3, sticky="ew", padx=3, pady=3)
        self.schema_combo.bind("<<ComboboxSelected>>", lambda _e: self._schema_changed())
        ttk.Label(basic, text="处理模式").grid(row=1, column=0, sticky="w", padx=3, pady=3)
        self.mode_combo = ttk.Combobox(basic, textvariable=self._v("processing_mode", "prediction_warning"), state="readonly", values=("prediction_warning", "capture_only"), width=20)
        self.mode_combo.grid(row=1, column=1, columnspan=3, sticky="ew", padx=3, pady=3)
        self.mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._mode_changed())
        ttk.Label(basic, text="连接方式").grid(row=2, column=0, sticky="w", padx=3, pady=3)
        self.driver_combo = ttk.Combobox(basic, textvariable=self._v("driver", "simulator"), state="readonly", values=("simulator", "serial_json", "tcp_json"), width=15)
        self.driver_combo.grid(row=2, column=1, sticky="ew", padx=3, pady=3)
        self._entry(basic, "采样Hz", "sample_rate_hz", 10.0, 2, 2)
        self._entry(basic, "端点/模拟CSV", "endpoint", "", 3, 0, width=30)
        ttk.Button(basic, text="浏览", command=self._pick_endpoint).grid(row=3, column=2, columnspan=2, sticky="ew", padx=3)
        self._entry(basic, "串口波特率", "baudrate", 115200, 4, 0)

        identity = ttk.LabelFrame(parent, text="试样、工况与铺层", padding=8)
        identity.grid(row=1, column=0, sticky="ew", pady=4)
        identity.columnconfigure(1, weight=1); identity.columnconfigure(3, weight=1)
        self._entry(identity, "试样名", "specimen_id", "LIVE_SPECIMEN_001", 0, 0)
        self._entry(identity, "工况编号", "condition_id", "C001", 0, 2)
        self._entry(identity, "独立重复", "replicate", 1, 1, 0)
        self._entry(identity, "铺层数", "layer_display", 1, 1, 2)
        self._entry(identity, "旧功率p", "p", 600.0, 2, 0)
        self._entry(identity, "旧速度v", "v", 100.0, 2, 2)
        self._entry(identity, "旧压实力pr", "pr", 600.0, 3, 0)
        self._entry(identity, "初始压实力N", "initial_compaction_force_N", 400.0, 4, 0)
        self._entry(identity, "铺放速度mm/s", "placement_speed_mm_s", 80.0, 4, 2)
        self._entry(identity, "PID角度°", "pid_angle_deg", 5.0, 5, 0)
        self._entry(identity, "温度设定°C", "temperature_setpoint_C", 360.0, 5, 2)

        channels = ttk.LabelFrame(parent, text="采集、模型输入与模型输出", padding=8)
        channels.grid(row=2, column=0, sticky="ew", pady=4)
        for col, title in enumerate(("采集保存", "模型输入", "模型输出")):
            ttk.Label(channels, text=title, foreground=COLORS["navy"]).grid(row=0, column=col, pady=(0, 3))
        self.sensor_lists: list[tk.Listbox] = []
        for col in range(3):
            box = tk.Listbox(channels, selectmode="multiple", exportselection=False, height=8, width=15)
            box.grid(row=1, column=col, sticky="nsew", padx=2)
            self.sensor_lists.append(box)
            channels.columnconfigure(col, weight=1)
        ttk.Button(channels, text="当前方案全选", command=self._select_all_sensors).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(5, 0))

        prediction = ttk.LabelFrame(parent, text="预测与健康指标", padding=8)
        prediction.grid(row=3, column=0, sticky="ew", pady=4)
        prediction.columnconfigure(1, weight=1); prediction.columnconfigure(3, weight=1)
        ttk.Checkbutton(prediction, text="使用登记的最佳预测模型", variable=self._v("use_best_prediction_override", True)).grid(row=0, column=0, columnspan=4, sticky="w")
        self._entry(prediction, "模型文件", "prediction_model_file", "", 1, 0, width=30)
        ttk.Button(prediction, text="选择", command=self._pick_model).grid(row=1, column=2, columnspan=2, sticky="ew", padx=3)
        ttk.Label(prediction, text="健康指标").grid(row=2, column=0, sticky="w", padx=3)
        self.indicator_combo = ttk.Combobox(prediction, textvariable=self._v("health_indicator", "TC-HI"), state="readonly", width=15)
        self.indicator_combo.grid(row=2, column=1, sticky="ew", padx=3)
        self.indicator_combo.bind("<<ComboboxSelected>>", lambda _e: self._indicator_changed())
        ttk.Label(prediction, text="异常分数模型").grid(row=2, column=2, sticky="w", padx=3)
        self.warning_model_combo = ttk.Combobox(prediction, textvariable=self._v("warning_model", "random_forest"), state="readonly", values=("logistic", "svm_rbf", "random_forest", "extra_trees"), width=14)
        self.warning_model_combo.grid(row=2, column=3, sticky="ew", padx=3)
        self._entry(prediction, "未来步长", "prediction_horizon", 24, 3, 0)
        self._entry(prediction, "因果提前量", "forecast_lead", 1, 3, 2)
        self._entry(prediction, "阈值", "threshold", 0.5, 4, 0)
        self._entry(prediction, "CAP ρ", "rho", 0.5, 4, 2)
        ttk.Checkbutton(prediction, text="启用因果在线优化（兼容时）", variable=self._v("use_optimized_warning", True)).grid(row=5, column=0, columnspan=4, sticky="w")

        saving = ttk.LabelFrame(parent, text="本地与 MySQL 保存", padding=8)
        saving.grid(row=4, column=0, sticky="ew", pady=4)
        saving.columnconfigure(1, weight=1); saving.columnconfigure(3, weight=1)
        self._entry(saving, "保存根目录", "save_root", r"F:\AFP_Capture", 0, 0, width=28)
        ttk.Button(saving, text="选择", command=self._pick_save_root).grid(row=0, column=2, columnspan=2, sticky="ew", padx=3)
        ttk.Checkbutton(saving, text="试样结束后同步 MySQL", variable=self._v("mysql_enabled", False)).grid(row=1, column=0, columnspan=4, sticky="w")
        self._entry(saving, "主机", "mysql_host", "127.0.0.1", 2, 0)
        self._entry(saving, "端口", "mysql_port", 3306, 2, 2)
        self._entry(saving, "用户", "mysql_user", "root", 3, 0)
        self._entry(saving, "密码", "mysql_password", "", 3, 2, show="*")
        self._entry(saving, "数据库", "mysql_database", "afp_state_warning", 4, 0, width=20)
        ttk.Button(saving, text="查看工况—试样—铺层关系", command=self._show_mysql_relations).grid(row=5, column=0, columnspan=4, sticky="ew", pady=(5, 0))

        actions = ttk.Frame(parent)
        actions.grid(row=5, column=0, sticky="ew", pady=8)
        for col in range(4): actions.columnconfigure(col, weight=1)
        self.test_button = ttk.Button(actions, text="检查连接", command=self.test_connection, state="disabled")
        self.start_button = ttk.Button(actions, text="开始采集", command=self.start, state="disabled")
        self.stop_button = ttk.Button(actions, text="停止并保存", command=self.stop, state="disabled")
        self.open_button = ttk.Button(actions, text="打开数据目录", command=self.open_folder)
        self.test_button.grid(row=0, column=0, sticky="ew", padx=2)
        self.start_button.grid(row=0, column=1, sticky="ew", padx=2)
        self.stop_button.grid(row=0, column=2, sticky="ew", padx=2)
        self.open_button.grid(row=0, column=3, sticky="ew", padx=2)
        self.status_text = tk.StringVar(value="正在加载模型与健康指标资源……")
        ttk.Label(parent, textvariable=self.status_text, wraplength=350, foreground=COLORS["muted"]).grid(row=6, column=0, sticky="ew", pady=4)

    def _build_monitor(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1); parent.rowconfigure(2, weight=1)
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="实时传感器与未来预测", font=("Microsoft YaHei UI", 15, "bold"), foreground=COLORS["navy"]).grid(row=0, column=0, sticky="w")
        self.monitor_status = tk.StringVar(value="等待后台初始化")
        ttk.Label(header, textvariable=self.monitor_status, foreground=COLORS["blue"]).grid(row=0, column=1, sticky="e")
        select = ttk.Frame(parent)
        select.grid(row=1, column=0, sticky="ew", pady=3)
        ttk.Label(select, text="显示通道").pack(side="left")
        self.sensor_display = tk.StringVar()
        self.sensor_combo = ttk.Combobox(select, textvariable=self.sensor_display, state="readonly", width=24)
        self.sensor_combo.pack(side="left", padx=6)
        self.sensor_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_chart())
        self.chart = LineChart(parent, height=410)
        self.chart.grid(row=2, column=0, sticky="nsew")
        table_frame = ttk.LabelFrame(parent, text="全部通道当前值", padding=5)
        table_frame.grid(row=3, column=0, sticky="nsew", pady=(7, 0))
        table_frame.columnconfigure(0, weight=1); table_frame.rowconfigure(0, weight=1)
        self.channel_table = ttk.Treeview(table_frame, columns=("actual", "prediction", "rmse"), show="tree headings", height=7)
        self.channel_table.heading("#0", text="通道"); self.channel_table.heading("actual", text="实际"); self.channel_table.heading("prediction", text="预测"); self.channel_table.heading("rmse", text="RMSE")
        self.channel_table.column("#0", width=145); self.channel_table.column("actual", width=90, anchor="center"); self.channel_table.column("prediction", width=90, anchor="center"); self.channel_table.column("rmse", width=90, anchor="center")
        self.channel_table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.channel_table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns"); self.channel_table.configure(yscrollcommand=scrollbar.set)

    def _build_warning(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        ttk.Label(parent, text="三级状态预警", font=("Microsoft YaHei UI", 15, "bold"), foreground=COLORS["navy"]).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.warning_vars: dict[str, dict[str, tk.StringVar]] = {}
        for row, (key, title) in enumerate((("window", "窗口级"), ("layer", "铺层级"), ("specimen", "试样级")), 1):
            frame = ttk.LabelFrame(parent, text=title, padding=10)
            frame.grid(row=row, column=0, sticky="ew", pady=4)
            state = tk.StringVar(value="等待数据"); score = tk.StringVar(value="健康指标：—")
            ttk.Label(frame, textvariable=state, font=("Microsoft YaHei UI", 14, "bold"), foreground=COLORS["blue"]).pack(anchor="w")
            ttk.Label(frame, textvariable=score, foreground=COLORS["muted"]).pack(anchor="w", pady=(3, 0))
            self.warning_vars[key] = {"state": state, "score": score}
        evidence_frame = ttk.LabelFrame(parent, text="铺层证据形成进度", padding=6)
        evidence_frame.grid(row=4, column=0, sticky="ew", pady=7)
        self.evidence_table = ttk.Treeview(evidence_frame, columns=("windows", "health", "state"), show="headings", height=7)
        for name, label, width in (("windows", "窗口证据", 75), ("health", "HI", 60), ("state", "状态", 95)):
            self.evidence_table.heading(name, text=label); self.evidence_table.column(name, width=width, anchor="center")
        self.evidence_table.pack(fill="both", expand=True)
        probability = ttk.LabelFrame(parent, text="异常类型概率", padding=6)
        probability.grid(row=5, column=0, sticky="ew", pady=7)
        self.probability_text = tk.Text(probability, height=9, wrap="word", relief="flat", background="white")
        self.probability_text.pack(fill="both", expand=True)

    def attach_dashboard(self, dashboard: Any, bootstrap: dict[str, Any]) -> None:
        self.dashboard, self.bootstrap = dashboard, bootstrap
        self.test_button.config(state="normal"); self.start_button.config(state="normal")
        self.status_text.set("后台加载完成；可检查连接或开始采集。")
        self.monitor_status.set("就绪（原生直连，无本地网页服务）")
        self._schema_changed()
        self.after(250, self._poll)

    def _schema_changed(self) -> None:
        schema = str(self.vars["dataset_schema"].get())
        sensors = list(ACQUISITION_SCHEMAS[schema]["sensors"])
        for box in self.sensor_lists:
            box.delete(0, "end")
            for sensor in sensors: box.insert("end", sensor)
            box.select_set(0, "end")
        self.sensor_combo["values"] = sensors
        if sensors: self.sensor_display.set(sensors[0])
        if schema == "new_collection_v11_3" and self.bootstrap:
            demo = self.bootstrap.get("acquisition", {}).get("new_collection_demo", {})
            self.vars["endpoint"].set(demo.get("source_file") or str(DEFAULT_SIMULATOR_FILE))
        elif schema == "legacy_original":
            self.vars["endpoint"].set(str(DEFAULT_SIMULATOR_FILE))
        indicators = self.bootstrap.get("indicator_schemas", {}).get(schema, []) if self.bootstrap else []
        ids = [item.get("id") for item in indicators] or ["TC-HI", "T-HI", "C-HI", "RFHI", "PR-HI", "MPRF-HI"]
        self.indicator_combo["values"] = ids
        if self.vars["health_indicator"].get() not in ids: self.vars["health_indicator"].set(ids[0])
        self._indicator_changed()

    def _mode_changed(self) -> None:
        capture_only = self.vars["processing_mode"].get() == "capture_only"
        self.monitor_status.set("仅采集模式" if capture_only else "预测预警模式")

    def _indicator_changed(self) -> None:
        if not self.bootstrap: return
        schema = str(self.vars["dataset_schema"].get())
        indicator = str(self.vars["health_indicator"].get())
        for item in self.bootstrap.get("indicator_schemas", {}).get(schema, []):
            if item.get("id") == indicator:
                self.warning_model_combo["values"] = [m.get("id") for m in item.get("models", [])]
                self.vars["warning_model"].set(item.get("recommended_model") or "random_forest")
                break

    def _selected(self, box: tk.Listbox) -> list[str]:
        return [str(box.get(index)) for index in box.curselection()]

    def _select_all_sensors(self) -> None:
        for box in self.sensor_lists: box.select_set(0, "end")

    def _pick_endpoint(self) -> None:
        selected = filedialog.askopenfilename(title="选择模拟采集CSV", filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")])
        if selected: self.vars["endpoint"].set(selected)

    def _pick_model(self) -> None:
        selected = filedialog.askopenfilename(title="选择预测模型", filetypes=[("PyTorch模型", "*.pth *.pt"), ("所有文件", "*.*")])
        if selected:
            self.vars["prediction_model_file"].set(selected)
            self.vars["use_best_prediction_override"].set(False)

    def _pick_save_root(self) -> None:
        selected = filedialog.askdirectory(title="选择采集保存根目录", initialdir=str(self.vars["save_root"].get()))
        if selected: self.vars["save_root"].set(selected)

    def _config(self) -> AcquisitionConfig:
        capture = self._selected(self.sensor_lists[0])
        inputs = self._selected(self.sensor_lists[1])
        outputs = self._selected(self.sensor_lists[2])
        return AcquisitionConfig(
            processing_mode=str(self.vars["processing_mode"].get()),
            dataset_schema=str(self.vars["dataset_schema"].get()),
            use_best_prediction_override=bool(self.vars["use_best_prediction_override"].get()),
            driver=str(self.vars["driver"].get()), endpoint=str(self.vars["endpoint"].get()).strip(),
            source_file=(str(self.vars["endpoint"].get()).strip() if str(self.vars["driver"].get()) == "simulator" else ""),
            baudrate=_safe_int(self.vars["baudrate"].get(), 115200, 1), sample_rate_hz=_safe_float(self.vars["sample_rate_hz"].get(), 10.0),
            selected_sensors=capture, model_input_sensors=inputs, model_output_sensors=outputs,
            prediction_sensors=outputs, prediction_model_file=str(self.vars["prediction_model_file"].get()).strip(),
            health_indicator=str(self.vars["health_indicator"].get()),
            run_id=str(self.vars["specimen_id"].get()), specimen_id=str(self.vars["specimen_id"].get()), condition_id=str(self.vars["condition_id"].get()),
            layer=_safe_int(self.vars["layer_display"].get(), 1, 1) - 1, replicate=_safe_int(self.vars["replicate"].get(), 1, 1),
            p=_safe_float(self.vars["p"].get(), 600.0), v=_safe_float(self.vars["v"].get(), 100.0), pr=_safe_float(self.vars["pr"].get(), 600.0),
            initial_compaction_force_N=_safe_float(self.vars["initial_compaction_force_N"].get(), 400.0), placement_speed_mm_s=_safe_float(self.vars["placement_speed_mm_s"].get(), 80.0), pid_angle_deg=_safe_float(self.vars["pid_angle_deg"].get(), 5.0), temperature_setpoint_C=_safe_float(self.vars["temperature_setpoint_C"].get(), 360.0),
            save_root=str(self.vars["save_root"].get()).strip(), mysql_enabled=bool(self.vars["mysql_enabled"].get()), mysql_host=str(self.vars["mysql_host"].get()), mysql_port=_safe_int(self.vars["mysql_port"].get(), 3306, 1), mysql_user=str(self.vars["mysql_user"].get()), mysql_password=str(self.vars["mysql_password"].get()), mysql_database=str(self.vars["mysql_database"].get()),
        )

    def _background(self, label: str, work: Callable[[], Any], done: Callable[[Any], None] | None = None) -> None:
        self.status_text.set(label)
        def runner() -> None:
            try:
                result = work()
                self.after(0, lambda: (done(result) if done else self.status_text.set("操作完成")))
            except Exception as exc:
                self.after(0, lambda e=str(exc): (self.status_text.set(e), messagebox.showerror("操作失败", e)))
        threading.Thread(target=runner, daemon=True).start()

    def test_connection(self) -> None:
        config = self._config()
        def work() -> dict[str, Any]:
            model = self.dashboard.validate_prediction_setup(config, load_model=False)
            result = self.dashboard.acquisition.test_connection(config)
            result["prediction_model"] = model
            return result
        self._background("正在检查传感器与模型兼容性……", work, lambda result: self.status_text.set("连接检查通过" if result.get("ok") else "连接检查未通过：" + "; ".join(result.get("errors", []))))

    def start(self) -> None:
        config = self._config()
        def work() -> dict[str, Any]:
            self.dashboard.validate_prediction_setup(config, load_model=True)
            return self.dashboard.acquisition.start(config)
        def done(result: dict[str, Any]) -> None:
            self.start_button.config(state="disabled"); self.stop_button.config(state="normal")
            self.status_text.set(f"采集中：{result.get('layer_file') or result.get('session_dir')}")
            self.app.set_runtime("采集中 + 实时预测预警" if config.processing_mode == "prediction_warning" else "仅采集中")
        self._background("正在加载模型并启动采集……", work, done)

    def stop(self) -> None:
        def done(result: dict[str, Any]) -> None:
            self.start_button.config(state="normal"); self.stop_button.config(state="disabled")
            mysql = result.get("mysql", {})
            suffix = f"；MySQL已保存{mysql.get('saved_rows', 0)}行" if mysql.get("ok") else (f"；MySQL未保存：{mysql.get('error', '')}" if mysql.get("enabled") else "")
            self.status_text.set(f"采集已停止并保存{suffix}")
            self.app.set_runtime("就绪")
        self._background("正在停止并保存完整试样/分层数据……", self.dashboard.acquisition.stop, done)

    def open_folder(self) -> None:
        path = Path(str(self.vars["save_root"].get()) or r"F:\AFP_Capture")
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def _show_mysql_relations(self) -> None:
        settings = MySQLSettings(enabled=True, host=str(self.vars["mysql_host"].get()), port=_safe_int(self.vars["mysql_port"].get(), 3306, 1), user=str(self.vars["mysql_user"].get()), password=str(self.vars["mysql_password"].get()), database=str(self.vars["mysql_database"].get()))
        def show(result: dict[str, Any]) -> None:
            window = tk.Toplevel(self); window.title("工况—试样—铺层关系"); window.geometry("1100x620")
            columns = list(result.get("columns") or [])
            tree = ttk.Treeview(window, columns=columns, show="headings")
            for name in columns: tree.heading(name, text=name); tree.column(name, width=130)
            for row in result.get("rows", []): tree.insert("", "end", values=[row.get(name, "") for name in columns])
            tree.pack(fill="both", expand=True, padx=8, pady=8)
        self._background("正在读取数据库关系视图……", lambda: MySQLCaptureStore(settings).relation_map(2000), show)

    def _poll(self) -> None:
        if self.dashboard is None:
            self.after(500, self._poll); return
        status = self.dashboard.acquisition.status()
        if status.get("running") and not self._live_busy:
            self._live_busy = True
            sensor_names = list(self.sensor_combo["values"])
            sensor_id = sensor_names.index(self.sensor_display.get()) if self.sensor_display.get() in sensor_names else 0
            def work() -> None:
                try:
                    payload = self.dashboard.live(sensor_id=sensor_id, history=360, step=1, threshold=_safe_float(self.vars["threshold"].get(), 0.5), rho=_safe_float(self.vars["rho"].get(), 0.5), indicator=str(self.vars["health_indicator"].get()), model_kind=str(self.vars["warning_model"].get()), prediction_horizon=min(600, _safe_int(self.vars["prediction_horizon"].get(), 24, 1)), use_optimized_warning=bool(self.vars["use_optimized_warning"].get()), prediction_sensors=self._selected(self.sensor_lists[2]), processing_mode=str(self.vars["processing_mode"].get()), forecast_lead=min(24, _safe_int(self.vars["forecast_lead"].get(), 1, 1)))
                    self.after(0, lambda: self._apply_live(payload))
                except Exception as exc:
                    self.after(0, lambda e=str(exc): self.monitor_status.set("实时计算错误：" + e))
                finally:
                    self.after(0, lambda: setattr(self, "_live_busy", False))
            threading.Thread(target=work, daemon=True).start()
        self.after(250, self._poll)

    def _apply_live(self, payload: dict[str, Any]) -> None:
        self._last_live = payload
        acquisition = payload.get("acquisition", {})
        self.monitor_status.set(f"已采集 {acquisition.get('sample_count', 0)} 点｜窗口实时更新")
        names = [item.get("name") for item in payload.get("channels", [])]
        if tuple(self.sensor_combo["values"]) != tuple(names): self.sensor_combo["values"] = names
        if self.sensor_display.get() not in names and names: self.sensor_display.set(names[0])
        for item in self.channel_table.get_children(): self.channel_table.delete(item)
        for channel in payload.get("channels", []):
            self.channel_table.insert("", "end", text=channel.get("name"), values=(_fmt(channel.get("actual_current"), 3), _fmt(channel.get("prediction_current"), 3) if channel.get("prediction_enabled") else "未启用", _fmt(channel.get("rmse"), 3)))
        self._refresh_chart()
        for key in ("window", "layer", "specimen"):
            item = payload.get(key)
            state = item.get("state_label", "等待数据") if item else "等待数据"
            health = item.get("score") if key == "window" and item else item.get("health") if item else None
            self.warning_vars[key]["state"].set(state)
            self.warning_vars[key]["score"].set(f"健康指标：{_fmt(health, 4)}")
        for item in self.evidence_table.get_children(): self.evidence_table.delete(item)
        for layer in payload.get("layers", []):
            agg = layer.get("aggregate") or {}
            self.evidence_table.insert("", "end", values=(f"第{layer.get('display_layer')}层 {layer.get('completed_windows', 0)}/{layer.get('total_windows', 0)}", _fmt(agg.get("health"), 3), agg.get("state_label") or layer.get("status", "")))
        probs = (payload.get("window") or {}).get("type_probabilities", {})
        self.probability_text.delete("1.0", "end")
        for name, value in sorted(probs.items(), key=lambda pair: pair[1], reverse=True):
            self.probability_text.insert("end", f"{name}: {_fmt(100 * value, 1)}%\n")

    def _refresh_chart(self) -> None:
        if not self._last_live: return
        name = self.sensor_display.get()
        channel = next((item for item in self._last_live.get("channels", []) if item.get("name") == name), None)
        self.chart.set_channel(channel)


class TrainingPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: "IntegratedApp") -> None:
        super().__init__(parent, padding=8)
        self.app = app
        self.manager = WebTrainingManager(Path(r"F:\AFP_Training_Models"))
        self.vars: dict[str, tk.Variable] = {}
        self.last_seq = 0
        self.loss_history: list[dict[str, Any]] = []
        self.imported = False
        self._build()
        self._mode_changed()
        self.after(300, self._poll)

    def _v(self, name: str, value: Any = "") -> tk.Variable:
        if isinstance(value, bool): var: tk.Variable = tk.BooleanVar(value=value)
        elif isinstance(value, int): var = tk.IntVar(value=value)
        elif isinstance(value, float): var = tk.DoubleVar(value=value)
        else: var = tk.StringVar(value=value)
        self.vars[name] = var; return var

    def _field(self, parent: tk.Misc, label: str, name: str, value: Any, row: int, col: int, width: int = 13, show: str = "") -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row * 2, column=col, sticky="w", padx=4, pady=(4, 0))
        entry = ttk.Entry(parent, textvariable=self._v(name, value), width=width, show=show)
        entry.grid(row=row * 2 + 1, column=col, sticky="ew", padx=4, pady=(0, 4)); parent.columnconfigure(col, weight=1)
        return entry

    def _build(self) -> None:
        self.columnconfigure(0, weight=1); self.columnconfigure(1, weight=1); self.rowconfigure(0, weight=1)
        left = ScrollFrame(self); left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        right = ttk.Frame(self); right.grid(row=0, column=1, sticky="nsew", padx=(5, 0)); right.columnconfigure(0, weight=1); right.rowconfigure(2, weight=1)
        parent = left.body; parent.columnconfigure(0, weight=1)

        source = ttk.LabelFrame(parent, text="1. 数据导入与统一化", padding=8); source.grid(row=0, column=0, sticky="ew", pady=4)
        for col in range(3): source.columnconfigure(col, weight=1)
        ttk.Label(source, text="数据模式").grid(row=0, column=0, sticky="w", padx=4)
        self.data_mode = ttk.Combobox(source, textvariable=self._v("data_mode", "new"), state="readonly", values=("new", "legacy", "other")); self.data_mode.grid(row=1, column=0, sticky="ew", padx=4); self.data_mode.bind("<<ComboboxSelected>>", lambda _e: self._mode_changed())
        ttk.Label(source, text="数据来源").grid(row=0, column=1, sticky="w", padx=4)
        self.source_kind = ttk.Combobox(source, textvariable=self._v("source_kind", "csv"), state="readonly", values=("csv", "mysql", "complete_csv")); self.source_kind.grid(row=1, column=1, sticky="ew", padx=4); self.source_kind.bind("<<ComboboxSelected>>", lambda _e: self._source_changed())
        ttk.Label(source, text="训练任务").grid(row=0, column=2, sticky="w", padx=4)
        ttk.Combobox(source, textvariable=self._v("training_type", "prediction"), state="readonly", values=("prediction", "prediction_warning")).grid(row=1, column=2, sticky="ew", padx=4)
        self._field(source, "CSV文件夹/完整CSV", "source_path", "", 1, 0, width=36)
        ttk.Button(source, text="浏览数据", command=self._pick_source).grid(row=3, column=1, sticky="ew", padx=4)
        ttk.Button(source, text="导入并预检", command=self.import_data).grid(row=3, column=2, sticky="ew", padx=4)
        self._field(source, "工况列（逗号分隔）", "condition_columns", "", 2, 0, width=30)
        self._field(source, "模型输入列", "input_columns", "", 2, 1, width=30)
        self._field(source, "模型输出列", "output_columns", "", 2, 2, width=30)
        self._field(source, "历史输入步长", "history_length", 24, 3, 0)
        self._field(source, "未来预测步长", "prediction_length", 24, 3, 1)
        self._field(source, "窗口滑动步长", "stride", 24, 3, 2)

        mysql = ttk.LabelFrame(parent, text="MySQL 来源（选择 MySQL 时使用）", padding=8); mysql.grid(row=1, column=0, sticky="ew", pady=4)
        for col in range(3): mysql.columnconfigure(col, weight=1)
        self._field(mysql, "主机", "train_mysql_host", "127.0.0.1", 0, 0)
        self._field(mysql, "端口", "train_mysql_port", 3306, 0, 1)
        self._field(mysql, "用户", "train_mysql_user", "root", 0, 2)
        self._field(mysql, "密码", "train_mysql_password", "", 1, 0, show="*")
        self._field(mysql, "数据库", "train_mysql_database", "afp_state_warning", 1, 1)
        ttk.Label(mysql, text="SQL查询").grid(row=4, column=0, sticky="w", padx=4)
        self.query_text = tk.Text(mysql, height=8, wrap="none"); self.query_text.grid(row=5, column=0, columnspan=3, sticky="ew", padx=4, pady=4)

        train = ttk.LabelFrame(parent, text="2. I-ModernTCN 训练设置", padding=8); train.grid(row=2, column=0, sticky="ew", pady=4)
        for col in range(4): train.columnconfigure(col, weight=1)
        self._field(train, "Epoch", "epochs", 100, 0, 0); self._field(train, "Patience", "patience", 10, 0, 1); self._field(train, "Batch size", "batch_size", 32, 0, 2); self._field(train, "学习率", "learning_rate", 0.0008, 0, 3)
        self._field(train, "权重衰减", "weight_decay", 0.0001, 1, 0); self._field(train, "Dropout", "dropout", 0.05, 1, 1); self._field(train, "最小改进量", "min_delta", 0.000001, 1, 2)
        ttk.Label(train, text="设备").grid(row=2, column=3, sticky="w", padx=4, pady=(4, 0)); ttk.Combobox(train, textvariable=self._v("device", "auto"), state="readonly", values=("auto", "cpu", "cuda")).grid(row=3, column=3, sticky="ew", padx=4)
        self._field(train, "随机种子", "seed", 20260813, 2, 0); self._field(train, "任务名称", "task_name", "AFP训练任务", 2, 1, width=22)
        self._field(train, "已有模型（可留空）", "pretrained_model", "", 3, 0, width=35)
        ttk.Button(train, text="选择已有模型", command=self._pick_pretrained).grid(row=7, column=1, sticky="ew", padx=4)
        self._field(train, "模型及整合CSV保存位置", "output_root", r"F:\AFP_Training_Models", 4, 0, width=38)
        ttk.Button(train, text="选择保存位置", command=self._pick_output).grid(row=9, column=1, sticky="ew", padx=4)
        self.train_button = ttk.Button(train, text="开始训练", command=self.toggle_training, state="disabled"); self.train_button.grid(row=9, column=2, columnspan=2, sticky="ew", padx=4)
        self.precheck = tk.StringVar(value="尚未导入数据。")
        ttk.Label(parent, textvariable=self.precheck, wraplength=620, foreground=COLORS["muted"]).grid(row=3, column=0, sticky="ew", pady=6)

        stats = ttk.LabelFrame(right, text="数据概览", padding=8); stats.grid(row=0, column=0, sticky="ew")
        self.stat_vars = {name: tk.StringVar(value="—") for name in ("rows", "conditions", "retained", "rejected")}
        for col, (name, label) in enumerate((("rows", "数据行数"), ("conditions", "工况种数"), ("retained", "保留文件"), ("rejected", "剔除文件"))):
            frame = ttk.Frame(stats); frame.grid(row=0, column=col, sticky="ew", padx=5); stats.columnconfigure(col, weight=1)
            ttk.Label(frame, text=label, foreground=COLORS["muted"]).pack(); ttk.Label(frame, textvariable=self.stat_vars[name], font=("Segoe UI", 15, "bold"), foreground=COLORS["navy"]).pack()
        progress = ttk.LabelFrame(right, text="训练过程", padding=8); progress.grid(row=1, column=0, sticky="ew", pady=7); progress.columnconfigure(1, weight=1)
        self.stage_var = tk.StringVar(value="等待导入"); self.epoch_var = tk.StringVar(value="0 / 0"); self.patience_var = tk.StringVar(value="0 / 0"); self.best_loss_var = tk.StringVar(value="—")
        for row, (label, var) in enumerate((("阶段", self.stage_var), ("Epoch", self.epoch_var), ("Patience", self.patience_var), ("最佳验证损失", self.best_loss_var))): ttk.Label(progress, text=label).grid(row=row, column=0, sticky="w"); ttk.Label(progress, textvariable=var, foreground=COLORS["navy"]).grid(row=row, column=1, sticky="e")
        self.epoch_bar = ttk.Progressbar(progress, maximum=100); self.epoch_bar.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(5, 2))
        self.patience_bar = ttk.Progressbar(progress, maximum=100); self.patience_bar.grid(row=5, column=0, columnspan=2, sticky="ew", pady=2)
        self.loss_chart = LossChart(right, height=330); self.loss_chart.grid(row=2, column=0, sticky="nsew")
        outputs = ttk.Notebook(right); outputs.grid(row=3, column=0, sticky="nsew", pady=(7, 0)); right.rowconfigure(3, weight=1)
        preview_tab = ttk.Frame(outputs); log_tab = ttk.Frame(outputs); result_tab = ttk.Frame(outputs)
        outputs.add(preview_tab, text="数据预览"); outputs.add(log_tab, text="训练日志"); outputs.add(result_tab, text="输出文件")
        self.preview = ttk.Treeview(preview_tab, show="headings", height=10); self.preview.pack(fill="both", expand=True)
        self.log = tk.Text(log_tab, wrap="word", background="#10283b", foreground="#d7edfa", font=("Consolas", 9)); self.log.pack(fill="both", expand=True)
        self.result = tk.Text(result_tab, wrap="word"); self.result.pack(fill="both", expand=True)

    def _mode_changed(self) -> None:
        mode = str(self.vars["data_mode"].get())
        columns = default_columns("legacy" if mode == "legacy" else "new")
        for key in ("condition_columns", "input_columns", "output_columns"): self.vars[key].set(",".join(columns[key]))
        self.query_text.delete("1.0", "end"); self.query_text.insert("1.0", default_mysql_query(mode))
        self.imported = False; self.train_button.config(state="disabled")

    def _source_changed(self) -> None:
        self.precheck.set(f"已选择 {self.vars['source_kind'].get()} 数据源，请导入并预检。")

    def _pick_source(self) -> None:
        if self.vars["source_kind"].get() == "complete_csv": selected = filedialog.askopenfilename(title="选择完整CSV", filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")])
        else: selected = filedialog.askdirectory(title="选择采集数据文件夹")
        if selected: self.vars["source_path"].set(selected)

    def _pick_pretrained(self) -> None:
        selected = filedialog.askopenfilename(title="选择已有预测模型", filetypes=[("PyTorch模型", "*.pth *.pt"), ("所有文件", "*.*")])
        if selected: self.vars["pretrained_model"].set(selected)

    def _pick_output(self) -> None:
        selected = filedialog.askdirectory(title="选择模型保存位置", initialdir=str(self.vars["output_root"].get()))
        if selected: self.vars["output_root"].set(selected)

    def import_data(self) -> None:
        payload = {"source": str(self.vars["source_kind"].get()), "path": str(self.vars["source_path"].get()), "data_mode": str(self.vars["data_mode"].get()), "condition_columns": str(self.vars["condition_columns"].get()), "input_columns": str(self.vars["input_columns"].get()), "output_columns": str(self.vars["output_columns"].get()), "history_length": _safe_int(self.vars["history_length"].get(), 24, 1), "prediction_length": _safe_int(self.vars["prediction_length"].get(), 24, 1), "stride": _safe_int(self.vars["stride"].get(), 24, 1), "query": self.query_text.get("1.0", "end").strip(), "mysql": {"host": str(self.vars["train_mysql_host"].get()), "port": _safe_int(self.vars["train_mysql_port"].get(), 3306, 1), "user": str(self.vars["train_mysql_user"].get()), "password": str(self.vars["train_mysql_password"].get()), "database": str(self.vars["train_mysql_database"].get())}}
        self.precheck.set("正在读取、筛选并整合数据……")
        def work() -> None:
            try:
                result = self.manager.import_source(payload); self.after(0, lambda: self._import_done(result))
            except Exception as exc: self.after(0, lambda e=str(exc): (self.precheck.set("导入失败：" + e), messagebox.showerror("导入失败", e)))
        threading.Thread(target=work, daemon=True).start()

    def _import_done(self, result: dict[str, Any]) -> None:
        validation = result.get("validation", {}); self.imported = bool(validation.get("ok"))
        self.stat_vars["rows"].set(str(validation.get("rows", "—"))); self.stat_vars["conditions"].set(str(validation.get("conditions", "—"))); self.stat_vars["retained"].set(str(validation.get("retained_files", validation.get("accepted_files", "—")))); self.stat_vars["rejected"].set(str(validation.get("rejected_files", 0)))
        self.precheck.set(("预检通过" if self.imported else "预检未通过") + f"：{validation.get('rows', 0)} 行，{validation.get('conditions', 0)} 种工况，{validation.get('specimens', 0)} 个试样，{validation.get('layers', 0)} 个铺层。" + (" 可训练预测预警模型。" if validation.get("warning_ready") else " 当前标签仅支持预测模型训练。"))
        self.train_button.config(state="normal" if self.imported else "disabled")
        columns = result.get("preview_columns", [])[:18]
        self.preview.delete(*self.preview.get_children()); self.preview["columns"] = columns
        for column in columns: self.preview.heading(column, text=column); self.preview.column(column, width=105)
        for row in result.get("preview", []): self.preview.insert("", "end", values=[row.get(column, "") for column in columns])

    def _settings(self) -> dict[str, Any]:
        return {"training_type": str(self.vars["training_type"].get()), "epochs": _safe_int(self.vars["epochs"].get(), 100, 1), "patience": _safe_int(self.vars["patience"].get(), 10, 1), "batch_size": _safe_int(self.vars["batch_size"].get(), 32, 1), "learning_rate": _safe_float(self.vars["learning_rate"].get(), 0.0008), "weight_decay": _safe_float(self.vars["weight_decay"].get(), 0.0001), "dropout": _safe_float(self.vars["dropout"].get(), 0.05), "min_delta": _safe_float(self.vars["min_delta"].get(), 0.000001), "device": str(self.vars["device"].get()), "seed": _safe_int(self.vars["seed"].get(), 20260813), "task_name": str(self.vars["task_name"].get()), "output_root": str(self.vars["output_root"].get()), "pretrained_model": str(self.vars["pretrained_model"].get()).strip()}

    def toggle_training(self) -> None:
        if self.manager.running:
            self.manager.stop(); self.train_button.config(text="正在结束并保存……", state="disabled"); return
        try:
            self.loss_history.clear(); self.loss_chart.set_history([]); self.result.delete("1.0", "end")
            self.manager.start(self._settings()); self.train_button.config(text="结束并保存", state="normal"); self.app.set_runtime("模型训练中")
        except Exception as exc: messagebox.showerror("无法开始训练", str(exc))

    def _poll(self) -> None:
        status = self.manager.status(self.last_seq); self.last_seq = int(status.get("last_seq", self.last_seq))
        progress = status.get("progress", {}); stages = {"idle": "等待导入", "preparing": "准备与标准化", "prediction_training": "预测模型训练", "warning_training": "预警模型训练", "finished": "训练完成", "stopped": "已停止并保存", "error": "训练失败"}
        self.stage_var.set(stages.get(progress.get("stage"), str(progress.get("stage", "—"))))
        epoch, epochs = int(progress.get("epoch", 0)), int(progress.get("epochs", 0)); bad, patience = int(progress.get("bad_epochs", 0)), int(progress.get("patience", 0))
        self.epoch_var.set(f"{epoch} / {epochs}"); self.patience_var.set(f"{bad} / {patience}"); self.best_loss_var.set(_fmt(progress.get("best_validation_loss"), 7)); self.epoch_bar["value"] = 100 * epoch / max(1, epochs); self.patience_bar["value"] = 100 * bad / max(1, patience)
        for event in status.get("events", []):
            self.log.insert("end", f"[{event.get('time')}] {event.get('event')}  {json.dumps(event, ensure_ascii=False, default=str)}\n"); self.log.see("end")
            if event.get("event") == "epoch_progress": self.loss_history.append(event); self.loss_chart.set_history(self.loss_history)
        if not status.get("running") and self.train_button.cget("text") != "开始训练":
            self.train_button.config(text="开始训练", state="normal" if self.imported else "disabled"); self.app.set_runtime("就绪")
        result = status.get("result")
        if result and not self.result.get("1.0", "end").strip():
            self.result.insert("end", f"任务目录：{result.get('task_dir')}\n整合CSV：{result.get('complete_csv')}\n预测模型：{result.get('checkpoint')}\n")
            if result.get("warning"): self.result.insert("end", f"预警模型：{result['warning'].get('selected_model')}\n验证平衡准确率：{_fmt(result['warning'].get('validation_balanced_accuracy'), 4)}\n")
        if status.get("error"): self.result.delete("1.0", "end"); self.result.insert("end", "训练失败：" + str(status["error"]))
        self.after(300, self._poll)


class IntegratedApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("AFP 数据采集、实时预测预警与模型训练系统")
        self.root.geometry("1540x930")
        self.root.minsize(1180, 720)
        try: self.root.state("zoomed")
        except tk.TclError: pass
        style = ttk.Style(self.root)
        if "vista" in style.theme_names(): style.theme_use("vista")
        style.configure("TNotebook.Tab", padding=(18, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"), foreground=COLORS["navy"])
        header = tk.Frame(self.root, background=COLORS["navy"], height=58)
        header.pack(fill="x")
        tk.Label(header, text="AFP 智能制造状态监测系统", background=COLORS["navy"], foreground="white", font=("Microsoft YaHei UI", 18, "bold")).pack(side="left", padx=18, pady=12)
        self.runtime = tk.StringVar(value="正在初始化")
        tk.Label(header, textvariable=self.runtime, background=COLORS["navy"], foreground="#bfe8f5", font=("Microsoft YaHei UI", 11)).pack(side="right", padx=20)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        self.acquisition_panel = AcquisitionPanel(self.notebook, self)
        self.training_panel = TrainingPanel(self.notebook, self)
        self.notebook.add(self.acquisition_panel, text="实时采集 · 预测 · 预警")
        self.notebook.add(self.training_panel, text="模型训练中心")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self._load_dashboard, daemon=True).start()

    def set_runtime(self, value: str) -> None: self.runtime.set("运行状态：" + value)

    def _load_dashboard(self) -> None:
        try:
            from app import DashboardData
            dashboard = DashboardData(); bootstrap = dashboard.bootstrap()
            self.root.after(0, lambda: (self.acquisition_panel.attach_dashboard(dashboard, bootstrap), self.set_runtime("就绪")))
        except Exception:
            details = traceback.format_exc()
            try: (Path.home() / "AFP_native_startup_error.log").write_text(details, encoding="utf-8")
            except Exception: pass
            self.root.after(0, lambda: (self.set_runtime("初始化失败"), messagebox.showerror("初始化失败", details[-4000:])))

    def close(self) -> None:
        try:
            if self.acquisition_panel.dashboard and self.acquisition_panel.dashboard.acquisition.status().get("running"):
                if not messagebox.askyesno("确认退出", "采集仍在运行。是否停止、保存并退出？"): return
                self.acquisition_panel.dashboard.acquisition.stop()
            if self.training_panel.manager.running:
                if not messagebox.askyesno("确认退出", "训练仍在运行。是否请求停止并保存后退出？"): return
                self.training_panel.manager.stop()
        finally: self.root.destroy()

    def run(self) -> None: self.root.mainloop()


def main() -> None:
    if "--self-test" in sys.argv:
        for path in (APP_DIR / "data", APP_DIR / "new_collection_demo_v11_3"):
            if not path.exists(): raise FileNotFoundError(path)
        print("native-integrated-self-test: ok")
        return
    if "--integration-smoke" in sys.argv:
        from app import DashboardData
        from web_training_pipeline import import_unified, train_models

        report_root = (
            Path(sys.executable).resolve().parent / "verification"
            if getattr(sys, "frozen", False)
            else Path(tempfile.gettempdir()) / "AFP_Integrated_Native_Verification"
        )
        report_root.mkdir(parents=True, exist_ok=True)
        dashboard = DashboardData()
        bootstrap = dashboard.bootstrap()
        schema = "new_collection_v11_3"
        sensors = list(ACQUISITION_SCHEMAS[schema]["sensors"])
        profile = dashboard.best_prediction_profile(schema)
        inputs = [name for name in profile["input_sensors"] if name in sensors]
        outputs = [name for name in profile["output_sensors"] if name in sensors]
        source_file = bootstrap["acquisition"]["new_collection_demo"]["source_file"]
        config = AcquisitionConfig(
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
            specimen_id="PACKAGED_SMOKE",
            condition_id="SMOKE",
            layer=0,
            replicate=1,
            save_root=str(report_root / "captured"),
        )
        dashboard.validate_prediction_setup(config, load_model=True)
        dashboard.acquisition.start(config)
        deadline = time.time() + 30.0
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
        columns = default_columns("new")
        unified = import_unified(
            {"kind": "csv", "path": str(APP_DIR / "new_collection_demo_v11_3")},
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
        training = train_models(
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
                "seed": 20260813,
                "task_name": "packaged_integration_smoke",
            },
            report_root / "training",
            threading.Event(),
            lambda _event, **_payload: None,
        )
        report = {
            "ok": True,
            "server_free": True,
            "acquisition_samples": stopped["sample_count"],
            "window_state": live["window"]["state_label"],
            "layer_state": (live.get("layer") or {}).get("state_label"),
            "specimen_state": (live.get("specimen") or {}).get("state_label"),
            "training_validation": unified.validation,
            "training": training,
        }
        (report_root / "integration_smoke_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return
    IntegratedApp().run()


if __name__ == "__main__":
    main()
