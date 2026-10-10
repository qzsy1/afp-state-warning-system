from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "visualization_app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


class HelperProtocolDrillTests(unittest.TestCase):
    def test_protocol_fault_sequence_reconciles_without_claiming_two_machine(self) -> None:
        from pre_sensor_drill import run_helper_protocol_drill

        with tempfile.TemporaryDirectory() as temporary:
            report = run_helper_protocol_drill(
                Path(temporary), sample_rate_hz=10.0, frame_count=30
            )

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["evidence_tier"], "protocol_simulation")
        self.assertFalse(report["two_machine_verified"])
        self.assertEqual(report["target_frames"], 30)
        self.assertEqual(report["reconciled_frames"], 30)
        self.assertEqual(report["absolute_sequence"], [0, 29])
        self.assertEqual(report["effective_hz"], 10.0)
        self.assertTrue(report["faults"]["gap_rejected"])
        self.assertTrue(report["faults"]["duplicate_idempotent"])
        self.assertTrue(report["faults"]["disconnect_replayed"])
        self.assertTrue(report["faults"]["helper_restart_replayed"])
        self.assertTrue(report["faults"]["server_restart_restored"])
        self.assertTrue(report["faults"]["repeated_command_idempotent"])
        self.assertEqual(report["journal_state"], "ready")

    def test_invalid_drill_parameters_fail_closed(self) -> None:
        from pre_sensor_drill import run_helper_protocol_drill

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                run_helper_protocol_drill(Path(temporary), sample_rate_hz=0, frame_count=10)
            with self.assertRaises(ValueError):
                run_helper_protocol_drill(Path(temporary), sample_rate_hz=10, frame_count=1)

    def test_report_writer_emits_machine_and_human_readable_evidence(self) -> None:
        from pre_sensor_drill import (
            run_helper_protocol_drill,
            write_helper_protocol_drill_report,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = run_helper_protocol_drill(root / "runtime", frame_count=10)
            json_path, markdown_path = write_helper_protocol_drill_report(
                report, root / "evidence"
            )
            self.assertEqual(json_path.name, "helper-protocol-drill.json")
            self.assertTrue(markdown_path.is_file())
            self.assertEqual(
                __import__("json").loads(json_path.read_text(encoding="utf-8"))["ok"],
                True,
            )
            self.assertIn("protocol_simulation", markdown_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
