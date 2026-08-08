"""Reproduce the high-performing TC-HI experiment with the archived forecasts.

The archived ``results/3`` directory contains the I-ModernTCN predictions used
by the original TC-HI result.  Its inputs and ground truth are byte-identical
to the current experiment; only the forecast arrays differ.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from run_physics_guided_health_indicator_v13 import (
    CONTACT_FORCE_THRESHOLD_N,
    CONTACT_MIN_CONSECUTIVE_POINTS,
    SPECIMEN_MIN_COMPACTION_EVENT_WINDOWS,
    run,
)


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = SCRIPT_DIR.parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
ARCHIVED_RESULT_DIR = PROJECT_ROOT / "results" / "3"
DEFAULT_OUTPUT = SCRIPT_DIR / "outputs_tc_hi_old_reproduction_v13_5"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reproduce TC-HI | Random Forest using archived I-ModernTCN predictions"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--seed-repeats", type=int, default=5)
    parser.add_argument("--no-plots", action="store_true")
    options = parser.parse_args()

    if not ARCHIVED_RESULT_DIR.is_dir():
        raise FileNotFoundError(
            f"Archived prediction directory is missing: {ARCHIVED_RESULT_DIR}"
        )

    args = argparse.Namespace(
        project_root=PROJECT_ROOT,
        result_dir=ARCHIVED_RESULT_DIR,
        train_csv=WORKSPACE / "health_split_v3_accuracy" / "train_normal.csv",
        manifest_csv=WORKSPACE / "health_split_v3_accuracy" / "split_manifest.csv",
        output=options.output,
        seed=options.seed,
        seed_repeats=options.seed_repeats,
        stride=24,
        contact_force_threshold_n=CONTACT_FORCE_THRESHOLD_N,
        contact_min_consecutive_points=CONTACT_MIN_CONSECUTIVE_POINTS,
        specimen_min_event_windows=SPECIMEN_MIN_COMPACTION_EVENT_WINDOWS,
        no_plots=options.no_plots,
    )
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
