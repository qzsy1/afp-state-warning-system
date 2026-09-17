from __future__ import annotations

import unittest

from edge_capture import RemoteAcquisitionMirror, RemoteAcquisitionRegistry


def batch(capture_uuid: str, sequence: int, values: list[float]) -> dict:
    return {
        "capture_uuid": capture_uuid,
        "sequence": sequence,
        "rows": [{"温度": value, "压力": value + 1.0} for value in values],
        "timestamps": [1000.0 + sequence * 10 + index for index in range(len(values))],
        "status": {
            "running": True,
            "config": {
                "acquisition_mode": "real",
                "dataset_schema": "legacy_original",
                "selected_sensors": ["温度", "压力"],
            },
        },
    }


class RemoteAcquisitionMirrorTests(unittest.TestCase):
    def test_first_batch_populates_acquisition_compatible_surface(self):
        mirror = RemoteAcquisitionMirror(max_rows=10)

        ack = mirror.ingest(batch("capture-a", 0, [350.0, 351.0]))

        self.assertTrue(ack["ok"])
        self.assertEqual(ack["ack_sequence"], 0)
        rows, timestamps = mirror.numeric_matrix()
        self.assertEqual([row["温度"] for row in rows], [350.0, 351.0])
        self.assertEqual(timestamps, [1000.0, 1001.0])
        status = mirror.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["capture_uuid"], "capture-a")
        self.assertEqual(status["remote_received_rows"], 2)

    def test_duplicate_is_acknowledged_without_adding_rows_twice(self):
        mirror = RemoteAcquisitionMirror()
        payload = batch("capture-a", 0, [350.0])
        mirror.ingest(payload)

        duplicate = mirror.ingest(payload)

        self.assertTrue(duplicate["ok"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(mirror.numeric_matrix()[0]), 1)

    def test_out_of_order_batch_is_rejected_with_expected_sequence(self):
        mirror = RemoteAcquisitionMirror()
        mirror.ingest(batch("capture-a", 0, [350.0]))

        rejected = mirror.ingest(batch("capture-a", 2, [352.0]))

        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"], "sample_sequence_gap")
        self.assertEqual(rejected["expected_sequence"], 1)
        self.assertEqual(len(mirror.numeric_matrix()[0]), 1)

    def test_new_capture_sequence_zero_replaces_previous_rows(self):
        mirror = RemoteAcquisitionMirror()
        mirror.ingest(batch("capture-a", 0, [350.0, 351.0]))

        mirror.ingest(batch("capture-b", 0, [410.0]))

        self.assertEqual(mirror.status()["capture_uuid"], "capture-b")
        self.assertEqual(mirror.numeric_matrix()[0], [{"温度": 410.0, "压力": 411.0}])

    def test_registry_keeps_sessions_isolated(self):
        registry = RemoteAcquisitionRegistry()
        registry.ingest("session-a", batch("capture-a", 0, [350.0]))
        registry.ingest("session-b", batch("capture-b", 0, [450.0]))

        self.assertEqual(registry.for_session("session-a").numeric_matrix()[0][0]["温度"], 350.0)
        self.assertEqual(registry.for_session("session-b").numeric_matrix()[0][0]["温度"], 450.0)


if __name__ == "__main__":
    unittest.main()
