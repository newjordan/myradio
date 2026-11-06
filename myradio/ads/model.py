"""Sponsor messages ("spots"): the data model, input checking and the spoken-script rules.

Everything here is plain data and pure functions: no clock, no files and no network. Times are
epoch seconds in UTC; dates and wall times are local to the campaign's IANA timezone.

Words used on screen: a *campaign* is one sponsor (or one station promo) with its rules; a
*creative* is one message it can play, either a recording from the library or a written script
that the speech voice reads; a *flight* is a run of dates with the days, hours and shows it may
play in; a *house* campaign is the station's own promo, used when no sponsor message can play.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import re
import unicodedata
import uuid
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROLE = "spot"  # the shared name for sponsor messages in timelines and generation jobs

ID_RE = re.compile(r"[0-9a-f]{32}")
TRACK_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
SHOW_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
VOICE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CLOCK_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d|24:00")
DAYS_RE = re.compile(r"[1-7](?:,[1-7])*")

MAX_CAMPAIGNS = 200
MAX_CREATIVES = 10
MAX_FLIGHTS = 12
MAX_DAYPARTS = 6
MAX_SHOW_IDS = 50
MAX_NAME = 60
MAX_ADVERTISER = 80
MAX_TEXT = 400
MAX_DISCLOSURE = 160
MAX_FLIGHT_DAYS = 3660
MAX_PLAN_TOTAL = 100000
MAX_PLAN_PER_DAY = 1000

DEFAULT_DISCLOSURE = "This message is sponsored by {sponsor}"
PLACEHOLDER = "{sponsor}"
ALL_DAYS = (1, 2, 3, 4, 5, 6, 7)

# "How often?" on the screen. Explicit caps and gaps always win over a preset.
FREQUENCY_PRESETS = {
    "light": {"per_hour": 1, "per_day": 4, "per_week": 20, "campaign_minutes": 45, "category_minutes": 20},
    "regular": {"per_hour": 2, "per_day": 10, "per_week": 50, "campaign_minutes": 20, "category_minutes": 10},
    "often": {"per_hour": 3, "per_day": 20, "per_week": 100, "campaign_minutes": 10, "category_minutes": 5},
}

TZ_MESSAGE = "Unknown timezone. Use a name like Europe/London or America/New_York, or choose UTC."
DISCLOSURE_HELP = ("The sponsorship line must still say who is paying. Keep the word \"sponsored\" and the sponsor's "
                   "name, for example \"This message is sponsored by {sponsor}\".")


class AdsError(ValueError):
    """A problem the operator can fix. The message is written for a nontechnical reader."""


# ---------------------------------------------------------------------------------------------
# Text rules
# ---------------------------------------------------------------------------------------------

def clean_text(value: Any, label: str, *, maximum: int, minimum: int = 1) -> str:
    """One tidy line of text: invisible formatting marks dropped, control characters and
    line breaks turned into spaces, runs of spaces collapsed."""
    if not isinstance(value, str):
        raise AdsError(f"{label} must be text.")
    if len(value) > maximum * 4 + 64:  # do not spend time tidying something that can only be refused
        raise AdsError(f"{label} is too long. Keep it to {maximum} characters or fewer.")
    kept = []
    for ch in unicodedata.normalize("NFC", value):
        category = unicodedata.category(ch)
        if category == "Cf":
            continue
        kept.append(" " if category[0] == "C" or category in ("Zl", "Zp") else ch)
    text = " ".join("".join(kept).split())
    if len(text) < minimum:
        raise AdsError(f"{label} can't be empty.")
    if len(text) > maximum:
        raise AdsError(f"{label} is too long. Keep it to {maximum} characters or fewer.")
    return text


_TAG = re.compile(r"<\|.{0,80}?\|>", re.S)


def neutralize_tags(text: str) -> str:
    """Make text safe to hand to the speech voice.

    Text from outside the station operator (advertiser copy, sponsor names, track titles) must not
    be able to switch speakers or trigger sound effects. The speech model reads ``<|speaker:N|>``
    markers and bracketed event tags such as ``[laughs]``, so both are defused: speaker markers are
    removed, and square brackets become round ones so a tag is read as ordinary words.
    """
    text = unicodedata.normalize("NFKC", str(text))  # fold full-width look-alikes first
    for _ in range(6):
        folded = _TAG.sub(" ", text)
        if folded == text:
            break
        text = folded
    text = text.replace("<|", " ").replace("|>", " ")
    text = text.replace("[", "(").replace("]", ")")
    return " ".join(text.split())


_NEGATED = re.compile(r"(?:\bnot\b|\bnever\b|\bno\b|\bnon\b|\bwithout\b|\w+n['’]t\b)[\s-]+(?:\w+[\s-]+){0,2}sponsored\b"
                      r"|\bunsponsored\b")


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _name_pattern(name: str) -> "re.Pattern":
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)")


def disclosure_ok(rendered: str, sponsor: str) -> bool:
    """True when the sentence still names the sponsor and still says the message is sponsored.

    The sponsor's own name is taken out first (whole words only), so a name containing
    "sponsored" cannot satisfy the rule on its own, and a sentence that denies sponsorship
    ("not sponsored by ...") is refused."""
    name, text = _fold(sponsor).strip(), _fold(rendered)
    if not name:
        return False
    pattern = _name_pattern(name)
    if not pattern.search(text):
        return False
    rest = pattern.sub(" ", text)
    return bool(re.search(r"\bsponsored\b", rest)) and not _NEGATED.search(rest)


def validate_disclosure(template: Any, sponsor: str) -> str:
    """The stored wording of the sponsorship line. Empty or missing means the default."""
    if template is None or (isinstance(template, str) and not template.strip()):
        return DEFAULT_DISCLOSURE
    text = clean_text(template, "The sponsorship line", maximum=MAX_DISCLOSURE)
    leftover = text.replace(PLACEHOLDER, "")
    if "{" in leftover or "}" in leftover:
        raise AdsError("The sponsorship line can only use {sponsor} between braces.")
    if not disclosure_ok(text.replace(PLACEHOLDER, sponsor), sponsor):
        raise AdsError(DISCLOSURE_HELP)
    return text


def sponsor_name_ok(name: Any) -> bool:
    """True when the voice has a name to read out: once speech tags are defused, some letters or digits are left.
    A name made only of speaker markers or punctuation would leave the sponsorship line with nobody in it."""
    return isinstance(name, str) and any(ch.isalnum() for ch in neutralize_tags(name))


def render_disclosure(template: Any, sponsor: str) -> str:
    """The sentence the voice reads. Falls back to the default wording if the stored wording no
    longer passes the rule (for example after the sponsor's name was edited).

    The default passes for every sponsor name that ``sponsor_name_ok`` accepts. For a name that is empty once speech
    tags are removed, nothing can pass, so the last line has no name in it; saving such a spoken spot is refused and
    ``ads.spot_speech`` will not hand it out, but this function still returns a sentence rather than failing in the
    middle of a break preview."""
    name = neutralize_tags(sponsor)
    for candidate in (template, DEFAULT_DISCLOSURE):
        if isinstance(candidate, str) and candidate.strip():
            line = neutralize_tags(candidate.replace(PLACEHOLDER, name))
            if disclosure_ok(line, name):
                return line if line.endswith((".", "!", "?")) else line + "."
    return f"{DEFAULT_DISCLOSURE.replace(PLACEHOLDER, name)}."


NAMELESS_MESSAGE = ("The voice can't read that name aloud, and a spoken message has to say who is paying. "
                    "Use a name with some letters or numbers in it, for example the name of the business.")
NAMELESS_STATION_MESSAGE = ("The voice can't read this station's name aloud, and a spoken message has to say who is speaking. "
                            "Give the station a name with some letters or numbers in it, then try again.")
HOUSE_NAME_MESSAGE = ("A station promo is named after your station. If a business is paying for this spot, "
                      "turn \"station promo\" off, so it counts as a paid spot.")


def house_name_ok(advertiser: str, station_name: str) -> bool:
    """A station promo may carry only the station's own name (capitals and extra spaces do not matter). Without
    this, a paid sponsor could be filed as a "promo" and skip the paid-use confirmation."""
    try:
        own = clean_text(station_name, "The station name", maximum=MAX_ADVERTISER)
        given = clean_text(advertiser, "Who the spot is for", maximum=MAX_ADVERTISER)
    except AdsError:
        return False
    return _fold(given) == _fold(own)


def spoken_text(sponsor: str, copy: str, template: Any = None) -> str:
    """Exactly what the speech voice is asked to read: the sponsorship line first, then the copy.
    Both parts have speaker and event tags neutralized."""
    return f"{render_disclosure(template, sponsor)} {neutralize_tags(copy)}".strip()


def short_title(text: str, limit: int = 36) -> str:
    """The start of some copy as a label: cut at a word boundary, with an ellipsis if anything was left out."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    if text[limit] != " " and " " in head:
        head = head.rsplit(" ", 1)[0]  # do not cut a word in half
    return head.rstrip(" ,;:-") + "\u2026"


def estimate_seconds(text: str) -> float:
    """A rough reading time, used only until the real recording exists."""
    return round(max(3.0, len(text.split()) / 2.4 + 1.0), 1)


# ---------------------------------------------------------------------------------------------
# Time zones, dates and clock windows
# ---------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def zone_for(name: Any) -> ZoneInfo:
    if not isinstance(name, str) or not name or len(name) > 100:
        raise AdsError(TZ_MESSAGE)
    try:
        return _zone(name)
    except (ZoneInfoNotFoundError, ValueError, OSError, KeyError, TypeError):
        raise AdsError(TZ_MESSAGE) from None


def parse_date(value: Any, label: str) -> dt.date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise AdsError(f"{label} must be a date like 2026-12-31.")
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        raise AdsError(f"{label} is not a real date. Use a date like 2026-12-31.") from None
    if not 2000 <= day.year <= 2100:
        raise AdsError(f"{label} must be between the years 2000 and 2100.")
    return day


def clock_seconds(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60


def clock_label(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def count_weekdays(start: dt.date, end: dt.date, days) -> int:
    """How many dates from ``start`` to ``end`` (both included) fall on one of the ISO weekdays."""
    span = (end - start).days + 1
    if span <= 0 or not days:
        return 0
    wanted = set(days)
    weeks, extra = divmod(span, 7)
    total = weeks * len(wanted)
    for offset in range(extra):
        if (start + dt.timedelta(days=offset)).isoweekday() in wanted:
            total += 1
    return total


# ---------------------------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Creative:
    id: str
    kind: str  # "recorded" (a library track of kind ad) or "script" (read by the speech voice)
    title: str = ""
    track_id: Optional[str] = None  # the library track that holds the audio, once it exists
    text: str = ""  # the copy of a script
    disclosure: str = ""  # wording of the sponsorship line (script only)
    voice: Optional[str] = None  # persona slug (script only)
    position: int = 0

    @property
    def spoken(self) -> bool:
        return self.kind == "script"

    def to_dict(self) -> dict:
        out: dict = {"id": self.id, "kind": self.kind, "title": self.title}
        if self.track_id:
            out["track_id"] = self.track_id
        if self.spoken:
            out["text"], out["disclosure"] = self.text, self.disclosure
            if self.voice:
                out["voice"] = self.voice
        return out


@dataclasses.dataclass(frozen=True)
class Flight:
    start: dt.date
    end: Optional[dt.date] = None  # inclusive; None keeps going until the campaign is paused or removed
    days: tuple = ALL_DAYS  # ISO weekdays, Monday=1
    dayparts: tuple = ()  # ((start_second, end_second), ...) of local wall time; empty means all day
    show_ids: tuple = ()  # empty means any show

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat() if self.end else None,
                "days": ",".join(map(str, self.days)),
                "dayparts": [{"from": clock_label(a), "to": clock_label(b)} for a, b in self.dayparts],
                "show_ids": list(self.show_ids)}


@dataclasses.dataclass(frozen=True)
class Caps:
    per_hour: Optional[int] = None  # in any rolling 60 minutes
    per_day: Optional[int] = None  # per calendar day in the campaign's timezone
    per_week: Optional[int] = None  # per Monday-to-Sunday week in the campaign's timezone

    def to_dict(self) -> dict:
        return {"per_hour": self.per_hour, "per_day": self.per_day, "per_week": self.per_week}


@dataclasses.dataclass(frozen=True)
class Separation:
    campaign_minutes: int = 0  # least gap between two plays of this campaign
    category_minutes: int = 0  # least gap from any play of the same category

    def to_dict(self) -> dict:
        return {"campaign_minutes": self.campaign_minutes, "category_minutes": self.category_minutes}


@dataclasses.dataclass(frozen=True)
class Plan:
    total: Optional[int] = None  # plays booked over all flights
    per_day: Optional[int] = None  # plays booked for each day the campaign may play

    def to_dict(self) -> Optional[dict]:
        if self.total is not None:
            return {"total": self.total}
        if self.per_day is not None:
            return {"per_day": self.per_day}
        return None


@dataclasses.dataclass(frozen=True)
class Campaign:
    id: str
    name: str
    advertiser: str
    creatives: tuple
    flights: tuple
    timezone: str = "UTC"
    category: Optional[str] = None
    house: bool = False
    priority: int = 5
    pacing: str = "even"  # "even" spreads the plan across the flight; "asap" plays whenever allowed
    frequency: str = "regular"  # "light", "regular", "often", or "custom"
    caps: Caps = dataclasses.field(default_factory=Caps)
    separation: Separation = dataclasses.field(default_factory=Separation)
    plan: Plan = dataclasses.field(default_factory=Plan)
    paused: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    removed_at: Optional[float] = None

    @property
    def paid(self) -> bool:
        return not self.house

    @property
    def has_script(self) -> bool:
        return any(c.spoken for c in self.creatives)

    def to_dict(self) -> dict:
        """The same shape ``parse_campaign`` accepts, so a saved campaign can be sent back as is."""
        return {"id": self.id, "name": self.name, "advertiser": self.advertiser,
                "category": self.category, "house": self.house, "priority": self.priority, "pacing": self.pacing,
                "frequency": self.frequency, "timezone": self.timezone,
                "flights": [f.to_dict() for f in self.flights], "caps": self.caps.to_dict(),
                "separation": self.separation.to_dict(), "plan": self.plan.to_dict(),
                "creatives": [c.to_dict() for c in self.creatives], "paused": self.paused}


# ---------------------------------------------------------------------------------------------
# Checking what the operator (or an agent) sends
# ---------------------------------------------------------------------------------------------

CAMPAIGN_KEYS = frozenset({"id", "name", "advertiser", "category", "house", "priority", "pacing", "frequency", "timezone",
                           "flights", "caps", "separation", "plan", "creatives", "paused"})
CREATIVE_KEYS = frozenset({"id", "kind", "title", "track_id", "text", "disclosure", "voice"})
FLIGHT_KEYS = frozenset({"start", "end", "days", "dayparts", "show_ids"})
CAPS_KEYS = ("per_hour", "per_day", "per_week")
SEPARATION_KEYS = ("campaign_minutes", "category_minutes")
PLAN_KEYS = ("total", "per_day")


def _keys(raw: Mapping, allowed, where: str) -> None:
    for key in raw:
        if key not in allowed:
            shown = str(key)[:30]
            raise AdsError(f"Unknown setting \"{shown}\" in {where}. Allowed: {', '.join(sorted(allowed))}.")


def _whole(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdsError(f"{label} must be a whole number.")
    if not low <= value <= high:
        raise AdsError(f"{label} must be between {low} and {high}.")
    return value


def slugify(value: Any, label: str, maximum: int = 40) -> str:
    text = clean_text(value, label, maximum=100)
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:maximum].strip("-")
    if not slug:
        raise AdsError(f"{label} needs some letters or numbers.")
    return slug


def _parse_days(value: Any) -> tuple:
    if value is None:
        return ALL_DAYS
    if not isinstance(value, str) or not DAYS_RE.fullmatch(value):
        raise AdsError("Days must be comma-separated weekday numbers, Monday=1 to Sunday=7, for example \"1,2,3,4,5\".")
    return tuple(sorted(set(map(int, value.split(",")))))


def _parse_dayparts(value: Any) -> tuple:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_DAYPARTS:
        raise AdsError(f"Hours must be a list of at most {MAX_DAYPARTS} time windows.")
    windows = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"from", "to"} or "from" not in item or "to" not in item:
            raise AdsError("Each time window needs a \"from\" and a \"to\", for example 07:00 and 19:00.")
        start, end = item["from"], item["to"]
        if (not isinstance(start, str) or not CLOCK_RE.fullmatch(start) or start == "24:00"
                or not isinstance(end, str) or not CLOCK_RE.fullmatch(end)):
            raise AdsError("Use 24-hour times like 07:00. A window may end at 24:00 (midnight).")
        a, b = clock_seconds(start), clock_seconds(end)
        if a >= b:
            raise AdsError("A time window must end after it starts. Split a window that crosses midnight into two.")
        windows.append((a, b))
    windows.sort()
    for (_, previous_end), (next_start, _) in zip(windows, windows[1:]):
        if next_start < previous_end:
            raise AdsError("Time windows can't overlap.")
    return tuple(windows)


def _parse_show_ids(value: Any) -> tuple:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_SHOW_IDS:
        raise AdsError(f"Shows must be a list of at most {MAX_SHOW_IDS} show ids.")
    out = []
    for item in value:
        if not isinstance(item, str) or not SHOW_RE.fullmatch(item):
            raise AdsError("A show id is lowercase letters, numbers, dashes or underscores.")
        if item not in out:
            out.append(item)
    return tuple(out)


def _parse_flight(raw: Any, today: dt.date) -> Flight:
    if not isinstance(raw, dict):
        raise AdsError("Each flight needs named fields: start, end, days, dayparts and show_ids.")
    _keys(raw, FLIGHT_KEYS, "a flight")
    start = today if raw.get("start") is None else parse_date(raw["start"], "The start date")
    end = None if raw.get("end") is None else parse_date(raw["end"], "The end date")
    if end is not None and end < start:
        raise AdsError("The end date must be on or after the start date.")
    if end is not None and (end - start).days > MAX_FLIGHT_DAYS:
        raise AdsError("A flight can last at most ten years.")
    days = _parse_days(raw.get("days"))
    if end is not None and not count_weekdays(start, end, days):
        raise AdsError("This flight has no day it can play on. Pick other weekdays or other dates.")
    return Flight(start, end, days, _parse_dayparts(raw.get("dayparts")), _parse_show_ids(raw.get("show_ids")))


def _parse_flights(raw: Any, today: dt.date) -> tuple:
    if raw is None or raw == []:
        raw = [{}]
    if not isinstance(raw, list) or len(raw) > MAX_FLIGHTS:
        raise AdsError(f"Flights must be a list of at most {MAX_FLIGHTS} date ranges.")
    flights = sorted((_parse_flight(item, today) for item in raw), key=lambda f: f.start)
    for first, second in zip(flights, flights[1:]):
        if first.end is None or first.end >= second.start:
            raise AdsError("Flights can't overlap. Give each one its own dates, and only the last one may have no end date.")
    return tuple(flights)


LIMIT_LABELS = {"per_hour": "The hourly limit", "per_day": "The daily limit", "per_week": "The weekly limit",
                "campaign_minutes": "The gap between plays of this spot", "category_minutes": "The gap from similar spots"}


def _parse_limits(raw: Any, keys: tuple, base: Optional[Mapping], where: str, low: int, high: int, example: str = "") -> dict:
    """Numbers named in ``keys``. A missing key takes the preset value; an explicit null means no limit."""
    if raw is not None and not isinstance(raw, dict):
        raise AdsError(f"{where} must be given as named numbers, for example {example}.")
    raw = raw or {}
    _keys(raw, keys, where)
    out = {}
    for key in keys:
        if key in raw:
            out[key] = None if raw[key] is None else _whole(raw[key], LIMIT_LABELS[key], low, high)
        else:
            out[key] = None if base is None else base[key]
    return out


def _parse_plan(raw: Any) -> Plan:
    if raw is None:
        return Plan()
    if not isinstance(raw, dict):
        raise AdsError("The plan must be {\"total\": 60} or {\"per_day\": 6}.")
    _keys(raw, PLAN_KEYS, "the plan")
    given = {k: v for k, v in raw.items() if v is not None}
    if len(given) > 1:
        raise AdsError("Choose either a total number of plays or a number per day, not both.")
    if "total" in given:
        return Plan(total=_whole(given["total"], "The total plan", 1, MAX_PLAN_TOTAL))
    if "per_day" in given:
        return Plan(per_day=_whole(given["per_day"], "The daily plan", 1, MAX_PLAN_PER_DAY))
    return Plan()


def _parse_creative(raw: Any, index: int, *, sponsor: str, own_ids: frozenset, used: set,
                    new_id: Callable[[], str]) -> Creative:
    if not isinstance(raw, dict):
        raise AdsError("Each message needs a kind, and either a recording or some text.")
    _keys(raw, CREATIVE_KEYS, "a message")
    kind = raw.get("kind")
    if kind not in ("recorded", "script"):
        raise AdsError("A message is either \"recorded\" (a recording from your library) or \"script\" (text the voice reads).")
    given = raw.get("id")
    if given is None:
        creative_id = new_id()
    else:
        if not isinstance(given, str) or given not in own_ids:
            raise AdsError("One of the messages isn't part of this spot any more. Reload the spot and try again.")
        creative_id = given
    if creative_id in used:
        raise AdsError("Two messages have the same id. Give each message its own.")
    used.add(creative_id)
    track = raw.get("track_id")
    if track is not None and (not isinstance(track, str) or not TRACK_RE.fullmatch(track)):
        raise AdsError("That recording id doesn't look right. Pick the recording again from your library.")
    shown = raw.get("title")
    title = "" if shown is None or (isinstance(shown, str) and not shown.strip()) else clean_text(shown, "A message title", maximum=MAX_NAME)
    if kind == "recorded":
        if not track:
            raise AdsError("A recorded message needs a recording. Import it into the Library as an ad, then pick it.")
        if raw.get("text") or raw.get("disclosure") or raw.get("voice"):
            raise AdsError("A recorded message plays its recording as is. Remove the text, or make it a written script.")
        return Creative(creative_id, kind, title, track, position=index)
    if raw.get("text") is None:
        raise AdsError("A written message needs something to say. Add the text the voice should read.")
    text = clean_text(raw["text"], "What the message says", maximum=MAX_TEXT)
    voice = raw.get("voice")
    if voice is not None and (not isinstance(voice, str) or not VOICE_RE.fullmatch(voice)):
        raise AdsError("A voice is a short lowercase name such as \"dex\".")
    disclosure = validate_disclosure(raw.get("disclosure"), sponsor)
    return Creative(creative_id, kind, title or short_title(text), track or None, text, disclosure, voice, index)


def _uuid_hex() -> str:
    return uuid.uuid4().hex


def parse_campaign(raw: Any, *, today: dt.date, timezone: str = "UTC", station_name: str = "this station",
                   campaign_id: Optional[str] = None, own_creatives: frozenset = frozenset(), paused: bool = False,
                   new_id: Callable[[], str] = _uuid_hex, check_zone: bool = True, check_names: bool = True) -> tuple:
    """Check a campaign object and return ``(Campaign, warnings)``.

    ``today`` is the station's local date, used when a flight has no start. ``timezone`` and
    ``station_name`` fill in what the sender left out. ``campaign_id`` and ``own_creatives``
    identify an existing campaign being edited; ``paused`` is its current state when the sender
    does not say. ``check_zone=False`` and ``check_names=False`` are for reading back stored spots: a
    timezone this computer has since lost, or a name saved before a naming rule existed, then keeps the
    spot visible (and explained) instead of making it vanish.
    Raises ``AdsError`` with a plain-language message."""
    if not isinstance(raw, dict):
        raise AdsError("Send the spot as a set of named fields, such as advertiser and creatives.")
    _keys(raw, CAMPAIGN_KEYS, "the spot")
    if raw.get("id") is not None and raw["id"] != campaign_id:
        raise AdsError("That spot isn't in your list any more. Reload the list and try again.")
    warnings: list = []

    house = raw.get("house", False)
    if not isinstance(house, bool):
        raise AdsError("\"house\" must be true or false.")
    advertiser_raw, category_raw = raw.get("advertiser"), raw.get("category")
    if isinstance(advertiser_raw, dict):
        _keys(advertiser_raw, {"name", "category"}, "the advertiser")
        category_raw = category_raw if category_raw is not None else advertiser_raw.get("category")
        advertiser_raw = advertiser_raw.get("name")
    if advertiser_raw is None and house:
        advertiser_raw = station_name
    if advertiser_raw is None:
        raise AdsError("Say who the spot is for, for example the name of the business.")
    advertiser = clean_text(advertiser_raw, "Who the spot is for", maximum=MAX_ADVERTISER)
    name = advertiser if raw.get("name") is None else clean_text(raw["name"], "The spot name", maximum=MAX_NAME)
    category = None if category_raw is None else slugify(category_raw, "The category")

    priority = _whole(raw.get("priority", 5), "Priority", 1, 10)
    pacing = raw.get("pacing", "even")
    if pacing not in ("even", "asap"):
        raise AdsError("Pacing must be \"even\" (spread out) or \"asap\" (as soon as allowed).")
    zone_name = raw.get("timezone", timezone)
    if check_zone:
        zone_for(zone_name)
    elif not isinstance(zone_name, str) or not zone_name:
        raise AdsError(TZ_MESSAGE)

    frequency = raw.get("frequency")
    if frequency is None:
        frequency = "light" if house else "regular"
    if not isinstance(frequency, str) or (frequency not in FREQUENCY_PRESETS and frequency != "custom"):
        raise AdsError("How often must be \"light\", \"regular\" or \"often\".")
    base = FREQUENCY_PRESETS.get(frequency)
    caps = _parse_limits(raw.get("caps"), CAPS_KEYS, base, "Limits", 1, 100000, '{"per_hour": 2}')
    gaps = _parse_limits(raw.get("separation"), SEPARATION_KEYS, base, "Gaps", 0, 1440, '{"campaign_minutes": 20}')
    for key in SEPARATION_KEYS:
        gaps[key] = gaps[key] or 0
    if base is not None and all(caps[k] == base[k] for k in CAPS_KEYS) and all(gaps[k] == base[k] for k in SEPARATION_KEYS):
        label = frequency
    else:
        label = "custom"

    flights = _parse_flights(raw.get("flights"), today)
    plan = _parse_plan(raw.get("plan"))
    if plan.total is not None and pacing == "even" and any(f.end is None for f in flights):
        raise AdsError("To spread a total evenly, give the flight an end date, or choose \"asap\" pacing.")
    if plan.per_day is not None and caps["per_day"] is not None and caps["per_day"] < plan.per_day:
        warnings.append(f"The daily limit ({caps['per_day']}) is lower than the {plan.per_day} plays planned each day, "
                        "so the plan can't be met.")

    items = raw.get("creatives")
    if not isinstance(items, list) or not items:
        raise AdsError("Add at least one message: a recording from your library or something for the voice to say.")
    if len(items) > MAX_CREATIVES:
        raise AdsError(f"A spot can have at most {MAX_CREATIVES} messages.")
    used: set = set()
    creatives = tuple(_parse_creative(item, i, sponsor=advertiser, own_ids=own_creatives, used=used, new_id=new_id)
                      for i, item in enumerate(items))
    if check_names and any(c.spoken for c in creatives) and not sponsor_name_ok(advertiser):
        raise AdsError(NAMELESS_STATION_MESSAGE if house else NAMELESS_MESSAGE)
    if not house and any(not c.spoken for c in creatives):
        warnings.append("A recorded spot plays exactly as recorded. Make sure the recording says who is paying for it.")

    paused = raw.get("paused", paused)
    if not isinstance(paused, bool):
        raise AdsError("\"paused\" must be true or false.")
    campaign = Campaign(
        id=campaign_id or new_id(), name=name, advertiser=advertiser, creatives=creatives, flights=flights,
        timezone=zone_name, category=category, house=house, priority=priority, pacing=pacing, frequency=label,
        caps=Caps(caps["per_hour"], caps["per_day"], caps["per_week"]),
        separation=Separation(gaps["campaign_minutes"], gaps["category_minutes"]), plan=plan, paused=paused)
    return campaign, warnings
