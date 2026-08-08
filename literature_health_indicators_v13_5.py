# -*- coding: utf-8 -*-
"""Leakage-safe literature and classical HI feature constructions for AFP.

All reference distributions and representation models are fitted with measured
normal windows from training specimens only.  The returned vectors are then
used by the same four downstream classifiers as the AFP-specific indicators.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np
import pandas as pd
# Import torch before NumPy/Scikit heavy linear-algebra work.  On the user's
# Windows CUDA environment, a first lazy torch import after MKL eigensolvers can
# trigger an optree DLL access violation.
import torch
from sklearn.covariance import LedoitWolf
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler


EPS = 1e-8


def _finite(values: np.ndarray) -> np.ndarray:
    return np.nan_to_num(np.asarray(values, dtype=np.float64), nan=0.0, posinf=1e6, neginf=-1e6)


def _normal_training_mask(metadata: pd.DataFrame) -> np.ndarray:
    return (
        metadata["dataset_split"].eq("train")
        & metadata["true_specimen_state"].eq("normal")
        & metadata["window_training_eligible"].astype(bool)
    ).to_numpy(dtype=bool)


def pca_spe_features(values: np.ndarray, normal_mask: np.ndarray) -> np.ndarray:
    """PCA score distance, Hotelling T2 and residual-space SPE/Q."""
    scaler = StandardScaler().fit(values[normal_mask])
    z = _finite(scaler.transform(values))
    limit = min(z.shape[1], max(1, int(normal_mask.sum()) - 1))
    pca = PCA(n_components=limit, svd_solver="full", random_state=0).fit(z[normal_mask])
    cumulative = np.cumsum(pca.explained_variance_ratio_)
    retained = int(np.searchsorted(cumulative, 0.95) + 1)
    retained = min(max(2, retained), min(12, limit))
    components = pca.components_[:retained]
    eigenvalues = np.maximum(pca.explained_variance_[:retained], EPS)
    scores = z @ components.T
    score_distance = scores / np.sqrt(eigenvalues)
    reconstruction = scores @ components
    spe = np.sum(np.square(z - reconstruction), axis=1, keepdims=True)
    t2 = np.sum(np.square(score_distance), axis=1, keepdims=True)
    return _finite(np.concatenate([score_distance, t2, spe], axis=1))


def robust_mahalanobis_features(values: np.ndarray, normal_mask: np.ndarray) -> np.ndarray:
    """Ledoit-Wolf regularized Mahalanobis distance and feature contributions."""
    scaler = StandardScaler().fit(values[normal_mask])
    z = _finite(scaler.transform(values))
    covariance = LedoitWolf().fit(z[normal_mask])
    projected = z @ covariance.precision_
    contributions = np.abs(z * projected)
    distance2 = np.sum(z * projected, axis=1, keepdims=True)
    return _finite(np.concatenate([contributions, distance2], axis=1))


def _pairwise_squared(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.maximum(
        np.sum(a * a, axis=1, keepdims=True)
        + np.sum(b * b, axis=1, keepdims=True).T
        - 2.0 * a @ b.T,
        0.0,
    )


def keca_spe_features(
    values: np.ndarray,
    normal_mask: np.ndarray,
    seed: int,
    max_reference: int = 512,
    max_components: int = 10,
) -> np.ndarray:
    """Kernel entropy component scores plus kernel residual-space SPE.

    Entropy contribution follows the KECA ordering term
    (sqrt(lambda_j) * e_j^T 1)^2.  The RBF feature-space reconstruction
    residual is used as the health-deviation statistic.
    """
    scaler = StandardScaler().fit(values[normal_mask])
    z = _finite(scaler.transform(values))
    normal = z[normal_mask]
    rng = np.random.default_rng(seed)
    if len(normal) > max_reference:
        reference = normal[np.sort(rng.choice(len(normal), max_reference, replace=False))]
    else:
        reference = normal.copy()
    probe = reference[np.linspace(0, len(reference) - 1, min(256, len(reference)), dtype=int)]
    distances = _pairwise_squared(probe, probe)
    positive = distances[distances > EPS]
    gamma = 1.0 / max(float(np.median(positive)) if len(positive) else 1.0, EPS)
    kernel = np.exp(-gamma * _pairwise_squared(reference, reference))
    eigenvalues, eigenvectors = np.linalg.eigh((kernel + kernel.T) * 0.5)
    valid = eigenvalues > max(float(eigenvalues.max()) * 1e-10, EPS)
    eigenvalues = eigenvalues[valid]
    eigenvectors = eigenvectors[:, valid]
    entropy = np.square(np.sqrt(eigenvalues) * (eigenvectors.T @ np.ones(len(reference))))
    retained = min(max_components, len(eigenvalues))
    chosen = np.argsort(entropy)[::-1][:retained]
    selected_values = eigenvalues[chosen]
    selected_vectors = eigenvectors[:, chosen]
    outputs = []
    for start in range(0, len(z), 512):
        cross_kernel = np.exp(-gamma * _pairwise_squared(z[start:start + 512], reference))
        scores = cross_kernel @ selected_vectors / np.sqrt(selected_values)
        spe = np.maximum(1.0 - np.sum(np.square(scores), axis=1, keepdims=True), 0.0)
        outputs.append(np.concatenate([scores, spe], axis=1))
    transformed = _finite(np.concatenate(outputs, axis=0))
    normal_values = transformed[normal_mask]
    scale = np.maximum(np.std(normal_values, axis=0, ddof=0), EPS)
    center = np.mean(normal_values, axis=0)
    return _finite((transformed - center) / scale)


def wasserstein_features(sensor_sequences: np.ndarray, normal_mask: np.ndarray) -> np.ndarray:
    """Per-sensor W1/W2 distances and location/scale shifts from normal AFP windows."""
    sequences = _finite(sensor_sequences)
    n_samples, n_steps, n_channels = sequences.shape
    normal_points = sequences[normal_mask].reshape(-1, n_channels)
    probabilities = (np.arange(n_steps, dtype=float) + 0.5) / n_steps
    templates = np.quantile(normal_points, probabilities, axis=0)
    q25, q75 = np.quantile(normal_points, [0.25, 0.75], axis=0)
    scale = np.maximum((q75 - q25) / 1.349, EPS)
    normal_mean = np.mean(normal_points, axis=0)
    normal_std = np.maximum(np.std(normal_points, axis=0), EPS)
    ordered = np.sort(sequences, axis=1)
    difference = ordered - templates[None, :, :]
    w1 = np.mean(np.abs(difference), axis=1) / scale
    w2 = np.sqrt(np.mean(np.square(difference), axis=1)) / scale
    mean_shift = (np.mean(sequences, axis=1) - normal_mean) / scale
    scale_shift = np.log(np.maximum(np.std(sequences, axis=1), EPS) / normal_std)
    return _finite(np.concatenate([w1, w2, mean_shift, scale_shift], axis=1))


def _greedy_mcfs_indices(normal_values: np.ndarray, max_features: int = 24) -> np.ndarray:
    """Unsupervised multi-criterion feature selection: variability with redundancy control."""
    q25, q75 = np.quantile(normal_values, [0.25, 0.75], axis=0)
    variability = np.maximum(q75 - q25, EPS)
    variability = variability / max(float(variability.max()), EPS)
    correlation = np.nan_to_num(np.abs(np.corrcoef(normal_values, rowvar=False)), nan=0.0)
    count = min(max_features, normal_values.shape[1])
    selected = [int(np.argmax(variability))]
    while len(selected) < count:
        redundancy = np.max(correlation[:, selected], axis=1)
        score = variability * (1.0 - np.clip(redundancy, 0.0, 1.0))
        score[selected] = -np.inf
        selected.append(int(np.argmax(score)))
    return np.asarray(selected, dtype=int)


def mcfs_avae_features(
    values: np.ndarray,
    normal_mask: np.ndarray,
    seed: int,
    epochs: int = 45,
) -> Tuple[np.ndarray, np.ndarray]:
    """McFS-selected attentive VAE latent and reconstruction-deviation features."""
    import torch
    from torch import nn

    torch.manual_seed(seed)
    torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
    scaler = StandardScaler().fit(values[normal_mask])
    z = _finite(scaler.transform(values)).astype(np.float32)
    selected = _greedy_mcfs_indices(z[normal_mask])
    x = z[:, selected]
    normal = torch.from_numpy(x[normal_mask])
    input_dim = x.shape[1]
    latent_dim = min(8, max(3, input_dim // 3))

    class AttentiveVAE(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.encoder = nn.Sequential(nn.Linear(input_dim, 32), nn.ReLU(), nn.Linear(32, 24), nn.ReLU())
            self.mu = nn.Linear(24, latent_dim)
            self.logvar = nn.Linear(24, latent_dim)
            self.decoder = nn.Sequential(nn.Linear(latent_dim, 24), nn.ReLU(), nn.Linear(24, 32), nn.ReLU())
            self.base = nn.Linear(32, input_dim)
            self.correction = nn.Linear(32, input_dim)
            self.attention = nn.Sequential(nn.Linear(32, input_dim), nn.Sigmoid())

        def forward(self, batch: torch.Tensor, sample: bool = True):
            encoded = self.encoder(batch)
            mu = self.mu(encoded)
            logvar = torch.clamp(self.logvar(encoded), -8.0, 8.0)
            latent = mu + torch.randn_like(mu) * torch.exp(0.5 * logvar) if sample else mu
            decoded = self.decoder(latent)
            reconstruction = self.base(decoded) + self.attention(decoded) * self.correction(decoded)
            return reconstruction, mu, logvar

    model = AttentiveVAE()
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-5)
    generator = torch.Generator().manual_seed(seed)
    best_loss = np.inf
    best_state = None
    stale = 0
    for _ in range(epochs):
        order = torch.randperm(len(normal), generator=generator)
        total = 0.0
        model.train()
        for start in range(0, len(normal), 128):
            batch = normal[order[start:start + 128]]
            reconstruction, mu, logvar = model(batch, sample=True)
            mse = torch.mean(torch.square(reconstruction - batch))
            kl = -0.5 * torch.mean(1.0 + logvar - torch.square(mu) - torch.exp(logvar))
            loss = mse + 1e-3 * kl
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += float(loss.detach()) * len(batch)
        epoch_loss = total / max(len(normal), 1)
        if epoch_loss < best_loss - 1e-5:
            best_loss = epoch_loss
            best_state = {key: value.detach().clone() for key, value in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
            if stale >= 8:
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()
    outputs = []
    with torch.no_grad():
        tensor = torch.from_numpy(x)
        for start in range(0, len(tensor), 512):
            batch = tensor[start:start + 512]
            reconstruction, mu, logvar = model(batch, sample=False)
            error = torch.abs(reconstruction - batch)
            mse = torch.mean(torch.square(reconstruction - batch), dim=1, keepdim=True)
            kl = -0.5 * torch.mean(1.0 + logvar - torch.square(mu) - torch.exp(logvar), dim=1, keepdim=True)
            outputs.append(torch.cat([mu, error, mse, kl], dim=1).cpu().numpy())
    return _finite(np.concatenate(outputs, axis=0)), selected


def cnn_lstm_ae_features(
    sensor_sequences: np.ndarray,
    normal_mask: np.ndarray,
    seed: int,
    epochs: int = 35,
) -> np.ndarray:
    """CNN-LSTM autoencoder latent and channel-wise reconstruction-error HI features."""
    import torch
    from torch import nn

    torch.manual_seed(seed + 17)
    torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
    sequences = _finite(sensor_sequences).astype(np.float32)
    normal_points = sequences[normal_mask].reshape(-1, sequences.shape[2])
    center = normal_points.mean(axis=0)
    scale = np.maximum(normal_points.std(axis=0), 1e-4)
    x = ((sequences - center) / scale).astype(np.float32)
    normal = torch.from_numpy(x[normal_mask])
    steps, channels = x.shape[1], x.shape[2]
    hidden = 16

    class SequenceAutoencoder(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.conv = nn.Sequential(nn.Conv1d(channels, 24, kernel_size=3, padding=1), nn.ReLU())
            self.encoder = nn.LSTM(24, hidden, batch_first=True)
            self.decoder = nn.LSTM(hidden, 24, batch_first=True)
            self.output = nn.Linear(24, channels)

        def forward(self, batch: torch.Tensor):
            convolved = self.conv(batch.transpose(1, 2)).transpose(1, 2)
            _, (state, _) = self.encoder(convolved)
            latent = state[-1]
            decoded, _ = self.decoder(latent[:, None, :].repeat(1, steps, 1))
            return self.output(decoded), latent

    model = SequenceAutoencoder()
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-5)
    generator = torch.Generator().manual_seed(seed + 17)
    best_loss = np.inf
    best_state = None
    stale = 0
    for _ in range(epochs):
        order = torch.randperm(len(normal), generator=generator)
        total = 0.0
        model.train()
        for start in range(0, len(normal), 96):
            batch = normal[order[start:start + 96]]
            reconstruction, _ = model(batch)
            loss = torch.mean(torch.square(reconstruction - batch))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += float(loss.detach()) * len(batch)
        epoch_loss = total / max(len(normal), 1)
        if epoch_loss < best_loss - 1e-5:
            best_loss = epoch_loss
            best_state = {key: value.detach().clone() for key, value in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
            if stale >= 7:
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()
    outputs = []
    with torch.no_grad():
        tensor = torch.from_numpy(x)
        for start in range(0, len(tensor), 256):
            batch = tensor[start:start + 256]
            reconstruction, latent = model(batch)
            difference = reconstruction - batch
            rmse = torch.sqrt(torch.mean(torch.square(difference), dim=1) + 1e-8)
            mae = torch.mean(torch.abs(difference), dim=1)
            total_mse = torch.mean(torch.square(difference), dim=(1, 2), keepdim=False)[:, None]
            outputs.append(torch.cat([latent, rmse, mae, total_mse], dim=1).cpu().numpy())
    return _finite(np.concatenate(outputs, axis=0))


@dataclass
class LiteratureFeatureResult:
    features: Dict[str, np.ndarray]
    audit: pd.DataFrame


def build_literature_feature_sets(
    response_plus_residual: np.ndarray,
    sensor_sequences: np.ndarray,
    metadata: pd.DataFrame,
    seed: int,
) -> LiteratureFeatureResult:
    normal_mask = _normal_training_mask(metadata)
    if int(normal_mask.sum()) < 20:
        raise RuntimeError("Fewer than 20 normal training windows are available for literature HI fitting")
    base = _finite(response_plus_residual)
    features: Dict[str, np.ndarray] = {
        "pca_spe": pca_spe_features(base, normal_mask),
        "keca_spe": keca_spe_features(base, normal_mask, seed),
        "wasserstein": wasserstein_features(sensor_sequences, normal_mask),
        "robust_mahalanobis": robust_mahalanobis_features(base, normal_mask),
    }
    avae, selected = mcfs_avae_features(base, normal_mask, seed)
    features["mcfs_avae"] = avae
    features["cnn_lstm_ae"] = cnn_lstm_ae_features(sensor_sequences, normal_mask, seed)
    audit = literature_indicator_audit()
    audit["normal_training_windows"] = int(normal_mask.sum())
    audit.loc[audit["feature_key"].eq("mcfs_avae"), "implementation_detail"] += (
        f"; McFS retained {len(selected)} of {base.shape[1]} response/residual features"
    )
    for key, values in features.items():
        audit.loc[audit["feature_key"].eq(key), "output_feature_count"] = int(values.shape[1])
    return LiteratureFeatureResult(features=features, audit=audit)


def literature_indicator_audit() -> pd.DataFrame:
    rows = [
        {
            "indicator_family": "PCA-SPE-HI", "feature_key": "pca_spe",
            "source_type": "classical statistical baseline",
            "reference": "Hotelling T2 and PCA squared prediction error/Q statistic",
            "doi": "",
            "local_file": "",
            "reproduction_level": "standard exact construction",
            "implementation_detail": "95% normal-training variance, capped at 12 PCs; retained scores, T2 and SPE",
            "included_in_benchmark": True,
            "uses_process_parameters": False,
        },
        {
            "indicator_family": "KECA-SPE-HI", "feature_key": "keca_spe",
            "source_type": "user-provided literature",
            "reference": "Jing et al., KECA-GRNN wind-turbine gearbox state monitoring and health assessment (2021)",
            "doi": "10.19912/j.0254-0096.tynxb.2020-0005",
            "local_file": "6-重点学习思路和KECA这个方法-基于KECA-GRNN的风...组齿轮箱状态监测与健康评估_景彤梅-已读.pdf",
            "reproduction_level": "mechanism reproduced",
            "implementation_detail": "RBF KECA components ranked by entropy contribution plus kernel-space SPE",
            "included_in_benchmark": True,
            "uses_process_parameters": False,
        },
        {
            "indicator_family": "McFS-AVAE-HI", "feature_key": "mcfs_avae",
            "source_type": "user-provided literature",
            "reference": "Li et al., Unsupervised construction of health indicator via McFS and attentive VAE (2024)",
            "doi": "10.1007/s11431-023-2610-4",
            "local_file": "1区无监督学习健康指标用于旋转机械.pdf",
            "reproduction_level": "AFP-adapted reproduction",
            "implementation_detail": "normal-only multi-criterion selection, attentive VAE latent, reconstruction and KL deviations",
            "included_in_benchmark": True,
            "uses_process_parameters": False,
        },
        {
            "indicator_family": "CNN-LSTM-AE-HI", "feature_key": "cnn_lstm_ae",
            "source_type": "user-provided literature",
            "reference": "Chen et al., Wind turbine gearbox condition monitoring using AI-enabled virtual indicators (2024)",
            "doi": "10.1088/1361-6501/ad5c8e",
            "local_file": "基于CNN-LSTM-AE的齿轮箱状态监测-已读.pdf",
            "reproduction_level": "architecture reproduced for AFP windows",
            "implementation_detail": "12-channel 24-step CNN-LSTM autoencoder latent and channel reconstruction errors",
            "included_in_benchmark": True,
            "uses_process_parameters": False,
        },
        {
            "indicator_family": "W-HI", "feature_key": "wasserstein",
            "source_type": "user-provided literature",
            "reference": "Feng et al., Cyclic correntropy and Wasserstein-distance composite health indicator (Wear, 2023)",
            "doi": "10.1016/j.wear.2023.204697",
            "local_file": "一种基于振动的智能制造系统表面磨损过程中齿轮健康管理的新方案-泛读-基于循环熵和W距离的综合指标.pdf",
            "reproduction_level": "Wasserstein component reproduced",
            "implementation_detail": "per-sensor W1/W2 distribution distances and location/scale shifts to normal AFP reference",
            "included_in_benchmark": True,
            "uses_process_parameters": False,
        },
        {
            "indicator_family": "RMD-HI", "feature_key": "robust_mahalanobis",
            "source_type": "classical statistical baseline",
            "reference": "regularized Mahalanobis normal-distance health indicator",
            "doi": "",
            "local_file": "",
            "reproduction_level": "standard exact construction",
            "implementation_detail": "Ledoit-Wolf normal covariance, squared distance and feature contributions",
            "included_in_benchmark": True,
            "uses_process_parameters": False,
        },
        {
            "indicator_family": "MCAN-RCD-HI", "feature_key": "not_run",
            "source_type": "user-provided literature",
            "reference": "Guo et al., An unsupervised feature learning based HI construction method (MSSP, 2022)",
            "doi": "10.1016/j.ymssp.2021.108573",
            "local_file": "10-论文2 基于无监督特征学习的机器性能评估健康指标构建方法.pdf",
            "reproduction_level": "not directly comparable",
            "implementation_detail": "requires degradation-tendency weighting and a run-to-failure trajectory; current AFP specimens are short independent runs",
            "included_in_benchmark": False,
            "uses_process_parameters": False,
        },
        {
            "indicator_family": "PSN-HI", "feature_key": "not_run",
            "source_type": "user-provided literature",
            "reference": "Chen et al., Polynomial speed-normalized HI for variable-speed wind-turbine bearings (2025)",
            "doi": "10.1016/j.aei.2025.103455",
            "local_file": "1区新的HI用于风电机组状态预警.pdf",
            "reproduction_level": "not reproducible with current data",
            "implementation_detail": "requires continuously varying speed and long bearing life-cycle vibration; AFP has two nominal layup speeds and 24-step windows",
            "included_in_benchmark": False,
            "uses_process_parameters": False,
        },
        {
            "indicator_family": "ASPD-HI", "feature_key": "not_run",
            "source_type": "user-provided literature",
            "reference": "Zhang et al., HI based on signal probability-distribution measures (MSSP, 2023)",
            "doi": "10.1016/j.ymssp.2023.110460",
            "local_file": "1区重庆大学新型HI.pdf",
            "reproduction_level": "not reproducible with current data",
            "implementation_detail": "alpha-stable distribution estimation is unreliable for heterogeneous 24-point AFP windows and needs long vibration records",
            "included_in_benchmark": False,
            "uses_process_parameters": False,
        },
    ]
    return pd.DataFrame(rows)
