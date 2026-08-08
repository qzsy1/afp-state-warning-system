from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd


APP_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = APP_DIR.parents[1]
OUTPUT_ROOT = APP_DIR / "new_collection_demo_v11_3"
CONDITION_FILE = WORKSPACE_DIR / "工艺参数状态标签_v11_2.csv"
MANIFEST_TEMPLATE = WORKSPACE_DIR / "计划数据集" / "采集清单模板_v11_3.csv"
XJU_ROOT = Path(r"F:\program\XJUsorceopen")

SENSOR_COLUMNS = [
    "温度",
    "压力",
    "ROI平均温度",
    "张力",
    "线速度",
    "ABB_X",
    "ABB_Y",
    "ABB_Z",
    *[f"温度{index}" for index in range(1, 9)],
    "转速",
    "位移",
    "振动",
]
PROCESS_COLUMNS = [
    "initial_compaction_force_N",
    "placement_speed_mm_s",
    "pid_angle_deg",
    "temperature_setpoint_C",
]
MODEL_COLUMNS = [*SENSOR_COLUMNS, *PROCESS_COLUMNS]


@dataclass(frozen=True)
class BuildSettings:
    points_per_layer: int = 96
    layers_per_specimen: int = 5
    sample_rate_hz: float = 10.0
    seed: int = 20260730
    epochs: int = 3
    batch_size: int = 64
    learning_rate: float = 8e-4


def _split_for(condition_id: str, replicate: int) -> str:
    extrapolation = {"H04", "H12", "H16", "A04", "A06", "A12"}
    validation = {"H02", "H07", "H15", "A01", "A09", "A13"}
    if condition_id in extrapolation:
        return "test_extrapolation"
    if replicate == 1:
        return "train"
    if condition_id in validation:
        return "validation"
    return "test_interpolation"


def simulate_layer(
    condition: pd.Series,
    replicate: int,
    layer: int,
    settings: BuildSettings,
) -> pd.DataFrame:
    condition_id = str(condition["condition_id"])
    seed = (
        settings.seed
        + sum(ord(char) for char in condition_id) * 101
        + replicate * 1009
        + layer * 9176
    )
    rng = np.random.default_rng(seed)
    count = settings.points_per_layer
    time_s = np.arange(count, dtype=float) / settings.sample_rate_hz
    force = float(condition["initial_compaction_force_N"])
    speed = float(condition["placement_speed_mm_s"])
    angle = float(condition["pid_angle_deg"])
    setpoint = float(condition["temperature_setpoint_C"])

    contact = 1.0 / (1.0 + np.exp(-(time_s - 0.42) * 8.0))
    speed_ramp = 1.0 - np.exp(-time_s / 0.55)
    thermal_tau = 1.7 + 0.012 * speed + 0.025 * abs(angle - 5.0)
    heat_gain = (
        0.69
        * (80.0 / max(speed, 35.0)) ** 0.18
        * (1.0 + 0.00022 * (force - 400.0))
    )
    base_temperature = (
        24.0
        + (setpoint - 24.0)
        * heat_gain
        * (1.0 - np.exp(-time_s / thermal_tau))
        * contact
    )
    layer_heat = (layer - 1) * 1.3 * (1.0 - np.exp(-time_s / 2.2))
    thermal_wave = 1.6 * np.sin(2.0 * np.pi * 0.23 * time_s + 0.3 * layer)
    temperature = (
        base_temperature
        + layer_heat
        + thermal_wave
        + rng.normal(0.0, 0.55, count)
    )
    pressure = (
        force
        * contact
        * (
            1.0
            + 0.025 * np.sin(2.0 * np.pi * 0.7 * time_s)
            + 0.006 * (angle - 5.0)
        )
        + rng.normal(0.0, max(2.0, 0.012 * force), count)
    )
    roi_temperature = (
        temperature
        + 3.2
        - 0.014 * speed
        + rng.normal(0.0, 0.45, count)
    )
    line_speed = (
        speed * speed_ramp
        + 0.8 * np.sin(2.0 * np.pi * 0.45 * time_s)
        + rng.normal(0.0, 0.35, count)
    )
    tension = (
        19.0
        + 0.115 * line_speed
        + 0.82 * abs(angle - 5.0)
        + 1.2 * np.sin(2.0 * np.pi * 0.31 * time_s)
        + rng.normal(0.0, 0.35, count)
    )
    abb_x = np.cumsum(np.maximum(line_speed, 0.0)) / settings.sample_rate_hz
    abb_y = 2.5 * np.sin(np.linspace(0.0, np.pi, count)) + 0.35 * angle
    abb_z = np.full(count, (layer - 1) * 0.18) + 0.015 * np.sin(
        2.0 * np.pi * 0.4 * time_s
    )
    rotation = line_speed * 5.7 + rng.normal(0.0, 1.3, count)
    displacement = (
        0.42
        + 0.00075 * pressure
        + 0.018 * np.sin(2.0 * np.pi * 0.8 * time_s)
        + rng.normal(0.0, 0.006, count)
    )
    vibration = (
        0.08
        + 0.0013 * line_speed
        + 0.00012 * pressure
        + 0.025 * np.sin(2.0 * np.pi * 2.1 * time_s)
        + rng.normal(0.0, 0.008, count)
    )

    data: dict[str, np.ndarray | float | str | int] = {
        "时间": time_s,
        "温度": temperature,
        "压力": pressure,
        "ROI平均温度": roi_temperature,
        "张力": tension,
        "线速度": line_speed,
        "ABB_X": abb_x,
        "ABB_Y": abb_y,
        "ABB_Z": abb_z,
    }
    spatial_offsets = np.linspace(-3.5, 3.5, 8)
    for index, offset in enumerate(spatial_offsets, start=1):
        data[f"温度{index}"] = (
            temperature
            + offset
            + 0.7 * np.sin(2.0 * np.pi * (0.17 + index * 0.008) * time_s)
            + rng.normal(0.0, 0.32, count)
        )
    data.update(
        {
            "转速": rotation,
            "位移": displacement,
            "振动": vibration,
            "initial_compaction_force_N": force,
            "placement_speed_mm_s": speed,
            "pid_angle_deg": angle,
            "temperature_setpoint_C": setpoint,
            "condition_id": condition_id,
            "replicate": replicate,
            "layer_id": layer,
            "state_label": int(condition["state_label"]),
            "abnormal_type": str(condition["abnormal_type"]),
        }
    )
    return pd.DataFrame(data)


def generate_dataset(output_root: Path, settings: BuildSettings) -> pd.DataFrame:
    raw_dir = output_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    conditions = pd.read_csv(CONDITION_FILE, encoding="utf-8-sig")
    manifest_rows: list[dict] = []
    simulator_source: Path | None = None
    for _, condition in conditions.iterrows():
        condition_id = str(condition["condition_id"])
        for replicate in (1, 2):
            specimen_id = f"{condition_id}_R{replicate}"
            split = _split_for(condition_id, replicate)
            for layer in range(1, settings.layers_per_specimen + 1):
                run_id = f"{specimen_id}_L{layer}"
                frame = simulate_layer(
                    condition, replicate, layer, settings
                )
                frame.insert(0, "run_id", run_id)
                frame.insert(1, "specimen_id", specimen_id)
                path = raw_dir / f"{run_id}.csv"
                frame.to_csv(path, index=False, encoding="utf-8-sig")
                if condition_id == "H06" and replicate == 1 and layer == 1:
                    simulator_source = path
                manifest_rows.append(
                    {
                        "run_id": run_id,
                        "specimen_id": specimen_id,
                        "condition_id": condition_id,
                        "replicate": replicate,
                        "layer_id": layer,
                        "file_path": str(path.relative_to(output_root)),
                        "forecast_file_path": "",
                        "split": split,
                        "state_label": int(condition["state_label"]),
                        "abnormal_type": str(condition["abnormal_type"]),
                    }
                )
    manifest = pd.DataFrame(manifest_rows)
    manifest.to_csv(
        output_root / "manifest.csv", index=False, encoding="utf-8-sig"
    )
    if simulator_source is None:
        raise RuntimeError("未生成默认模拟采集文件")
    simulator_frame = pd.read_csv(simulator_source, encoding="utf-8-sig")
    simulator_frame.to_csv(
        output_root / "simulator_stream.csv",
        index=False,
        encoding="utf-8-sig",
    )
    metadata = {
        "purpose": "v11.3界面与模型链路模拟，不能作为真实缺陷证据",
        "formula_summary": {
            "thermal": "设定温度、铺放速度、压实力、角度和层间蓄热共同决定一阶热响应",
            "pressure": "初始压实力乘接触建立函数，并叠加角度与周期波动",
            "motion": "线速度经启动动态积分形成ABB_X，ABB_Y/Z描述轨迹与层高",
        },
        "settings": settings.__dict__,
        "sensor_columns": SENSOR_COLUMNS,
        "process_parameter_columns": PROCESS_COLUMNS,
        "specimen_count": int(manifest["specimen_id"].nunique()),
        "layer_file_count": int(len(manifest)),
        "split_counts": manifest.groupby("split")["specimen_id"]
        .nunique()
        .astype(int)
        .to_dict(),
    }
    (output_root / "dataset_generation_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _windows(
    manifest: pd.DataFrame, output_root: Path, split: str
) -> tuple[np.ndarray, np.ndarray]:
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    for row in manifest.loc[manifest["split"].eq(split)].itertuples():
        frame = pd.read_csv(
            output_root / row.file_path, encoding="utf-8-sig"
        )
        values = frame[MODEL_COLUMNS].to_numpy(dtype=np.float32)
        for start in range(0, len(values) - 47, 24):
            xs.append(values[start : start + 24])
            ys.append(values[start + 24 : start + 48])
    return np.stack(xs), np.stack(ys)


def train_model(
    output_root: Path,
    manifest: pd.DataFrame,
    settings: BuildSettings,
) -> dict:
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    if str(XJU_ROOT) not in sys.path:
        sys.path.insert(0, str(XJU_ROOT))
    import torch
    import shijie.model_mine.I_modernTCN_GAT_abalation as model_module

    random.seed(settings.seed)
    np.random.seed(settings.seed)
    torch.manual_seed(settings.seed)
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    device = torch.device("cpu")
    model_module.device = device

    x_train_raw, y_train_raw = _windows(manifest, output_root, "train")
    x_validation_raw, y_validation_raw = _windows(
        manifest, output_root, "validation"
    )
    train_rows = np.concatenate(
        [x_train_raw.reshape(-1, len(MODEL_COLUMNS)),
         y_train_raw.reshape(-1, len(MODEL_COLUMNS))],
        axis=0,
    )
    mean = train_rows.mean(axis=0)
    scale = train_rows.std(axis=0)
    scale[scale < 1e-6] = 1.0

    def standardized(array: np.ndarray) -> np.ndarray:
        return ((array - mean) / scale).astype(np.float32)

    x_train = standardized(x_train_raw)
    y_train = standardized(y_train_raw)
    x_validation = standardized(x_validation_raw)
    y_validation = standardized(y_validation_raw)
    config = SimpleNamespace(
        enc_in=len(MODEL_COLUMNS),
        seq_len=24,
        pred_len=24,
        dropout=0.05,
    )
    model = model_module.Model(config).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=settings.learning_rate, weight_decay=1e-4
    )
    criterion = torch.nn.MSELoss()
    train_dataset = torch.utils.data.TensorDataset(
        torch.from_numpy(x_train), torch.from_numpy(y_train)
    )
    generator = torch.Generator().manual_seed(settings.seed)
    loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=settings.batch_size,
        shuffle=True,
        generator=generator,
    )
    history: list[dict] = []
    best_loss = float("inf")
    best_state: dict[str, torch.Tensor] | None = None
    zeros_x = None
    for epoch in range(1, settings.epochs + 1):
        model.train()
        losses: list[float] = []
        for batch_x, batch_y in loader:
            batch_x = batch_x.to(device)
            batch_y = batch_y.to(device)
            if zeros_x is None or len(zeros_x) != len(batch_x):
                zeros_x = torch.zeros(
                    (len(batch_x), 24, 4), dtype=torch.float32, device=device
                )
            decoder = torch.zeros(
                (len(batch_x), 48, len(MODEL_COLUMNS)),
                dtype=torch.float32,
                device=device,
            )
            optimizer.zero_grad(set_to_none=True)
            prediction = model(batch_x, zeros_x, decoder, zeros_x)
            loss = criterion(prediction, batch_y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
        model.eval()
        with torch.no_grad():
            val_x = torch.from_numpy(x_validation).to(device)
            val_y = torch.from_numpy(y_validation).to(device)
            val_mark = torch.zeros(
                (len(val_x), 24, 4), dtype=torch.float32, device=device
            )
            val_decoder = torch.zeros(
                (len(val_x), 48, len(MODEL_COLUMNS)),
                dtype=torch.float32,
                device=device,
            )
            val_prediction = model(
                val_x, val_mark, val_decoder, val_mark
            )
            validation_loss = float(
                criterion(val_prediction, val_y).detach().cpu()
            )
        history.append(
            {
                "epoch": epoch,
                "train_mse_standardized": float(np.mean(losses)),
                "validation_mse_standardized": validation_loss,
            }
        )
        if validation_loss < best_loss:
            best_loss = validation_loss
            best_state = {
                key: value.detach().cpu().clone()
                for key, value in model.state_dict().items()
            }
    if best_state is None:
        raise RuntimeError("训练没有生成有效检查点")
    model.load_state_dict(best_state)
    model_dir = output_root / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = model_dir / "i_modern_tcn_new_collection_v11_3.pth"
    torch.save(best_state, checkpoint)

    metrics: dict[str, dict] = {}
    model.eval()
    for split in (
        "validation",
        "test_interpolation",
        "test_extrapolation",
    ):
        x_raw, y_raw = _windows(manifest, output_root, split)
        x = standardized(x_raw)
        with torch.no_grad():
            x_tensor = torch.from_numpy(x).to(device)
            mark = torch.zeros(
                (len(x_tensor), 24, 4), dtype=torch.float32, device=device
            )
            decoder = torch.zeros(
                (len(x_tensor), 48, len(MODEL_COLUMNS)),
                dtype=torch.float32,
                device=device,
            )
            prediction_standardized = (
                model(x_tensor, mark, decoder, mark).cpu().numpy()
            )
        prediction = prediction_standardized * scale + mean
        sensor_prediction = prediction[:, :, : len(SENSOR_COLUMNS)]
        sensor_truth = y_raw[:, :, : len(SENSOR_COLUMNS)]
        error = sensor_prediction - sensor_truth
        metrics[split] = {
            "window_count": int(len(x_raw)),
            "mae_all_sensors": float(np.mean(np.abs(error))),
            "rmse_all_sensors": float(np.sqrt(np.mean(np.square(error)))),
            "per_sensor_rmse": {
                name: float(
                    np.sqrt(np.mean(np.square(error[:, :, index])))
                )
                for index, name in enumerate(SENSOR_COLUMNS)
            },
        }
    pd.DataFrame(history).to_csv(
        model_dir / "training_history.csv",
        index=False,
        encoding="utf-8-sig",
    )
    metrics_payload = {
        "best_validation_mse_standardized": best_loss,
        "splits": metrics,
        "warning": "模拟数据指标只验证软件链路，不代表真实AFP泛化性能",
    }
    (model_dir / "test_metrics.json").write_text(
        json.dumps(metrics_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    metadata = {
        "name": "I-ModernTCN 新采集方案v11.3模拟模型",
        "architecture": "I-ModernTCN",
        "enc_in": len(MODEL_COLUMNS),
        "seq_len": 24,
        "pred_len": 24,
        "dropout": 0.05,
        "input_sensors": SENSOR_COLUMNS,
        "output_sensors": SENSOR_COLUMNS,
        "model_columns": MODEL_COLUMNS,
        "scaler_mean": mean.astype(float).tolist(),
        "scaler_scale": scale.astype(float).tolist(),
        "training_dataset": str(output_root),
        "synthetic_training": True,
        "use_boundary": "仅用于采集/预测软件联调，正式实验必须用实测训练集重训",
    }
    checkpoint.with_suffix(".pth.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "checkpoint": str(checkpoint),
        "metadata": str(checkpoint.with_suffix(".pth.json")),
        "metrics": metrics_payload,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="生成v11.3模拟采集数据并训练I-ModernTCN 24→24模型"
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--points-per-layer", type=int, default=96)
    parser.add_argument("--skip-training", action="store_true")
    args = parser.parse_args()
    settings = BuildSettings(
        epochs=max(1, args.epochs),
        points_per_layer=max(48, args.points_per_layer),
    )
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = generate_dataset(args.output, settings)
    result = {
        "dataset": str(args.output),
        "manifest_rows": int(len(manifest)),
    }
    if not args.skip_training:
        result["model"] = train_model(args.output, manifest, settings)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
