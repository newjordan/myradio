"""A small lexicon of the instruments and sound sources the style library names.

The compatibility checker (``compat.py``) uses it to answer questions the free text of a palette
cannot answer by itself: Can this instrument carry a melody? Does it belong to one specific
tradition? Is it electric or electronic? Does it have fixed (twelve-tone) pitches? It is a
reading aid, not a claim about every instrument in the world: a word that is not in the table is
reported as "not recognised" (a warning), never guessed at.

Flags (one letter each):
  m  melodic        can carry a tune
  b  bass-capable   can fill the bass slot
  B  bass only      belongs in the bass slot, not as a lead or supporting part
  p  percussion     unpitched or rhythm-only
  e  electric or electronic (needs amplification or synthesis)
  f  fixed pitch    cannot bend between the twelve equal-tempered notes
  x  texture        a sound colour or effect rather than a player
  n  neutral        a playing figure that does not say what makes the sound (an arpeggio)
  i  intense        loud or aggressive by nature
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Instrument:
    key: str
    pattern: re.Pattern
    flags: str
    tradition: str | None
    label: str

    def has(self, flag: str) -> bool:
        return flag in self.flags


# (key, regex, flags, tradition, plain-language label). Where two patterns overlap, the longer
# matched text wins ("bass clarinet" is a clarinet, not also a bass).
_TABLE: tuple[tuple[str, str, str, str | None, str], ...] = (
    # --- keyboards
    ("piano", r"\b(?:pianos?|left-hand|right-hand)\b", "mf", None, "piano"),
    ("electric-piano", r"\b(?:electric[- ]pianos?|rhodes|wurlitzer)\b", "mfe", None, "electric piano"),
    ("keyboard", r"\b(?:digital )?keyboards?\b", "mfe", None, "keyboard"),
    ("organ", r"\borgans?\b", "mf", None, "organ"),
    ("electric-organ", r"\b(?:electric|drawbar|bubble[- ]|rotary )organs?\b", "mfe", None, "electric organ"),
    ("harpsichord", r"\bharpsichords?\b", "mf", None, "harpsichord"),
    ("celesta", r"\bcelestas?\b", "mf", None, "celesta"),
    ("music-box", r"\bmusic[- ]box(?:es)?\b", "mf", None, "music box"),
    ("clavinet", r"\bclavinets?\b", "mfe", None, "clavinet"),
    ("harmonium", r"\bharmoniums?\b", "mf", None, "harmonium"),
    ("accordion", r"\baccordions?\b", "mf", None, "accordion"),
    ("concertina", r"\bconcertinas?\b", "mf", None, "concertina"),
    ("bandoneon", r"\bbandoneons?\b", "mf", "tango", "bandoneon"),
    ("melodica", r"\bmelodicas?\b", "mf", None, "melodica"),
    # --- mallets
    ("vibraphone", r"\bvibraphones?\b", "mf", None, "vibraphone"),
    ("marimba", r"\bmarimbas?\b", "mf", None, "marimba"),
    ("mallets", r"\bmallet notes\b", "m", None, "mallet notes"),
    # --- winds and brass
    ("saxophone", r"\b(?:soprano |alto |tenor |baritone )?saxophones?\b", "m", None, "saxophone"),
    ("trumpet", r"\btrumpets?\b", "m", None, "trumpet"),
    ("flugelhorn", r"\bflugelhorns?\b", "m", None, "flugelhorn"),
    ("trombone", r"\btrombones?\b", "m", None, "trombone"),
    ("french-horn", r"\bfrench horns?\b", "m", None, "French horn"),
    ("horns", r"\bhorns?\b", "m", None, "horns"),
    ("brass", r"\bbrass\b", "m", None, "brass"),
    ("tuba", r"\b(?:tubas?|sousaphones?)\b", "mb", None, "tuba"),
    ("bass-clarinet", r"\bbass clarinets?\b", "mb", None, "bass clarinet"),
    ("clarinet", r"\bclarinets?\b", "m", None, "clarinet"),
    ("woodwinds", r"\bwoodwinds?\b", "m", None, "woodwinds"),
    ("flute", r"\b(?:flutes?|piccolos?|recorders?)\b", "m", None, "flute"),
    ("harmonica", r"\bharmonicas?\b", "m", None, "harmonica"),
    ("bagpipe", r"\bbagpipes?\b", "m", None, "bagpipe"),
    ("celtic-pipes", r"\b(?:highland|uilleann|northumbrian|scottish|irish) (?:bag)?pipes?\b|\b(?:tin|penny) whistles?\b", "m", "celtic", "Celtic pipes or whistle"),
    # --- bowed strings
    ("violin", r"\b(?:violins?|fiddles?)\b", "m", None, "violin"),
    ("viola", r"\bviolas?\b", "m", None, "viola"),
    ("cello", r"\bcellos?\b", "mb", None, "cello"),
    ("strings", r"\bstrings\b|\bbowed-string\b|\bstring (?:quartet|ensemble|section)\b|\bstring-quartet\b", "m", None, "strings"),
    # --- plucked strings
    ("harp", r"\bharps?\b|\bharp's\b", "mb", None, "harp"),
    ("guitar", r"\bguitars?\b", "m", None, "guitar"),
    ("electric-guitar", r"\b(?:electric[- ]guitars?|clean electric|funky guitar|delay guitar|chorused guitar|tapped guitar)\b", "me", None, "electric guitar"),
    ("steel-guitar", r"\b(?:pedal steel|steel guitar)\b", "me", None, "pedal steel guitar"),
    ("banjo", r"\bbanjos?\b", "m", None, "banjo"),
    ("mandolin", r"\bmandolins?\b", "m", None, "mandolin"),
    ("bouzouki", r"\bbouzoukis?\b", "m", None, "bouzouki"),
    ("dulcimer", r"\bdulcimers?\b", "m", "appalachian", "dulcimer"),
    # --- electronic sound sources
    ("synthesizer", r"\b(?:brass-like synth(?:s|esi[sz]ers?)?|synthesi[sz]ed brass|synths?|synthesi[sz]ers?|synthesi[sz]ed|polysynths?|sequencers?)\b", "me", None, "synthesizer"),
    ("pad", r"\b(?:string |chord )?pads?\b", "ex", None, "pad"),
    ("pulse-wave", r"\b(?:pulse|square)[- ]wave\b", "me", None, "pulse-wave tone"),
    ("chip-channel", r"\b(?:noise|triangle|pulse)[- ]channel\b|\bone channel\b", "me", None, "chip channel"),
    ("chord-stab", r"\b(?:minor-)?chord stabs?\b|\bmetallic chord pulses\b", "me", None, "chord stabs"),
    ("plucks", r"\bplucks\b", "mn", None, "plucked tones"),
    ("arpeggio", r"\barpeggios?\b", "mn", None, "arpeggios"),
    ("drum-machine", r"\bdrum machines?\b", "pe", None, "drum machine"),
    ("sampler", r"\b(?:sampler|sampled)\b", "e", None, "sampler"),
    # --- tradition-bound melodic instruments
    ("kora", r"\bkoras?\b", "m", "west-african", "kora"),
    ("balafon", r"\bbalafons?\b", "mf", "west-african", "balafon"),
    ("ngoni", r"\bngonis?\b", "m", "west-african", "ngoni"),
    ("oud", r"\bouds?\b", "m", "near-east", "oud"),
    ("qanun", r"\b(?:qanuns?|kanuns?)\b", "m", "near-east", "qanun"),
    ("ney", r"\bney\b", "m", "near-east", "ney"),
    ("saz", r"\bsaz\b", "m", "near-east", "saz"),
    ("sitar", r"\bsitars?\b", "m", "indian", "sitar"),
    ("sarangi", r"\bsarangis?\b", "m", "indian", "sarangi"),
    ("bansuri", r"\bbansuri\b", "m", "indian", "bansuri"),
    ("tanpura", r"\b(?:tanpuras?|tamburas?)\b", "mb", "indian", "tanpura"),
    ("bass-koto", r"\bbass koto\b", "mb", "japanese", "bass koto"),
    ("koto", r"\bkotos?\b", "m", "japanese", "koto"),
    ("shakuhachi", r"\bshakuhachi\b", "m", "japanese", "shakuhachi"),
    ("shamisen", r"\bshamisen\b", "m", "japanese", "shamisen"),
    ("guzheng", r"\bguzheng\b", "m", "chinese", "guzheng"),
    ("erhu", r"\berhu\b", "m", "chinese", "erhu"),
    ("pipa", r"\bpipa\b", "m", "chinese", "pipa"),
    ("dizi", r"\bdizi\b", "m", "chinese", "dizi"),
    ("gender", r"\b(?:gender|saron)\b", "m", "gamelan", "gender"),
    ("bonang", r"\b(?:bonang|kenong)\b", "m", "gamelan", "bonang"),
    ("slentem", r"\bslentem\b", "mb", "gamelan", "slentem"),
    ("gong", r"\bgongs?\b", "pb", "gamelan", "gong"),
    ("suling", r"\bsuling\b", "m", "gamelan", "suling"),
    ("nyckelharpa", r"\bnyckelharpa\b", "m", "nordic", "nyckelharpa"),
    ("gaida", r"\bgaida\b", "m", "balkan", "gaida"),
    ("gadulka", r"\bgadulka\b", "m", "balkan", "gadulka"),
    ("charango", r"\bcharango\b", "m", "andean", "charango"),
    ("quena", r"\bquena\b", "m", "andean", "quena"),
    ("panpipes", r"\b(?:zampona|siku|panpipes?)\b", "m", "andean", "panpipes"),
    ("gaita", r"\bgaita\b", "m", "colombian", "gaita flute"),
    ("requinto", r"\brequinto(?: jarocho)?\b", "m", "mexican", "requinto"),
    ("jarana", r"\bjarana\b", "m", "mexican", "jarana"),
    ("arpa-jarocha", r"\barpa jarocha\b", "mb", "mexican", "arpa jarocha"),
    ("cavaquinho", r"\bcavaquinho\b", "m", "brazilian", "cavaquinho"),
    ("steelpan", r"\b(?:steelpans?|steel pans?|double-second|tenor pan)\b", "mf", "steelpan", "steelpan"),
    ("bass-pan", r"\bbass pans?\b", "bBf", "steelpan", "bass pan"),
    # --- bass-only instruments
    ("upright-bass", r"\b(?:upright|double|acoustic|fretless|fingerstyle|organ) bass(?:es)?\b|(?<!-)\bstring bass(?:es)?\b", "bB", None, "upright bass"),
    ("electric-bass", r"\b(?:electric bass|bass guitar|synth bass|sub-bass|log-drum bass|triangle-wave bass|bass synth)\b", "bBe", None, "electric or synthesized bass"),
    # --- percussion
    ("drum-kit", r"\b(?:drum kit|snares?|hi-hats?|kicks?|ride cymbal|cymbals?|brushes|rim[- ]clicks?)\b", "p", None, "drum kit"),
    ("drums", r"\bdrums\b", "p", None, "drums"),
    ("bass-drum", r"\bbass drums?\b", "p", None, "bass drum"),
    ("latin-percussion", r"\b(?:congas?|bongos?|timbales|claves|maracas|guiro|cuica)\b", "p", None, "Latin percussion"),
    ("shaker", r"\b(?:shakers?|tambourines?|scraper|wood ?blocks?)\b", "p", None, "shaker or small percussion"),
    ("frame-drum", r"\b(?:frame drums?|hand drums?|cajon|calabash)\b", "p", None, "frame or hand drum"),
    ("bodhran", r"\bbodhrans?\b", "p", "celtic", "bodhran"),
    ("tapan", r"\btapan\b", "p", "balkan", "tapan"),
    ("darbuka", r"\b(?:darbukas?|doumbek|riq|bendir)\b", "p", "near-east", "darbuka"),
    ("tabla", r"\b(?:tabla|mridangam|ghatam)\b", "p", "indian", "tabla"),
    ("kendang", r"\bkendang\b", "p", "gamelan", "kendang"),
    ("taiko", r"\btaiko\b", "p", "japanese", "taiko"),
    ("bombo", r"\bbombo\b", "p", "andean", "bombo"),
    ("colombian-drums", r"\b(?:alegre|llamador|guacharaca)\b", "p", "colombian", "cumbia drums"),
    ("pandeiro", r"\bpandeiro\b", "p", "brazilian", "pandeiro"),
    ("tarima", r"\b(?:tarima|zapateado)\b", "p", "mexican", "tarima footwork"),
    ("palmas", r"\bpalmas\b", "p", "flamenco", "palmas"),
    ("handclap", r"\bhandclaps?\b", "p", None, "handclaps"),
    ("stomp", r"\bfoot stomp\b", "p", None, "foot stomp"),
    ("percussion", r"\bpercussion\b", "p", None, "percussion"),
    # --- loud or aggressive sounds (a calm style names none)
    ("distortion", r"\b(?:heavy distortion|distorted|overdriven|fuzz(?:ed)?)\b", "xi", None, "heavy distortion"),
    ("blast-beat", r"\b(?:blast[- ]beats?|double[- ]kick)\b", "pi", None, "blast beats"),
    # --- textures that are not players
    ("texture", r"\b(?:vinyl crackle|tape hiss|crackle)\b", "x", None, "vinyl or tape texture"),
)

TRADITIONS = tuple(sorted({row[3] for row in _TABLE if row[3]}))

INSTRUMENTS: tuple[Instrument, ...] = tuple(
    Instrument(key, re.compile(pattern, re.IGNORECASE), flags, tradition, label)
    for key, pattern, flags, tradition, label in _TABLE
)
_BY_KEY = {item.key: item for item in INSTRUMENTS}
if len(_BY_KEY) != len(INSTRUMENTS):  # pragma: no cover - guards edits to the table
    raise RuntimeError("duplicate instrument key in the lexicon")


def instrument(key: str) -> Instrument:
    return _BY_KEY[key]


@lru_cache(maxsize=4096)
def _find(text: str) -> tuple[Instrument, ...]:
    matches = []
    for item in INSTRUMENTS:
        for match in item.pattern.finditer(text):
            matches.append((match.start(), match.end(), item))
    if len(matches) > 1:
        matches.sort(key=lambda m: (-(m[1] - m[0]), m[0]))
        accepted: list[tuple[int, int, Instrument]] = []
        for start, end, item in matches:
            if all(end <= a_start or start >= a_end for a_start, a_end, _ in accepted):
                accepted.append((start, end, item))
        matches = sorted(accepted, key=lambda m: m[0])
    seen: set[str] = set()
    result = []
    for _, _, item in matches:
        if item.key not in seen:
            seen.add(item.key)
            result.append(item)
    return tuple(result)


def find_instruments(text: str) -> list[Instrument]:
    """Instruments named in ``text``, in order of first appearance, each once.

    Where two patterns match overlapping words the longer match wins, so "bass clarinet" counts
    as a clarinet and not also as a bass, and "synthesized brass" is a synthesizer, not brass.
    """
    return list(_find(text))


# Words that make a phrase a *role description* for the bass slot even without a named instrument,
# for example "occasional low piano pedals" or "deep melodic bass".
BASS_ROLE_WORDS = re.compile(
    r"\b(?:bass|sub-bass|low (?:sustained )?tones?|low (?:piano )?pedals?|low strings?|low notes|drone|pedal tones?)\b",
    re.IGNORECASE,
)
