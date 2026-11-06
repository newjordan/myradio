"""Anti-repetition memory: which script pieces each host used lately.

The composer asks for a host's recent compositions of one kind and avoids reusing their
templates inside the window. Only template ids and a short digest are kept, never the words
that were spoken, so the memory holds no track titles or other station facts.
"""
from __future__ import annotations

import collections
import threading
from typing import Deque, Dict, List, NamedTuple, Sequence, Tuple

MAX_RECORDS = 30   # per host and kind of script


class Record(NamedTuple):
    units: Tuple[str, ...]   # the template id first, then any extra lines used
    digest: str              # short hash of the finished text, to spot exact repeats
    at: float


class Memory:
    """In-memory implementation. The station's own copy (store.SqliteMemory) also persists."""

    def __init__(self, limit: int = MAX_RECORDS):
        self._limit = max(2, int(limit))
        self._data: Dict[Tuple[str, str], Deque[Record]] = {}
        self._lock = threading.Lock()

    def recent(self, persona: str, segment: str, limit: int) -> List[Record]:
        """The last ``limit`` records, newest first."""
        with self._lock:
            records = self._data.get((persona, segment))
            if not records or limit <= 0:
                return []
            return list(records)[-limit:][::-1]

    def remember(self, persona: str, segment: str, units: Sequence[str], digest: str, at: float) -> None:
        with self._lock:
            records = self._data.setdefault((persona, segment), collections.deque(maxlen=self._limit))
            records.append(Record(tuple(units), digest, float(at)))

    def forget(self, persona: str) -> None:
        with self._lock:
            for key in [k for k in self._data if k[0] == persona]:
                del self._data[key]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def size(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._data.values())
