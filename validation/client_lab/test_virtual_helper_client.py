from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from validation.client_lab.virtual_helper_client import (
    OneInFlightPump,
    simulate_transport,
    write_report,
)


class VirtualHelperClientTests(unittest.TestCase):
    def test_virtual_time_transport_covers_30000_rows_and_idempotent_reconnect(self) -> None:
        """Catches backlog, duplicate acceptance, or more than one in-flight batch."""
        report = simulate_transport(
            rate_hz=50.0,
            duration_seconds=600,
            disconnect_at_seconds=300,
            disconnect_seconds=10,
        )

        self.assertEqual(report["produced_rows"], 30_000)
        self.assertEqual(report["accepted_rows"], 30_000)
        self.assertEqual(report["missing_rows"], 0)
        self.assertEqual(report["duplicate_rows"], 0)
        self.assertEqual(report["max_in_flight_batches"], 1)
        self.assertEqual(report["disconnect_seconds"], 10)
        self.assertGreaterEqual(report["replayed_batches"], 1)
        self.assertLessEqual(report["p95_seconds"], 1.0)
        self.assertNotIn("pairing_token", json.dumps(report))
        self.assertNotIn("rows", report)

    def test_pump_replays_same_batch_until_matching_ack(self) -> None:
        """Catches sequence advancement on disconnect or a mismatched ACK."""
        pump = OneInFlightPump(capture_uuid="capture-test", max_batch_rows=25)
        pump.produce(25, generated_at=1.0)
        first = pump.next_batch(now=1.1)

        self.assertIsNotNone(first)
        self.assertIsNone(pump.next_batch(now=1.2))
        self.assertFalse(pump.acknowledge("capture-test", 99, now=1.3))
        pump.connection_lost()
        replay = pump.next_batch(now=2.0)

        self.assertEqual(replay, first)
        self.assertTrue(pump.acknowledge("capture-test", 0, now=2.1))
        self.assertEqual(pump.accepted_rows, 25)

    def test_report_writer_excludes_tokens_and_sample_payloads(self) -> None:
        """Catches persistence of one-time credentials or raw samples."""
        report = simulate_transport(
            rate_hz=10.0,
            duration_seconds=2,
            disconnect_at_seconds=1,
            disconnect_seconds=1,
        )
        report["pairing_token"] = "must-not-be-written"
        report["samples"] = [{"温度": 320.0}]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "helper-transport.json"
            write_report(path, report)
            raw = path.read_text(encoding="utf-8")
            stored = json.loads(raw)

        self.assertNotIn("must-not-be-written", raw)
        self.assertNotIn("pairing_token", stored)
        self.assertNotIn("samples", stored)
        self.assertEqual(stored["accepted_rows"], 20)


if __name__ == "__main__":
    unittest.main()
