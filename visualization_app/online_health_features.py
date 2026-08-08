from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd
import torch
from sklearn.covariance import LedoitWolf
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler
from torch import nn

APP_DIR = Path(__file__).resolve().parent
STATE_DIR = APP_DIR.parent
WORKSPACE_DIR = STATE_DIR.parent
for module_path in (WORKSPACE_DIR, STATE_DIR):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

from run_physics_guided_health_indicator_v13 import (  # noqa: E402
    FeatureScaler,
    build_residual_features,
    response_feature_names,
    response_features,
)


EPS = 1e-8


def _finite(values: np.ndarray) -> np.ndarray:
    return np.nan_to_num(
        np.asarray(values, dtype=np.float64),
        nan=0.0,
        posinf=1e6,
        neginf=-1e6,
    )


def _pairwise_squared(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.maximum(
        np.sum(a * a, axis=1, keepdims=True)
        + np.sum(b * b, axis=1, keepdims=True).T
        - 2.0 * a @ b.T,
        0.0,
    )


@dataclass
class PCASPETransformer:
    scaler: StandardScaler
    components: np.ndarray
    eigenvalues: np.ndarray

    @classmethod
    def fit(cls, values: np.ndarray, normal_mask: np.ndarray) -> "PCASPETransformer":
        scaler = StandardScaler().fit(values[normal_mask])
        z = _finite(scaler.transform(values))
        limit = min(z.shape[1], max(1, int(normal_mask.sum()) - 1))
        pca = PCA(
            n_components=limit, svd_solver="full", random_state=0
        ).fit(z[normal_mask])
        cumulative = np.cumsum(pca.explained_variance_ratio_)
        retained = int(np.searchsorted(cumulative, 0.95) + 1)
        retained = min(max(2, retained), min(12, limit))
        return cls(
            scaler=scaler,
            components=pca.components_[:retained].copy(),
            eigenvalues=np.maximum(
                pca.explained_variance_[:retained], EPS
            ).copy(),
        )

    def transform(self, values: np.ndarray, _sequences: np.ndarray) -> np.ndarray:
        z = _finite(self.scaler.transform(values))
        scores = z @ self.components.T
        score_distance = scores / np.sqrt(self.eigenvalues)
        reconstruction = scores @ self.components
        spe = np.sum(np.square(z - reconstruction), axis=1, keepdims=True)
        t2 = np.sum(np.square(score_distance), axis=1, keepdims=True)
        return _finite(np.concatenate([score_distance, t2, spe], axis=1))


@dataclass
class RobustMahalanobisTransformer:
    scaler: StandardScaler
    precision: np.ndarray

    @classmethod
    def fit(
        cls, values: np.ndarray, normal_mask: np.ndarray
    ) -> "RobustMahalanobisTransformer":
        scaler = StandardScaler().fit(values[normal_mask])
        z = _finite(scaler.transform(values))
        covariance = LedoitWolf().fit(z[normal_mask])
        return cls(scaler=scaler, precision=covariance.precision_.copy())

    def transform(self, values: np.ndarray, _sequences: np.ndarray) -> np.ndarray:
        z = _finite(self.scaler.transform(values))
        projected = z @ self.precision
        contributions = np.abs(z * projected)
        distance2 = np.sum(z * projected, axis=1, keepdims=True)
        return _finite(np.concatenate([contributions, distance2], axis=1))


@dataclass
class KECASPETransformer:
    scaler: StandardScaler
    reference: np.ndarray
    gamma: float
    selected_values: np.ndarray
    selected_vectors: np.ndarray
    center: np.ndarray
    scale: np.ndarray

    @classmethod
    def fit(
        cls,
        values: np.ndarray,
        normal_mask: np.ndarray,
        seed: int,
        max_reference: int = 512,
        max_components: int = 10,
    ) -> "KECASPETransformer":
        scaler = StandardScaler().fit(values[normal_mask])
        z = _finite(scaler.transform(values))
        normal = z[normal_mask]
        rng = np.random.default_rng(seed)
        if len(normal) > max_reference:
            reference = normal[
                np.sort(rng.choice(len(normal), max_reference, replace=False))
            ]
        else:
            reference = normal.copy()
        probe = reference[
            np.linspace(0, len(reference) - 1, min(256, len(reference)), dtype=int)
        ]
        distances = _pairwise_squared(probe, probe)
        positive = distances[distances > EPS]
        gamma = 1.0 / max(
            float(np.median(positive)) if len(positive) else 1.0, EPS
        )
        kernel = np.exp(-gamma * _pairwise_squared(reference, reference))
        eigenvalues, eigenvectors = np.linalg.eigh((kernel + kernel.T) * 0.5)
        valid = eigenvalues > max(float(eigenvalues.max()) * 1e-10, EPS)
        eigenvalues = eigenvalues[valid]
        eigenvectors = eigenvectors[:, valid]
        entropy = np.square(
            np.sqrt(eigenvalues)
            * (eigenvectors.T @ np.ones(len(reference)))
        )
        retained = min(max_components, len(eigenvalues))
        chosen = np.argsort(entropy)[::-1][:retained]
        selected_values = eigenvalues[chosen]
        selected_vectors = eigenvectors[:, chosen]
        raw = cls._raw_transform(
            z, reference, gamma, selected_values, selected_vectors
        )
        normal_values = raw[normal_mask]
        center = np.mean(normal_values, axis=0)
        scale = np.maximum(np.std(normal_values, axis=0, ddof=0), EPS)
        return cls(
            scaler,
            reference,
            gamma,
            selected_values,
            selected_vectors,
            center,
            scale,
        )

    @staticmethod
    def _raw_transform(
        z: np.ndarray,
        reference: np.ndarray,
        gamma: float,
        selected_values: np.ndarray,
        selected_vectors: np.ndarray,
    ) -> np.ndarray:
        cross_kernel = np.exp(-gamma * _pairwise_squared(z, reference))
        scores = (
            cross_kernel @ selected_vectors / np.sqrt(selected_values)
        )
        spe = np.maximum(
            1.0 - np.sum(np.square(scores), axis=1, keepdims=True), 0.0
        )
        return _finite(np.concatenate([scores, spe], axis=1))

    def transform(self, values: np.ndarray, _sequences: np.ndarray) -> np.ndarray:
        z = _finite(self.scaler.transform(values))
        raw = self._raw_transform(
            z,
            self.reference,
            self.gamma,
            self.selected_values,
            self.selected_vectors,
        )
        return _finite((raw - self.center) / self.scale)


@dataclass
class WassersteinTransformer:
    templates: np.ndarray
    robust_scale: np.ndarray
    normal_mean: np.ndarray
    normal_std: np.ndarray

    @classmethod
    def fit(
        cls, sequences: np.ndarray, normal_mask: np.ndarray
    ) -> "WassersteinTransformer":
        sequences = _finite(sequences)
        n_steps = sequences.shape[1]
        normal_points = sequences[normal_mask].reshape(-1, sequences.shape[2])
        probabilities = (np.arange(n_steps, dtype=float) + 0.5) / n_steps
        templates = np.quantile(normal_points, probabilities, axis=0)
        q25, q75 = np.quantile(normal_points, [0.25, 0.75], axis=0)
        robust_scale = np.maximum((q75 - q25) / 1.349, EPS)
        normal_mean = np.mean(normal_points, axis=0)
        normal_std = np.maximum(np.std(normal_points, axis=0), EPS)
        return cls(templates, robust_scale, normal_mean, normal_std)

    def transform(self, _values: np.ndarray, sequences: np.ndarray) -> np.ndarray:
        sequences = _finite(sequences)
        ordered = np.sort(sequences, axis=1)
        difference = ordered - self.templates[None, :, :]
        w1 = np.mean(np.abs(difference), axis=1) / self.robust_scale
        w2 = (
            np.sqrt(np.mean(np.square(difference), axis=1))
            / self.robust_scale
        )
        mean_shift = (
            np.mean(sequences, axis=1) - self.normal_mean
        ) / self.robust_scale
        scale_shift = np.log(
            np.maximum(np.std(sequences, axis=1), EPS) / self.normal_std
        )
        return _finite(
            np.concatenate([w1, w2, mean_shift, scale_shift], axis=1)
        )


def _greedy_mcfs_indices(
    normal_values: np.ndarray, max_features: int = 24
) -> np.ndarray:
    q25, q75 = np.quantile(normal_values, [0.25, 0.75], axis=0)
    variability = np.maximum(q75 - q25, EPS)
    variability = variability / max(float(variability.max()), EPS)
    correlation = np.nan_to_num(
        np.abs(np.corrcoef(normal_values, rowvar=False)), nan=0.0
    )
    count = min(max_features, normal_values.shape[1])
    selected = [int(np.argmax(variability))]
    while len(selected) < count:
        redundancy = np.max(correlation[:, selected], axis=1)
        score = variability * (1.0 - np.clip(redundancy, 0.0, 1.0))
        score[selected] = -np.inf
        selected.append(int(np.argmax(score)))
    return np.asarray(selected, dtype=int)


class AttentiveVAE(nn.Module):
    def __init__(self, input_dim: int, latent_dim: int) -> None:
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 32),
            nn.ReLU(),
            nn.Linear(32, 24),
            nn.ReLU(),
        )
        self.mu = nn.Linear(24, latent_dim)
        self.logvar = nn.Linear(24, latent_dim)
        self.decoder = nn.Sequential(
            nn.Linear(latent_dim, 24),
            nn.ReLU(),
            nn.Linear(24, 32),
            nn.ReLU(),
        )
        self.base = nn.Linear(32, input_dim)
        self.correction = nn.Linear(32, input_dim)
        self.attention = nn.Sequential(nn.Linear(32, input_dim), nn.Sigmoid())

    def forward(self, batch: torch.Tensor, sample: bool = True):
        encoded = self.encoder(batch)
        mu = self.mu(encoded)
        logvar = torch.clamp(self.logvar(encoded), -8.0, 8.0)
        latent = (
            mu + torch.randn_like(mu) * torch.exp(0.5 * logvar)
            if sample
            else mu
        )
        decoded = self.decoder(latent)
        reconstruction = (
            self.base(decoded)
            + self.attention(decoded) * self.correction(decoded)
        )
        return reconstruction, mu, logvar


def _numpy_state(model: nn.Module) -> dict[str, np.ndarray]:
    return {
        key: value.detach().cpu().numpy().copy()
        for key, value in model.state_dict().items()
    }


def _load_numpy_state(model: nn.Module, state: dict[str, np.ndarray]) -> None:
    model.load_state_dict(
        {key: torch.from_numpy(value) for key, value in state.items()}
    )


@dataclass
class McFSAVAETransformer:
    scaler: StandardScaler
    selected: np.ndarray
    input_dim: int
    latent_dim: int
    state: dict[str, np.ndarray]

    @classmethod
    def fit(
        cls,
        values: np.ndarray,
        normal_mask: np.ndarray,
        seed: int,
        epochs: int = 45,
    ) -> "McFSAVAETransformer":
        torch.manual_seed(seed)
        torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
        scaler = StandardScaler().fit(values[normal_mask])
        z = _finite(scaler.transform(values)).astype(np.float32)
        selected = _greedy_mcfs_indices(z[normal_mask])
        x = z[:, selected]
        normal = torch.from_numpy(x[normal_mask])
        input_dim = x.shape[1]
        latent_dim = min(8, max(3, input_dim // 3))
        model = AttentiveVAE(input_dim, latent_dim)
        optimizer = torch.optim.Adam(
            model.parameters(), lr=1e-3, weight_decay=1e-5
        )
        generator = torch.Generator().manual_seed(seed)
        best_loss = np.inf
        best_state = None
        stale = 0
        for _ in range(epochs):
            order = torch.randperm(len(normal), generator=generator)
            total = 0.0
            model.train()
            for start in range(0, len(normal), 128):
                batch = normal[order[start : start + 128]]
                reconstruction, mu, logvar = model(batch, sample=True)
                mse = torch.mean(torch.square(reconstruction - batch))
                kl = -0.5 * torch.mean(
                    1.0 + logvar - torch.square(mu) - torch.exp(logvar)
                )
                loss = mse + 1e-3 * kl
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                total += float(loss.detach()) * len(batch)
            epoch_loss = total / max(len(normal), 1)
            if epoch_loss < best_loss - 1e-5:
                best_loss = epoch_loss
                best_state = _numpy_state(model)
                stale = 0
            else:
                stale += 1
                if stale >= 8:
                    break
        if best_state is not None:
            _load_numpy_state(model, best_state)
        return cls(
            scaler,
            selected,
            input_dim,
            latent_dim,
            _numpy_state(model),
        )

    def transform(self, values: np.ndarray, _sequences: np.ndarray) -> np.ndarray:
        z = _finite(self.scaler.transform(values)).astype(np.float32)
        x = z[:, self.selected]
        model = AttentiveVAE(self.input_dim, self.latent_dim)
        _load_numpy_state(model, self.state)
        model.eval()
        with torch.no_grad():
            batch = torch.from_numpy(x)
            reconstruction, mu, logvar = model(batch, sample=False)
            error = torch.abs(reconstruction - batch)
            mse = torch.mean(
                torch.square(reconstruction - batch), dim=1, keepdim=True
            )
            kl = -0.5 * torch.mean(
                1.0 + logvar - torch.square(mu) - torch.exp(logvar),
                dim=1,
                keepdim=True,
            )
            output = torch.cat([mu, error, mse, kl], dim=1).cpu().numpy()
        return _finite(output)


class SequenceAutoencoder(nn.Module):
    def __init__(self, steps: int, channels: int, hidden: int = 16) -> None:
        super().__init__()
        self.steps = steps
        self.conv = nn.Sequential(
            nn.Conv1d(channels, 24, kernel_size=3, padding=1), nn.ReLU()
        )
        self.encoder = nn.LSTM(24, hidden, batch_first=True)
        self.decoder = nn.LSTM(hidden, 24, batch_first=True)
        self.output = nn.Linear(24, channels)

    def forward(self, batch: torch.Tensor):
        convolved = self.conv(batch.transpose(1, 2)).transpose(1, 2)
        _, (state, _) = self.encoder(convolved)
        latent = state[-1]
        decoded, _ = self.decoder(
            latent[:, None, :].repeat(1, self.steps, 1)
        )
        return self.output(decoded), latent


@dataclass
class CNNLSTMAETransformer:
    center: np.ndarray
    scale: np.ndarray
    steps: int
    channels: int
    state: dict[str, np.ndarray]

    @classmethod
    def fit(
        cls,
        sequences: np.ndarray,
        normal_mask: np.ndarray,
        seed: int,
        epochs: int = 35,
    ) -> "CNNLSTMAETransformer":
        torch.manual_seed(seed + 17)
        torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
        sequences = _finite(sequences).astype(np.float32)
        normal_points = sequences[normal_mask].reshape(-1, sequences.shape[2])
        center = normal_points.mean(axis=0)
        scale = np.maximum(normal_points.std(axis=0), 1e-4)
        x = ((sequences - center) / scale).astype(np.float32)
        normal = torch.from_numpy(x[normal_mask])
        steps, channels = x.shape[1], x.shape[2]
        model = SequenceAutoencoder(steps, channels)
        optimizer = torch.optim.Adam(
            model.parameters(), lr=1e-3, weight_decay=1e-5
        )
        generator = torch.Generator().manual_seed(seed + 17)
        best_loss = np.inf
        best_state = None
        stale = 0
        for _ in range(epochs):
            order = torch.randperm(len(normal), generator=generator)
            total = 0.0
            model.train()
            for start in range(0, len(normal), 96):
                batch = normal[order[start : start + 96]]
                reconstruction, _ = model(batch)
                loss = torch.mean(torch.square(reconstruction - batch))
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                total += float(loss.detach()) * len(batch)
            epoch_loss = total / max(len(normal), 1)
            if epoch_loss < best_loss - 1e-5:
                best_loss = epoch_loss
                best_state = _numpy_state(model)
                stale = 0
            else:
                stale += 1
                if stale >= 7:
                    break
        if best_state is not None:
            _load_numpy_state(model, best_state)
        return cls(center, scale, steps, channels, _numpy_state(model))

    def transform(self, _values: np.ndarray, sequences: np.ndarray) -> np.ndarray:
        x = ((_finite(sequences) - self.center) / self.scale).astype(np.float32)
        model = SequenceAutoencoder(self.steps, self.channels)
        _load_numpy_state(model, self.state)
        model.eval()
        with torch.no_grad():
            batch = torch.from_numpy(x)
            reconstruction, latent = model(batch)
            difference = reconstruction - batch
            rmse = torch.sqrt(
                torch.mean(torch.square(difference), dim=1) + 1e-8
            )
            mae = torch.mean(torch.abs(difference), dim=1)
            total_mse = torch.mean(
                torch.square(difference), dim=(1, 2), keepdim=False
            )[:, None]
            output = torch.cat(
                [latent, rmse, mae, total_mse], dim=1
            ).cpu().numpy()
        return _finite(output)


def fit_literature_transformers(
    base_values: np.ndarray,
    sensor_sequences: np.ndarray,
    metadata: pd.DataFrame,
    seed: int,
) -> dict[str, Any]:
    normal_mask = (
        metadata["dataset_split"].eq("train")
        & metadata["true_specimen_state"].eq("normal")
        & metadata["window_training_eligible"].astype(bool)
    ).to_numpy(dtype=bool)
    return {
        "pca_spe": PCASPETransformer.fit(base_values, normal_mask),
        "keca_spe": KECASPETransformer.fit(
            base_values, normal_mask, seed
        ),
        "mcfs_avae": McFSAVAETransformer.fit(
            base_values, normal_mask, seed
        ),
        "cnn_lstm_ae": CNNLSTMAETransformer.fit(
            sensor_sequences, normal_mask, seed
        ),
        "wasserstein": WassersteinTransformer.fit(
            sensor_sequences, normal_mask
        ),
        "robust_mahalanobis": RobustMahalanobisTransformer.fit(
            base_values, normal_mask
        ),
    }


class OnlineWindowFeatureEngine:
    """Generate all 12 benchmark HI feature vectors from one completed window."""

    def __init__(
        self,
        scaler: FeatureScaler,
        ambient: float,
        coherence_floor: np.ndarray,
        transformers: dict[str, Any],
    ) -> None:
        self.scaler = scaler
        self.ambient = float(ambient)
        self.coherence_floor = np.asarray(coherence_floor, dtype=float)
        self.transformers = transformers
        _, self.groups = response_feature_names()

    def transform(
        self,
        actual: np.ndarray,
        prediction: np.ndarray,
        requested_key: str | None = None,
    ) -> dict[str, np.ndarray]:
        actual = np.asarray(actual, dtype=float)
        prediction = np.asarray(prediction, dtype=float)
        if actual.ndim == 2:
            actual = actual[None, ...]
        if prediction.ndim == 2:
            prediction = prediction[None, ...]
        if (
            actual.shape != prediction.shape
            or actual.shape[1:] != (24, 12)
            or not np.isfinite(actual).all()
            or not np.isfinite(prediction).all()
        ):
            raise ValueError(
                "online HI requires finite actual/prediction arrays [N,24,12]"
            )
        response = response_features(actual, prediction, self.ambient)
        residual = build_residual_features(
            self.scaler.transform_sensors(actual),
            self.scaler.transform_sensors(prediction),
            self.coherence_floor,
        )
        all_response = response[:, self.groups["all"]]
        base = np.concatenate([all_response, residual], axis=1)
        features = {
            "thermal_response": response[:, self.groups["thermal"]],
            "compaction_response": response[:, self.groups["compaction"]],
            "thermomechanical_response": all_response,
            "residual": residual,
            "response_plus_residual": base,
        }
        if requested_key in features:
            return {requested_key: features[requested_key]}
        if requested_key is not None:
            if requested_key not in self.transformers:
                raise KeyError(f"unknown online HI feature key: {requested_key}")
            transformer = self.transformers[requested_key]
            return {requested_key: transformer.transform(base, actual)}
        for key, transformer in self.transformers.items():
            features[key] = transformer.transform(base, actual)
        return features
