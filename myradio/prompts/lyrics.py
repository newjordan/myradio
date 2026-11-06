"""Surreal-but-singable lyric outlines for vocal music.

``lyrics_scaffold(preset, seed)`` writes a section-tagged lyric skeleton for YuE2 (the music model),
shaped by the preset: the syllables per line follow the style's meter, every section carries a
rhyme plan, and one short hook phrase returns in the chorus, the bridge and the outro.

This is a *separate path* from supplied lyrics. Supplied lyrics are the operator's own words and are
passed through untouched. A scaffold is generated text from a small word bank, always marked
``source: "scaffold"``, and it never contains DJ, announcer or station wording and no speech tags:
host and announcer speech belongs to Drama 3 and never enters a music prompt.

The result is an outline, not a finished lyric. It makes no claim to be good poetry and no claim
about how the model will sing it.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from itertools import product

from .compat import parse_meter
from .library import MUSIC_MODEL, Preset, _seed, shared_library
from .lyric_words import (
    DJ_SPEECH_PHRASES, PREPOSITIONS, PRONOUNS, RHYME_GROUPS, VERBS_I, VERBS_T, WORLDS, EndWord, World,
)
from .textutil import shown

SCAFFOLD_VERSION = 1
TAG_STYLES = ("title", "lower")
FORMS = ("short", "full")
MAX_SCAFFOLD_CHARS = 2400
_FORM_SECTIONS = {
    "short": ("verse", "chorus", "verse", "chorus", "outro"),
    "full": ("verse", "chorus", "verse", "chorus", "bridge", "chorus", "outro"),
}
_VERSE_SCHEMES = ("ABAB", "AABB", "ABCB")
_TAG = re.compile(r"\[(?:Verse \d|Chorus|Bridge|Outro|verse|chorus|bridge|outro)\]\Z")
_VOWELS = "aeiou"

# Slots are filled with words of a chosen syllable count; literal phrases have fixed counts.
_LITERALS = {
    "are": 1, "is": 1, "learns to": 2, "learn to": 2, "help the": 2, "helps the": 2, "let the": 2,
}
# agreement: "pron" (we/you/they + base verb), "np" (the X + third person verb), "imp" (base verb first)
_FRAMES = {
    "n": (
        ("pron", ("P", "VT", "D", "OA?", "E")),
        ("pron", ("P", "VT", "D", "ON", "PT", "E")),
        ("pron", ("P", "VI", "PT", "OA?", "E")),
        ("np", ("D", "SA?", "SN", "VT", "D", "OA?", "E")),
        ("np", ("D", "SA?", "SN", "VI", "PT", "OA?", "E")),
        ("imp", ("VT", "D", "OA?", "E")),
        ("imp", ("VT", "D", "ON", "PT", "E")),
    ),
    "a": (
        ("np", ("D", "SA?", "SN", "is", "E")),
        ("pron", ("P", "VT", "D", "OA?", "ON", "E")),
        ("np", ("D", "SA?", "SN", "VT", "D", "OA?", "ON", "E")),
        ("imp", ("VT", "D", "OA?", "ON", "E")),
    ),
    "v": (
        ("np", ("D", "SA?", "SN", "learns to", "E")),
        ("pron", ("P", "help the", "OA?", "SN", "E")),
        ("imp", ("let the", "SA?", "SN", "E")),
        ("np", ("D", "SA?", "SN", "helps the", "OA?", "ON", "E")),
    ),
}
_VARIABLE = {
    "P": (1,), "D": (1,), "SA?": (0, 1, 2, 3), "OA?": (0, 1, 2, 3), "SN": (1, 2, 3), "ON": (1, 2, 3),
    "VT": (1, 2, 3), "VI": (1, 2), "PT": (2, 3),
}


# ---------------------------------------------------------------------------------------------
# helpers shared with the tests

_SYLLABLE_EXCEPTIONS = {"eye": 1, "eyes": 1}


def count_syllables(word: str) -> int:
    """A rough English syllable counter (vowel groups with the usual silent-letter fixes).

    It is used to cross-check the word bank's declared counts, not to write lyrics: the bank is
    authoritative for generation. A "y" counts as a vowel unless a vowel follows it ("beyond").
    """
    text = re.sub(r"[^a-z]", "", word.lower())
    if not text:
        return 0
    if text in _SYLLABLE_EXCEPTIONS:
        return _SYLLABLE_EXCEPTIONS[text]
    count = len(re.findall(r"(?:[aeiou]|y(?![aeiou]))+", text))
    if text.endswith("e") and not text.endswith("ee") and count > 1:
        if not (text.endswith("le") and len(text) > 2 and text[-3] not in "aeiouy"):
            count -= 1
    elif text.endswith("es") and count > 1 and len(text) > 2 and text[-3] not in "aeiouysxzcgl":
        count -= 1
    elif text.endswith("ed") and count > 1 and len(text) > 2 and text[-3] not in "aeiouytd":
        count -= 1
    return max(count, 1)


_DJ_PATTERN = re.compile(
    "|".join(r"(?<!\w)" + re.escape(phrase.strip()) + r"(?!\w)" for phrase in DJ_SPEECH_PHRASES),
    re.IGNORECASE,
)


def contains_dj_speech(text: str) -> list[str]:
    """Phrases in ``text`` that sound like a radio host or announcer (empty means clean).

    Whole words only: "right afternoon" does not contain "right after".
    """
    found = []
    for match in _DJ_PATTERN.finditer(text):
        phrase = match.group(0).casefold()
        if phrase not in found:
            found.append(phrase)
    return found


def line_syllables_for_meter(meter: str) -> int:
    """Syllables per line for a meter such as ``6/8`` or ``7/8 (2+2+3)``: one eighth note per syllable
    over one bar, halved when a bar is longer than ten and doubled when it is shorter than six.

    The meter is read by the same reader the compatibility rules use (``compat.parse_meter``), so a
    style that passes the rules always has a bar of at least one beat. A meter that cannot be read,
    or that has no length, raises ``ValueError`` instead of being guessed at.
    """
    parsed = parse_meter(meter)
    if parsed is None:
        raise ValueError(f"Cannot read the meter {shown(meter)!r}")
    numerator, denominator, _ = parsed
    base = {2: numerator * 4, 4: numerator * 2, 8: numerator, 16: max(1, numerator // 2)}[denominator]
    if base < 1:  # unreachable for a parsed meter; a loop below would never end if it happened
        raise ValueError(f"The meter {shown(meter)!r} has no length")
    while base > 10:
        base = (base + 1) // 2
    while base < 6:
        base *= 2
    return base


# ---------------------------------------------------------------------------------------------
# result types

@dataclass(frozen=True)
class ScaffoldLine:
    text: str
    syllables: int
    rhyme: str          # a plan letter such as "A", or "H" for the recurring hook


@dataclass(frozen=True)
class ScaffoldSection:
    tag: str            # for example "[Verse 1]"
    kind: str           # verse, chorus, bridge or outro
    scheme: str         # rhyme plan, for example "ABAB" or "HAHA"
    lines: tuple[ScaffoldLine, ...]

    def text(self) -> str:
        return "\n".join([self.tag, *(line.text for line in self.lines)])


@dataclass(frozen=True)
class LyricScaffold:
    preset_id: str
    seed: int
    tag_style: str
    form: str
    world: str
    syllables_per_line: int
    motif: str
    motif_syllables: int
    sections: tuple[ScaffoldSection, ...]
    source: str = "scaffold"

    @property
    def text(self) -> str:
        """The model-ready lyrics: section tags on their own lines, a blank line between sections."""
        return "\n\n".join(section.text() for section in self.sections)

    def plan(self) -> list[dict]:
        return [
            {"tag": section.tag, "kind": section.kind, "scheme": section.scheme,
             "syllables": [line.syllables for line in section.lines],
             "rhymes": [line.rhyme for line in section.lines]}
            for section in self.sections
        ]

    def to_dict(self) -> dict:
        return {
            "kind": "lyrics-scaffold", "source": self.source, "version": SCAFFOLD_VERSION,
            "preset_id": self.preset_id, "seed": self.seed, "target_model": MUSIC_MODEL, "speech": False,
            "tag_style": self.tag_style, "form": self.form, "world": self.world,
            "syllables_per_line": self.syllables_per_line,
            "motif": {"text": self.motif, "syllables": self.motif_syllables,
                      "returns_in": [s.tag for s in self.sections if any(line.rhyme == "H" for line in s.lines)]},
            "plan": self.plan(), "lyrics": self.text,
        }


# ---------------------------------------------------------------------------------------------
# line construction

class _Stuck(Exception):
    """No wording fits the requested syllable count (the tests prove this never happens for the shipped bank)."""


def _article(words: list[str]) -> list[str]:
    """Turn "a" into "an" before a vowel letter."""
    for index in range(len(words) - 1):
        if words[index] == "a" and words[index + 1][0] in _VOWELS:
            words[index] = "an"
    return words


def _solutions(frame: tuple[str, ...], target: int, end: EndWord):
    """Every way to give each flexible slot a syllable count so the line totals ``target``."""
    fixed = end.syllables
    slots = []
    for item in frame:
        if item == "E":
            continue
        if item in _LITERALS:
            fixed += _LITERALS[item]
        else:
            slots.append(item)
    remaining = target - fixed
    if remaining < 0:
        return
    for counts in product(*(_VARIABLE[slot] for slot in slots)):
        if sum(counts) == remaining:
            yield dict(zip(slots, counts))  # a repeated slot name (the determiner) always counts 1


def _fill(rng: random.Random, world: World, agreement: str, frame: tuple[str, ...],
          counts: dict[str, int], end: EndWord, avoid: set[str]) -> list[str]:
    """Pick words for one chosen syllable assignment."""
    by_slot = counts
    used: set[str] = set()

    def pick(pool: tuple[str, ...]) -> str:
        fresh = [word for word in pool if word not in avoid and word not in used]
        word = rng.choice(fresh or [word for word in pool if word not in used] or list(pool))
        used.add(word)
        return word

    words: list[str] = []
    for item in frame:
        if item == "E":
            words.append(end.word)
        elif item in _LITERALS:
            words.extend(item.split())
        elif item == "P":
            words.append(rng.choice(PRONOUNS))
        elif item == "D":
            words.append(rng.choice(("the", "the", "a")))
        elif item in ("SA?", "OA?"):
            if by_slot[item]:
                words.append(pick(world.adjectives[by_slot[item]]))
        elif item in ("SN", "ON"):
            words.append(pick(world.nouns[by_slot[item]]))
        elif item in ("VT", "VI"):
            base, third = rng.choice((VERBS_T if item == "VT" else VERBS_I)[by_slot[item]])
            words.append(third if agreement == "np" else base)
        elif item == "PT":
            words.extend(rng.choice(PREPOSITIONS[by_slot["PT"]]).split())
        else:  # pragma: no cover - a frame with an unknown slot is a bug in this module
            raise _Stuck(item)
    return _article(words)


def _line(rng: random.Random, world: World, group: str, target: int, avoid: set[str],
          taken_ends: set[str]) -> tuple[str, EndWord]:
    """One lyric line of exactly ``target`` syllables ending on a word from the rhyme ``group``."""
    ends = [e for e in RHYME_GROUPS[group] if e.word not in taken_ends]
    rng.shuffle(ends)
    for end in ends:
        frames = list(_FRAMES[end.pos])
        rng.shuffle(frames)
        for agreement, frame in frames:
            solutions = list(_solutions(frame, target, end))
            if not solutions:
                continue
            words = _fill(rng, world, agreement, frame, rng.choice(solutions), end, avoid)
            text = " ".join(words)
            return text[0].upper() + text[1:], end
    raise _Stuck(f"{group}:{target}")


def _pick_world(rng: random.Random, preset: Preset) -> World:
    palette = preset.palette
    text = " ".join([palette.family, *palette.tags, *palette.moods]).casefold()
    weights = [1 + sum(1 for keyword in world.keywords if keyword in text) for world in WORLDS]
    return rng.choices(WORLDS, weights=weights, k=1)[0]


def lyrics_scaffold(
    preset: Preset | str, seed: int, *, tag_style: str = "title", form: str | None = None,
) -> LyricScaffold:
    """A section-tagged lyric outline for ``preset``, reproducible from ``seed``.

    ``preset`` is a Preset (built-in or a user's own) or a built-in preset id. ``tag_style`` is
    ``"title"`` for ``[Verse 1]`` / ``[Chorus]`` (the style the reference station's lyric files use)
    or ``"lower"`` for ``[verse]`` / ``[chorus]``. ``form`` is ``"short"`` or ``"full"``; by default
    an arrangement with five or more layered sections gets the full form with a bridge.
    """
    _seed(seed)
    if isinstance(preset, str):
        preset = shared_library().get(preset)
    if not isinstance(preset, Preset):
        raise ValueError("lyrics_scaffold needs a Preset or a built-in preset id")
    if tag_style not in TAG_STYLES:
        raise ValueError(f"tag_style must be one of {TAG_STYLES}")
    if form is not None and form not in FORMS:
        raise ValueError(f"form must be one of {FORMS}")
    chosen_form = form or ("full" if len(preset.arrangement.sections.at("layered")) >= 5 else "short")

    rng = random.Random(f"lyrics-{SCAFFOLD_VERSION}:{preset.id}#{preset.digest}:{seed}")
    world = _pick_world(rng, preset)
    hook, hook_syllables, hook_group = rng.choice(world.motifs)
    hook_end = hook.rsplit(" ", 1)[-1]
    per_line = line_syllables_for_meter(preset.palette.meter)

    other_groups = [name for name in RHYME_GROUPS if name != hook_group]
    rng.shuffle(other_groups)
    cursor = 0
    avoid: set[str] = set()
    used_ends = {hook_end}  # no end word is used twice in a song, except the hook's own

    def fresh_groups(count: int) -> list[str]:
        nonlocal cursor
        picked = []
        for _ in range(count):
            picked.append(other_groups[cursor % len(other_groups)])
            cursor += 1
        return picked

    def new_line(group: str, letter: str) -> ScaffoldLine:
        text, end = _line(rng, world, group, per_line, avoid, used_ends)
        used_ends.add(end.word)
        avoid.update(text.casefold().split())
        return ScaffoldLine(text, per_line, letter)

    hook_line = ScaffoldLine(hook[0].upper() + hook[1:], hook_syllables, "H")
    chorus: tuple[ScaffoldLine, ...] | None = None  # the chorus is written once and sung each time
    sections: list[ScaffoldSection] = []
    verse_number = 0
    for kind in _FORM_SECTIONS[chosen_form]:
        if kind == "verse":
            verse_number += 1
            scheme = rng.choice(_VERSE_SCHEMES)
            letters = sorted(set(scheme))
            groups = dict(zip(letters, fresh_groups(len(letters))))
            lines = tuple(new_line(groups[letter], letter) for letter in scheme)
        elif kind == "chorus":
            scheme = "HAHA"
            if chorus is None:
                chorus = (hook_line, new_line(hook_group, "A"), hook_line, new_line(hook_group, "A"))
            lines = chorus
        elif kind == "bridge":
            scheme = "ABAH"
            groups = dict(zip("AB", fresh_groups(2)))
            lines = tuple(hook_line if letter == "H" else new_line(groups[letter], letter) for letter in scheme)
        else:  # outro
            scheme = "HA"
            lines = (hook_line, new_line(hook_group, "A"))
        if tag_style == "title":
            tag = f"[Verse {verse_number}]" if kind == "verse" else f"[{kind.capitalize()}]"
        else:
            tag = f"[{kind}]"
        sections.append(ScaffoldSection(tag, kind, scheme, lines))

    scaffold = LyricScaffold(
        preset_id=preset.id, seed=seed, tag_style=tag_style, form=chosen_form, world=world.id,
        syllables_per_line=per_line, motif=hook, motif_syllables=hook_syllables, sections=tuple(sections),
    )
    problems = validate_scaffold_text(scaffold.text)
    if problems:  # pragma: no cover - the word bank is checked by tests; this is a last guard
        raise ValueError("The lyric outline failed its own checks: " + "; ".join(problems))
    return scaffold


def validate_scaffold_text(text: str) -> list[str]:
    """Why ``text`` is not a clean scaffold (empty list means it is)."""
    problems = []
    if not text.strip():
        problems.append("empty")
    if len(text) > MAX_SCAFFOLD_CHARS:
        problems.append("too long")
    if re.search(r"[<>|]", text):
        problems.append("contains speech-style markup")
    for line in text.splitlines():
        if line.startswith("[") or line.endswith("]"):
            if not _TAG.fullmatch(line):
                problems.append(f"unknown section tag {line!r}")
        elif "[" in line or "]" in line:
            problems.append("bracket inside a lyric line")
    heard = contains_dj_speech(text)
    if heard:
        problems.append("host or station wording: " + ", ".join(heard))
    return problems
