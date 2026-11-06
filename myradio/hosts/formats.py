"""Entertainment formats: short made-up pieces a host can perform between songs.

They are fiction, and the composer says so. Every format carries ``fictional=True`` and starts
with a one-line plain disclosure, so a listener can never take a poem or a forecast for news.
They use only word lists and the Facts the caller supplied (the almanac states nothing else), so
the same grounding check as every other script applies.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Dict, List, Optional, Tuple

from .composer import Script, _digest, normalise_seed, seconds_value
from .errors import HostsError
from .facts import Facts
from .lint import grounding_lint
from .personas import Persona
from .rng import Rng
from .segments import KIND_SPEECH, ROLE_HOST_BREAK
from .textsafe import audit_speech_text, speaker_token, strip_markup
from .timing import estimate_seconds

ADJECTIVES = (
    "velvet crooked luminous tangled hollow sleepy electric paper brass quiet borrowed glittering patient wobbly "
    "enormous tiny moonlit dusty ticking polite restless woolen magnetic crumpled copper gentle sideways invisible "
    "rusty sparkling floating stubborn melted knitted echoing folded humming peculiar salted orbiting").split()
NOUNS = (
    "lantern teapot comet staircase orchard metronome umbrella glacier kettle compass lighthouse bicycle accordion "
    "pigeon marble weathervane telescope sandwich harbor typewriter cactus balloon windmill postcard sofa whistle "
    "ladder pancake thimble satellite button harmonica raincoat anchor cathedral mailbox lamp trombone pocket teacup "
    "meadow").split()
VERBS = (
    "borrows|unravels|hums to|argues with|drifts past|sings to|forgets|polishes|balances|follows|whispers to|winks at|"
    "tickles|orbits|ignores|folds|answers|carries|interrupts|waves at|tunes|mends|paints|overtakes|adopts").split("|")
PLACES = (
    "the attic|a tin harbor|the back of the moon|a borrowed kitchen|the middle of a rainstorm|an empty ballroom|"
    "the end of the hallway|a field of radios|the tallest shelf|a sleepy lighthouse|the corner of the sky|"
    "a quiet elevator|the bottom of a teacup|a cloud with a doorbell|the edge of the map|a room made of curtains").split("|")
ENDINGS = (
    "And that is all the poem knows.|Nobody asked, but here we are.|The end, or maybe just a pause.|"
    "Nothing in it is true, and all of it is nice.|That is the whole poem, and it is leaving now.").split("|")
SAYINGS = (
    "The almanac says a song well chosen is a song half played.|"
    "The almanac holds that the best time for music is whenever there is music.|"
    "The almanac advises keeping one ear on the radio and the other on the kettle.|"
    "The almanac recommends humming along, but only if no one is sleeping.|"
    "The almanac predicts a moderate chance of toe tapping.|"
    "The almanac says every song ends, and that is how the next one begins.|"
    "The almanac suggests drinking something warm and listening to something warmer.|"
    "The almanac notes that the volume knob is a good friend.|"
    "The almanac observes that headphones are a very small concert hall.|"
    "The almanac reminds you that rhythm is just walking with confidence.|"
    "The almanac believes a good song is a small holiday.|"
    "The almanac insists that silence is only music taking a breath.").split("|")
WEATHER = (
    "scattered synths|gentle brass|patches of warm bass|a light drizzle of piano|passing clouds of reverb|"
    "drifting guitars|a breeze of strings|a steady rain of snare|low fog over the bass|sunny spells of melody|"
    "a front of soft drums|waves of slow harmony").split("|")


# The format's own words. They hold no numbers or names, so they may ground the words of a script.
VOCABULARY = tuple(" ".join(group) for group in (ADJECTIVES, NOUNS, VERBS, PLACES, ENDINGS, WEATHER)) + tuple(SAYINGS)


def a_an(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


@dataclasses.dataclass(frozen=True)
class Format:
    id: str
    label: str
    summary: str
    disclosure: str                   # one plain line, said first
    needs: Tuple[str, ...]            # facts it can use (none is required)
    default_seconds: float
    min_s: float
    max_s: float
    build: Callable[["Rng", "Persona", "Facts", Dict[str, str], int], Tuple[List[str], List[str]]]

    def to_public(self) -> dict:
        return {"id": self.id, "label": self.label, "summary": self.summary, "disclosure": self.disclosure,
                "fictional": True, "uses": list(self.needs), "default_seconds": self.default_seconds,
                "min_seconds": self.min_s, "max_seconds": self.max_s}


def _poem_line(rng: Rng, persona: Persona, index: int) -> str:
    adj1, adj2 = rng.pick(ADJECTIVES), rng.pick(ADJECTIVES)
    n1, n2 = rng.pick(NOUNS), rng.pick(NOUNS)
    verb, place = rng.pick(VERBS), rng.pick(PLACES)
    pattern = index % 6
    if pattern == 0:
        return f"The {adj1} {n1} {verb} the {adj2} {n2}."
    if pattern == 1:
        return f"{a_an(adj1).capitalize()} {n1} {verb} {a_an(adj2)} {n2} in {place}."
    if pattern == 2:
        return f"In {place}, the {n1} {verb} the {n2}."
    if pattern == 3:
        return f"{adj1.capitalize()} {n1}, {adj2} {n2}, {rng.pick(ADJECTIVES)} {rng.pick(NOUNS)}."
    if pattern == 4:
        return f"Nobody noticed that the {adj1} {n1} {verb} the {n2}."
    return f"From {rng.pick(persona.lexicon['place'])}, the {adj1} {n1} {verb} the {adj2} {n2}."


def _build_poem(rng: Rng, persona: Persona, facts: Facts, values: Dict[str, str], lines: int) -> Tuple[List[str], List[str]]:
    start = rng.below(6)
    body = [_poem_line(rng, persona, start + i) for i in range(lines)]
    return body + [rng.pick(ENDINGS)], []


def _build_almanac(rng: Rng, persona: Persona, facts: Facts, values: Dict[str, str], lines: int) -> Tuple[List[str], List[str]]:
    facty: List[Tuple[str, str]] = []
    if facts.station:
        facty.append((f"This is the {facts.station} almanac.", "station"))
    if values.get("tagline"):
        facty.append((f"The motto reads: {values['tagline']}", "tagline"))
    facty.append((f"The host on duty is {facts.host}.", "host"))
    if facts.now_title:
        facty.append((f"The song just played was {facts.now_title}" + (f" by {facts.now_artist}." if facts.now_artist else "."),
                      "now_title"))
    if facts.next_title:
        facty.append((f"The song coming up is {facts.next_title}" + (f" by {facts.next_artist}." if facts.next_artist else "."),
                      "next_title"))
    if values.get("time"):
        facty.append((f"The almanac clock reads {values['time']}.", "local_time"))
    if facts.weekday:
        facty.append((f"The day is {facts.weekday}.", "weekday"))
    if values.get("note"):
        facty.append((f"A note pinned to the almanac says: {values['note']}", "notes"))
    pool_f = facty[:1] + rng.shuffled(facty[1:])
    pool_s = rng.shuffled(SAYINGS)
    out: List[str] = []
    used: List[str] = []
    for index in range(max(1, lines)):
        if (index % 2 == 0 and pool_f) or (not pool_s and pool_f):
            line, field = pool_f.pop(0)
            out.append(line)
            used.append(field)
        elif pool_s:
            out.append(pool_s.pop(0))
    return out, used


def _build_forecast(rng: Rng, persona: Persona, facts: Facts, values: Dict[str, str], lines: int) -> Tuple[List[str], List[str]]:
    used: List[str] = []
    opener = "Here is the airwave forecast."
    if facts.daypart:
        opener = f"Here is the airwave forecast for the {facts.daypart} hours."
        used.append("daypart")
    w = rng.shuffled(WEATHER)
    candidates = [f"Expect {w[0]}, with {w[1]} later.", f"There is a good chance of {w[2]}.",
                  f"Winds will carry {w[3]} across the dial.", f"Look out for {w[4]} and {w[5]}.",
                  f"The outlook is {w[6]}, clearing to {w[7]}.", f"Stay alert for {w[8]} around the middle of the song."]
    start = rng.below(len(candidates))
    body = [candidates[(start + i) % len(candidates)] for i in range(max(1, lines))]
    if facts.station:
        closing = f"That is the forecast from {facts.station}."
        used.append("station")
    else:
        closing = "That is the forecast."
    return [opener] + body + [closing], used


FORMATS: Dict[str, Format] = {f.id: f for f in (
    Format("word-salad-poem", "Word-salad poem", "A short, surreal poem made from word lists. Pure make-believe.",
           "This next bit is a made-up poem, and none of it is real.", (), 22, 15, 40, _build_poem),
    Format("station-almanac", "Station almanac",
           "Playful sayings mixed with facts the station really has (its name, host, songs and time).",
           "This almanac is playful, so the sayings are made up and only the facts are real.",
           ("station", "tagline", "host", "now_title", "now_artist", "next_title", "next_artist", "local_time", "weekday", "notes"),
           22, 14, 40, _build_almanac),
    Format("airwave-forecast", "Airwave forecast", "A pretend weather report for the music. Not a real forecast.",
           "This forecast is made up for fun and is not a real weather report.", ("station", "daypart"), 26, 21, 40, _build_forecast),
)}


def compose_format(composer: Any, persona: Persona, format_id: Any, facts: Any = None, *, seed: Any = None,
                   target_seconds: Any = None, remember: bool = True) -> Script:
    """Write a fictional piece. Returns a Script flagged ``fictional`` whose text begins with the
    format's plain disclosure."""
    fmt = FORMATS.get(format_id) if isinstance(format_id, str) else None
    if fmt is None:
        raise HostsError("Choose a format: " + ", ".join(FORMATS) + ".")
    if persona.language != "en":
        raise HostsError("Scripts can only be written in English for now.")
    cleaned, warnings = Facts.from_any(facts)
    warnings = list(warnings)
    host_given = bool(cleaned.host)
    cleaned = cleaned.with_host(persona.name)
    cleaned = composer.filter_avoided(cleaned, persona, warnings)
    seed = normalise_seed(seed)
    target = fmt.default_seconds
    if target_seconds is not None:
        number = seconds_value(target_seconds)
        target = min(fmt.max_s, max(fmt.min_s, number))
        if target != number:
            warnings.append(f"A {fmt.label.lower()} can run from {fmt.min_s:g} to {fmt.max_s:g} seconds, so {target:g} seconds was used.")
    kind = f"format:{fmt.id}"
    with composer._lock:
        seen = {r.digest for r in composer.memory.recent(persona.id, kind, composer.config.window)}
        picked: Optional[Tuple[str, List[str]]] = None
        for attempt in range(6):
            values = composer.slot_values(cleaned, persona, Rng(seed, persona.id, fmt.id, attempt))
            best: Optional[Tuple[float, List[str], List[str]]] = None
            for lines in range(1, 10):
                body, used = fmt.build(Rng(seed, persona.id, fmt.id, attempt, "lines"), persona, cleaned, values, lines)
                parts = [fmt.disclosure] + body
                distance = abs(estimate_seconds(" ".join(parts), persona.wps) - target)
                if best is None or distance < best[0]:
                    best = (distance, parts, used)
            assert best is not None
            text = " ".join(best[1])
            if picked is None:
                picked = (text, best[2])
            if _digest(text) not in seen:
                picked = (text, best[2])
                break
        assert picked is not None
        text, used_fields = picked
        issues = grounding_lint(strip_markup(text), cleaned, persona, extra=VOCABULARY)
        if issues or audit_speech_text(speaker_token(persona.speaker) + text, persona.speaker, persona.allowed_tags):
            # word lists should never trip the check; if one does, say only the notice and stop
            warnings.append("This piece could not be checked against your facts, so only its notice was kept.")
            text, used_fields = fmt.disclosure, []
        if remember:
            composer.memory.remember(persona.id, kind, (kind,), _digest(text), composer.config.clock())
    est = estimate_seconds(text, persona.wps)
    if abs(est - target) > composer.tolerance(target):
        word = "shorter" if est < target else "longer"
        warnings.append(f"This runs about {est:g} seconds, {word} than the {target:g} asked for.")
    used = tuple(name for name in dict.fromkeys(used_fields) if name != "host" or host_given)
    return Script(speaker_token(persona.speaker) + text, strip_markup(text), persona.speaker, ROLE_HOST_BREAK, KIND_SPEECH,
                  kind, persona.id, est, target, used, tuple(warnings), "format", None, seed,
                  fictional=True, format=fmt.id)
