from __future__ import annotations

import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "visualization_app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


class PreSensorEnduranceTests(unittest.TestCase):
    def test_default_plan_requires_real_wall_clock_thirty_minutes_per_mode(self) -> None:
        from pre_sensor_endurance import EndurancePlan

        plan = EndurancePlan()
        self.assertEqual(plan.duration_seconds, 1800.0)
        self.assertEqual(plan.sample_rate_hz, 10.0)
        self.assertEqual(plan.modes, ("local_direct", "remote_helper"))
        self.assertEqual(plan.evidence_tier(), "endurance_30m")
        self.assertEqual(
            EndurancePlan(duration_seconds=1.0).evidence_tier(), "short_test"
        )

    def test_short_runs_use_production_core_and_reconcile_both_topologies(self) -> None:
        from pre_sensor_endurance import EndurancePlan, run_endurance_mode

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = [
                run_endurance_mode(
                    root / mode,
                    mode=mode,
                    plan=EndurancePlan(duration_seconds=0.8, modes=(mode,)),
                )
                for mode in ("local_direct", "remote_helper")
            ]
        for report in reports:
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["evidence_tier"], "short_test")
            self.assertGreaterEqual(report["actual_frames"], 5)
            self.assertEqual(report["actual_frames"], report["reconciliation"]["csv_rows"])
            self.assertEqual(report["actual_frames"], report["reconciliation"]["quality_rows"])
            self.assertTrue(report["reconciliation"]["absolute_sequence_contiguous"])
            self.assertTrue(report["reconciliation"]["timestamps_monotonic"])
            self.assertTrue(report["faults"]["normal_stop_idempotent"])
        remote = reports[1]
        self.assertEqual(remote["actual_frames"], remote["reconciliation"]["journal_rows"])
        self.assertTrue(remote["faults"]["network_disconnect_replayed"])
        self.assertTrue(remote["faults"]["helper_restart_replayed"])

    def test_reconciliation_fails_closed_on_any_artifact_mismatch(self) -> None:
        from pre_sensor_endurance import reconcile_endurance_counts

        baseline = {
            "expected_frames": 10,
            "csv_rows": 10,
            "quality_rows": 10,
            "journal_rows": 10,
            "absolute_sequence": [0, 9],
            "timestamps_monotonic": True,
            "channel_quality_complete": True,
            "final_state": "complete",
        }
        self.assertTrue(reconcile_endurance_counts(baseline)["ok"])
        for field in ("csv_rows", "quality_rows", "journal_rows"):
            changed = deepcopy(baseline)
            changed[field] -= 1
            result = reconcile_endurance_counts(changed)
            self.assertFalse(result["ok"], field)
            self.assertIn(field, result["differences"])

    def test_boundary_fault_drill_is_bounded_and_retryable(self) -> None:
        from pre_sensor_endurance import run_boundary_fault_drill

        with tempfile.TemporaryDirectory() as temporary:
            result = run_boundary_fault_drill(Path(temporary))
        self.assertTrue(result["ok"], result)
        for field in (
            "slow_disk_backpressure",
            "queue_overflow_fail_closed",
            "stop_timeout_explicit",
            "capacity_warning_observed",
            "capacity_hard_limit_rejected",
            "mysql_disconnect_spooled",
            "stop_while_database_unavailable_retryable",
            "mysql_recovery_reconciled",
        ):
            self.assertTrue(result["faults"][field], field)
        self.assertLessEqual(result["peaks"]["writer_queue_rows"], 2)

    def test_candidate_exposes_one_click_endurance_entrypoint(self) -> None:
        assembly = (ROOT / "modular_runtime" / "assemble_modular_delivery.ps1").read_text(
            encoding="utf-8-sig"
        )
        bootstrap = (ROOT / "modular_runtime" / "app" / "bootstrap.py").read_text(
            encoding="utf-8"
        )
        wrapper = (ROOT / "pre_sensor" / "run-endurance.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("pre_sensor_endurance.py", assembly)
        self.assertIn("run-pre-sensor-endurance.ps1", assembly)
        self.assertIn("--pre-sensor-endurance", bootstrap)
        self.assertIn("default=1800.0", bootstrap)
        self.assertIn("--pre-sensor-endurance", wrapper)
        self.assertIn("Start-Process", wrapper)
        self.assertIn(".ExitCode", wrapper)


if __name__ == "__main__":
    unittest.main()
