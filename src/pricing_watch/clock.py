"""The simulated clock. Code reads time only from an injected SimClock, never the wall clock."""

from datetime import datetime, timedelta
from typing import Protocol


class SimClock(Protocol):
    def now(self) -> datetime:
        """Current simulated time."""
        ...


TICK = timedelta(hours=1)


class TickClock:
    """The simulator's clock: one tick is one simulated hour."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self) -> None:
        self._now += TICK
