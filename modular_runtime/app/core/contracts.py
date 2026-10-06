from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class SampleFrame:
    """One causal acquisition sample shared by all acquisition modules."""

    timestamp: str
    schema_mode: str
    condition_id: str
    replicate: int
    layer_id: int
    sample_index: int
    values: dict[str, float]
    source_interface: str = ""
    quality: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.replicate < 1 or self.layer_id < 1 or self.sample_index < 0:
            raise ValueError("replicate/layer_id must start at 1 and sample_index at 0")
        if not self.values:
            raise ValueError("sample frame has no channel values")
        if any(not isinstance(name, str) or not name for name in self.values):
            raise ValueError("sample frame contains an invalid channel name")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass(slots=True)
class PredictionFrame:
    """Forecast aligned to the sample index where it was emitted."""

    emitted_at: str
    origin_sample_index: int
    horizon: int
    model_id: str
    input_channels: list[str]
    output_channels: list[str]
    values: dict[str, list[float]]
    model_metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.origin_sample_index < 0 or self.horizon < 1:
            raise ValueError("prediction origin and horizon are invalid")
        missing = set(self.output_channels) - set(self.values)
        if missing:
            raise ValueError(f"prediction is missing output channels: {sorted(missing)}")
        wrong = [name for name in self.output_channels if len(self.values[name]) != self.horizon]
        if wrong:
            raise ValueError(f"prediction horizon mismatch for: {wrong}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass(slots=True)
class HealthEvidence:
    """Window/layer/specimen evidence without conflating prediction and diagnosis."""

    indicator: str
    scorer: str
    window_score: float | None = None
    layer_score: float | None = None
    specimen_score: float | None = None
    threshold: float | None = None
    binary_state: str = "unknown"
    abnormal_type: str = "unknown"
    abnormal_probabilities: dict[str, float] = field(default_factory=dict)
    evidence_source: str = "model_based_warning"
    label_confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ModuleRegistration:
    module_id: str
    version: str
    api_version: str
    capabilities: tuple[str, ...]
    service: Any
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class HealthCheckable(Protocol):
    def health_check(self) -> Mapping[str, Any]: ...


@runtime_checkable
class LifecycleService(Protocol):
    def start(self, *args: Any, **kwargs: Any) -> Any: ...

    def stop(self, *args: Any, **kwargs: Any) -> Any: ...

