from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for source_root in (REPO_ROOT, REPO_ROOT / "visualization_app"):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from visualization_app.helper_relay import HelperRegistry


def _journal_blockers(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(path) as connection:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM captures "
                    "WHERE final_status IS NULL OR save_state IN ('receiving','saving')"
                ).fetchone()[0]
            )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return ["journal_reconciliation_unavailable"]
    return [f"unfinished_capture_count:{count}"] if count else []


def _helper_state_blockers(path: Path) -> tuple[list[str], list[str]]:
    if not path.is_file():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("helper state root must be an object")
        captures = payload.get("active_captures", {})
        commands = payload.get("commands", {})
        if not isinstance(captures, dict) or not isinstance(commands, dict):
            raise ValueError("helper state collections are invalid")
        active_capture_uuids = sorted(
            str(capture_uuid)
            for capture_uuid, session_id in captures.items()
            if str(capture_uuid).strip() and str(session_id).strip()
        )
        blocking_request_ids = sorted(
            str(request_id)
            for request_id, item in commands.items()
            if isinstance(item, dict)
            and str(item.get("command") or "") in {"start_capture", "stop_capture"}
            and str(item.get("delivery_state") or "") not in {"completed", "terminal"}
        )
        return active_capture_uuids, blocking_request_ids
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return [], ["helper_state_reconciliation_unavailable"]


def _reserve_local_only(path: Path, ttl_seconds: int) -> dict[str, object]:
    now = time.time()
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        token_hash = str(existing.get("reservation_hash") or "")
        expires_at = float(existing.get("expires_at") or 0.0)
        if len(token_hash) != 64:
            raise ValueError("invalid reservation hash")
        if expires_at > now:
            return {
                "allowed": False,
                "reservation_active": True,
                "reservation_id": "",
                "error": "cutover_reservation_active",
            }
    except FileNotFoundError:
        pass
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "allowed": False,
            "reservation_active": True,
            "reservation_id": "",
            "error": "cutover_reservation_unavailable",
        }

    reservation_id = secrets.token_urlsafe(24)
    expires_at = now + max(30, min(int(ttl_seconds), 600))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "reservation_hash": hashlib.sha256(
                        reservation_id.encode("utf-8")
                    ).hexdigest(),
                    "expires_at": expires_at,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {
        "allowed": True,
        "reservation_active": True,
        "reservation_id": reservation_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=600)
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root).resolve()
    state_root = runtime_root / "runtime"
    journal_blockers = _journal_blockers(
        state_root / "server_target_capture_journal.sqlite3"
    )
    if args.local_only:
        active_captures, helper_blockers = _helper_state_blockers(
            state_root / "helper_registry.json"
        )
        blockers = sorted(set(helper_blockers).union(journal_blockers))
        readiness: dict[str, object] = {
            "allowed": not active_captures and not blockers,
            "generation": 0,
            "active_capture_uuids": active_captures,
            "blocking_request_ids": blockers,
            "reservation_active": False,
        }
        if readiness["allowed"]:
            readiness.update(
                _reserve_local_only(
                    state_root / "public-ingress-cutover.json",
                    args.ttl_seconds,
                )
            )
        print(json.dumps(readiness, ensure_ascii=False, separators=(",", ":")))
        return 0 if readiness.get("allowed") and readiness.get("reservation_id") else 2

    registry = HelperRegistry(
        persistence_path=state_root / "helper_registry.json",
        ingress_state_path=state_root / "public-ingress-state.json",
        expected_candidate_id=args.candidate_id,
    )
    readiness = registry.cutover_readiness()
    blockers = set(str(item) for item in readiness.get("blocking_request_ids", []))
    blockers.update(journal_blockers)
    readiness["blocking_request_ids"] = sorted(blockers)
    readiness["allowed"] = bool(
        readiness.get("allowed")
        and not readiness.get("active_capture_uuids")
        and not blockers
    )
    if readiness["allowed"]:
        readiness = registry.reserve_cutover(args.ttl_seconds)
    print(json.dumps(readiness, ensure_ascii=False, separators=(",", ":")))
    return 0 if readiness.get("allowed") and readiness.get("reservation_id") else 2


if __name__ == "__main__":
    raise SystemExit(main())
