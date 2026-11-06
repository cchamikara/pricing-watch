"""The tick loop.

Every tick runs the hooks in this fixed order, then advances the clock by one hour:

1. faults: apply or revert scheduled faults
2. competitor_feed: competitors publish (the hook itself decides every 6 h)
3. demand: draw demand and record sales
4. replenishment: inbound orders arrive
5. repricer: the rules repricer reacts
6. triggers: detect incident conditions
7. incidents: handle open incidents

Determinism depends on this order: each hook sees the state the earlier ones left.
"""

from collections.abc import Callable, Mapping
from datetime import datetime

from pricing_watch.clock import TickClock

HOOK_ORDER = (
    "faults",
    "competitor_feed",
    "demand",
    "replenishment",
    "repricer",
    "triggers",
    "incidents",
)

Hook = Callable[[datetime], None]


def run(clock: TickClock, hours: int, hooks: Mapping[str, Hook]) -> None:
    """Run `hours` ticks. Hooks not yet built can be left out."""
    if unknown := set(hooks) - set(HOOK_ORDER):
        raise ValueError(f"unknown hooks {sorted(unknown)}; expected names from {HOOK_ORDER}")
    ordered = [hooks[name] for name in HOOK_ORDER if name in hooks]
    for _ in range(hours):
        for hook in ordered:
            hook(clock.now())
        clock.advance()
