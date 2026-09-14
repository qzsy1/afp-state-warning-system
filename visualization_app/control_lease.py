from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LeaseDecision:
    granted: bool
    error: str = ""
    owner_id: str | None = None
    label: str = ""
    last_heartbeat: float | None = None


class RealControlLease:
    """Single-owner arbitration for commands that can affect real hardware."""

    def __init__(self, heartbeat_timeout_seconds: float = 30.0) -> None:
        self.heartbeat_timeout_seconds = max(1.0, float(heartbeat_timeout_seconds))
        self._owner_id: str | None = None
        self._label = ""
        self._last_heartbeat: float | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _now(now: float | None) -> float:
        return time.monotonic() if now is None else float(now)

    def _expire_if_needed(self, now: float) -> None:
        if (
            self._owner_id is not None
            and self._last_heartbeat is not None
            and now - self._last_heartbeat > self.heartbeat_timeout_seconds
        ):
            self._owner_id = None
            self._label = ""
            self._last_heartbeat = None

    def acquire(
        self, owner_id: str, label: str, now: float | None = None
    ) -> LeaseDecision:
        clean_owner = str(owner_id or "").strip()
        if not clean_owner:
            return LeaseDecision(False, "owner_required")
        current = self._now(now)
        with self._lock:
            self._expire_if_needed(current)
            if self._owner_id is None or self._owner_id == clean_owner:
                self._owner_id = clean_owner
                self._label = str(label or clean_owner)[:200]
                self._last_heartbeat = current
                return LeaseDecision(
                    True,
                    owner_id=self._owner_id,
                    label=self._label,
                    last_heartbeat=current,
                )
            return LeaseDecision(
                False,
                "real_control_busy",
                owner_id=self._owner_id,
                label=self._label,
                last_heartbeat=self._last_heartbeat,
            )

    def heartbeat(self, owner_id: str, now: float | None = None) -> LeaseDecision:
        clean_owner = str(owner_id or "").strip()
        current = self._now(now)
        with self._lock:
            self._expire_if_needed(current)
            if self._owner_id != clean_owner:
                return LeaseDecision(
                    False,
                    "real_control_not_owner",
                    owner_id=self._owner_id,
                    label=self._label,
                    last_heartbeat=self._last_heartbeat,
                )
            self._last_heartbeat = current
            return LeaseDecision(
                True,
                owner_id=self._owner_id,
                label=self._label,
                last_heartbeat=current,
            )

    def release(self, owner_id: str) -> bool:
        clean_owner = str(owner_id or "").strip()
        with self._lock:
            if self._owner_id != clean_owner:
                return False
            self._owner_id = None
            self._label = ""
            self._last_heartbeat = None
            return True

    def force_takeover(
        self, owner_id: str = "local-admin", label: str = "本机软件"
    ) -> LeaseDecision:
        clean_owner = str(owner_id or "local-admin").strip() or "local-admin"
        current = time.monotonic()
        with self._lock:
            self._owner_id = clean_owner
            self._label = str(label or clean_owner)[:200]
            self._last_heartbeat = current
            return LeaseDecision(
                True,
                owner_id=self._owner_id,
                label=self._label,
                last_heartbeat=current,
            )

    def status(self, now: float | None = None) -> dict[str, Any]:
        current = self._now(now)
        with self._lock:
            self._expire_if_needed(current)
            age = (
                None
                if self._last_heartbeat is None
                else max(0.0, current - self._last_heartbeat)
            )
            return {
                "owner_id": self._owner_id,
                "label": self._label,
                "last_heartbeat": self._last_heartbeat,
                "age_seconds": age,
                "heartbeat_timeout_seconds": self.heartbeat_timeout_seconds,
            }
