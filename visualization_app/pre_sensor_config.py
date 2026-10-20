"""Fail-closed validation for the two field acquisition configuration templates.

The templates contain reference names only.  Machine paths, network endpoints,
pairing material and database profiles are supplied by the deployment
environment and are never persisted in the repository templates.
"""

from __future__ import annotations

import re
import json
import os
import threading
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar


# ``acquisition.py`` remains importable both as a package module in tests and
# as a top-level legacy module in the assembled field application.
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from acquisition import (  # noqa: E402
    ACQUISITION_SCHEMAS,
    AcquisitionConfig,
    SENSOR_CHANNEL_METADATA,
    SENSOR_INTERFACE_PROFILES,
    STRAIN_SENSOR_COLUMNS,
)
from esr1001_usb import RAW_CHANNELS
from release_contract import (  # noqa: E402
    ReleaseContractError,
    assert_helper_contract,
    load_release_contract,
)
from thermocouple64 import hardware_release_readiness  # noqa: E402


class PreSensorConfigError(ValueError):
    """Raised before any device or remote start side effect is allowed."""


_ROLE_MAP = {
    "thermocouple64": "thermocouple64",
    "plc_process": "plc",
    "uvc_temperature": "thermal_uvc",
    "abb_motion": "robot",
    "m3232_pressure": "pressure",
    "esr1001_strain": "esr1001_strain",
}
_REFERENCE_NAME = re.compile(r"^AFP_[A-Z0-9_]+$")
_SECRET_KEY = re.compile(r"password|token|secret|credential|api[_-]?key", re.I)
_T = TypeVar("_T")


class InterfacePlanStore:
    """本机管理员共享非敏感方案；不提供采集就绪授权。"""

    _lock = threading.RLock()
    fields = frozenset((
        "id", "enabled", "role", "driver", "endpoint", "baudrate", "slave_id",
        "data_mode", "calibration_profile", "bridge", "channel_count", "channel_map", "channels",
        "physical_interface_id", "physical_port_id", "physical_interface_kind",
        "physical_transport_family", "selection_origin",
    ))

    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def sanitize(cls, payload):
        if not isinstance(payload, dict) or payload.get("active") not in ACQUISITION_SCHEMAS:
            raise ValueError("接口方案无效")
        plans = payload.get("plans")
        if not isinstance(plans, dict) or len(plans) > len(ACQUISITION_SCHEMAS):
            raise ValueError("接口方案目录无效")
        def clean(value):
            if isinstance(value, dict):
                return {str(k): clean(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value
        result = {}
        for schema, plan in plans.items():
            if schema not in ACQUISITION_SCHEMAS or not isinstance(plan, dict):
                raise ValueError("未知接口方案")
            interfaces = plan.get("interfaces")
            if not isinstance(interfaces, list) or not 1 <= len(interfaces) <= 32:
                raise ValueError("接口数量无效")
            if not all(isinstance(item, dict) for item in interfaces):
                raise ValueError("接口配置无效")
            result[schema] = {
                "interfaces": [clean({k: v for k, v in item.items() if k in cls.fields}) for item in interfaces],
                "processingMode": "capture_only" if plan.get("processingMode") == "capture_only" else "prediction_warning",
                "mysqlEnabled": bool(plan.get("mysqlEnabled")),
                "mysqlLocalEnabled": bool(plan.get("mysqlLocalEnabled")),
            }
        if payload["active"] not in result:
            raise ValueError("缺少当前接口方案")
        return {"active": payload["active"], "plans": result}

    def load(self):
        with self._lock:
            try:
                return self.sanitize(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                return None

    def save(self, payload):
        rendered = json.dumps(self.sanitize(payload), ensure_ascii=False, allow_nan=False)
        if len(rendered.encode("utf-8")) > 262144:
            raise ValueError("接口方案过大")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(rendered, encoding="utf-8")
            os.replace(temporary, self.path)


def _object(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = payload.get(name)
    if not isinstance(value, Mapping):
        raise PreSensorConfigError(f"{name} must be an object")
    return value


def _number(
    section: Mapping[str, Any],
    field: str,
    *,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> float | int:
    value = section.get(field)
    if isinstance(value, bool):
        raise PreSensorConfigError(f"{field} must be numeric")
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise PreSensorConfigError(f"{field} must be numeric") from exc
    if parsed < minimum or parsed > maximum:
        raise PreSensorConfigError(
            f"{field} must be between {minimum:g} and {maximum:g}"
        )
    return parsed


def _reference(
    references: Mapping[str, Any],
    name: Any,
    *,
    field: str,
) -> Any:
    ref = str(name or "").strip()
    if not ref or not _REFERENCE_NAME.fullmatch(ref):
        raise PreSensorConfigError(f"{field} must be an AFP_* reference")
    if ref not in references:
        raise PreSensorConfigError(f"{field} reference is unresolved: {ref}")
    value = references[ref]
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PreSensorConfigError(f"{field} reference is empty: {ref}")
    return value


def _reject_inline_secrets(value: Any, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}"
            if _SECRET_KEY.search(str(key)):
                raise PreSensorConfigError(f"secret field is not allowed: {child}")
            _reject_inline_secrets(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_inline_secrets(item, f"{path}[{index}]")


def _validate_operational_limits(payload: Mapping[str, Any]) -> None:
    storage = _object(payload, "storage")
    policy = str(storage.get("policy") or "").strip()
    if policy not in {"required", "engineering_preview"}:
        raise PreSensorConfigError("storage.policy must be required or engineering_preview")
    save_ref = str(storage.get("save_root_ref") or "").strip()
    if not _REFERENCE_NAME.fullmatch(save_ref):
        raise PreSensorConfigError("save_root_ref must be a non-empty AFP_* reference")
    mysql_enabled = storage.get("mysql_enabled")
    if not isinstance(mysql_enabled, bool):
        raise PreSensorConfigError("mysql_enabled must be boolean")
    mysql_ref = str(storage.get("mysql_profile_ref") or "").strip()
    if mysql_enabled and not _REFERENCE_NAME.fullmatch(mysql_ref):
        raise PreSensorConfigError(
            "mysql_profile_ref must be an AFP_* reference when MySQL is enabled"
        )
    if not mysql_enabled and mysql_ref:
        raise PreSensorConfigError(
            "mysql_profile_ref cannot be set when MySQL is disabled"
        )

    timeouts = _object(payload, "timeouts")
    _number(timeouts, "first_sample_seconds", minimum=0.01, maximum=30)
    _number(timeouts, "writer_submit_seconds", minimum=0.001, maximum=5)
    _number(timeouts, "writer_stop_seconds", minimum=0.001, maximum=300)
    _number(timeouts, "sampling_stop_seconds", minimum=0.001, maximum=300)
    _number(timeouts, "force_stop_seconds", minimum=0.001, maximum=60)

    reconnect = _object(payload, "reconnect")
    if not isinstance(reconnect.get("enabled"), bool):
        raise PreSensorConfigError("reconnect.enabled must be boolean")
    initial_delay = _number(
        reconnect, "initial_delay_seconds", minimum=0.01, maximum=300
    )
    maximum_delay = _number(
        reconnect, "maximum_delay_seconds", minimum=0.01, maximum=300
    )
    if initial_delay > maximum_delay:
        raise PreSensorConfigError(
            "initial_delay_seconds cannot exceed maximum_delay_seconds"
        )
    _number(reconnect, "maximum_attempts", minimum=1, maximum=1000, integer=True)

    queues = _object(payload, "queues")
    _number(queues, "writer_rows", minimum=1, maximum=200000, integer=True)
    _number(queues, "transport_rows", minimum=1, maximum=1000000, integer=True)
    high_water = _number(queues, "high_water_ratio", minimum=0.01, maximum=0.99)
    if high_water >= 1:
        raise PreSensorConfigError("high_water_ratio must be below 1")
    if queues.get("overflow_policy") != "fail_closed":
        raise PreSensorConfigError("overflow_policy must be fail_closed")

    logging = _object(payload, "logging")
    directory_ref = str(logging.get("directory_ref") or "").strip()
    if not _REFERENCE_NAME.fullmatch(directory_ref):
        raise PreSensorConfigError("logging.directory_ref must be an AFP_* reference")
    if str(logging.get("level") or "").upper() not in {
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    }:
        raise PreSensorConfigError("logging.level is invalid")
    max_file = _number(logging, "max_file_mb", minimum=1, maximum=10240)
    max_total = _number(logging, "max_total_mb", minimum=1, maximum=102400)
    if max_total < max_file:
        raise PreSensorConfigError("max_total_mb cannot be smaller than max_file_mb")
    _number(logging, "retention_days", minimum=1, maximum=3650, integer=True)

    capacity = _object(payload, "capacity")
    _number(capacity, "minimum_free_mb", minimum=1, maximum=100000000)
    _number(capacity, "journal_max_mb", minimum=1, maximum=1000000)
    _number(capacity, "retry_spool_max_mb", minimum=1, maximum=1000000)
    warning = _number(capacity, "warning_ratio", minimum=0.01, maximum=0.99)
    hard = _number(capacity, "hard_limit_ratio", minimum=0.01, maximum=1.0)
    if hard < warning:
        raise PreSensorConfigError(
            "hard_limit_ratio cannot be lower than warning_ratio"
        )


def validate_pre_sensor_config(
    payload: Mapping[str, Any],
    *,
    references: Mapping[str, Any],
) -> AcquisitionConfig:
    """Validate a complete template and map it to the existing core config.

    Validation deliberately completes before an ``AcquisitionConfig`` is
    returned, so callers can keep device opening and Helper commands strictly
    on the far side of this boundary.
    """

    if not isinstance(payload, Mapping):
        raise PreSensorConfigError("configuration must be an object")
    if not isinstance(references, Mapping):
        raise PreSensorConfigError("references must be an object")
    _reject_inline_secrets(payload)

    contract = load_release_contract(start=Path(__file__))
    if payload.get("schema_version") != contract.configuration_schema_version:
        raise PreSensorConfigError("schema_version does not match the release contract")
    if str(payload.get("release_revision") or "") != contract.release_revision:
        raise PreSensorConfigError("release_revision does not match the release contract")
    dataset_schema = str(payload.get("dataset_schema") or "")
    if dataset_schema not in {"new_collection_v13_0", "esr1001_raw_adc"}:
        raise PreSensorConfigError("dataset_schema must be new_collection_v13_0 or esr1001_raw_adc")

    mode = str(payload.get("mode") or "").strip()
    if mode not in {"local_direct", "remote_helper"}:
        raise PreSensorConfigError("mode must be local_direct or remote_helper")
    helper = payload.get("helper")
    if mode == "local_direct" and helper is not None:
        raise PreSensorConfigError("helper is forbidden in local_direct mode")
    if mode == "remote_helper" and not isinstance(helper, Mapping):
        raise PreSensorConfigError("helper is required in remote_helper mode")

    interfaces_value = payload.get("interfaces")
    if not isinstance(interfaces_value, list) or not interfaces_value:
        raise PreSensorConfigError("interfaces must be a non-empty list")
    expected_location = "local" if mode == "local_direct" else "remote_helper"
    interface_specs: list[dict[str, Any]] = []
    interface_ids: set[str] = set()
    for index, raw in enumerate(interfaces_value):
        if not isinstance(raw, Mapping):
            raise PreSensorConfigError(f"interface[{index}] must be an object")
        interface_id = str(raw.get("id") or "").strip()
        if not interface_id or interface_id in interface_ids:
            raise PreSensorConfigError(f"interface id is missing or duplicated: {interface_id}")
        interface_ids.add(interface_id)
        if raw.get("location") != expected_location:
            raise PreSensorConfigError(
                f"interface {interface_id} location must be {expected_location}"
            )
        role = _ROLE_MAP.get(str(raw.get("role") or "").strip())
        if role is None:
            raise PreSensorConfigError(f"interface {interface_id} role is unsupported")
        expected_driver = str(SENSOR_INTERFACE_PROFILES[role]["driver"])
        driver = str(raw.get("driver") or "").strip()
        if driver != expected_driver and not (role == "esr1001_strain" and driver == "esr1001_usb_ttl"):
            raise PreSensorConfigError(
                f"interface {interface_id} driver must be {expected_driver}"
            )
        endpoint_ref = str(raw.get("endpoint_ref") or "").strip()
        if not _REFERENCE_NAME.fullmatch(endpoint_ref):
            raise PreSensorConfigError(
                f"interface {interface_id} endpoint_ref must be an AFP_* reference"
            )
        interface_specs.append(
            {
                "id": interface_id,
                "role": role,
                "driver": driver,
                "endpoint_ref": endpoint_ref,
                "baudrate": int(raw.get("baudrate") or 0),
                "slave_id": int(raw.get("slave_id", 1)),
                "hardware_facts": dict(raw.get("hardware_facts") or {}),
                "data_mode": raw.get("data_mode", "microstrain"),
                "calibration_profile": raw.get("calibration_profile"),
                "bridge": raw.get("bridge", ""),
                "device_identity": dict(raw.get("device_identity") or {}),
            }
        )

    channels_value = payload.get("channels")
    if not isinstance(channels_value, list) or not channels_value:
        raise PreSensorConfigError("channels must be a non-empty list")
    channels: list[str] = []
    assignments = {interface_id: [] for interface_id in interface_ids}
    for index, raw in enumerate(channels_value):
        if not isinstance(raw, Mapping):
            raise PreSensorConfigError(f"channel[{index}] must be an object")
        name = str(raw.get("name") or "").strip()
        metadata = SENSOR_CHANNEL_METADATA.get(name)
        if metadata is None or name in channels:
            raise PreSensorConfigError(f"channel is unsupported or duplicated: {name}")
        unit = str(raw.get("unit") or "").strip()
        if unit != metadata["unit"]:
            raise PreSensorConfigError(
                f"channel {name} unit must be {metadata['unit']}"
            )
        interface_id = str(raw.get("interface_id") or "").strip()
        if interface_id not in interface_ids:
            raise PreSensorConfigError(
                f"channel {name} interface does not exist: {interface_id}"
            )
        channels.append(name)
        assignments[interface_id].append(name)
    required_channels = set(
        (ACQUISITION_SCHEMAS.get(dataset_schema) or {}).get("sensors", [])
    )
    if dataset_schema == "new_collection_v13_0" and any(
        item["driver"] == "esr1001_usb_ttl" and item["data_mode"] == "raw_adc"
        for item in interface_specs
    ):
        required_channels.difference_update(STRAIN_SENSOR_COLUMNS)
        required_channels.update(RAW_CHANNELS)
    if set(channels) != required_channels:
        missing = sorted(required_channels - set(channels))
        raise PreSensorConfigError(f"channel contract is incomplete: {missing}")
    thermocouple_specs = [
        item for item in interface_specs if item["role"] == "thermocouple64"
    ]
    if dataset_schema == "new_collection_v13_0" and len(thermocouple_specs) != 1:
        raise PreSensorConfigError("exactly one thermocouple64 interface is required")
    if thermocouple_specs:
        thermocouple_readiness = hardware_release_readiness(thermocouple_specs[0]["hardware_facts"])
        if not thermocouple_readiness["ready"]:
            raise PreSensorConfigError(str(thermocouple_readiness["message"]))

    _validate_operational_limits(payload)
    storage = _object(payload, "storage")
    timeouts = _object(payload, "timeouts")
    queues = _object(payload, "queues")
    logging = _object(payload, "logging")
    save_root = _reference(
        references, storage.get("save_root_ref"), field="save_root_ref"
    )
    _reference(references, logging.get("directory_ref"), field="logging.directory_ref")

    interfaces: list[dict[str, Any]] = []
    for spec in interface_specs:
        endpoint = _reference(
            references,
            spec["endpoint_ref"],
            field=f"interface {spec['id']} endpoint_ref",
        )
        interface = {
                "id": spec["id"],
                "enabled": True,
                "role": spec["role"],
                "driver": spec["driver"],
                "endpoint": str(endpoint).strip(),
                # Real hardware identity is intentionally pending until the
                # device is physically connected and selected on site.
                "physical_verified": False,
                "hardware_facts": dict(spec["hardware_facts"]),
            }
        if spec["baudrate"]:
            interface["baudrate"] = spec["baudrate"]
        if spec["driver"] == "esr1001_usb_ttl":
            if spec["baudrate"] != 115200:
                raise PreSensorConfigError("原生USB必须使用115200-8-N-1")
            for key in ("data_mode", "calibration_profile", "bridge", "device_identity"):
                interface[key] = spec[key]
        if spec["driver"] in {"modbus_rtu_esr1001", "huipu_tc64_modbus_rtu"}:
            interface["slave_id"] = spec["slave_id"]
        interfaces.append(interface)

    mysql_args: dict[str, Any] = {"mysql_enabled": False}
    if storage.get("mysql_enabled"):
        profile = _reference(
            references, storage.get("mysql_profile_ref"), field="mysql_profile_ref"
        )
        if not isinstance(profile, Mapping):
            raise PreSensorConfigError("mysql_profile_ref must resolve to an object")
        # Credentials stay in the deployment reference store; the core still
        # receives them only in memory for its existing compatibility path.
        mysql_args = {
            "mysql_enabled": True,
            "mysql_host": str(profile.get("host") or ""),
            "mysql_port": int(profile.get("port") or 3306),
            "mysql_user": str(profile.get("user") or ""),
            "mysql_password": str(profile.get("password") or ""),
            "mysql_database": str(profile.get("database") or ""),
            "mysql_target_config_id": str(profile.get("profile_id") or ""),
        }
        if not mysql_args["mysql_host"] or not mysql_args["mysql_user"]:
            raise PreSensorConfigError("mysql profile is incomplete")

    remote_args: dict[str, Any] = {"execution_host": ""}
    if mode == "remote_helper":
        assert isinstance(helper, Mapping)
        _number(helper, "health_timeout_seconds", minimum=0.01, maximum=60)
        _number(helper, "command_timeout_seconds", minimum=0.01, maximum=300)
        required_capabilities = helper.get("required_capabilities")
        if not isinstance(required_capabilities, list) or not required_capabilities:
            raise PreSensorConfigError("helper.required_capabilities must be non-empty")
        _reference(
            references, helper.get("server_url_ref"), field="helper.server_url_ref"
        )
        pairing = _reference(
            references,
            helper.get("pairing_profile_ref"),
            field="helper.pairing_profile_ref",
        )
        if not isinstance(pairing, Mapping):
            raise PreSensorConfigError("helper pairing profile must resolve to an object")
        capabilities = pairing.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise PreSensorConfigError("helper pairing profile lacks capabilities")
        advertised = {
            key for key, value in capabilities.items() if value is True
        } | set(capabilities.get("features") or [])
        missing = [name for name in required_capabilities if name not in advertised]
        if missing:
            raise PreSensorConfigError(f"helper required_capabilities missing: {missing}")
        try:
            assert_helper_contract(capabilities, contract)
        except ReleaseContractError as exc:
            raise PreSensorConfigError(str(exc)) from exc
        remote_args = {
            "execution_host": "helper_local",
            "helper_session_id": str(pairing.get("session_id") or "").strip(),
            "helper_capabilities": dict(capabilities),
        }
        if not remote_args["helper_session_id"]:
            raise PreSensorConfigError("helper pairing profile lacks session_id")

    try:
        return AcquisitionConfig(
            diagnostic_validation=True,
            application_version=contract.application_version,
            release_revision=contract.release_revision,
            configuration_schema_version=contract.configuration_schema_version,
            acquisition_mode="real",
            real_acquisition_mode=mode,
            processing_mode="capture_only",
            dataset_schema=dataset_schema,
            interfaces=interfaces,
            interface_channel_assignments=assignments,
            selected_sensors=channels,
            sample_rate_hz=10.0,
            save_root=str(save_root),
            persistence_policy=str(storage["policy"]),
            writer_queue_rows=int(queues["writer_rows"]),
            writer_submit_timeout_seconds=float(timeouts["writer_submit_seconds"]),
            writer_stop_timeout_seconds=float(timeouts["writer_stop_seconds"]),
            stop_sampling_timeout_seconds=float(timeouts["sampling_stop_seconds"]),
            stop_force_timeout_seconds=float(timeouts["force_stop_seconds"]),
            first_sample_timeout_seconds=float(timeouts["first_sample_seconds"]),
            **remote_args,
            **mysql_args,
        )
    except (TypeError, ValueError) as exc:
        raise PreSensorConfigError(f"acquisition configuration is invalid: {exc}") from exc


def prepare_pre_sensor_start(
    payload: Mapping[str, Any],
    *,
    references: Mapping[str, Any],
    local_start: Callable[[AcquisitionConfig], _T],
    remote_start: Callable[[AcquisitionConfig], _T],
) -> _T:
    """Validate first, then dispatch to exactly one mode-specific starter."""

    config = validate_pre_sensor_config(payload, references=references)
    if config.real_acquisition_mode == "local_direct":
        return local_start(config)
    return remote_start(config)

