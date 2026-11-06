"""The researcher: its fields, validation and schedule.

``normalize_researcher`` turns whatever a form or an agent sends into a clean, bounded specification, or raises
``ExtensionError`` with a sentence a non-technical person can act on. Unknown fields are refused so a typo cannot
silently do nothing; the read-only fields a screen echoes back are ignored.
"""
from __future__ import annotations

import random
import re
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlsplit

from ..extensions import ExtensionError
from ..scheduling import next_occurrence, recurrence
from .errors import FetchError
from .selection import compile_source_patterns, source_excluded
from .textutil import canonical_key, clean_text, url_host

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
ID = re.compile(r"[0-9a-f]{32}")
REVIEW_MODES = ("before_air", "auto")
SOURCE_KINDS = ("reported", "commentary", "announcement")
MAX_RESEARCHERS = 50
LIMITS = {
    "researchers": MAX_RESEARCHERS, "sources": 20, "interests": 20, "questions": 10, "exclude_topics": 30, "exclude_sources": 30,
    "tone_words": 50, "tone_allow": 50, "name_chars": 80, "purpose_chars": 300, "interest_chars": 80, "question_chars": 200,
    "topic_chars": 80, "source_pattern_chars": 200, "tone_chars": 60, "source_name_chars": 80,
    "freshness_hours": [1, 720], "segment_seconds": [15, 600], "every_hours": [1, 168],
}
WRITABLE = frozenset("""id name purpose interests questions sources exclude_topics exclude_sources cadence freshness_hours
segment_seconds anchor review tone_filter tone_words tone_allow paused""".split())
READ_ONLY = frozenset("""created_at created_ts updated_at updated_ts next_run next_run_at next_run_ts last_run state pending_review
version cadence_text consecutive_errors""".split())
WEEKDAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


# Parts of a web address's query that usually carry a password or key. The words are matched as whole words of the name
# (api_key, access-token) or, for the long ones, anywhere in it (accessToken), so an ordinary name such as "keyword" is not flagged.
_SECRET_WORDS = frozenset("key apikey auth authorization sig signature password passwd pwd secret token access session sid jwt credential credentials".split())
_SECRET_PARTS = ("token", "secret", "password", "passwd", "apikey", "credential")


def sensitive_sources(urls) -> list:
    """The hosts of the addresses whose query holds something that looks like a password or key."""
    found = []
    for url in urls:
        try:
            pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
        except ValueError:
            continue
        for name, value in pairs:
            lowered = name.lower()
            words = set(re.split(r"[^a-z0-9]+", lowered))
            if value and (words & _SECRET_WORDS or any(part in lowered for part in _SECRET_PARTS)):
                host = url_host(url)
                if host and host not in found:
                    found.append(host)
                break
    return found


def _text(value, label: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ExtensionError(f"{label} must be text.")
    text = clean_text(value)
    if required and not text:
        raise ExtensionError(f"{label} is needed.")
    if len(text) > limit:
        raise ExtensionError(f"{label} can be at most {limit} characters.")
    return text


def _phrases(value, label: str, *, max_items: int, max_chars: int, required: bool = False, split_commas: bool = True) -> list:
    if value is None:
        value = []
    if isinstance(value, str):
        value = re.split(r"[\n;,]+" if split_commas else r"[\n;]+", value)
    if not isinstance(value, list):
        raise ExtensionError(f"{label} must be a list.")
    out, seen = [], set()
    for entry in value:
        if not isinstance(entry, str):
            raise ExtensionError(f"Each item in {label} must be text.")
        text = clean_text(entry)
        if not text:
            continue
        if len(text) > max_chars:
            raise ExtensionError(f"Each item in {label} can be at most {max_chars} characters.")
        if text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    if len(out) > max_items:
        raise ExtensionError(f"{label} can have at most {max_items} items.")
    if required and not out:
        raise ExtensionError(f"Add at least one item to {label}.")
    return out


def _integer(value, label: str, low: int, high: int, default: int, unit: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExtensionError(f"{label} must be a whole number of {unit}, from {low} to {high}.")
    if not low <= value <= high:
        raise ExtensionError(f"{label} must be between {low} and {high} {unit}.")
    return value


def _sources(value, validate_url: Callable[[str], str]) -> list:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ExtensionError("Add at least one website or feed address for this researcher to read.")
    if len(value) > LIMITS["sources"]:
        raise ExtensionError(f"A researcher can read at most {LIMITS['sources']} sources.")
    out, seen = [], set()
    for entry in value:
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict) or set(entry) - {"url", "name", "kind"}:
            raise ExtensionError("Each source needs an address, and can have a name and a kind.")
        raw = entry.get("url")
        if not isinstance(raw, str) or not raw.strip():
            raise ExtensionError("Each source needs a web address.")
        try:
            url = validate_url(raw)
        except FetchError as error:
            raise ExtensionError(error.message) from None
        key = canonical_key(url)
        if key in seen:
            continue
        seen.add(key)
        kind = entry.get("kind")
        if kind in (None, ""):
            kind = None
        elif kind not in SOURCE_KINDS:
            raise ExtensionError("A source's kind must be reported, commentary or announcement, or left out.")
        out.append({"url": url, "name": _text(entry.get("name"), "A source's name", LIMITS["source_name_chars"]), "kind": kind})
    return out


def _cadence(value) -> dict:
    if value is None:
        return {"type": "every_hours", "hours": 24}
    if not isinstance(value, dict):
        raise ExtensionError("Choose how often this researcher runs.")
    kind = value.get("type")
    if kind == "every_hours":
        if set(value) - {"type", "hours"}:
            raise ExtensionError("An 'every N hours' schedule only needs the number of hours.")
        low, high = LIMITS["every_hours"]
        return {"type": "every_hours", "hours": _integer(value.get("hours"), "Hours between runs", low, high, 24, "hours")}
    if kind == "daily":
        if set(value) - {"type", "time", "timezone", "weekdays"}:
            raise ExtensionError("A daily schedule needs a time, a time zone and, if you like, the weekdays.")
        weekdays = value.get("weekdays", "1,2,3,4,5,6,7")
        if isinstance(weekdays, list) and all(isinstance(d, int) and not isinstance(d, bool) for d in weekdays):
            weekdays = ",".join(map(str, weekdays))
        try:
            _, _, days = recurrence(value.get("timezone"), value.get("time"), weekdays)
        except ValueError as error:
            raise ExtensionError(str(error)) from None
        return {"type": "daily", "time": value["time"], "timezone": value["timezone"], "weekdays": ",".join(map(str, days))}
    raise ExtensionError("Choose either 'every N hours' or 'daily at a time'.")


def cadence_text(cadence: dict) -> str:
    """The schedule in plain words."""
    if cadence["type"] == "every_hours":
        hours = cadence["hours"]
        return "Every hour" if hours == 1 else f"Every {hours} hours"
    days = [int(d) for d in cadence["weekdays"].split(",")]
    if days == [1, 2, 3, 4, 5, 6, 7]:
        when = "Every day"
    elif days == [1, 2, 3, 4, 5]:
        when = "Weekdays"
    elif days == [6, 7]:
        when = "Weekends"
    else:
        when = ", ".join(WEEKDAY_NAMES[d] for d in days)
    return f"{when} at {cadence['time']} ({cadence['timezone']})"


def normalize_researcher(raw, validate_url: Callable[[str], str]) -> dict:
    """A clean specification (without id or run state). Raises ``ExtensionError``."""
    if not isinstance(raw, dict):
        raise ExtensionError("Send the researcher as an object with a name, interests and sources.")
    unknown = set(raw) - WRITABLE - READ_ONLY
    if unknown:
        raise ExtensionError(f"The setting \u201c{clean_text(str(sorted(unknown, key=str)[0]), 40)}\u201d is not recognised. Check the spelling, or leave it out.")
    name = _text(raw.get("name"), "The name", LIMITS["name_chars"], required=True)
    low, high = LIMITS["freshness_hours"]
    seg_low, seg_high = LIMITS["segment_seconds"]
    anchor = raw.get("anchor")
    if anchor in (None, ""):
        anchor = None
    elif not isinstance(anchor, str) or not SLUG.fullmatch(anchor):
        raise ExtensionError("The anchor must be a host's short name made of lowercase letters, numbers and dashes.")
    review = raw.get("review", "before_air")
    if review not in REVIEW_MODES:
        raise ExtensionError("Review must be 'before_air' (you approve each update) or 'auto' (updates can be used without your review).")
    tone_filter = raw.get("tone_filter", True)
    if not isinstance(tone_filter, bool):
        raise ExtensionError("The tone filter is either on or off.")
    spec = {
        "name": name,
        "purpose": _text(raw.get("purpose"), "The purpose", LIMITS["purpose_chars"]),
        "interests": _phrases(raw.get("interests"), "Interests", max_items=LIMITS["interests"], max_chars=LIMITS["interest_chars"], required=True),
        "questions": _phrases(raw.get("questions"), "Questions", max_items=LIMITS["questions"], max_chars=LIMITS["question_chars"], split_commas=False),
        "sources": _sources(raw.get("sources"), validate_url),
        "exclude_topics": _phrases(raw.get("exclude_topics"), "Topics to skip", max_items=LIMITS["exclude_topics"], max_chars=LIMITS["topic_chars"]),
        "exclude_sources": _phrases(raw.get("exclude_sources"), "Sources to skip", max_items=LIMITS["exclude_sources"], max_chars=LIMITS["source_pattern_chars"]),
        "cadence": _cadence(raw.get("cadence")),
        "freshness_hours": _integer(raw.get("freshness_hours"), "How recent items must be", low, high, 24, "hours"),
        "segment_seconds": _integer(raw.get("segment_seconds"), "The segment length", seg_low, seg_high, 60, "seconds"),
        "anchor": anchor,
        "review": review,
        "tone_filter": tone_filter,
        "tone_words": _phrases(raw.get("tone_words"), "Extra tone words", max_items=LIMITS["tone_words"], max_chars=LIMITS["tone_chars"]),
        "tone_allow": _phrases(raw.get("tone_allow"), "Allowed words", max_items=LIMITS["tone_allow"], max_chars=LIMITS["tone_chars"]),
    }
    patterns = compile_source_patterns(spec["exclude_sources"])
    for source in spec["sources"]:
        hit = source_excluded(patterns, source_url=source["url"], source_name=source["name"], item_url=None)
        if hit:
            raise ExtensionError(f"You chose {url_host(source['url'])} as a source and also asked to skip \u201c{hit}\u201d. Remove one of them.")
    if "paused" in raw and not isinstance(raw["paused"], bool):
        raise ExtensionError("Paused is either true or false.")
    notes = []
    for host in sensitive_sources(source["url"] for source in spec["sources"])[:3]:
        notes.append(f"The address for {host} has a value in it that looks like a password or key. The station keeps addresses exactly as typed, "
                     "shows them on screen and includes them in backups, so only use an address like this if you are comfortable with that.")
    if spec["cadence"]["type"] == "every_hours" and spec["freshness_hours"] < spec["cadence"]["hours"]:
        notes.append("Items must be newer than the time between runs, so some stories may be missed. Raise how recent items can be, or run more often.")
    spec["_notes"] = notes
    return spec


def public_spec(spec: dict) -> dict:
    return {k: v for k, v in spec.items() if not k.startswith("_")}


# ---- scheduling ------------------------------------------------------------------------------------

def nominal_next(cadence: dict, *, now: float, last_start: Optional[float] = None) -> float:
    """When the researcher is next due, before jitter.

    Every-N-hours: N hours after the last start, or right away if it has never run or is overdue.
    Daily: the next wall-clock occurrence strictly after ``now`` in the named zone (spring-forward gaps are skipped,
    autumn repeats run once), exactly as the station's own recurring programs behave.
    """
    if cadence["type"] == "every_hours":
        if last_start is None:
            return now
        return max(now, last_start + cadence["hours"] * 3600)
    try:
        return next_occurrence(cadence["timezone"], cadence["time"], cadence["weekdays"], now)
    except ValueError:
        return now + 24 * 3600


def jitter_seconds(seed: str, spread: float) -> float:
    """Deterministic per-researcher offset in [0, spread) so runs do not all land on the same second."""
    return random.Random(seed).uniform(0.0, spread) if spread > 0 else 0.0


def plausible_next(cadence: dict, next_run: Optional[float], now: float) -> bool:
    """False when a stored due time cannot have come from this schedule (the clock was set back, or the file is odd)."""
    if next_run is None:
        return False
    horizon = cadence["hours"] * 3600 + 3600 if cadence["type"] == "every_hours" else 8 * 86400
    return next_run <= now + horizon + 120
