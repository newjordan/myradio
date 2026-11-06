"""A small deterministic random source.

Scripts must be reproducible from a seed on every supported Python version, so this only uses
``random.random()`` (whose output is stable across 3.10 to 3.13) and does its own picking and
shuffling instead of relying on helpers whose algorithms have changed between releases.
"""
from __future__ import annotations

import hashlib
import random
from typing import List, Sequence, TypeVar

T = TypeVar("T")
MAX_SEED = 2 ** 63 - 1


class Rng:
    def __init__(self, *parts: object):
        digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8", "replace")).digest()
        self._random = random.Random(int.from_bytes(digest[:16], "big"))

    def random(self) -> float:
        return self._random.random()

    def below(self, n: int) -> int:
        return min(n - 1, int(self._random.random() * n)) if n > 0 else 0

    def pick(self, items: Sequence[T]) -> T:
        return items[self.below(len(items))]

    def shuffled(self, items: Sequence[T]) -> List[T]:
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.below(i + 1)
            out[i], out[j] = out[j], out[i]
        return out
