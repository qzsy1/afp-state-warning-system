"""Training-center data import, normalization, and specimen-safe validation.

The module deliberately keeps the import contract independent from the UI. It
accepts Excel/CSV files or a MySQL query and writes a reproducible manifest
whose rows point to per-layer CSV files, matching the existing I-ModernTCN
training entry point.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "time", "时间", "时刻", "采样时间"),
    "specimen_id": ("specimen_id", "specimen", "试样", "试件", "样件", "sample_id"),
    "layer_id": ("layer_id", "layer", "铺层", "层数", "铺层号", "layer_no"),
    "repeat_id": ("repeat_id", "repeat", "重复", "独立重复", "重复次数"),
    "condition_id": ("condition_id", "condition", "工况", "工艺参数组合", "工况编号"),
    "state_label": ("state_label", "state", "状态", "健康状态", "label"),
    "abnormal_type": ("abnormal_type", "异常类型", "故障类型", "状态类型"),
}

# These channels remain in the legacy model tensor for compatibility, but the
# current collection plan does not acquire them.  A MySQL query may therefore
# omit them; they are deterministically filled with zero rather than reported
# as a mysterious missing training field.
OPTIONAL_ZERO_CHANNELS = ("转速", "位移", "振动")


@dataclass
class ImportResult:
    frame: pd.DataFrame
    source_description: str
    source_files: list[str]
    warnings: list[str]


def _expand_json_fields(frame: pd.DataFrame) -> pd.DataFrame:
    """Expand the JSON columns used by the acquisition MySQL schema.

    The training center also accepts an already flattened SQL result.  When a
    user selects ``afp_flat_all`` directly, however, the sensor and process
    values are stored in ``sensor_json``/``process_json``.  Expanding them at
    the import boundary makes both query styles equivalent.
    """
    out = frame.copy()
    for source in ("sensor_json", "process_json"):
        if source not in out.columns:
            continue
        values: list[dict[str, Any]] = []
        for value in out[source]:
            if isinstance(value, dict):
                values.append(value)
                continue
            try:
                decoded = json.loads(value) if isinstance(value, str) else {}
            except Exception:
                decoded = {}
            values.append(decoded if isinstance(decoded, dict) else {})
        expanded = pd.DataFrame(values, index=out.index)
        for column in expanded.columns:
            if column not in out.columns:
                out[column] = expanded[column]
    return out


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
        sheets = pd.read_excel(path, sheet_name=None)
        frames = []
        for sheet, frame in sheets.items():
            if not frame.empty:
                copy = frame.copy()
                copy["__source_sheet__"] = str(sheet)
                frames.append(copy)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)
    errors: list[str] = []
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except Exception as exc:
            errors.append(f"{encoding}: {exc}")
    raise ValueError(f"无法读取文件 {path}: {' | '.join(errors)}")


def read_excel_or_folder(path: str | Path) -> ImportResult:
    root = Path(path).expanduser().resolve()
    files = [root] if root.is_file() else sorted(
        p for p in root.rglob("*") if p.suffix.lower() in {".csv", ".xlsx", ".xls", ".xlsm"}
    )
    if not files:
        raise FileNotFoundError(f"没有找到 Excel/CSV 文件：{root}")
    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    for file in files:
        frame = _read_table(file)
        if frame.empty:
            warnings.append(f"空文件已跳过：{file.name}")
            continue
        frame["__source_file__"] = str(file)
        frames.append(frame)
    if not frames:
        raise ValueError("所有输入文件均为空")
    return ImportResult(pd.concat(frames, ignore_index=True), f"Excel/CSV：{root}", [str(x) for x in files], warnings)


def read_mysql(connection: dict[str, Any], query: str) -> ImportResult:
    try:
        import mysql.connector
    except ImportError as exc:
        raise RuntimeError("未安装 mysql-connector-python，无法读取 MySQL") from exc
    config = {
        "host": connection.get("host", "127.0.0.1"),
        "port": int(connection.get("port", 3306)),
        "user": connection.get("user", "root"),
        "password": connection.get("password", ""),
        "database": connection.get("database", ""),
    }
    conn = mysql.connector.connect(**config)
    try:
        frame = pd.read_sql(query, conn)
    finally:
        conn.close()
    frame = _expand_json_fields(frame)
    frame["__source_query__"] = query
    return ImportResult(frame, f"MySQL：{config['host']}:{config['port']}/{config['database']}", [query], [])


def guess_mapping(columns: list[str]) -> dict[str, str]:
    lower = {str(c).strip().lower(): str(c) for c in columns}
    mapping: dict[str, str] = {}
    for canonical, aliases in CANONICAL_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lower:
                mapping[canonical] = lower[alias.lower()]
                break
        if canonical not in mapping:
            for col in columns:
                if any(alias.lower() in str(col).lower() for alias in aliases):
                    mapping[canonical] = str(col)
                    break
    return mapping


def _from_filename(path: str) -> dict[str, str]:
    stem = Path(path).stem
    layer = re.search(r"(?:layer|铺层|第)\s*[_-]?(\d+)", stem, re.I)
    repeat = re.search(r"(?:repeat|重复|独立)\s*[_-]?(\d+)", stem, re.I)
    return {
        "layer_id": layer.group(1) if layer else "1",
        "repeat_id": repeat.group(1) if repeat else "1",
        "specimen_id": stem,
        "condition_id": Path(path).parent.name,
    }


def normalize_frame(frame: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    out = frame.copy()
    for canonical, source in mapping.items():
        if source and source in out.columns:
            out[canonical] = out[source]
    source_col = "__source_file__" if "__source_file__" in out.columns else None
    inferred = out[source_col].map(_from_filename) if source_col else None
    for name in ("specimen_id", "layer_id", "repeat_id", "condition_id"):
        if name not in out.columns:
            out[name] = inferred.map(lambda x, key=name: x[key]) if inferred is not None else "unknown"
    if "state_label" not in out.columns:
        out["state_label"] = 0
    if "abnormal_type" not in out.columns:
        out["abnormal_type"] = np.where(pd.to_numeric(out["state_label"], errors="coerce").fillna(0).eq(0), "normal", "unknown")
    if "timestamp" not in out.columns:
        out["timestamp"] = np.arange(len(out), dtype=float)
    for name in ("specimen_id", "layer_id", "repeat_id", "condition_id", "abnormal_type"):
        out[name] = out[name].astype(str)
    out["state_label"] = pd.to_numeric(out["state_label"], errors="coerce").fillna(0).astype(int)
    # Existing new-collection files use the compact manifest names while the
    # trainer expects the four process columns below. Preserve source values
    # when present and otherwise derive constant process values per group.
    aliases = {
        "initial_compaction_force_N": ("initial_compaction_force_N", "initial_compaction_force", "压实力", "compaction_force"),
        "placement_speed_mm_s": ("placement_speed_mm_s", "placement_speed", "铺放速度", "速度"),
        "pid_angle_deg": ("pid_angle_deg", "angle_deg", "铺放角度", "角度"),
        "temperature_setpoint_C": ("temperature_setpoint_C", "temperature_setpoint", "温度设定", "设定温度"),
    }
    for canonical, names in aliases.items():
        if canonical not in out.columns:
            match = next((col for col in out.columns if str(col).lower() in {name.lower() for name in names}), None)
            out[canonical] = pd.to_numeric(out[match], errors="coerce") if match else 0.0
    for column in OPTIONAL_ZERO_CHANNELS:
        if column not in out.columns:
            out[column] = 0.0
    return out


def validate_frame(frame: pd.DataFrame, sensor_columns: list[str], process_columns: list[str]) -> dict[str, Any]:
    required = ["specimen_id", "layer_id", "condition_id", "state_label", *sensor_columns, *process_columns]
    missing = [col for col in required if col not in frame.columns]
    numeric = [col for col in [*sensor_columns, *process_columns] if col in frame.columns]
    numeric_frame = frame[numeric].apply(pd.to_numeric, errors="coerce") if numeric else pd.DataFrame()
    missing_rates = numeric_frame.isna().mean().sort_values(ascending=False).to_dict() if not numeric_frame.empty else {}
    nonfinite = int((~np.isfinite(numeric_frame.to_numpy(dtype=float))).sum()) if not numeric_frame.empty else 0
    specimens = frame["specimen_id"].nunique() if "specimen_id" in frame else 0
    layers = frame[["specimen_id", "layer_id"]].drop_duplicates().shape[0] if {"specimen_id", "layer_id"}.issubset(frame.columns) else 0
    leakage = frame.groupby("specimen_id")["condition_id"].nunique().gt(1).sum() if {"specimen_id", "condition_id"}.issubset(frame.columns) else 0
    layer_sizes = (
        frame.groupby(["specimen_id", "layer_id"], dropna=False).size()
        if {"specimen_id", "layer_id"}.issubset(frame.columns)
        else pd.Series(dtype=int)
    )
    short_layers = int((layer_sizes < 48).sum())
    state_counts = frame["state_label"].value_counts().sort_index().astype(int).to_dict() if "state_label" in frame else {}
    return {
        "rows": int(len(frame)), "columns": int(len(frame.columns)), "specimens": int(specimens),
        "layers": int(layers), "missing_required": missing, "missing_rates": missing_rates,
        "nonfinite_values": nonfinite, "specimen_condition_conflicts": int(leakage),
        "states": state_counts,
        "short_layers": short_layers,
        "minimum_points_per_layer": 48,
        "has_normal_and_abnormal": bool(0 in state_counts and any(int(k) != 0 for k in state_counts)),
        "conditions": int(frame["condition_id"].nunique()) if "condition_id" in frame else 0,
        "ok": not missing and nonfinite == 0 and leakage == 0 and specimens >= 2 and short_layers == 0 and bool(0 in state_counts and any(int(k) != 0 for k in state_counts)),
    }


def write_manifest_dataset(frame: pd.DataFrame, output: str | Path, sensor_columns: list[str], process_columns: list[str]) -> Path:
    root = Path(output).expanduser().resolve()
    if root.exists():
        shutil.rmtree(root)
    data_dir = root / "layers"
    data_dir.mkdir(parents=True, exist_ok=True)
    work = frame.sort_values(["specimen_id", "layer_id", "timestamp"], kind="stable")
    specimen_state = work.groupby("specimen_id", sort=True)["state_label"].first()
    split_map: dict[str, str] = {}
    for state, state_series in specimen_state.groupby(specimen_state):
        specimens = list(state_series.index)
        train_n = max(1, int(round(len(specimens) * 0.6)))
        val_n = max(1, int(round(len(specimens) * 0.2))) if len(specimens) >= 4 else 1
        test_n = max(0, len(specimens) - train_n - val_n)
        interpolation_n = (test_n + 1) // 2
        for i, sid in enumerate(specimens):
            split_map[str(sid)] = (
                "train" if i < train_n
                else "validation" if i < train_n + val_n
                else "test_interpolation" if i < train_n + val_n + interpolation_n
                else "test_extrapolation"
            )
    records: list[dict[str, Any]] = []
    for (specimen, layer), group in work.groupby(["specimen_id", "layer_id"], sort=False):
        rel = Path("layers") / f"{specimen}_layer_{layer}.csv"
        model = group[[*sensor_columns, *process_columns]].copy()
        model.to_csv(root / rel, index=False, encoding="utf-8-sig")
        state = int(group["state_label"].iloc[0])
        abnormal = str(group["abnormal_type"].iloc[0])
        records.append({"specimen_id": str(specimen), "layer_id": str(layer), "file_path": str(rel), "split": split_map[str(specimen)], "state_label": state, "abnormal_type": abnormal, "run_id": str(specimen), **{col: float(pd.to_numeric(group[col], errors="coerce").iloc[0]) for col in process_columns}})
    manifest = pd.DataFrame(records)
    manifest.to_csv(root / "manifest.csv", index=False, encoding="utf-8-sig")
    (root / "import_summary.json").write_text(json.dumps({"rows": int(len(frame)), "specimens": len(specimens), "splits": split_map}, ensure_ascii=False, indent=2), encoding="utf-8")
    return root
