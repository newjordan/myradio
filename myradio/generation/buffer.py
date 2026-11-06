"""The generate-ahead buffer policy: how much is "enough" and which waiting job runs next.

"Ready" means audio the engine has made and put in the library that has not started playing yet. Music
is measured in seconds, every kind of speech in items. For each kind there is a low and a high mark:

* below the low mark the engine is *filling*: it works through the waiting jobs of that kind;
* it keeps filling until the high mark is reached, then stops (no more music than needed piles up);
* between the marks it keeps doing whatever it was doing (hysteresis, so it does not flip back and forth);
* a low mark of 0 means "start again only when nothing is ready" (fewer than 0 is never true, so it needs this rule).

Producers (the director, hosts, ads) enqueue the jobs; this policy decides when the engine works on them.
A job whose priority is at least the *urgent* level ignores the marks, because someone needs it now.

Among jobs that may run, the one for the worker that is already loaded on the resource goes first, so a
single GPU is not swapped between music and speech models for every job. A job for another worker that has
waited longer than the group's ``switch_after_s`` is never held back by that preference.
"""
from __future__ import annotations

import threading
from typing import Callable, Mapping, Optional

from .config import ITEM_BUFFER_MAX, SONG_BUFFER_MAX
from .errors import GenerationError
from .payloads import ROLES, ROLE_LABEL_PLURAL


def unit_for(role: str) -> str:
    return "seconds" if role == "song" else "items"


def level_value(role: str, level: Mapping) -> float:
    """The ready amount in the unit of the role (seconds of music, items of speech)."""
    return float(level["seconds"]) if role == "song" else float(level["items"])


class BufferPolicy:
    def __init__(self, defaults: Mapping[str, tuple], urgent_priority: int, overrides: Optional[Mapping] = None):
        self._lock = threading.Lock()
        self._defaults = {role: tuple(defaults[role]) for role in ROLES}
        self._overrides = {role: tuple(v) for role, v in (overrides or {}).items() if role in ROLES}
        self._filling: dict = {}
        self.urgent_priority = urgent_priority

    def marks(self, role: str) -> tuple:
        with self._lock:
            return self._overrides.get(role, self._defaults[role])

    def marks_from(self, role: str) -> str:
        """``"operation"`` when the marks were set with ``generation.set_buffer``, ``"settings"`` when they come from config.json."""
        with self._lock:
            return "operation" if role in self._overrides else "settings"

    def overrides(self) -> dict:
        with self._lock:
            return {role: list(v) for role, v in self._overrides.items()}

    def set_marks(self, role: str, low: int, high: int) -> None:
        if role not in ROLES:
            raise GenerationError("Choose a kind of audio: " + ", ".join(ROLES) + ".")
        lo_max, hi_max = SONG_BUFFER_MAX if role == "song" else ITEM_BUFFER_MAX
        unit = "seconds" if role == "song" else "items"
        if not 0 <= low <= lo_max:
            raise GenerationError(f"The low mark for {ROLE_LABEL_PLURAL[role]} must be from 0 to {lo_max} {unit}.")
        if not 1 <= high <= hi_max:
            raise GenerationError(f"The high mark for {ROLE_LABEL_PLURAL[role]} must be from 1 to {hi_max} {unit}.")
        if low > high:
            raise GenerationError("The low mark cannot be above the high mark.")
        with self._lock:
            self._overrides[role] = (low, high)

    def update(self, role: str, ready: float) -> bool:
        """Apply the hysteresis for a new ready amount and return whether the engine is filling this kind."""
        low, high = self.marks(role)
        with self._lock:
            if role not in self._filling:
                self._filling[role] = ready < high
            elif ready < low or (low <= 0 and ready <= 0):
                self._filling[role] = True
            elif ready >= high:
                self._filling[role] = False
            return self._filling[role]

    def filling(self, role: str) -> Optional[bool]:
        with self._lock:
            return self._filling.get(role)

    def allows(self, role: str, priority: int, filling: bool) -> bool:
        return filling or priority >= self.urgent_priority


def choose(candidates: list, *, now: float, loaded_worker: Optional[str], switch_after_s: float, allowed: Callable[[dict], bool],
           urgent_priority: int) -> Optional[dict]:
    """Pick the next job from ``candidates`` (already sorted: priority high to low, then oldest first)."""
    pool = [job for job in candidates if allowed(job)]
    if not pool:
        return None
    urgent = [job for job in pool if job["priority"] >= urgent_priority]
    if urgent:
        return urgent[0]
    if loaded_worker is not None:
        same = [job for job in pool if job["worker_id"] == loaded_worker]
        overdue = [job for job in pool if job["worker_id"] != loaded_worker and now - job["not_before"] >= switch_after_s]
        if same and not overdue:
            return same[0]
    return pool[0]
