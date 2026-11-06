"""The template composer: facts in, a script for Drama 3 out.

It never speaks, makes no audio and makes no network call. Every specific in a script comes from
the Facts the caller supplied or from the host's own phrases; :func:`grounding_lint` checks that,
and a script that fails the check is replaced by a plain one (it never goes out with an
unsupported detail).
"""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import re
import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import HostsError
from .facts import Facts, greeting_for, number_words, parse_time, spoken_time
from .lint import grounding_lint
from .memory import Memory
from .personas import Persona
from .rng import MAX_SEED, Rng
from .segments import LEXICON_KEYS, SEGMENTS, Segment
from .templates import BARE, PLAIN, Beat, Extension, Template, TemplateLibrary
from .textsafe import audit_speech_text, speaker_token, strip_markup
from .timing import estimate_raw, estimate_seconds

BORROW_BAND = 0.5      # seconds: lines borrowed to avoid a repeat may differ in length by this much from the nearest one
MAX_EFFECTS = 2
MAX_EXTRA_LINES = 6
EXTRA_CANDIDATES = 7   # extra lines considered when stretching a script
_DISCLOSURE = re.compile(r"\b(sponsor\w*|paid\s+(?:message|advertisement)|advertisement)\b", re.IGNORECASE)
_TAG = re.compile(r"\[([a-z][a-z ]{0,24})\]")
_REQUIRED_MESSAGES = {
    "station": "Add the station's name so the host can say it.",
    "local_time": "Add the local time, for example 21:07, so the host can say it.",
    "sponsor": "Add the sponsor's name so it can be named.",
    "sponsor_copy": "Add the words the sponsor wants read.",
}
# which Facts field a template value comes from (None: it comes from the host, not the facts)
SLOT_FACTS = {"station": "station", "tagline": "tagline", "host": "host", "now_title": "now_title",
              "now_artist": "now_artist", "next_title": "next_title", "next_artist": "next_artist",
              "time": "local_time", "daypart": "daypart", "greeting": "daypart", "weekday": "weekday",
              "note": "notes", "sponsor": "sponsor", "sponsor_copy": "sponsor_copy", "topic": "topic",
              "count": "item_count", "items": "item_count", "here_are": "item_count", "those": "item_count",
              "were": "item_count", "item_noun": "item_count", "sources": "sources"}
_FACT_ORDER = [f.name for f in dataclasses.fields(Facts)]


@dataclasses.dataclass(frozen=True)
class Script:
    """What the composer returns. ``text`` is ready for the speech model; the rest is for people."""
    text: str
    plain_text: str
    speaker: int
    role: str
    library_kind: str
    segment: str
    persona: str
    est_seconds: float
    target_seconds: Optional[float]
    facts_used: Tuple[str, ...]
    warnings: Tuple[str, ...]
    source: str                     # "template", "plain", "llm" or "format"
    template: Optional[str]
    seed: int
    fictional: bool = False
    format: Optional[str] = None
    citations: Tuple[dict, ...] = ()
    skipped: Tuple[dict, ...] = ()
    variants: int = 0               # how many ready-made lines fitted the request (more means more variety)

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        for key in ("facts_used", "warnings", "citations", "skipped"):
            data[key] = list(data[key])
        return data


@dataclasses.dataclass(frozen=True)
class ComposerConfig:
    window: int = 8             # a template is not reused for this many compositions of a kind
    extra_window: int = 2       # extra lines used in this many recent scripts come last in line
    tolerance: float = 0.15     # allowed miss, as a share of the target length ...
    min_tolerance: float = 1.5  # ... but never tighter than this many seconds
    clock: Callable[[], float] = time.time


@dataclasses.dataclass
class _Assembly:
    template: Optional[Template]
    texts: List[str]
    units: List[str]
    used: set
    est: float
    distance: float
    plain: bool = False
    variants: int = 0
    borrowed: bool = False     # picked from outside the lines that fit the length, to avoid repeating a recent line


@dataclasses.dataclass
class Request:
    """A request after its input has been checked: what any writer (templates or the writing assistant) needs."""
    segment: Segment
    facts: Facts                  # cleaned, with the host's name filled in and avoided topics left out
    host_given: bool              # the caller supplied the host's name (it is not just the persona's own)
    seed: int
    rng: Rng
    target: Optional[float]       # seconds steered towards; None for a sponsor message
    warnings: List[str]           # what was cleaned, dropped or clamped on the way in


# Seeds the station makes up stay below 2**53, so they survive a trip through JavaScript (whose numbers are doubles)
# and a script can be repeated from the seed a browser sends back. Seeds a caller supplies may be larger.
DEFAULT_SEED_BITS = 52


def normalise_seed(seed: Any) -> int:
    if seed is None:
        return secrets.randbits(DEFAULT_SEED_BITS)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
        raise HostsError("The seed (a code that repeats a choice) must be a whole number from 0 to 9223372036854775807.")
    return seed


def seconds_value(value: Any) -> float:
    """A length in seconds as a finite float; raises HostsError for anything else (huge integers included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostsError("The length must be a number of seconds.")
    try:
        number = float(value)
    except OverflowError:
        number = float("inf")
    if number != number or number in (float("inf"), float("-inf")):
        raise HostsError("The length must be a number of seconds.")
    return number


def sentence(text: str) -> str:
    """Make a value end like a sentence, so it can stand on its own in a script."""
    text = text.strip()
    if not text:
        return text
    return text if re.search(r"[.!?…][\"'”’)]*$", text) else text + "."


def join_names(names: Sequence[str]) -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    if len(names) == 3:
        return f"{names[0]}, {names[1]} and {names[2]}"
    return f"{names[0]}, {names[1]}, {names[2]} and others"


@functools.lru_cache(maxsize=512)
def _topic_pattern(topic: str) -> "Optional[re.Pattern[str]]":
    """How a topic to avoid is looked for. Plain words match by their first letters, so 'politics' also finds
    'political' and 'politician', and 'religion' finds 'religious': a word of five letters or more is matched by all
    but its last two letters (never fewer than four; a long word ending in 'ation' loses three), a shorter word is
    matched whole. Each word must start a word in the text. A topic with symbols in it is matched exactly as
    written."""
    shown = topic.strip()
    if not shown:
        return None
    words = re.findall(r"[^\W_]+", shown)
    if not words or not re.fullmatch(r"[^\W_]+(?:[\s-]+[^\W_]+)*", shown):
        return re.compile(r"(?<!\w)" + re.escape(shown).replace(r"\ ", r"\s+"), re.IGNORECASE)
    parts = []
    for index, word in enumerate(words):
        keep = len(word) if len(word) <= 4 else max(4, len(word) - 2)
        if word.endswith("ation") and len(word) > 7:
            keep = len(word) - 3        # 'speculation' also finds 'speculate' and 'speculating'
        parts.append(re.escape(word[:keep]) + (r"\w*" if index < len(words) - 1 else ""))
    return re.compile(r"(?<!\w)" + r"[\s-]+".join(parts), re.IGNORECASE)


def blocked_by_topic(text: str, topics: Sequence[str]) -> Optional[str]:
    """The first of a host's topics to avoid that ``text`` mentions, if any (see :func:`_topic_pattern`)."""
    if not isinstance(text, str) or not text:
        return None
    for topic in topics:
        pattern = _topic_pattern(topic) if isinstance(topic, str) else None
        if pattern is not None and pattern.search(text):
            return topic
    return None


def map_tags(text: str, tag_map: Optional[Mapping[str, Optional[str]]]) -> str:
    """Rename effect tags to the names the speech model expects (or drop them), as the operator configured."""
    if not tag_map:
        return text

    def swap(match: "re.Match[str]") -> str:
        emit = tag_map.get(match.group(1), match.group(1))
        return f"[{emit}]" if emit else ""

    text = _TAG.sub(swap, text)
    text = re.sub(r"\s+([,.;:!?\u2026])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def spoken_text(text: str, allowed: Sequence[str]) -> str:
    """``text`` as it will be said by a host allowed only the effects in ``allowed``: the others take no time, so
    they are not counted when lines are chosen to reach a length."""
    if "[" not in text:
        return text
    return re.sub(r"\s{2,}", " ", _TAG.sub(lambda m: m.group(0) if m.group(1) in allowed else "", text)).strip()


def emitted_tags(allowed: Sequence[str], tag_map: Optional[Mapping[str, Optional[str]]]) -> List[str]:
    """The tag names that may appear in a finished script for a host allowed ``allowed``."""
    out = []
    for tag in allowed:
        emit = (tag_map or {}).get(tag, tag)
        if emit:
            out.append(emit)
    return out


def apply_effects(text: str, allowed: Sequence[str], cap: int = MAX_EFFECTS) -> str:
    """Keep only effect tags this host may use (at most ``cap``) and repair the spacing."""
    kept = 0

    def decide(match: "re.Match[str]") -> str:
        nonlocal kept
        if match.group(1) in allowed and kept < cap:
            kept += 1
            return match.group(0)
        return ""

    text = _TAG.sub(decide, text)
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


class TemplateComposer:
    def __init__(self, library: Optional[TemplateLibrary] = None, memory: Optional[Memory] = None,
                 config: Optional[ComposerConfig] = None, tag_map: Optional[Mapping[str, Optional[str]]] = None):
        self.library = library or TemplateLibrary.builtin()
        self.memory = memory if memory is not None else Memory()
        self.config = config or ComposerConfig()
        self.tag_map = dict(tag_map or {})
        self._lock = threading.Lock()

    # ---- public ---------------------------------------------------------------------------
    def compose(self, persona: Persona, segment: str, facts: Any = None, *, seed: Any = None,
                target_seconds: Any = None, remember: bool = True) -> Script:
        """Write one script. With ``remember=False`` this is a dry run: the repetition memory is read but
        not changed, so previews never use up a host's lines."""
        request = self.prepare(persona, segment, facts, seed=seed, target_seconds=target_seconds)
        seg, cleaned, warnings, seed, rng, target = (request.segment, request.facts, request.warnings, request.seed,
                                                     request.rng, request.target)
        host_given = request.host_given
        values = self.slot_values(cleaned, persona, rng)
        with self._lock:
            assembly = self._choose(persona, seg, values, target, rng)
            text, used = self._finish(assembly, persona, seg, cleaned, values, warnings)
            if remember and assembly is not None:
                self.memory.remember(persona.id, seg.id, assembly.units, _digest(text), self.config.clock())
        plain = strip_markup(text)
        est = estimate_seconds(text, persona.wps)
        if target is not None:
            self._length_note(est, target, seg, warnings, borrowed=assembly is not None and assembly.borrowed)
        facts_used = self._facts_used(used, cleaned, host_given)
        return Script(speaker_token(persona.speaker) + text, plain, persona.speaker, seg.role, seg.library_kind, seg.id,
                      persona.id, est, target, facts_used, tuple(warnings),
                      "plain" if assembly is None or assembly.plain else "template",
                      assembly.template.id if assembly is not None and assembly.template else None, seed,
                      variants=assembly.variants if assembly is not None and not assembly.plain else 0)

    # ---- pieces (public for tests and for the other composers) -----------------------------------
    def prepare(self, persona: Persona, segment: Any, facts: Any = None, *, seed: Any = None,
                target_seconds: Any = None) -> Request:
        """Check and clean a request: the kind of script, the facts, the seed and the length. Raises ``HostsError``
        (in plain words) when the request cannot be answered honestly, before any writer is asked."""
        seg = self._segment(segment)
        if persona.language != "en":
            raise HostsError("Scripts can only be written in English for now.")
        cleaned, warnings = Facts.from_any(facts)
        warnings = list(warnings)
        host_given = bool(cleaned.host)
        cleaned = cleaned.with_host(persona.name)
        for field in seg.requires:
            if not getattr(cleaned, field):
                raise HostsError(_REQUIRED_MESSAGES.get(field, f"A {seg.label.lower()} needs more facts."))
        cleaned = self.filter_avoided(cleaned, persona, warnings)
        seed = normalise_seed(seed)
        rng = Rng(seed, persona.id, seg.id)
        target = self._target(persona, seg, target_seconds, rng, warnings)
        return Request(seg, cleaned, host_given, seed, rng, target, warnings)

    def _segment(self, segment: Any) -> Segment:
        seg = SEGMENTS.get(segment) if isinstance(segment, str) else None
        if seg is None:
            raise HostsError("Choose what the host should say: " + ", ".join(SEGMENTS) + ".")
        return seg

    def slot_values(self, facts: Facts, persona: Persona, rng: Rng) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for name in ("station", "host", "now_title", "now_artist", "next_title", "next_artist", "sponsor", "topic"):
            values[name] = getattr(facts, name)
        values["tagline"] = sentence(facts.tagline)
        values["sponsor_copy"] = sentence(facts.sponsor_copy)
        values["weekday"] = facts.weekday
        values["daypart"] = facts.daypart
        values["greeting"] = greeting_for(facts.daypart)
        parsed = parse_time(facts.local_time)
        values["time"] = spoken_time(*parsed) if parsed else ""
        values["note"] = sentence(rng.pick(facts.notes)) if facts.notes else ""
        if facts.item_count is not None:
            single = facts.item_count == 1
            values["count"] = number_words(facts.item_count)
            values["items"] = f"{values['count']} item" + ("" if single else "s")
            values["here_are"] = "here is" if single else "here are"
            values["those"] = "that" if single else "those"
            values["were"] = "was" if single else "were"
            values["item_noun"] = "item" if single else "items"
        values["sources"] = join_names(facts.sources)
        for key in LEXICON_KEYS:
            values[key] = rng.pick(persona.lexicon[key])
        return {k: v for k, v in values.items() if v}

    def filter_avoided(self, facts: Facts, persona: Persona, warnings: List[str]) -> Facts:
        """Leave out the notes, and the topic, that mention something this host does not talk about."""
        if not persona.prohibited_topics or not (facts.notes or facts.topic):
            return facts
        kept = []
        for note in facts.notes:
            hit = blocked_by_topic(note, persona.prohibited_topics)
            if hit:
                warnings.append(f"A note was left out because {persona.name} does not talk about {hit}.")
            else:
                kept.append(note)
        topic = facts.topic
        hit = blocked_by_topic(topic, persona.prohibited_topics) if topic else None
        if hit:
            warnings.append(f"The topic was left out because {persona.name} does not talk about {hit}.")
            topic = ""
        if len(kept) == len(facts.notes) and topic == facts.topic:
            return facts
        return dataclasses.replace(facts, notes=tuple(kept), topic=topic)

    def _target(self, persona: Persona, seg: Segment, requested: Any, rng: Rng, warnings: List[str]) -> Optional[float]:
        if requested is None:
            if seg.id == "spot_wrapper":
                return None        # a sponsor's words set the length; the wrapper adds only what it needs
            low, high = persona.verbosity
            level = low + rng.below(high - low + 1)
            return round(seg.low + (seg.high - seg.low) * (level - 1) / 4, 1)
        number = seconds_value(requested)
        clamped = min(seg.max_s, max(seg.min_s, number))
        if clamped != number:
            warnings.append(f"A {seg.label.lower()} can run from {seg.min_s:g} to {seg.max_s:g} seconds, "
                            f"so {clamped:g} seconds was used.")
        return clamped

    def tolerance(self, target: float) -> float:
        return max(self.config.min_tolerance, self.config.tolerance * target)

    def _length_note(self, est: float, target: float, seg: Segment, warnings: List[str], borrowed: bool = False) -> None:
        if abs(est - target) > self.tolerance(target):
            word = "shorter" if est < target else "longer"
            reason = (f"The closer {seg.label.lower()} lines were used a moment ago, and repeating one would be worse."
                      if borrowed else f"That is as close as this host's {seg.label.lower()} lines allow.")
            warnings.append(f"This runs about {est:g} seconds, {word} than the {target:g} asked for. {reason}")

    def _facts_used(self, used: set, facts: Facts, host_given: bool) -> Tuple[str, ...]:
        fields = set()
        for slot in used:
            field = SLOT_FACTS.get(slot)
            if field is None or (field == "host" and not host_given):
                continue
            if field == "daypart" and slot == "greeting" and not facts.daypart:
                continue
            fields.add(field)
        return tuple(name for name in _FACT_ORDER if name in fields)

    # ---- choosing ----------------------------------------------------------------------------------
    def _render_all(self, template: Template, values: Mapping[str, str]):
        return [beat.render(values) for beat in template.beats]

    def _assemble(self, template: Template, values: Mapping[str, str], target: Optional[float], wps: float,
                  extras: Tuple[Sequence[Extension], Sequence[Extension]], seg: Segment, rng: Rng,
                  effects: Sequence[str] = ()) -> Optional[_Assembly]:
        """Pick which of a template's optional lines (and, for long targets, which extra lines) to say so the
        script lands as close to ``target`` seconds as this template allows. Line lengths are added up
        rather than re-measured, so the search over combinations is cheap."""
        rendered = [beat.render(values) for beat in template.beats]
        if rendered[0] is None:
            return None
        musts = [i for i, beat in enumerate(template.beats) if i > 0 and beat.must]
        if any(rendered[i] is None for i in musts):
            return None
        optional = [i for i, beat in enumerate(template.beats) if i > 0 and not beat.must and rendered[i] is not None]
        base = [0] + musts
        cost = {i: estimate_raw(spoken_text(rendered[i].text, effects), wps) for i in base + optional}
        base_cost = sum(cost[i] for i in base)
        extra_pieces: List[Tuple[Extension, Any, float]] = []
        chosen_extra: List[int] = []
        if target is None:
            chosen = sorted(base + optional)
            total = base_cost + sum(cost[i] for i in optional)
        else:
            tol = self.tolerance(target)
            own = _best(base_cost, [cost[i] for i in optional], target, tol, [])
            chosen = sorted(base + [optional[k] for k in own[1]])
            total = own[0]
            if abs(total - target) > tol and total < target and seg.extendable and (extras[0] or extras[1]):
                taken = {rendered[i].text.lower() for i in range(len(rendered)) if rendered[i] is not None}
                for extra in rng.shuffled(extras[0]) + rng.shuffled(extras[1]):   # lines not used lately come first
                    piece = extra.beat.render(values)
                    if piece is not None and piece.text.lower() not in taken:
                        extra_pieces.append((extra, piece, estimate_raw(spoken_text(piece.text, effects), wps)))
                        taken.add(piece.text.lower())
                    if len(extra_pieces) >= EXTRA_CANDIDATES:
                        break
                both = _best(base_cost, [cost[i] for i in optional] + [c for _, _, c in extra_pieces], target, tol,
                             list(range(len(optional), len(optional) + len(extra_pieces))), MAX_EXTRA_LINES)
                if abs(both[0] - target) < abs(total - target):
                    chosen = sorted(base + [optional[k] for k in both[1] if k < len(optional)])
                    chosen_extra = [k - len(optional) for k in both[1] if k >= len(optional)]
                    total = both[0]
        texts = [rendered[i].text for i in chosen]
        used = set().union(*(rendered[i].used for i in chosen))
        units = [template.id]
        if chosen_extra:
            at = len(texts) - 1 if seg.ext_at == "before_last" and len(texts) > 1 else len(texts)
            lines = [extra_pieces[k] for k in chosen_extra]
            texts = texts[:at] + [piece.text for _, piece, _ in lines] + texts[at:]
            for extra, piece, _ in lines:
                used |= piece.used
                units.append(extra.id)
        distance = abs(total - target) if target is not None else 0.0
        return _Assembly(template, texts, units, used, round(total, 1), distance)

    def _choose(self, persona: Persona, seg: Segment, values: Mapping[str, str], target: Optional[float],
                rng: Rng) -> Optional[_Assembly]:
        available = set(values)
        templates = [t for t in self.library.templates(persona.voice, seg.id) if t.needs <= available]
        wanted = {name for name in seg.key if name in values}
        if wanted:   # lines that cannot use the song's name need not be tried
            templates = [t for t in templates if wanted <= t.slots] or templates
        history = self.memory.recent(persona.id, seg.id, max(self.config.window, self.config.extra_window))
        recent_extras = {u for r in history[: self.config.extra_window] for u in r.units[1:]}
        every = self.library.extensions(persona.voice, seg.id)
        extras = ([e for e in every if e.id not in recent_extras], [e for e in every if e.id in recent_extras])
        effects = [tag for tag in persona.allowed_tags if self.tag_map.get(tag, tag)]    # effects that will be heard
        assemblies: List[_Assembly] = []
        for template in templates:
            # each template gets its own random stream, so adding a template never reshuffles the rest
            made = self._assemble(template, values, target, persona.wps, extras, seg,
                                  Rng(rng.random(), template.id), effects)
            if made is not None:
                assemblies.append(made)
        if not assemblies:
            return None
        tol = self.tolerance(target) if target is not None else float("inf")
        pool = [a for a in assemblies if a.distance <= tol]
        if not pool:
            pool = sorted(assemblies, key=lambda a: a.distance)[:3]
        if wanted:   # a host that was told the song's name should say it
            pool = [a for a in pool if wanted <= a.used] or pool
        window = self.config.window
        last_use: Dict[str, int] = {}          # template id -> how many compositions ago it was used (0 = the last one)
        for age, record in enumerate(history[:window]):
            last_use.setdefault(record.units[0], age)
        in_pool = {id(a) for a in pool}
        fresh = [a for a in pool if a.template.id not in last_use]
        if not fresh and len(pool) <= window:
            # Every line that fits the length was used inside the window. A line that misses the length a little
            # is better than saying the same one again, so borrow the nearest lines that were not used lately
            # (the length note says so).
            spare = [a for a in assemblies if id(a) not in in_pool and a.template.id not in last_use]
            if wanted:
                spare = [a for a in spare if wanted <= a.used] or spare
            spare.sort(key=lambda a: a.distance)
            fresh = [a for a in spare if a.distance <= spare[0].distance + BORROW_BAND][:3] if spare else []
        if not fresh:
            # Nothing at all is unused inside the window, so the window has to shrink to the lines there are: take
            # the one used longest ago, a line that fits the length first among equals.
            everything = list(pool) + [a for a in assemblies if id(a) not in in_pool and (not wanted or wanted <= a.used)]
            oldest = max(last_use.get(a.template.id, window) for a in everything)
            fresh = [a for a in everything if last_use.get(a.template.id, window) == oldest]
            fresh = [a for a in fresh if id(a) in in_pool] or fresh
        picked = rng.pick(fresh)
        picked.borrowed = id(picked) not in in_pool
        picked.variants = len(pool)
        return picked

    # ---- finishing ----------------------------------------------------------------------------------
    def _plain_assembly(self, seg: Segment, values: Mapping[str, str]) -> Optional[_Assembly]:
        for option in PLAIN[seg.id]:
            beats = [Beat.compile(src) for src in option]
            rendered = [b.render(values) for b in beats]
            if rendered[0] is None or any(r is None for r, b in zip(rendered, beats) if b.must):
                continue
            texts = [r.text for r, b in zip(rendered, beats) if r is not None and (b is beats[0] or b.must)]
            used = set().union(*(r.used for r in rendered if r is not None))
            return _Assembly(None, texts, [f"plain:{seg.id}"], used, 0.0, 0.0, plain=True)
        return None

    def _finish(self, assembly: Optional[_Assembly], persona: Persona, seg: Segment, facts: Facts,
                values: Mapping[str, str], warnings: List[str]) -> Tuple[str, set]:
        """Apply the host's effect rules, then the safety checks. Falls back rather than fail."""
        problem: Optional[str] = None
        candidates = [assembly] if assembly is not None else []
        if assembly is None or not assembly.plain:
            candidates.append(self._plain_assembly(seg, values))
        for candidate in (c for c in candidates if c is not None):
            text = map_tags(apply_effects(" ".join(candidate.texts), persona.allowed_tags), self.tag_map)
            issues = grounding_lint(strip_markup(text), facts, persona)
            if not issues and seg.id == "spot_wrapper" and not _DISCLOSURE.search(strip_markup(text)):
                issues = ["The sponsor message did not say that it is sponsored."]
            if not issues and audit_speech_text(speaker_token(persona.speaker) + text, persona.speaker,
                                                emitted_tags(persona.allowed_tags, self.tag_map)):
                issues = ["The script held voice codes that are not allowed."]
            if not issues:
                if candidate.plain and assembly is not None and not assembly.plain and problem:
                    warnings.append("A detail in the script could not be matched to your facts, so a plain version "
                                    f"was used instead. ({problem})")
                if candidate.plain and assembly is None:
                    warnings.append("No ready-made line fitted the facts given, so a plain version was used.")
                if candidate.plain:
                    self._swap_units(assembly, candidate)
                return text, candidate.used
            problem = problem or issues[0]
        if seg.id == "spot_wrapper":
            raise HostsError("The sponsor message could not be prepared safely. Check the sponsor's words and try again.")
        warnings.append("The script could only use a short, general line. " + (f"({problem})" if problem else ""))
        return BARE[seg.id], set()

    @staticmethod
    def _swap_units(original: Optional[_Assembly], plain: _Assembly) -> None:
        if original is not None:
            original.units[:] = plain.units
            original.plain = True
            original.template = None


def _best(base: float, costs: Sequence[float], target: float, tol: float, extra_indexes: Sequence[int],
          max_extra: int = 0) -> Tuple[float, List[int]]:
    """Which of ``costs`` (seconds each) to add to ``base`` to land nearest ``target``. Lines listed in
    ``extra_indexes`` are padding, so each one counts a little against a choice. Returns (total, chosen indexes)."""
    n = len(costs)
    sums = [base] * (1 << n)
    for mask in range(1, 1 << n):
        low = mask & -mask
        sums[mask] = sums[mask ^ low] + costs[low.bit_length() - 1]
    pad = 0
    for k in extra_indexes:
        pad |= 1 << k
    best_mask, best_key = 0, None
    for mask in range(1 << n):
        padding = (mask & pad).bit_count()
        if max_extra and padding > max_extra:
            continue
        distance = abs(sums[mask] - target)
        key = (0, distance + 0.6 * padding) if distance <= tol else (1, distance, padding)
        if best_key is None or key < best_key:
            best_mask, best_key = mask, key
    return sums[best_mask], [k for k in range(n) if best_mask >> k & 1]


def _digest(text: str) -> str:
    return hashlib.sha1(strip_markup(text).lower().encode("utf-8", "replace")).hexdigest()[:16]
