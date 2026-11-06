"""Small shared helpers."""
from __future__ import annotations

import datetime as dt
import math
import os
from typing import Optional


def finite_number(value: object) -> bool:
    """True for a real number (a whole number or a float) that is finite. ``True`` and ``False`` are not numbers here.

    ``math.isfinite`` raises ``OverflowError`` for a whole number too large for a float (``10 ** 400`` is legal
    JSON), so whole numbers are never handed to it: they are always finite, and are compared as they are.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return isinstance(value, int) or math.isfinite(value)


def iso(ts: Optional[float]) -> Optional[str]:
    """Epoch seconds (UTC) to ISO-8601 with an explicit offset, for API output."""
    if ts is None:
        return None
    try:
        return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def short(text: object, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def free_bytes(path) -> Optional[int]:
    """Free space of the volume holding ``path``, or None when it cannot be read."""
    import shutil

    try:
        return shutil.disk_usage(os.fspath(path)).free
    except OSError:
        return None
