from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "visualization_app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from acquisition import (
    AcquisitionManager,
    _resolve_pending_layer_file,
    scan_persistence_recovery,
)
from visualization_app.pre_sensor_preflight import probe_persistence_recovery


class PersistenceRestartRecoveryTests(unittest.TestCase):
    def test_moved_capture_root_resolves_layer_from_matching_session_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            specimen = root / "specimen"
            records = specimen / "采集记录"
            archive = (
                specimen
                / "历史版本"
                / "试样会话_20261001_092655_capture-moved"
            )
            records.mkdir(parents=True)
            archive.mkdir(parents=True)
            layer = archive / "layer.csv"
            layer.write_text("capture_uuid,value\ncapture-moved,1\n", encoding="utf-8")
            pending = records / "layer_mysql_target_pending.json"
            payload = {
                "config": {"capture_uuid": "capture-moved"},
                "layer_file": r"F:\old-root\specimen\layer.csv",
            }
            pending.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(
                _resolve_pending_layer_file(pending, payload, root),
                layer,
            )
            recovery = scan_persistence_recovery(root)
            self.assertEqual(recovery["missing_retry_sources"], 0)
            self.assertEqual(recovery["relocated_retry_sources"], 1)

    def test_restart_discovers_database_retry_and_blocks_next_real_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "condition" / "specimen" / "采集记录"
            capture.mkdir(parents=True)
            pending = capture / "layer_mysql_target_pending.json"
            pending.write_text(
                json.dumps(
                    {
                        "config": {"capture_uuid": "capture-restart"},
                        "layer_file": "layer.csv",
                    }
                ),
                encoding="utf-8",
            )

            recovery = scan_persistence_recovery(root)
            self.assertTrue(recovery["blocked"])
            self.assertEqual(recovery["database_pending_retry"], 1)
            self.assertEqual(recovery["capture_uuids"], ["capture-restart"])
            self.assertNotIn(str(root), json.dumps(recovery, ensure_ascii=False))

            manager = AcquisitionManager(root)
            status = manager.status()
            self.assertEqual(status["capture_phase"], "database_pending_retry")
            self.assertFalse(status["finalization_complete"])
            with self.assertRaisesRegex(RuntimeError, "database_pending_retry"):
                manager._start_new(SimpleNamespace(acquisition_mode="real"))

            pending.unlink()
            restarted = AcquisitionManager(root)
            self.assertEqual(restarted.status()["capture_phase"], "idle")

    def test_partial_files_are_restart_recovery_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "capture" / "layer.csv.partial"
            partial.parent.mkdir()
            partial.write_bytes(b"unfinished")
            recovery = scan_persistence_recovery(root)
            self.assertTrue(recovery["blocked"])
            self.assertEqual(recovery["incomplete_files"], 1)
            manager = AcquisitionManager(root)
            self.assertEqual(manager.status()["capture_phase"], "finalization_failed")

    def test_preflight_reports_pending_retry_before_formal_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / "capture_mysql_target_pending.json"
            pending.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "database_pending_retry"):
                probe_persistence_recovery({"save_path": str(root)})
            pending.unlink()
            outcome = probe_persistence_recovery({"save_path": str(root)})
            self.assertEqual(outcome.status, "passed")


if __name__ == "__main__":
    unittest.main()
