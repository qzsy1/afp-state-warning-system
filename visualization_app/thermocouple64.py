from __future__ import annotations

import hashlib
import json
import math
import statistics
import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


THERMOCOUPLE64_MODE_ID = "new_collection_v13_0"
THERMOCOUPLE64_INTERFACE_ID = "thermocouple_64ch"
THERMOCOUPLE64_ROLE = "thermocouple64"
THERMOCOUPLE64_DRIVER = "huipu_tc64_modbus_rtu"
THERMOCOUPLE64_CHANNELS = tuple(f"温度{index}" for index in range(1, 65))
THERMOCOUPLE64_GROUP_IDS = tuple(f"TC-G{index:02d}" for index in range(1, 9))
THERMOCOUPLE64_DERIVED_CHANNELS = tuple(
    f"热电偶均值{index}" for index in range(1, 9)
)
THERMOCOUPLE64_AGGREGATION_VERSION = "tc64-contiguous-mean-v1"
THERMOCOUPLE64_IMPUTATION_VERSION = "specimen-layer-median-v1"
THERMOCOUPLE64_ALLOWED_SCOPES = frozenset(
    {"causal_so_far", "completed_specimen"}
)

HARDWARE_FACT_FIELDS = (
    "manufacturer",
    "model",
    "manual_source",
    "nameplate_identity",
    "protocol_version",
    "interface_type",
    "electrical_topology",
    "sampling_rate_hz",
    "measurement_range_c",
    "cold_junction_compensation",
    "fault_codes",
    "vendor_channel_mapping",
)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def thermocouple_group_channels(group_index: int) -> tuple[str, ...]:
    if not 1 <= int(group_index) <= 8:
        raise ValueError("group_index must be between 1 and 8")
    start = (int(group_index) - 1) * 8 + 1
    return tuple(f"温度{index}" for index in range(start, start + 8))


THERMOCOUPLE64_GROUPS = {
    group_id: thermocouple_group_channels(index)
    for index, group_id in enumerate(THERMOCOUPLE64_GROUP_IDS, start=1)
}


def _catalog_id(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def raw_catalog(channels: Iterable[str], units: Mapping[str, str]) -> dict[str, Any]:
    ordered = tuple(str(name) for name in channels)
    if len(ordered) != len(set(ordered)):
        raise ValueError("raw catalog channels must be unique")
    payload = {
        "kind": "raw",
        "mode_id": THERMOCOUPLE64_MODE_ID,
        "channels": [
            {"name": name, "unit": str(units.get(name) or "")}
            for name in ordered
        ],
    }
    return {**payload, "catalog_id": _catalog_id(payload)}


def derived_catalog(scope: str) -> dict[str, Any]:
    if scope not in THERMOCOUPLE64_ALLOWED_SCOPES:
        raise ValueError(f"unsupported derivation scope: {scope}")
    payload = {
        "kind": "derived",
        "mode_id": THERMOCOUPLE64_MODE_ID,
        "channels": list(THERMOCOUPLE64_DERIVED_CHANNELS),
        "unit": "°C",
        "aggregation_version": THERMOCOUPLE64_AGGREGATION_VERSION,
        "imputation_version": THERMOCOUPLE64_IMPUTATION_VERSION,
        "scope": scope,
    }
    return {**payload, "catalog_id": _catalog_id(payload)}


def hardware_fact_issues(facts: Mapping[str, Any] | None) -> list[str]:
    source = facts if isinstance(facts, Mapping) else {}
    return [
        field
        for field in HARDWARE_FACT_FIELDS
        if source.get(field) in (None, "", [], {})
    ]


def hardware_release_readiness(facts: Mapping[str, Any] | None) -> dict[str, Any]:
    missing = hardware_fact_issues(facts)
    return {
        "ready": not missing,
        "evidence_level": "physical_hardware" if not missing else "unverified",
        "missing_facts": missing,
        "message": (
            "64路热电偶硬件事实已完整记录"
            if not missing
            else "64路热电偶实机验收资料不完整；缺少硬件事实：" + ", ".join(missing)
        ),
    }


@dataclass(frozen=True)
class Thermocouple64Snapshot:
    device_identity: str
    source_sequence: int
    source_timestamp: float
    values: Mapping[str, float | None]
    quality: Mapping[str, str]
    errors: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_identity": self.device_identity,
            "source_sequence": self.source_sequence,
            "source_timestamp": self.source_timestamp,
            "values": dict(self.values),
            "quality": dict(self.quality),
            "errors": dict(self.errors),
        }


class Thermocouple64AdapterContract:
    """Validate vendor output without assuming any physical device protocol."""

    def __init__(self) -> None:
        self.last_sequence_by_device: dict[str, int] = {}

    @staticmethod
    def _named_values(payload: Any) -> dict[str, Any]:
        if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
            if len(payload) != 64:
                raise ValueError("thermocouple64 array must contain exactly 64 values")
            return {
                name: value for name, value in zip(THERMOCOUPLE64_CHANNELS, payload)
            }
        if not isinstance(payload, Mapping):
            raise ValueError("thermocouple64 values must be an array or object")
        mapped: dict[str, Any] = {}
        for raw_name, value in payload.items():
            source = str(raw_name).strip()
            if source in THERMOCOUPLE64_CHANNELS:
                target = source
            else:
                import re

                match = re.fullmatch(r"(?:CH|TC|CHANNEL)[_ -]?(\d+)", source, re.I)
                if not match or not 1 <= int(match.group(1)) <= 64:
                    raise ValueError(f"unknown thermocouple64 channel: {source}")
                target = f"温度{int(match.group(1))}"
            if target in mapped:
                raise ValueError(f"duplicate thermocouple64 channel: {target}")
            mapped[target] = value
        return mapped

    def adapt(
        self,
        *,
        device_identity: str,
        source_sequence: int,
        source_timestamp: float,
        values: Any,
        errors: Mapping[str, Any] | None = None,
    ) -> Thermocouple64Snapshot:
        identity = str(device_identity or "").strip()
        if not identity:
            raise ValueError("device_identity is required")
        sequence = int(source_sequence)
        if sequence < 0:
            raise ValueError("source_sequence must be non-negative")
        previous = self.last_sequence_by_device.get(identity)
        if previous is not None and sequence <= previous:
            reason = "duplicate" if sequence == previous else "out_of_order"
            raise ValueError(f"{reason} source sequence: {sequence}")
        timestamp = _finite(source_timestamp)
        if timestamp is None:
            raise ValueError("source_timestamp must be finite")
        named = self._named_values(values)
        raw_errors = errors if isinstance(errors, Mapping) else {}
        normalized_values: dict[str, float | None] = {}
        quality: dict[str, str] = {}
        normalized_errors: dict[str, str] = {}
        for channel in THERMOCOUPLE64_CHANNELS:
            value = _finite(named.get(channel))
            error = str(raw_errors.get(channel) or "").strip()
            normalized_values[channel] = value
            if error:
                quality[channel] = "invalid"
                normalized_errors[channel] = error
                normalized_values[channel] = None
            elif channel not in named:
                quality[channel] = "missing"
                normalized_errors[channel] = "source_channel_missing"
            elif value is None:
                quality[channel] = "invalid"
                normalized_errors[channel] = "non_finite_value"
            else:
                quality[channel] = "measured_new"
        self.last_sequence_by_device[identity] = sequence
        return Thermocouple64Snapshot(
            device_identity=identity,
            source_sequence=sequence,
            source_timestamp=timestamp,
            values=normalized_values,
            quality=quality,
            errors=normalized_errors,
        )


def strict_group_means(
    values: Mapping[str, Any],
    quality: Mapping[str, str] | None = None,
    *,
    scope: str = "causal_so_far",
) -> dict[str, dict[str, Any]]:
    if scope not in THERMOCOUPLE64_ALLOWED_SCOPES:
        raise ValueError(f"unsupported derivation scope: {scope}")
    quality = quality or {}
    result: dict[str, dict[str, Any]] = {}
    accepted = {"measured_new", "held_within_freshness", "imputed_layer_median"}
    for group_index, (group_id, channels) in enumerate(
        THERMOCOUPLE64_GROUPS.items(), start=1
    ):
        finite_values: list[float] = []
        invalid_channels: list[str] = []
        imputed_channels: list[str] = []
        for channel in channels:
            number = _finite(values.get(channel))
            channel_quality = str(quality.get(channel) or "measured_new")
            if number is None or channel_quality not in accepted:
                invalid_channels.append(channel)
                continue
            finite_values.append(number)
            if channel_quality == "imputed_layer_median":
                imputed_channels.append(channel)
        name = THERMOCOUPLE64_DERIVED_CHANNELS[group_index - 1]
        usable = len(finite_values) == 8
        result[name] = {
            "value": sum(finite_values) / 8.0 if usable else None,
            "quality": "derived_valid" if usable else "derived_unavailable",
            "model_usable": usable,
            "valid_count": len(finite_values),
            "group_id": group_id,
            "source_channels": list(channels),
            "source_range": [channels[0], channels[-1]],
            "invalid_channels": invalid_channels,
            "imputed_channels": imputed_channels,
            "scope": scope,
            "aggregation_version": THERMOCOUPLE64_AGGREGATION_VERSION,
        }
    return result


def validate_group_mean_model_contract(metadata: Mapping[str, Any]) -> list[str]:
    expected = list(THERMOCOUPLE64_DERIVED_CHANNELS)
    issues: list[str] = []
    input_sensors = list(metadata.get("input_sensors") or [])
    temperature_features = list(
        metadata.get("temperature_features")
        or [name for name in input_sensors if str(name).startswith("热电偶均值")]
    )
    if temperature_features != expected:
        issues.append("temperature_features must be 热电偶均值1..热电偶均值8 in order")
    if any(name in input_sensors for name in (f"温度{index}" for index in range(1, 9))):
        issues.append("legacy physical 温度1..温度8 features cannot receive group means")
    if metadata.get("aggregation_version") != THERMOCOUPLE64_AGGREGATION_VERSION:
        issues.append("aggregation_version mismatch")
    if metadata.get("imputation_scope") not in THERMOCOUPLE64_ALLOWED_SCOPES:
        issues.append("imputation_scope must be causal_so_far or completed_specimen")
    if int(metadata.get("temperature_feature_dimension") or 0) != 8:
        issues.append("temperature_feature_dimension must be 8")
    scaling = metadata.get("temperature_feature_scaling")
    if not isinstance(scaling, Mapping) or set(scaling) != set(expected):
        issues.append("scaling must describe every group-mean feature")
    return issues


def _raw_digest(layers: Sequence[Mapping[str, Any]]) -> str:
    def safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else {"non_finite": repr(value)}
        if isinstance(value, Mapping):
            return {str(key): safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [safe(item) for item in value]
        return value

    canonical = json.dumps(
        safe(layers),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def project_specimen_layers(
    layers: Sequence[Mapping[str, Any]],
    *,
    sensor_columns: Iterable[str],
    scope: str,
    include_row_metadata: bool = True,
) -> dict[str, Any]:
    """Build aligned raw and imputed derived views without mutating inputs."""

    if scope not in THERMOCOUPLE64_ALLOWED_SCOPES:
        raise ValueError(f"unsupported derivation scope: {scope}")
    sensors = tuple(dict.fromkeys(str(name) for name in sensor_columns))
    normalized_layers: list[dict[str, Any]] = []
    for position, layer in enumerate(layers):
        rows = layer.get("rows") if isinstance(layer, Mapping) else None
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            raise ValueError("each layer must contain a rows array")
        normalized_layers.append(
            {
                "layer_id": layer.get("layer_id", position + 1),
                "completed": bool(layer.get("completed", True)),
                "rows": [dict(row) for row in rows if isinstance(row, Mapping)],
            }
        )
    raw_layers = [
        {
            **layer,
            "rows": [
                {**row, **{name: row.get(name) for name in sensors if name not in row}}
                for row in layer["rows"]
            ],
        }
        for layer in normalized_layers
    ]
    derived_layers = copy.deepcopy(raw_layers)
    imputations: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for target_index, target in enumerate(normalized_layers):
        allowed_indices = (
            range(len(normalized_layers))
            if scope == "completed_specimen"
            else range(target_index + 1)
        )
        allowed_layers = [
            normalized_layers[index]
            for index in allowed_indices
            if normalized_layers[index]["completed"] or index == target_index
        ]
        for channel in sensors:
            target_values = [_finite(row.get(channel)) for row in target["rows"]]
            if any(value is not None for value in target_values):
                continue
            source_values = [
                value
                for layer in allowed_layers
                for row in layer["rows"]
                for value in [_finite(row.get(channel))]
                if value is not None
            ]
            if not source_values:
                unavailable.append(
                    {"layer_id": target["layer_id"], "channel": channel}
                )
                continue
            scalar = float(statistics.median(source_values))
            mask: list[int] = []
            for row_index, row in enumerate(derived_layers[target_index]["rows"]):
                row[channel] = scalar
                mask.append(row_index)
            imputations.append(
                {
                    "layer_id": target["layer_id"],
                    "channel": channel,
                    "value": scalar,
                    "sample_count": len(source_values),
                    "row_mask": mask,
                    "scope": scope,
                }
            )
    imputation_lookup = {
        (item["layer_id"], item["channel"]): item for item in imputations
    }
    for layer in derived_layers:
        for row in layer["rows"]:
            if not include_row_metadata:
                for name, channels in zip(THERMOCOUPLE64_DERIVED_CHANNELS, THERMOCOUPLE64_GROUPS.values()):
                    values = [_finite(row.get(channel)) for channel in channels]
                    row[name] = sum(values) / 8.0 if all(value is not None for value in values) else None
                continue
            quality = {
                channel: (
                    "imputed_layer_median"
                    if (layer["layer_id"], channel) in imputation_lookup
                    else "measured_new"
                    if _finite(row.get(channel)) is not None
                    else "missing"
                )
                for channel in THERMOCOUPLE64_CHANNELS
                if channel in sensors
            }
            means = strict_group_means(row, quality, scope=scope)
            for name, item in means.items():
                row[name] = item["value"]
            row["_thermocouple64_derived"] = means
    manifest_body = {
        "scope": scope,
        "sensor_union": list(sensors),
        "layer_ids": [layer["layer_id"] for layer in normalized_layers],
        "imputations": imputations,
        "unavailable": unavailable,
        "aggregation_version": THERMOCOUPLE64_AGGREGATION_VERSION,
        "imputation_version": THERMOCOUPLE64_IMPUTATION_VERSION,
        "raw_sha256": _raw_digest(normalized_layers),
    }
    return {
        "raw_layers": raw_layers,
        "derived_layers": derived_layers,
        "manifest": {
            **manifest_body,
            "manifest_sha256": _catalog_id(manifest_body),
        },
    }


def encode_derived_realtime_payload(
    *,
    sequence: int,
    catalog: Mapping[str, Any],
    means: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if str(catalog.get("kind") or "") != "derived":
        raise ValueError("derived payload requires a derived catalog")
    channels = list(catalog.get("channels") or [])
    if channels != list(THERMOCOUPLE64_DERIVED_CHANNELS):
        raise ValueError("derived catalog channel order mismatch")
    return {
        "contract_version": "thermocouple64_derived_v1",
        "catalog_id": str(catalog.get("catalog_id") or ""),
        "sequence": int(sequence),
        "values": [means[name].get("value") for name in channels],
        "valid_counts": [int(means[name].get("valid_count") or 0) for name in channels],
        "imputed": [bool(means[name].get("imputed_channels")) for name in channels],
        "scope": str(catalog.get("scope") or ""),
        "aggregation_version": str(catalog.get("aggregation_version") or ""),
    }


def decode_derived_realtime_payload(
    payload: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    if str(payload.get("contract_version") or "") != "thermocouple64_derived_v1":
        raise ValueError("unsupported derived payload contract")
    if str(payload.get("catalog_id") or "") != str(catalog.get("catalog_id") or ""):
        raise ValueError("derived catalog_id mismatch")
    channels = list(catalog.get("channels") or [])
    arrays = [payload.get("values"), payload.get("valid_counts"), payload.get("imputed")]
    if any(not isinstance(array, list) or len(array) != len(channels) for array in arrays):
        raise ValueError("derived payload array length mismatch")
    result: dict[str, dict[str, Any]] = {}
    for index, name in enumerate(channels):
        value = _finite(payload["values"][index])
        if payload["values"][index] is not None and value is None:
            raise ValueError(f"non-finite derived value at index {index}")
        result[str(name)] = {
            "value": value,
            "valid_count": int(payload["valid_counts"][index]),
            "imputed": bool(payload["imputed"][index]),
            "scope": str(payload.get("scope") or ""),
            "aggregation_version": str(payload.get("aggregation_version") or ""),
        }
    return result
