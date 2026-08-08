from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd

from new_collection_health import (
    INDICATOR_FEATURES,
    INDICATOR_LABELS,
    NEW_ABNORMAL_STATES,
    PROCESS_COLUMNS,
    SENSOR_COLUMNS,
    build_calibration,
    build_feature_vector,
)


APP_DIR = Path(__file__).resolve().parent
DATA_ROOT = APP_DIR / "new_collection_demo_v11_3"
MODEL_DIR = DATA_ROOT / "models"
MANIFEST = DATA_ROOT / "manifest.csv"
CHECKPOINT = MODEL_DIR / "i_modern_tcn_new_collection_v11_3.pth"
METADATA = CHECKPOINT.with_suffix(".pth.json")
ARTIFACT = MODEL_DIR / "new_collection_hi_artifacts.joblib"
METRICS_FILE = MODEL_DIR / "new_collection_hi_metrics.csv"
CATALOG_FILE = MODEL_DIR / "new_collection_hi_catalog.csv"
SUMMARY_FILE = MODEL_DIR / "new_collection_hi_summary.json"
XJU_ROOT = Path(r"F:\program\XJUsorceopen")
MODEL_COLUMNS = [*SENSOR_COLUMNS, *PROCESS_COLUMNS]
RANDOM_SEED = 20260730


def cap_pool(scores: np.ndarray, rho: float = 0.5) -> float:
    scores = np.asarray(scores, dtype=float)
    if not len(scores):
        return 0.0
    alpha = float(np.clip(rho, 0.0, 1.0)) * np.log(max(len(scores) - 1, 1))
    logits = alpha * scores
    weights = np.exp(logits - np.max(logits))
    weights /= weights.sum()
    return float(weights @ scores)


def _load_windows() -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    manifest = pd.read_csv(MANIFEST, encoding="utf-8-sig")
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    rows: list[dict] = []
    for record in manifest.itertuples():
        frame = pd.read_csv(DATA_ROOT / record.file_path, encoding="utf-8-sig")
        model_values = frame[MODEL_COLUMNS].to_numpy(dtype=np.float32)
        sensor_values = frame[SENSOR_COLUMNS].to_numpy(dtype=np.float32)
        process = {name: float(frame[name].iloc[0]) for name in PROCESS_COLUMNS}
        for start in range(0, len(frame) - 47, 24):
            xs.append(model_values[start : start + 24])
            ys.append(sensor_values[start + 24 : start + 48])
            rows.append(
                {
                    "run_id": str(record.run_id),
                    "specimen_id": str(record.specimen_id),
                    "layer_id": int(record.layer_id),
                    "split": str(record.split),
                    "state_label": int(record.state_label),
                    "abnormal_type": (
                        "normal" if str(record.abnormal_type) == "none"
                        else str(record.abnormal_type)
                    ),
                    **process,
                }
            )
    return np.stack(xs), np.stack(ys), pd.DataFrame(rows)


def _predict(x_raw: np.ndarray) -> np.ndarray:
    """Generate causal 24-step predictions with the selected I-ModernTCN."""
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    if str(XJU_ROOT) not in sys.path:
        sys.path.insert(0, str(XJU_ROOT))
    import torch
    import shijie.model_mine.I_modernTCN_GAT_abalation as model_module

    metadata = json.loads(METADATA.read_text(encoding="utf-8"))
    mean = np.asarray(metadata["scaler_mean"], dtype=np.float32)
    scale = np.asarray(metadata["scaler_scale"], dtype=np.float32)
    standardized_input = ((x_raw - mean) / scale).astype(np.float32)
    device = torch.device("cpu")
    model_module.device = device
    config = SimpleNamespace(
        enc_in=int(metadata["enc_in"]),
        seq_len=24,
        pred_len=24,
        dropout=float(metadata.get("dropout", 0.05)),
    )
    model = model_module.Model(config).to(device)
    model.load_state_dict(torch.load(CHECKPOINT, map_location="cpu", weights_only=False))
    model.eval()
    outputs: list[np.ndarray] = []
    with torch.no_grad():
        for start in range(0, len(standardized_input), 64):
            batch = torch.from_numpy(standardized_input[start : start + 64]).to(device)
            mark = torch.zeros((len(batch), 24, 4), device=device)
            decoder = torch.zeros((len(batch), 48, len(MODEL_COLUMNS)), device=device)
            standardized = model(batch, mark, decoder, mark).cpu().numpy()
            physical = standardized * scale[None, None, :] + mean[None, None, :]
            outputs.append(physical[:, :, : len(SENSOR_COLUMNS)])
    return np.concatenate(outputs, axis=0)


def _best_threshold(labels: np.ndarray, scores: np.ndarray) -> tuple[float, float]:
    from sklearn.metrics import balanced_accuracy_score

    candidates = np.unique(
        np.concatenate([np.linspace(0.05, 0.95, 181), np.asarray(scores, dtype=float)])
    )
    best = (0.5, -1.0)
    for threshold in candidates:
        value = balanced_accuracy_score(labels, scores >= threshold)
        if value > best[1] + 1e-12:
            best = (float(threshold), float(value))
    return best


def _aggregate(
    frame: pd.DataFrame,
    scores: np.ndarray,
    group_columns: list[str],
) -> tuple[np.ndarray, np.ndarray]:
    working = frame.copy()
    working["score"] = scores
    labels: list[int] = []
    pooled: list[float] = []
    for _, group in working.groupby(group_columns, sort=False):
        labels.append(int(group["state_label"].iloc[0]))
        pooled.append(cap_pool(group["score"].to_numpy(dtype=float)))
    return np.asarray(labels), np.asarray(pooled)


def _aggregate_predictions(
    frame: pd.DataFrame,
    scores: np.ndarray,
    type_probabilities: np.ndarray,
    group_columns: list[str],
    threshold: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    working = frame.copy()
    working["row_position"] = np.arange(len(working))
    true_binary: list[int] = []
    predicted_binary: list[int] = []
    true_state: list[str] = []
    predicted_state: list[str] = []
    for _, group in working.groupby(group_columns, sort=False):
        positions = group["row_position"].to_numpy(dtype=int)
        group_scores = np.asarray(scores, dtype=float)[positions]
        alpha = 0.5 * np.log(max(len(group_scores) - 1, 1))
        weights = np.exp(alpha * group_scores - np.max(alpha * group_scores))
        weights /= weights.sum()
        health = float(weights @ group_scores)
        pooled_types = weights @ np.asarray(type_probabilities)[positions]
        abnormal = health >= threshold
        true_binary.append(int(group["state_label"].iloc[0]))
        predicted_binary.append(int(abnormal))
        truth = str(group["abnormal_type"].iloc[0])
        true_state.append(truth)
        predicted_state.append(
            NEW_ABNORMAL_STATES[int(np.argmax(pooled_types))]
            if abnormal else "normal"
        )
    return (
        np.asarray(true_binary),
        np.asarray(predicted_binary),
        np.asarray(true_state),
        np.asarray(predicted_state),
    )


def _estimators() -> dict[str, object]:
    from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC

    return {
        "logistic": make_pipeline(
            StandardScaler(),
            LogisticRegression(
                C=1.0, class_weight="balanced", max_iter=3000,
                random_state=RANDOM_SEED,
            ),
        ),
        "svm_rbf": make_pipeline(
            StandardScaler(),
            SVC(
                C=2.0, gamma="scale", probability=True,
                class_weight="balanced", random_state=RANDOM_SEED,
            ),
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=400, min_samples_leaf=2,
            class_weight="balanced_subsample", random_state=RANDOM_SEED,
            n_jobs=-1,
        ),
        "extra_trees": ExtraTreesClassifier(
            n_estimators=400, min_samples_leaf=2,
            class_weight="balanced", random_state=RANDOM_SEED, n_jobs=-1,
        ),
    }


def _type_estimator(model_kind: str) -> object:
    return _estimators()[model_kind]


def main() -> None:
    random.seed(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)
    x_raw, actual, index = _load_windows()
    prediction = _predict(x_raw)
    # Import scikit-learn only after the torch forward pass. On Windows the
    # two stacks may otherwise load conflicting native/OpenMP runtimes.
    from sklearn.metrics import (
        accuracy_score,
        balanced_accuracy_score,
        roc_auc_score,
    )
    labels = index["state_label"].to_numpy(dtype=int)
    train = index["split"].eq("train").to_numpy()
    validation = index["split"].eq("validation").to_numpy()
    train_healthy = train & (labels == 0)
    calibration = build_calibration(
        actual[train_healthy],
        prediction[train_healthy],
        index.loc[train_healthy, PROCESS_COLUMNS].drop_duplicates().to_numpy(dtype=float),
    )

    feature_sets: dict[str, np.ndarray] = {}
    for indicator in INDICATOR_FEATURES:
        feature_sets[indicator] = np.stack(
            [
                build_feature_vector(
                    indicator,
                    actual[row],
                    prediction[row],
                    index.iloc[row].to_dict(),
                    calibration,
                )
                for row in range(len(index))
            ]
        )

    catalog: list[dict] = []
    metrics: list[dict] = []
    models: dict[tuple[str, str], dict[str, object]] = {}
    abnormal_train = train & (labels == 1)
    type_labels = index["abnormal_type"].astype(str).to_numpy()
    for indicator, features in feature_sets.items():
        indicator_rows: list[dict] = []
        for model_kind, binary_model in _estimators().items():
            binary_model.fit(features[train], labels[train])
            type_model = _type_estimator(model_kind)
            type_model.fit(features[abnormal_train], type_labels[abnormal_train])
            models[(indicator, model_kind)] = {
                "binary_model": binary_model,
                "type_model": type_model,
            }
            scores = binary_model.predict_proba(features)[:, list(binary_model.classes_).index(1)]
            window_threshold, window_ba = _best_threshold(labels[validation], scores[validation])
            validation_index = index.loc[validation].reset_index(drop=True)
            validation_scores = scores[validation]
            layer_labels, layer_scores = _aggregate(
                validation_index, validation_scores, ["specimen_id", "layer_id"]
            )
            specimen_labels, specimen_scores = _aggregate(
                validation_index, validation_scores, ["specimen_id"]
            )
            layer_threshold, layer_ba = _best_threshold(layer_labels, layer_scores)
            specimen_threshold, specimen_ba = _best_threshold(specimen_labels, specimen_scores)
            selection_score = 0.5 * window_ba + 0.25 * layer_ba + 0.25 * specimen_ba
            row = {
                "indicator": indicator,
                "indicator_label": INDICATOR_LABELS[indicator],
                "feature_names": "|".join(INDICATOR_FEATURES[indicator]),
                "model": model_kind,
                "validation_selection_score": selection_score,
                "validation_window_balanced_accuracy": window_ba,
                "validation_layer_balanced_accuracy": layer_ba,
                "validation_specimen_balanced_accuracy": specimen_ba,
                "validation_auc": roc_auc_score(labels[validation], validation_scores),
                "window_threshold": window_threshold,
                "layer_threshold": layer_threshold,
                "specimen_threshold": specimen_threshold,
                "cap_rho": 0.5,
                "recommended": False,
                "dataset_schema": "new_collection_v11_3",
            }
            indicator_rows.append(row)

            type_probability = type_model.predict_proba(features)
            type_classes = list(type_model.classes_)
            aligned_types = np.zeros((len(features), len(NEW_ABNORMAL_STATES)))
            for source_index, state in enumerate(type_classes):
                if state in NEW_ABNORMAL_STATES:
                    aligned_types[:, NEW_ABNORMAL_STATES.index(state)] = (
                        type_probability[:, source_index]
                    )
            for split in ("validation", "test_interpolation", "test_extrapolation"):
                mask = index["split"].eq(split).to_numpy()
                predicted_binary = scores[mask] >= window_threshold
                predicted_type = np.asarray(
                    [type_classes[position] for position in np.argmax(type_probability[mask], axis=1)]
                )
                predicted_state = np.where(predicted_binary, predicted_type, "normal")
                metrics.append(
                    {
                        "indicator": indicator,
                        "model": model_kind,
                        "dataset": split,
                        "level": "window",
                        "balanced_accuracy": balanced_accuracy_score(labels[mask], predicted_binary),
                        "state_accuracy": accuracy_score(type_labels[mask], predicted_state),
                        "threshold": window_threshold,
                        "sample_count": int(mask.sum()),
                    }
                )
                split_index = index.loc[mask].reset_index(drop=True)
                for level, groups, decision_threshold in (
                    ("layer", ["specimen_id", "layer_id"], layer_threshold),
                    ("specimen", ["specimen_id"], specimen_threshold),
                ):
                    y_binary, p_binary, y_state, p_state = _aggregate_predictions(
                        split_index,
                        scores[mask],
                        aligned_types[mask],
                        groups,
                        decision_threshold,
                    )
                    metrics.append(
                        {
                            "indicator": indicator,
                            "model": model_kind,
                            "dataset": split,
                            "level": level,
                            "balanced_accuracy": balanced_accuracy_score(
                                y_binary, p_binary
                            ),
                            "state_accuracy": accuracy_score(y_state, p_state),
                            "threshold": decision_threshold,
                            "sample_count": int(len(y_binary)),
                        }
                    )
        best = max(
            indicator_rows,
            key=lambda row: (row["validation_selection_score"], row["validation_auc"]),
        )
        best["recommended"] = True
        catalog.extend(indicator_rows)

    artifact = {
        **calibration,
        "schema_version": "new_collection_hi_v2",
        "sensor_columns": SENSOR_COLUMNS,
        "process_columns": PROCESS_COLUMNS,
        "indicator_features": INDICATOR_FEATURES,
        "state_names": ["normal", *NEW_ABNORMAL_STATES],
        "models": models,
        "catalog": catalog,
        "training_boundary": (
            "正常基线仅用训练集正常窗口；模型仅用训练集；"
            "阈值与推荐模型仅用验证集；内推和外推测试集锁定"
        ),
        "warning": "当前为模拟新数据集联调结果，不能代表真实AFP缺陷识别性能",
    }
    joblib.dump(artifact, ARTIFACT)
    pd.DataFrame(catalog).to_csv(CATALOG_FILE, index=False, encoding="utf-8-sig")
    pd.DataFrame(metrics).to_csv(METRICS_FILE, index=False, encoding="utf-8-sig")
    summary = {
        "schema_version": artifact["schema_version"],
        "indicator_count": len(INDICATOR_FEATURES),
        "candidate_count": len(catalog),
        "selected_models": {
            indicator: next(
                row["model"] for row in catalog
                if row["indicator"] == indicator and row["recommended"]
            )
            for indicator in INDICATOR_FEATURES
        },
        "artifact": str(ARTIFACT),
        "catalog": str(CATALOG_FILE),
        "metrics": str(METRICS_FILE),
        "training_boundary": artifact["training_boundary"],
        "warning": artifact["warning"],
    }
    SUMMARY_FILE.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
