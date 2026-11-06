"""Seeded random streams, one per subsystem.

Each stream is keyed by subsystem name rather than spawn order, so adding or reordering
subsystems never shifts another subsystem's stream.
"""

import hashlib

import numpy as np


def rng_for(root_seed: int, subsystem: str) -> np.random.Generator:
    key = int.from_bytes(hashlib.sha256(subsystem.encode()).digest()[:8], "big")
    return np.random.default_rng(np.random.SeedSequence(root_seed, spawn_key=(key,)))
