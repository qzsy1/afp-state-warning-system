from __future__ import annotations

import unittest

from simulation_replay import MonotonicReplayScheduler


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        self.now += max(0.0, float(seconds))
        return False


class SimulationReplaySchedulerTests(unittest.TestCase):
    def test_ten_hertz_for_six_hundred_seconds_emits_exactly_six_thousand(self):
        clock = FakeClock()
        emitted = []
        scheduler = MonotonicReplayScheduler(
            10.0,
            clock=clock.monotonic,
            wait=clock.wait,
        )

        for row_index in range(6000):
            emitted.append(row_index)
            clock.now += 0.03  # parsing/writing time must not accumulate as drift
            scheduler.wait_next()

        self.assertEqual(len(emitted), 6000)
        self.assertAlmostEqual(clock.now, 600.0, places=6)
        self.assertAlmostEqual(scheduler.effective_rate_hz, 10.0, places=6)

    def test_processing_overrun_skips_sleep_without_moving_future_deadlines(self):
        clock = FakeClock()
        scheduler = MonotonicReplayScheduler(
            10.0,
            clock=clock.monotonic,
            wait=clock.wait,
        )

        clock.now += 0.35
        scheduler.wait_next()
        self.assertEqual(clock.now, 0.35)
        scheduler.wait_next()
        self.assertEqual(clock.now, 0.35)
        scheduler.wait_next()
        self.assertEqual(clock.now, 0.35)
        scheduler.wait_next()
        self.assertAlmostEqual(clock.now, 0.4, places=6)


if __name__ == "__main__":
    unittest.main()
