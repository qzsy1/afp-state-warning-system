"""Strict, immutable view of the shared public ingress generation state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

try:
    from .release_contract import ReleaseContract
except ImportError:
    from release_contract import ReleaseContract


class PublicIngressStateError(ValueError):
    """The public ingress state is absent, malformed, or inconsistent."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class PublicIngressState:
    schema_version: int
    generation: int
    public_url: str
    provider: str
    recovery_owner: str
    candidate_id: str
    status: str
    published_at: str
    layers: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "layers", _freeze(self.layers))


def public_ingress_state_path(contract: ReleaseContract) -> Path:
    source = Path(contract.source_path).resolve()
    manifest_root = source.parent.parent if source.parent.name.lower() == "pre_sensor" else source.parent
    return manifest_root / "runtime" / "public-ingress-state.json"


def release_candidate_id(contract: ReleaseContract) -> str:
    source = Path(contract.source_path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return contract.release_revision
    candidate = payload.get("candidate") if isinstance(payload, dict) else None
    candidate_id = str(candidate.get("candidate_id") or "").strip() if isinstance(candidate, dict) else ""
    return candidate_id or contract.release_revision


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise PublicIngressStateError(f"public ingress field is required: {name}")
    return value.strip()


def load_public_ingress_state(
    path: Path,
    expected_candidate_id: str = "",
) -> PublicIngressState:
    state_path = Path(path)
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise PublicIngressStateError(f"public ingress state is missing: {state_path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicIngressStateError(f"public ingress state is unreadable: {state_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise PublicIngressStateError("public ingress state must be an object")

    required = {
        "schema_version", "generation", "public_url", "provider", "recovery_owner",
        "candidate_id", "status", "published_at", "layers",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise PublicIngressStateError(f"public ingress fields are missing: {', '.join(missing)}")
    if payload.get("schema_version") != 1:
        raise PublicIngressStateError("public ingress schema_version must be 1")
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise PublicIngressStateError("public ingress generation must be a nonnegative integer")

    public_url = _required_text(payload, "public_url")
    parsed_url = urlsplit(public_url)
    if (
        parsed_url.scheme.lower() != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.fragment
    ):
        raise PublicIngressStateError("public ingress URL must be absolute HTTPS without credentials or fragment")
    provider = _required_text(payload, "provider")
    recovery_owner = _required_text(payload, "recovery_owner")
    candidate_id = _required_text(payload, "candidate_id")
    status = _required_text(payload, "status")
    published_at = _required_text(payload, "published_at")
    try:
        timestamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicIngressStateError("public ingress published_at must be ISO-8601") from exc
    if timestamp.tzinfo is None:
        raise PublicIngressStateError("public ingress published_at must include a timezone")
    layers = payload.get("layers")
    if not isinstance(layers, Mapping):
        raise PublicIngressStateError("public ingress layers must be an object")
    if expected_candidate_id and candidate_id != str(expected_candidate_id):
        raise PublicIngressStateError("public ingress candidate_id mismatch")

    return PublicIngressState(
        schema_version=1,
        generation=generation,
        public_url=public_url,
        provider=provider.lower(),
        recovery_owner=recovery_owner.lower(),
        candidate_id=candidate_id,
        status=status.lower(),
        published_at=published_at,
        layers=layers,
    )
