"""Compatibility rules for style palettes and arrangement designs.

A palette is checked for two kinds of mistake that make a music prompt contradict itself:

* **Instruments that do not belong together**: a lead that cannot carry a tune, a bass part that
  is not a bass, instruments from different traditions stacked without saying so, electronic
  sound in an "acoustic" style, a piano promised alongside a microtonal scale, and so on.
* **Contradictory tempo or meter claims**: a meter that does not add up, a text that names a
  different time signature or beat count than the style's own, a tempo figure or word outside the
  style's range, an energy class that does not fit the tempo.

Every rule has a stable identifier listed in ``RULES``; ``docs/PROMPTS.md`` documents the same
list and a test keeps the two in step. The checks read text and a small instrument lexicon
(``instruments.py``). They find authoring mistakes. They cannot say that a model will play the
result well, and passing them is not a listening result.

Findings are *errors* (the preset must not be saved or shipped) or *warnings* (worth a look, for
example an instrument the lexicon does not know). Messages are written for a person editing a
style, not for a programmer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import product
from typing import Iterable

from .instruments import BASS_ROLE_WORDS, Instrument, find_instruments
from .textutil import shown

ERROR = "error"
WARNING = "warning"

# rule id -> (severity, one-line description). docs/PROMPTS.md lists these; a test checks they match.
RULES: dict[str, tuple[str, str]] = {
    # meter and tempo
    "meter.format": (ERROR, "The meter is written like 4/4, 6/8 or 7/8 (2+2+3)."),
    "meter.grouping-sum": (ERROR, "The groups in brackets add up to the bar."),
    "meter.mention": (ERROR, "Any other time signature in the text has the same bar length and is described as a cross-rhythm."),
    "meter.beat-count": (ERROR, "An 'N-beat' claim fits the meter, or is described as a cross-rhythm."),
    "meter.grouping-mention": (ERROR, "A grouping such as 3+2+2 in the text adds up to the bar and, when the style declares a grouping, rearranges it."),
    "meter.tag": (ERROR, "A style tagged or named a waltz is in three, and an 'odd-meter' style has an uneven meter."),
    "tempo.range": (ERROR, "The tempo range is ordered, between 30 and 240, and no wider than 40."),
    "tempo.number": (ERROR, "A BPM figure in the text lies inside the range (or is marked half-time or double-time)."),
    "tempo.word": (ERROR, "A 'slow' or 'fast' tempo word agrees with the range."),
    "tempo.energy": (ERROR, "The energy class fits the tempo range (calm tops out at 120, steady stays within 60 to 140, driving reaches 96)."),
    # instruments
    "instrument.lead-melodic": (ERROR, "Every lead can carry a melody."),
    "instrument.bass-slot": (ERROR, "The bass part names a bass instrument or a bass role."),
    "instrument.bass-only-elsewhere": (ERROR, "Bass-only instruments are not used as a lead or supporting part."),
    "instrument.tradition-mix": (ERROR, "Instruments tied to different traditions are mixed only in a style tagged 'fusion'."),
    "instrument.purity": (ERROR, "'acoustic' styles use no electric or electronic instrument; 'chiptune' and 'synthetic' styles use no acoustic one."),
    "instrument.microtonal": (ERROR, "Fixed-pitch instruments (piano, accordion, marimba and similar) are not combined with a microtonal scale."),
    "instrument.negation": (ERROR, "The text does not rule out an instrument the style names."),
    "instrument.stacking": (ERROR, "No more than 3 instruments in one part and 6 playing together."),
    "instrument.duplicate": (WARNING, "The same instrument as lead and support is called a 'second' or 'another' one, so two parts are not played by one."),
    "instrument.unrecognized": (WARNING, "A part names no instrument the checker recognises, so it could not be checked."),
    # energy, loudness and wording
    "energy.intense-instrument": (ERROR, "A calm style names no loud or aggressive sound."),
    "energy.loud-claim": (ERROR, "A calm style does not describe itself as aggressive, crushing or explosive."),
    "complexity.loudness": (ERROR, "Complexity and development are never described as getting louder."),
    "text.speech-markup": (ERROR, "No speech tags, announcer or DJ wording in music text."),
    "text.punctuation": (WARNING, "Sentence fields end with a full stop; short phrases do not."),
    "arrangement.instrument": (ERROR, "An arrangement names no instrument (it describes roles and shapes only)."),
    "arrangement.tempo-meter": (ERROR, "An arrangement names no tempo or time signature."),
}


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    subject: str
    field: str
    message: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity, "subject": self.subject,
                "field": self.field, "message": self.message}


# ---------------------------------------------------------------------------------------------
# text helpers

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
}
_BEAT_CLAIM = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r"|\d{1,2})-beat\b", re.IGNORECASE)
_SIGNATURE = re.compile(r"(?<![\w/])(\d{1,2})/(2|4|8|16)(?![\w/])")
_GROUPING = re.compile(r"(?<![\w+])(\d(?:\+\d){1,8})(?![\w+])")
# A meter: one or two plain (ASCII) digits of beats, no leading zero and never zero, over 2, 4, 8 or 16,
# with an optional grouping of single non-zero digits. A bar with no beats is not a meter, and digits
# from other scripts (which Python also reads as numbers) are not accepted.
_METER = re.compile(r"([1-9][0-9]?)/(2|4|8|16)(?: \(([1-9](?:\+[1-9]){1,8})\))?\Z")
_BPM_FIGURE = re.compile(r"\b(\d{2,3})\s?BPM\b", re.IGNORECASE)
_CROSS_CUE = re.compile(
    r"\b(?:against|over|across|hemiola|sesquialtera|cross[- ]rhythms?|superimpos\w*|layer\w*|"
    r"implied|implying|feels?|alternat\w*)\b", re.IGNORECASE)
_GROUP_CUE = re.compile(
    r"\b(?:rotat\w*|rearrang\w*|reorder\w*|displac\w*|shift\w*|regroup\w*|alternat\w*|against|over)\b",
    re.IGNORECASE)
_SLOW_TEMPO = re.compile(
    r"\b(?:very slow|slow|sluggish|languid|crawling)[ -](?:tempo|pulse|pace|ballad|groove|waltz|dance|beat|shuffle|swing)\b",
    re.IGNORECASE)
_FAST_TEMPO = re.compile(
    r"\b(?:very fast|fast|brisk|rapid|breakneck|frantic|frenetic)[ -](?:tempo|pulse|pace|ballad|groove|waltz|dance|beat|shuffle|swing)\b|\bup-?tempo\b",
    re.IGNORECASE)
_HALF_DOUBLE = re.compile(r"\b(?:half-time|double-time|half time|double time)\b", re.IGNORECASE)
_MICROTONAL = re.compile(
    r"\b(?:maqams?|makams?|dastgah|ragas?|slendro|pelog|microtonal|quarter[- ]tones?|shruti|sruti|just intonation)\b",
    re.IGNORECASE)
_RETUNED = re.compile(r"\b(?:retuned|re-tuned|prepared|fretless|detuned|tuned to)\b", re.IGNORECASE)
_NEGATION = re.compile(
    r"\b(?:no|not|never|without|avoid|avoiding|rather than|instead of|nor|neither|nothing)\b", re.IGNORECASE)
_SPEECH_MARKUP = re.compile(
    r"<\|[^|>]*\|>|\[(?:laughs?|whispers?|excited|sighs?|gasps?|coughs?|sniffs?)\]|\bDJs?\b|\bannouncers?\b|"
    r"\bnewsreaders?\b|\bradio host\b", re.IGNORECASE)
# Wording that belongs to a host, not to a piece of music. These come from the lyric guard's list
# (lyric_words.DJ_SPEECH_PHRASES) without the words that have an ordinary musical sense in a style
# description ("studio", "radio", "frequency", "tune in", "this is", "coming up", "listening to"),
# so honest text such as "a warm studio room" is never refused.
HOST_PHRASES = (
    "you're listening", "you are listening", "tuned in", "stay tuned", "welcome to", "welcome back",
    "after the break", "back after", "on the air", "on air", "your host", "your dj", "station id",
    "call sign", "all request", "brought to you", "thanks for listening", "thank you for listening",
    "listeners", "news at", "weather report", "traffic report", "ladies and gentlemen", "good morning",
    "good evening", "good afternoon", "on the dial", "our sponsor", "sponsored by", "commercial break",
    "d.j.",
)
_HOST_WORDS = re.compile("|".join(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)" for phrase in HOST_PHRASES), re.IGNORECASE)
_LOUD_CLAIM = re.compile(
    r"\b(?:aggressive|crushing|ferocious|thunderous|explosive|screaming|brutal|deafening|punishing|bone-rattling)\b",
    re.IGNORECASE)
_LOUDER = re.compile(
    r"\b(?:louder|loudest|loudness|crescendo|fortissimo|climax|climactic|build-?up|"
    r"(?:bass|drum|big|huge|massive|final|sudden) drops?|the drop)\b|\bgetting (?:bigger|heavier)\b",
    re.IGNORECASE)
_INTENSE_WORDS = re.compile(
    r"\b(?:heavy distortion|distorted|overdriven|fuzz(?:ed)?|blast[- ]beats?|double[- ]kick|screamed|growled)\b",
    re.IGNORECASE)

def host_wording(text: str) -> list[str]:
    """The speech tags and host phrases found in ``text``, lower-cased, each once (empty means it is clean)."""
    found: list[str] = []
    straight = text.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    for pattern, source in ((_SPEECH_MARKUP, text), (_HOST_WORDS, straight)):
        for match in pattern.finditer(source):
            word = match.group(0).lower()
            if word not in found:
                found.append(word)
    return found


def speaks_like_a_host(text: str) -> bool:
    """True when music text holds a speech tag, or wording that belongs to a DJ, announcer or station."""
    return bool(host_wording(text))


def host_message(text: str) -> str:
    """The words that were found, quoted for a message (at most three, each cut short)."""
    words = host_wording(text)
    quoted = ", ".join(f'"{shown(word, 30)}"' for word in words[:3])
    return quoted + (f" and {len(words) - 3} more" if len(words) > 3 else "")


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?;])\s+", text) if part]


def _clauses(text: str) -> list[tuple[int, int]]:
    """Spans of comma, semicolon and sentence clauses (and ' but ' splits)."""
    spans = []
    start = 0
    for match in re.finditer(r"[,;.]|\bbut\b", text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return spans


def _negated_spans(text: str) -> list[tuple[int, int]]:
    """Parts of the text that say what is NOT wanted ("no drum kit", "rather than loudness")."""
    found = []
    for start, end in _clauses(text):
        clause = text[start:end]
        negation = _NEGATION.search(clause)
        if negation:
            found.append((start + negation.start(), end))
    return found


_EXCLUDER = re.compile(r"\b(?:no|without|avoid|never use)\s+(\w+)", re.IGNORECASE)


def _exclusions(text: str) -> list[str]:
    """Statements that rule something out ("no drum kit and no heavy distortion").

    "Without crowding the guitar" is about an action, not about leaving the guitar out, so a
    negator followed by an -ing word is skipped.
    """
    found = []
    for start, end in _clauses(text):
        clause = text[start:end]
        match = _EXCLUDER.search(clause)
        if match and not match.group(1).lower().endswith("ing"):
            found.append(clause[match.start():])
    return found


def _positive_text(text: str) -> str:
    """The text with every negated clause blanked out, so 'no distortion' is not read as distortion."""
    chars = list(text)
    for start, end in _negated_spans(text):
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _numerals(text: str) -> Iterable[int]:
    for match in _BEAT_CLAIM.finditer(text):
        token = match.group(1).lower()
        yield _NUMBER_WORDS.get(token) or int(token)


# ---------------------------------------------------------------------------------------------
# palette checks

def _acoustic(item: Instrument) -> bool:
    """A sound source that is plainly acoustic: it plays notes and is not electric, a texture or a bare figure."""
    return (item.has("m") or item.has("b") or item.has("B")) and not any(item.has(flag) for flag in "exn")


def parse_meter(meter: object):
    """``(beats in a bar, note value, declared grouping or None)`` for a meter such as ``7/8 (2+2+3)``.

    Returns None when the text is not a meter. A bar always has at least one beat, so no result
    here has a zero or negative length.
    """
    if not isinstance(meter, str):
        return None
    match = _METER.fullmatch(meter)
    if not match:
        return None
    numerator, denominator = int(match.group(1)), int(match.group(2))
    grouping = tuple(int(n) for n in match.group(3).split("+")) if match.group(3) else None
    return numerator, denominator, grouping


_parse_meter = parse_meter  # the name earlier code used


def _bar_units(numerator: int, denominator: int) -> set[int]:
    """Lengths a grouping may add up to: the bar counted in its own beats, or in eighth notes."""
    units = {numerator}
    if denominator == 4:
        units.add(numerator * 2)
    if denominator == 2:
        units.add(numerator * 4)
    return units


def _allowed_beat_counts(numerator: int, denominator: int) -> set[int]:
    allowed = {numerator}
    if numerator % 2 == 0:
        allowed.add(numerator // 2)
    if denominator == 8 and numerator % 3 == 0:
        allowed.add(numerator // 3)
    if denominator in (2, 4):
        allowed.add(numerator * 2)
    return allowed


def _palette_fields(palette) -> list[tuple[str, str]]:
    """Every free-text field of a palette as (field name, text)."""
    fields: list[tuple[str, str]] = []
    for index, value in enumerate(palette.moods):
        fields.append((f"moods[{index}]", value))
    for index, value in enumerate(palette.leads):
        fields.append((f"leads[{index}]", value))
    for index, value in enumerate(palette.supports):
        fields.append((f"supports[{index}]", value))
    fields.append(("bass", palette.bass))
    for level in ("simple", "layered", "intricate"):
        fields.append((f"harmony.{level}", palette.harmony.at(level)))
    for level in ("simple", "layered", "intricate"):
        fields.append((f"rhythm.{level}", palette.rhythm.at(level)))
    for index, value in enumerate(palette.motifs):
        fields.append((f"motifs[{index}]", value))
    fields.append(("production", palette.production))
    return fields


def check_palette(palette) -> list[Finding]:
    """Check one style palette. Returns findings, most serious first; an empty list means it passed."""
    subject = f"palette:{palette.id}"
    findings: list[Finding] = []

    def add(rule: str, field: str, message: str) -> None:
        findings.append(Finding(rule, RULES[rule][0], subject, field, message))

    fields = _palette_fields(palette)
    tags = set(palette.tags)
    low, high = palette.bpm
    shown_meter = shown(palette.meter)

    # ---- tempo range and energy
    if not (30 <= low <= high <= 240) or high - low > 40:
        add("tempo.range", "bpm",
            f"The tempo range {low} to {high} beats per minute is not usable. Give two numbers between 30 and 240, "
            "no more than 40 apart, so the music has one clear speed.")
    if "half-time" not in tags:
        problem = None
        if palette.energy == "calm" and high > 120:
            problem = "A calm style should not run faster than 120 beats per minute (tag it 'half-time' if it feels slow at a fast count)."
        elif palette.energy == "steady" and (low < 60 or high > 140):
            problem = "A steady style should stay between 60 and 140 beats per minute."
        elif palette.energy == "driving" and high < 96:
            problem = "A driving style should reach at least 96 beats per minute."
        if problem:
            add("tempo.energy", "energy", f"The energy label \"{palette.energy}\" does not fit the tempo ({low} to {high}). {problem}")

    # ---- meter
    meter = _parse_meter(palette.meter)
    numerator = denominator = None
    declared_grouping: tuple[int, ...] | None = None
    if meter is None:
        add("meter.format", "meter",
            f"The meter \"{shown_meter}\" is not written in a way the music maker can follow. "
            "Use a form such as 4/4, 6/8 or 7/8 (2+2+3).")
    else:
        numerator, denominator, declared_grouping = meter
        if declared_grouping and sum(declared_grouping) not in _bar_units(numerator, denominator):
            add("meter.grouping-sum", "meter",
                f"The groups in \"{shown_meter}\" add up to {sum(declared_grouping)}, but a bar of "
                f"{numerator}/{denominator} has {numerator}. Change the groups so they fill the bar exactly.")

    for name, text in fields:
        # tempo figures
        for match in _BPM_FIGURE.finditer(text):
            figure = int(match.group(1))
            marked = bool(_HALF_DOUBLE.search(text))
            inside = low <= figure <= high
            related = marked and any(low <= v <= high for v in (figure * 2, figure / 2))
            if not (inside or related):
                add("tempo.number", name,
                    f"The text says {figure} BPM, but this style runs from {low} to {high}. "
                    "Use a figure inside the range, or say 'half-time' or 'double-time'.")
        # tempo words
        for sentence in _sentences(text):
            if _SLOW_TEMPO.search(sentence) and high > 100 and not _HALF_DOUBLE.search(sentence):
                add("tempo.word", name,
                    f"The text asks for a slow tempo, but this style goes up to {high} beats per minute. "
                    "Lower the range or take out the word 'slow'.")
            if _FAST_TEMPO.search(sentence) and low < 100 and not _HALF_DOUBLE.search(sentence):
                add("tempo.word", name,
                    f"The text asks for a fast tempo, but this style starts at {low} beats per minute. "
                    "Raise the range or take out the word 'fast'.")
        if numerator is None:
            continue
        # time signatures named in the text
        for sentence in _sentences(text):
            for match in _SIGNATURE.finditer(sentence):
                other = (int(match.group(1)), int(match.group(2)))
                if other == (numerator, denominator):
                    continue
                same_bar = other[0] * denominator == numerator * other[1]
                if not (same_bar and _CROSS_CUE.search(sentence)):
                    add("meter.mention", name,
                        f"The text names {other[0]}/{other[1]}, but this style is in {shown_meter}. "
                        "Name another meter only for a deliberate cross-rhythm of the same bar length, "
                        "such as \"6/8 against 3/4\".")
            # beat counts
            for count in _numerals(sentence):
                if count not in _allowed_beat_counts(numerator, denominator) and not _CROSS_CUE.search(sentence):
                    add("meter.beat-count", name,
                        f"The text says \"{count}-beat\", but this style is in {shown_meter}. "
                        "Use a beat count that fits the meter, or describe it as a cross-rhythm.")
            # groupings
            for match in _GROUPING.finditer(sentence):
                group = tuple(int(n) for n in match.group(1).split("+"))
                if group == declared_grouping:
                    continue
                fits_bar = sum(group) in _bar_units(numerator, denominator)
                rearranged = declared_grouping is None or sorted(group) == sorted(declared_grouping)
                cue = bool(_GROUP_CUE.search(sentence) or _CROSS_CUE.search(sentence))
                if not fits_bar or not (rearranged or cue):
                    rule = ("A grouping must add up to the bar and, when the style already has one, rearrange the same groups."
                            if declared_grouping else "A grouping must add up to the bar.")
                    add("meter.grouping-mention", name, f"The grouping {match.group(1)} does not fit {shown_meter}. {rule}")

    # ---- meter tags (a style called a waltz is held to the same rule as one tagged 'waltz')
    labels = tags | set(re.split(r"[^a-z0-9]+", f"{palette.id} {palette.name}".lower()))
    if "waltz" in labels and meter is not None and (numerator, denominator) not in {(3, 4), (6, 8), (9, 8), (3, 8), (12, 8)}:
        add("meter.tag", "tags", f"The style is a waltz but its meter is {shown_meter}. A waltz is in three (3/4, 6/8, 9/8 or 12/8).")
    if "odd-meter" in tags and meter is not None:
        symmetric = (declared_grouping is None or len(set(declared_grouping)) == 1)
        if numerator in (2, 3, 4, 6, 8, 9, 12) and symmetric:
            add("meter.tag", "tags", f"The style is tagged 'odd-meter' but {shown_meter} is an ordinary even or triple meter. Group it unevenly, for example 9/8 (2+2+2+3).")

    # ---- instruments
    lead_items = [(f"leads[{i}]", text) for i, text in enumerate(palette.leads)]
    support_items = [(f"supports[{i}]", text) for i, text in enumerate(palette.supports)]
    role_items = lead_items + support_items + [("bass", palette.bass)]
    mentions: dict[str, list[Instrument]] = {name: find_instruments(text) for name, text in role_items}

    for name, text in lead_items:
        found = mentions[name]
        if found and not any(item.has("m") for item in found):
            add("instrument.lead-melodic", name,
                f"\"{shown(text)}\" cannot carry a melody. Choose an instrument that can play a tune as the lead.")
        if any(item.has("B") for item in found):
            add("instrument.bass-only-elsewhere", name,
                f"\"{shown(text)}\" is a bass instrument. Give it to the bass part and choose another lead.")
    for name, text in support_items:
        if any(item.has("B") for item in mentions[name]):
            add("instrument.bass-only-elsewhere", name,
                f"\"{shown(text)}\" is a bass instrument. Give it to the bass part and choose another supporting part.")
    bass_found = mentions["bass"]
    if not (any(item.has("b") or item.has("B") for item in bass_found) or BASS_ROLE_WORDS.search(palette.bass)):
        add("instrument.bass-slot", "bass",
            f"\"{shown(palette.bass)}\" does not describe a bass part. Name a bass instrument or a low foundation such as a drone or pedal.")
    for name, text in role_items:
        if not mentions[name] and not (name == "bass" and BASS_ROLE_WORDS.search(text)):
            add("instrument.unrecognized", name,
                f"No instrument in \"{shown(text)}\" is one the checker knows, so it could not be checked.")

    # positive mentions anywhere (roles and text) for purity, tradition and tuning
    positive_fields = [(name, _positive_text(text)) for name, text in fields]
    positive_found: dict[str, list[Instrument]] = {name: find_instruments(text) for name, text in positive_fields}
    everything: dict[str, Instrument] = {}
    for items in positive_found.values():
        for item in items:
            everything.setdefault(item.key, item)

    traditions = sorted({item.tradition for item in everything.values() if item.tradition})
    if len(traditions) > 1 and "fusion" not in tags:
        names = ", ".join(f"{item.label} ({item.tradition})" for item in everything.values()
                          if item.tradition and item.tradition in traditions)
        add("instrument.tradition-mix", "instruments",
            f"This style mixes instruments tied to different traditions: {names}. "
            "Keep to one tradition, or add the tag 'fusion' to show the mix is deliberate.")

    role_instruments = {item.key: item for name, _ in role_items for item in mentions[name]}
    if "acoustic" in tags or "unplugged" in tags:
        electric = [item.label for item in role_instruments.values() if item.has("e")]
        if electric:
            add("instrument.purity", "tags",
                f"The style is tagged 'acoustic' but uses {', '.join(electric)}. Remove the tag or use acoustic instruments.")
    if "chiptune" in tags or "synthetic" in tags:
        acoustic = [item.label for item in role_instruments.values() if _acoustic(item)]
        if acoustic:
            add("instrument.purity", "tags",
                f"The style is tagged '{'chiptune' if 'chiptune' in tags else 'synthetic'}' but uses {', '.join(acoustic)}. "
                "Use only electronic sound sources, or remove the tag.")

    claim_text = " ".join([palette.name, " ".join(palette.tags)] + [text for name, text in fields if name.startswith("harmony")])
    if _MICROTONAL.search(claim_text):
        for name, text in role_items:
            for item in mentions[name]:
                if item.has("f") and not _RETUNED.search(text):
                    add("instrument.microtonal", name,
                        f"This style uses a microtonal scale, but \"{shown(text)}\" names a {item.label}, which cannot bend between its fixed notes. "
                        "Use an instrument that can play the scale, or say it is retuned.")

    # text that rules out something the style names
    negated_texts = [(name, clause) for name, text in fields for clause in _exclusions(text)]
    for name, clause in negated_texts:
        for excluded in find_instruments(clause):
            if excluded.key in role_instruments and not excluded.has("x"):
                add("instrument.negation", name,
                    f"The text rules out {excluded.label} (\"{shown(clause.strip())}\"), but the style names it as a part.")
        if re.search(r"\b(?:electronic|electric|amplified|synthesi[sz]ed) (?:instruments|sounds)\b", clause, re.IGNORECASE):
            electric = [item.label for item in role_instruments.values() if item.has("e")]
            if electric:
                add("instrument.negation", name,
                    f"The text rules out electric or electronic instruments, but the style names {', '.join(electric)}.")
        if re.search(r"\b(?:acoustic|sampled)\b", clause, re.IGNORECASE):
            acoustic = [item.label for item in role_instruments.values() if _acoustic(item)]
            if acoustic and ("chiptune" in tags or "synthetic" in tags):
                add("instrument.negation", name,
                    f"The text rules out acoustic instruments, but the style names {', '.join(acoustic)}.")

    for name, text in role_items:
        if len(mentions[name]) > 3:
            add("instrument.stacking", name,
                f"\"{shown(text)}\" names {len(mentions[name])} instruments in one part. Keep each part to three or fewer.")
    worst = 0
    for lead, support in product(lead_items, support_items):
        keys = {item.key for item in mentions[lead[0]] + mentions[support[0]] + mentions["bass"] if not item.has("x")}
        worst = max(worst, len(keys))
    if worst > 6:
        add("instrument.stacking", "instruments",
            f"Up to {worst} different instruments can play at once in this style. Keep it to six or fewer so each part stays clear.")
    for (lead_name, lead_text), (support_name, support_text) in product(lead_items, support_items):
        shared = ({i.key for i in mentions[lead_name]} & {i.key for i in mentions[support_name]}) - {"strings", "drum-kit", "percussion"}
        if shared and not re.search(r"\b(?:second|another|other|doubling|two|pair of|both)\b", support_text, re.IGNORECASE):
            add("instrument.duplicate", support_name,
                f"\"{shown(support_text)}\" uses the same instrument as the lead \"{shown(lead_text)}\". "
                "Call it a 'second' one (for example \"a second guitar\") so the music maker hears two parts, not one.")
            break

    # ---- energy and loudness
    if palette.energy == "calm":
        for item in role_instruments.values():
            if item.has("i"):
                add("energy.intense-instrument", "instruments",
                    f"The style is calm but uses {item.label}, which is loud or aggressive by nature.")
        for name, text in fields:
            positive = _positive_text(text)
            if _LOUD_CLAIM.search(positive) or _INTENSE_WORDS.search(positive):
                add("energy.loud-claim", name,
                    "The style is calm but this text asks for something aggressive or heavy. Describe the character gently, or change the energy.")
    for name, text in fields:
        if name.startswith(("harmony", "rhythm")) or name == "production" or name.startswith("motifs"):
            if _LOUDER.search(_positive_text(text)):
                add("complexity.loudness", name,
                    "This text makes the music more complex by making it louder or by building to a drop. "
                    "Complexity should come from harmony, rhythm and voice leading, not volume.")
    for name, text in [*fields, ("name", palette.name)]:
        if speaks_like_a_host(text):
            add("text.speech-markup", name,
                f"Music text must not contain speech tags, announcer or DJ wording (found {host_message(text)}). "
                "Speech belongs to the host voices, not the music.")

    # ---- punctuation (shape of the compiled prompt)
    for name, text in fields:
        sentence_field = name.startswith(("harmony", "rhythm")) or name == "production"
        if sentence_field and not text.endswith((".", "!", "?")):
            add("text.punctuation", name, "This line should end with a full stop so the compiled prompt reads cleanly.")
        if not sentence_field and text.endswith("."):
            add("text.punctuation", name, "This short phrase should not end with a full stop; the prompt adds its own punctuation.")

    findings.sort(key=lambda f: (f.severity != ERROR, f.rule, f.field))
    return findings


# ---------------------------------------------------------------------------------------------
# arrangement checks

def _arrangement_fields(arrangement) -> list[tuple[str, str]]:
    fields = []
    for level in ("simple", "layered", "intricate"):
        for index, text in enumerate(arrangement.sections.at(level), 1):
            fields.append((f"sections.{level}[{index}]", text))
    for level in ("simple", "layered", "intricate"):
        fields.append((f"development.{level}", arrangement.development.at(level)))
    fields.append(("dynamics", arrangement.dynamics))
    fields.append(("ending", arrangement.ending))
    return fields


def check_arrangement(arrangement) -> list[Finding]:
    """Check one arrangement design. It must stay neutral about instruments, tempo and meter."""
    subject = f"arrangement:{arrangement.id}"
    findings: list[Finding] = []

    def add(rule: str, field: str, message: str) -> None:
        findings.append(Finding(rule, RULES[rule][0], subject, field, message))

    for name, text in _arrangement_fields(arrangement):
        positive = _positive_text(text)
        named = [item for item in find_instruments(positive) if item.key not in {"strings"}]
        if named:
            add("arrangement.instrument", name,
                f"This arrangement names {', '.join(item.label for item in named)}. Arrangements describe roles "
                "(lead, answer, foundation) so they fit any style.")
        if _BPM_FIGURE.search(text) or _SIGNATURE.search(text) or _BEAT_CLAIM.search(text):
            add("arrangement.tempo-meter", name,
                "This arrangement names a tempo or a time signature. The style decides tempo and meter.")
        if _LOUDER.search(positive):
            add("complexity.loudness", name,
                "This text builds interest by getting louder or by a drop. Use register, space and harmony instead.")
        if speaks_like_a_host(text):
            add("text.speech-markup", name,
                f"Music text must not contain speech tags, announcer or DJ wording (found {host_message(text)}). "
                "Speech belongs to the host voices, not the music.")
        if not text.endswith((".", "!", "?")):
            add("text.punctuation", name, "This line should end with a full stop so the compiled prompt reads cleanly.")
    if speaks_like_a_host(arrangement.name):
        add("text.speech-markup", "name",
            f"Music text must not contain speech tags, announcer or DJ wording (found {host_message(arrangement.name)}). "
            "Speech belongs to the host voices, not the music.")
    findings.sort(key=lambda f: (f.severity != ERROR, f.rule, f.field))
    return findings


def check_preset(palette, arrangement) -> list[Finding]:
    """A palette and an arrangement together (used for a user's own preset)."""
    return check_palette(palette) + check_arrangement(arrangement)


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [item for item in findings if item.severity == ERROR]


def check_library(library) -> dict:
    """Run every rule over a whole library. ``ok`` is true when there is no error."""
    found: list[Finding] = []
    for palette in library.palettes:
        found.extend(check_palette(palette))
    for arrangement in library.arrangements:
        found.extend(check_arrangement(arrangement))
    return {
        "ok": not errors(found),
        "palettes": len(library.palettes),
        "arrangements": len(library.arrangements),
        "errors": [item.to_dict() for item in found if item.severity == ERROR],
        "warnings": [item.to_dict() for item in found if item.severity == WARNING],
        "rules": len(RULES),
    }


_REPORTS: dict[str, tuple[object, dict]] = {}


def check_library_cached(library) -> dict:
    """``check_library`` for the shipped, immutable catalog, computed once per process and version."""
    entry = _REPORTS.get(library.version)
    if entry is None or entry[0] is not library:
        entry = _REPORTS[library.version] = (library, check_library(library))
    return entry[1]
