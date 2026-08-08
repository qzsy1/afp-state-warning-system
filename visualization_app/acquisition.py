from __future__ import annotations

import csv
import hashlib
import json
import math
import socket
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


APP_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = APP_DIR.parents[1]
DEFAULT_CAPTURE_ROOT = Path(WORKSPACE_DIR.anchor) / "AFP_Capture"
_PACKAGED_SIMULATOR_FILE = APP_DIR / "data" / "原文件.csv"
DEFAULT_SIMULATOR_FILE = (
    _PACKAGED_SIMULATOR_FILE
    if _PACKAGED_SIMULATOR_FILE.exists()
    else WORKSPACE_DIR / "原文件.csv"
)

LEGACY_SENSOR_COLUMNS = [
    "转速",
    "位移",
    "温度1",
    "温度2",
    "温度3",
    "温度4",
    "温度5",
    "温度6",
    "温度7",
    "温度8",
    "压力",
    "振动",
]
SENSOR_COLUMNS = LEGACY_SENSOR_COLUMNS
NEW_CORE_SENSOR_COLUMNS = [
    "温度",
    "压力",
    "ROI平均温度",
    "张力",
    "线速度",
    "ABB_X",
    "ABB_Y",
    "ABB_Z",
]
NEW_OPTIONAL_SENSOR_COLUMNS = [
    *[f"温度{index}" for index in range(1, 9)],
    "转速",
    "位移",
    "振动",
]
NEW_COLLECTION_SENSOR_COLUMNS = list(
    dict.fromkeys([*NEW_CORE_SENSOR_COLUMNS, *NEW_OPTIONAL_SENSOR_COLUMNS])
)
ALL_SENSOR_COLUMNS = list(
    dict.fromkeys([*LEGACY_SENSOR_COLUMNS, *NEW_COLLECTION_SENSOR_COLUMNS])
)
PROCESS_PARAMETER_COLUMNS = [
    "initial_compaction_force_N",
    "placement_speed_mm_s",
    "pid_angle_deg",
    "temperature_setpoint_C",
]
ORIGINAL_COLUMNS = [
    "振动",
    "转速",
    "位移",
    "温度1",
    "温度2",
    "温度3",
    "温度4",
    "温度5",
    "温度6",
    "温度7",
    "温度8",
    "压力",
    "cycle",
    "file",
    "root",
    "p",
    "v",
    "pr",
    "l",
    "试件",
]
NEW_COLLECTION_COLUMNS = [
    "时间",
    *NEW_COLLECTION_SENSOR_COLUMNS,
    *PROCESS_PARAMETER_COLUMNS,
    "run_id",
    "specimen_id",
    "condition_id",
    "replicate",
    "layer_id",
]
ACQUISITION_SCHEMAS = {
    "legacy_original": {
        "label": "旧数据兼容方案（原12传感器）",
        "sensors": LEGACY_SENSOR_COLUMNS,
        "raw_columns": ORIGINAL_COLUMNS,
    },
    "new_collection_v11_3": {
        "label": "新数据集采集方案 v11.3（19传感器＋4工艺参数）",
        "sensors": NEW_COLLECTION_SENSOR_COLUMNS,
        "raw_columns": NEW_COLLECTION_COLUMNS,
    },
}
ALIASES = {
    "rotation": "转速",
    "rotation_speed": "转速",
    "speed_sensor": "转速",
    "displacement": "位移",
    "pressure": "压力",
    "compaction": "压力",
    "vibration": "振动",
    "temperature": "温度",
    "roi_temperature": "ROI平均温度",
    "tension": "张力",
    "line_speed": "线速度",
    "abb_x": "ABB_X",
    "abb_y": "ABB_Y",
    "abb_z": "ABB_Z",
    **{f"temperature_{index}": f"温度{index}" for index in range(1, 9)},
    **{f"temp{index}": f"温度{index}" for index in range(1, 9)},
}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_sample(payload: dict[str, Any]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for key, value in payload.items():
        target = ALIASES.get(str(key), str(key))
        if target in ALL_SENSOR_COLUMNS:
            number = _finite(value)
            if number is not None:
                normalized[target] = number
    return normalized


@dataclass
class AcquisitionConfig:
    processing_mode: str = "prediction_warning"
    dataset_schema: str = "legacy_original"
    use_best_prediction_override: bool = False
    driver: str = "simulator"
    endpoint: str = ""
    baudrate: int = 115200
    sample_rate_hz: float = 10.0
    selected_sensors: list[str] | None = None
    prediction_sensors: list[str] | None = None
    model_input_sensors: list[str] | None = None
    model_output_sensors: list[str] | None = None
    prediction_model_file: str = ""
    health_indicator: str = "TC-HI"
    run_id: str = "LIVE_RUN"
    specimen_id: str = "LIVE_SPECIMEN"
    condition_id: str = "LIVE"
    layer: int = 0
    cycle: int = 1
    p: float = 600.0
    v: float = 100.0
    pr: float = 600.0
    root: str = "LIVE"
    source_file: str = ""
    save_root: str = ""
    initial_compaction_force_N: float = 400.0
    placement_speed_mm_s: float = 80.0
    pid_angle_deg: float = 5.0
    temperature_setpoint_C: float = 360.0
    replicate: int = 1

    def __post_init__(self) -> None:
        if self.processing_mode not in {"capture_only", "prediction_warning"}:
            raise ValueError(
                "processing_mode必须是capture_only或prediction_warning"
            )
        if self.dataset_schema not in ACQUISITION_SCHEMAS:
            raise ValueError(f"未知数据采集方案：{self.dataset_schema}")
        allowed_sensors = list(
            ACQUISITION_SCHEMAS[self.dataset_schema]["sensors"]
        )
        if self.selected_sensors is None:
            self.selected_sensors = allowed_sensors.copy()
        self.selected_sensors = [
            name for name in self.selected_sensors if name in allowed_sensors
        ]
        if not self.selected_sensors:
            raise ValueError("至少选择一个采集传感器")
        if self.model_input_sensors is None:
            self.model_input_sensors = self.selected_sensors.copy()
        self.model_input_sensors = [
            name
            for name in self.model_input_sensors
            if name in allowed_sensors and name in self.selected_sensors
        ]
        if self.prediction_sensors is None:
            self.prediction_sensors = self.selected_sensors.copy()
        self.prediction_sensors = [
            name
            for name in self.prediction_sensors
            if name in allowed_sensors and name in self.selected_sensors
        ]
        if self.model_output_sensors is None:
            self.model_output_sensors = self.prediction_sensors.copy()
        self.model_output_sensors = [
            name
            for name in self.model_output_sensors
            if name in allowed_sensors and name in self.model_input_sensors
        ]
        if self.processing_mode == "capture_only":
            self.prediction_sensors = []
            self.model_input_sensors = []
            self.model_output_sensors = []
            self.prediction_model_file = ""
        # Keep the legacy field as the display/output selection used by /api/live.
        self.prediction_sensors = self.model_output_sensors.copy()
        self.sample_rate_hz = max(0.1, min(float(self.sample_rate_hz), 1000.0))
        self.baudrate = int(self.baudrate)
        self.layer = int(self.layer)
        self.cycle = int(self.cycle)
        self.replicate = int(self.replicate)
        self.use_best_prediction_override = bool(
            self.use_best_prediction_override
        )
        self.initial_compaction_force_N = float(
            self.initial_compaction_force_N
        )
        self.placement_speed_mm_s = float(self.placement_speed_mm_s)
        self.pid_angle_deg = float(self.pid_angle_deg)
        self.temperature_setpoint_C = float(self.temperature_setpoint_C)

    @property
    def schema_sensors(self) -> list[str]:
        return list(ACQUISITION_SCHEMAS[self.dataset_schema]["sensors"])

    @property
    def raw_columns(self) -> list[str]:
        return list(ACQUISITION_SCHEMAS[self.dataset_schema]["raw_columns"])


def _safe_component(value: Any, max_length: int = 40) -> str:
    """Create a readable Windows-safe path component with a bounded length."""
    text = str(value).strip()
    invalid = '<>:"/\\|?*'
    text = "".join("_" if char in invalid or ord(char) < 32 else char for char in text)
    text = text.rstrip(" .") or "未命名"
    if len(text) <= max_length:
        return text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{text[: max_length - 9]}_{digest}"


def _parameter_token(config: AcquisitionConfig) -> str:
    if config.dataset_schema == "new_collection_v11_3":
        return (
            f"F{config.initial_compaction_force_N:g}_"
            f"V{config.placement_speed_mm_s:g}_"
            f"A{config.pid_angle_deg:g}_"
            f"T{config.temperature_setpoint_C:g}"
        )
    return f"p{config.p:g}_v{config.v:g}_pr{config.pr:g}"


def select_capture_folder(initial_path: str = "") -> str:
    """Open the native folder chooser used by the local web/desktop interface."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            parent=root,
            title="选择AFP采集数据保存位置",
            initialdir=initial_path or str(DEFAULT_CAPTURE_ROOT),
            mustexist=False,
        )
        return str(Path(selected).resolve()) if selected else ""
    finally:
        root.destroy()


class SampleDriver:
    def open(self) -> None:
        raise NotImplementedError

    def read_sample(self) -> dict[str, float] | None:
        raise NotImplementedError

    def close(self) -> None:
        return


class SimulatorDriver(SampleDriver):
    def __init__(self, path: Path, sensor_columns: list[str]) -> None:
        self.path = path
        self.sensor_columns = sensor_columns
        self.records: list[dict[str, Any]] = []
        self.index = 0

    def open(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"模拟数据文件不存在：{self.path}")
        try:
            frame = pd.read_csv(self.path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            frame = pd.read_csv(self.path, encoding="gb18030")
        missing = [
            name for name in self.sensor_columns if name not in frame.columns
        ]
        if missing:
            raise ValueError(f"模拟数据缺少传感器列：{missing}")
        self.records = frame[self.sensor_columns].to_dict(orient="records")
        self.index = 0

    def read_sample(self) -> dict[str, float] | None:
        if not self.records:
            return None
        record = self.records[self.index % len(self.records)]
        self.index += 1
        return normalize_sample(record)


class SerialJsonDriver(SampleDriver):
    def __init__(self, endpoint: str, baudrate: int) -> None:
        self.endpoint = endpoint
        self.baudrate = baudrate
        self.serial = None

    def open(self) -> None:
        if not self.endpoint:
            raise ValueError("串口模式必须填写端口，例如 COM3")
        import serial

        self.serial = serial.Serial(
            self.endpoint,
            self.baudrate,
            timeout=0.5,
        )

    def read_sample(self) -> dict[str, float] | None:
        raw = self.serial.readline()
        if not raw:
            return None
        payload = json.loads(raw.decode("utf-8-sig").strip())
        if not isinstance(payload, dict):
            raise ValueError("串口每行必须是JSON对象")
        return normalize_sample(payload)

    def close(self) -> None:
        if self.serial is not None:
            self.serial.close()
            self.serial = None


class TcpJsonDriver(SampleDriver):
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.sock: socket.socket | None = None
        self.file = None

    def open(self) -> None:
        if ":" not in self.endpoint:
            raise ValueError("TCP地址格式必须是 host:port")
        host, port_text = self.endpoint.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port_text)), timeout=2.0)
        self.sock.settimeout(0.5)
        self.file = self.sock.makefile("rb")

    def read_sample(self) -> dict[str, float] | None:
        try:
            raw = self.file.readline()
        except (socket.timeout, TimeoutError):
            return None
        if not raw:
            return None
        payload = json.loads(raw.decode("utf-8-sig").strip())
        if not isinstance(payload, dict):
            raise ValueError("TCP每行必须是JSON对象")
        return normalize_sample(payload)

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def build_driver(config: AcquisitionConfig) -> SampleDriver:
    if config.driver == "simulator":
        path = (
            Path(config.source_file)
            if config.source_file
            else DEFAULT_SIMULATOR_FILE
        )
        return SimulatorDriver(path, list(config.selected_sensors or []))
    if config.driver == "serial_json":
        return SerialJsonDriver(config.endpoint, config.baudrate)
    if config.driver == "tcp_json":
        return TcpJsonDriver(config.endpoint)
    raise ValueError(f"不支持的采集驱动：{config.driver}")


class AcquisitionManager:
    def __init__(self, capture_root: Path = DEFAULT_CAPTURE_ROOT) -> None:
        self.capture_root = capture_root
        self.capture_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.driver: SampleDriver | None = None
        self.config: AcquisitionConfig | None = None
        self.rows: deque[dict[str, Any]] = deque(maxlen=200000)
        self.timestamps: deque[float] = deque(maxlen=200000)
        self.sensor_received = {name: 0 for name in ALL_SENSOR_COLUMNS}
        self.sensor_last_time = {name: None for name in ALL_SENSOR_COLUMNS}
        self.last_error = ""
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.session_dir: Path | None = None
        self.raw_path: Path | None = None
        self.timestamp_path: Path | None = None
        self.capture_record_dir: Path | None = None
        self.full_specimen_path: Path | None = None
        self.completed_layers: list[int] = []
        self.session_stamp = ""

    @staticmethod
    def available_drivers() -> list[dict[str, str]]:
        return [
            {"id": "simulator", "label": "模拟采集（CSV数据源）"},
            {"id": "serial_json", "label": "串口 JSON Lines"},
            {"id": "tcp_json", "label": "TCP JSON Lines"},
        ]

    @staticmethod
    def available_schemas() -> list[dict[str, Any]]:
        return [
            {
                "id": schema_id,
                "label": definition["label"],
                "sensors": list(definition["sensors"]),
                "raw_columns": list(definition["raw_columns"]),
            }
            for schema_id, definition in ACQUISITION_SCHEMAS.items()
        ]

    def test_connection(
        self, config: AcquisitionConfig, timeout_seconds: float = 2.5
    ) -> dict:
        driver = build_driver(config)
        selected = set(config.selected_sensors or [])
        received = {name: 0 for name in ALL_SENSOR_COLUMNS}
        errors: list[str] = []
        started = time.time()
        try:
            driver.open()
            while time.time() - started < timeout_seconds:
                try:
                    sample = driver.read_sample()
                except Exception as exc:
                    errors.append(str(exc))
                    break
                if sample:
                    for name in selected:
                        if name in sample and _finite(sample[name]) is not None:
                            received[name] += 1
                    if selected and all(received[name] > 0 for name in selected):
                        break
                if config.driver == "simulator":
                    time.sleep(min(0.02, 1.0 / config.sample_rate_hz))
        except Exception as exc:
            errors.append(str(exc))
        finally:
            driver.close()
        sensors = [
            {
                "name": name,
                "selected": name in selected,
                "received_samples": int(received[name]),
                "ok": name not in selected or received[name] > 0,
            }
            for name in config.schema_sensors
        ]
        ok = bool(selected) and not errors and all(
            item["ok"] for item in sensors
        )
        return {
            "ok": ok,
            "driver": config.driver,
            "endpoint": config.endpoint,
            "elapsed_seconds": time.time() - started,
            "errors": errors,
            "sensors": sensors,
        }

    def start(self, config: AcquisitionConfig) -> dict:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("采集已经在运行")
            self.config = config
            self.rows.clear()
            self.timestamps.clear()
            self.sensor_received = {
                name: 0 for name in ALL_SENSOR_COLUMNS
            }
            self.sensor_last_time = {
                name: None for name in ALL_SENSOR_COLUMNS
            }
            self.last_error = ""
            self.started_at = time.time()
            self.stopped_at = None
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session_stamp = stamp
            selected_root = (
                Path(config.save_root).expanduser()
                if config.save_root.strip()
                else self.capture_root
            )
            selected_root = selected_root.resolve()
            selected_root.mkdir(parents=True, exist_ok=True)
            safe_specimen = _safe_component(config.specimen_id)
            parameter_token = _parameter_token(config)
            specimen_folder_name = f"{safe_specimen}_{parameter_token}"
            self.session_dir = selected_root / specimen_folder_name
            self.capture_record_dir = self.session_dir / "采集记录"
            self.capture_record_dir.mkdir(parents=True, exist_ok=True)
            file_name = (
                f"{specimen_folder_name}_第{config.layer + 1}层.CSV"
            )
            self.raw_path = self.session_dir / file_name
            self.timestamp_path = self.capture_record_dir / (
                f"{specimen_folder_name}_第{config.layer + 1}层_"
                f"{stamp}_时间戳.csv"
            )
            manifest_path = self.capture_record_dir / (
                f"{specimen_folder_name}_第{config.layer + 1}层_"
                f"{stamp}_采集清单.json"
            )
            if max(len(str(self.raw_path)), len(str(self.timestamp_path))) > 235:
                raise ValueError(
                    "保存路径过长。请改选更短的保存根目录，或缩短试样名。"
                )
            self.driver = build_driver(config)
            self.driver.open()
            self._archive_existing(self.raw_path, "分层数据")
            manifest_path.write_text(
                json.dumps(
                    {
                        **asdict(config),
                        "started_at": datetime.now().isoformat(timespec="milliseconds"),
                        "raw_encoding": "gb18030",
                        "raw_columns": config.raw_columns,
                        "selected_save_root": str(selected_root),
                        "specimen_folder": str(self.session_dir),
                        "layer_file": str(self.raw_path),
                        "timestamp_file": str(self.timestamp_path),
                        "whole_specimen_rule": (
                            f"{specimen_folder_name}_完整试样_已采N层.CSV"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._run,
                name="AFP-Acquisition",
                daemon=True,
            )
            self.thread.start()
        return self.status()

    def _archive_existing(self, path: Path, category: str) -> None:
        if not path.exists():
            return
        history_dir = self.session_dir / "历史版本" / category
        history_dir.mkdir(parents=True, exist_ok=True)
        archived = history_dir / (
            f"{path.stem}_{self.session_stamp}{path.suffix}"
        )
        counter = 2
        while archived.exists():
            archived = history_dir / (
                f"{path.stem}_{self.session_stamp}_{counter}{path.suffix}"
            )
            counter += 1
        path.replace(archived)

    def _rebuild_whole_specimen(self) -> Path | None:
        if self.config is None or self.session_dir is None:
            return None
        safe_specimen = _safe_component(self.config.specimen_id)
        specimen_folder_name = f"{safe_specimen}_{_parameter_token(self.config)}"
        layer_paths = [
            self.session_dir
            / f"{specimen_folder_name}_第{layer + 1}层.CSV"
            for layer in range(5)
        ]
        available = [
            (layer, path)
            for layer, path in enumerate(layer_paths)
            if path.exists() and path.stat().st_size > 0
        ]
        self.completed_layers = [layer for layer, _ in available]
        if not available:
            return None
        combined_path = self.session_dir / (
            f"{specimen_folder_name}_完整试样_已采{len(available)}层.CSV"
        )
        self._archive_existing(combined_path, "完整试样快照")
        with combined_path.open(
            "w", encoding="gb18030", newline=""
        ) as output_file:
            writer = csv.DictWriter(
                output_file, fieldnames=self.config.raw_columns
            )
            writer.writeheader()
            for _, layer_path in available:
                with layer_path.open(
                    "r", encoding="gb18030", newline=""
                ) as layer_file:
                    reader = csv.DictReader(layer_file)
                    if reader.fieldnames != self.config.raw_columns:
                        raise ValueError(
                            f"分层文件列名与原始格式不一致：{layer_path.name}"
                        )
                    writer.writerows(reader)
        self.full_specimen_path = combined_path
        return combined_path

    def _run(self) -> None:
        config = self.config
        selected = set(config.selected_sensors or [])
        try:
            with self.raw_path.open(
                "w", encoding="gb18030", newline=""
            ) as raw_file, self.timestamp_path.open(
                "w", encoding="utf-8-sig", newline=""
            ) as time_file:
                writer = csv.DictWriter(
                    raw_file, fieldnames=config.raw_columns
                )
                timestamp_writer = csv.DictWriter(
                    time_file,
                    fieldnames=[
                        "row_index",
                        "timestamp_iso",
                        "timestamp_unix",
                    ],
                )
                writer.writeheader()
                timestamp_writer.writeheader()
                next_deadline = time.perf_counter()
                while not self.stop_event.is_set():
                    try:
                        sample = self.driver.read_sample()
                    except Exception as exc:
                        self.last_error = str(exc)
                        break
                    if sample:
                        now = time.time()
                        row_index = len(self.rows)
                        row = {
                            name: (
                                sample.get(name, "")
                                if name in selected
                                else ""
                            )
                            for name in config.schema_sensors
                        }
                        if config.dataset_schema == "new_collection_v11_3":
                            row.update(
                                {
                                    "时间": datetime.fromtimestamp(now).isoformat(
                                        timespec="milliseconds"
                                    ),
                                    "initial_compaction_force_N": (
                                        config.initial_compaction_force_N
                                    ),
                                    "placement_speed_mm_s": (
                                        config.placement_speed_mm_s
                                    ),
                                    "pid_angle_deg": config.pid_angle_deg,
                                    "temperature_setpoint_C": (
                                        config.temperature_setpoint_C
                                    ),
                                    "run_id": config.run_id,
                                    "specimen_id": config.specimen_id,
                                    "condition_id": config.condition_id,
                                    "replicate": config.replicate,
                                    "layer_id": config.layer + 1,
                                }
                            )
                        else:
                            row.update(
                                {
                                    "cycle": config.cycle,
                                    "file": self.raw_path.name,
                                    "root": config.root,
                                    "p": config.p,
                                    "v": config.v,
                                    "pr": config.pr,
                                    "l": config.layer,
                                    "试件": config.specimen_id,
                                }
                            )
                        writer.writerow(row)
                        timestamp_writer.writerow(
                            {
                                "row_index": row_index,
                                "timestamp_iso": datetime.fromtimestamp(now).isoformat(
                                    timespec="milliseconds"
                                ),
                                "timestamp_unix": f"{now:.6f}",
                            }
                        )
                        raw_file.flush()
                        time_file.flush()
                        with self.lock:
                            self.rows.append(row)
                            self.timestamps.append(now)
                            for name in selected:
                                if _finite(row.get(name)) is not None:
                                    self.sensor_received[name] += 1
                                    self.sensor_last_time[name] = now
                    if config.driver == "simulator":
                        next_deadline += 1.0 / config.sample_rate_hz
                        self.stop_event.wait(
                            max(0.0, next_deadline - time.perf_counter())
                        )
        except Exception as exc:
            self.last_error = str(exc)
        finally:
            try:
                if self.driver is not None:
                    self.driver.close()
            finally:
                self.stopped_at = time.time()

    def stop(self) -> dict:
        self.stop_event.set()
        thread = self.thread
        if thread is not None:
            thread.join(timeout=3.0)
        with self.lock:
            if self.session_dir is not None:
                full_specimen_path = self._rebuild_whole_specimen()
                summary = {
                    "stopped_at": datetime.now().isoformat(timespec="milliseconds"),
                    "sample_count": len(self.rows),
                    "last_error": self.last_error,
                    "layer_file": str(self.raw_path),
                    "whole_specimen_file": (
                        str(full_specimen_path)
                        if full_specimen_path is not None
                        else None
                    ),
                    "completed_layers": [
                        layer + 1 for layer in self.completed_layers
                    ],
                    "timestamp_file": str(self.timestamp_path),
                    "sensor_received": self.sensor_received,
                }
                summary_path = self.capture_record_dir / (
                    f"{self.raw_path.stem}_{self.session_stamp}_采集摘要.json"
                )
                summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        return self.status()

    def status(self) -> dict:
        with self.lock:
            now = time.time()
            running = self.thread is not None and self.thread.is_alive()
            selected = set(
                self.config.selected_sensors
                if self.config is not None
                else []
            )
            sensors = []
            available_sensors = (
                self.config.schema_sensors
                if self.config is not None
                else LEGACY_SENSOR_COLUMNS
            )
            for name in available_sensors:
                last = self.sensor_last_time[name]
                age = None if last is None else now - float(last)
                sensors.append(
                    {
                        "name": name,
                        "selected": name in selected,
                        "received_samples": int(self.sensor_received[name]),
                        "last_sample_age_seconds": age,
                        "ok": (
                            name not in selected
                            or (
                                self.sensor_received[name] > 0
                                and (
                                    not running
                                    or (age is not None and age <= 2.0)
                                )
                            )
                        ),
                    }
                )
            model_inputs = (
                self.config.model_input_sensors
                if self.config is not None
                else []
            )
            model_ready = bool(model_inputs) and all(
                name in selected
                and self.sensor_received[name] >= 24
                and self.sensor_last_time[name] is not None
                for name in model_inputs
            )
            return {
                "running": running,
                "sample_count": len(self.rows),
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "last_error": self.last_error,
                "session_dir": str(self.session_dir) if self.session_dir else None,
                "save_root": (
                    str(self.session_dir.parent)
                    if self.session_dir is not None
                    else None
                ),
                "raw_file": str(self.raw_path) if self.raw_path else None,
                "layer_file": str(self.raw_path) if self.raw_path else None,
                "full_specimen_file": (
                    str(self.full_specimen_path)
                    if self.full_specimen_path
                    else None
                ),
                "completed_layers": [
                    layer + 1 for layer in self.completed_layers
                ],
                "timestamp_file": (
                    str(self.timestamp_path) if self.timestamp_path else None
                ),
                "model_ready": model_ready,
                "minimum_prediction_points": 24,
                "minimum_warning_points": 48,
                "sensors": sensors,
                "config": asdict(self.config) if self.config else None,
            }

    def numeric_matrix(self) -> tuple[list[dict[str, Any]], list[float]]:
        with self.lock:
            return list(self.rows), list(self.timestamps)
