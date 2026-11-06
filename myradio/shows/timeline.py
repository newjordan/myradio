"""From the weekly wall-clock schedule to real moments in time.

Shows are written in local wall-clock time ("every weekday at 06:00"). This module turns that into
UTC instants for a given date range, following the same daylight-saving rules as
``myradio.scheduling.next_occurrence``:

* a wall-clock time that does not exist (the hour skipped when clocks go forward) is skipped over;
* a wall-clock time that happens twice (the hour repeated when clocks go back) is taken once, at its
  first occurrence (``fold=0``).

A recurring point in time can simply skip a missing day, but a show is an interval, and two shows that
meet at 02:30 must still meet when 02:30 does not exist. So a show boundary that falls inside the
skipped hour moves to the moment the clocks jump (a show that lies wholly inside it airs for no time
that day), and a boundary in the repeated hour is the first of the two. Neighbouring shows therefore
always tile real time with no overlap and no hole, and a show that spans a clock change lasts an hour
less or more than its nominal length.

Everything here is a pure function of its arguments. Nothing reads the current time.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..scheduling import UTC, recurrence
from .labels import DAY_MIN
from .model import Show, ShowError

HOUR = 3600
DAY = 86400
MAX_TS = 4102444800            # 2100-01-01 UTC: the latest moment this module will work with
ZONE_RE = re.compile(r"[A-Za-z0-9_+\-/]{1,64}")
NOT_ZONES = frozenset({"posixrules", "localtime", "Factory"})
_EPOCH = dt.datetime(1970, 1, 1)
ZONE_HELP = "That time zone is not recognized. Use a name such as Europe/London or America/New_York."


@lru_cache(maxsize=64)
def zone_for(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def check_zone(name) -> str:
    """Return a usable IANA zone name or raise ``ShowError`` in plain words."""
    if not isinstance(name, str):
        raise ShowError(ZONE_HELP)
    text = name.strip()
    if (not ZONE_RE.fullmatch(text) or text in NOT_ZONES or text.startswith("/") or ".." in text
            or text.endswith("/")):
        raise ShowError(ZONE_HELP)
    try:
        zone_for(text)
        recurrence(text, "00:00", "1")        # the station scheduler has to accept the zone as well
    except (ZoneInfoNotFoundError, ValueError, OSError, KeyError):
        raise ShowError(ZONE_HELP) from None
    return text


def check_timestamp(value, what: str = "time") -> int:
    """A finite epoch-seconds value between 1970 and 2100, as a whole number of seconds (rounded down)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShowError(f"The {what} must be a number of seconds since 1970.")
    if value != value or value in (float("inf"), float("-inf")) or not 0 <= value <= MAX_TS:
        raise ShowError(f"The {what} is outside the dates myradio can plan (1970 to 2100).")
    return int(value // 1)


def _offset(zone, ts: int) -> int:
    return int(dt.datetime.fromtimestamp(ts, zone).utcoffset().total_seconds())


def _gap_end(zone, naive_seconds: int, pushed: int) -> int:
    """The first real moment after a skipped wall-clock time: where the clocks jump forward."""
    after = _offset(zone, pushed)
    low, high = naive_seconds - after, pushed        # the old offset applies at low, the new one at high
    if low >= high or _offset(zone, low) == after:
        return pushed                                  # not a clean single jump: use the standard mapping
    while high - low > 1:
        middle = (low + high) // 2
        if _offset(zone, middle) == after:
            high = middle
        else:
            low = middle
    return high


@lru_cache(maxsize=1 << 15)
def resolve_local(zone_name: str, ordinal: int, minute: int) -> int:
    """UTC epoch seconds of a wall-clock moment: ``minute`` minutes after local midnight on the date
    ``ordinal`` (``date.toordinal()``). ``minute`` may be up to 2 days long (a show that ends tomorrow)."""
    ordinal += minute // DAY_MIN
    minute %= DAY_MIN
    zone = zone_for(zone_name)
    day = dt.date.fromordinal(ordinal)
    naive = dt.datetime(day.year, day.month, day.day, minute // 60, minute % 60)
    utc = naive.replace(tzinfo=zone).astimezone(UTC)   # fold=0: the first of two repeated times
    if utc.astimezone(zone).replace(tzinfo=None) == naive:
        return int(utc.timestamp())
    return _gap_end(zone, int((naive - _EPOCH).total_seconds()), int(utc.timestamp()))


def grid_next(zone_name: str, moment: int) -> int:
    """The first top of a local hour strictly after ``moment``.

    Local hours are defined by the clock reading in force at that instant, so the grid is a property of
    time itself, not of any schedule or window. Blocks cut along it have the same edges whichever
    window asks for them."""
    zone = zone_for(zone_name)
    offset = _offset(zone, moment)
    candidate = moment + HOUR - (moment + offset) % HOUR
    if _offset(zone, candidate) == offset:
        return candidate
    low, high = moment, candidate                      # a clock change lies in (moment, candidate]
    while high - low > 1:
        middle = (low + high) // 2
        if _offset(zone, middle) == offset:
            low = middle
        else:
            high = middle
    return high + (-(high + _offset(zone, high))) % HOUR


def local_ordinal(zone_name: str, ts: int) -> int:
    return dt.datetime.fromtimestamp(ts, zone_for(zone_name)).date().toordinal()


def to_local_iso(zone_name: str, ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, zone_for(zone_name)).isoformat(timespec="seconds")


def parse_moment(value, zone_name: str, what: str = "time") -> int:
    """Accept epoch seconds or an ISO-8601 text that carries an offset, for the API edges."""
    if isinstance(value, bool):
        raise ShowError(f"The {what} is not valid.")
    if isinstance(value, (int, float)):
        return check_timestamp(value, what)
    if isinstance(value, str) and len(value) <= 40:
        text = value.strip()
        if re.fullmatch(r"[0-9]{1,11}(\.[0-9]+)?", text):
            return check_timestamp(float(text), what)
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
        except ValueError:
            moment = None
        if moment is not None and moment.tzinfo is not None:
            try:
                seconds = moment.timestamp()
            except (OverflowError, OSError, ValueError):
                seconds = None
            if seconds is not None:
                return check_timestamp(seconds, what)
    raise ShowError(f"Give the {what} as a full date and time with its time zone offset, for example "
                    "2025-03-01T06:00:00-05:00, or as seconds since 1970.")


@dataclass(frozen=True)
class Occurrence:
    start: int
    end: int
    show: Show


@dataclass(frozen=True)
class Segment:
    """A stretch of real time on which exactly one show is on air."""
    start: int
    end: int
    show: Show
    fallback: bool          # True when this is the default show filling time no show is scheduled for


def expand(zone_name: str, shows: Sequence[Show], low: int, high: int) -> list:
    """Every occurrence of the scheduled shows that touches ``[low, high)``, sorted by start then id."""
    first = local_ordinal(zone_name, low) - 1
    last = local_ordinal(zone_name, high) + 1
    found = []
    for ordinal in range(first, last + 1):
        weekday = dt.date.fromordinal(ordinal).isoweekday()
        for show in shows:
            if weekday not in show.day_set:
                continue
            start = resolve_local(zone_name, ordinal, show.start_min)
            end = resolve_local(zone_name, ordinal, show.start_min + show.length_min)
            if end > start and end > low and start < high:
                found.append(Occurrence(start, end, show))
    found.sort(key=lambda o: (o.start, o.show.id))
    return found


def cover(occurrences: Sequence[Occurrence], default: Show, low: int, high: int) -> list:
    """Tile ``[low, high)`` with segments. Where two occurrences overlap (the saved schedule never allows
    it, but damaged data might) the one that started later wins, then the smaller id. Time with no
    occurrence belongs to the default show. Neighbouring pieces of the same show are merged."""
    cuts = {low, high}
    for occurrence in occurrences:
        if low < occurrence.start < high:
            cuts.add(occurrence.start)
        if low < occurrence.end < high:
            cuts.add(occurrence.end)
    points = sorted(cuts)
    segments: list = []
    active: list = []
    cursor = 0
    for a, b in zip(points, points[1:]):
        while cursor < len(occurrences) and occurrences[cursor].start <= a:
            active.append(occurrences[cursor])
            cursor += 1
        active = [o for o in active if o.end > a]
        best = min(active, key=lambda o: (-o.start, o.show.id)) if active else None
        show, fallback = (best.show, False) if best else (default, True)
        if segments and segments[-1].show.id == show.id and segments[-1].fallback == fallback:
            segments[-1] = Segment(segments[-1].start, b, show, fallback)
        else:
            segments.append(Segment(a, b, show, fallback))
    return segments


def blocks(zone_name: str, start: int, end: int):
    """Cut ``[start, end)`` at every top of a local hour: yields ``(block_start, block_end)``."""
    cursor = start
    while cursor < end:
        edge = min(grid_next(zone_name, cursor), end)
        yield cursor, edge
        cursor = edge
