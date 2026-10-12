"""Fail-closed evidence sanitization and real-device checklist validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|token|secret|credential|api[_-]?key|authorization|cookie)"
)
_ENV_SECRET = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]*(?:PASSWORD|PASSWD|TOKEN|SECRET|CREDENTIAL|API_KEY)[A-Z0-9_]*)\s*=\s*([^\s,;]+)"
)
_WINDOWS_USER_PATH = re.compile(r"(?i)\b([A-Z]:\\Users\\)[^\\\s]+")
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

PHYSICAL_CHECKLIST_IDS = frozenset(
    {
        "physical.identity_ports",
        "physical.channel_integrity",
        "physical.units_ranges_encoding_direction",
        "physical.calibration_zero_noise_drift_crosstalk",
        "physical.all_devices_10hz",
        "physical.usb_power_bandwidth_emi",
        "physical.unplug_powercycle_restart",
        "physical.driver_close",
        "physical.near_production_endurance",
    }
)


def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[REDACTED_URL]"
    hostname = parsed.hostname or ""
    if not hostname:
        return "[REDACTED_URL]"
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = f":{parsed.port}" if parsed.port else ""
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        query.append((key, "[REDACTED]" if _SENSITIVE_KEY.search(key) else item))
    return urlunsplit((parsed.scheme, host + port, parsed.path, urlencode(query), ""))


def _sanitize_string(value: str) -> str:
    value = _ENV_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    value = _WINDOWS_USER_PATH.sub(lambda match: match.group(1) + "[REDACTED]", value)
    value = _URL.sub(lambda match: _sanitize_url(match.group(0)), value)
    return value


def sanitize_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(str(key))
                else sanitize_evidence(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_evidence(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_evidence(item) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value)
    return value


def secret_values_from_environment() -> tuple[str, ...]:
    return tuple(
        value
        for key, value in os.environ.items()
        if value and _SENSITIVE_KEY.search(key)
    )


def _contains_secret(payload: bytes | str, secrets: Iterable[str]) -> str | None:
    text = payload.decode("utf-8", errors="ignore") if isinstance(payload, bytes) else payload
    for secret in secrets:
        if secret and secret in text:
            return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]
    return None


def export_sanitized_evidence(
    sources: Iterable[str | Path],
    output_directory: str | Path,
    *,
    secret_values: Iterable[str] = (),
) -> dict[str, Any]:
    source_paths = [Path(source).resolve() for source in sources]
    if not source_paths:
        raise ValueError("at least one evidence source is required")
    secrets = tuple(dict.fromkeys(str(item) for item in secret_values if str(item)))
    documents: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    text_suffixes = {".json", ".md", ".txt", ".log", ".csv"}
    for path in source_paths:
        if not path.is_file():
            raise ValueError(f"evidence source is not a file: {path.name}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        entry: dict[str, Any] = {
            "name": path.name,
            "sha256": digest,
            "bytes": len(raw),
        }
        if path.suffix.lower() not in text_suffixes:
            if _contains_secret(raw, secrets):
                raise ValueError(f"binary evidence cannot be safely redacted: {path.name}")
            entry["content_exported"] = False
            manifest.append(entry)
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"text evidence cannot be safely redacted: {path.name}") from exc
        if path.suffix.lower() == ".json":
            try:
                content = sanitize_evidence(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON evidence is invalid: {path.name}") from exc
        else:
            content = _sanitize_string(text)
        serialized = json.dumps(content, ensure_ascii=False, sort_keys=True)
        if _contains_secret(serialized, secrets):
            raise ValueError(f"evidence cannot be safely redacted: {path.name}")
        entry["content_exported"] = True
        documents.append({"name": path.name, "content": content})
        manifest.append(entry)

    payload = {
        "schema_version": 1,
        "sanitized": True,
        "source_count": len(source_paths),
        "manifest": manifest,
        "documents": documents,
    }
    serialized_payload = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if _contains_secret(serialized_payload, secrets):
        raise ValueError("export cannot be safely redacted")
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / "evidence.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(serialized_payload, encoding="utf-8")
    temporary.replace(target)
    result = {
        "ok": True,
        "files": len(source_paths),
        "text_documents": len(documents),
        "manifest": manifest,
        "output": target.name,
    }
    return result


def validate_physical_checklist(payload: dict[str, Any]) -> list[dict[str, str]]:
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("physical checklist schema_version must be 1")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("physical checklist items are required")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("physical checklist item must be an object")
        item_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if item_id not in PHYSICAL_CHECKLIST_IDS:
            raise ValueError(f"not a physical-device item: {item_id}")
        if item_id in seen or not title:
            raise ValueError(f"invalid or duplicate physical checklist item: {item_id}")
        seen.add(item_id)
        normalized.append({"id": item_id, "title": title})
    if seen != PHYSICAL_CHECKLIST_IDS:
        raise ValueError("physical checklist must contain the complete device-only whitelist")
    return normalized
