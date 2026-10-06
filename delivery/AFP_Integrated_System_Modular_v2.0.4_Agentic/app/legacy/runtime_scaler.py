"""Small runtime-only scaler used by the desktop dashboard.

Keeping it here avoids importing the offline experiment pipeline (and its
training/plotting dependencies) when the packaged application starts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


SENSOR_MODEL_INDICES = np.asarray([*range(0, 11), 16], dtype=int)


@dataclass
class FeatureScaler:
    mean: np.ndarray
    scale: np.ndarray
    source: Path

    def inverse_full(self, values: np.ndarray) -> np.ndarray:
        return np.asarray(values, dtype=float) * self.scale + self.mean

    def inverse_sensors(self, values: np.ndarray) -> np.ndarray:
        return (
            np.asarray(values, dtype=float) * self.scale[SENSOR_MODEL_INDICES]
            + self.mean[SENSOR_MODEL_INDICES]
        )

    def transform_sensors(self, values: np.ndarray) -> np.ndarray:
        return (
            np.asarray(values, dtype=float) - self.mean[SENSOR_MODEL_INDICES]
        ) / self.scale[SENSOR_MODEL_INDICES]

    def inverse_params(self, standardized_condition: np.ndarray) -> np.ndarray:
        indices = np.asarray([13, 12, 14, 15], dtype=int)
        return (
            np.asarray(standardized_condition, dtype=float) * self.scale[indices]
            + self.mean[indices]
        )
