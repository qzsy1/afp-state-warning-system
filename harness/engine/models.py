from __future__ import annotations

from dataclasses import dataclass


class GateConfigError(ValueError):
    """Raised when gate configuration cannot be trusted."""


PRE_SENSOR_STATUSES = frozenset(
    {"passed", "failed", "blocked", "physical_sensor_pending"}
)

PHYSICAL_SENSOR_CHECK_IDS = frozenset(
    {
        "physical.device_identity_ports",
        "physical.channel_integrity",
        "physical.units_scaling_endianness_direction",
        "physical.calibration_zero_noise_drift_crosstalk",
        "physical.all_devices_10hz",
        "physical.usb_power_bandwidth_emi",
        "physical.disconnect_power_restart",
        "physical.driver_close",
        "physical.hardware_endurance",
    }
)


@dataclass(frozen=True)
class PreSensorResult:
    """One sensor-independent readiness result with strict evidence semantics."""

    check_id: str
    title: str
    status: str
    evidence: str
    remediation: str
    external_environment: bool = False

    def __post_init__(self) -> None:
        if self.status not in PRE_SENSOR_STATUSES:
            raise ValueError(f"unsupported pre-sensor status: {self.status}")
        if not self.check_id.strip() or not self.title.strip():
            raise ValueError("pre-sensor check_id and title are required")
        if self.status == "physical_sensor_pending":
            if self.check_id not in PHYSICAL_SENSOR_CHECK_IDS:
                raise ValueError(
                    "physical_sensor_pending is restricted to the physical sensor whitelist"
                )
            if self.external_environment:
                raise ValueError(
                    "physical_sensor_pending cannot describe an external environment"
                )
        if self.check_id.startswith("environment.") and not self.external_environment:
            raise ValueError("environment checks must set external_environment")
        if self.status in {"failed", "blocked", "physical_sensor_pending"}:
            if not self.remediation.strip():
                raise ValueError(f"remediation is required for status {self.status}")


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
