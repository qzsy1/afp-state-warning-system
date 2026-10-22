"""高精度采集时间域、截止与跨主机来源合同。"""
import json
import sys
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acquisition_clock import monotonic, wait_interruptible
from acquisition import AbbRobotDriver
from local_direct_acquisition import LocalDirectAdapter
from real_acquisition import ChannelSampleCache, FrameAssembler, UnifiedFrame
from plc_measurement import frame_source_records


class AcquisitionClockTests(unittest.TestCase):
    def test_sub_tick_intervals_cutoff_and_helper_survive_unrelated_clock_jumps(self):
        tick, wall = [90000.001], [1000.0]
        cache = ChannelSampleCache()
        driver = AbbRobotDriver("http://fixture", wall_clock=lambda: wall[0])
        positions = iter([(0, 0, 0), (3, 4, 0), (30, 40, 0)])
        driver._fetch_position = lambda: next(positions)
        adapter = LocalDirectAdapter("abb", driver, ["ABB_X", "线速度"], cache)
        frames = FrameAssembler(["ABB_X", "线速度"], 10, {"ABB_X": 0.5, "线速度": 0.5},
                                start_monotonic=90000, start_wall_time=1000)
        with patch("acquisition_clock.perf_counter", side_effect=lambda: tick[0]), \
                patch("time.monotonic", side_effect=AssertionError("legacy clock used")):
            driver.open()
            for payload, record in driver.read_samples():
                adapter._publish(payload, record)
            self.assertIsNone(cache.latest("线速度").value)
            tick[0] = 90000.1023
            wall[0] -= 3600
            for payload, record in driver.read_samples():
                adapter._publish(payload, record)
            self.assertIsNone(frames.assemble(cache, 90000.102).channels["线速度"].value)
            frame = frames.assemble(cache, 90000.103)
            self.assertEqual(frame.channels["线速度"].value, round(5 / .1013, 2))
            self.assertAlmostEqual(frame.channels["ABB_X"].metadata["age_seconds"], .0007)
            saved = deepcopy(frame_source_records(frame))
            held = frames.assemble(cache, 90000.2)
            self.assertEqual(held.channels["ABB_X"].source_sequence, 2)
            self.assertIsNone(frames.assemble(cache, 90001).channels["ABB_X"].value)
            tick[0] += 800000
            envelope = json.loads(json.dumps(frame.to_envelope("helper-session", {}), allow_nan=False))
            restored = UnifiedFrame.from_envelope(envelope)
            self.assertEqual(frame_source_records(restored), saved)
            self.assertEqual(restored.channels["ABB_X"].received_monotonic, 90000.1023)
            self.assertEqual(restored.channels["线速度"].value, frame.channels["线速度"].value)

    def test_frame_rechecks_early_wait_without_selecting_future_response(self):
        tick, waits = [100.0], []
        def wait(delay):
            waits.append(delay)
            tick[0] += delay / 2 if len(waits) == 1 else delay
        frames = FrameAssembler(["ABB_X"], 10, clock=lambda: tick[0], wait=wait,
                                start_monotonic=100.1)
        self.assertEqual(frames.wait_next_deadline(), 100.1)
        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(tick[0], 100.1)

    def test_precision_wait_is_cancelled_during_coarse_and_fine_waits(self):
        for duration in (.1, .005):
            with self.subTest(duration=duration):
                event = threading.Event()
                with patch("acquisition_clock.time.sleep", side_effect=lambda _: event.set()):
                    if duration > .02:
                        with patch.object(event, "wait", side_effect=lambda _: event.set()):
                            self.assertTrue(wait_interruptible(event, duration))
                    else:
                        self.assertTrue(wait_interruptible(event, duration))

    def test_defaults_share_one_clock_in_driver_adapter_and_frame(self):
        driver = AbbRobotDriver("http://fixture")
        worker = LocalDirectAdapter("abb", driver, [], ChannelSampleCache())
        frame = FrameAssembler([], 10)
        self.assertIs(driver.clock, monotonic)
        self.assertIs(worker.clock, monotonic)
        self.assertIs(frame.clock, monotonic)


if __name__ == "__main__":
    unittest.main()
