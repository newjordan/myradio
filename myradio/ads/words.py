"""Plain-language wording for the spots screen: why a spot was left out, what the next break
holds, and how a campaign's schedule reads. Nothing here decides anything; it only describes."""
from __future__ import annotations

from typing import Iterable

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SHORT_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Reasons that only say "this spot is not scheduled for this moment". They are explained in a
# preview but are not counted as "held back" in reports, because they are not a decision about
# a spot that was due to play.
STRUCTURAL = frozenset({"paused", "not_started", "ended", "wrong_day", "outside_hours", "wrong_show", "house_standby"})

# Short labels for report columns, in the order the columns appear.
REASON_LABELS = {
    "voice_rights_needed": "Voice rights not confirmed",
    "commercial_ack_needed": "Paid use not confirmed",
    "no_creative": "Nothing to play",
    "timezone_unknown": "Unknown timezone",
    "audio_not_ready": "Recording not ready",
    "audio_missing": "Recording missing",
    "audio_unavailable": "Recording unavailable",
    "plan_met": "Plan already met",
    "hour_cap": "Hourly limit reached",
    "day_cap": "Daily limit reached",
    "week_cap": "Weekly limit reached",
    "too_soon_campaign": "Played too recently",
    "too_soon_category": "Similar spot too close",
    "ahead_of_pace": "Ahead of even pace",
    "no_room": "Did not fit the break",
    "break_full": "Break was full",
    "hour_limit_station": "Station hourly limit reached",
}
REPORTED_REASONS = tuple(REASON_LABELS)


def plural(n: int, one: str, many: str = "") -> str:
    return one if n == 1 else (many or one + "s")


def seconds_text(seconds: float) -> str:
    whole = int(round(seconds))
    if whole < 60:
        return f"{whole} {plural(whole, 'second')}"
    minutes, rest = divmod(whole, 60)
    text = f"{minutes} {plural(minutes, 'minute')}"
    return text if not rest else f"{text} {rest} {plural(rest, 'second')}"


def minutes_text(seconds: float) -> str:
    whole = max(0, int(round(abs(seconds) / 60)))
    return f"{whole} {plural(whole, 'minute')}"


def distance_text(seconds: float, ahead: bool) -> str:
    """How far a play is from a moment, read aloud: "20 seconds ago", "1 minute 30 seconds ago", "5 minutes from now".
    Anything under two minutes is given in seconds, so a play that is only moments away never reads "0 minutes"."""
    whole = int(round(abs(seconds)))
    if whole < 1:
        return "right now" if ahead else "just now"
    amount = seconds_text(seconds) if whole < 120 else minutes_text(seconds)
    return f"{amount} from now" if ahead else f"{amount} ago"


def clock_text(seconds: int) -> str:
    """25200 -> "7 am", 45900 -> "12:45 pm", 0 -> "midnight"."""
    if seconds in (0, 86400):
        return "midnight"
    hours, minutes = divmod(seconds // 60, 60)
    suffix = "am" if hours < 12 else "pm"
    shown = hours % 12 or 12
    return f"{shown} {suffix}" if not minutes else f"{shown}:{minutes:02d} {suffix}"


def windows_text(windows: Iterable) -> str:
    parts = [f"{clock_text(a)} – {clock_text(b)}" for a, b in windows]
    return ", ".join(parts) if parts else "all day"


def days_text(days: Iterable) -> str:
    chosen = tuple(sorted(days))
    if chosen == (1, 2, 3, 4, 5, 6, 7):
        return "Every day"
    if chosen == (1, 2, 3, 4, 5):
        return "Weekdays"
    if chosen == (6, 7):
        return "Weekends"
    return ", ".join(SHORT_DAYS[d - 1] for d in chosen)


def date_text(day) -> str:
    return f"{MONTHS[day.month - 1]} {day.day}"


def flight_text(flight) -> str:
    """"Weekdays, 7 am – 7 pm, Nov 12 to Dec 31"."""
    parts = [days_text(flight.days)]
    if flight.dayparts:
        parts.append(windows_text(flight.dayparts))
    if flight.end is None:
        parts.append(f"from {date_text(flight.start)}")
    else:
        parts.append(f"{date_text(flight.start)} to {date_text(flight.end)}")
    if flight.show_ids:
        parts.append("some shows only")
    return ", ".join(parts)


def schedule_text(campaign) -> str:
    first = flight_text(campaign.flights[0])
    extra = len(campaign.flights) - 1
    return first if not extra else f"{first} (and {extra} more {plural(extra, 'run')})"


def skip_text(reason: str, name: str, detail: dict) -> str:
    """One sentence saying why a spot is not in the break, and what to do when there is something to do."""
    d = detail or {}
    if reason == "paused":
        return f"{name} is paused. Resume it to let it play."
    if reason == "not_started":
        return f"{name} starts on {d.get('date', 'a later date')}."
    if reason == "ended":
        return f"{name} ended on {d.get('date', 'an earlier date')}."
    if reason == "wrong_day":
        return f"{name} doesn't play on {WEEKDAYS[int(d.get('weekday', 1)) - 1]}s."
    if reason == "outside_hours":
        return f"{name} only plays {windows_text(d.get('windows', ()))}."
    if reason == "wrong_show":
        return f"{name} is only for certain shows, and this break isn't in one of them."
    if reason == "house_standby":
        return f"{name} is a station promo. It only fills a break that would otherwise be empty."
    if reason == "commercial_ack_needed":
        return f"{name} is a paid spot. It waits until you confirm you're allowed to run paid spots."
    if reason == "voice_rights_needed":
        return f"{name} is a spoken message. It waits until you confirm you have the right to use the voice."
    if reason == "no_creative":
        return f"{name} has nothing to play yet. Add a recording or a written message."
    if reason == "timezone_unknown":
        return f"{name} uses a timezone this computer doesn't know. Edit the spot and pick another."
    if reason == "audio_not_ready":
        return f"The voice recording for {name} isn't made yet."
    if reason == "audio_missing":
        return f"The recording for {name} is no longer in your library. Pick another recording."
    if reason == "audio_unavailable":
        return f"The recording for {name} can't be played right now. Check it in the Library."
    if reason == "plan_met":
        n = d.get("n")
        times = "the 1 time" if n == 1 else (f"all {n} times" if isinstance(n, int) else "all the times")
        if d.get("scope") == "day":
            return f"{name} has already played {times} planned for today."
        return f"{name} has already played {times} it was booked for."
    if reason in ("hour_cap", "day_cap", "week_cap"):
        span = {"hour_cap": "in the last hour", "day_cap": "today", "week_cap": "this week"}[reason]
        n = int(d.get("played", 0))
        return f"{name} already played {n} {plural(n, 'time')} {span} (limit {d.get('limit')})."
    if reason == "too_soon_campaign":
        ahead = bool(d.get("ahead"))
        gap = distance_text(d.get("seconds", 0), ahead)
        return f"{name} {'is planned' if ahead else 'played'} {gap}. It waits at least {d.get('gap')} {plural(int(d.get('gap', 0)), 'minute')} between plays."
    if reason == "too_soon_category":
        ahead = bool(d.get("ahead"))
        gap = distance_text(d.get("seconds", 0), ahead)
        return (f"A similar spot ({d.get('other')}) {'is planned' if ahead else 'played'} {gap}. "
                f"{name} waits at least {d.get('gap')} {plural(int(d.get('gap', 0)), 'minute')} from the same kind of spot.")
    if reason == "ahead_of_pace":
        return f"{name} is ahead of its even pace, so it waits."
    if reason == "no_room":
        return f"{name} needs {seconds_text(d.get('need', 0))} but only {seconds_text(d.get('left', 0))} are left in this break."
    if reason == "break_full":
        return f"The break already has the most spots allowed ({d.get('n')})."
    if reason == "hour_limit_station":
        played = d.get("played")
        spots = plural(played, "spot") if isinstance(played, int) else "spots"
        return f"The station already played {played} {spots} in the last hour (limit {d.get('limit')})."
    return f"{name} was left out."


def kind_text(item) -> str:
    if item.house:
        return "station promo"
    return "spoken message" if item.kind == "script" else "recorded spot"


def plan_lines(items, skipped, *, limit: int = 8) -> list:
    """The preview of a break, one plain sentence per line."""
    lines = []
    if items:
        total = sum(i.duration for i in items)
        lines.append(f"The next break would play {len(items)} {plural(len(items), 'spot')} "
                     f"({seconds_text(total)}).")
        for item in items:
            guess = " (estimated length)" if item.estimated else ""
            lines.append(f"{item.position}. {item.campaign}: {kind_text(item)}, {seconds_text(item.duration)}{guess}.")
    else:
        lines.append("The next break would have no spots.")
    held = [s for s in skipped if not s.structural]
    quiet = [s for s in skipped if s.structural]
    if not items and not skipped:
        lines.append("You have no spots yet. Add one to fill your breaks.")
    elif not items and not held:
        lines.append("No spot is scheduled for this time.")
    if held:
        lines.append("Left out:" if items else "Why:")
        for s in held[:limit]:
            lines.append(f"- {s.text}")
        if len(held) > limit:
            lines.append(f"- and {len(held) - limit} more.")
    if quiet:
        lines.append(f"{len(quiet)} other {plural(len(quiet), 'spot isn', 'spots aren')}'t scheduled for this time.")
    return lines
