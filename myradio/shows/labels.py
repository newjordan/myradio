"""Plain-language labels for times, days and roles.

Everything a person reads in the Shows screen, in a conflict message or in a status line is
built here, so wording stays consistent and is easy to review in one place.
"""
from __future__ import annotations

from typing import Iterable

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_MIN = 1440
WEEK_MIN = 7 * DAY_MIN

# Library kind and label for each planned role (the shared vocabulary).
ROLE_LABELS = {
    "id": "Station ID",
    "song": "Songs",
    "host_break": "Host breaks",
    "spot": "Spots",
    "update": "Update",
}
ROLE_ORDER = ("id", "song", "host_break", "spot", "update")
# One item, as it appears in a "coming up" list.
ITEM_LABELS = {"id": "Station ID", "song": "Song", "host_break": "Host break", "spot": "Spot", "update": "Update"}


def time_label(minute: int) -> str:
    """6 am, 6:30 am, 12 pm, 12 am. Minute 1440 (the end of the day) reads as 12 am."""
    hour, mins = divmod(int(minute) % DAY_MIN, 60)
    suffix = "am" if hour < 12 else "pm"
    clock = hour % 12 or 12
    return f"{clock} {suffix}" if mins == 0 else f"{clock}:{mins:02d} {suffix}"


def range_label(start_minute: int, end_minute: int) -> str:
    return f"{time_label(start_minute)}–{time_label(end_minute)}"


def clock_string(minute: int) -> str:
    """HH:MM for a minute of the day. Minute 1440 is written 24:00 (the end of the day)."""
    minute = int(minute)
    if minute == DAY_MIN:
        return "24:00"
    hour, mins = divmod(minute % DAY_MIN, 60)
    return f"{hour:02d}:{mins:02d}"


def end_string(start_minute: int, length: int) -> str:
    """The end boundary as shown to people: 00:15 to 24:00, never 00:00."""
    end = (int(start_minute) + int(length)) % DAY_MIN
    return clock_string(end or DAY_MIN)


def days_label(days: Iterable[int]) -> str:
    """'every day', 'weekdays', 'weekends' or a list such as 'Monday to Wednesday and Friday'."""
    chosen = sorted(set(days))
    if chosen == [1, 2, 3, 4, 5, 6, 7]:
        return "every day"
    if chosen == [1, 2, 3, 4, 5]:
        return "weekdays"
    if chosen == [6, 7]:
        return "weekends"
    runs, current = [], []
    for day in chosen:
        if current and day == current[-1] + 1:
            current.append(day)
        else:
            if current:
                runs.append(current)
            current = [day]
    if current:
        runs.append(current)
    parts = []
    for run in runs:
        if len(run) >= 3:
            parts.append(f"{DAY_NAMES[run[0] - 1]} to {DAY_NAMES[run[-1] - 1]}")
        else:
            parts.extend(DAY_NAMES[d - 1] for d in run)
    if not parts:
        return "no days"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def duration_label(minutes: int) -> str:
    """'45 minutes', '1 hour', '2 hours 30 minutes'."""
    minutes = int(minutes)
    hours, rest = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("" if hours == 1 else "s"))
    if rest or not hours:
        parts.append(f"{rest} minute" + ("" if rest == 1 else "s"))
    return " ".join(parts)


def mmss(seconds: int) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
