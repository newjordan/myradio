"""The Facts object: the only source of specifics a script may state.

Everything a caller supplies is cleaned here (markup and control characters removed, lengths
capped) before the composer sees it. Missing or unusable values are dropped with a plain-language
warning; nothing is guessed to fill a gap.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
import re
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import HostsError
from .textsafe import neutralize, strip_markup

DAYPARTS = ("overnight", "morning", "afternoon", "evening", "night")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# Longest accepted value per field, in characters.
LIMITS: Dict[str, int] = {
    "station": 80, "tagline": 160, "host": 60,
    "now_title": 120, "now_artist": 120, "next_title": 120, "next_artist": 120,
    "sponsor": 80, "sponsor_copy": 600, "topic": 80, "note": 240, "source": 80,
}
MAX_NOTES = 6
MAX_SOURCES = 8
MAX_ITEM_COUNT = 99

LABELS: Dict[str, str] = {
    "station": "station name", "tagline": "tagline", "host": "host name",
    "now_title": "title of the song that just played", "now_artist": "artist of the song that just played",
    "next_title": "title of the song coming up", "next_artist": "artist of the song coming up",
    "local_time": "time", "daypart": "part of the day", "weekday": "day of the week", "notes": "note",
    "sponsor": "sponsor name", "sponsor_copy": "sponsor message", "topic": "topic", "item_count": "item count",
    "sources": "source name",
}
KNOWN_FIELDS = frozenset(("station", "tagline", "host", "now_title", "now_artist", "next_title", "next_artist",
                          "local_time", "daypart", "weekday", "notes", "sponsor", "sponsor_copy", "topic",
                          "item_count", "sources"))
_STRING_FIELDS = ("station", "tagline", "host", "now_title", "now_artist", "next_title", "next_artist",
                  "sponsor", "sponsor_copy", "topic")
_HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
_DAYPART_ALIASES = {"late night": "night", "late-night": "night", "latenight": "night", "midnight": "overnight",
                    "dawn": "morning", "early morning": "morning", "dusk": "evening", "midday": "afternoon"}

_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def number_words(n: int) -> str:
    """0 to 999 in words, for spoken counts and clock times."""
    if not isinstance(n, int) or isinstance(n, bool) or n < 0 or n > 999:
        raise ValueError("number out of range")
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    hundreds, rest = divmod(n, 100)
    return f"{_ONES[hundreds]} hundred" + (f" and {number_words(rest)}" if rest else "")


def daypart_for(hour: int) -> str:
    if hour < 5:
        return "overnight"
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 21:
        return "evening"
    return "night"


def greeting_for(daypart: str) -> str:
    return {"morning": "Good morning", "afternoon": "Good afternoon", "evening": "Good evening",
            "night": "Good evening"}.get(daypart, "Hello")


def parse_time(value: object) -> Optional[Tuple[int, int]]:
    if isinstance(value, str):
        match = _HHMM.fullmatch(value.strip())
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _clock_words(hour: int, minute: int) -> str:
    """The clock face as people say it, without the part of the day: 'nine oh seven', 'a quarter to ten'."""
    if hour == 0 and minute == 0:
        return "midnight"
    if hour == 12 and minute == 0:
        return "noon"
    h12 = hour % 12 or 12
    following = (hour + 1) % 12 or 12
    if minute == 0:
        return f"{number_words(h12)} o'clock"
    if minute == 15:
        return f"a quarter past {number_words(h12)}"
    if minute == 30:
        return f"half past {number_words(h12)}"
    if minute == 45:
        return f"a quarter to {number_words(following)}"
    if minute < 10:
        return f"{number_words(h12)} oh {number_words(minute)}"
    return f"{number_words(h12)} {number_words(minute)}"


def spoken_time(hour: int, minute: int) -> str:
    """A 24-hour clock time as people say it: 'nine oh seven at night', 'a quarter past six in the morning'."""
    core = _clock_words(hour, minute)
    if core in ("midnight", "noon"):
        return core
    if hour < 12:
        part = "in the morning"
    elif hour < 17:
        part = "in the afternoon"
    elif hour < 21:
        part = "in the evening"
    else:
        part = "at night"
    return f"{core} {part}"


def time_numbers(hour: int, minute: int) -> List[str]:
    """Numbers a spoken version of this time may contain (as plain decimal strings)."""
    h12 = hour % 12 or 12
    return sorted({str(hour), str(h12), str(minute), str((hour + 1) % 12 or 12)} - {"0"})


@dataclasses.dataclass(frozen=True)
class Facts:
    station: str = ""
    tagline: str = ""
    host: str = ""
    now_title: str = ""
    now_artist: str = ""
    next_title: str = ""
    next_artist: str = ""
    local_time: str = ""          # "HH:MM", 24-hour, in the station's own time zone
    daypart: str = ""             # one of DAYPARTS
    weekday: str = ""             # "Monday" ... "Sunday"
    notes: Tuple[str, ...] = ()   # operator-written notes
    sponsor: str = ""
    sponsor_copy: str = ""
    topic: str = ""
    item_count: Optional[int] = None
    sources: Tuple[str, ...] = ()

    # ---- construction -----------------------------------------------------------
    @classmethod
    def from_any(cls, raw: Any) -> "Tuple[Facts, List[str]]":
        """Clean whatever the caller supplied. Returns (facts, warnings); raises only when ``raw`` is
        not a mapping (or a Facts object) at all.

        A hand-built :class:`Facts` is trusted no more than a mapping: its values go through the same cleaning
        (markers and tags removed, lengths capped). A Facts object that is already clean comes back as the very
        same object, with no warnings, so cleaning it again is free."""
        if raw is None:
            return cls(), []
        if isinstance(raw, Facts):
            given = {f.name: getattr(raw, f.name, None) for f in dataclasses.fields(Facts)}
            cleaned, warnings = cls._from_mapping(given)
            if cleaned == raw and not warnings:
                return raw, []
            return cleaned, warnings
        if not isinstance(raw, Mapping):
            raise HostsError("The facts should be labelled details, for example station and next_title.")
        return cls._from_mapping(raw)

    @classmethod
    def _from_mapping(cls, raw: Mapping) -> "Tuple[Facts, List[str]]":
        warnings: List[str] = []
        values: Dict[str, Any] = {}
        keys = []
        for key in raw:
            keys.append(key)
            if len(keys) >= 400:
                break
        unknown = [str(k)[:30] for k in keys if k not in KNOWN_FIELDS]
        if unknown:
            warnings.append("Some facts were not recognised and were ignored: " + ", ".join(sorted(set(unknown))[:6]) + ".")
        for name in _STRING_FIELDS:
            values[name] = _clean_field(name, raw.get(name), warnings)
        values["notes"] = _clean_list("notes", "note", raw.get("notes"), MAX_NOTES, warnings)
        values["sources"] = _clean_list("sources", "source", raw.get("sources"), MAX_SOURCES, warnings)
        # time of day
        local_time = ""
        parsed = None
        if raw.get("local_time") not in (None, ""):
            parsed = parse_time(raw.get("local_time"))
            if parsed is None:
                warnings.append("The time should look like 21:07 (hours and minutes on a 24-hour clock), so it was left out.")
            else:
                local_time = f"{parsed[0]:02d}:{parsed[1]:02d}"
        values["local_time"] = local_time
        daypart = ""
        given = raw.get("daypart")
        if isinstance(given, str) and given.strip():
            candidate = given.strip().lower()[:30]
            candidate = _DAYPART_ALIASES.get(candidate, candidate)
            if candidate in DAYPARTS:
                daypart = candidate
            else:
                warnings.append("The part of the day should be one of: " + ", ".join(DAYPARTS) + ".")
        elif given not in (None, ""):
            warnings.append("The part of the day was not text, so it was left out.")
        if not daypart and parsed is not None:
            daypart = daypart_for(parsed[0])
        values["daypart"] = daypart
        values["weekday"] = _clean_weekday(raw.get("weekday"), warnings)
        count = raw.get("item_count")
        values["item_count"] = None
        if count is not None:
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= MAX_ITEM_COUNT:
                values["item_count"] = count
            else:
                warnings.append(f"The item count should be a whole number from 0 to {MAX_ITEM_COUNT}, so it was left out.")
        return cls(**values), _dedupe(warnings)[:12]

    # ---- views ---------------------------------------------------------------------
    def present(self) -> Dict[str, Any]:
        """Only the fields that have a value."""
        out: Dict[str, Any] = {}
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if value not in ("", (), None):
                out[field.name] = value
        return out

    def texts(self) -> List[str]:
        """Every piece of text the script is allowed to draw words from."""
        out: List[str] = []
        for name, value in self.present().items():
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, tuple):
                out.extend(value)
        return out

    def extra_numbers(self) -> List[str]:
        """Numbers that follow from the facts without appearing in their text."""
        out: List[str] = []
        parsed = parse_time(self.local_time)
        if parsed:
            out.extend(time_numbers(*parsed))
        if self.item_count is not None:
            out.append(str(self.item_count))
        return out

    def with_host(self, name: str) -> "Facts":
        return self if self.host else dataclasses.replace(self, host=name)

    def used_in(self, text: str, *, host: bool = True) -> Tuple[str, ...]:
        """Names of the fields whose values appear in ``text``, in field order.

        Text values match case-insensitively as whole words; a list field counts when any of its entries
        appears; the time counts when it is said ("nine oh seven") or written ("9:07"); the item count counts
        as a digit or a word. ``host`` False leaves out the host's name (it is only the caller's fact when the
        caller supplied it)."""
        if not isinstance(text, str) or not text.strip():
            return ()
        haystack = unicodedata.normalize("NFKC", strip_markup(text)).lower()
        found: List[str] = []
        for field in dataclasses.fields(self):
            name = field.name
            value = getattr(self, name)
            if value in ("", (), None) or (name == "host" and not host):
                continue
            if name == "local_time":
                needles = _time_forms(value)
            elif name == "item_count":
                counted = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 999
                needles = [str(value)] + ([number_words(value)] if counted else [])
            elif isinstance(value, tuple):
                needles = list(value)
            else:
                needles = [value]
            if any(_has_phrase(haystack, needle) for needle in needles):
                found.append(name)
        return tuple(found)


# ---- helpers ----------------------------------------------------------------------------------
def _has_phrase(haystack: str, needle: object) -> bool:
    """True when ``needle`` occurs in the (already lower-cased) ``haystack`` as whole words."""
    if not isinstance(needle, str):
        return False
    needle = unicodedata.normalize("NFKC", needle).lower().strip()
    if not needle:
        return False
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack) is not None


def _time_forms(local_time: str) -> List[str]:
    """Ways a clock time can appear in a script: as said in words, or as digits."""
    parsed = parse_time(local_time)
    if parsed is None:
        return [local_time]
    hour, minute = parsed
    h12 = hour % 12 or 12
    forms = [local_time, f"{hour}:{minute:02d}", f"{h12}:{minute:02d}", spoken_time(hour, minute).lower(),
             _clock_words(hour, minute)]
    return list(dict.fromkeys(forms))


def _flag_message(label: str, flags: Iterable[str]) -> Optional[str]:
    flags = set(flags)
    if "not_text" in flags:
        return f"The {label} was not text, so it was left out."
    if "empty" in flags:
        if "markup" in flags:
            return f"The {label} held only voice codes, so it was left out."
        return f"The {label} had nothing that can be spoken, so it was left out."
    parts = []
    if "markup" in flags:
        parts.append("voice codes were removed")
    if "control" in flags:
        parts.append("hidden characters were removed")
    if "shortened" in flags:
        parts.append("it was shortened")
    if parts:
        return f"The {label}: " + " and ".join(parts) + "."
    return None


def _clean_field(name: str, value: Any, warnings: List[str]) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        warnings.append(f"The {LABELS[name]} was not text, so it was left out.")
        return ""
    if isinstance(value, (int, float)):
        if (isinstance(value, float) and not math.isfinite(value)) or (isinstance(value, int) and abs(value) >= 10 ** 18):
            warnings.append(f"The {LABELS[name]} was not a usable number, so it was left out.")
            return ""
        value = str(value)
    cleaned = neutralize(value, LIMITS[name])
    message = _flag_message(LABELS[name], cleaned.flags)
    if message:
        warnings.append(message)
    return cleaned.text


def _clean_list(name: str, singular: str, value: Any, limit: int, warnings: List[str]) -> Tuple[str, ...]:
    if value is None or value == "" or value == ():
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        warnings.append(f"The {LABELS[name]} list was not a list of text, so it was left out.")
        return ()
    out: List[str] = []
    more = False
    for index, item in enumerate(value):
        if index >= limit * 4 + 16:      # bound the work on absurdly long lists
            more = True
            break
        cleaned = neutralize(item, LIMITS[singular])
        message = _flag_message(LABELS[name], cleaned.flags)
        if message:
            warnings.append(message)
        if cleaned.text and cleaned.text not in out:
            if len(out) >= limit:
                more = True
                break
            out.append(cleaned.text)
    if more:
        warnings.append(f"Only the first {limit} {LABELS[name]}s were used.")
    return tuple(out)


def _dedupe(items: List[str]) -> List[str]:
    return list(dict.fromkeys(items))


def _clean_weekday(value: Any, warnings: List[str]) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        warnings.append("The day of the week was not usable, so it was left out.")
        return ""
    if isinstance(value, int) and 1 <= value <= 7:
        return WEEKDAYS[value - 1]
    if isinstance(value, str):
        text = value.strip().lower()[:12]
        if text.isdigit() and 1 <= int(text) <= 7:
            return WEEKDAYS[int(text) - 1]
        for name in WEEKDAYS:
            if text == name.lower() or (len(text) >= 3 and name.lower().startswith(text) and len(text) <= 4):
                return name
    warnings.append("The day of the week should be a name such as Tuesday, or a number from 1 (Monday) to 7 (Sunday).")
    return ""


def facts_from_status(status: Any, *, recent_music: Sequence[Mapping] = (), now: Optional[float] = None,
                      tz: Optional[dt.tzinfo] = None) -> Dict[str, Any]:
    """Build a raw facts mapping from a station status snapshot (and, for the song that just played,
    the latest music the extension saw). Tolerant of missing keys; the result still goes through
    :meth:`Facts.from_any` before it is used."""
    out: Dict[str, Any] = {}
    status = status if isinstance(status, Mapping) else {}
    station = status.get("station") if isinstance(status.get("station"), Mapping) else {}
    if station.get("name"):
        out["station"] = station.get("name")
    if station.get("tagline"):
        out["tagline"] = station.get("tagline")
    current = status.get("current") if isinstance(status.get("current"), Mapping) else None
    if current and current.get("kind") == "music":
        out["now_title"], out["now_artist"] = current.get("title", ""), current.get("artist", "")
    elif recent_music:
        last = recent_music[-1]
        out["now_title"], out["now_artist"] = last.get("title", ""), last.get("artist", "")
    queue = status.get("queue") if isinstance(status.get("queue"), list) else []
    for entry in queue[:50]:
        if isinstance(entry, Mapping) and entry.get("kind") == "music":
            out["next_title"], out["next_artist"] = entry.get("title", ""), entry.get("artist", "")
            break
    moment = dt.datetime.fromtimestamp(time.time() if now is None else now, tz)
    out["local_time"] = f"{moment.hour:02d}:{moment.minute:02d}"
    out["weekday"] = moment.isoweekday()
    return out
