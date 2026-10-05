from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .models import GateConfigError


CATEGORIES = ("external_update", "rebuild_required", "manual_review")
HEX_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class ExePolicyError(RuntimeError):
    """Raised when stable executable evidence cannot be trusted."""


@dataclass(frozen=True)
class ExeRule:
    pattern: str
    reason: str


@dataclass(frozen=True)
class ExeRules:
    schema_version: int
    external_update: tuple[ExeRule, ...]
    rebuild_required: tuple[ExeRule, ...]
    manual_review: tuple[ExeRule, ...]


@dataclass(frozen=True)
class RuleMatch:
    path: str
    category: str
    pattern: str
    reason: str


@dataclass(frozen=True)
class ExeDecision:
    decision: str
    changed_files: tuple[str, ...]
    matches: tuple[RuleMatch, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ExeHashEvidence:
    path: str
    baseline_sha256: str
    current_sha256: str
    matches: bool


def _rules_list(value: Any, category: str) -> tuple[ExeRule, ...]:
    if not isinstance(value, list) or not value:
        raise GateConfigError(f"exe rules category {category} must be a non-empty array")
    parsed: list[ExeRule] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) != {"pattern", "reason"}:
            raise GateConfigError(f"exe rules {category}[{index}] must contain pattern and reason")
        pattern = raw["pattern"]
        reason = raw["reason"]
        if not isinstance(pattern, str) or not pattern.strip():
            raise GateConfigError(f"exe rules {category}[{index}].pattern must be non-empty")
        if not isinstance(reason, str) or not reason.strip():
            raise GateConfigError(f"exe rules {category}[{index}].reason must be non-empty")
        parsed.append(ExeRule(pattern.replace("\\", "/"), reason.strip()))
    return tuple(parsed)


def load_exe_rules(path: Path) -> ExeRules:
    path = Path(path)
    if not path.is_file():
        raise GateConfigError(f"EXE rebuild rules do not exist: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateConfigError(f"EXE rebuild rules are not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "categories"}:
        raise GateConfigError("EXE rebuild rules require schema_version and categories")
    if raw["schema_version"] != 1:
        raise GateConfigError("EXE rebuild rules schema_version must be 1")
    categories = raw["categories"]
    if not isinstance(categories, dict) or set(categories) != set(CATEGORIES):
        raise GateConfigError(f"EXE rebuild rules categories must be: {', '.join(CATEGORIES)}")
    return ExeRules(
        schema_version=1,
        external_update=_rules_list(categories["external_update"], "external_update"),
        rebuild_required=_rules_list(categories["rebuild_required"], "rebuild_required"),
        manual_review=_rules_list(categories["manual_review"], "manual_review"),
    )


def _normalized_changed_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateConfigError("changed file path must be non-empty")
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or re.match(r"^[A-Za-z]:/", normalized) or ".." in path.parts:
        raise GateConfigError(f"changed file must be repository-relative: {value}")
    result = path.as_posix()
    while result.startswith("./"):
        result = result[2:]
    return result


def _first_match(path: str, rules: tuple[ExeRule, ...]) -> ExeRule | None:
    return next((rule for rule in rules if fnmatch.fnmatchcase(path, rule.pattern)), None)


def classify_changed_files(paths: Sequence[str], rules: ExeRules) -> ExeDecision:
    normalized = tuple(dict.fromkeys(_normalized_changed_path(path) for path in paths))
    matches: list[RuleMatch] = []
    unmatched: list[str] = []
    for path in normalized:
        selected: tuple[str, ExeRule] | None = None
        for category, category_rules in (
            ("rebuild_required", rules.rebuild_required),
            ("manual_review", rules.manual_review),
            ("external_update", rules.external_update),
        ):
            match = _first_match(path, category_rules)
            if match is not None:
                selected = (category, match)
                break
        if selected is None:
            unmatched.append(path)
            continue
        category, rule = selected
        matches.append(RuleMatch(path, category, rule.pattern, rule.reason))
    categories = {match.category for match in matches}
    if "rebuild_required" in categories:
        decision = "rebuild_required"
    elif "manual_review" in categories or unmatched:
        decision = "manual_review"
    else:
        decision = "reuse"
    reasons = [f"{match.path}: {match.reason} ({match.pattern})" for match in matches]
    reasons.extend(f"{path}: no EXE rebuild rule matched" for path in unmatched)
    if not reasons:
        reasons.append("no changed files were supplied")
    return ExeDecision(decision, normalized, tuple(matches), tuple(reasons))


def sha256_file(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise ExePolicyError(f"EXE does not exist: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ExePolicyError(f"could not read EXE for SHA-256: {path}: {exc}") from exc
    return digest.hexdigest()


def verify_reuse_hash(path: Path, baseline_sha256: str) -> ExeHashEvidence:
    if not isinstance(baseline_sha256, str) or not HEX_SHA256.fullmatch(baseline_sha256):
        raise ExePolicyError("baseline EXE SHA-256 must contain exactly 64 hexadecimal characters")
    baseline = baseline_sha256.lower()
    current = sha256_file(path)
    return ExeHashEvidence(str(Path(path).resolve()), baseline, current, baseline == current)
