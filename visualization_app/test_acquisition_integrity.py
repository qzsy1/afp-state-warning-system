from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acquisition import (  # noqa: E402
    AcquisitionConfig,
    AcquisitionManager,
    MySQLSettings,
    NEW_COLLECTION_SENSOR_COLUMNS,
)
from training_data import read_excel_or_folder  # noqa: E402


class AcquisitionIntegrityTests(unittest.TestCase):
    def _source(self, root: Path, rows: int = 200) -> Path:
        path = root / "simulation.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=NEW_COLLECTION_SENSOR_COLUMNS
            )
            writer.writeheader()
            for index in range(rows):
                writer.writerow(
                    {
                        name: 20.0 + channel + index * 0.01
                        for channel, name in enumerate(
                            NEW_COLLECTION_SENSOR_COLUMNS
                        )
                    }
                )
        return path

    def _capture(
        self, root: Path, source: Path, layer: int, target_rows: int = 20
    ) -> dict:
        manager = AcquisitionManager(root / "unused")
        config = AcquisitionConfig(
            processing_mode="capture_only",
            dataset_schema="new_collection_v11_3",
            driver="simulator",
            source_file=str(source),
            simulation_source_path=str(source),
            selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
            sample_rate_hz=1000.0,
            save_root=str(root / "capture"),
            condition_id="H06",
            replicate=1,
            layer=layer,
        )
        manager.start(config)
        deadline = time.time() + 5.0
        while manager.status()["sample_count"] < target_rows and time.time() < deadline:
            time.sleep(0.01)
        return manager.stop()

    def test_layers_share_uuid_and_files_are_finalized_with_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            first = self._capture(root, source, 0)
            second = self._capture(root, source, 1)

            self.assertTrue(first["capture_uuid"])
            self.assertEqual(first["capture_uuid"], second["capture_uuid"])
            self.assertEqual(second["completed_layers"], [1, 2])
            self.assertTrue(Path(second["raw_file"]).is_file())
            self.assertFalse(
                Path(str(second["raw_file"]) + ".partial").exists()
            )
            summaries = sorted(
                Path(second["session_dir"]).joinpath("采集记录").glob(
                    "*_采集摘要.json"
                )
            )
            summary = json.loads(summaries[-1].read_text(encoding="utf-8"))
            self.assertEqual(summary["capture_uuid"], second["capture_uuid"])
            self.assertEqual(summary["sample_count"], second["sample_count"])
            self.assertEqual(
                len(summary["file_integrity"]["layer_file"]["sha256"]), 64
            )
            self.assertGreater(
                summary["data_quality"]["effective_sample_rate_hz"], 0
            )

    def test_new_layer_one_archives_previous_specimen_without_mixing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            previous = self._capture(root, source, 0)
            previous_uuid = previous["capture_uuid"]
            current = self._capture(root, source, 0)

            self.assertNotEqual(previous_uuid, current["capture_uuid"])
            self.assertEqual(current["completed_layers"], [1])
            archive = Path(current["archived_previous_session"])
            self.assertTrue(archive.is_dir())
            self.assertTrue(any(archive.glob("*_第1层.CSV")))
            with Path(current["raw_file"]).open(
                "r", encoding="gb18030", newline=""
            ) as handle:
                active_rows = list(csv.DictReader(handle))
            self.assertEqual(
                {row["specimen_id"] for row in active_rows},
                {current["capture_uuid"]},
            )

    def test_public_manifest_masks_both_mysql_passwords(self) -> None:
        config = AcquisitionConfig(
            mysql_password="secret",
            simulation_mysql_password="other-secret",
        )
        public = AcquisitionManager._public_config(config)
        self.assertEqual(public["mysql_password"], "***")
        self.assertEqual(public["simulation_mysql_password"], "***")

    def test_training_import_ignores_archived_specimen_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active = self._source(root, rows=5)
            history = root / "历史版本" / "旧试样"
            history.mkdir(parents=True)
            archived = history / active.name
            archived.write_bytes(active.read_bytes())
            imported = read_excel_or_folder(root)
            self.assertEqual(imported.source_files, [str(active)])
            self.assertEqual(len(imported.frame), 5)

    def test_pending_mysql_record_is_retried_and_marked_synced(self) -> None:
        class FakeStore:
            def __init__(self, settings):
                self.settings = settings

            def test_connection(self):
                return {"ok": True}

            def save_layer(self, config, **kwargs):
                return {
                    "ok": True,
                    "saved_rows": len(kwargs["rows"]),
                    "specimen_key": config.capture_uuid,
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, rows=5)
            pending = root / "x_mysql_pending.json"
            config = AcquisitionConfig(
                dataset_schema="new_collection_v11_3",
                processing_mode="capture_only",
                driver="simulator",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                capture_uuid="AFP-test",
                mysql_enabled=True,
                mysql_database="afp_test",
            )
            pending.write_text(
                json.dumps(
                    {
                        "config": AcquisitionManager._public_config(config),
                        "layer_file": str(source),
                        "folder_path": str(root),
                        "summary": {"completed_layers": [1]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manager = AcquisitionManager(root / "capture")
            settings = MySQLSettings(
                enabled=True, database="afp_test", password="runtime-secret"
            )
            with patch("acquisition.MySQLCaptureStore", FakeStore):
                result = manager._retry_pending_mysql(settings, root)
            self.assertEqual(result["succeeded"], 1, result)
            self.assertFalse(pending.exists())
            self.assertTrue((root / "x_mysql_synced.json").is_file())


if __name__ == "__main__":
    unittest.main()
