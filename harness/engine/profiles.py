from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import CheckSpec, GateConfigError, RegressionMatrix


VALID_PROFILES = ("quick", "full", "release")


def load_profiles(path: Path) -> dict[str, tuple[str, ...]]:
    path = Path(path)
    if not path.is_file():
        raise GateConfigError(f"profiles file does not exist: {path}")
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateConfigError(f"profiles file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "profiles"}:
        raise GateConfigError("profiles file must contain only schema_version and profiles")
    if payload["schema_version"] != 1 or not isinstance(payload["profiles"], dict):
        raise GateConfigError("profiles.schema_version must be 1 and profiles must be an object")
    if set(payload["profiles"]) != set(VALID_PROFILES):
        raise GateConfigError("profiles must define quick, full, and release")
    result: dict[str, tuple[str, ...]] = {}
    for name in VALID_PROFILES:
        values = payload["profiles"][name]
        if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
            raise GateConfigError(f"profiles.{name} must be a non-empty string array")
        if len(values) != len(set(values)):
            raise GateConfigError(f"profiles.{name} contains duplicate check ids")
        result[name] = tuple(values)
    return result


def select_profile_checks(
    matrix: RegressionMatrix,
    profiles: dict[str, tuple[str, ...]],
    profile: str,
    environment: str,
) -> tuple[CheckSpec, ...]:
    if profile not in profiles:
        raise GateConfigError(f"unknown profile: {profile}")
    by_id = {check.id: check for check in matrix.checks}
    unknown = [check_id for check_id in profiles[profile] if check_id not in by_id]
    if unknown:
        raise GateConfigError(f"profile {profile} references unknown checks: {', '.join(unknown)}")
    selected = tuple(
        by_id[check_id]
        for check_id in profiles[profile]
        if environment in by_id[check_id].environments
    )
    covered = {requirement for check in selected for requirement in check.requirements}
    missing = [
        item.id for item in matrix.requirements
        if item.mandatory and profile in item.profiles and item.id not in covered
    ]
    if missing:
        raise GateConfigError(
            f"mandatory requirements have no selected check for {profile}/{environment}: {', '.join(missing)}"
        )
    return selected


def select_single_check(matrix: RegressionMatrix, check_id: str, environment: str) -> CheckSpec:
    for check in matrix.checks:
        if check.id == check_id:
            if environment not in check.environments:
                raise GateConfigError(f"check {check_id} is not available in environment {environment}")
            return check
    raise GateConfigError(f"unknown check: {check_id}")
