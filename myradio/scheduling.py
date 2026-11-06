"""Wall-clock recurrence with explicit DST policy and no pre-expanded event queue."""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc


def recurrence(timezone, local_time, weekdays):
    """Validate a rule. Weekdays use ISO numbers, Monday=1 … Sunday=7."""
    if not isinstance(timezone, str) or len(timezone) > 100:
        raise ValueError("Choose an IANA timezone, for example Europe/London.")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown timezone. Install system timezone data or choose UTC.") from None
    if not isinstance(local_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", local_time):
        raise ValueError("Use a 24-hour local time, HH:MM.")
    if not isinstance(weekdays, str) or not re.fullmatch(r"[1-7](?:,[1-7])*", weekdays):
        raise ValueError("Weekdays must be comma-separated ISO days: Monday=1 … Sunday=7.")
    days = sorted(set(map(int, weekdays.split(','))))
    return zone, dt.time.fromisoformat(local_time), days


def next_occurrence(timezone, local_time, weekdays, after):
    """First occurrence strictly after UTC epoch ``after``.

    Missing spring-forward times are skipped. Repeated fall-back times air once,
    at the first occurrence (fold=0). Dates are advanced in the named zone, not
    by adding 24 hours to UTC; recurring programs retain their local wall time.
    """
    zone, clock, days = recurrence(timezone, local_time, weekdays)
    date = dt.datetime.fromtimestamp(after, UTC).astimezone(zone).date()
    for offset in range(15):
        day = date + dt.timedelta(days=offset)
        if day.isoweekday() not in days:
            continue
        local = dt.datetime.combine(day, clock, zone)
        utc = local.astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if utc.timestamp() > after:
            return utc.timestamp()
    raise ValueError("Cannot find a valid occurrence in the next two weeks.")
