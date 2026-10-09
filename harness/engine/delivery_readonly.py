from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .real_acquisition_preflight import DeliverySnapshot, capture_delivery_snapshot


_MANIFEST_LINE = re.compile(r"^([0-9A-Fa-f]{64}) [ *](.+)$")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def snapshot_tree_digest(snapshot: DeliverySnapshot) -> str:
    digest = hashlib.sha256()
    for relative in snapshot.files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(snapshot.sha256[relative].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _load_baseline(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"delivery baseline is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("delivery baseline schema_version must be 1")
    return payload


def _manifest_entries(path: Path) -> tuple[dict[str, str], tuple[str, ...]]:
    entries: dict[str, str] = {}
    issues: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        return {}, (f"per-file manifest unreadable: {exc}",)
    for line_number, line in enumerate(lines, 1):
        match = _MANIFEST_LINE.fullmatch(line.strip())
        if not match:
            issues.append(f"per-file manifest line {line_number} is invalid")
            continue
        digest, raw_relative = match.groups()
        relative = raw_relative.replace("\\", "/")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:/", relative):
            issues.append(f"per-file manifest path is unsafe: {relative}")
            continue
        if relative in entries:
            issues.append(f"per-file manifest path is duplicated: {relative}")
            continue
        entries[relative] = digest.lower()
    return entries, tuple(issues)


def verify_delivery_baseline(
    delivery_root: Path,
    baseline_path: Path,
) -> tuple[str, ...]:
    baseline = _load_baseline(baseline_path)
    snapshot = capture_delivery_snapshot(delivery_root)
    issues: list[str] = []
    expected_count = baseline.get("managed_file_count")
    if len(snapshot.files) != expected_count:
        issues.append(
            f"managed_file_count mismatch: expected {expected_count}, got {len(snapshot.files)}"
        )
    expected_tree = str(baseline.get("tree_sha256", "")).lower()
    actual_tree = snapshot_tree_digest(snapshot)
    if actual_tree != expected_tree:
        issues.append(f"tree_sha256 mismatch: expected {expected_tree}, got {actual_tree}")

    manifest_relative = str(baseline.get("per_file_manifest", ""))
    manifest_path = Path(delivery_root).resolve() / manifest_relative
    expected_manifest_digest = str(
        baseline.get("per_file_manifest_sha256", "")
    ).lower()
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        issues.append(f"per-file manifest unreadable: {exc}")
        return tuple(issues)
    actual_manifest_digest = _sha256_bytes(manifest_bytes)
    if actual_manifest_digest != expected_manifest_digest:
        issues.append(
            "per-file manifest file hash mismatch: "
            f"expected {expected_manifest_digest}, got {actual_manifest_digest}"
        )
    entries, manifest_issues = _manifest_entries(manifest_path)
    issues.extend(manifest_issues)
    expected_entries = baseline.get("per_file_manifest_entries")
    if len(entries) != expected_entries:
        issues.append(
            f"per-file manifest entry count mismatch: expected {expected_entries}, got {len(entries)}"
        )
    for relative, expected in entries.items():
        actual = snapshot.sha256.get(relative)
        if actual is None:
            issues.append(f"manifest file missing from delivery: {relative}")
        elif actual != expected:
            issues.append(
                f"manifest digest mismatch: {relative}; expected {expected}, got {actual}"
            )
    manifest_members = set(entries) | {manifest_relative}
    for relative in sorted(set(snapshot.files) - manifest_members):
        issues.append(f"managed file missing from per-file manifest: {relative}")
    return tuple(issues)


def main(argv: Sequence[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[2]
    default_baseline = repo / "harness" / "config" / "delivery-readonly-baseline.json"
    parser = argparse.ArgumentParser(description="Verify the formal delivery is unchanged")
    parser.add_argument("--baseline", type=Path, default=default_baseline)
    parser.add_argument("--delivery", type=Path)
    args = parser.parse_args(argv)
    try:
        baseline = _load_baseline(args.baseline)
        delivery = args.delivery or repo / str(baseline.get("delivery_path", ""))
        issues = verify_delivery_baseline(delivery, args.baseline)
    except (OSError, ValueError) as exc:
        print(f"delivery-readonly: {exc}")
        return 2
    if issues:
        for issue in issues:
            print(f"delivery-readonly: {issue}")
        return 1
    print(f"delivery-readonly: passed ({baseline['managed_file_count']} managed files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
