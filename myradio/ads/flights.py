"""Calendar rules for campaigns: which flight is open now, and how an even pace is worked out.

All wall-clock rules follow the campaign's IANA timezone:

* A flight's start and end are local dates and both are included. Whether a moment is inside a
  flight is decided from its local date, so daylight-saving changes cannot shift a flight by an hour.
* Days are ISO weekdays of the local date. Hours (dayparts) are windows of local wall time. When the
  clock springs forward the skipped hour simply is not there; when it falls back the repeated hour
  matches its window twice, because the wall clock shows it twice.
* "Per day" and "per week" limits use the local calendar day and the Monday-to-Sunday week.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Optional

from .model import AdsError, Campaign, Flight, count_weekdays, zone_for

DAY = dt.timedelta(days=1)
MAX_TIMESTAMP = 253402300799.0  # the last second of year 9999


def local_datetime(ts: float, zone) -> dt.datetime:
    try:
        return dt.datetime.fromtimestamp(ts, zone)
    except (OverflowError, OSError, ValueError):
        raise AdsError("That time is outside the range the station can handle.") from None


def seconds_of_day(local: dt.datetime) -> float:
    return local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6


def day_bounds(day: dt.date, zone) -> tuple:
    """UTC epoch seconds of local midnight at the start and at the end of ``day`` (end excluded)."""
    start = dt.datetime.combine(day, dt.time(0, 0), zone).timestamp()
    end = dt.datetime.combine(day + DAY, dt.time(0, 0), zone).timestamp()
    return start, end


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.isoweekday() - 1)


def week_bounds(day: dt.date, zone) -> tuple:
    monday = week_start(day)
    return (dt.datetime.combine(monday, dt.time(0, 0), zone).timestamp(),
            dt.datetime.combine(monday + 7 * DAY, dt.time(0, 0), zone).timestamp())


@dataclasses.dataclass(frozen=True)
class Window:
    """Whether a campaign may play at one moment. ``reason`` is set when it may not."""
    open: bool
    flight: Optional[Flight] = None
    reason: str = ""
    detail: dict = dataclasses.field(default_factory=dict)
    local: Optional[dt.datetime] = None


def window_state(campaign: Campaign, now: float, show_id: Optional[str] = None) -> Window:
    """Is ``campaign`` inside one of its flights, on an allowed day, hour and show, at ``now``?"""
    zone = zone_for(campaign.timezone)
    local = local_datetime(now, zone)
    today = local.date()
    flight = next((f for f in campaign.flights if f.start <= today and (f.end is None or today <= f.end)), None)
    if flight is None:
        later = [f.start for f in campaign.flights if f.start > today]
        if later:
            return Window(False, None, "not_started", {"date": min(later).isoformat()}, local)
        ends = [f.end for f in campaign.flights if f.end]
        return Window(False, None, "ended", {"date": max(ends).isoformat()} if ends else {}, local)
    if today.isoweekday() not in flight.days:
        return Window(False, flight, "wrong_day", {"weekday": today.isoweekday()}, local)
    sod = seconds_of_day(local)
    if flight.dayparts and not any(a <= sod < b for a, b in flight.dayparts):
        return Window(False, flight, "outside_hours", {"windows": list(flight.dayparts)}, local)
    if flight.show_ids and (show_id is None or show_id not in flight.show_ids):
        return Window(False, flight, "wrong_show", {"shows": list(flight.show_ids)}, local)
    return Window(True, flight, "", {}, local)


def campaign_state(campaign: Campaign, now: float) -> str:
    """"paused", "running" (inside a flight's dates), "scheduled" (starts later) or "ended"."""
    if campaign.paused:
        return "paused"
    today = local_datetime(now, zone_for(campaign.timezone)).date()
    if any(f.start <= today and (f.end is None or today <= f.end) for f in campaign.flights):
        return "running"
    if any(f.start > today for f in campaign.flights):
        return "scheduled"
    return "ended"


def next_change(campaign: Campaign, now: float) -> Optional[float]:
    """The next moment a flight of this campaign starts or finishes, or None. Used to know when a
    cached "running / scheduled / ended" count goes stale."""
    zone = zone_for(campaign.timezone)
    best = None
    for flight in campaign.flights:
        edges = [day_bounds(flight.start, zone)[0]]
        if flight.end is not None:
            edges.append(day_bounds(flight.end, zone)[1])
        for edge in edges:
            if edge > now and (best is None or edge < best):
                best = edge
    return best


def is_active_day(campaign: Campaign, day: dt.date) -> bool:
    return any(f.start <= day and (f.end is None or day <= f.end) and day.isoweekday() in f.days
               for f in campaign.flights)


def active_days_total(campaign: Campaign) -> Optional[int]:
    """How many days the campaign may play on across all flights, or None if a flight has no end."""
    if any(f.end is None for f in campaign.flights):
        return None
    return sum(count_weekdays(f.start, f.end, f.days) for f in campaign.flights)


def active_days_before(campaign: Campaign, today: dt.date) -> int:
    """Playable days strictly before ``today``."""
    total = 0
    for flight in campaign.flights:
        last = today - DAY if flight.end is None or flight.end >= today else flight.end
        total += count_weekdays(flight.start, last, flight.days)
    return total


def day_fraction(flight: Flight, sod: float) -> float:
    """How much of today's playable hours have passed, from 0.0 to 1.0."""
    windows = flight.dayparts or ((0, 86400),)
    total = sum(b - a for a, b in windows)
    done = sum(min(max(sod - a, 0.0), b - a) for a, b in windows)
    return done / total


def pace_target(campaign: Campaign) -> Optional[tuple]:
    """What an even pace aims at: ("day", plays per day) or ("flight", total plays), or None.

    The booked plan comes first. Without a plan, an even pace spreads the daily limit across the
    playable hours."""
    if campaign.plan.per_day is not None:
        return ("day", campaign.plan.per_day)
    if campaign.plan.total is not None:
        return ("flight", campaign.plan.total)
    if campaign.caps.per_day is not None:
        return ("day", campaign.caps.per_day)
    return None


def expected_by_now(campaign: Campaign, flight: Flight, local: dt.datetime, target: tuple) -> float:
    """How many plays an even pace would have delivered by ``local``."""
    kind, amount = target
    fraction = day_fraction(flight, seconds_of_day(local))
    if kind == "day":
        return amount * fraction
    days = active_days_total(campaign)
    if not days:
        return float(amount)  # cannot spread over an open-ended run: do not hold back
    return amount * (active_days_before(campaign, local.date()) + fraction) / days
