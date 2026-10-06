from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .models import CheckSpec, GateConfigError, RegressionMatrix, RequirementSpec


VALID_PROFILES = ("quick", "full", "release")
VALID_ENVIRONMENTS = ("local", "ci")
VALID_EVIDENCE_TIERS = ("automated", "local_integration", "field", "unverified")
VALID_KINDS = ("command", "field")
TOP_LEVEL_FIELDS = {"schema_version", "requirements", "profiles", "checks"}
REQUIREMENT_FIELDS = {"id", "title", "mandatory", "profiles"}
CHECK_FIELDS = {
    "id",
    "title",
    "kind",
    "command",
    "cwd",
    "timeout_seconds",
    "profiles",
    "environments",
    "blocking",
    "evidence_tier",
    "requirements",
    "change_patterns",
}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateConfigError(f"{where} must be an object")
    return value


def _strict_fields(value: dict[str, Any], required: set[str], allowed: set[str], where: str) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise GateConfigError(f"{where} missing fields: {', '.join(missing)}")
    if unknown:
        raise GateConfigError(f"{where} has unknown fields: {', '.join(unknown)}")


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateConfigError(f"{where} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, where: str) -> str:
    result = _string(value, where)
    if not ID_PATTERN.fullmatch(result):
        raise GateConfigError(f"{where} contains unsupported characters")
    return result


def _string_tuple(
    value: Any,
    where: str,
    *,
    nonempty: bool = True,
    unique: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise GateConfigError(f"{where} must be a {qualifier}array")
    result = tuple(_string(item, f"{where} item") for item in value)
    if unique and len(set(result)) != len(result):
        raise GateConfigError(f"{where} contains duplicates")
    return result


def _validated_choices(value: Any, where: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
    result = _string_tuple(value, where)
    invalid = sorted(set(result) - set(allowed))
    if invalid:
        raise GateConfigError(f"{where} contains unknown value: {', '.join(invalid)}")
    return result


def _repo_relative_path(value: Any, where: str) -> str:
    normalized = _string(value, where).replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or re.match(r"^[A-Za-z]:/", normalized) or ".." in path.parts:
        raise GateConfigError(f"{where} must be a repository-relative path")
    return path.as_posix()


def _parse_requirement(raw: Any, index: int) -> RequirementSpec:
    where = f"requirements[{index}]"
    value = _mapping(raw, where)
    _strict_fields(value, REQUIREMENT_FIELDS, REQUIREMENT_FIELDS, where)
    if not isinstance(value["mandatory"], bool):
        raise GateConfigError(f"{where}.mandatory must be boolean")
    return RequirementSpec(
        id=_identifier(value["id"], f"{where}.id"),
        title=_string(value["title"], f"{where}.title"),
        mandatory=value["mandatory"],
        profiles=_validated_choices(value["profiles"], f"{where}.profiles", VALID_PROFILES),
    )


def _parse_check(raw: Any, index: int) -> CheckSpec:
    where = f"checks[{index}]"
    value = _mapping(raw, where)
    required = CHECK_FIELDS - {"change_patterns"}
    _strict_fields(value, required, CHECK_FIELDS, where)
    kind = _string(value["kind"], f"{where}.kind")
    if kind not in VALID_KINDS:
        raise GateConfigError(f"{where}.kind contains unknown kind: {kind}")
    command = _string_tuple(
        value["command"],
        f"{where}.command",
        nonempty=kind == "command",
        unique=False,
    )
    if kind == "field" and command:
        raise GateConfigError(f"{where}.command must be empty for field checks")
    timeout = value["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise GateConfigError(f"{where}.timeout_seconds must be positive")
    if not isinstance(value["blocking"], bool):
        raise GateConfigError(f"{where}.blocking must be boolean")
    evidence_tier = _string(value["evidence_tier"], f"{where}.evidence_tier")
    if evidence_tier not in VALID_EVIDENCE_TIERS:
        raise GateConfigError(f"{where}.evidence_tier contains unknown evidence value: {evidence_tier}")
    patterns = _string_tuple(value.get("change_patterns", []), f"{where}.change_patterns", nonempty=False)
    return CheckSpec(
        id=_identifier(value["id"], f"{where}.id"),
        title=_string(value["title"], f"{where}.title"),
        kind=kind,
        command=command,
        cwd=_repo_relative_path(value["cwd"], f"{where}.cwd"),
        timeout_seconds=float(timeout),
        profiles=_validated_choices(value["profiles"], f"{where}.profiles", VALID_PROFILES),
        environments=_validated_choices(value["environments"], f"{where}.environments", VALID_ENVIRONMENTS),
        blocking=value["blocking"],
        evidence_tier=evidence_tier,
        requirements=_string_tuple(value["requirements"], f"{where}.requirements"),
        change_patterns=patterns,
    )


def load_matrix(path: Path) -> RegressionMatrix:
    path = Path(path)
    if not path.is_file():
        raise GateConfigError(f"matrix does not exist: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateConfigError(f"matrix is not valid JSON: {path}: {exc}") from exc
    root = _mapping(raw, "matrix")
    _strict_fields(root, TOP_LEVEL_FIELDS, TOP_LEVEL_FIELDS, "matrix")
    if root["schema_version"] != 1:
        raise GateConfigError("matrix.schema_version must be 1")
    profiles = _validated_choices(root["profiles"], "matrix.profiles", VALID_PROFILES)
    if set(profiles) != set(VALID_PROFILES):
        raise GateConfigError("matrix.profiles must define quick, full, and release")
    if not isinstance(root["requirements"], list) or not root["requirements"]:
        raise GateConfigError("matrix.requirements must be a non-empty array")
    if not isinstance(root["checks"], list) or not root["checks"]:
        raise GateConfigError("matrix.checks must be a non-empty array")
    requirements = tuple(_parse_requirement(value, index) for index, value in enumerate(root["requirements"]))
    checks = tuple(_parse_check(value, index) for index, value in enumerate(root["checks"]))
    requirement_ids = [item.id for item in requirements]
    check_ids = [item.id for item in checks]
    if len(set(requirement_ids)) != len(requirement_ids):
        raise GateConfigError("duplicate requirement id")
    if len(set(check_ids)) != len(check_ids):
        raise GateConfigError("duplicate check id")
    known_requirements = set(requirement_ids)
    for check in checks:
        unknown = sorted(set(check.requirements) - known_requirements)
        if unknown:
            raise GateConfigError(f"check {check.id} references unknown requirement: {', '.join(unknown)}")
    covered = {requirement for check in checks for requirement in check.requirements}
    missing = [item.id for item in requirements if item.mandatory and item.id not in covered]
    if missing:
        raise GateConfigError(f"mandatory requirements have no validation item: {', '.join(missing)}")
    return RegressionMatrix(1, profiles, requirements, checks)


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def select_checks(
    matrix: RegressionMatrix,
    profile: str,
    environment: str,
    changed_files: Sequence[str] = (),
) -> tuple[CheckSpec, ...]:
    if profile not in VALID_PROFILES:
        raise GateConfigError(f"unknown profile: {profile}")
    if environment not in VALID_ENVIRONMENTS:
        raise GateConfigError(f"unknown environment: {environment}")
    available = tuple(
        check for check in matrix.checks if profile in check.profiles and environment in check.environments
    )
    if profile == "quick":
        always = tuple(check for check in available if not check.change_patterns)
        affected = tuple(
            check
            for check in available
            if check.change_patterns and any(_matches_any(path, check.change_patterns) for path in changed_files)
        )
        selected = always + tuple(check for check in affected if check not in always)
        if not changed_files or not affected:
            selected = available
    else:
        selected = available
    covered = {requirement for check in selected for requirement in check.requirements}
    missing = [
        item.id
        for item in matrix.requirements
        if item.mandatory and profile in item.profiles and item.id not in covered
    ]
    if missing:
        raise GateConfigError(
            f"mandatory requirements have no selected check for {profile}/{environment}: {', '.join(missing)}"
        )
    return selected
