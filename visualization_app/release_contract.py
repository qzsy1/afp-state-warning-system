from __future__ import annotations

import json
import re
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ReleaseContractError(ValueError):
    """Raised when a release identity is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class ReleaseContract:
    schema_version: int
    application_version: str
    helper_version: str
    launcher_version: str
    release_revision: str
    helper_protocol_version: int
    unified_frame_contract: str
    configuration_schema_version: int
    readiness_schema_version: int
    required_static_assets: tuple[str, ...]
    managed_file_manifest: str
    digest_algorithm: str
    managed_model_roots: tuple[str, ...]
    source_path: Path

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "application_version": self.application_version,
            "helper_version": self.helper_version,
            "launcher_version": self.launcher_version,
            "release_revision": self.release_revision,
            "helper_protocol_version": self.helper_protocol_version,
            "unified_frame_contract": self.unified_frame_contract,
            "configuration_schema_version": self.configuration_schema_version,
            "readiness_schema_version": self.readiness_schema_version,
        }


def _required_object(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = payload.get(name)
    if not isinstance(value, Mapping):
        raise ReleaseContractError(f"release contract {name} must be an object")
    return value


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ReleaseContractError(f"release contract {name} is required")
    return value


def release_contract_from_mapping(
    payload: Mapping[str, Any],
    *,
    source_path: Path | None = None,
) -> ReleaseContract:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ReleaseContractError("release contract schema_version must be 1")
    identity = _required_object(payload, "identity")
    contracts = _required_object(payload, "contracts")
    static = _required_object(payload, "static_assets")
    managed = _required_object(payload, "managed_files")
    assets = static.get("required")
    if not isinstance(assets, list) or not assets or not all(
        isinstance(item, str) and item.strip() for item in assets
    ):
        raise ReleaseContractError("release contract static_assets.required must be non-empty")
    model_roots = managed.get("managed_model_roots")
    if not isinstance(model_roots, list) or not model_roots:
        raise ReleaseContractError("release contract managed_model_roots must be non-empty")
    normalized_model_roots: list[str] = []
    for value in model_roots:
        relative = str(value or "").strip().replace("\\", "/").strip("/")
        parts = Path(relative).parts
        if (
            not relative
            or Path(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ReleaseContractError(
                "release contract managed_model_roots must contain normalized relative paths"
            )
        normalized_model_roots.append(relative)
    contract = ReleaseContract(
        schema_version=1,
        application_version=_required_text(identity, "application_version"),
        helper_version=_required_text(identity, "helper_version"),
        launcher_version=_required_text(identity, "launcher_version"),
        release_revision=_required_text(identity, "release_revision"),
        helper_protocol_version=int(contracts.get("helper_protocol_version") or 0),
        unified_frame_contract=_required_text(contracts, "unified_frame_contract"),
        configuration_schema_version=int(
            contracts.get("configuration_schema_version") or 0
        ),
        readiness_schema_version=int(contracts.get("readiness_schema_version") or 0),
        required_static_assets=tuple(str(item) for item in assets),
        managed_file_manifest=_required_text(managed, "manifest"),
        digest_algorithm=_required_text(managed, "digest_algorithm").lower(),
        managed_model_roots=tuple(normalized_model_roots),
        source_path=Path(source_path or "<mapping>"),
    )
    versions = {
        contract.application_version,
        contract.helper_version,
        contract.launcher_version,
    }
    if len(versions) != 1:
        raise ReleaseContractError(
            "application_version, helper_version and launcher_version must match"
        )
    if contract.helper_protocol_version < 1:
        raise ReleaseContractError("helper_protocol_version must be positive")
    if contract.configuration_schema_version < 1 or contract.readiness_schema_version < 1:
        raise ReleaseContractError("configuration/readiness schema versions must be positive")
    if contract.digest_algorithm != "sha256":
        raise ReleaseContractError("managed file digest_algorithm must be sha256")
    return contract


def managed_model_paths(contract: ReleaseContract) -> tuple[Path, ...]:
    source = contract.source_path.resolve()
    if source.name.lower() == "release-manifest.json":
        root = source.parent
    elif source.name.lower() == "release-contract.json" and source.parent.name.lower() == "pre_sensor":
        root = source.parent.parent / "visualization_app"
    else:
        root = source.parent
    return tuple((root / relative).resolve() for relative in contract.managed_model_roots)


def _candidate_paths(start: Path | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    configured = str(os.environ.get("AFP_RELEASE_CONTRACT_FILE") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    origin = Path(start or __file__).resolve()
    base = origin if origin.is_dir() else origin.parent
    search_roots = [base, *base.parents]
    if getattr(sys, "frozen", False):
        executable_root = Path(sys.executable).resolve().parent
        search_roots.extend((executable_root, *executable_root.parents))
    for parent in dict.fromkeys(search_roots):
        candidates.append(parent / "RELEASE-MANIFEST.json")
        candidates.append(parent / "pre_sensor" / "release-contract.json")
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def load_release_contract(
    *,
    start: Path | None = None,
    explicit: Path | None = None,
) -> ReleaseContract:
    candidates = (Path(explicit).resolve(),) if explicit is not None else _candidate_paths(start)
    selected = next((path for path in candidates if path.is_file()), None)
    if selected is None:
        rendered = ", ".join(str(path) for path in candidates)
        raise ReleaseContractError(f"release contract not found; checked: {rendered}")
    try:
        payload = json.loads(selected.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseContractError(f"release contract unreadable: {selected}: {exc}") from exc
    return release_contract_from_mapping(payload, source_path=selected)


def compare_release_contracts(
    expected: ReleaseContract,
    actual: Mapping[str, Any] | ReleaseContract,
) -> tuple[str, ...]:
    if isinstance(actual, ReleaseContract):
        actual_identity = actual.identity_payload()
    else:
        identity = actual.get("identity") if isinstance(actual, Mapping) else None
        contracts = actual.get("contracts") if isinstance(actual, Mapping) else None
        actual_identity = {
            **(dict(identity) if isinstance(identity, Mapping) else {}),
            **(dict(contracts) if isinstance(contracts, Mapping) else {}),
        }
    expected_identity = expected.identity_payload()
    return tuple(
        field
        for field, value in expected_identity.items()
        if actual_identity.get(field) != value
    )


def assert_config_contract(config: Any, expected: ReleaseContract) -> None:
    fields = {
        "application_version": expected.application_version,
        "release_revision": expected.release_revision,
        "configuration_schema_version": expected.configuration_schema_version,
    }
    mismatches = [
        name
        for name, value in fields.items()
        if getattr(config, name, None) != value
    ]
    if mismatches:
        raise ReleaseContractError(
            "release_contract_mismatch: " + ", ".join(mismatches)
        )


def helper_capability_mismatches(
    expected: ReleaseContract,
    capabilities: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    actual = capabilities if isinstance(capabilities, Mapping) else {}
    expected_values = {
        "protocol_version": expected.helper_protocol_version,
        "helper_version": expected.helper_version,
        "build_id": expected.release_revision,
        "unified_frame_contract": expected.unified_frame_contract,
        "configuration_schema_version": expected.configuration_schema_version,
        "readiness_schema_version": expected.readiness_schema_version,
    }
    candidate_id, helper_sha256 = release_manifest_helper_identity(expected)
    if candidate_id:
        expected_values["candidate_id"] = candidate_id
    if helper_sha256:
        expected_values["helper_sha256"] = helper_sha256
    mismatches = [
        name for name, value in expected_values.items() if actual.get(name) != value
    ]
    if actual.get("unified_frame_v1") is not True:
        mismatches.append("unified_frame_v1")
    return tuple(mismatches)


def release_manifest_helper_identity(contract: ReleaseContract) -> tuple[str, str]:
    try:
        payload = json.loads(contract.source_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "", ""
    if not isinstance(payload, Mapping):
        return "", ""
    candidate = payload.get("candidate")
    candidate_id = (
        str(candidate.get("candidate_id") or "").strip()
        if isinstance(candidate, Mapping)
        else ""
    )
    runtime = payload.get("runtime_requirements")
    executables = runtime.get("required_executables") if isinstance(runtime, Mapping) else None
    helper_sha256 = ""
    if isinstance(executables, list):
        for item in executables:
            if isinstance(item, Mapping) and item.get("id") == "local_capture_helper":
                value = str(item.get("sha256") or "").strip().lower()
                if re.fullmatch(r"[0-9a-f]{64}", value):
                    helper_sha256 = value
                break
    return candidate_id, helper_sha256


def assert_helper_contract(
    capabilities: Mapping[str, Any] | None,
    expected: ReleaseContract,
) -> None:
    mismatches = helper_capability_mismatches(expected, capabilities)
    if mismatches:
        raise ReleaseContractError(
            "release_contract_mismatch: " + ", ".join(mismatches)
        )
