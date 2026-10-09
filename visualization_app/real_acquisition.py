from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


class ChannelQuality(str, Enum):
    MEASURED_NEW = "measured_new"
    HELD_WITHIN_FRESHNESS = "held_within_freshness"
    INTERPOLATED = "interpolated"
    MISSING = "missing"
    STALE = "stale"
    INVALID = "invalid"


VALID_MODEL_QUALITIES = frozenset(
    {ChannelQuality.MEASURED_NEW, ChannelQuality.HELD_WITHIN_FRESHNESS}
)

READINESS_STAGE_NAMES = (
    "port_present",
    "endpoint_compatible",
    "driver_open",
    "protocol_identified",
    "channels_complete",
    "values_valid",
    "freshness_stable",
    "ready",
)
READINESS_STAGE_STATES = frozenset({"passed", "failed", "pending", "not_applicable"})


@dataclass(frozen=True)
class ReadinessStage:
    """One independently auditable layer of a hardware readiness check."""

    name: str
    state: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0
    remediation: str = ""

    def __post_init__(self) -> None:
        if self.name not in READINESS_STAGE_NAMES:
            raise ValueError(f"unknown readiness stage: {self.name}")
        if self.state not in READINESS_STAGE_STATES:
            raise ValueError(f"unknown readiness stage state: {self.state}")
        object.__setattr__(self, "evidence", dict(self.evidence or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "evidence": dict(self.evidence),
            "started_at": float(self.started_at),
            "finished_at": float(self.finished_at),
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class ReadinessReport:
    """Layered evidence used by both the check action and the start gate."""

    interface_id: str
    state: str
    stages: tuple[ReadinessStage, ...]
    expected_channels: tuple[str, ...] = ()
    detected_channels: tuple[str, ...] = ()
    missing_channels: tuple[str, ...] = ()
    invalid_channels: tuple[str, ...] = ()
    stale_channels: tuple[str, ...] = ()
    message: str = ""
    config_fingerprint: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "ready" and all(
            stage.state in {"passed", "not_applicable"} for stage in self.stages
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "interface_id": self.interface_id,
            "state": self.state,
            "ready": self.ready,
            "stages": [stage.to_dict() for stage in self.stages],
            "expected_channels": list(self.expected_channels),
            "detected_channels": list(self.detected_channels),
            "missing_channels": list(self.missing_channels),
            "invalid_channels": list(self.invalid_channels),
            "stale_channels": list(self.stale_channels),
            "message": self.message,
            "config_fingerprint": self.config_fingerprint,
        }


_READINESS_SECRET_MARKERS = ("password", "token", "secret", "credential")


def _sanitize_readiness_evidence(value: Any) -> Any:
    """Copy readiness evidence while removing credential-bearing fields."""

    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_readiness_evidence(item)
            for key, item in value.items()
            if not any(marker in str(key).lower() for marker in _READINESS_SECRET_MARKERS)
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_readiness_evidence(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class ReadinessSnapshot:
    """Portable, expiring evidence produced by the Helper check process."""

    config_fingerprint: str
    checked_at: float
    expires_at: float
    ok: bool
    interfaces: tuple[Mapping[str, Any], ...] = ()
    sensors: tuple[Mapping[str, Any], ...] = ()
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    device_identity: tuple[str, ...] = ()
    schema_version: int = 1

    @classmethod
    def create(
        cls,
        config: Any,
        result: Mapping[str, Any],
        *,
        capabilities: Mapping[str, Any] | None = None,
        now: float | None = None,
        ttl_seconds: float = 120.0,
    ) -> "ReadinessSnapshot":
        checked_at = float(time.time() if now is None else now)
        lifetime = max(1.0, float(ttl_seconds))
        safe = _sanitize_readiness_evidence(dict(result or {}))
        interfaces = tuple(
            dict(item) for item in (safe.get("interfaces") or []) if isinstance(item, Mapping)
        )
        sensors = tuple(
            dict(item) for item in (safe.get("sensors") or []) if isinstance(item, Mapping)
        )
        identities = tuple(
            sorted(
                {
                    str(item.get("physical_interface_id") or item.get("id") or "").strip()
                    for item in interfaces
                    if str(item.get("physical_interface_id") or item.get("id") or "").strip()
                }
            )
        )
        return cls(
            config_fingerprint=readiness_config_fingerprint(config),
            checked_at=checked_at,
            expires_at=checked_at + lifetime,
            ok=bool(safe.get("ok")),
            interfaces=interfaces,
            sensors=sensors,
            capabilities=dict(_sanitize_readiness_evidence(capabilities or {})),
            device_identity=identities,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReadinessSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("readiness snapshot must be an object")
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            config_fingerprint=str(payload.get("config_fingerprint") or ""),
            checked_at=float(payload.get("checked_at") or 0.0),
            expires_at=float(payload.get("expires_at") or 0.0),
            ok=bool(payload.get("ok")),
            interfaces=tuple(
                dict(item) for item in (payload.get("interfaces") or []) if isinstance(item, Mapping)
            ),
            sensors=tuple(
                dict(item) for item in (payload.get("sensors") or []) if isinstance(item, Mapping)
            ),
            capabilities=dict(payload.get("capabilities") or {}),
            device_identity=tuple(str(item) for item in (payload.get("device_identity") or [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "config_fingerprint": self.config_fingerprint,
            "checked_at": self.checked_at,
            "expires_at": self.expires_at,
            "ok": self.ok,
            "interfaces": [dict(item) for item in self.interfaces],
            "sensors": [dict(item) for item in self.sensors],
            "capabilities": dict(self.capabilities),
            "device_identity": list(self.device_identity),
        }

    def validate(
        self,
        config: Any,
        *,
        now: float | None = None,
        required_capabilities: Iterable[str] = (),
    ) -> None:
        current = float(time.time() if now is None else now)
        if self.schema_version != 1:
            raise ValueError(f"unsupported readiness snapshot schema: {self.schema_version}")
        if not self.ok:
            raise ValueError("readiness snapshot is not ready")
        if current > self.expires_at:
            raise ValueError("readiness snapshot expired")
        if self.config_fingerprint != readiness_config_fingerprint(config):
            raise ValueError("readiness snapshot fingerprint mismatch")
        missing = [name for name in required_capabilities if not self.capabilities.get(name)]
        if missing:
            raise ValueError("readiness snapshot capability missing: " + ", ".join(missing))
        source = vars(config) if hasattr(config, "__dict__") else dict(config or {})
        expected_capabilities = dict(source.get("helper_capabilities") or {})
        for name in ("unified_frame_contract", "protocol_version", "build_id"):
            expected = str(expected_capabilities.get(name) or "").strip()
            if expected and str(self.capabilities.get(name) or "").strip() != expected:
                raise ValueError(f"readiness snapshot capability revision mismatch: {name}")
        expected_identities = {
            str(item.get("physical_interface_id") or item.get("id") or "").strip()
            for item in (source.get("interfaces") or [])
            if isinstance(item, Mapping) and item.get("enabled", True)
        }
        expected_identities.discard("")
        ready_states = {"ok", "ready", "passed"}
        observed_identities = {
            str(item.get("physical_interface_id") or item.get("id") or "").strip()
            for item in self.interfaces
            if (
                str(item.get("state") or "").strip().lower() in ready_states
                or item.get("ok") is True
            )
        }
        missing_identities = sorted(expected_identities - observed_identities)
        if missing_identities:
            raise ValueError(
                "readiness snapshot device identity missing: " + ", ".join(missing_identities)
            )
        ready_channels = {
            str(item.get("name") or item.get("channel_name") or "").strip()
            for item in self.sensors
            if str(item.get("state") or "").strip().lower() in ready_states
            or item.get("ok") is True
        }
        expected_channels = {
            str(name)
            for name in (source.get("selected_sensors") or [])
            if str(name).strip()
        }
        missing_channels = sorted(expected_channels - ready_channels)
        if missing_channels:
            raise ValueError(
                "readiness snapshot channel evidence missing: " + ", ".join(missing_channels)
            )


def readiness_config_fingerprint(config: Any) -> str:
    """Hash only readiness-relevant, non-secret configuration fields."""

    if hasattr(config, "__dict__"):
        source = vars(config)
    elif isinstance(config, Mapping):
        source = config
    else:
        source = {}
    interfaces = []
    for item in source.get("interfaces", []) or []:
        if not isinstance(item, Mapping):
            continue
        interfaces.append({
            key: item.get(key)
            for key in (
                "id", "enabled", "role", "driver", "endpoint", "baudrate",
                "physical_interface_id", "physical_port_id", "physical_verified",
                "physical_vid", "physical_pid", "physical_serial", "physical_location",
                "device_profile_id", "identity_confirmed",
                "source_address", "device_subnet", "network_interface_name",
                "profile_id", "profile_version", "protocol_verified",
                "channel_types", "channel_types_confirmed", "roi", "dll_path",
            )
        })
    payload = {
        "config_version": source.get("config_version"),
        "acquisition_mode": source.get("acquisition_mode"),
        "real_acquisition_mode": source.get("real_acquisition_mode"),
        "execution_host": source.get("execution_host"),
        "execution_device_id": source.get("execution_device_id"),
        "capture_policy": source.get("capture_policy"),
        "dataset_schema": source.get("dataset_schema"),
        "sample_rate_hz": source.get("sample_rate_hz"),
        "selected_sensors": source.get("selected_sensors") or [],
        "interface_channel_assignments": source.get("interface_channel_assignments") or {},
        "interfaces": interfaces,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ipv4_addresses(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result = []
    for item in values:
        text = str(item or "").strip().split("%", 1)[0]
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if address.version == 4:
            result.append(str(address))
    return result


def discover_connection_source_address(target_host: str, target_port: int) -> str:
    """Ask the OS routing table which source address would reach a target."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((str(target_host), int(target_port)))
        return str(probe.getsockname()[0])


def discover_interface_id_for_source(source_address: str) -> str:
    """Map the OS-selected source address back to the Windows adapter identity."""

    try:
        import psutil
    except ImportError:
        return ""
    for name, addresses in psutil.net_if_addrs().items():
        if any(
            getattr(item, "family", None) == socket.AF_INET
            and str(getattr(item, "address", "")) == source_address
            for item in addresses
        ):
            return f"ethernet:{name}"
    return ""


def validate_industrial_network_path(
    target_host: str,
    selected_interface: Mapping[str, Any],
    *,
    target_port: int = 0,
    actual_source_address: str | None = None,
    route_interface_id: str | None = None,
) -> dict[str, Any]:
    """Validate that PLC/ABB traffic uses the selected physical direct route."""

    selected = dict(selected_interface or {})
    selected_id = str(
        selected.get("physical_interface_id") or selected.get("id") or ""
    ).strip()
    selected_name = " ".join(
        str(selected.get(key) or "")
        for key in ("network_interface_name", "name", "description", "label", "kind")
    ).strip()
    lowered_name = selected_name.casefold()
    virtual_markers = (
        "flclash", "clash", "tun", "tap", "vpn", "loopback", "wintun",
        "wireguard", "openvpn", "hyper-v", "virtualbox", "vmware",
    )
    selected_addresses = _ipv4_addresses(
        selected.get("source_address")
        or selected.get("addresses")
        or selected.get("ipv4_addresses")
    )
    source = str(actual_source_address or "").strip()
    if not source and target_port:
        try:
            source = discover_connection_source_address(target_host, target_port)
        except OSError:
            source = ""
    errors: list[str] = []
    try:
        target = ipaddress.ip_address(str(target_host).split("%", 1)[0])
    except ValueError:
        target = None
        errors.append("目标设备地址不是可验证的IPv4地址")
    try:
        source_ip = ipaddress.ip_address(source.split("%", 1)[0]) if source else None
    except ValueError:
        source_ip = None
        errors.append("实际源地址无效")
    if not selected_id:
        errors.append("未选择物理工业网卡")
    if any(marker in lowered_name for marker in virtual_markers):
        errors.append("所选接口是代理、VPN、隧道、回环或虚拟网卡")
    if source_ip is None:
        errors.append("无法确定到目标设备的实际源地址")
    elif source_ip.is_loopback or source_ip.is_link_local or source_ip in ipaddress.ip_network("198.18.0.0/15"):
        errors.append("实际源地址属于回环、APIPA或代理测试网段")
    if source and selected_addresses and source not in selected_addresses:
        errors.append("Windows最佳路由的实际源地址不属于所选工业网卡")
    configured_subnet = str(selected.get("device_subnet") or "").strip()
    network = None
    if configured_subnet:
        try:
            network = ipaddress.ip_network(configured_subnet, strict=False)
        except ValueError:
            errors.append("设备网段配置无效")
    elif target is not None and target.version == 4:
        network = ipaddress.ip_network(f"{target}/24", strict=False)
    if network is not None and source_ip is not None and source_ip not in network:
        errors.append("实际源地址与设备不在配置的直连网段")
    observed_route_interface_id = str(route_interface_id or "").strip()
    if not observed_route_interface_id and source:
        observed_route_interface_id = discover_interface_id_for_source(source)
    if observed_route_interface_id and selected_id and observed_route_interface_id != selected_id:
        errors.append("Windows最佳路由接口与操作员选择的工业网卡不一致")
    ok = not errors
    return {
        "ok": ok,
        "state": "ready" if ok else "network_path_invalid",
        "target_address": str(target_host),
        "target_port": int(target_port or 0),
        "selected_interface_id": selected_id,
        "selected_interface_name": selected_name,
        "selected_addresses": selected_addresses,
        "actual_source_address": source,
        "route_interface_id": observed_route_interface_id or selected_id,
        "route_type": "direct_physical" if ok else "invalid_or_unverified",
        "errors": errors,
        "remediation": (
            "为物理工业网卡配置设备网段静态IPv4地址，添加直连路由，并排除FlClash/TUN/VPN/回环/APIPA"
            if errors else ""
        ),
    }


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class ChannelSample:
    interface_id: str
    channel_name: str
    value: float | None
    quality: ChannelQuality = ChannelQuality.MEASURED_NEW
    device_timestamp: float | None = None
    received_wall_time: float = field(default_factory=time.time)
    received_monotonic: float = field(default_factory=time.monotonic)
    source_sequence: int | None = None
    protocol_ok: bool | None = None
    error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        quality = (
            self.quality
            if isinstance(self.quality, ChannelQuality)
            else ChannelQuality(str(self.quality))
        )
        value = _finite(self.value)
        if value is None and quality not in {
            ChannelQuality.MISSING,
            ChannelQuality.STALE,
        }:
            quality = ChannelQuality.INVALID
        object.__setattr__(self, "interface_id", str(self.interface_id))
        object.__setattr__(self, "channel_name", str(self.channel_name))
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "device_timestamp", _finite(self.device_timestamp))
        object.__setattr__(self, "received_wall_time", float(self.received_wall_time))
        object.__setattr__(self, "received_monotonic", float(self.received_monotonic))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "interface_id": self.interface_id,
            "channel_name": self.channel_name,
            "value": self.value,
            "quality": self.quality.value,
            "device_timestamp": self.device_timestamp,
            "received_wall_time": self.received_wall_time,
            "received_monotonic": self.received_monotonic,
            "source_sequence": self.source_sequence,
            "protocol_ok": self.protocol_ok,
            "error": self.error,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChannelSample":
        return cls(
            interface_id=str(payload.get("interface_id") or ""),
            channel_name=str(payload.get("channel_name") or ""),
            value=payload.get("value"),
            quality=ChannelQuality(str(payload.get("quality") or "invalid")),
            device_timestamp=payload.get("device_timestamp"),
            received_wall_time=float(payload.get("received_wall_time") or 0.0),
            received_monotonic=float(payload.get("received_monotonic") or 0.0),
            source_sequence=(
                int(payload["source_sequence"])
                if payload.get("source_sequence") is not None
                else None
            ),
            protocol_ok=payload.get("protocol_ok"),
            error=str(payload.get("error") or ""),
            metadata=(
                payload.get("metadata")
                if isinstance(payload.get("metadata"), Mapping)
                else {}
            ),
        )


class ChannelSampleCache:
    """Thread-safe bounded event cache with an independent latest value per channel."""

    def __init__(self, max_events: int = 512) -> None:
        self.max_events = max(8, int(max_events))
        self._lock = threading.RLock()
        self._events: deque[ChannelSample] = deque(maxlen=self.max_events)
        self._latest: dict[str, ChannelSample] = {}
        self._updates: Counter[str] = Counter()
        self._dropped_events = 0

    def publish(self, sample: ChannelSample) -> None:
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._dropped_events += 1
            self._events.append(sample)
            self._latest[sample.channel_name] = sample
            self._updates[sample.channel_name] += 1

    def latest(self, channel_name: str) -> ChannelSample | None:
        with self._lock:
            return self._latest.get(channel_name)

    def latest_at_or_before(
        self, channel_name: str, deadline_monotonic: float
    ) -> ChannelSample | None:
        with self._lock:
            latest = self._latest.get(channel_name)
            if latest is not None and latest.received_monotonic <= deadline_monotonic:
                return latest
            for sample in reversed(self._events):
                if (
                    sample.channel_name == channel_name
                    and sample.received_monotonic <= deadline_monotonic
                ):
                    return sample
        return None

    def snapshot(self) -> dict[str, ChannelSample]:
        with self._lock:
            return dict(self._latest)

    def events(self) -> tuple[ChannelSample, ...]:
        """Return the bounded diagnostic window without exposing mutable storage."""
        with self._lock:
            return tuple(self._events)

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "buffered_events": len(self._events),
                "buffer_capacity": self.max_events,
                "diagnostic_events_dropped": self._dropped_events,
                "channel_updates": dict(self._updates),
            }


class DriverWorker:
    """Run one possibly slow driver without blocking any other interface."""

    def __init__(
        self,
        interface_id: str,
        driver: Any,
        channels: Iterable[str],
        cache: ChannelSampleCache,
        *,
        poll_interval_seconds: float = 0.002,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.interface_id = str(interface_id)
        self.driver = driver
        self.channels = frozenset(str(item) for item in channels)
        self.cache = cache
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self.clock = clock
        self.wall_clock = wall_clock
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.opened = threading.Event()
        self.source_sequence = 0
        self.read_calls = 0
        self.last_error = ""
        self.error_details: dict[str, Any] = {}
        self.last_sample_monotonic: float | None = None

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name=f"AFP-Driver-{self.interface_id}",
            daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        try:
            self.driver.open()
            self.opened.set()
            while not self.stop_event.is_set():
                self.read_calls += 1
                payload = self.driver.read_sample()
                received_monotonic = self.clock()
                received_wall_time = self.wall_clock()
                if payload:
                    self.source_sequence += 1
                    driver_metadata = getattr(self.driver, "quality_metadata", {})
                    published = False
                    for name, raw_value in payload.items():
                        if name not in self.channels:
                            continue
                        value = _finite(raw_value)
                        sample = ChannelSample(
                            interface_id=self.interface_id,
                            channel_name=name,
                            value=value,
                            quality=(
                                ChannelQuality.MEASURED_NEW
                                if value is not None
                                else ChannelQuality.INVALID
                            ),
                            received_wall_time=received_wall_time,
                            received_monotonic=received_monotonic,
                            source_sequence=self.source_sequence,
                            protocol_ok=getattr(self.driver, "protocol_verified", None),
                            metadata=(
                                driver_metadata
                                if isinstance(driver_metadata, Mapping)
                                else {}
                            ),
                        )
                        self.cache.publish(sample)
                        published = True
                    if published:
                        self.last_sample_monotonic = received_monotonic
                if self.poll_interval_seconds:
                    self.stop_event.wait(self.poll_interval_seconds)
        except Exception as exc:
            self.last_error = str(exc)
            detail_fields = {
                "vendor_stage": getattr(exc, "stage", None),
                "vendor_return_code": getattr(exc, "return_code", None),
                "vendor_last_error_code": getattr(exc, "last_error_code", None),
            }
            self.error_details = {
                key: value for key, value in detail_fields.items() if value is not None
            }
        finally:
            self.opened.set()
            try:
                self.driver.close()
            except Exception as exc:
                if not self.last_error:
                    self.last_error = str(exc)

    def stop(self, timeout_seconds: float = 0.5) -> bool:
        self.stop_event.set()
        thread = self.thread
        if thread is not None:
            thread.join(max(0.0, float(timeout_seconds)))
        return thread is None or not thread.is_alive()

    def status(self) -> dict[str, Any]:
        thread = self.thread
        return {
            "interface_id": self.interface_id,
            "running": bool(thread is not None and thread.is_alive()),
            "opened": self.opened.is_set() and not self.last_error,
            "read_calls": self.read_calls,
            "source_sequence": self.source_sequence,
            "last_sample_monotonic": self.last_sample_monotonic,
            "last_error": self.last_error,
            "error_details": dict(self.error_details),
        }


@dataclass(frozen=True)
class UnifiedFrame:
    capture_sequence: int
    target_monotonic: float
    target_wall_time: float
    channels: Mapping[str, ChannelSample]
    deadline_missed: bool = False
    assembled_monotonic: float = 0.0

    def values(self) -> dict[str, float | None]:
        return {name: sample.value for name, sample in self.channels.items()}

    def quality(self) -> dict[str, str]:
        return {name: sample.quality.value for name, sample in self.channels.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_sequence": self.capture_sequence,
            "target_monotonic": self.target_monotonic,
            "target_wall_time": self.target_wall_time,
            "deadline_missed": self.deadline_missed,
            "assembled_monotonic": self.assembled_monotonic,
            "channels": {
                name: sample.to_dict() for name, sample in self.channels.items()
            },
        }

    def to_envelope(
        self,
        capture_uuid: str,
        channel_units: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return the source-agnostic frame contract used by every mode."""

        identity = str(capture_uuid or "").strip()
        if not identity:
            raise ValueError("capture_uuid is required")
        units = {str(key): str(value) for key, value in (channel_units or {}).items()}
        channels: dict[str, dict[str, Any]] = {}
        for name, sample in self.channels.items():
            age = _finite(sample.metadata.get("age_seconds"))
            channels[str(name)] = {
                "value": sample.value,
                "unit": units.get(str(name), ""),
                "quality": sample.quality.value,
                "source_timestamp": (
                    sample.device_timestamp
                    if sample.device_timestamp is not None
                    else sample.received_wall_time
                ),
                "received_timestamp": sample.received_wall_time,
                "age_seconds": age,
                "is_new": sample.quality == ChannelQuality.MEASURED_NEW,
                "interface_id": sample.interface_id,
                "source_sequence": sample.source_sequence,
                "protocol_ok": sample.protocol_ok,
                "error": sample.error,
            }
        return {
            "contract_version": "unified_frame_v1",
            "capture_uuid": identity,
            "frame_sequence": int(self.capture_sequence),
            "frame_time": float(self.target_wall_time),
            "target_monotonic": float(self.target_monotonic),
            "assembled_monotonic": float(self.assembled_monotonic),
            "deadline_missed": bool(self.deadline_missed),
            "channels": channels,
        }

    @classmethod
    def from_envelope(cls, payload: Mapping[str, Any]) -> "UnifiedFrame":
        """Validate and restore a frame without recalculating remote quality."""

        if str(payload.get("contract_version") or "") != "unified_frame_v1":
            raise ValueError("unsupported unified frame contract")
        capture_uuid = str(payload.get("capture_uuid") or "").strip()
        if not capture_uuid:
            raise ValueError("capture_uuid is required")
        sequence = int(payload.get("frame_sequence", -1))
        if sequence < 0:
            raise ValueError("frame_sequence must be non-negative")
        target_monotonic = float(payload.get("target_monotonic") or 0.0)
        raw_channels = payload.get("channels")
        if not isinstance(raw_channels, Mapping):
            raise ValueError("channels must be an object")
        channels: dict[str, ChannelSample] = {}
        for raw_name, raw_sample in raw_channels.items():
            if not isinstance(raw_sample, Mapping):
                raise ValueError(f"channel {raw_name} must be an object")
            name = str(raw_name)
            quality = ChannelQuality(str(raw_sample.get("quality") or "invalid"))
            age = _finite(raw_sample.get("age_seconds"))
            received_wall = float(
                raw_sample.get("received_timestamp")
                if raw_sample.get("received_timestamp") is not None
                else raw_sample.get("source_timestamp") or 0.0
            )
            channels[name] = ChannelSample(
                interface_id=str(raw_sample.get("interface_id") or ""),
                channel_name=name,
                value=raw_sample.get("value"),
                quality=quality,
                device_timestamp=raw_sample.get("source_timestamp"),
                received_wall_time=received_wall,
                received_monotonic=(
                    target_monotonic - age if age is not None else target_monotonic
                ),
                source_sequence=(
                    int(raw_sample["source_sequence"])
                    if raw_sample.get("source_sequence") is not None
                    else None
                ),
                protocol_ok=raw_sample.get("protocol_ok"),
                error=str(raw_sample.get("error") or ""),
                metadata={"age_seconds": age, "unit": str(raw_sample.get("unit") or "")},
            )
        return cls(
            capture_sequence=sequence,
            target_monotonic=target_monotonic,
            target_wall_time=float(payload.get("frame_time") or 0.0),
            channels=channels,
            deadline_missed=bool(payload.get("deadline_missed", False)),
            assembled_monotonic=float(payload.get("assembled_monotonic") or 0.0),
        )


class FrameAssembler:
    """Create unified frames from independent channel events on fixed deadlines."""

    def __init__(
        self,
        channels: Iterable[str],
        frame_rate_hz: float,
        freshness_by_channel: Mapping[str, float] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        wait: Callable[[float], Any] = time.sleep,
        start_monotonic: float | None = None,
        start_wall_time: float | None = None,
    ) -> None:
        self.channels = tuple(dict.fromkeys(str(item) for item in channels))
        self.frame_rate_hz = max(0.1, float(frame_rate_hz))
        self.period_seconds = 1.0 / self.frame_rate_hz
        self.freshness_by_channel = {
            str(key): max(0.0, float(value))
            for key, value in (freshness_by_channel or {}).items()
        }
        self.clock = clock
        self.wall_clock = wall_clock
        self.wait = wait
        self.start_monotonic = (
            float(start_monotonic) if start_monotonic is not None else self.clock()
        )
        self.start_wall_time = (
            float(start_wall_time) if start_wall_time is not None else self.wall_clock()
        )
        self.next_sequence = 0
        self.last_source_sequence: dict[str, int | None] = {}
        self.deadline_misses = 0

    def deadline(self, sequence: int) -> float:
        return self.start_monotonic + int(sequence) / self.frame_rate_hz

    def wait_next_deadline(self, stop_event: threading.Event | None = None) -> float:
        deadline = self.deadline(self.next_sequence)
        delay = deadline - self.clock()
        if delay > 0:
            if stop_event is not None:
                stop_event.wait(delay)
            else:
                self.wait(delay)
        return deadline

    def assemble(
        self,
        cache: ChannelSampleCache,
        deadline_monotonic: float | None = None,
    ) -> UnifiedFrame:
        sequence = self.next_sequence
        deadline = (
            float(deadline_monotonic)
            if deadline_monotonic is not None
            else self.deadline(sequence)
        )
        assembled_at = self.clock()
        missed = assembled_at - deadline > self.period_seconds
        if missed:
            self.deadline_misses += 1
        channel_values: dict[str, ChannelSample] = {}
        for channel in self.channels:
            source = cache.latest_at_or_before(channel, deadline)
            if source is None:
                channel_values[channel] = ChannelSample(
                    interface_id="",
                    channel_name=channel,
                    value=None,
                    quality=ChannelQuality.MISSING,
                    received_wall_time=self.start_wall_time + (deadline - self.start_monotonic),
                    received_monotonic=deadline,
                    error="no_source_sample",
                )
                continue
            age = max(0.0, deadline - source.received_monotonic)
            freshness = self.freshness_by_channel.get(channel, self.period_seconds * 2.5)
            previous_sequence = self.last_source_sequence.get(channel)
            is_new = source.source_sequence != previous_sequence
            if source.value is None or source.quality == ChannelQuality.INVALID:
                quality = ChannelQuality.INVALID
                value = None
            elif age > freshness:
                quality = ChannelQuality.STALE
                value = source.value
            elif is_new:
                quality = ChannelQuality.MEASURED_NEW
                value = source.value
            else:
                quality = ChannelQuality.HELD_WITHIN_FRESHNESS
                value = source.value
            channel_values[channel] = ChannelSample(
                interface_id=source.interface_id,
                channel_name=channel,
                value=value,
                quality=quality,
                device_timestamp=source.device_timestamp,
                received_wall_time=source.received_wall_time,
                received_monotonic=source.received_monotonic,
                source_sequence=source.source_sequence,
                protocol_ok=source.protocol_ok,
                error=source.error,
                metadata={**dict(source.metadata), "age_seconds": age},
            )
            self.last_source_sequence[channel] = source.source_sequence
        self.next_sequence += 1
        return UnifiedFrame(
            capture_sequence=sequence,
            target_monotonic=deadline,
            target_wall_time=self.start_wall_time + (deadline - self.start_monotonic),
            channels=channel_values,
            deadline_missed=missed,
            assembled_monotonic=assembled_at,
        )


class AcquisitionQualityMetrics:
    def __init__(self, channels: Iterable[str], target_rate_hz: float) -> None:
        self.channels = tuple(channels)
        self.target_rate_hz = float(target_rate_hz)
        self.frame_count = 0
        self.deadline_misses = 0
        self.complete_frames = 0
        self.first_target: float | None = None
        self.last_target: float | None = None
        self.quality_counts = {
            channel: Counter() for channel in self.channels
        }
        self.update_times: dict[str, list[float]] = {
            channel: [] for channel in self.channels
        }
        self.latest_age_seconds: dict[str, float | None] = {
            channel: None for channel in self.channels
        }

    def observe(self, frame: UnifiedFrame) -> None:
        self.frame_count += 1
        self.deadline_misses += int(frame.deadline_missed)
        if all(sample.quality in VALID_MODEL_QUALITIES for sample in frame.channels.values()):
            self.complete_frames += 1
        self.first_target = frame.target_monotonic if self.first_target is None else self.first_target
        self.last_target = frame.target_monotonic
        for name, sample in frame.channels.items():
            self.quality_counts.setdefault(name, Counter())[sample.quality.value] += 1
            if sample.quality == ChannelQuality.MEASURED_NEW:
                self.update_times.setdefault(name, []).append(sample.received_monotonic)
            age = sample.metadata.get("age_seconds")
            self.latest_age_seconds[name] = _finite(age)

    def summary(self) -> dict[str, Any]:
        duration = (
            self.last_target - self.first_target
            if self.first_target is not None
            and self.last_target is not None
            and self.last_target > self.first_target
            else 0.0
        )
        actual_rate = (self.frame_count - 1) / duration if duration > 0 else 0.0
        channels: dict[str, Any] = {}
        for name in self.channels:
            counts = self.quality_counts.get(name, Counter())
            updates = self.update_times.get(name, [])
            update_duration = updates[-1] - updates[0] if len(updates) > 1 else 0.0
            intervals = [right - left for left, right in zip(updates, updates[1:])]
            denominator = max(1, self.frame_count)
            channels[name] = {
                "update_rate_hz": (
                    (len(updates) - 1) / update_duration if update_duration > 0 else 0.0
                ),
                "latest_age_seconds": self.latest_age_seconds.get(name),
                "missing_rate": counts[ChannelQuality.MISSING.value] / denominator,
                "stale_rate": counts[ChannelQuality.STALE.value] / denominator,
                "held_rate": counts[ChannelQuality.HELD_WITHIN_FRESHNESS.value] / denominator,
                "interpolated_rate": counts[ChannelQuality.INTERPOLATED.value] / denominator,
                "maximum_update_gap_seconds": max(intervals) if intervals else 0.0,
                "quality_counts": dict(counts),
            }
        return {
            "target_frame_rate_hz": self.target_rate_hz,
            "actual_frame_rate_hz": actual_rate,
            "frame_count": self.frame_count,
            "deadline_misses": self.deadline_misses,
            "complete_frame_rate": (
                self.complete_frames / self.frame_count if self.frame_count else 0.0
            ),
            "channels": channels,
        }


def recent_complete_window(
    rows: Iterable[Mapping[str, Any]],
    required_channels: Iterable[str],
    seq_len: int,
    *,
    quality_rows: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    required = tuple(str(item) for item in required_channels)
    needed = max(1, int(seq_len))
    row_list = list(rows)
    quality_list = list(quality_rows or [])
    continuous = 0
    invalid_channels: set[str] = set()
    for reverse_index, row in enumerate(reversed(row_list)):
        quality = (
            quality_list[len(row_list) - reverse_index - 1]
            if len(quality_list) == len(row_list)
            else {}
        )
        row_invalid = []
        for name in required:
            if _finite(row.get(name)) is None:
                row_invalid.append(name)
                continue
            raw_quality = quality.get(name)
            if raw_quality is not None:
                try:
                    parsed_quality = ChannelQuality(str(raw_quality))
                except ValueError:
                    parsed_quality = ChannelQuality.INVALID
                if parsed_quality not in VALID_MODEL_QUALITIES:
                    row_invalid.append(name)
        if row_invalid:
            invalid_channels.update(row_invalid)
            break
        continuous += 1
        if continuous >= needed:
            break
    return {
        "ready": bool(required) and continuous >= needed,
        "continuous_points": continuous,
        "required_points": needed,
        "remaining_points": max(0, needed - continuous),
        "invalid_channels": sorted(invalid_channels),
    }


class VirtualClock:
    """Deterministic clock for deadline and endurance contract tests."""

    def __init__(self, start: float = 0.0, wall_start: float = 1_700_000_000.0) -> None:
        self.current = float(start)
        self.wall_start = float(wall_start)

    def monotonic(self) -> float:
        return self.current

    def time(self) -> float:
        return self.wall_start + self.current

    def wait(self, seconds: float) -> None:
        self.current += max(0.0, float(seconds))

    def advance_processing(self, seconds: float) -> None:
        self.current += max(0.0, float(seconds))


def capture_m3232_raw_serial(
    endpoint: str,
    output_path: str | Path,
    *,
    duration_seconds: float = 10.0,
    baudrate: int = 115200,
    serial_instance: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    wait: Callable[[float], Any] = time.sleep,
    max_bytes: int = 16 * 1024 * 1024,
) -> dict[str, Any]:
    """Capture M3232 bytes without transmitting an unverified command.

    The artifact stores base64 chunks, timestamps and a digest.  It intentionally
    accepts COM endpoints only and never serializes credentials or arbitrary
    connection strings.
    """

    endpoint_text = str(endpoint or "").strip().upper()
    if not re.fullmatch(r"(?:\\\\\.\\)?COM\d{1,3}", endpoint_text):
        raise ValueError("M3232原始抓帧仅允许明确的Windows COM端点")
    target = Path(output_path).expanduser().resolve()
    if not target.parent.is_dir():
        raise ValueError("抓帧输出目录不存在")
    owned_serial = serial_instance is None
    port = serial_instance
    if port is None:
        import serial

        port = serial.Serial(
            endpoint_text,
            int(baudrate),
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.1,
        )
    started_monotonic = clock()
    started_wall = wall_clock()
    chunks: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    total_bytes = 0
    try:
        while clock() - started_monotonic < max(0.0, float(duration_seconds)):
            pending = int(getattr(port, "in_waiting", 0) or 0)
            if pending <= 0:
                wait(min(0.01, max(0.0, float(duration_seconds))))
                continue
            raw = bytes(port.read(min(pending, 65536, max_bytes - total_bytes)))
            if not raw:
                wait(0.001)
                continue
            captured_at = clock()
            digest.update(raw)
            total_bytes += len(raw)
            chunks.append(
                {
                    "offset_seconds": captured_at - started_monotonic,
                    "wall_time": wall_clock(),
                    "length": len(raw),
                    "data_base64": base64.b64encode(raw).decode("ascii"),
                }
            )
            if total_bytes >= max_bytes:
                break
    finally:
        if owned_serial:
            port.close()
    artifact = {
        "schema_version": 1,
        "capture_kind": "m3232_read_only_raw_serial",
        "hardware_protocol_status": "hardware_protocol_unverified",
        "endpoint": endpoint_text,
        "baudrate": int(baudrate),
        "started_wall_time": started_wall,
        "duration_seconds": max(0.0, clock() - started_monotonic),
        "total_bytes": total_bytes,
        "sha256": digest.hexdigest(),
        "commands_sent": [],
        "chunks": chunks,
    }
    temporary = target.with_name(f".{target.name}.partial")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(artifact, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return {
        "path": str(target),
        "endpoint": endpoint_text,
        "total_bytes": total_bytes,
        "sha256": artifact["sha256"],
        "hardware_protocol_status": artifact["hardware_protocol_status"],
    }
