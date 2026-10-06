"""Adaptive Terminal-Aligned Variance Normalization (ATAVN).

The AFP manuscript describes ATAVN as a symmetric, closed-loop transform:
the last observation in a window is used as a terminal-aligned baseline,
the window is scaled by its unbiased temporal standard deviation, and the
predicted residual is restored to the original standardized scale.  Keeping
the transform outside the individual forecasting architectures lets the
same mechanism be used fairly for the thesis comparison models.
"""

from __future__ import annotations

from typing import Tuple

import torch


ATAVN_VERSION = "terminal_aligned_unbiased_v1"
ATAVN_EPS = 1.0e-5
ATAVN_MIN_SCALE = 1.0e-1


def statistics(x: torch.Tensor, eps: float = ATAVN_EPS) -> Tuple[torch.Tensor, torch.Tensor]:
    """Return terminal baseline and unbiased temporal scale for ``[B,T,C]``."""
    if x.ndim != 3:
        raise ValueError(f"ATAVN expects [batch,time,channels], got {tuple(x.shape)}")
    terminal = x[:, -1:, :].detach()
    if x.shape[1] > 1:
        scale = torch.std(x, dim=1, keepdim=True, unbiased=True)
    else:
        scale = torch.zeros_like(terminal)
    scale = torch.nan_to_num(scale, nan=0.0, posinf=0.0, neginf=0.0)
    # Add the smoothing term under the square root, as in the manuscript's
    # variance-normalization formulation.  This avoids exploding residuals
    # for an almost constant sensor channel while preserving transient peaks.
    scale = torch.sqrt(scale.square() + float(eps))
    # The legacy AFP channels contain long near-constant stretches followed
    # by abrupt process transitions.  A small relative floor prevents those
    # transitions from becoming unbounded residual targets after global
    # standardization, while leaving ordinary transient variance unchanged.
    scale = torch.clamp(scale, min=ATAVN_MIN_SCALE)
    return terminal, scale


def normalize(
    x: torch.Tensor,
    terminal: torch.Tensor | None = None,
    scale: torch.Tensor | None = None,
    eps: float = ATAVN_EPS,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Terminal-align and variance-normalize a sequence or target tensor."""
    if terminal is None or scale is None:
        terminal, scale = statistics(x, eps=eps)
    return (x - terminal) / scale, terminal, scale


def restore(residual: torch.Tensor, terminal: torch.Tensor, scale: torch.Tensor) -> torch.Tensor:
    """Restore a normalized residual to the standardized absolute scale."""
    return residual * scale + terminal


def metadata(mode: str = "external") -> dict:
    return {
        "enabled": True,
        "version": ATAVN_VERSION,
        "mode": str(mode),
        "epsilon": ATAVN_EPS,
        "minimum_scale": ATAVN_MIN_SCALE,
        "terminal": "last_observation",
        "scale": "unbiased_temporal_std",
    }
