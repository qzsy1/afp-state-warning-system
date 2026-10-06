from __future__ import annotations

from dataclasses import dataclass


class GateConfigError(ValueError):
    """Raised when gate configuration cannot be trusted."""


@dataclass(frozen=True)
class RequirementSpec:
    id: str
    title: str
    mandatory: bool
    profiles: tuple[str, ...]


@dataclass(frozen=True)
class CheckSpec:
    id: str
    title: str
    kind: str
    command: tuple[str, ...]
    cwd: str
    timeout_seconds: float
    profiles: tuple[str, ...]
    environments: tuple[str, ...]
    blocking: bool
    evidence_tier: str
    requirements: tuple[str, ...]
    change_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegressionMatrix:
    schema_version: int
    profiles: tuple[str, ...]
    requirements: tuple[RequirementSpec, ...]
    checks: tuple[CheckSpec, ...]
