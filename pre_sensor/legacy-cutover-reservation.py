from __future__ import annotations

import argparse
import json
import sqlite3
import sys
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=600)
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root).resolve()
    state_root = runtime_root / "runtime"
    registry = HelperRegistry(
        persistence_path=state_root / "helper_registry.json",
        ingress_state_path=state_root / "public-ingress-state.json",
        expected_candidate_id=args.candidate_id,
    )
    readiness = registry.cutover_readiness()
    blockers = set(str(item) for item in readiness.get("blocking_request_ids", []))
    blockers.update(_journal_blockers(state_root / "server_target_capture_journal.sqlite3"))
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
