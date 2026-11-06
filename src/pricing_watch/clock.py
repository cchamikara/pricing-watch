"""The simulated clock. Code reads time only from an injected SimClock, never the wall clock."""

from datetime import datetime
from typing import Protocol


class SimClock(Protocol):
    def now(self) -> datetime:
        """Current simulated time."""
        ...
