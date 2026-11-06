"""The week: a time zone, the scheduled shows and the always-present default show."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..scheduling import recurrence
from .labels import clock_string
from .model import DEFAULT_SHOW_ID, MAX_SHOWS, Show, ShowError, default_show
from .timeline import DAY, MAX_TS, check_timestamp, check_zone, cover, expand

LOOK = 8 * DAY          # how far airing() looks back and ahead; one full week and a day


@dataclass(frozen=True)
class Airing:
    """What is on air at one moment."""
    show: Show
    fallback: bool                 # True when the default show is filling an unscheduled stretch
    since: Optional[int]           # when this stretch began; None if it began more than 8 days ago
    until: Optional[int]           # when the show changes; None if nothing changes within 8 days
    next_show: Optional[Show]
    next_fallback: bool


class Week:
    """An immutable snapshot of the schedule. Methods that "change" the week return a new one."""

    def __init__(self, timezone: str, shows: Iterable[Show] = (), default: Optional[Show] = None):
        self.timezone = check_zone(timezone)
        self.default = default if default is not None else default_show()
        if not isinstance(self.default, Show) or not self.default.is_default:
            raise ShowError("The default show is missing or is not the default show.")
        scheduled = tuple(shows)
        if len(scheduled) > MAX_SHOWS:
            raise ShowError(f"A week can hold up to {MAX_SHOWS} shows. Remove some before adding more.")
        seen: set = set()
        for show in scheduled:
            if not isinstance(show, Show) or show.is_default:
                raise ShowError("Only ordinary shows can be scheduled; the default show is kept separately.")
            if show.id in seen:
                raise ShowError(f"Two shows share the id {show.id}.")
            seen.add(show.id)
            try:
                recurrence(self.timezone, clock_string(show.start_min), show.days)   # the station scheduler's own rule check
            except ValueError as exc:
                raise ShowError(str(exc)) from None
        self.shows = tuple(sorted(scheduled, key=lambda s: (s.start_min, s.id)))
        self._by_id = {show.id: show for show in self.shows}

    # -- lookup and copies ---------------------------------------------------------------------------
    def get(self, show_id: str) -> Optional[Show]:
        if show_id == DEFAULT_SHOW_ID:
            return self.default
        return self._by_id.get(show_id)

    def all_shows(self) -> tuple:
        return (self.default,) + self.shows

    def with_show(self, show: Show) -> "Week":
        if show.is_default:
            return Week(self.timezone, self.shows, show)
        others = [s for s in self.shows if s.id != show.id]
        return Week(self.timezone, others + [show], self.default)

    def without(self, show_id: str) -> "Week":
        return Week(self.timezone, [s for s in self.shows if s.id != show_id], self.default)

    def with_timezone(self, timezone: str) -> "Week":
        return Week(timezone, self.shows, self.default)

    def with_shows(self, shows: Iterable[Show]) -> "Week":
        return Week(self.timezone, shows, self.default)

    # -- real time -------------------------------------------------------------------------------------
    def segments(self, low, high) -> list:
        """Tile ``[low, high)`` (epoch seconds) with the show on air, following daylight-saving rules."""
        low, high = check_timestamp(low, "start"), check_timestamp(high, "end")
        return cover(expand(self.timezone, self.shows, low, high), self.default, low, high)

    def airing(self, ts) -> Airing:
        moment = check_timestamp(ts, "time")
        if moment >= MAX_TS:
            raise ShowError("The time is outside the dates myradio can plan (1970 to 2100).")
        low, high = max(0, moment - LOOK), min(moment + LOOK, MAX_TS)
        segments = self.segments(low, high)
        for index, segment in enumerate(segments):
            if segment.start <= moment < segment.end:
                following = segments[index + 1] if index + 1 < len(segments) else None
                return Airing(segment.show, segment.fallback,
                              None if segment.start <= low and low > 0 else segment.start,
                              following.start if following else None,
                              following.show if following else None,
                              following.fallback if following else False)
        raise ShowError("There is no show information for that time.")      # unreachable: the segments tile the window

    def next_change(self, ts) -> Optional[tuple]:
        """``(moment, show, fallback)`` of the next time the show on air changes, or None."""
        airing = self.airing(ts)
        if airing.until is None or airing.next_show is None:
            return None
        return airing.until, airing.next_show, airing.next_fallback

    def plan(self, from_ts, to_ts, seed: int = 0) -> list:
        from .planner import plan
        return plan(self, from_ts, to_ts, seed)
