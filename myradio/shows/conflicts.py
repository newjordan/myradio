"""Overlaps, gaps and the day-by-day map of the week.

These all work on the idealized week: seven days of 24 wall-clock hours laid end to end on a circle
(Sunday night runs into Monday morning). Daylight saving only shifts a couple of nights a year and
never creates an overlap, so it is handled where real time is needed (``timeline``), not here.

Messages are written for a person. Every suggested fix carries the exact operation and arguments that
would apply it, so a screen or an agent can offer it as one click.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .labels import DAY_MIN, DAY_NAMES, WEEK_MIN, clock_string, days_label, end_string, time_label
from .model import MIN_LENGTH, Show, ShowError
from .timeline import Occurrence, cover

MAX_PIECES = 2000        # stop looking for more overlaps after this many (damaged data can overlap everywhere)
MAX_REPORTED = 50        # overlaps listed in a report
MAX_WITH_FIXES = 10      # overlaps for which fixes are worked out


def week_intervals(show: Show) -> list:
    """The show's airtime as ``(start, end, rank)`` in minutes from Monday 00:00, split at the end of
    the week. ``rank`` is the minute the whole stretch began (negative for the part that wraps round to
    Monday), which orders two overlapping shows by who started first."""
    out = []
    for day in sorted(show.day_set):
        start = (day - 1) * DAY_MIN + show.start_min
        end = start + show.length_min
        if end <= WEEK_MIN:
            out.append((start, end, start))
        else:
            out.append((start, WEEK_MIN, start))
            out.append((0, end - WEEK_MIN, start - WEEK_MIN))
    return out


@dataclass(frozen=True)
class Overlap:
    """Two shows scheduled at the same time. ``first`` is the one that started earlier."""
    first: Show
    second: Show
    pieces: tuple            # ((start, end, first_rank, second_rank), ...) in week minutes, earliest first

    def groups(self) -> list:
        """Pieces that repeat identically on several days: ``(days, minute_of_day, length)``."""
        found: dict = {}
        for start, end, _, _ in self.pieces:
            found.setdefault((start % DAY_MIN, end - start), set()).add(start // DAY_MIN % 7 + 1)
        return sorted(((tuple(sorted(days)), minute, length) for (minute, length), days in found.items()),
                      key=lambda g: (g[0][0], g[1]))


def find_overlaps(shows: Sequence[Show], max_pieces: int = MAX_PIECES):
    """Return ``(overlaps, truncated)``. Shows are compared on the weekly circle; touching is not overlapping."""
    items = sorted((start, end, show.id, rank) for show in shows for start, end, rank in week_intervals(show))
    by_id = {show.id: show for show in shows}
    pairs: dict = {}
    active: list = []
    found, truncated = 0, False
    for start, end, sid, rank in items:
        active = [t for t in active if t[0] > start]
        for end2, sid2, rank2 in active:
            if sid2 == sid:
                continue
            stop = min(end, end2)
            if stop <= start:
                continue
            key, ranks = ((sid2, sid), (rank2, rank)) if sid2 < sid else ((sid, sid2), (rank, rank2))
            pairs.setdefault(key, []).append((start, stop) + ranks)
            found += 1
            if found >= max_pieces:
                truncated = True
                break
        if truncated:
            break
        active.append((end, sid, rank))
    overlaps = []
    for (id_a, id_b), pieces in pairs.items():
        pieces.sort()
        if len(pieces) > 1 and pieces[0][0] == 0 and pieces[-1][1] == WEEK_MIN:
            head, tail = pieces[0], pieces[-1]       # one overlap that runs across the end of the week
            pieces = [(tail[0], head[1] + WEEK_MIN, tail[2], tail[3])] + pieces[1:-1]
        a_first = (pieces[0][2], id_a) <= (pieces[0][3], id_b)
        first, second = (by_id[id_a], by_id[id_b]) if a_first else (by_id[id_b], by_id[id_a])
        ordered = tuple((s, e, ra, rb) if a_first else (s, e, rb, ra) for s, e, ra, rb in pieces)
        overlaps.append(Overlap(first, second, ordered))
    overlaps.sort(key=lambda o: (o.pieces[0][0], o.first.id, o.second.id))
    return overlaps, truncated


def on_days(label: str) -> str:
    return label if label == "every day" else "on " + label


def _window(minute: int, length: int) -> str:
    return f"from {time_label(minute)} to {time_label(minute + length)}"


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def overlap_message(overlap: Overlap) -> str:
    """'Morning Rise (Every day, 6 am-10 am) and Breakfast Club (...) are both scheduled on weekdays from 9 am to 10 am.'"""
    groups = overlap.groups()
    days, minute, length = groups[0]
    more = " (and at other times too)" if len(groups) > 1 else ""
    return (f"{overlap.first.name} ({overlap.first.when()}) and {overlap.second.name} ({overlap.second.when()}) "
            f"are both scheduled {on_days(days_label(days))} {_window(minute, length)}{more}.")


def would_overlap_message(candidate: Show, overlap: Overlap, fixes: Sequence[dict], *, extra: int = 0) -> str:
    """The message shown when saving would create an overlap."""
    other = overlap.second if overlap.first.id == candidate.id else overlap.first
    groups = overlap.groups()
    days, minute, length = groups[0]
    more = " (and at other times too)" if len(groups) > 1 else ""
    text = (f"{candidate.name} would overlap {other.name} ({other.when()}) {on_days(days_label(days))} "
            f"{_window(minute, length)}{more}.")
    tips = [fix["text"] for fix in fixes if fix["operation"] == "shows.save"][:2]
    if len(tips) == 2:
        text += f" {tips[0]} Or {_lower_first(tips[1])}"
    elif tips:
        text += f" {tips[0]}"
    else:
        text += " Shorten one of them, or move one to different days."
    if extra:
        text += f" It also overlaps {extra} other show{'s' if extra != 1 else ''}."
    return text


def suggest_fixes(week, overlap: Overlap, limit: int = 3) -> list:
    """Changes that clear this overlap, the smallest change first. A change is offered only if the show it changes
    would then overlap nothing at all, which is exactly what ``shows.save`` demands, so every offered fix works as is."""
    first, second = overlap.first, overlap.second
    start, end, rank1, rank2 = overlap.pieces[0]
    candidates = []          # (minutes of airtime a week loses, show, change, plain sentence)
    if rank1 < start and start - rank1 >= MIN_LENGTH:
        lost = (rank1 + first.length_min - start) * len(first.day_set)
        candidates.append((lost, first, {"end": clock_string(start % DAY_MIN or DAY_MIN)},
                           f"Make {first.name} end at {time_label(start)}."))
    if rank2 + second.length_min > end and rank2 + second.length_min - end >= MIN_LENGTH:
        lost = (end - rank2) * len(second.day_set)
        candidates.append((lost, second, {"start": clock_string(end % DAY_MIN)},
                           f"Start {second.name} at {time_label(end)}."))
    for show in (second, first):
        in_conflict = {(piece[3] if show.id == second.id else piece[2]) % WEEK_MIN // DAY_MIN + 1
                       for piece in overlap.pieces}
        remaining = sorted(show.day_set - in_conflict)
        if remaining and in_conflict & show.day_set:
            dropped = in_conflict & show.day_set
            candidates.append((show.length_min * len(dropped), show, {"days": ",".join(map(str, remaining))},
                               f"Stop {show.name} airing {on_days(days_label(dropped))}."))
    candidates.sort(key=lambda c: c[0])                  # stable: equal changes keep the order above
    fixes = []
    for _, show, change, text in candidates:
        try:
            changed = Show.parse(change, show)
        except ShowError:
            continue
        trial, _ = find_overlaps(week.with_show(changed).shows)
        if any(changed.id in (o.first.id, o.second.id) for o in trial):
            continue                                     # saving it would be refused too: it still clashes with some show
        fixes.append({"operation": "shows.save", "arguments": {"show": {"id": show.id, **change}}, "text": text})
        if len(fixes) >= limit - 1:
            break
    fixes.append({"operation": "shows.remove", "arguments": {"id": second.id}, "text": f"Remove {second.name}."})
    return fixes[:limit]


# ---- gaps -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Gap:
    start: int        # minutes from Monday 00:00
    length: int       # minutes; may run past midnight, and past the end of the week


def find_gaps(shows: Sequence[Show]) -> list:
    """Stretches of the weekly circle that no scheduled show covers (the default show plays there)."""
    merged: list = []
    for start, end in sorted((s, e) for show in shows for s, e, _ in week_intervals(show)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if not merged:
        return [Gap(0, WEEK_MIN)]
    gaps, cursor = [], 0
    for start, end in merged:
        if start > cursor:
            gaps.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < WEEK_MIN:
        gaps.append([cursor, WEEK_MIN])
    if len(gaps) > 1 and gaps[0][0] == 0 and gaps[-1][1] == WEEK_MIN:
        gaps[-1][1] = WEEK_MIN + gaps[0][1]           # one gap that runs across the end of the week
        gaps.pop(0)
    return [Gap(s, e - s) for s, e in gaps]


def describe_gaps(gaps: Sequence[Gap], default_name: str, has_shows: bool) -> list:
    """Plain-language entries for the gaps; gaps that repeat on several days are described together."""
    if not has_shows:
        return [{"kind": "gap", "severity": "info", "days": "1,2,3,4,5,6,7", "start": "00:00", "end": "24:00",
                 "minutes": WEEK_MIN,
                 "message": f"No shows are scheduled yet, so {default_name} (your default show) plays all day, every day.",
                 "fixes": [{"operation": "shows.apply_template", "arguments": {"name": "starter"},
                            "text": "Use the ready-made week."}]}]
    grouped: dict = {}
    for gap in gaps:
        grouped.setdefault((gap.start % DAY_MIN, gap.length), []).append(gap.start // DAY_MIN % 7 + 1)
    out = []
    for (minute, length), days in sorted(grouped.items(), key=lambda kv: (min(kv[1]), kv[0])):
        days = sorted(set(days))
        if length >= DAY_MIN:
            end_day = DAY_NAMES[(days[0] - 1 + (minute + length) // DAY_MIN) % 7]
            where = (f"from {DAY_NAMES[days[0] - 1]} at {time_label(minute)} "
                     f"until {end_day} at {time_label(minute + length)}")
        else:
            where = f"{on_days(days_label(days))} {_window(minute, length)}"
        entry = {"kind": "gap", "severity": "info", "days": ",".join(map(str, days)), "start": clock_string(minute),
                 "end": end_string(minute, length), "minutes": length * len(days), "fixes": [],
                 "message": f"Nothing is scheduled {where}, so {default_name} (your default show) plays then."}
        if length <= DAY_MIN:
            entry["fixes"].append({"operation": "shows.save",
                                   "arguments": {"show": {"days": entry["days"], "start": entry["start"], "end": entry["end"]}},
                                   "text": "Add a show for this time (give it a name)."})
        out.append(entry)
    return out


# ---- the whole report -------------------------------------------------------------------------------

def report(week, *, with_fixes: bool = True) -> dict:
    """Everything wrong or worth knowing about the week, in plain language."""
    overlaps, truncated = find_overlaps(week.shows)
    gaps = find_gaps(week.shows)
    entries = []
    for index, overlap in enumerate(overlaps[:MAX_REPORTED]):
        days, minute, length = overlap.groups()[0]
        entries.append({
            "kind": "overlap", "severity": "error", "show_ids": [overlap.first.id, overlap.second.id],
            "days": ",".join(map(str, days)), "start": clock_string(minute), "end": end_string(minute, length),
            "message": overlap_message(overlap),
            "fixes": suggest_fixes(week, overlap) if with_fixes and index < MAX_WITH_FIXES else [],
        })
    gap_minutes = sum(g.length for g in gaps) if week.shows else WEEK_MIN
    return {
        "ok": not overlaps,
        "overlap_count": len(overlaps),
        "overlaps_truncated": truncated or len(overlaps) > MAX_REPORTED,
        "overlaps": entries,
        "gaps": describe_gaps(gaps, week.default.name, bool(week.shows)),
        "gap_hours": round(gap_minutes / 60, 1),
        "summary": _summary(len(overlaps), gap_minutes, bool(week.shows), week.default.name),
    }


def _summary(overlaps: int, gap_minutes: int, has_shows: bool, default_name: str) -> str:
    if overlaps:
        return (f"{overlaps} pair{'s' if overlaps != 1 else ''} of shows overlap. "
                "Shorten one of each pair so only one show is on air at a time.")
    if not has_shows:
        return f"No shows are scheduled yet, so {default_name} plays all day."
    if gap_minutes:
        hours = gap_minutes / 60
        return (f"No overlaps. About {hours:g} hour{'s' if hours != 1 else ''} a week have no show, "
                f"and {default_name} plays then.")
    return "No overlaps, and every hour of the week has a show."


# ---- the day-by-day map ---------------------------------------------------------------------------

def day_view(week) -> list:
    """For each weekday, the blocks that cover its 24 hours: scheduled shows and default-show gaps."""
    occurrences = []
    for show in week.shows:
        for day in sorted(show.day_set):
            start = (day - 1) * DAY_MIN + show.start_min
            end = start + show.length_min
            occurrences.append(Occurrence(start, min(end, WEEK_MIN), show))
            if end > WEEK_MIN:
                occurrences.append(Occurrence(start - WEEK_MIN, end - WEEK_MIN, show))
    occurrences.sort(key=lambda o: (o.start, o.show.id))
    segments = cover(occurrences, week.default, 0, WEEK_MIN)
    head, tail = segments[0], segments[-1]
    wraps = head.show.id == tail.show.id and head.fallback == tail.fallback
    days = []
    for index in range(7):
        low, high = index * DAY_MIN, (index + 1) * DAY_MIN
        blocks = []
        for seg in segments:
            if seg.end <= low or seg.start >= high:
                continue
            a, b = max(seg.start, low), min(seg.end, high)
            blocks.append({
                "show_id": seg.show.id, "fallback": seg.fallback,
                "start_minute": a - low, "end_minute": b - low,
                "start": clock_string(a - low), "end": clock_string(b - low),
                "continues_from_previous_day": seg.start < low or (low == 0 and wraps and seg is head),
                "continues_to_next_day": seg.end > high or (high == WEEK_MIN and wraps and seg is tail),
            })
        days.append({"day": index + 1, "name": DAY_NAMES[index], "blocks": blocks})
    return days
