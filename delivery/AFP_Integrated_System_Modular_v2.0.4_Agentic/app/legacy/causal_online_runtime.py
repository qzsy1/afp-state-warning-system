from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import joblib
import numpy as np


APP_DIR = Path(__file__).resolve().parent
_WORKSPACE_ARTIFACT = (
    APP_DIR.parent
    / "outputs_causal_online_consistency_v13_9"
    / "causal_online_consistency_artifact.joblib"
)
_PACKAGED_ARTIFACT = APP_DIR / "data" / "causal_online_consistency_artifact.joblib"
DEFAULT_ARTIFACT = (
    _PACKAGED_ARTIFACT if _PACKAGED_ARTIFACT.exists() else _WORKSPACE_ARTIFACT
)


def _normalise(values: np.ndarray, floor: float = 1e-10) -> np.ndarray:
    values = np.clip(np.asarray(values, dtype=float), floor, None)
    return values / values.sum(axis=-1, keepdims=True)


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -50.0, 50.0)))


def _logit(values: np.ndarray) -> np.ndarray:
    values = np.clip(np.asarray(values, dtype=float), 1e-8, 1.0 - 1e-8)
    return np.log(values / (1.0 - values))


def _cap_pool(values: np.ndarray, rho: float) -> tuple[float, np.ndarray]:
    values = np.asarray(values, dtype=float)
    if len(values) == 1:
        return float(values[0]), np.ones(1, dtype=float)
    alpha = float(rho) * math.log(max(len(values) - 1, 1))
    logits = alpha * values
    logits -= logits.max()
    weights = np.exp(logits)
    weights /= weights.sum()
    return float(np.dot(weights, values)), weights


def _aligned_probabilities(
    model: object,
    values: np.ndarray,
    labels: list[Any],
) -> np.ndarray:
    raw = np.asarray(model.predict_proba(values), dtype=float)
    classes = model[-1].classes_ if hasattr(model, "__getitem__") else model.classes_
    aligned = np.full((len(values), len(labels)), 1e-10, dtype=float)
    for source, label in enumerate(classes):
        if label in labels:
            aligned[:, labels.index(label)] = raw[:, source]
        elif isinstance(label, (int, np.integer)) and 0 <= int(label) < len(labels):
            aligned[:, int(label)] = raw[:, source]
    return _normalise(aligned)


class CausalOnlineConsistency:
    def __init__(self, artifact_path: Path = DEFAULT_ARTIFACT) -> None:
        if not artifact_path.exists():
            raise FileNotFoundError(
                f"causal online consistency artifact not found: {artifact_path}"
            )
        self.artifact = joblib.load(artifact_path)
        self.states = list(self.artifact["states"])
        self.anomaly_states = list(self.artifact["anomaly_states"])
        self.summary_columns = list(self.artifact["summary_columns"])
        self.parameters = dict(self.artifact["parameters"])
        self.window_threshold = float(self.artifact["window_threshold"])
        self.rho = float(self.artifact["cap_rho"])

    def build_layer_summary(
        self,
        scores: np.ndarray,
        type_matrix: np.ndarray,
        contact_observed: np.ndarray,
    ) -> dict[str, float]:
        scores = np.asarray(scores, dtype=float)
        type_matrix = _normalise(np.asarray(type_matrix, dtype=float))
        contact_observed = np.asarray(contact_observed, dtype=bool)
        top_count = min(
            len(scores), max(2, int(np.ceil(0.08 * len(scores))))
        )
        top_indices = np.argsort(scores)[-top_count:]
        tail_weights = np.maximum(scores[top_indices], 1e-6)
        tail_weights /= tail_weights.sum()
        type_vector = np.average(
            type_matrix[top_indices], axis=0, weights=tail_weights
        )
        type_vector /= max(float(type_vector.sum()), 1e-12)
        health, _ = _cap_pool(scores, self.rho)
        compaction_probability = np.max(type_matrix[:, -2:], axis=1)
        compaction_event = (
            contact_observed
            & (scores >= self.window_threshold)
            & (compaction_probability >= 0.40)
        )
        predicted_type = np.asarray(
            [
                self.anomaly_states[index]
                for index in type_matrix.argmax(axis=1)
            ],
            dtype=object,
        )
        summary = {
            "layer_health_index": health,
            "window_score_q95": float(np.percentile(scores, 95)),
            "top_window_score_mean": float(scores[top_indices].mean()),
            "abnormal_window_fraction": float(
                np.mean(scores >= self.window_threshold)
            ),
            "compaction_low_event_count": int(
                np.sum(compaction_event & (predicted_type == "compaction_low"))
            ),
            "compaction_high_event_count": int(
                np.sum(compaction_event & (predicted_type == "compaction_high"))
            ),
        }
        summary.update(
            {
                f"probability_{state}": float(type_vector[index])
                for index, state in enumerate(self.anomaly_states)
            }
        )
        summary["legacy_fixed_tail_health_index"] = (
            0.35 * summary["window_score_q95"]
            + 0.45 * summary["top_window_score_mean"]
            + 0.20 * summary["abnormal_window_fraction"]
        )
        return summary

    def _summary_statistics(
        self, layer_summaries: list[dict[str, float]]
    ) -> list[float]:
        values = np.asarray(
            [
                [float(summary[column]) for column in self.summary_columns]
                for summary in layer_summaries
            ],
            dtype=float,
        )
        output: list[float] = []
        for column in range(values.shape[1]):
            vector = values[:, column]
            output.extend(
                [
                    float(vector.mean()),
                    float(vector.std(ddof=0)),
                    float(vector.max()),
                    float(vector.min()),
                ]
            )
        return output

    def _specimen_local(
        self, layer_summaries: list[dict[str, float]]
    ) -> tuple[float, np.ndarray, bool, str]:
        health, _ = _cap_pool(
            np.asarray(
                [item["layer_health_index"] for item in layer_summaries],
                dtype=float,
            ),
            self.rho,
        )
        weights = np.maximum(
            np.asarray(
                [
                    item["legacy_fixed_tail_health_index"]
                    for item in layer_summaries
                ],
                dtype=float,
            ),
            1e-6,
        )
        types = np.average(
            np.asarray(
                [
                    [
                        item[f"probability_{state}"]
                        for state in self.anomaly_states
                    ]
                    for item in layer_summaries
                ],
                dtype=float,
            ),
            axis=0,
            weights=weights,
        )
        types /= max(float(types.sum()), 1e-12)
        low = int(
            sum(item["compaction_low_event_count"] for item in layer_summaries)
        )
        high = int(
            sum(item["compaction_high_event_count"] for item in layer_summaries)
        )
        return (
            health,
            types,
            low + high >= 2,
            "compaction_high" if high > low else "compaction_low",
        )

    def _prefix_context(
        self,
        layer_summaries: list[dict[str, float]],
        completed_layers: int,
        current_window_fraction: float,
    ) -> np.ndarray:
        features = self._summary_statistics(layer_summaries)
        features.extend(
            [
                len(layer_summaries) / 5.0,
                completed_layers / 5.0,
                float(np.clip(current_window_fraction, 0.0, 1.0)),
            ]
        )
        return _aligned_probabilities(
            self.artifact["model"],
            np.asarray(features, dtype=float)[None, :],
            list(range(len(self.states))),
        )[0]

    def _full_context(
        self,
        layer_summaries: list[dict[str, float]],
        specimen_health: float,
        specimen_types: np.ndarray,
        compaction_override: bool,
    ) -> np.ndarray:
        features = np.asarray(
            self._summary_statistics(layer_summaries), dtype=float
        )[None, :]
        calibrated = _aligned_probabilities(
            self.artifact["full_five_layer_calibrator"],
            features,
            self.anomaly_states,
        )[0]
        gamma = float(self.artifact["full_five_layer_gamma"])
        logits = (
            (1.0 - gamma)
            * np.log(np.clip(specimen_types, 1e-10, None))
            + gamma * np.log(np.clip(calibrated, 1e-10, None))
        )
        logits -= logits.max()
        fused_types = np.exp(logits)
        fused_types /= fused_types.sum()
        anomaly = float(_sigmoid((specimen_health - 0.403721) / 0.05))
        anomaly = (
            max(anomaly, 0.500001)
            if specimen_health >= 0.403721
            else min(anomaly, 0.499999)
        )
        if compaction_override:
            anomaly = 0.99
        return _normalise(
            np.concatenate([[1.0 - anomaly], anomaly * fused_types])[None, :]
        )[0]

    def _soft_fusion(
        self,
        health: float,
        types: np.ndarray,
        context: np.ndarray,
        level: str,
        threshold: float,
    ) -> np.ndarray:
        scale = float(self.parameters[f"{level}_scale"])
        binary_weight = float(
            self.parameters[f"{level}_binary_weight"]
        )
        type_weight = float(self.parameters[f"{level}_type_weight"])
        temperature = float(self.parameters[f"{level}_temperature"])
        local_abnormal = float(_sigmoid((health - threshold) / scale))
        context_abnormal = float(1.0 - context[0])
        abnormal = float(
            _sigmoid(
                (1.0 - binary_weight) * _logit(local_abnormal)
                + binary_weight * _logit(context_abnormal)
            )
        )
        local_types = _normalise(np.asarray(types, dtype=float)[None, :])[0]
        context_types = _normalise(context[None, 1:])[0]
        tempered = np.power(np.clip(context_types, 1e-10, None), 1.0 / temperature)
        tempered /= tempered.sum()
        logits = (
            (1.0 - type_weight)
            * np.log(np.clip(local_types, 1e-10, None))
            + type_weight * np.log(np.clip(tempered, 1e-10, None))
        )
        logits -= logits.max()
        fused_types = np.exp(logits)
        fused_types /= fused_types.sum()
        return _normalise(
            np.concatenate([[1.0 - abnormal], abnormal * fused_types])[None, :]
        )[0]

    def predict(
        self,
        layer_summaries: list[dict[str, float]],
        window_health: float,
        window_types: np.ndarray,
        current_layer_complete: bool,
        current_window_fraction: float,
    ) -> dict[str, Any]:
        completed_layers = len(layer_summaries) - (
            0 if current_layer_complete else 1
        )
        specimen_health, specimen_types, override, override_state = (
            self._specimen_local(layer_summaries)
        )
        all_five_complete = current_layer_complete and len(layer_summaries) == 5
        context = (
            self._full_context(
                layer_summaries, specimen_health, specimen_types, override
            )
            if all_five_complete
            else self._prefix_context(
                layer_summaries,
                completed_layers,
                current_window_fraction,
            )
        )
        current_layer = layer_summaries[-1]
        layer_types = np.asarray(
            [
                current_layer[f"probability_{state}"]
                for state in self.anomaly_states
            ],
            dtype=float,
        )
        window_posterior = self._soft_fusion(
            window_health,
            window_types,
            context,
            "window",
            self.window_threshold,
        )
        layer_posterior = self._soft_fusion(
            current_layer["layer_health_index"],
            layer_types,
            context,
            "layer",
            0.27023794,
        )
        specimen_posterior = (
            context
            if all_five_complete
            else self._soft_fusion(
                specimen_health,
                specimen_types,
                context,
                "specimen",
                0.403721,
            )
        )
        if override:
            specimen_posterior = np.full(len(self.states), 1e-10)
            specimen_posterior[self.states.index(override_state)] = 1.0
        return {
            "window_posterior": window_posterior,
            "layer_posterior": layer_posterior,
            "specimen_posterior": specimen_posterior,
            "context_posterior": context,
            "all_five_complete": all_five_complete,
            "method": (
                "full_five_layer_v13_8_at_completion"
                if all_five_complete
                else "causal_online_prefix_v13_9"
            ),
        }
