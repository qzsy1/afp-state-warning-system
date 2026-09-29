"""Absolute-deadline pacing for local simulation replay."""

from __future__ import annotations

import time
from typing import Callable


class MonotonicReplayScheduler:
    """Pace samples from their absolute index so work time never accumulates."""

    def __init__(
        self,
        rate_hz: float,
        *,
        clock: Callable[[], float] = time.perf_counter,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self.rate_hz = max(0.1, float(rate_hz))
        self.clock = clock
        self.wait = wait or self._default_wait
        self.started_at = float(clock())
        self.emitted_count = 0

    @staticmethod
    def _default_wait(seconds: float) -> bool:
        time.sleep(max(0.0, float(seconds)))
        return False

    def wait_next(self) -> bool:
        self.emitted_count += 1
        deadline = self.started_at + self.emitted_count / self.rate_hz
        remaining = max(0.0, deadline - float(self.clock()))
        return bool(self.wait(remaining)) if remaining > 0 else False

    @property
    def effective_rate_hz(self) -> float:
        elapsed = max(0.0, float(self.clock()) - self.started_at)
        return self.emitted_count / elapsed if elapsed > 0 else 0.0
