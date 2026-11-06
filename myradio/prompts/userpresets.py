"""A person's own styles: favourites, edited copies and fully written presets.

A user preset lives under the ``user:`` id namespace (``user:evening-porch``) and is checked with
exactly the rules the shipped catalog follows: the same JSON schema (``Palette.parse`` and
``Arrangement.parse``) and the same compatibility rules (``compat.py``), and it must also be
possible to write the style out for every seed (it fits the music maker's length limit even when
a seed picks its longest mood, lead, support and motif). It is stored as a full copy, never as a
reference, so a later catalog release can never change what a saved preset composes. Its
randomness is keyed by a digest of its content (and its id), never by the catalog version, so the
same preset and seed always give the same choices.

Spec accepted by ``build_user_preset`` (all text is checked, nothing is trusted):

    {
      "id": "evening-porch",                   # required: lowercase letters, digits and hyphens
      "title": "Evening porch",                # optional name shown in lists
      "based_on": "porch-blues-slide/slow-tide",   # optional: copy a built-in or one of your own
      "palette": {...},                        # the style; with based_on, only what you change
      "arrangement": "slow-tide" | {...}       # a built-in arrangement id, or the design itself
    }
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable

from .compat import ERROR, Finding, check_arrangement, check_palette, host_message, speaks_like_a_host
from .library import (
    MAX_STYLE_CHARS, Arrangement, CatalogError, FieldsError, Library, Palette, Preset, longest_style_chars,
)
from .textutil import BROKEN_TEXT_MESSAGE, broken_text, is_tidy_line, shown, shown_names

USER_PREFIX = "user:"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
MAX_DOC_CHARS = 24000
SPEC_KEYS = {"id", "title", "based_on", "palette", "arrangement"}

_LABELS = {
    "id": "style id", "name": "style name", "family": "style family", "tags": "tags", "energy": "energy",
    "bpm": "tempo range", "meter": "meter", "moods": "moods", "leads": "lead instruments",
    "supports": "supporting instruments", "bass": "bass part", "harmony": "harmony", "rhythm": "rhythm",
    "motifs": "motifs", "production": "production notes", "sections": "sections",
    "development": "development", "dynamics": "dynamics", "ending": "ending",
}
_PLURAL_FIELDS = frozenset({"tags", "moods", "leads", "supports", "motifs", "sections"})
_LEVEL_FIELDS = frozenset({"harmony", "rhythm", "development", "sections"})  # one entry per complexity level


class UserPresetError(ValueError):
    """The preset cannot be saved. The message is written for the person who wrote it."""

    def __init__(self, message: str, findings: tuple[Finding, ...] = ()):
        super().__init__(message)
        self.findings = findings


def _shallow(value: object, depth: int = 6, budget: int = 4000) -> bool:
    """True when a spec nests at most ``depth`` levels and holds at most ``budget`` values (checked without recursion)."""
    stack = [(value, 0)]
    seen = 0
    while stack:
        item, level = stack.pop()
        seen += 1
        if seen > budget or level > depth:
            return False
        if isinstance(item, dict):
            stack.extend((child, level + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((child, level + 1) for child in item)
    return True


def palette_to_doc(palette: Palette) -> dict:
    return {
        "id": palette.id, "name": palette.name, "family": palette.family, "tags": list(palette.tags),
        "energy": palette.energy, "bpm": list(palette.bpm), "meter": palette.meter,
        "moods": list(palette.moods), "leads": list(palette.leads), "supports": list(palette.supports),
        "bass": palette.bass,
        "harmony": {level: palette.harmony.at(level) for level in ("simple", "layered", "intricate")},
        "rhythm": {level: palette.rhythm.at(level) for level in ("simple", "layered", "intricate")},
        "motifs": list(palette.motifs), "production": palette.production,
    }


def arrangement_to_doc(arrangement: Arrangement) -> dict:
    return {
        "id": arrangement.id, "name": arrangement.name,
        "sections": {level: list(arrangement.sections.at(level)) for level in ("simple", "layered", "intricate")},
        "development": {level: arrangement.development.at(level) for level in ("simple", "layered", "intricate")},
        "dynamics": arrangement.dynamics, "ending": arrangement.ending,
    }


def _oxford(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _field_of(path: str) -> str:
    """The top-level part of a style a schema path points into: ``palette.harmony.simple`` gives ``harmony``."""
    inner = path.split(".", 1)[1] if "." in path else ""
    return re.sub(r"\[\d+\]", "", inner).split(".")[0]


def _fields_message(error: FieldsError, kind: str, field: str, label: str) -> str:
    """Parts that are missing or not recognised, said in words and bounded however many were sent."""
    parts = []
    if field in _LEVEL_FIELDS:
        if error.missing:
            parts.append(f"The {label} needs a simple, a layered and an intricate version, and is missing: {', '.join(error.missing)}.")
        if error.extra:
            parts.append(f"The {label} can only have a simple, a layered and an intricate version. These are not recognised: {shown_names(error.extra)}.")
    else:
        if error.missing:
            parts.append(f"The {kind} is missing: {_oxford([_LABELS.get(name, name) for name in error.missing])}.")
        if error.extra:
            parts.append(f"These parts of the {kind} are not recognised: {shown_names(error.extra)}.")
    return " ".join(parts)


def _friendly(error: CatalogError, kind: str) -> str:
    """Rewrite a schema error so it says which part of the style is wrong and how to fix it.

    Nothing the person typed is repeated at length: unknown names are cut short and capped in number.
    """
    text = str(error)
    path = error.path if isinstance(error, FieldsError) else text.partition(": ")[0]
    detail = text.partition(": ")[2]
    field = _field_of(path)
    label = _LABELS.get(field, kind)
    if isinstance(error, FieldsError):
        return _fields_message(error, kind, field, label)

    def need_list(match: re.Match) -> str:
        count = int(match.group(1))
        return f"The {label} must be a list with at least {count} {'entry' if count == 1 else 'entries'}."

    def need_object(match: re.Match) -> str:
        if field in _LEVEL_FIELDS:
            return f"The {label} needs a simple, a layered and an intricate version."
        return f"The {kind} must be written as an object with named parts."

    rules = (
        (r"expected nonempty text of at most (\d+) characters", lambda m: f"The {label} needs some text of at most {m.group(1)} characters."),
        (r"expected a lowercase hyphenated identifier", lambda m: f"The {label} must be lowercase words joined by hyphens, like late-night."),
        (r"expected \[minimum, maximum\] within 30\.\.240", lambda m: "The tempo range must be two whole numbers between 30 and 240, lowest first."),
        (r"expected one of", lambda m: f"The {label} must be one of calm, steady or driving."),
        (r"duplicate strings", lambda m: f"The {label} list has the same entry twice. Each entry must be different."),
        (r"expected an array of at least (\d+)", need_list),
        (r"text must be trimmed", lambda m: f"The {label} has spaces at the start or end, or an unusual character. Remove them."),
        (r"text must be on one line", lambda m: f"The {label} has a line break, extra spaces or an unusual character inside it. Write it on one line with single spaces."),
        (r"cannot be stored", lambda m: f"The {label} contains a broken character, for example half of an emoji. Retype that part."),
        (r"expected an object", need_object),
        (r"each arrangement needs three to six sections", lambda m: "Each arrangement level needs three to six sections."),
    )
    for pattern, make in rules:
        match = re.search(pattern, detail or text)
        if match:
            return make(match)
    return f"The {label} is not usable: {shown(detail or text, 60)}"


_PLACES = {
    "bpm": "the tempo range", "energy": "the energy", "meter": "the meter", "tags": "the tags", "name": "the name",
    "instruments": "the instruments", "bass": "the bass part", "production": "the production notes",
    "dynamics": "the dynamics", "ending": "the ending",
}
_ITEMS = {"moods": "mood", "leads": "lead instrument", "supports": "supporting instrument", "motifs": "motif"}


def _place(field: str) -> str:
    """Where in a style a finding points, in words (``harmony.intricate`` gives "the intricate harmony")."""
    item = re.fullmatch(r"(moods|leads|supports|motifs)\[(\d+)\]", field)
    if item:
        return f"{_ITEMS[item.group(1)]} {int(item.group(2)) + 1}"
    level = re.fullmatch(r"(harmony|rhythm|development)\.(simple|layered|intricate)", field)
    if level:
        return f"the {level.group(2)} {level.group(1)}"
    section = re.fullmatch(r"sections\.(simple|layered|intricate)\[(\d+)\]", field)
    if section:  # sections are numbered from 1 already
        return f"section {section.group(2)} of the {section.group(1)} version"
    return _PLACES.get(field, "the style")


def _problem_list(problems: list[Finding]) -> str:
    """The first few problems, each naming where it is; identical sentences are said once for all their places."""
    groups: dict[str, list[str]] = {}
    for item in problems:
        places = groups.setdefault(item.message, [])
        where = _place(item.field)
        if where not in places:
            places.append(where)
    lines = []
    for number, (message, places) in enumerate(list(groups.items())[:4], 1):
        if len(places) > 3:
            where = ", ".join(places[:3]) + f" and {len(places) - 3} more places"
        else:
            where = _oxford(places)
        lines.append(f"{number}. In {where}: {message}")
    more = f" ({len(groups) - 4} more)" if len(groups) > 4 else ""
    return " ".join(lines) + more


def too_long_message(chars: int) -> str:
    """Plain wording for a style whose longest written-out form does not fit the music maker."""
    over = chars - MAX_STYLE_CHARS
    return (
        f"This style is too long for the music maker. Written out in full, its longest version is {chars:,} "
        f"characters and the limit is {MAX_STYLE_CHARS:,}. Shorten the harmony, rhythm, production, mood or "
        f"arrangement notes by about {over:,} character{'s' if over != 1 else ''}, then save it again."
    )


def _title(value: object) -> str | None:
    """The optional title, checked like the other text of a style."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 100 or value != value.strip():
        raise UserPresetError("The title must be some text of at most 100 characters, without spaces at the start or end.")
    if not is_tidy_line(value):
        raise UserPresetError("The title must be on one line, with single spaces and no unusual characters.")
    if speaks_like_a_host(value):
        raise UserPresetError(f"The title sounds like a radio host or announcer (found {host_message(value)}). "
                              "Choose a name that describes the music.")
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class UserPresetRecord:
    slug: str
    title: str
    based_on: str | None
    palette: dict
    arrangement: dict
    digest: str

    @property
    def id(self) -> str:
        return USER_PREFIX + self.slug

    def preset(self) -> Preset:
        return Preset(
            Palette.parse(self.palette, "palette"), Arrangement.parse(self.arrangement, "arrangement"),
            user_id=self.id, digest=self.digest, user_title=self.title,
        )


def digest_of(palette: dict, arrangement: dict) -> str:
    return hashlib.sha256(_canonical({"palette": palette, "arrangement": arrangement}).encode("utf-8")).hexdigest()[:16]


def build_user_preset(
    spec: object, *, library: Library, resolve: Callable[[str], Preset | None],
) -> tuple[UserPresetRecord, list[Finding]]:
    """Validate ``spec`` and return the record to store plus any warnings. Raises UserPresetError."""
    if not isinstance(spec, dict):
        raise UserPresetError("Send the style as an object with an id, a palette and an arrangement.")
    if not _shallow(spec):
        raise UserPresetError("This style is nested too deeply or has too many parts. A style is a flat description with short lists.")
    if broken_text(spec):
        raise UserPresetError(BROKEN_TEXT_MESSAGE)
    unknown = sorted(set(spec) - SPEC_KEYS)
    if unknown:
        raise UserPresetError(f"These settings are not recognised: {shown_names(unknown)}. Use id, title, based_on, palette and arrangement.")
    slug = spec.get("id")
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise UserPresetError("Give the style an id of lowercase letters, numbers and hyphens (up to 63 characters), such as evening-porch.")
    title = _title(spec.get("title"))

    base: Preset | None = None
    based_on = spec.get("based_on")
    if based_on is not None:
        if not isinstance(based_on, str):
            raise UserPresetError("based_on must be the id of a style to copy.")
        base = resolve(based_on)
        if base is None:
            raise UserPresetError(f"The style you copied from (\"{shown(based_on)}\") is not in the library. Check its id in the catalog.")

    palette_part = spec.get("palette")
    arrangement_part = spec.get("arrangement")
    if palette_part is not None and not isinstance(palette_part, dict):
        raise UserPresetError("The palette must be an object describing the style.")

    # ---- palette document
    if base is not None:
        palette_doc = palette_to_doc(base.palette)
        changed = False
        if palette_part:
            extra = sorted(set(palette_part) - set(palette_doc))
            if extra:
                raise UserPresetError(f"These style parts are not recognised: {shown_names(extra)}.")
            for key, value in palette_part.items():
                if key != "id":
                    changed = changed or palette_doc.get(key) != value
                    palette_doc[key] = copy.deepcopy(value)
        if changed:
            palette_doc["id"] = slug
    else:
        if not isinstance(palette_part, dict) or not palette_part:
            raise UserPresetError("A new style needs a palette: name, family, tags, energy, tempo range, meter, moods, instruments, harmony, rhythm, motifs and production notes.")
        palette_doc = copy.deepcopy(palette_part)
        palette_doc["id"] = slug

    # ---- arrangement document
    if isinstance(arrangement_part, str):
        try:
            source = library.get_arrangement(arrangement_part)
        except ValueError:
            raise UserPresetError(f"The arrangement \"{shown(arrangement_part)}\" is not in the library. Check its id in the catalog.") from None
        arrangement_doc = arrangement_to_doc(source)
    elif isinstance(arrangement_part, dict):
        if base is not None:
            arrangement_doc = arrangement_to_doc(base.arrangement)
            changed = False
            for key, value in arrangement_part.items():
                if key == "id":
                    continue
                if key not in arrangement_doc:
                    raise UserPresetError(f"The arrangement part \"{shown(key, 30)}\" is not recognised.")
                changed = changed or arrangement_doc[key] != value
                arrangement_doc[key] = copy.deepcopy(value)
            if changed:
                arrangement_doc["id"] = slug
        else:
            arrangement_doc = copy.deepcopy(arrangement_part)
            arrangement_doc["id"] = slug
    elif arrangement_part is None and base is not None:
        arrangement_doc = arrangement_to_doc(base.arrangement)
    else:
        raise UserPresetError("Choose an arrangement: the id of a built-in one, or write your own.")

    if len(_canonical(palette_doc)) + len(_canonical(arrangement_doc)) > MAX_DOC_CHARS:
        raise UserPresetError(f"This style is too long. Keep the whole preset under {MAX_DOC_CHARS:,} characters.")

    # ---- the same schema the catalog uses
    try:
        palette = Palette.parse(palette_doc, "palette")
    except CatalogError as exc:
        raise UserPresetError(_friendly(exc, "style")) from None
    try:
        arrangement = Arrangement.parse(arrangement_doc, "arrangement")
    except CatalogError as exc:
        raise UserPresetError(_friendly(exc, "arrangement")) from None

    # ---- the same compatibility rules the catalog follows
    findings = check_palette(palette) + check_arrangement(arrangement)
    problems = [item for item in findings if item.severity == ERROR]
    if problems:
        raise UserPresetError("This style cannot be saved yet. " + _problem_list(problems), tuple(problems))
    warnings = [item for item in findings if item.severity != ERROR]

    # ---- it must also be possible to write the style out, whatever the seed picks
    longest = longest_style_chars(Preset(palette, arrangement, user_id=USER_PREFIX + slug))
    if longest > MAX_STYLE_CHARS:
        raise UserPresetError(too_long_message(longest))

    shown_title = title if title is not None else Preset(palette, arrangement).title
    palette_doc, arrangement_doc = palette_to_doc(palette), arrangement_to_doc(arrangement)
    record = UserPresetRecord(
        slug=slug, title=shown_title, based_on=based_on, palette=palette_doc,
        arrangement=arrangement_doc, digest=digest_of(palette_doc, arrangement_doc),
    )
    return record, warnings
