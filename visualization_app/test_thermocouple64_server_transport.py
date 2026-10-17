from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import acquisition  # noqa: E402
from edge_capture import RemoteAcquisitionRegistry  # noqa: E402
from real_acquisition import ChannelQuality, ChannelSample, UnifiedFrame  # noqa: E402
from server_capture_journal import ServerCaptureJournal  # noqa: E402
from thermocouple64 import THERMOCOUPLE64_MODE_ID, raw_catalog  # noqa: E402


class Thermocouple64ServerTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.journal = ServerCaptureJournal(Path(self.temp.name) / "journal.sqlite3")
        self.mirrors = RemoteAcquisitionRegistry(max_rows=24)
        self.channels = list(acquisition.CURRENT_COLLECTION_SENSOR_COLUMNS)
        self.units = {
            name: str((acquisition.SENSOR_CHANNEL_METADATA.get(name) or {}).get("unit") or "")
            for name in self.channels
        }
        self.catalog = raw_catalog(self.channels, self.units)
        self.journal.arm(
            "session-a", "selection-a",
            config={"selected_sensors": self.channels, "dataset_schema": THERMOCOUPLE64_MODE_ID},
        )

    def tearDown(self) -> None:
        self.journal.close()
        self.temp.cleanup()

    def _batch(self, sequence: int, *, catalog: dict | None = None) -> dict:
        timestamp = 100.0 + sequence
        values = {name: float(index) for index, name in enumerate(self.channels, start=1)}
        frame = UnifiedFrame(
            capture_sequence=sequence,
            target_monotonic=10.0 + sequence,
            target_wall_time=timestamp,
            assembled_monotonic=10.01 + sequence,
            channels={
                name: ChannelSample(
                    interface_id="fixture", channel_name=name, value=value,
                    quality=ChannelQuality.MEASURED_NEW,
                    received_wall_time=timestamp, received_monotonic=10.0 + sequence,
                ) for name, value in values.items()
            },
        )
        frame_catalog = catalog or self.catalog
        return {
            "capture_uuid": "capture-a",
            "sequence": sequence,
            "required_frame_contract": "unified_frame_v2",
            "frame_catalog": frame_catalog,
            "frames": [frame.to_envelope_v2(
                session_id="capture-a", mode_id=THERMOCOUPLE64_MODE_ID,
                catalog=frame_catalog,
            )],
            "rows": [values],
            "timestamps": [timestamp],
            "status": {
                "running": False, "finalization_complete": True,
                "sample_count": sequence + 1,
                "config": {"selected_sensors": self.channels,
                           "dataset_schema": THERMOCOUPLE64_MODE_ID},
            },
        }

    def test_journal_persists_and_restores_v2_batch_idempotently(self) -> None:
        batch = self._batch(0)
        accepted = self.journal.ingest("session-a", batch, self.mirrors)
        self.assertTrue(accepted["ok"], accepted)
        record = self.journal.ready_capture("session-a", "capture-a")
        self.assertEqual(record.row_count, 1)
        self.assertEqual(list(record.rows)[0]["温度64"], float(self.channels.index("温度64") + 1))

        restarted = RemoteAcquisitionRegistry(max_rows=24)
        duplicate = self.journal.ingest("session-a", batch, restarted)
        self.assertTrue(duplicate["ok"], duplicate)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(
            restarted.for_session("session-a").numeric_matrix()[0][0]["温度64"],
            float(self.channels.index("温度64") + 1),
        )

    def test_unknown_catalog_is_rejected_without_committing(self) -> None:
        invalid_catalog = {**self.catalog, "catalog_id": "unknown"}
        rejected = self.journal.ingest(
            "session-a", self._batch(0, catalog=invalid_catalog), self.mirrors
        )
        self.assertFalse(rejected["ok"])
        self.assertIsNone(self.journal.ready_capture("session-a", "capture-a"))
        self.assertEqual(self.mirrors.for_session("session-a").numeric_matrix()[0], [])


if __name__ == "__main__":
    unittest.main()
