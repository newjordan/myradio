"""The show model: validation and normalization.

Every ``Show`` is valid by construction. ``Show.parse`` accepts the friendly shapes people and agents
send (hour numbers, ``6:30``, day lists, empty strings for "none") and produces the canonical form;
the dataclass itself refuses anything that is not canonical.
"""
from __future__ import annotations

import dataclasses
import re
import unicodedata
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Optional

from ..extensions import ExtensionError
from .clocks import DEFAULT_TEMPLATE, TALK_LEVELS, TEMPLATES
from .labels import (DAY_MIN, capitalize, clock_string, days_label, duration_label, end_string,
                     range_label)

DEFAULT_SHOW_ID = "default"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
COLOR_RE = re.compile(r"#[0-9a-f]{6}")
TIME_RE = re.compile(r"([0-9]{1,2}):([0-9]{2})")

MAX_SHOWS = 200          # scheduled shows, not counting the default show
MAX_NAME = 40
MIN_LENGTH = 15          # minutes
MAX_STYLE_ITEMS = 32
ALL_DAYS = "1,2,3,4,5,6,7"
BAD_NAME_CHARS = frozenset("<>[]{}|\\")
AUTO_COLORS = ("#3b4a63", "#8a4d3a", "#3f6050", "#7a5c26", "#454a7a", "#5a4468", "#2f5560", "#6b4a4a")
DAY_ALIASES = {"everyday": (1, 2, 3, 4, 5, 6, 7), "every day": (1, 2, 3, 4, 5, 6, 7), "daily": (1, 2, 3, 4, 5, 6, 7),
               "weekdays": (1, 2, 3, 4, 5), "weekends": (6, 7)}
INPUT_FIELDS = ("id", "name", "days", "start", "end", "style", "host_id", "talk", "spots_allowed",
                "updates_allowed", "update_researcher_id", "color", "clock")
# Values the API sends back. They are ignored on input so a show can be sent back unchanged.
READ_ONLY_FIELDS = ("start_minute", "duration_minutes", "crosses_midnight", "when", "is_default")


class ShowError(ExtensionError):
    """A problem the operator can fix. The message is written for a person."""


# ---- field cleaners -------------------------------------------------------------------------------

def clean_name(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 4000:
        raise ShowError("Give the show a name of up to %d characters." % MAX_NAME)
    text = " ".join(value.split())
    if not text:
        raise ShowError("Give the show a name.")
    if len(text) > MAX_NAME:
        raise ShowError(f"Show names can be up to {MAX_NAME} characters. This one is {len(text)}.")
    for ch in text:
        if ch in BAD_NAME_CHARS or unicodedata.category(ch).startswith("C"):
            raise ShowError("Show names can use letters, numbers, spaces and simple punctuation, "
                            "but not control characters or the symbols < > [ ] { } | \\.")
    return text


def clean_slug(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ShowError(f"The {what} must be text made of lowercase letters, numbers and hyphens.")
    text = value.strip().lower()
    if not SLUG_RE.fullmatch(text):
        raise ShowError(f"The {what} can use lowercase letters, numbers and hyphens (up to 63 characters), "
                        "and must start with a letter or number.")
    return text


def clean_optional_slug(value: Any, what: str) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return clean_slug(value, what)


def clean_days(value: Any) -> str:
    hint = "Choose at least one day. Use numbers where Monday is 1 and Sunday is 7, for example 1,2,3,4,5 for weekdays."
    days: set = set()
    if isinstance(value, str):
        text = value.strip().lower()
        if len(text) > 40:
            raise ShowError(hint)
        if text in DAY_ALIASES:
            days = set(DAY_ALIASES[text])
        else:
            parts = [p.strip() for p in text.split(",")]
            if not parts or not all(re.fullmatch(r"[1-7]", p) for p in parts):
                raise ShowError(hint)
            days = {int(p) for p in parts}
    elif isinstance(value, (list, tuple)):
        if not 0 < len(value) <= 14:
            raise ShowError(hint)
        for item in value:
            if isinstance(item, str) and re.fullmatch(r"[1-7]", item.strip()):
                item = int(item.strip())                       # ["1", "2"] is as good as [1, 2]
            if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 7:
                raise ShowError(hint)
            days.add(item)
    else:
        raise ShowError(hint)
    return ",".join(str(d) for d in sorted(days))


def clean_level(value: Any, what: str) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ShowError(f"{capitalize(what)} must be a whole number from 0 to 100.")
    return value


def clean_slug_list(value: Any, what: str) -> tuple:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > MAX_STYLE_ITEMS:
        raise ShowError(f"Pick up to {MAX_STYLE_ITEMS} {what}, as a list.")
    seen: list = []
    for item in value:
        text = clean_slug(item, f"{what[:-1] if what.endswith('s') else what} name")
        if text not in seen:
            seen.append(text)
    return tuple(seen)


def clean_color(value: Any) -> str:
    if isinstance(value, str) and COLOR_RE.fullmatch(value.strip().lower()):
        return value.strip().lower()
    raise ShowError("Choose a color written like #3b4a63.")


def clean_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ShowError(f"{capitalize(what)} must be true or false.")
    return value


def clean_talk(value: Any) -> str:
    if not isinstance(value, str) or value.strip().lower() not in TALK_LEVELS:
        raise ShowError("Talk breaks must be rare, some or often.")
    return value.strip().lower()


def clean_clock(value: Any) -> str:
    if not isinstance(value, str) or value.strip().lower() not in TEMPLATES:
        raise ShowError("Choose an hour style: " + ", ".join(TEMPLATES) + ".")
    return value.strip().lower()


def parse_minute(value: Any, what: str) -> int:
    """A start (00:00 to 23:59) or an end (00:00 to 24:00) as minutes. Whole numbers are hours."""
    start = what == "start"
    hint = ("Use a start time like 06:00 or 6:30 (or an hour from 0 to 23)." if start
            else "Use an end time like 10:00 or 22:30 (or an hour from 1 to 24). 24:00 means midnight at the end of the day.")
    if isinstance(value, bool):
        raise ShowError(hint)
    if isinstance(value, int):
        hour, minute = value, 0
    elif isinstance(value, str) and len(value) <= 16:
        match = TIME_RE.fullmatch(value.strip())
        if not match:
            raise ShowError(hint)
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        raise ShowError(hint)
    if not (0 <= minute <= 59 and 0 <= hour <= 24) or (hour == 24 and (minute != 0 or start)):
        raise ShowError(hint)
    return hour * 60 + minute


def slugify(name: str, taken=()) -> str:
    """A readable unique id for a new show: 'Morning Rise' becomes 'morning-rise', then 'morning-rise-2'."""
    folded = "".join(c if ord(c) < 128 else ("" if unicodedata.category(c).startswith("M") else " ")
                     for c in unicodedata.normalize("NFKD", name)).lower()      # accents drop, other symbols become breaks
    base = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:40].strip("-") or "show"
    if base == DEFAULT_SHOW_ID:
        base = "show"
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


# ---- value types ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Style:
    """Music filters for a show. Empty lists mean "anything"."""
    palettes: tuple = ()
    tags: tuple = ()
    energy: int = 50
    adventurousness: int = 40
    vocals: int = 30

    def __post_init__(self):
        if (not isinstance(self.palettes, tuple) or not isinstance(self.tags, tuple)
                or clean_slug_list(list(self.palettes), "palettes") != self.palettes
                or clean_slug_list(list(self.tags), "tags") != self.tags):
            raise ShowError("Music styles and tags must be lowercase words without repeats.")
        for name in ("energy", "adventurousness", "vocals"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 100:
                raise ShowError(f"{capitalize(name)} must be a whole number from 0 to 100.")

    def to_dict(self) -> dict:
        return {"palettes": list(self.palettes), "tags": list(self.tags), "energy": self.energy,
                "adventurousness": self.adventurousness, "vocals": self.vocals}


def parse_style(value: Any, base: Style) -> Style:
    if value is None:
        return base
    if not isinstance(value, dict):
        raise ShowError("The music style must be an object with palettes, tags, energy, adventurousness and vocals.")
    unknown = sorted(str(k) for k in value if k not in ("palettes", "tags", "energy", "adventurousness", "vocals"))
    if unknown:
        raise ShowError("Unknown music style setting: " + ", ".join(unknown[:5]) +
                        ". Known settings are palettes, tags, energy, adventurousness and vocals.")
    return Style(
        palettes=clean_slug_list(value["palettes"], "palettes") if "palettes" in value else base.palettes,
        tags=clean_slug_list(value["tags"], "tags") if "tags" in value else base.tags,
        energy=clean_level(value["energy"], "energy") if "energy" in value else base.energy,
        adventurousness=(clean_level(value["adventurousness"], "adventurousness")
                         if "adventurousness" in value else base.adventurousness),
        vocals=clean_level(value["vocals"], "vocals") if "vocals" in value else base.vocals)


@dataclass(frozen=True)
class Show:
    id: str
    name: str
    days: str                 # canonical ISO weekday list, for example "1,2,3,4,5"
    start_min: int            # minute of the day the show starts, 0..1439 (local wall clock)
    length_min: int           # 15..1440; a show ends this long after it starts, possibly after midnight
    style: Style
    host_id: Optional[str]
    talk: str
    spots_allowed: bool
    updates_allowed: bool
    update_researcher_id: Optional[str]
    color: str
    clock: str = DEFAULT_TEMPLATE
    is_default: bool = False

    def __post_init__(self):
        def need(ok: bool, message: str):
            if not ok:
                raise ShowError(message)

        need(isinstance(self.id, str) and bool(SLUG_RE.fullmatch(self.id)), "The show id is not valid.")
        need(isinstance(self.name, str) and clean_name(self.name) == self.name, "The show name is not valid.")
        need(isinstance(self.days, str) and clean_days(self.days) == self.days, "The show days are not valid.")
        need(type(self.start_min) is int and 0 <= self.start_min < DAY_MIN, "The show start is not valid.")
        need(type(self.length_min) is int and MIN_LENGTH <= self.length_min <= DAY_MIN, "The show length is not valid.")
        need(isinstance(self.style, Style), "The show style is not valid.")
        need(self.host_id is None or (isinstance(self.host_id, str) and bool(SLUG_RE.fullmatch(self.host_id))),
             "The host id is not valid.")
        need(self.talk in TALK_LEVELS, "Talk breaks must be rare, some or often.")
        need(type(self.spots_allowed) is bool and type(self.updates_allowed) is bool and type(self.is_default) is bool,
             "Spots and updates must be true or false.")
        need(self.update_researcher_id is None or (isinstance(self.update_researcher_id, str)
                                                   and bool(SLUG_RE.fullmatch(self.update_researcher_id))),
             "The researcher id is not valid.")
        need(self.update_researcher_id is None or self.updates_allowed, "A researcher needs updates to be switched on.")
        need(isinstance(self.color, str) and bool(COLOR_RE.fullmatch(self.color)), "The show color is not valid.")
        need(self.clock in TEMPLATES, "The hour style is not valid.")
        need(self.is_default == (self.id == DEFAULT_SHOW_ID), "Only the default show can use the id 'default'.")
        if self.is_default:
            need(self.days == ALL_DAYS and self.start_min == 0 and self.length_min == DAY_MIN,
                 "The default show has no start or end.")

    # -- derived -----------------------------------------------------------------------------------
    @cached_property
    def day_set(self) -> frozenset:
        return frozenset(int(d) for d in self.days.split(","))

    @property
    def end_min(self) -> int:
        """The end boundary on the clock, 1..1440 (1440 means midnight)."""
        return (self.start_min + self.length_min) % DAY_MIN or DAY_MIN

    @property
    def crosses_midnight(self) -> bool:
        return self.start_min + self.length_min > DAY_MIN

    def when(self) -> str:
        if self.is_default:
            return "Whenever no other show is scheduled"
        return f"{capitalize(days_label(self.day_set))}, {range_label(self.start_min, self.end_min)}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "days": self.days,
            "start": clock_string(self.start_min), "end": end_string(self.start_min, self.length_min),
            "start_minute": self.start_min, "duration_minutes": self.length_min,
            "crosses_midnight": self.crosses_midnight, "when": self.when(),
            "style": self.style.to_dict(), "host_id": self.host_id, "talk": self.talk,
            "spots_allowed": self.spots_allowed, "updates_allowed": self.updates_allowed,
            "update_researcher_id": self.update_researcher_id, "color": self.color, "clock": self.clock,
            "is_default": self.is_default,
        }

    # -- construction ------------------------------------------------------------------------------
    @classmethod
    def parse(cls, data: Any, base: Optional["Show"] = None, *, taken_ids=(), taken_colors=()) -> "Show":
        """Build a show from what a person or agent sent. Fields left out keep the value in ``base``
        (a new show gets sensible defaults). Raises ``ShowError`` with a plain message."""
        if not isinstance(data, dict):
            raise ShowError("Send the show as an object, for example {\"name\": \"Morning Rise\", \"start\": \"06:00\", \"end\": \"10:00\"}.")
        unknown = sorted(str(k) for k in data if k not in INPUT_FIELDS and k not in READ_ONLY_FIELDS)
        if unknown:
            raise ShowError("Unknown show setting: " + ", ".join(unknown[:5]) + ". Known settings are "
                            + ", ".join(INPUT_FIELDS) + ".")
        if "name" in data:
            name = clean_name(data["name"])
        elif base is not None:
            name = base.name
        else:
            raise ShowError("Give the show a name.")
        if "id" in data:
            show_id = clean_slug(data["id"], "show id")
        elif base is not None:
            show_id = base.id
        else:
            show_id = slugify(name, taken_ids)       # a new show without an id gets one from its name
        if base is not None and show_id != base.id:
            raise ShowError("A show's id cannot be changed. Create a new show instead.")
        if show_id == DEFAULT_SHOW_ID and (base is None or not base.is_default):
            raise ShowError("The id 'default' is kept for the default show.")
        is_default = bool(base and base.is_default)

        if is_default:
            for key in ("days", "start", "end"):
                if key in data and not _same_schedule_value(key, data[key]):
                    raise ShowError("The default show plays whenever no other show is scheduled, so it has no "
                                    "days, start or end. Change its sound, host or talk instead.")
            days, start_min, length = ALL_DAYS, 0, DAY_MIN
        else:
            days = clean_days(data["days"]) if "days" in data else (base.days if base else ALL_DAYS)
            if base is None and not ("start" in data and "end" in data):
                raise ShowError("Say when the show starts and ends, for example start 06:00 and end 10:00.")
            start_min = parse_minute(data["start"], "start") if "start" in data else base.start_min
            end_min = parse_minute(data["end"], "end") if "end" in data else base.end_min
            if end_min == start_min:
                raise ShowError("A show needs to end after it starts. To run until midnight use 24:00, "
                                "and to run past midnight end earlier than you start (for example 22:00 to 02:00).")
            length = end_min - start_min if end_min > start_min else DAY_MIN - start_min + end_min
            if length < MIN_LENGTH:
                raise ShowError(f"A show needs to be at least {MIN_LENGTH} minutes long. "
                                f"{clock_string(start_min)} to {clock_string(end_min)} is {duration_label(length)}.")

        base_style = base.style if base else Style()
        style = parse_style(data.get("style"), base_style) if "style" in data else base_style

        host_id = clean_optional_slug(data["host_id"], "host id") if "host_id" in data else (base.host_id if base else None)
        talk = clean_talk(data["talk"]) if "talk" in data else (base.talk if base else "some")
        spots = clean_bool(data["spots_allowed"], "spots") if "spots_allowed" in data else (base.spots_allowed if base else False)
        updates = (clean_bool(data["updates_allowed"], "updates") if "updates_allowed" in data
                   else (base.updates_allowed if base else False))
        if "update_researcher_id" in data:
            researcher = clean_optional_slug(data["update_researcher_id"], "researcher id")
            if researcher is not None and "updates_allowed" not in data:
                updates = True
            elif researcher is not None and not updates:
                raise ShowError("A researcher is chosen but updates are switched off for this show. "
                                "Switch updates on, or clear the researcher.")
        else:
            researcher = base.update_researcher_id if base else None
            if not updates:
                researcher = None
        if "color" in data:
            color = clean_color(data["color"])
        elif base is not None:
            color = base.color
        else:
            color = next((c for c in AUTO_COLORS if c not in taken_colors), AUTO_COLORS[len(taken_colors) % len(AUTO_COLORS)])
        clock = clean_clock(data["clock"]) if "clock" in data else (base.clock if base else DEFAULT_TEMPLATE)
        return cls(show_id, name, days, start_min, length, style, host_id, talk, spots, updates, researcher,
                   color, clock, is_default)

    def replace(self, **changes) -> "Show":
        return dataclasses.replace(self, **changes)


def _same_schedule_value(key: str, value: Any) -> bool:
    """True when a value sent for the default show's schedule is just the unchanged all-day schedule."""
    try:
        if key == "days":
            return clean_days(value) == ALL_DAYS
        if key == "start":
            return parse_minute(value, "start") == 0
        return parse_minute(value, "end") in (0, DAY_MIN)
    except ShowError:
        return False


def default_show(**changes) -> Show:
    """The built-in default show: it plays whenever nothing else is scheduled."""
    show = Show(id=DEFAULT_SHOW_ID, name="Station Mix", days=ALL_DAYS, start_min=0, length_min=DAY_MIN,
                style=Style((), (), 50, 40, 30), host_id=None, talk="some", spots_allowed=False,
                updates_allowed=False, update_researcher_id=None, color="#4b5a78", clock=DEFAULT_TEMPLATE,
                is_default=True)
    return show.replace(**changes) if changes else show
