from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Protocol


class Clock(Protocol):
    def wall_iso(self) -> str:
        """Return an ISO-8601 UTC time for display."""

    def boottime(self) -> float:
        """Return seconds elapsed since boot, including suspend time."""


class SystemClock:
    def wall_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def boottime(self) -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
