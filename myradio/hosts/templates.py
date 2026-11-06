"""Script templates and the library that holds them.

A template is a list of *beats* (short pieces of speech). The first beat is the core; later beats
are optional extras that the composer adds to reach a target length, unless a beat starts with ``!``
which marks it as one that must stay (a sponsor's own words, for example).

Template syntax (a deliberately small language, no code is ever evaluated):

``{name}``        a value from the facts or the host's phrases. ``{Name}`` capitalises its first letter.
``{? ... ?}``     an optional group, dropped as a whole when any value inside it is missing.
``[laughs]``      an effect tag, kept only if the host is allowed to use it.

Templates never contain digits or number words and never contain capitalised names: every
specific in a finished script comes from a value.
"""
from __future__ import annotations

import dataclasses
import functools
import json
import re
from importlib import resources
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import HostsError
from .lint import number_words_in
from .segments import LEXICON_KEYS, SEGMENT_IDS, SEGMENTS
from .textsafe import EVENT_TAGS

MIN_TEMPLATES = 12
MAX_BEATS = 5
MAX_BEAT_CHARS = 420

_TOKEN = re.compile(r"\{\?|\?\}|\{([A-Za-z][A-Za-z_]*)\}")
_TAG = re.compile(r"\[([a-z][a-z ]{0,24})\]")
_END = re.compile(r"[.!?…][\"'”’)\]]*$")


class _Missing(Exception):
    pass


def parse(text: str) -> tuple:
    """Turn template text into nested nodes: ('lit', text), ('slot', name) and ('opt', nodes)."""
    stack: List[list] = [[]]
    position = 0
    for match in _TOKEN.finditer(text):
        if match.start() > position:
            stack[-1].append(("lit", text[position:match.start()]))
        position = match.end()
        token = match.group(0)
        if token == "{?":
            stack.append([])
        elif token == "?}":
            if len(stack) == 1:
                raise ValueError("an optional group is closed but was never opened")
            inner = stack.pop()
            stack[-1].append(("opt", tuple(inner)))
        else:
            stack[-1].append(("slot", match.group(1)))
    if position < len(text):
        stack[-1].append(("lit", text[position:]))
    if len(stack) != 1:
        raise ValueError("an optional group is opened but never closed")
    return tuple(stack[0])


def _walk(nodes: Iterable[tuple]):
    for kind, payload in nodes:
        yield kind, payload
        if kind == "opt":
            yield from _walk(payload)


def slot_names(nodes: Iterable[tuple], *, required_only: bool = False) -> Set[str]:
    out: Set[str] = set()
    for kind, payload in nodes:
        if kind == "slot":
            out.add(payload[0].lower() + payload[1:])
        elif kind == "opt" and not required_only:
            out |= slot_names(payload)
    return out


def literal_text(nodes: Iterable[tuple]) -> str:
    return " ".join(payload for kind, payload in _walk(nodes) if kind == "lit")


_TIDY_STEPS = (
    (re.compile(r"\s+([,.;:!?…])"), r"\1"),
    (re.compile(r"([!?])[.,;:]+"), r"\1"),
    (re.compile(r"\?{2,}"), "?"),
    (re.compile(r"!{2,}"), "!"),
    (re.compile(r"(?<!\.)\.\.(?!\.)"), "."),
    (re.compile(r"\.{4,}"), "..."),
    (re.compile(r",\s*([,.;:])"), r"\1"),
    (re.compile(r"([;:])\s*,"), r"\1"),
    (re.compile(r"\s{2,}"), " "),
)


_NEEDS_TIDY = re.compile(r"\s[,.;:!?\u2026]|[!?][.,;:]|\?\?|!!|\.\.|,\s*[,.;:]|[;:]\s*,|\s{2,}|^\s|\s$")


def tidy(text: str) -> str:
    """Fix the punctuation seams that appear where a value meets template text."""
    for _ in range(3):
        if not _NEEDS_TIDY.search(text):
            break
        for pattern, replacement in _TIDY_STEPS:
            text = pattern.sub(replacement, text)
    return text.strip()


@dataclasses.dataclass(frozen=True)
class Rendered:
    text: str
    used: FrozenSet[str]
    tags: Tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Beat:
    source: str
    nodes: tuple
    required: FrozenSet[str]
    must: bool
    tags: Tuple[str, ...]

    @classmethod
    def compile(cls, source: str) -> "Beat":
        must = source.startswith("!")
        body = source[1:].lstrip() if must else source
        nodes = parse(body)
        return cls(body, nodes, frozenset(slot_names(nodes, required_only=True)), must,
                   tuple(_TAG.findall(body)))

    def render(self, values: Mapping[str, str]) -> Optional[Rendered]:
        parts: List[Tuple[str, bool]] = []
        used: Set[str] = set()
        try:
            self._fill(self.nodes, values, parts, used)
        except _Missing:
            return None
        pieces = [(p, from_slot) for p, from_slot in parts if p]
        text = "".join(p for p, _ in pieces)
        text = tidy(text)
        if not text:
            return None
        if text[0].islower() and not (pieces and pieces[0][1]):
            text = text[0].upper() + text[1:]
        if not _END.search(text):
            text += "."
        return Rendered(text, frozenset(used), tuple(_TAG.findall(text)))

    def _fill(self, nodes: Sequence[tuple], values: Mapping[str, str], parts: list, used: set) -> None:
        for kind, payload in nodes:
            if kind == "lit":
                parts.append((payload, False))
            elif kind == "slot":
                key = payload[0].lower() + payload[1:]
                value = values.get(key)
                if not value:
                    raise _Missing(key)
                if payload[0].isupper():
                    value = value[0].upper() + value[1:]
                parts.append((value, True))
                used.add(key)
            else:
                inner_parts: list = []
                inner_used: set = set()
                try:
                    self._fill(payload, values, inner_parts, inner_used)
                except _Missing:
                    continue
                parts.extend(inner_parts)
                used |= inner_used


@dataclasses.dataclass(frozen=True)
class Template:
    id: str
    voice: str
    segment: str
    beats: Tuple[Beat, ...]

    @property
    def needs(self) -> FrozenSet[str]:
        needed = set(self.beats[0].required)
        for beat in self.beats[1:]:
            if beat.must:
                needed |= beat.required
        return frozenset(needed)

    @functools.cached_property
    def slots(self) -> FrozenSet[str]:
        """Every value any line of this template could use."""
        return frozenset(name for beat in self.beats for name in slot_names(beat.nodes))


@dataclasses.dataclass(frozen=True)
class Extension:
    id: str
    beat: Beat


@dataclasses.dataclass(frozen=True)
class VoicePack:
    id: str
    label: str
    summary: str
    lexicon: Mapping[str, Tuple[str, ...]]
    templates: Mapping[str, Tuple[Template, ...]]
    extensions: Mapping[str, Tuple[Extension, ...]]

    def to_public(self) -> dict:
        return {"id": self.id, "label": self.label, "summary": self.summary}


# Used when the voice's own scripts cannot be used (or fail the grounding check): short, neutral and
# built only from the facts. Tried in order; the first whose values are all present is used.
PLAIN: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "station_id": (("You're listening to {station}.",),),
    "song_intro": (("Up next, {next_title}{? by {next_artist}?}.",), ("Here is the next song.",)),
    "song_back_announce": (("That was {now_title}{? by {now_artist}?}.",), ("That was the last song.",)),
    "time_check": (("The time is {time}.",),),
    "sign_on": (("Welcome to {station}.",), ("Welcome.",)),
    "sign_off": (("Thank you for listening to {station}.",), ("Thank you for listening.",)),
    "spot_wrapper": (("This message is sponsored by {sponsor}.", "!{sponsor_copy}"),),
    "update_intro": (("Here is an update on {topic}.",), ("Here is an update.",)),
    "update_outro": (("That was the update from {sources}.",), ("That was the update.",)),
    "filler": (("You're listening to {station}.",), ("Thanks for listening.",)),
}
# Facts-free last resort. Contains no name, number or address.
BARE: Dict[str, str] = {
    "station_id": "You're listening to the radio.",
    "song_intro": "Here is the next song.",
    "song_back_announce": "That was the last song.",
    "time_check": "Here is the time check.",
    "sign_on": "Welcome.",
    "sign_off": "Thank you for listening.",
    "spot_wrapper": "This is a sponsored message.",
    "update_intro": "Here is an update.",
    "update_outro": "That was the update.",
    "filler": "Thanks for listening.",
}


def _clean_literal(text: str) -> None:
    for bad in ("<", ">", "|", "\\"):
        if bad in text:
            raise ValueError(f"contains {bad!r}")
    if re.search(r"\d", text):
        raise ValueError("contains a digit")
    if number_words_in(text):
        raise ValueError("contains a number written in words")


def compile_beats(raw: object, label: str, segment: str, vocabulary: Iterable[str]) -> Tuple[Beat, ...]:
    beats_in = [raw] if isinstance(raw, str) else raw
    if not isinstance(beats_in, list) or not 1 <= len(beats_in) <= MAX_BEATS or not all(isinstance(b, str) for b in beats_in):
        raise HostsError(f"{label}: each template must be a line or a list of 1 to {MAX_BEATS} lines.")
    allowed = SEGMENTS[segment].slots
    known_tags = set(vocabulary)
    beats: List[Beat] = []
    for index, source in enumerate(beats_in):
        if not source.strip() or len(source) > MAX_BEAT_CHARS:
            raise HostsError(f"{label}: line {index + 1} is empty or too long.")
        try:
            beat = Beat.compile(source)
            _clean_literal(literal_text(beat.nodes))
        except ValueError as exc:
            raise HostsError(f"{label}: line {index + 1} {exc}.") from None
        unknown = {n for k, n in _walk(beat.nodes) if k == "slot" and n not in allowed}
        if unknown:
            raise HostsError(f"{label}: line {index + 1} uses {sorted(unknown)}, which {segment} does not offer.")
        stray = _TAG.sub("", literal_text(beat.nodes))
        if "[" in stray or "]" in stray:
            raise HostsError(f"{label}: line {index + 1} has a stray bracket.")
        bad_tags = [t for t in beat.tags if t not in known_tags]
        if bad_tags:
            raise HostsError(f"{label}: line {index + 1} uses an unknown effect {bad_tags[0]!r}.")
        beats.append(beat)
    if beats[0].must:
        raise HostsError(f"{label}: the first line is always kept, so it needs no '!'.")
    return tuple(beats)


def load_voice(data: object, filename: str, vocabulary: Iterable[str] = EVENT_TAGS) -> VoicePack:
    vocabulary = tuple(vocabulary)
    if not isinstance(data, dict):
        raise HostsError(f"{filename}: expected an object.")
    voice = data.get("id")
    if not isinstance(voice, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,23}", voice):
        raise HostsError(f"{filename}: bad voice id.")
    label, summary = data.get("label"), data.get("summary")
    if not isinstance(label, str) or not isinstance(summary, str) or not label.strip() or not summary.strip():
        raise HostsError(f"{filename}: a label and a summary are required.")
    raw_lexicon = data.get("lexicon")
    if not isinstance(raw_lexicon, dict) or set(raw_lexicon) != set(LEXICON_KEYS):
        raise HostsError(f"{filename}: the lexicon needs exactly {', '.join(LEXICON_KEYS)}.")
    lexicon: Dict[str, Tuple[str, ...]] = {}
    for key in LEXICON_KEYS:
        items = raw_lexicon[key]
        if not isinstance(items, list) or not items or not all(isinstance(i, str) and i.strip() for i in items):
            raise HostsError(f"{filename}: lexicon {key} must be a list of phrases.")
        lexicon[key] = tuple(items)
    segments = data.get("segments")
    if not isinstance(segments, dict) or set(segments) != set(SEGMENT_IDS):
        raise HostsError(f"{filename}: templates are needed for exactly: {', '.join(SEGMENT_IDS)}.")
    templates: Dict[str, Tuple[Template, ...]] = {}
    for segment in SEGMENT_IDS:
        entries = segments[segment]
        if not isinstance(entries, list) or len(entries) < MIN_TEMPLATES:
            raise HostsError(f"{filename}: {segment} needs at least {MIN_TEMPLATES} templates.")
        built: List[Template] = []
        seen: Set[str] = set()
        for index, entry in enumerate(entries):
            label_here = f"{filename} {segment} #{index + 1}"
            beats = compile_beats(entry, label_here, segment, vocabulary)
            fingerprint = " ".join(b.source.lower() for b in beats)
            if fingerprint in seen:
                raise HostsError(f"{label_here}: duplicates another template.")
            seen.add(fingerprint)
            if segment == "spot_wrapper":
                first = beats[0]
                if "sponsor" not in literal_text(first.nodes).lower() or "sponsor" not in first.required:
                    raise HostsError(f"{label_here}: the first line must say the message is sponsored and name the sponsor.")
                if not any("sponsor_copy" in b.required and b.must for b in beats):
                    raise HostsError(f"{label_here}: the sponsor's message must be a kept line ('!').")
            built.append(Template(f"{voice}.{segment}.{index + 1:02d}", voice, segment, beats))
        templates[segment] = tuple(built)
    extensions: Dict[str, Tuple[Extension, ...]] = {}
    raw_extensions = data.get("extensions", {})
    if not isinstance(raw_extensions, dict):
        raise HostsError(f"{filename}: extensions must be an object.")
    for segment, entries in raw_extensions.items():
        if segment not in SEGMENTS or not SEGMENTS[segment].extendable or not isinstance(entries, list):
            raise HostsError(f"{filename}: {segment} cannot have extra lines.")
        built_ext: List[Extension] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, str):
                raise HostsError(f"{filename}: extra line {index + 1} of {segment} must be text.")
            beats = compile_beats(entry, f"{filename} {segment} extra #{index + 1}", segment, vocabulary)
            built_ext.append(Extension(f"{voice}.{segment}.x{index + 1:02d}", beats[0]))
        extensions[segment] = tuple(built_ext)
    for segment in SEGMENT_IDS:
        if SEGMENTS[segment].extendable and len(extensions.get(segment, ())) < 4:
            raise HostsError(f"{filename}: {segment} needs at least 4 extra lines so long scripts stay varied.")
    return VoicePack(voice, label.strip(), summary.strip(), lexicon, templates, extensions)


class TemplateLibrary:
    def __init__(self, voices: Mapping[str, VoicePack]):
        self.voices: Dict[str, VoicePack] = dict(voices)

    _builtin: Optional["TemplateLibrary"] = None

    @classmethod
    def builtin(cls) -> "TemplateLibrary":
        if cls._builtin is None:
            cls._builtin = cls.from_package()
        return cls._builtin

    @classmethod
    def from_package(cls, vocabulary: Iterable[str] = EVENT_TAGS) -> "TemplateLibrary":
        try:
            folder = resources.files(__package__).joinpath("data")
            names = sorted(entry.name for entry in folder.iterdir()
                           if entry.name.startswith("voice-") and entry.name.endswith(".json"))
        except (OSError, ModuleNotFoundError, AttributeError):
            names = []
        if not names:
            raise HostsError("The built-in host scripts could not be found. Reinstall myradio to restore them.")
        voices: Dict[str, VoicePack] = {}
        for name in names:
            try:
                data = json.loads(folder.joinpath(name).read_text("utf-8"))
            except (OSError, ValueError):
                raise HostsError(f"The built-in host scripts in {name} are damaged. Reinstall myradio to restore them.") from None
            pack = load_voice(data, name, vocabulary)
            if pack.id in voices:
                raise HostsError(f"The built-in host scripts define the voice {pack.id} twice.")
            voices[pack.id] = pack
        return cls(voices)

    def voice(self, voice_id: str) -> VoicePack:
        try:
            return self.voices[voice_id]
        except KeyError:
            raise HostsError("That script style is not available.") from None

    def templates(self, voice_id: str, segment: str) -> Tuple[Template, ...]:
        return self.voice(voice_id).templates[segment]

    def extensions(self, voice_id: str, segment: str) -> Tuple[Extension, ...]:
        return self.voice(voice_id).extensions.get(segment, ())

    def counts(self) -> Dict[str, Dict[str, int]]:
        return {v.id: {s: len(t) for s, t in v.templates.items()} for v in self.voices.values()}
