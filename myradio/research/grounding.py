"""A grounding lint: does a spoken script stay inside what the brief says?

``check_script(script, brief)`` is an advisory check for the hosts lane and the director. It looks for the ways a script
written from a brief most often goes beyond it, and returns plain-language findings (an empty list means nothing was found).

It is **heuristic**. It compares words, not meaning: it cannot tell that a sentence is a fair paraphrase, and it can miss an
invented claim that uses only words from the sources. It checks:

* numbers (digits and spelled-out numbers from two to a million) that no source mentions;
* capitalised names in the middle of a sentence that no source mentions (the station's own names go in ``allowed_words``);
* addresses, e-mail addresses and handles the sources do not contain;
* that every item's source is named in the script;
* certainty words ("confirmed", "officially") when an item is only unverified or commentary and the source does not say so itself;
* freshness claims ("breaking", "just in") when no item is under half an hour old;
* a script far longer than the segment (``target_words``).

Speaker and event markup in the script (``<|speaker:1|>``, ``[laughs]``) is ignored: it is the station's own, not a claim.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .textutil import fold

CODES = ("number", "name", "link", "attribution", "certainty", "freshness", "length")
_SPEAKER = re.compile(r"<\|[^>\n]{0,100}>")
_EVENT = re.compile(r"\[[A-Za-z][A-Za-z :_-]{0,30}\]")
_URL = re.compile(r"(?:https?://|www\.)\S+|(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+|(?<!\w)@\w{2,}", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CAPITAL = re.compile(r"(?<![\w'\u2019-])([A-Z][\w'\u2019-]*)")
_SENTENCE_START = re.compile(r"(?:[.!?:\u2026]\s+|\n\s*|[\"\u201c(]\s*)$")
_CALENDAR = frozenset(fold(w) for w in (
    "Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August September October November December"
).split())
_PRONOUN = frozenset({"i", "i'm", "i've", "i'll", "i'd", "id", "im", "ive", "ill"})
_WORD_NUMBERS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_WORD_NUMBERS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
                      "hundred": 100, "thousand": 1000, "million": 1_000_000})
_IGNORED_NUMBER_WORDS = frozenset({"zero", "one"})            # "one of the", "no one": not claims
_CERTAINTY = ("confirmed", "confirms", "confirm", "officially", "proven", "proves", "definitely", "undeniably", "certainly", "for sure",
              "beyond doubt", "without a doubt")
_FRESHNESS = ("breaking", "just in", "right now", "moments ago", "just announced", "this just in", "live now", "minutes ago")
FRESH_SECONDS = 30 * 60
LENGTH_FACTOR = 1.5
MAX_SCRIPT = 1_000_000         # characters of a script that are read at all
MAX_EXAMINED = 20_000          # characters of a script that the word-by-word checks look at; a longer script is reported as too long
LOOKBACK = 400                 # how much of the text before a word is read to decide whether the word starts a sentence


@dataclass(frozen=True)
class Finding:
    code: str            # one of CODES
    text: str            # the part of the script concerned
    message: str         # a plain-language sentence

    def as_dict(self) -> dict:
        return {"code": self.code, "text": self.text, "message": self.message}


def _numbers(text: str) -> set:
    found = set()
    for match in _NUMBER.finditer(text):
        raw = match.group(0).rstrip(",").replace(",", "")
        try:
            found.add(float(raw))
        except ValueError:
            continue
    for word in re.findall(r"[A-Za-z]+", text.lower()):
        if word in _WORD_NUMBERS and word not in _IGNORED_NUMBER_WORDS:
            found.add(float(_WORD_NUMBERS[word]))
    return found


def _date_numbers(items: list) -> set:
    """The day, the days either side (the station does not know the listener's time zone) and the year of each item's date."""
    found = set()
    for item in items:
        ts = item.get("published_ts")
        if isinstance(ts, (int, float)):
            for shift in (-1, 0, 1):
                moment = dt.datetime.fromtimestamp(ts + shift * 86400, dt.timezone.utc)
                found.update({float(moment.day), float(moment.year)})
    return found


def _label(number: float) -> str:
    return str(int(number)) if number == int(number) else str(number)


def check_script(script: str, brief: dict, *, allowed_words: Iterable[str] = (), item_ids: Optional[Iterable[str]] = None,
                 now: Optional[float] = None) -> list:
    """Findings (a list of ``Finding``) for ``script`` written from ``brief``. ``item_ids`` limits the check to the items the
    script actually covers (default: all). ``now`` is when the script will air (default: when the brief was made)."""
    wanted = set(item_ids) if item_ids is not None else None
    items = [i for i in brief.get("items", []) if wanted is None or i.get("id") in wanted]
    findings: list = []
    whole = _EVENT.sub(" ", _SPEAKER.sub(". ", (script or "")[:MAX_SCRIPT]))             # a new speaker starts a new sentence
    spoken = whole[:MAX_EXAMINED]
    corpus = " ".join(" ".join(filter(None, [i.get("headline"), i.get("extract"), i.get("source_name"), " ".join(i.get("categories", []))])) for i in items)
    known_words = set(fold(corpus).split()) | {fold(w) for w in allowed_words} | _CALENDAR | _PRONOUN
    known_numbers = _numbers(corpus) | _date_numbers(items)

    for number in sorted(_numbers(spoken) - known_numbers):
        findings.append(Finding("number", _label(number), f"The script mentions the number {_label(number)}, which none of the sources mention."))

    lead = len(spoken) - len(spoken.lstrip())
    for match in _CAPITAL.finditer(spoken):
        word = match.group(1)
        if match.start() <= lead or _SENTENCE_START.search(spoken[max(0, match.start() - LOOKBACK):match.start()]):
            continue
        folded = fold(word)
        if folded and folded not in known_words and not any(part in known_words for part in folded.split()):
            findings.append(Finding("name", word, f"The script names \u201c{word}\u201d, which none of the sources mention."))
            known_words.add(folded)

    for match in _URL.finditer(spoken):
        if match.group(0).lower().rstrip(".,;") not in corpus.lower():
            findings.append(Finding("link", match.group(0), "The script includes an address or handle that the sources do not contain."))

    spoken_folded = fold(spoken)
    for item in items:
        name = item.get("source_name") or ""
        if name and fold(name) and f" {fold(name)} " not in f" {spoken_folded} ":
            findings.append(Finding("attribution", name, f"The script does not say that this came from {name}."))

    unsure = [i for i in items if i.get("kind") in ("unverified", "commentary")]
    corpus_folded = f" {fold(corpus)} "
    if unsure:
        for word in _CERTAINTY:
            if f" {fold(word)} " in f" {spoken_folded} " and f" {fold(word)} " not in corpus_folded:
                findings.append(Finding("certainty", word, f"The script says \u201c{word}\u201d, but a source it relies on is unconfirmed or opinion."))

    when = now if now is not None else brief.get("created_ts")
    ages = [when - i["published_ts"] for i in items if isinstance(i.get("published_ts"), (int, float)) and isinstance(when, (int, float))]
    if ages and min(ages) > FRESH_SECONDS:
        for phrase in _FRESHNESS:
            if f" {fold(phrase)} " in f" {spoken_folded} " and f" {fold(phrase)} " not in corpus_folded:
                findings.append(Finding("freshness", phrase, f"The script says \u201c{phrase}\u201d, but nothing in this brief is under half an hour old."))

    target = brief.get("target_words")
    count = len(whole.split())
    if isinstance(target, int) and target > 0 and count > target * LENGTH_FACTOR:
        findings.append(Finding("length", f"{count} words", f"The script is {count} words; the segment was planned for about {target}."))
    return findings
