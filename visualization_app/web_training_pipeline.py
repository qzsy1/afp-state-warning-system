"""Unified CSV/MySQL ingestion and web-controlled AFP model training.

All data sources are converted to one canonical CSV before model training.
The module deliberately does not depend on the realtime dashboard state.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import joblib
import numpy as np
import pandas as pd

from training_data import _expand_json_fields, _read_table, read_mysql


LEGACY_SENSORS = [
    "转速", "位移", *[f"温度{i}" for i in range(1, 9)], "压力", "振动",
]
LEGACY_CONDITIONS = ["p", "v", "pr"]
LEGACY_REQUIRED = [
    *LEGACY_SENSORS, "cycle", "file", "root", *LEGACY_CONDITIONS,
    "l", "试件",
]
NEW_SENSORS = [
    "温度", "压力", "ROI平均温度", "张力", "线速度",
    "ABB_X", "ABB_Y", "ABB_Z", *[f"温度{i}" for i in range(1, 9)],
]
NEW_CONDITIONS = [
    "initial_compaction_force_N", "placement_speed_mm_s",
    "pid_angle_deg", "temperature_setpoint_C",
]
NEW_REQUIRED = [
    "时间", *NEW_SENSORS, *NEW_CONDITIONS, "run_id", "specimen_id",
    "condition_id", "replicate", "layer_id",
]
META_COLUMNS = [
    "source_type", "source_file", "schema_mode", "timestamp",
    "condition_id", "specimen_id", "replicate", "layer_id",
    "sample_index", "state_label", "abnormal_type",
]
NEW_MYSQL_QUERY = """SELECT
    s.condition_id,
    s.schema_id,
    s.run_id,
    s.specimen_key,
    s.specimen_id,
    s.replicate_no AS replicate,
    l.layer_no AS layer_id,
    COALESCE(
        l.layer_file,
        CONCAT('mysql://', s.specimen_key, '/layer-', l.layer_no)
    ) AS source_file,
    a.sample_index,
    a.timestamp_iso AS `时间`,
    a.sensor_json,
    a.process_json,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(a.process_json, '$.initial_compaction_force_N')) AS DECIMAL(20,6)) AS initial_compaction_force_N,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(a.process_json, '$.placement_speed_mm_s')) AS DECIMAL(20,6)) AS placement_speed_mm_s,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(a.process_json, '$.pid_angle_deg')) AS DECIMAL(20,6)) AS pid_angle_deg,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(a.process_json, '$.temperature_setpoint_C')) AS DECIMAL(20,6)) AS temperature_setpoint_C,
    -1 AS state_label,
    'unlabeled' AS abnormal_type
FROM afp_sample_all a
INNER JOIN afp_layer l
    ON l.specimen_key = a.specimen_key
   AND l.layer_no = a.layer_no
INNER JOIN afp_specimen s
    ON s.specimen_key = l.specimen_key
WHERE s.schema_id = 'new_collection_v11_3'
ORDER BY s.condition_id, s.specimen_id, s.replicate_no,
         l.layer_no, a.sample_index"""

LEGACY_MYSQL_QUERY = """SELECT
    s.condition_id,
    s.schema_id,
    s.run_id,
    s.specimen_key,
    s.specimen_id,
    s.replicate_no AS replicate,
    s.replicate_no AS cycle,
    l.layer_no AS layer_id,
    l.layer_no - 1 AS l,
    COALESCE(
        l.layer_file,
        CONCAT('mysql://', s.specimen_key, '/layer-', l.layer_no)
    ) AS source_file,
    a.sample_index,
    a.timestamp_iso AS `时间`,
    a.sensor_json,
    a.process_json,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(s.parameter_json, '$.p')) AS DECIMAL(20,6)) AS p,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(s.parameter_json, '$.v')) AS DECIMAL(20,6)) AS v,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(s.parameter_json, '$.pr')) AS DECIMAL(20,6)) AS pr,
    -1 AS state_label,
    'unlabeled' AS abnormal_type
FROM afp_sample_all a
INNER JOIN afp_layer l
    ON l.specimen_key = a.specimen_key
   AND l.layer_no = a.layer_no
INNER JOIN afp_specimen s
    ON s.specimen_key = l.specimen_key
WHERE s.schema_id = 'legacy_original'
ORDER BY s.condition_id, s.specimen_id, s.replicate_no,
         l.layer_no, a.sample_index"""

DEFAULT_MYSQL_QUERY = NEW_MYSQL_QUERY


def default_mysql_query(mode: str) -> str:
    return LEGACY_MYSQL_QUERY if mode == "legacy" else NEW_MYSQL_QUERY


def parse_columns(value: str | list[str]) -> list[str]:
    parts = value if isinstance(value, list) else str(value or "").split(",")
    return list(dict.fromkeys(item.strip() for item in parts if item.strip()))


def default_columns(mode: str) -> dict[str, list[str]]:
    if mode == "legacy":
        return {
            "condition_columns": LEGACY_CONDITIONS,
            "input_columns": LEGACY_SENSORS,
            "output_columns": LEGACY_SENSORS,
        }
    return {
        "condition_columns": NEW_CONDITIONS,
        "input_columns": NEW_SENSORS,
        "output_columns": NEW_SENSORS,
    }


def _capture_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(
        path for path in root.rglob("*")
        if path.suffix.lower() in {".csv", ".xlsx", ".xls", ".xlsm"}
        and "采集记录" not in path.parts
        and "历史版本" not in path.parts
        and "时间戳" not in path.name
        and "完整试样" not in path.name
        and path.name.lower() not in {"manifest.csv", "import_summary.csv"}
    )


def _matches(columns: list[str], mode: str, configured: dict[str, list[str]]) -> bool:
    available = set(map(str, columns))
    if mode == "new":
        return set(NEW_REQUIRED).issubset(available)
    if mode == "legacy":
        return set(LEGACY_REQUIRED).issubset(available)
    required = set(configured["condition_columns"] + configured["input_columns"] + configured["output_columns"])
    return bool(required) and required.issubset(available)


def _read_mode_table(path: Path, mode: str, configured: dict[str, list[str]]) -> pd.DataFrame:
    """Read a table and prefer the encoding that yields the requested schema."""
    first = _read_table(path)
    if _matches(list(first.columns), mode, configured) or path.suffix.lower() != ".csv":
        return first
    for encoding in ("gb18030", "utf-8-sig", "utf-8", "cp1252"):
        try:
            candidate = pd.read_csv(path, encoding=encoding)
        except Exception:
            continue
        if _matches(list(candidate.columns), mode, configured):
            return candidate
    return first


def _matches_mysql(columns: list[str], mode: str, configured: dict[str, list[str]]) -> bool:
    """Validate expanded MySQL rows without requiring CSV-only bookkeeping columns."""
    available = set(map(str, columns))
    metadata = {"specimen_id", "replicate", "layer_id"}
    if mode == "new":
        required = set(NEW_SENSORS + NEW_CONDITIONS) | metadata
    elif mode == "legacy":
        required = set(LEGACY_SENSORS + LEGACY_CONDITIONS) | metadata
    else:
        required = set(configured["condition_columns"] + configured["input_columns"] + configured["output_columns"]) | metadata
    return bool(required) and required.issubset(available)


def _filter_mysql_schema(frame: pd.DataFrame, mode: str) -> pd.DataFrame:
    if "schema_id" not in frame or mode == "other":
        return frame
    schema = frame["schema_id"].astype(str).str.lower()
    if mode == "new":
        mask = schema.str.contains("new|collection_v1", regex=True)
    else:
        mask = schema.str.contains("legacy|old", regex=True)
    return frame.loc[mask].copy()


def _logical_sources(frame: pd.DataFrame, fallback: str) -> pd.Series:
    """Return the acquisition-layer file identity for CSV and MySQL rows."""
    if "source_file" in frame:
        values = frame["source_file"].fillna("").astype(str)
        missing = values.str.strip().eq("")
    else:
        values = pd.Series([""] * len(frame), index=frame.index, dtype=str)
        missing = pd.Series([True] * len(frame), index=frame.index)
    if {"specimen_key", "layer_id"}.issubset(frame.columns):
        generated = (
            "mysql://" + frame["specimen_key"].astype(str)
            + "/layer-" + frame["layer_id"].astype(str)
        )
        values = values.mask(missing, generated)
        missing = values.str.strip().eq("")
    return values.mask(missing, fallback)


def _filter_required_rows(
    frame: pd.DataFrame,
    configured: dict[str, list[str]],
) -> tuple[pd.DataFrame, int]:
    required = list(dict.fromkeys(
        configured["condition_columns"]
        + configured["input_columns"]
        + configured["output_columns"]
    ))
    numeric = frame[required].apply(pd.to_numeric, errors="coerce")
    keep = np.isfinite(numeric.to_numpy(dtype=float)).all(axis=1)
    return frame.loc[keep].copy(), int((~keep).sum())


def _filename_meta(path: str) -> tuple[str, str]:
    name = Path(path).stem
    match = re.search(r"第(\d+)层", name)
    specimen = re.sub(r"[_-]?第\d+层.*$", "", name)
    return specimen or name, match.group(1) if match else "1"


def _first_existing(frame: pd.DataFrame, names: list[str]) -> str | None:
    return next((name for name in names if name in frame.columns), None)


def _prepare_bare_complete_csv(
    frame: pd.DataFrame,
    path: Path,
    mode: str,
    configured: dict[str, list[str]],
) -> tuple[pd.DataFrame, list[str]]:
    """Add only bookkeeping metadata to an otherwise trainable CSV."""
    out = _expand_json_fields(frame).copy()
    notes: list[str] = []
    selected = list(dict.fromkeys(
        configured["condition_columns"]
        + configured["input_columns"]
        + configured["output_columns"]
    ))
    missing_data = [column for column in selected if column not in out.columns]
    if missing_data:
        raise ValueError(
            "CSV缺少真正参与训练的工况/输入/输出列，不能自动补全："
            f"{missing_data}"
        )

    if "__source_file__" not in out.columns:
        source_column = _first_existing(out, ["source_file", "layer_file", "file"])
        out["__source_file__"] = (
            out[source_column].fillna(str(path)).astype(str)
            if source_column else str(path)
        )
        notes.append("source_type/source_file/schema_mode由当前文件和所选数据模式自动生成")

    specimen_column = _first_existing(
        out, ["specimen_id", "specimen_key", "试件", "specimen", "sample_id"]
    )
    if specimen_column and specimen_column != "specimen_id":
        out["specimen_id"] = out[specimen_column].astype(str)
        notes.append(f"specimen_id由列 {specimen_column} 推断")
    elif not specimen_column:
        out["specimen_id"] = out["__source_file__"].map(
            lambda value: _filename_meta(str(value))[0]
        )
        notes.append("specimen_id未提供，按源文件名推断；每个源文件视为一个独立试样")

    replicate_column = _first_existing(
        out, ["replicate", "replicate_no", "repeat_id", "cycle", "独立重复"]
    )
    if replicate_column and replicate_column != "replicate":
        out["replicate"] = out[replicate_column].astype(str)
        notes.append(f"replicate由列 {replicate_column} 推断")
    elif not replicate_column:
        out["replicate"] = "1"
        notes.append("replicate未提供，自动设为1")

    layer_column = _first_existing(
        out, ["layer_id", "layer_no", "layer", "铺层", "层数"]
    )
    if layer_column and layer_column != "layer_id":
        out["layer_id"] = out[layer_column].astype(str)
        notes.append(f"layer_id由列 {layer_column} 推断")
    elif not layer_column and "l" in out.columns:
        value = pd.to_numeric(out["l"], errors="coerce").fillna(0)
        out["layer_id"] = (value + 1 if mode == "legacy" else value).astype(int).astype(str)
        notes.append("layer_id由列 l 推断")
    elif not layer_column:
        out["layer_id"] = out["__source_file__"].map(
            lambda value: _filename_meta(str(value))[1]
        )
        notes.append("layer_id未提供，优先从文件名“第N层”推断，否则设为1")

    timestamp_column = _first_existing(
        out, ["timestamp", "时间", "time", "timestamp_iso"]
    )
    if timestamp_column and timestamp_column != "timestamp":
        out["timestamp"] = out[timestamp_column]
        notes.append(f"timestamp由列 {timestamp_column} 推断")
    elif not timestamp_column:
        out["timestamp"] = out.groupby(
            ["specimen_id", "layer_id"], sort=False
        ).cumcount()
        notes.append("timestamp未提供，按试样—铺层内行顺序生成")

    state_column = _first_existing(
        out, ["state_label", "label", "state", "health_label", "健康状态"]
    )
    if state_column and state_column != "state_label":
        out["state_label"] = pd.to_numeric(out[state_column], errors="coerce").fillna(-1)
        notes.append(f"state_label由列 {state_column} 推断")
    elif not state_column:
        out["state_label"] = -1
        notes.append("state_label未提供，设为-1（未标注；可训练预测模型，不能监督训练预警模型）")

    abnormal_column = _first_existing(
        out, ["abnormal_type", "fault_type", "state_type", "异常类型"]
    )
    if abnormal_column and abnormal_column != "abnormal_type":
        out["abnormal_type"] = out[abnormal_column].astype(str)
        notes.append(f"abnormal_type由列 {abnormal_column} 推断")
    elif not abnormal_column:
        out["abnormal_type"] = "unlabeled"
        notes.append("abnormal_type未提供，设为unlabeled")
    return out, notes


def _canonicalize(
    raw: pd.DataFrame,
    mode: str,
    configured: dict[str, list[str]],
    source_type: str,
) -> pd.DataFrame:
    frame = _expand_json_fields(raw).copy()
    source = frame.get("__source_file__", pd.Series([source_type] * len(frame), index=frame.index)).astype(str)
    file_meta = source.map(_filename_meta)
    output = pd.DataFrame(index=frame.index)
    output["source_type"] = source_type
    output["source_file"] = source
    output["schema_mode"] = mode

    if mode == "new":
        output["timestamp"] = frame["时间"] if "时间" in frame else frame.get("timestamp", np.arange(len(frame)))
        output["condition_id"] = frame[NEW_CONDITIONS].astype(str).agg("|".join, axis=1)
        output["replicate"] = frame["replicate"].astype(str)
        raw_specimen = frame["specimen_key"].astype(str) if "specimen_key" in frame else frame["specimen_id"].astype(str)
        output["specimen_id"] = output["condition_id"] + "::" + raw_specimen + "::R" + output["replicate"]
        output["layer_id"] = frame["layer_id"].astype(str)
    elif mode == "legacy":
        output["timestamp"] = (
            frame.groupby("__source_file__", dropna=False).cumcount() if "__source_file__" in frame
            else frame["时间"] if "时间" in frame
            else frame["timestamp"] if "timestamp" in frame
            else frame["sample_index"] if "sample_index" in frame
            else np.arange(len(frame))
        )
        output["condition_id"] = frame[LEGACY_CONDITIONS].astype(str).agg("|".join, axis=1)
        output["replicate"] = frame["cycle"].astype(str) if "cycle" in frame else frame["replicate"].astype(str)
        raw_specimen = frame["specimen_key"].astype(str) if "specimen_key" in frame else (
            frame["试件"].astype(str) if "试件" in frame else frame["specimen_id"].astype(str)
        )
        output["specimen_id"] = output["condition_id"] + "::" + raw_specimen + "::R" + output["replicate"]
        output["layer_id"] = (
            (pd.to_numeric(frame["l"], errors="coerce").fillna(0).astype(int) + 1).astype(str)
            if "l" in frame else frame["layer_id"].astype(str)
        )
    else:
        output["timestamp"] = frame["时间"] if "时间" in frame else (
            frame["timestamp"] if "timestamp" in frame else np.arange(len(frame))
        )
        output["condition_id"] = frame[configured["condition_columns"]].astype(str).agg("|".join, axis=1)
        raw_specimen = (
            frame["specimen_id"].astype(str) if "specimen_id" in frame
            else file_meta.map(lambda value: value[0])
        )
        output["replicate"] = frame["replicate"].astype(str) if "replicate" in frame else "1"
        output["specimen_id"] = output["condition_id"] + "::" + raw_specimen + "::R" + output["replicate"]
        output["layer_id"] = (
            frame["layer_id"].astype(str) if "layer_id" in frame
            else file_meta.map(lambda value: value[1])
        )

    output["sample_index"] = output.groupby(["specimen_id", "layer_id"], sort=False).cumcount()
    output["state_label"] = pd.to_numeric(frame["state_label"], errors="coerce") if "state_label" in frame else -1
    output["state_label"] = output["state_label"].fillna(-1).astype(int)
    output["abnormal_type"] = frame["abnormal_type"].astype(str) if "abnormal_type" in frame else "unlabeled"
    selected = list(dict.fromkeys(
        configured["condition_columns"] + configured["input_columns"] + configured["output_columns"]
    ))
    for column in selected:
        output[column] = pd.to_numeric(frame[column], errors="coerce")
    return output[[*META_COLUMNS, *selected]]


@dataclass
class UnifiedData:
    frame: pd.DataFrame
    config: dict[str, Any]
    validation: dict[str, Any]


def validate_unified(frame: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    numeric = list(dict.fromkeys(
        config["condition_columns"] + config["input_columns"] + config["output_columns"]
    ))
    missing = [column for column in [*META_COLUMNS, *numeric] if column not in frame]
    values = frame[[column for column in numeric if column in frame]].apply(pd.to_numeric, errors="coerce")
    invalid = int((~np.isfinite(values.to_numpy(dtype=float))).sum()) if len(values.columns) else 0
    layer_sizes = frame.groupby(["specimen_id", "layer_id"]).size()
    required_points = int(config["history_length"]) + int(config["prediction_length"])
    short = int((layer_sizes < required_points).sum())
    labels = frame.loc[frame["state_label"].isin([0, 1]), "state_label"]
    return {
        "ok": not missing and invalid == 0 and short == 0 and frame["specimen_id"].nunique() >= 2,
        "rows": int(len(frame)),
        "columns": int(len(frame.columns)),
        "specimens": int(frame["specimen_id"].nunique()),
        "conditions": int(frame["condition_id"].nunique()),
        "layers": int(frame[["specimen_id", "layer_id"]].drop_duplicates().shape[0]),
        "accepted_files": int(frame["source_file"].nunique()),
        "retained_files": int(frame["source_file"].nunique()),
        "missing_columns": missing,
        "nonfinite_values": invalid,
        "short_layers": short,
        "minimum_points_per_layer": required_points,
        "state_counts": frame["state_label"].value_counts().sort_index().astype(int).to_dict(),
        "warning_ready": set(labels.unique()) == {0, 1},
    }


def import_unified(source: dict[str, Any], options: dict[str, Any]) -> UnifiedData:
    mode = str(options.get("data_mode", "new"))
    configured = default_columns(mode)
    if mode == "other":
        configured = {
            "condition_columns": parse_columns(options.get("condition_columns", "")),
            "input_columns": parse_columns(options.get("input_columns", "")),
            "output_columns": parse_columns(options.get("output_columns", "")),
        }
        if not all(configured.values()):
            raise ValueError("其它数据模式必须填写工况数据、输入数据和输出数据列，使用英文或中文逗号分隔")
    config = {
        "data_mode": mode,
        **configured,
        "history_length": int(options.get("history_length", 24)),
        "prediction_length": int(options.get("prediction_length", 24)),
        "stride": int(options.get("stride", 24)),
    }
    source_kind = str(source.get("kind", "csv"))
    warnings: list[str] = []
    frames: list[pd.DataFrame] = []
    if source_kind == "complete_csv":
        root = Path(str(source.get("path", ""))).expanduser().resolve()
        if not root.is_file():
            raise FileNotFoundError(f"请选择一份已经整合完成的CSV文件：{root}")
        raw = _read_mode_table(root, mode, configured)
        selected = list(dict.fromkeys(configured["condition_columns"] + configured["input_columns"] + configured["output_columns"]))
        is_normalized = set(META_COLUMNS + selected).issubset(raw.columns)
        if is_normalized:
            modes = set(raw["schema_mode"].dropna().astype(str).str.lower().unique())
            if mode != "other" and modes != {mode}:
                raise ValueError(f"完整CSV的数据模式为 {sorted(modes)}，与当前选择的 {mode} 不一致")
            canonical = raw[[*META_COLUMNS, *selected]].copy()
            auto_notes: list[str] = []
        else:
            prepared, auto_notes = _prepare_bare_complete_csv(
                raw, root, mode, configured
            )
            canonical = _canonicalize(prepared, mode, configured, "complete_csv")
        canonical, dropped_invalid_rows = _filter_required_rows(canonical, configured)
        if canonical.empty:
            raise ValueError("自动补全元数据后没有可用于训练的有效数据行")
        description = str(root)
        validation = validate_unified(canonical, config)
        validation["warnings"] = [f"自动补全：{note}" for note in auto_notes]
        validation["source"] = description
        validation["normalized_input"] = is_normalized
        validation["metadata_auto_filled"] = not is_normalized
        validation["total_files"] = 1
        validation["retained_files"] = int(canonical["source_file"].nunique())
        validation["accepted_files"] = validation["retained_files"]
        validation["rejected_files"] = 0 if len(canonical) else 1
        validation["dropped_invalid_rows"] = dropped_invalid_rows
        return UnifiedData(canonical, config, validation)
    if source_kind == "csv":
        root = Path(str(source.get("path", ""))).expanduser().resolve()
        files = _capture_files(root)
        total_files = len(files)
        if not files:
            raise FileNotFoundError(f"未找到可读取的CSV/Excel：{root}")
        for path in files:
            try:
                current = _read_mode_table(path, mode, configured)
            except Exception as exc:
                warnings.append(f"跳过无法读取文件 {path.name}: {exc}")
                continue
            current = _expand_json_fields(current)
            if not _matches(list(current.columns), mode, configured):
                warnings.append(f"跳过格式不属于{mode}模式的文件：{path.name}")
                continue
            current["__source_file__"] = str(path)
            frames.append(current)
        description = str(root)
    elif source_kind == "mysql":
        result = read_mysql(
            source.get("connection", {}),
            str(source.get("query", "")) or default_mysql_query(mode),
        )
        queried = _expand_json_fields(result.frame)
        queried["__source_file__"] = _logical_sources(queried, "mysql://query-result")
        total_files = int(queried["__source_file__"].nunique())
        raw = _filter_mysql_schema(queried, mode)
        if raw.empty:
            raise ValueError(f"MySQL查询结果中没有属于 {mode} 模式的数据")
        if not _matches_mysql(list(raw.columns), mode, configured):
            required = configured["condition_columns"] + configured["input_columns"] + configured["output_columns"]
            raise ValueError(f"MySQL查询结果与{mode}格式不一致；要求列：{required}；实际列：{list(raw.columns)}")
        frames = [raw]
        description = result.source_description
    else:
        raise ValueError(f"未知数据来源：{source_kind}")
    if not frames:
        raise ValueError("没有任何文件符合当前训练数据模式；请检查新/旧/其它数据模式及列名")
    raw = pd.concat(frames, ignore_index=True, sort=False)
    raw, dropped_invalid_rows = _filter_required_rows(raw, configured)
    if raw.empty:
        raise ValueError("格式列存在，但没有任何数据行同时满足当前工况、输入和输出列要求")
    if dropped_invalid_rows:
        warnings.append(f"剔除 {dropped_invalid_rows} 行不属于当前列格式或包含空值的数据")
    canonical = _canonicalize(raw, mode, configured, source_kind)
    required_points = config["history_length"] + config["prediction_length"]
    sizes = canonical.groupby(["specimen_id", "layer_id"])["sample_index"].transform("size")
    dropped_layers = canonical.loc[sizes < required_points, ["specimen_id", "layer_id"]].drop_duplicates()
    if not dropped_layers.empty:
        warnings.append(f"剔除 {len(dropped_layers)} 个不足 {required_points} 点的未完成铺层")
        canonical = canonical.loc[sizes >= required_points].copy()
        canonical["sample_index"] = canonical.groupby(["specimen_id", "layer_id"], sort=False).cumcount()
    validation = validate_unified(canonical, config)
    validation["dropped_short_layers"] = int(len(dropped_layers))
    validation["dropped_invalid_rows"] = dropped_invalid_rows
    validation["total_files"] = int(total_files)
    validation["retained_files"] = int(canonical["source_file"].nunique())
    validation["accepted_files"] = validation["retained_files"]
    validation["rejected_files"] = max(0, validation["total_files"] - validation["retained_files"])
    validation["warnings"] = warnings[:100]
    validation["source"] = description
    return UnifiedData(canonical, config, validation)


def _split_specimens(frame: pd.DataFrame, seed: int) -> tuple[pd.DataFrame, dict[str, str]]:
    table = frame[["specimen_id", "condition_id", "state_label"]].drop_duplicates("specimen_id").copy()
    table["specimen_id"] = table["specimen_id"].astype(str)
    if len(table) < 2:
        raise ValueError("至少需要2个独立试样才能分离训练集和验证集")
    rng = random.Random(seed)
    conditions = list(map(str, table["condition_id"].drop_duplicates()))
    rng.shuffle(conditions)
    extrapolation_conditions: set[str] = set()
    if len(conditions) >= 2 and len(table) >= 8:
        extrapolation_conditions = set(conditions[:max(1, round(len(conditions) * 0.1))])
    external = table[table["condition_id"].astype(str).isin(extrapolation_conditions)]["specimen_id"].tolist()
    remaining = table[~table["specimen_id"].isin(external)].copy()
    grouped: dict[int, list[str]] = {}
    for label, group in remaining.groupby("state_label", sort=True):
        values = group["specimen_id"].astype(str).tolist()
        rng.shuffle(values)
        grouped[int(label)] = values
    validation: list[str] = []
    interpolation: list[str] = []
    training: list[str] = []
    for values in grouped.values():
        n = len(values)
        n_val = max(1, round(n * 0.2)) if n >= 3 else (1 if n == 2 else 0)
        # Keep as many specimens as possible for training: validation is about
        # one quarter of train, with a smaller interpolation-test holdout.
        n_interp = max(1, round(n * 0.05)) if n >= 8 else 0
        while n_val + n_interp >= n and n_val > 0:
            n_val -= 1
        validation.extend(values[:n_val])
        interpolation.extend(values[n_val:n_val + n_interp])
        training.extend(values[n_val + n_interp:])
    if not validation and len(training) > 1:
        validation.append(training.pop())
    mapping = {value: "train" for value in training}
    mapping.update({value: "validation" for value in validation})
    mapping.update({value: "test_interpolation" for value in interpolation})
    mapping.update({value: "test_extrapolation" for value in external})
    output = frame.copy()
    output["split"] = output["specimen_id"].astype(str).map(mapping)
    return output, mapping


def _windows(frame: pd.DataFrame, config: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    model_columns = config["model_columns"]
    history = int(config["history_length"])
    prediction = int(config["prediction_length"])
    stride = max(1, int(config["stride"]))
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    rows: list[dict[str, Any]] = []
    for (specimen, layer), group in frame.groupby(["specimen_id", "layer_id"], sort=False):
        group = group.sort_values("sample_index")
        values = group[model_columns].to_numpy(np.float32)
        for start in range(0, len(values) - history - prediction + 1, stride):
            xs.append(values[start:start + history])
            ys.append(values[start + history:start + history + prediction])
            rows.append({
                "specimen_id": str(specimen), "layer_id": str(layer),
                "split": str(group["split"].iloc[0]),
                "state_label": int(group["state_label"].iloc[0]),
                "abnormal_type": str(group["abnormal_type"].iloc[0]),
            })
    if not xs:
        raise ValueError("没有形成任何训练窗口，请增加每层采样点或减小历史/预测步长")
    return np.stack(xs), np.stack(ys), pd.DataFrame(rows)


def train_models(
    data: UnifiedData,
    settings: dict[str, Any],
    output_root: Path,
    stop_event: Any,
    emit: Callable[..., None],
) -> dict[str, Any]:
    import torch

    # Source runs use the research workspace.  Frozen desktop builds carry the
    # same model package below ``model_runtime`` so training remains available
    # on computers that do not have the original F: drive tree.
    if getattr(sys, "frozen", False):
        xju_root = Path(getattr(sys, "_MEIPASS")) / "model_runtime"
    else:
        xju_root = Path(r"F:\program\XJUsorceopen")
    if False and not (xju_root / "shijie").exists():
        raise FileNotFoundError(f"I-ModernTCN模型源码目录不存在：{xju_root / 'shijie'}")
    # Add both the I-ModernTCN source and the thesis comparison-model source.
    import_roots = [
        xju_root,
        Path(__file__).resolve().parents[4],
        Path(__file__).resolve().parents[4].parent,
    ]
    for import_root in import_roots:
        if import_root.exists() and str(import_root) not in sys.path:
            sys.path.insert(0, str(import_root))
    from online_inference import MODEL_REGISTRY, normalize_model_type
    from atavn import metadata as atavn_metadata, normalize as atavn_normalize
    selected_model_type = normalize_model_type(settings.get("model_type", "i_T_G"))
    definition = MODEL_REGISTRY[selected_model_type]
    if definition.get("training_only"):
        raise ValueError(f"算法 {definition['label']} 当前没有可训练的 PyTorch 适配器")
    # Both the I-ModernTCN source and the baseline models expose a top-level
    # package named ``models``.  A single training process may run all 11
    # algorithms, so clear the previous namespace before importing a
    # comparison architecture just as the online runtime does.
    if selected_model_type != "i_T_G":
        for module_name in list(sys.modules):
            if (module_name == "models" or module_name.startswith("models.")
                    or module_name == "layers" or module_name.startswith("layers.")
                    or module_name == "utilsaa" or module_name.startswith("utilsaa.")):
                del sys.modules[module_name]
        sys.path[:] = [item for item in sys.path if Path(item).name.lower() != "modern_tcn_models"]
        comparison_root = str(Path(__file__).resolve().parents[4])
        if comparison_root in sys.path:
            sys.path.remove(comparison_root)
        sys.path.insert(0, comparison_root)
    try:
        model_module = __import__(definition["module"], fromlist=[definition["class_name"]])
    except Exception as exc:
        raise ImportError(
            f"算法 {definition['label']} 的模型代码未随当前环境安装：{definition['module']}"
        ) from exc

    seed = int(settings.get("seed", 20260813))
    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)
    frame, split_map = _split_specimens(data.frame, seed)
    config = {**data.config}
    config["model_columns"] = list(dict.fromkeys(
        config["condition_columns"] + config["input_columns"] + config["output_columns"]
    ))
    config["output_indices"] = [config["model_columns"].index(column) for column in config["output_columns"]]
    config["input_indices"] = [config["model_columns"].index(column) for column in list(dict.fromkeys(config["condition_columns"] + config["input_columns"]))]

    stamp = time.strftime("%Y%m%d_%H%M%S")
    task_name = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "_", str(settings.get("task_name", "AFP训练任务")))
    task_dir = output_root.expanduser().resolve() / f"{task_name}_{stamp}"
    task_dir.mkdir(parents=True, exist_ok=False)
    complete_csv = task_dir / "normalized_training_data.csv"
    frame.to_csv(complete_csv, index=False, encoding="utf-8-sig")
    pd.DataFrame([{"specimen_id": key, "split": value} for key, value in split_map.items()]).to_csv(
        task_dir / "specimen_split_manifest.csv", index=False, encoding="utf-8-sig"
    )
    (task_dir / "data_validation_report.json").write_text(json.dumps(data.validation, ensure_ascii=False, indent=2), encoding="utf-8")

    x_raw, y_raw, index = _windows(frame, config)
    train_mask = index["split"].eq("train").to_numpy()
    val_mask = index["split"].eq("validation").to_numpy()
    if not train_mask.any() or not val_mask.any():
        raise ValueError("训练集或验证集没有形成有效窗口")
    train_rows = np.concatenate([x_raw[train_mask].reshape(-1, len(config["model_columns"])), y_raw[train_mask].reshape(-1, len(config["model_columns"]))])
    mean = train_rows.mean(0); scale = train_rows.std(0); scale[scale < 1e-6] = 1.0
    standardize = lambda value: ((value - mean) / scale).astype(np.float32)
    x = standardize(x_raw); y = standardize(y_raw)
    inactive = sorted(set(range(len(config["model_columns"]))) - set(config["input_indices"]))
    if inactive:
        x[:, :, inactive] = 0.0

    device_name = str(settings.get("device", "auto"))
    device = torch.device("cuda" if device_name == "auto" and torch.cuda.is_available() else "cpu" if device_name == "auto" else device_name)
    model_module.device = device
    history_length = int(config["history_length"])
    prediction_length = int(config["prediction_length"])
    model_config = SimpleNamespace(
        enc_in=len(config["model_columns"]),
        c_out=len(config["model_columns"]),
        dec_in=len(config["model_columns"]),
        seq_len=history_length,
        label_len=history_length,
        pred_len=prediction_length,
        dropout=float(settings.get("dropout", 0.05)),
        d_model=int(settings.get("d_model", 128)),
        n_heads=int(settings.get("n_heads", 8)),
        e_layers=int(settings.get("e_layers", 2)),
        d_layers=int(settings.get("d_layers", 1)),
        d_ff=int(settings.get("d_ff", 2048)),
        factor=int(settings.get("factor", 1)),
        embed="timeF", freq="h", activation="gelu", output_attention=False,
        distil=True, individual=False, flat_input=False, low_rank=False,
        rank_ratio=4, version=1, model=selected_model_type,
    )
    if selected_model_type in {"FNN_2024", "FNN_2025_Base"} and prediction_length != 24:
        raise ValueError("FNN 2024/2025 的论文复现实现固定输出24步，请将预测未来步长设为24")
    model = getattr(model_module, definition["class_name"])(model_config).to(device)
    # I-ModernTCN-GAT already contains the manuscript's terminal-aligned
    # shift/variance restoration internally.  The comparison architectures
    # receive the same ATAVN transform externally so every new checkpoint
    # uses an explicit, auditable mechanism without double normalizing it.
    atavn_enabled = bool(settings.get("atavn_enabled", True))
    atavn_external = atavn_enabled and selected_model_type != "i_T_G"
    atavn_mode = (
        "native" if selected_model_type == "i_T_G" and atavn_enabled
        else ("external" if atavn_external else "disabled")
    )
    pretrained = str(settings.get("pretrained_model", "")).strip()
    if pretrained:
        pretrained_path = Path(pretrained).expanduser().resolve()
        if not pretrained_path.is_file():
            raise FileNotFoundError(f"已有模型不存在：{pretrained_path}")
        metadata_path = Path(str(pretrained_path) + ".json")
        if not metadata_path.is_file():
            raise ValueError("继续训练要求模型旁边存在同名 .json 元数据文件，以校验输入输出列和步长")
        old_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected = {
            "model_columns": config["model_columns"],
            "input_sensors": config["input_columns"],
            "output_sensors": config["output_columns"],
            "seq_len": history_length,
            "pred_len": prediction_length,
        }
        mismatches = [key for key, value in expected.items() if old_metadata.get(key) != value]
        if normalize_model_type(old_metadata.get("model_type", "i_T_G")) != selected_model_type:
            mismatches.append("model_type")
        if mismatches:
            raise ValueError(f"已有模型与当前训练定义不一致：{', '.join(mismatches)}")
        state = torch.load(pretrained, map_location="cpu", weights_only=False)
        if isinstance(state, dict) and "model_state_dict" in state:
            state = state["model_state_dict"]
        model.load_state_dict(state, strict=True)
        emit("pretrained_loaded", path=pretrained)
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(settings.get("learning_rate", 8e-4)), weight_decay=float(settings.get("weight_decay", 1e-4)))
    batch_size = max(1, int(settings.get("batch_size", 32)))
    train_dataset = torch.utils.data.TensorDataset(torch.from_numpy(x[train_mask]), torch.from_numpy(y[train_mask]))
    loader = torch.utils.data.DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=0)
    output_indices = torch.tensor(config["output_indices"], dtype=torch.long, device=device)
    def _atavn_input(batch: Any) -> tuple[Any, Any, Any]:
        if not atavn_external:
            return batch, None, None
        normalized, terminal, scale = atavn_normalize(batch)
        return normalized, terminal, scale

    def _atavn_target(batch_x: Any, batch_y: Any) -> Any:
        if not atavn_external:
            return batch_y
        _, terminal, scale = _atavn_input(batch_x)
        return (batch_y - terminal) / scale

    def forward(batch: Any) -> Any:
        model_batch, _, _ = _atavn_input(batch)
        mark = torch.zeros((len(batch), history_length, 4), device=device)
        decoder = torch.zeros((len(batch), history_length + prediction_length, len(config["model_columns"])), device=device)
        decoder_mark = torch.zeros((len(batch), history_length + prediction_length, 4), device=device)
        output = model(model_batch.contiguous(), mark, decoder, decoder_mark)
        if isinstance(output, (tuple, list)):
            output = output[0]
        if output.ndim == 2:
            output = output.unsqueeze(1)
        if output.ndim != 3:
            raise ValueError(
                f"算法 {definition['label']} 输出维度应为 [batch, horizon, channels]，实际为 {tuple(output.shape)}"
            )
        if output.shape[-1] != len(config["model_columns"]):
            raise ValueError(
                f"算法 {definition['label']} 输出通道数为 {output.shape[-1]}，当前数据需要 {len(config['model_columns'])}"
            )
        if output.shape[1] >= prediction_length:
            return output[:, -prediction_length:, :]
        pad = output[:, -1:, :].repeat(1, prediction_length - output.shape[1], 1)
        return torch.cat([output, pad], dim=1)

    epochs = max(1, int(settings.get("epochs", 100))); patience = max(1, int(settings.get("patience", 10)))
    best_loss = math.inf; best_state = None; bad_epochs = 0; history_rows: list[dict[str, Any]] = []
    val_x = torch.from_numpy(x[val_mask]).to(device); val_y = torch.from_numpy(y[val_mask]).to(device)
    emit(
        "training_started",
        task_dir=str(task_dir), epochs=epochs, patience=patience,
        device=str(device), model_type=selected_model_type,
        model_label=definition["label"],
    )
    stopped = False
    for epoch in range(1, epochs + 1):
        model.train(); losses: list[float] = []
        for batch_x, batch_y in loader:
            if stop_event.is_set(): stopped = True; break
            batch_x = batch_x.to(device); batch_y = batch_y.to(device)
            optimizer.zero_grad(set_to_none=True)
            prediction = forward(batch_x).index_select(2, output_indices)
            target = _atavn_target(batch_x, batch_y).index_select(2, output_indices)
            loss = torch.nn.functional.mse_loss(prediction, target)
            loss.backward(); torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0); optimizer.step()
            losses.append(float(loss.detach().cpu()))
        if stopped: break
        model.eval()
        with torch.no_grad():
            validation_loss = float(torch.nn.functional.mse_loss(forward(val_x).index_select(2, output_indices), _atavn_target(val_x, val_y).index_select(2, output_indices)).cpu())
        train_loss = float(np.mean(losses))
        improved = validation_loss < best_loss - float(settings.get("min_delta", 1e-6))
        if improved:
            best_loss = validation_loss
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
            bad_epochs = 0
        else:
            bad_epochs += 1
        row = {"epoch": epoch, "train_loss": train_loss, "validation_loss": validation_loss, "best_validation_loss": best_loss, "bad_epochs": bad_epochs}
        history_rows.append(row)
        emit("epoch_progress", **row, epochs=epochs, patience=patience)
        if bad_epochs >= patience: break
    current_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
    if best_state is None: best_state = current_state
    torch.save(best_state, task_dir / "prediction_model_best.pth")
    torch.save({"model_state_dict": current_state, "optimizer_state_dict": optimizer.state_dict(), "epoch": len(history_rows), "best_validation_loss": best_loss, "bad_epochs": bad_epochs}, task_dir / "prediction_training_resume.pth")
    pd.DataFrame(history_rows).to_csv(task_dir / "training_history.csv", index=False, encoding="utf-8-sig")
    metadata = {
        "model_type": selected_model_type,
        "architecture": definition["architecture"],
        "model_module": definition["module"],
        "model_class": definition["class_name"],
        "enc_in": len(config["model_columns"]),
        "seq_len": history_length, "pred_len": prediction_length,
        "model_columns": config["model_columns"], "condition_columns": config["condition_columns"],
        "input_sensors": config["input_columns"], "output_sensors": config["output_columns"],
        "scaler_mean": mean.tolist(), "scaler_scale": scale.tolist(),
        "stopped_by_user": stopped, "epochs_completed": len(history_rows),
        "atavn": atavn_metadata(atavn_mode) if atavn_enabled else {"enabled": False, "mode": "disabled"},
    }
    (task_dir / "prediction_model_best.pth.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    warning_result: dict[str, Any] | None = None
    if str(settings.get("training_type", "prediction")) == "prediction_warning" and not stopped:
        labeled = index["state_label"].isin([0, 1]).to_numpy()
        if set(index.loc[labeled, "state_label"].unique()) != {0, 1}:
            raise ValueError("预测预警模式要求完整CSV同时包含state_label=0和state_label=1")
        model.load_state_dict(best_state); model.eval()
        predictions: list[np.ndarray] = []
        with torch.no_grad():
            for start in range(0, len(x), batch_size):
                value = forward(torch.from_numpy(x[start:start + batch_size]).to(device)).cpu().numpy()
                predictions.append(value)
        predicted = np.concatenate(predictions)[:, :, config["output_indices"]]
        if atavn_external:
            target_batches = []
            for start in range(0, len(x), batch_size):
                target_batches.append(
                    _atavn_target(
                        torch.from_numpy(x[start:start + batch_size]).to(device),
                        torch.from_numpy(y[start:start + batch_size]).to(device),
                    ).detach().cpu().numpy()
                )
            truth = np.concatenate(target_batches, axis=0)[:, :, config["output_indices"]]
        else:
            truth = y[:, :, config["output_indices"]]
        residual = truth - predicted
        features = np.column_stack([
            np.mean(np.abs(residual), axis=(1, 2)), np.sqrt(np.mean(residual ** 2, axis=(1, 2))),
            np.max(np.abs(residual), axis=(1, 2)), np.mean(residual, axis=(1, 2)),
            np.std(residual, axis=(1, 2)),
        ])
        from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import balanced_accuracy_score
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.svm import SVC
        candidates = {
            "logistic": make_pipeline(StandardScaler(), LogisticRegression(max_iter=3000, class_weight="balanced")),
            "svm_rbf": make_pipeline(StandardScaler(), SVC(probability=True, class_weight="balanced")),
            "random_forest": RandomForestClassifier(n_estimators=300, class_weight="balanced", random_state=seed),
            "extra_trees": ExtraTreesClassifier(n_estimators=300, class_weight="balanced", random_state=seed),
        }
        scores: dict[str, float] = {}; fitted: dict[str, Any] = {}
        train_labeled = labeled & index["split"].eq("train").to_numpy()
        val_labeled = labeled & index["split"].eq("validation").to_numpy()
        if set(index.loc[train_labeled, "state_label"].unique()) != {0, 1}:
            raise ValueError("训练集没有同时包含正常和异常标签，无法训练预警模型")
        if set(index.loc[val_labeled, "state_label"].unique()) != {0, 1}:
            raise ValueError("验证集没有同时包含正常和异常标签，无法选择预警模型")
        for position, (name, classifier) in enumerate(candidates.items(), 1):
            if stop_event.is_set(): stopped = True; break
            classifier.fit(features[train_labeled], index.loc[train_labeled, "state_label"])
            score = balanced_accuracy_score(index.loc[val_labeled, "state_label"], classifier.predict(features[val_labeled]))
            scores[name] = float(score); fitted[name] = classifier
            emit("warning_progress", model=name, current=position, total=len(candidates), validation_balanced_accuracy=score)
        if fitted:
            selected = max(scores, key=scores.get)
            warning_result = {"selected_model": selected, "validation_balanced_accuracy": scores[selected], "all_models": scores, "feature_names": ["mae", "rmse", "max_abs", "bias", "std"]}
            joblib.dump({"model": fitted[selected], "metadata": warning_result}, task_dir / "warning_model.joblib")
            (task_dir / "warning_metrics.json").write_text(json.dumps(warning_result, ensure_ascii=False, indent=2), encoding="utf-8")

    final_config = {"data": config, "training": settings, "metadata": metadata, "warning": warning_result}
    (task_dir / "training_config.json").write_text(json.dumps(final_config, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"task_dir": str(task_dir), "complete_csv": str(complete_csv), "checkpoint": str(task_dir / "prediction_model_best.pth"), "stopped": stopped, "epochs_completed": len(history_rows), "warning": warning_result}
