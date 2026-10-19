from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import industrial_acquisition_probe as probe


class ProbeTests(unittest.TestCase):
    def test_plc_only_probe_never_opens_or_checks_the_powered_off_abb(self):
        with patch.object(probe, "_probe_interface", side_effect=lambda item, *_: item["role"]) as read:
            result = probe.probe_round("ethernet:以太网", duration_seconds=1, roles=("plc",))
        self.assertEqual(result, {"plc": "plc"})
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args[0]["role"], "plc")

    def test_failed_abb_path_does_not_skip_real_plc_reads(self):
        def route(item):
            return {
                "ok": item["role"] == "plc", "actual_source_address": "192.168.125.209",
                "status": "network_path_ready" if item["role"] == "plc" else "network_path_invalid",
                "target_address": "192.168.125.5" if item["role"] == "plc" else "192.168.125.1",
            }

        class Plc:
            actual_source_address = "192.168.125.209"
            def __init__(self, *args, **kwargs): pass
            def open(self): pass
            def close(self): pass
            def read_sample(self): return {"温度": 100.12, "压力": 0.31, "张力": 0.0}

        with patch.object(probe.a, "validate_network_path", side_effect=route), patch.object(
            probe.a, "ModbusTcpDriver", Plc
        ), patch.object(probe.a, "AbbRobotDriver") as abb:
            results = probe.probe_round("ethernet:以太网", duration_seconds=0.25)
        abb.assert_not_called()
        self.assertTrue(results["plc"]["read_only_probe_passed"])
        self.assertGreaterEqual(results["plc"]["channels"]["温度"]["finite_samples"], 2)
        self.assertEqual(results["robot"]["status"], "network_path_invalid")

    def test_nonfinite_and_missing_samples_are_preserved_as_failures(self):
        result = probe.summarize_samples(
            [{"elapsed_seconds": 0.1, "values": {"温度": 10.0}},
             {"elapsed_seconds": 0.2, "values": {"温度": float("nan")}},
             {"elapsed_seconds": 0.3, "values": {}}], ["温度"], 0.4,
        )
        self.assertEqual(result["温度"]["finite_samples"], 1)
        self.assertAlmostEqual(result["温度"]["finite_ratio"], 1 / 3)
        self.assertAlmostEqual(result["温度"]["longest_no_valid_data_seconds"], 0.3)

    def test_probe_rejects_invalid_duration_and_rate_before_accessing_hardware(self):
        with patch.object(probe.a, "validate_network_path") as route:
            for duration, rate in [(0, 10), (float("nan"), 10), (10, 0), (10, 1000)]:
                with self.subTest(duration=duration, rate=rate), self.assertRaises(ValueError):
                    probe.probe_round("ethernet:以太网", duration_seconds=duration, sample_rate_hz=rate)
        route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
