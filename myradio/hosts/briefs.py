"""From a research brief to an update script that names its sources and says only what they said.

How a brief becomes an update (the rule the research lane can rely on):

1. A brief is either the research lane's own ``Brief`` (items with ``headline_speech``, ``extract_speech``,
   ``source_name_speech`` and a ``kind``; :func:`brief_from_research` maps it) or the simpler shape
   ``{"topic": str, "items": [...]}`` with optional ``id`` and ``generated_at``, each item
   ``{"text", "source"}`` with optional ``id``, ``url``, ``published_at`` and ``kind`` (``reported``,
   ``commentary``, ``announcement`` or ``uncertain``). Everything is cleaned of speech markup first.
2. An item with no ``source`` is never read: a bulletin must be attributable. Neither is an item whose source
   is only a web address, one that mentions a topic the host avoids (in its text or its source's name), one
   whose text holds symbols that cannot be read as written (``5 < 7``), one whose first sentence holds a web
   address, or one whose first sentence is too long to read as written.
3. What is read for each item is a **verbatim extract**: one or more whole sentences taken, unchanged,
   from the start of the item's text. Nothing is paraphrased, shortened mid-sentence or added.
4. Each extract is preceded by a short attribution naming the item's source, worded for its ``kind``
   (reported, announced, commentary, or "not confirmed"). Web addresses are never spoken; they travel in the
   citations.
5. Items are read in the brief's order while they fit the target length; the rest are listed as skipped.
   The opening and closing lines come from the host's own update templates.
6. If nothing can be read, no update is made (``NothingToRead``). The right response is music.

The result carries ``citations``: for every item that was read, its source, link, publication time,
kind and the exact extract spoken, so each spoken claim stays attributable.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
import re
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .composer import _FACT_ORDER, Script, TemplateComposer, blocked_by_topic, emitted_tags, normalise_seed, seconds_value
from .errors import HostsError, NothingToRead
from .facts import Facts
from .lint import grounding_lint, has_address
from .personas import Persona
from .rng import Rng
from .segments import UPDATE
from .textsafe import MAX_SCRIPT_CHARS, audit_speech_text, neutralize, neutralize_quote, speaker_token, strip_markup
from .timing import count_words, estimate_seconds

MAX_ITEMS = 20
MAX_TEXT = 1200
MAX_SOURCE = 80
MAX_URL = 300
MAX_TOPIC = 80
ITEM_WORDS = 45              # sentences are added to an extract while it stays within this many words
HARD_MAX_SENTENCE_WORDS = 60  # a first sentence longer than this is not read
MIN_EXTRACT_WORDS = 4        # a first reading shorter than this is joined to the next sentence when it can be
MAX_EXTRACT_INPUT = 4000     # characters of an item's text that are ever looked at
KINDS = ("reported", "commentary", "announcement", "uncertain")
# Full stops after these do not end a sentence, whatever the case ...
_ABBREVIATIONS = frozenset(("mr", "mrs", "ms", "mx", "dr", "prof", "sr", "jr", "st", "vs", "etc", "inc", "ltd", "co", "corp",
                            "bros", "no", "fig", "vol", "approx", "dept", "ave", "blvd", "rd", "hwy", "mt", "ft", "esq", "messrs",
                            "est", "assn", "univ", "pp", "e.g", "i.e", "u.s", "u.k", "a.m", "p.m"))
# ... and after these only when they are written with a capital, because in lower case they are ordinary words
# ("he sat.", "in the sun.").
_CAPITALISED_ABBREVIATIONS = frozenset((
    "sen", "rep", "gov", "gen", "lt", "col", "maj", "capt", "cpl", "pvt", "sgt", "cmdr", "adm", "rev", "hon", "pres", "supt",
    "atty", "fr", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"))
_BOUNDARY = re.compile(r"[.!?]+[\"'\u201d\u2019)\]]*\s+(?=[\"'\u201c\u2018(\[A-Z0-9])")
_FRAGMENT = re.compile(r"(?:^|\s)[A-Z][a-z]{0,4}\.$|^\W*\d+\.$")      # "Adm." or "12." on its own is not a sentence
_ENDS_SENTENCE = re.compile(r"[.!?\u2026][\"'\u201d\u2019)\]]*$")
_LEADS = {
    "reported": ("According to {s}:", "{s} reports:", "From {s}:", "{s} says:", "As reported by {s}:",
                 "Here is what {s} says:", "This is from {s}:", "{s} writes:"),
    "announcement": ("{s} announced:", "{s} has announced:", "Announcement from {s}:", "{s} says in an announcement:",
                     "From {s}, an announcement:"),
    "commentary": ("Commentary from {s}:", "{s} offers this view:", "This is opinion from {s}:", "{s} comments:"),
    "uncertain": ("This is not confirmed. {s} says:", "{s} says, though it is not confirmed:",
                  "Unconfirmed, but {s} reports:", "Not confirmed, but here is {s}:"),
}
_SKIP_MESSAGES = {
    "no_source": "Left out because it did not say where it came from.",
    "source_link": "Left out because its source is only a web address, which is not read aloud. Give the source a name to include it.",
    "no_text": "Left out because it had no words to read.",
    "symbols": "Left out because its text has symbols (< or >) that cannot be read aloud exactly as written.",
    "link": "Left out because it opens with a web address, which is not read aloud.",
    "too_long": "Left out because its first sentence is too long to read as written.",
    "fragment": "Left out because its first sentence is too short or cut off to read on its own.",
    "topic": "Left out because this host does not cover that topic.",
    "time": "Left out to keep the update to the length asked for.",
    "duplicate": "Left out because it repeats another item.",
    "not_an_item": "Left out because it was not an item.",
    "extra": "Left out because a brief holds at most {n} items.",
}
# The research lane's item kinds, in the words this module uses. "unverified" is never read as fact.
_RESEARCH_KINDS = {"reported": "reported", "commentary": "commentary", "announcement": "announcement", "unverified": "uncertain"}
_RESEARCH_ITEM_KEYS = ("extract_speech", "headline_speech", "source_name_speech")


@dataclasses.dataclass(frozen=True)
class BriefItem:
    id: str
    text: str
    source: str
    url: Optional[str]
    published_at: Optional[str]
    kind: str


@dataclasses.dataclass(frozen=True)
class Brief:
    id: str
    topic: str
    generated_at: Optional[str]
    items: Tuple[BriefItem, ...]
    warnings: Tuple[str, ...]
    dropped: Tuple[dict, ...]


# ---- times and addresses -------------------------------------------------------------------------------
# Written out rather than left to datetime.fromisoformat, which accepts more forms on Python 3.11 and later than on
# 3.10: a time is understood the same way on every supported version.
_ISO_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?\s*(Z|z|[+-]\d{2}(?::?\d{2})?)")


def _parse_iso(text: str) -> Optional[dt.datetime]:
    """A time like 2026-11-05T09:30:00+02:00 (seconds, a fraction, a colon in the offset and 'Z' are all optional
    or allowed). A time with no offset is not understood, because it cannot be put on the clock."""
    match = _ISO_TIME.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute = (int(match.group(n)) for n in range(1, 6))
    second = int(match.group(6) or 0)
    micro = int((match.group(7) or "0")[:6].ljust(6, "0"))
    zone = match.group(8)
    if zone in ("Z", "z"):
        offset = dt.timedelta(0)
    else:
        digits = zone[1:].replace(":", "")
        zone_hours, zone_minutes = int(digits[:2]), int(digits[2:4] or 0)
        if zone_hours > 23 or zone_minutes > 59:
            return None
        offset = (-1 if zone[0] == "-" else 1) * dt.timedelta(hours=zone_hours, minutes=zone_minutes)
    try:
        return dt.datetime(year, month, day, hour, minute, second, micro, tzinfo=dt.timezone(offset))
    except ValueError:
        return None


def iso_time(value: Any) -> Optional[str]:
    """Epoch seconds or an ISO-8601 time with a UTC offset, as an ISO string in UTC. Anything else is None."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(value) or not 0 <= value <= 4102444800:
                return None
            return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec="seconds")
        if isinstance(value, str) and 8 <= len(value.strip()) <= 40:
            moment = _parse_iso(value.strip())
            if moment is None:
                return None
            return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None
    return None


def clean_url(value: Any) -> Optional[str]:
    """A plain http(s) address, kept only for the citation (it is never spoken and never fetched)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) > MAX_URL or not re.fullmatch(r"https?://[^\s<>\"'\\|{}\[\]]+", text):
        return None
    return text


# ---- sentences and extracts ----------------------------------------------------------------------------
def _full_stop_is_not_an_end(text: str, start: int, stop: int, after: int) -> bool:
    """True when the full stop at ``text[stop]`` belongs to an abbreviation, an initial or a number and so does not
    end a sentence. ``start`` is where the sentence began; ``after`` is where the next piece would start."""
    token = re.search(r"([A-Za-z][A-Za-z.]*)$", text[max(start, stop - 8):stop])
    if token:
        raw = token.group(1).rstrip(".")
        word = raw.lower()
        if word in _ABBREVIATIONS or len(word) == 1 or re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]", raw):
            return True
        if raw[:1].isupper() and word in _CAPITALISED_ABBREVIATIONS:
            return True
        # a short word with a capital and a number straight after it: "Nov. 5", "Fig. 3", "Sec. 4"
        if raw[:1].isupper() and 2 <= len(raw) <= 6 and text[after:after + 1].isdigit():
            return True
    return stop > 0 and text[stop - 1].isdigit() and text[after:after + 1].isdigit()


def split_sentences(text: str) -> List[Tuple[int, int]]:
    """Sentence spans (start, end) in ``text``. Abbreviations ("Dr.", "Sen.", "Nov."), initials, decimals and
    numbers after a short capitalised word ("Nov. 5") do not end a sentence."""
    spans: List[Tuple[int, int]] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        end = match.start() + len(match.group().rstrip())
        stop = match.start()
        if (text[stop] == "." and text[stop:end].count(".") == 1
                and len(text[stop:end].strip(".\"')]\u201d\u2019")) == 0
                and _full_stop_is_not_an_end(text, start, stop, match.end())):
            continue
        spans.append((start, end))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def _extract(text: Any, max_words: int) -> "Tuple[Optional[str], str]":
    """(leading whole sentences of ``text``, '') or (None, why not): 'link', 'too_long' or 'fragment'."""
    if not isinstance(text, str):
        return None, "fragment"
    text = text.strip()
    if len(text) > MAX_EXTRACT_INPUT:
        text = text[:MAX_EXTRACT_INPUT]
        cut = True
    else:
        cut = False
    spans = split_sentences(text)
    if cut and spans:
        spans = spans[:-1]          # the last sentence may have been cut by the limit, so it is not used
    if not spans:
        return None, "too_long" if cut else "fragment"
    ends = [b for _, b in spans]
    # the first reading: a sentence, joined to the next ones for as long as it is only a few words and there is more
    k = 0
    while k + 1 < len(spans) and count_words(text[:ends[k]]) < MIN_EXTRACT_WORDS and not has_address(text[spans[k + 1][0]:ends[k + 1]]):
        k += 1
    if any(has_address(text[a:b]) for a, b in spans[:k + 1]):
        return None, "link"
    first = text[:ends[k]]
    words = count_words(first)
    if words > HARD_MAX_SENTENCE_WORDS:
        return None, "too_long"
    if words < MIN_EXTRACT_WORDS:
        # Still short although nothing more could be joined. If the item goes on, a reading that ends like an
        # abbreviation ("Adm.") or a number ("12.") is a fragment; if it is the whole item, a single word is not a reading.
        goes_on = ends[k] < len(text)
        if (goes_on and _FRAGMENT.search(first)) or (not goes_on and words < 2):
            return None, "fragment"
    end = ends[k]
    for a, b in spans[k + 1:]:
        if has_address(text[a:b]) or count_words(text[:b]) > max_words:
            break
        end = b
    return text[:end], ""


def verbatim_extract(text: str, max_words: int = ITEM_WORDS) -> Optional[str]:
    """Leading whole sentences of ``text``, unchanged, up to about ``max_words`` words; None if none can be read.

    The result is always a contiguous slice of ``text`` starting at its first character. A first reading of fewer
    than four words is joined to the next sentence when there is one. If the first sentence alone is longer than
    ``HARD_MAX_SENTENCE_WORDS``, holds a web address (any sentence read must be free of one), or is only an
    abbreviation or a number, there is no extract. Only the first ``MAX_EXTRACT_INPUT`` characters are examined."""
    return _extract(text, max_words)[0]


def split_first(text: str) -> str:
    spans = split_sentences(text.strip())
    return text.strip()[spans[0][0]:spans[0][1]] if spans else text


# ---- reading a brief -------------------------------------------------------------------------------------
def _whole_sentences(text: str) -> str:
    """``text`` without a last sentence that was cut off (it does not end like a sentence); '' if nothing whole is left."""
    if _ENDS_SENTENCE.search(text):
        return text
    spans = split_sentences(text)
    return text[:spans[-2][1]] if len(spans) > 1 else ""


def _research_text(entry: Mapping) -> str:
    """What to read for a research item: its extract (without a sentence that was cut off), else its headline."""
    extract = entry.get("extract_speech")
    if isinstance(extract, str) and extract.strip():
        text = extract.strip()
        if entry.get("extract_truncated") is True:
            text = _whole_sentences(text)
        if text:
            return text
    headline = entry.get("headline_speech")
    return headline.strip() if isinstance(headline, str) else ""


def brief_from_research(raw: Mapping) -> Mapping:
    """Map the research lane's ``Brief`` (``docs/lanes/research.md``) onto the shape :func:`parse_brief` reads.

    Only the ``*_speech`` fields are used, as that lane asks: ``extract_speech`` (or, when the source gave only a
    title, ``headline_speech``) becomes the text, ``source_name_speech`` the source. Its kinds map one to one;
    ``unverified`` becomes ``uncertain`` and is introduced as not confirmed. The brief's ``researcher_name`` is the
    topic unless a topic is given. A brief that did not end ``ready`` has nothing to read. A mapping in the plain
    shape comes back unchanged."""
    items = raw.get("items")
    shaped = isinstance(items, (list, tuple)) and any(
        isinstance(i, Mapping) and "text" not in i and any(k in i for k in _RESEARCH_ITEM_KEYS) for i in items)
    if not shaped and "researcher_name" not in raw:
        return raw          # the plain shape (a run that found nothing still names its researcher)
    mapped: List[Any] = []
    if isinstance(items, (list, tuple)) and raw.get("outcome") in (None, "ready"):
        for entry in items:
            if isinstance(entry, Mapping) and "text" not in entry and any(k in entry for k in _RESEARCH_ITEM_KEYS):
                stamp = entry.get("published_at")
                mapped.append({"id": entry.get("id"), "text": _research_text(entry), "source": entry.get("source_name_speech"),
                               "url": entry.get("url"), "published_at": stamp if stamp not in (None, "") else entry.get("published_ts"),
                               "kind": _RESEARCH_KINDS.get(entry.get("kind"), "uncertain")})
            else:
                mapped.append(entry)
    elif isinstance(items, (list, tuple)):
        mapped = []                      # an unfinished or empty run has nothing to read, whatever else it carries
    else:
        mapped = items                   # not a list: parse_brief will say so
    topic = raw.get("topic")
    if not (isinstance(topic, str) and topic.strip()):
        topic = raw.get("researcher_name")
    return {"id": raw.get("id"), "topic": topic, "generated_at": raw.get("generated_at", raw.get("created_at")), "items": mapped}


def parse_brief(raw: Any) -> Brief:
    if not isinstance(raw, Mapping):
        raise HostsError("A research brief needs a topic and a list of items, each with its text and its source.")
    raw = brief_from_research(raw)
    warnings: List[str] = []
    dropped: List[dict] = []
    topic = neutralize(raw.get("topic", ""), MAX_TOPIC).text if isinstance(raw.get("topic", ""), str) else ""
    items_raw = raw.get("items")
    if not isinstance(items_raw, (list, tuple)):
        raise HostsError("A research brief needs a list of items to read.")
    brief_id = neutralize(raw.get("id", ""), 64).text if isinstance(raw.get("id", ""), str) else ""
    items: List[BriefItem] = []
    seen_text = set()
    for index, entry in enumerate(items_raw):
        label = f"item-{index + 1}"
        if index >= MAX_ITEMS:
            dropped.append({"item_id": label, "source": "", "reason": "extra", "message": _SKIP_MESSAGES["extra"].format(n=MAX_ITEMS)})
            continue
        if not isinstance(entry, Mapping):
            dropped.append({"item_id": label, "source": "", "reason": "not_an_item", "message": _SKIP_MESSAGES["not_an_item"]})
            continue
        item_id = neutralize(entry.get("id", ""), 64).text if isinstance(entry.get("id", ""), str) else ""
        item_id = item_id or label
        source = neutralize(entry.get("source", ""), MAX_SOURCE).text if isinstance(entry.get("source", ""), str) else ""
        cleaned = neutralize_quote(entry.get("text", ""), MAX_TEXT)
        text = cleaned.text
        if not source:
            reason = "no_source"
        elif has_address(source):
            reason = "source_link"
        elif not text:
            reason = "no_text"
        elif "symbols" in cleaned.flags:
            reason = "symbols"
        elif text.lower() in seen_text:
            reason = "duplicate"
        else:
            reason = None
        if reason:
            dropped.append({"item_id": item_id, "source": source, "reason": reason, "message": _SKIP_MESSAGES[reason]})
            continue
        seen_text.add(text.lower())
        if "markup" in cleaned.flags or "control" in cleaned.flags:
            warnings.append(f"Voice codes or hidden characters were removed from the text of {item_id}.")
        if "brackets" in cleaned.flags:
            warnings.append(f"Square brackets were left out of the text of {item_id}; the words inside are still read.")
        kind = entry.get("kind")
        if kind is None or kind == "":
            kind = "reported"
        elif kind == "unverified":
            kind = "uncertain"
        elif not isinstance(kind, str) or kind not in KINDS:
            warnings.append(f"The kind of {item_id} was not recognised, so it is introduced as not confirmed.")
            kind = "uncertain"
        url = clean_url(entry.get("url"))
        if entry.get("url") not in (None, "") and url is None:
            warnings.append(f"The link for {item_id} was not a plain web address and was not kept.")
        stamp = iso_time(entry.get("published_at"))
        if entry.get("published_at") not in (None, "") and stamp is None:
            warnings.append(f"The publication time of {item_id} needs a UTC offset, such as 2026-11-05T09:30:00+00:00, so it was left out.")
        items.append(BriefItem(item_id, text, source, url, stamp, kind))
    return Brief(brief_id, topic, iso_time(raw.get("generated_at")), tuple(items), tuple(warnings), tuple(dropped))


# ---- writing the update ------------------------------------------------------------------------------------
def _lead(kind: str, source: str, rng: Rng, used: set) -> str:
    options = [t for t in _LEADS[kind] if t not in used] or list(_LEADS[kind])
    template = rng.pick(options)
    used.add(template)
    return template.format(s=source)


def _line(lead: str, extract: str) -> str:
    """The spoken line for one item: its attribution, the extract as found, and a full stop of the host's own when
    the extract does not end like a sentence (a headline, say), so the host's next words never run on from it."""
    line = f"{lead} {extract}"
    return line if _ENDS_SENTENCE.search(extract) else line + "."


def compose_update(composer: TemplateComposer, persona: Persona, brief: Any, facts: Any = None, *, seed: Any = None,
                   target_seconds: Any = None, remember: bool = True) -> Script:
    """Build a whole sourced update from a brief. See the module notes for the rules."""
    if persona.language != "en":
        raise HostsError("Scripts can only be written in English for now.")
    parsed = parse_brief(brief)
    base, warnings = Facts.from_any(facts)
    warnings = list(warnings) + list(parsed.warnings)
    skipped: List[dict] = list(parsed.dropped)
    seed = normalise_seed(seed)
    rng = Rng(seed, persona.id, "update")
    target = _target(persona, target_seconds, rng, warnings)
    avoided = persona.prohibited_topics

    # the subject of the update is not one the host avoids either
    subject = base.topic or parsed.topic
    hit = blocked_by_topic(subject, avoided) if subject else None
    if hit:
        warnings.append(f"The topic was left out because {persona.name} does not talk about {hit}.")
        base, parsed = dataclasses.replace(base, topic=""), dataclasses.replace(parsed, topic="")

    # 1. which items can be read, and exactly what would be said for each
    candidates: List[Tuple[BriefItem, str, str, str]] = []   # item, first sentence(s), fuller extract, attribution
    used_leads: set = set()
    for item in parsed.items:
        if blocked_by_topic(item.text, avoided) or blocked_by_topic(item.source, avoided):
            skipped.append({"item_id": item.id, "source": item.source, "reason": "topic", "message": _SKIP_MESSAGES["topic"]})
            continue
        fuller, why = _extract(item.text, ITEM_WORDS)
        if fuller is None:
            skipped.append({"item_id": item.id, "source": item.source, "reason": why, "message": _SKIP_MESSAGES[why]})
            continue
        first = _extract(item.text, 1)[0] or fuller
        candidates.append((item, first, fuller, _lead(item.kind, item.source, rng, used_leads)))
    if not candidates:
        raise NothingToRead("There is nothing to read from this brief, so no update was made. Music keeps playing.")

    # 2. pick what fits (first sentences first, then fuller readings while time allows), then write the
    #    opening and closing around it. The whole must also fit what the speech step accepts.
    budget = max(target - 11.0, 0.0)
    marker = len(speaker_token(persona.speaker))
    capped = False
    for _ in range(12):
        chosen = _fit(candidates, budget, persona.wps)
        intro, outro = _frame(composer, persona, base, parsed, chosen, seed, remember=False)
        body = " ".join([_body(intro, persona)] + [line for _, _, line in chosen] + [_body(outro, persona)])
        too_long = marker + len(body) > MAX_SCRIPT_CHARS
        if len(chosen) <= 1 or (not too_long and estimate_seconds(body, persona.wps) <= target * 1.25):
            break
        capped = capped or too_long
        budget *= 0.85
    kept = {c[0].id for c in chosen}
    for item, _, _, _ in candidates:
        if item.id not in kept:
            skipped.append({"item_id": item.id, "source": item.source, "reason": "time", "message": _SKIP_MESSAGES["time"]})
    if remember:
        intro, outro = _frame(composer, persona, base, parsed, chosen, seed, remember=True)
    warnings += [w for s in (intro, outro) for w in s.warnings if w not in warnings]
    text = " ".join([_body(intro, persona)] + [line for _, _, line in chosen] + [_body(outro, persona)])

    # 3. checks: the opening and closing are already checked; check the whole against the brief itself
    final_facts = _facts_for(base, parsed, chosen)
    attribution_words = [t.replace("{s}", " ") for options in _LEADS.values() for t in options]
    issues = grounding_lint(strip_markup(text), final_facts, persona,
                            extra=[c[0].text for c in chosen] + [c[0].source for c in chosen] + attribution_words)
    if issues:
        raise HostsError("This update could not be checked against its sources, so none was made. "
                         "Try again, or choose a different host or length.")
    for item, extract, _ in chosen:
        if extract not in item.text or extract not in text:
            raise HostsError("An item could not be read exactly as written, so no update was made.")
    full = speaker_token(persona.speaker) + text
    problems = audit_speech_text(full, persona.speaker, emitted_tags(persona.allowed_tags, composer.tag_map))
    if problems:
        if any("too long" in p for p in problems):
            raise HostsError("This update came out too long for the voice to read, so none was made. Ask for a shorter one.")
        raise HostsError("The update held voice codes that are not allowed, so none was made.")
    citations = tuple({"position": i + 1, "brief_id": parsed.id or None, "item_id": item.id, "source": item.source,
                       "url": item.url, "published_at": item.published_at, "kind": item.kind, "extract": extract,
                       "words": count_words(extract)} for i, (item, extract, _) in enumerate(chosen))
    est = estimate_seconds(text, persona.wps)
    if abs(est - target) > max(composer.config.min_tolerance * 2, 0.25 * target):
        word = "shorter" if est < target else "longer"
        reason = ("The voice reads at most 3,000 characters at a time, so the update was kept shorter." if capped
                  else "There were no more items to read." if len(chosen) == len(candidates)
                  else "Items are never cut in the middle of a sentence.")
        warnings.append(f"This update runs about {est:g} seconds, {word} than the {target:g} asked for. {reason}")
    used = set(intro.facts_used) | set(outro.facts_used)
    if final_facts.topic:
        used.add("topic")
    used = [name for name in _FACT_ORDER if name in used]
    return Script(full, strip_markup(text), persona.speaker, UPDATE.role, UPDATE.library_kind, "update", persona.id, est,
                  target, tuple(used), tuple(warnings), "template",
                  f"{intro.template or 'plain'}+{outro.template or 'plain'}", seed, citations=citations, skipped=tuple(skipped))


def _target(persona: Persona, requested: Any, rng: Rng, warnings: List[str]) -> float:
    if requested is None:
        low, high = persona.verbosity
        level = low + rng.below(high - low + 1)
        return round(UPDATE.low + (UPDATE.high - UPDATE.low) * (level - 1) / 4, 1)
    number = seconds_value(requested)
    clamped = min(UPDATE.max_s, max(UPDATE.min_s, number))
    if clamped != number:
        warnings.append(f"An update can run from {UPDATE.min_s:g} to {UPDATE.max_s:g} seconds, so {clamped:g} seconds was used.")
    return clamped


def _fit(candidates: Sequence[Tuple[BriefItem, str, str, str]], budget: float, wps: float):
    """Items to read as (item, extract, attributed line). Every item that fits is read at least by its first
    sentence; leftover time then lets earlier items run on to their fuller extract."""
    picked: List[List[Any]] = []
    running = 0.0
    for item, first, fuller, lead in candidates:
        cost = estimate_seconds(_line(lead, first), wps)
        if not picked or running + cost <= budget:
            picked.append([item, first, fuller, lead, cost])
            running += cost
    for entry in picked:
        item, first, fuller, lead, cost = entry
        if fuller != first:
            more = estimate_seconds(_line(lead, fuller), wps) - cost
            if running + more <= budget:
                entry[1] = fuller
                running += more
    return [(e[0], e[1], _line(e[3], e[1])) for e in picked]


def _facts_for(base: Facts, brief: Brief, chosen) -> Facts:
    sources: List[str] = []
    for item, _, _ in chosen:
        if item.source not in sources:
            sources.append(item.source)
    return dataclasses.replace(base, topic=base.topic or brief.topic, item_count=len(chosen), sources=tuple(sources[:8]))


def _frame(composer: TemplateComposer, persona: Persona, base: Facts, brief: Brief, chosen, seed: int, *, remember: bool):
    facts = _facts_for(base, brief, chosen)
    intro = composer.compose(persona, "update_intro", facts, seed=seed, remember=remember)
    outro = composer.compose(persona, "update_outro", facts, seed=seed, remember=remember)
    return intro, outro


def _body(script: Script, persona: Persona) -> str:
    return script.text[len(speaker_token(persona.speaker)):]
