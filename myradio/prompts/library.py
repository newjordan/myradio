"""A versioned, offline, music-only prompt catalog shared by agents and the UI.

This compiles musical directions, not audio. It neither calls a model nor makes
claims about a model's adherence to those directions.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import asdict, dataclass, field
from functools import cached_property
from importlib.resources import files
from typing import Iterable, Mapping, Sequence

from .textutil import encodable, has_odd_character, shown

COMPLEXITIES = ("simple", "layered", "intricate")
DENSITIES = ("sparse", "balanced", "lush")
ENERGIES = ("calm", "steady", "driving")
MODES = ("instrumental", "vocal")
MUSIC_MODEL = "Frosty40/YuE2-Turbo-Radio"
MAX_SEED = 2**63 - 1
MAX_STYLE_CHARS = 6000
MAX_LYRICS_CHARS = 12000
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")

_DENSITY = {
    "sparse": (
        "Sparse texture: one foreground line over a quiet foundation. Exchange "
        "roles rather than piling up parts; use space, voice leading, and "
        "phrasing for complexity, not constant activity."
    ),
    "balanced": (
        "Balanced texture: a clear foreground melody, a distinct answering "
        "role, and an uncluttered rhythmic foundation. Leave audible rests "
        "between phrases."
    ),
    "lush": (
        "Lush texture within the named ensemble: broaden registers and support "
        "voices without adding unrelated instruments. Keep melody, bass, and "
        "inner movement distinguishable; fuller need not mean louder."
    ),
}


class CatalogError(ValueError):
    """A shipped or supplied catalog violates the versioned data contract."""


class NoMatchingPreset(ValueError):
    """No candidate remains after applying the user's filters and history."""


class FieldsError(CatalogError):
    """An object has parts missing or parts it should not have.

    The names are kept on the exception so a caller can word its own message; the text of this
    exception quotes at most five unknown names, each cut short, however many were sent.
    """

    def __init__(self, path: str, missing: Iterable[object], extra: Iterable[object]):
        self.path = path
        self.missing = tuple(sorted(str(name) for name in missing))
        self.extra = tuple(sorted(str(name) for name in extra))
        quoted = ", ".join(repr(shown(name, 30)) for name in self.extra[:5])
        more = f" and {len(self.extra) - 5} more" if len(self.extra) > 5 else ""
        super().__init__(f"{path}: missing fields {list(self.missing)}; unknown fields [{quoted}]{more}")

    def __reduce__(self):  # keep the names when the error is copied or sent between processes
        return (FieldsError, (self.path, self.missing, self.extra))


def _text(value: object, path: str, *, limit: int = 1600) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CatalogError(f"{path}: expected nonempty text of at most {limit} characters")
    if value != value.strip() or "\x00" in value:
        raise CatalogError(f"{path}: text must be trimmed and contain no NUL character")
    if not encodable(value):
        raise CatalogError(f"{path}: text contains a character that cannot be stored")
    # The shipped catalog keeps every field to one line with single spaces; a saved style follows the
    # same habit so a field can never add a line (or a hidden control character) to a compiled prompt.
    if value != " ".join(value.split()) or has_odd_character(value):
        raise CatalogError(f"{path}: text must be on one line, with single spaces and no control characters")
    return value


def _identifier(value: object, path: str) -> str:
    text = _text(value, path, limit=100)
    if not _ID.fullmatch(text):
        raise CatalogError(f"{path}: expected a lowercase hyphenated identifier")
    return text


def _object(value: object, keys: set[str], path: str) -> dict:
    if not isinstance(value, dict):
        raise CatalogError(f"{path}: expected an object")
    missing = keys - value.keys()
    extra = value.keys() - keys
    if missing or extra:
        raise FieldsError(path, missing, extra)
    return value


def _texts(value: object, path: str, *, minimum: int = 1) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) < minimum:
        raise CatalogError(f"{path}: expected an array of at least {minimum} strings")
    result = tuple(_text(item, f"{path}[{i}]") for i, item in enumerate(value))
    if len(set(result)) != len(result):
        raise CatalogError(f"{path}: duplicate strings are not allowed")
    return result


@dataclass(frozen=True)
class LevelText:
    simple: str
    layered: str
    intricate: str

    @classmethod
    def parse(cls, value: object, path: str) -> LevelText:
        obj = _object(value, set(COMPLEXITIES), path)
        return cls(**{level: _text(obj[level], f"{path}.{level}") for level in COMPLEXITIES})

    def at(self, level: str) -> str:
        return getattr(self, level)


@dataclass(frozen=True)
class LevelSections:
    simple: tuple[str, ...]
    layered: tuple[str, ...]
    intricate: tuple[str, ...]

    @classmethod
    def parse(cls, value: object, path: str) -> LevelSections:
        obj = _object(value, set(COMPLEXITIES), path)
        result = cls(**{
            level: _texts(obj[level], f"{path}.{level}", minimum=3)
            for level in COMPLEXITIES
        })
        if any(len(result.at(level)) > 6 for level in COMPLEXITIES):
            raise CatalogError(f"{path}: each arrangement needs three to six sections")
        return result

    def at(self, level: str) -> tuple[str, ...]:
        return getattr(self, level)


@dataclass(frozen=True)
class Palette:
    id: str
    name: str
    family: str
    tags: tuple[str, ...]
    energy: str
    bpm: tuple[int, int]
    meter: str
    moods: tuple[str, ...]
    leads: tuple[str, ...]
    supports: tuple[str, ...]
    bass: str
    harmony: LevelText
    rhythm: LevelText
    motifs: tuple[str, ...]
    production: str

    @classmethod
    def parse(cls, value: object, path: str) -> Palette:
        obj = _object(value, set(cls.__dataclass_fields__), path)
        bpm = obj["bpm"]
        if (not isinstance(bpm, list) or len(bpm) != 2
                or any(type(n) is not int for n in bpm)
                or not 30 <= bpm[0] <= bpm[1] <= 240):
            raise CatalogError(f"{path}.bpm: expected [minimum, maximum] within 30..240")
        if obj["energy"] not in ENERGIES:
            raise CatalogError(f"{path}.energy: expected one of {ENERGIES}")
        tags = _texts(obj["tags"], f"{path}.tags")
        for tag in tags:
            _identifier(tag, f"{path}.tags")
        return cls(
            id=_identifier(obj["id"], f"{path}.id"),
            name=_text(obj["name"], f"{path}.name", limit=100),
            family=_identifier(obj["family"], f"{path}.family"),
            tags=tags,
            energy=obj["energy"],
            bpm=tuple(bpm),
            meter=_text(obj["meter"], f"{path}.meter", limit=80),
            moods=_texts(obj["moods"], f"{path}.moods", minimum=2),
            leads=_texts(obj["leads"], f"{path}.leads", minimum=2),
            supports=_texts(obj["supports"], f"{path}.supports", minimum=2),
            bass=_text(obj["bass"], f"{path}.bass"),
            harmony=LevelText.parse(obj["harmony"], f"{path}.harmony"),
            rhythm=LevelText.parse(obj["rhythm"], f"{path}.rhythm"),
            motifs=_texts(obj["motifs"], f"{path}.motifs", minimum=2),
            production=_text(obj["production"], f"{path}.production"),
        )


@dataclass(frozen=True)
class Arrangement:
    id: str
    name: str
    sections: LevelSections
    development: LevelText
    dynamics: str
    ending: str

    @classmethod
    def parse(cls, value: object, path: str) -> Arrangement:
        obj = _object(value, set(cls.__dataclass_fields__), path)
        return cls(
            id=_identifier(obj["id"], f"{path}.id"),
            name=_text(obj["name"], f"{path}.name", limit=100),
            sections=LevelSections.parse(obj["sections"], f"{path}.sections"),
            development=LevelText.parse(obj["development"], f"{path}.development"),
            dynamics=_text(obj["dynamics"], f"{path}.dynamics"),
            ending=_text(obj["ending"], f"{path}.ending"),
        )


@dataclass(frozen=True)
class Preset:
    """One style palette played through one arrangement design.

    Built-in presets are identified ``<palette id>/<arrangement id>``. A user's own preset (see
    ``myradio.prompts.store``) carries ``user_id`` (``user:<slug>``), the digest of its content and
    an optional display title; the compiler treats it exactly like a built-in one.
    """

    palette: Palette
    arrangement: Arrangement
    user_id: str | None = None
    digest: str = ""
    user_title: str | None = None

    @property
    def id(self) -> str:
        return self.user_id or f"{self.palette.id}/{self.arrangement.id}"

    @property
    def title(self) -> str:
        return self.user_title or f"{self.palette.name} — {self.arrangement.name}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "palette_id": self.palette.id,
            "arrangement_id": self.arrangement.id,
            "family": self.palette.family,
            "tags": list(self.palette.tags),
            "energy": self.palette.energy,
            "bpm": list(self.palette.bpm),
            "meter": self.palette.meter,
        }

    @cached_property
    def search_text(self) -> str:
        """Everything a text search looks through, lower-cased once."""
        palette = self.palette
        return " ".join((
            self.id, self.title, palette.family, *palette.tags,
            palette.meter, *palette.moods, *palette.leads, *palette.supports,
            palette.bass, *palette.motifs, palette.production,
            *(palette.harmony.at(level) for level in COMPLEXITIES),
            *(palette.rhythm.at(level) for level in COMPLEXITIES),
            *(self.arrangement.development.at(level) for level in COMPLEXITIES),
        )).casefold()


@dataclass(frozen=True)
class SongPrompt:
    schema_version: int
    catalog_version: str
    preset_id: str
    palette_id: str
    arrangement_id: str
    seed: int
    complexity: str
    density: str
    mode: str
    bpm: int
    meter: str
    title: str
    style: str
    lyrics: str | None
    target_model: str = MUSIC_MODEL
    kind: str = "music"
    speech: bool = False

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {**asdict(self), "fingerprint": self.fingerprint}


def _seed(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_SEED:
        raise ValueError(f"seed must be an integer in 0..{MAX_SEED}")


def _filters(values: Iterable[str], name: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a collection of strings, not one string")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be a collection of strings") from exc
    if any(not isinstance(v, str) or not v.strip() for v in result):
        raise ValueError(f"{name} must contain only nonempty strings")
    return frozenset(v.strip().lower() for v in result)


_VERSION = re.compile(r"\d{1,6}(?:\.\d{1,6}){0,3}\Z")


def _version_key(version: str) -> tuple[int, ...]:
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise CatalogError(f"catalog version {version!r}: expected dotted numbers such as 1.1.0")
    return tuple(int(part) for part in version.split("."))


def filter_presets(
    presets: Iterable[Preset], *, family: str | None = None, tags: Iterable[str] = (),
    energy: str | None = None, exclude_tags: Iterable[str] = (), query: str = "",
) -> tuple[Preset, ...]:
    """The one filter used for built-in and user presets alike.

    Tags use AND, exclusions use ANY, and query words are a case-insensitive AND over the
    preset's identity and musical details.
    """
    if family is not None and (not isinstance(family, str) or not family.strip()):
        raise ValueError("family must be a nonempty string")
    if energy is not None and energy not in ENERGIES:
        raise ValueError(f"energy must be one of {ENERGIES}")
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    wanted = _filters(tags, "tags")
    excluded = _filters(exclude_tags, "exclude_tags")
    terms = query.casefold().split()
    wanted_family = family.strip().lower() if family else None
    result = []
    for preset in presets:
        palette = preset.palette
        if wanted_family and palette.family != wanted_family:
            continue
        if energy and palette.energy != energy:
            continue
        if not wanted.issubset(palette.tags) or excluded.intersection(palette.tags):
            continue
        if terms:
            haystack = preset.search_text
            if not all(term in haystack for term in terms):
                continue
        result.append(preset)
    return tuple(result)


def _check_treatment(seed: int, complexity: str, density: str, mode: str, lyrics: str | None) -> None:
    _seed(seed)
    if complexity not in COMPLEXITIES:
        raise ValueError(f"complexity must be one of {COMPLEXITIES}")
    if density not in DENSITIES:
        raise ValueError(f"density must be one of {DENSITIES}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if mode == "instrumental" and lyrics is not None:
        raise ValueError("Instrumental prompts cannot include lyrics; select vocal mode instead")
    if mode == "vocal":
        if not isinstance(lyrics, str) or not lyrics.strip():
            raise ValueError("Vocal mode requires nonempty, user-supplied song lyrics")
        if len(lyrics) > MAX_LYRICS_CHARS or "\x00" in lyrics:
            raise ValueError(f"Lyrics must contain no NUL and be at most {MAX_LYRICS_CHARS} characters")
        if not encodable(lyrics):
            raise ValueError("Lyrics contain a broken character (for example half of an emoji) that cannot be written as text")


def _write_style(
    palette: Palette, arrangement: Arrangement, bpm: int, mood: str, lead: str, support: str,
    motif: str, complexity: str, density: str, mode: str,
) -> str:
    """The compiled style text for one set of choices (the words are fixed here, the choices are not)."""
    instrumental = mode == "instrumental"
    parts = [
        f"{'Instrumental' if instrumental else 'Sung'} {palette.name}. "
        f"{bpm} BPM; meter {palette.meter}; {palette.energy} energy; {mood}.",
        f"{'Lead' if instrumental else 'Instrumental answers between sung phrases'}: {lead}. "
        f"Supporting role: {support}. Bass: {palette.bass}.",
        f"Harmony: {palette.harmony.at(complexity)}",
        f"Rhythm: {palette.rhythm.at(complexity)}",
        f"Motif: {motif}. Development: {arrangement.development.at(complexity)}",
        "Form: " + " ".join(
            f"{i}. {section}" for i, section in enumerate(arrangement.sections.at(complexity), 1)
        ),
        _DENSITY[density],
        f"Dynamics: {arrangement.dynamics}",
        f"Production: {palette.production}",
        f"Ending: {arrangement.ending}",
        (
            "No vocals, sung lyrics, spoken words, DJ chatter, or station announcements."
            if instrumental else
            "Sing the supplied lyrics intelligibly; preserve their phrasing and leave space "
            "for breath. Keep instrumental lead answers between vocal phrases. No spoken "
            "DJ links, news reading, or station announcements."
        ),
    ]
    return "\n".join(parts)


def longest_style_chars(
    preset: Preset, complexities: Iterable[str] = COMPLEXITIES, densities: Iterable[str] = DENSITIES,
    modes: Iterable[str] = MODES,
) -> int:
    """The most characters the compiled style of ``preset`` can ever have, whatever the seed.

    A seed picks one mood, lead, support and motif and a tempo, so the longest possible text uses the
    longest of each. Writing that out for the given treatments (all of them by default) says exactly
    whether every seed fits under ``MAX_STYLE_CHARS``, without trying seeds.
    """
    palette = preset.palette
    mood = max(palette.moods, key=len)
    lead = max(palette.leads, key=len)
    support = max(palette.supports, key=len)
    motif = max(palette.motifs, key=len)
    bpm = palette.bpm[1]  # the largest tempo has the most digits
    densities, modes = tuple(densities), tuple(modes)
    return max(
        len(_write_style(palette, preset.arrangement, bpm, mood, lead, support, motif, complexity, density, mode))
        for complexity in complexities for density in densities for mode in modes
    )


@dataclass(frozen=True)
class Library:
    """Style palettes x arrangement designs, composed into stable presets.

    ``origins`` records the catalog version in which each palette and arrangement was introduced
    (keys ``palette:<id>`` and ``arrangement:<id>``). A preset's randomness is keyed by the later
    of its two origins, so adding content in a newer catalog never changes what an older preset
    composes. A library built from a single document pair has every origin equal to its version.
    """

    palettes: tuple[Palette, ...]
    arrangements: tuple[Arrangement, ...]
    version: str
    presets: tuple[Preset, ...] = field(init=False)
    origins: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        for name, items in (("palettes", self.palettes), ("arrangements", self.arrangements)):
            if not items:
                raise CatalogError(f"{name}: must not be empty")
            if len({item.id for item in items}) != len(items):
                raise CatalogError(f"{name}: duplicate IDs")
        object.__setattr__(self, "presets", tuple(
            Preset(palette, arrangement)
            for palette in self.palettes
            for arrangement in self.arrangements
        ))

    @staticmethod
    def _parse_pair(palettes: object, arrangements: object) -> tuple[tuple[Palette, ...], tuple[Arrangement, ...], str]:
        documents = []
        for value, name in ((palettes, "palettes"), (arrangements, "arrangements")):
            obj = _object(value, {"schema_version", "catalog_version", name}, name)
            if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
                raise CatalogError(f"{name}.schema_version: only version 1 is supported")
            _text(obj["catalog_version"], f"{name}.catalog_version", limit=50)
            if not isinstance(obj[name], list) or not obj[name]:
                raise CatalogError(f"{name}: expected a nonempty array")
            documents.append(obj)
        pd, ad = documents
        if pd["catalog_version"] != ad["catalog_version"]:
            raise CatalogError("palette and arrangement catalog versions must match")
        return (
            tuple(Palette.parse(p, f"palettes[{i}]") for i, p in enumerate(pd["palettes"])),
            tuple(Arrangement.parse(a, f"arrangements[{i}]") for i, a in enumerate(ad["arrangements"])),
            pd["catalog_version"],
        )

    @classmethod
    def from_documents(cls, palettes: object, arrangements: object) -> Library:
        parsed_palettes, parsed_arrangements, version = cls._parse_pair(palettes, arrangements)
        return cls(palettes=parsed_palettes, arrangements=parsed_arrangements, version=version)

    @classmethod
    def from_packs(cls, packs: Sequence[tuple[object, object]]) -> Library:
        """Combine catalog increments, oldest first. Every palette meets every arrangement.

        Each pack is a (palettes document, arrangements document) pair with its own, matching,
        catalog version. Versions must be dotted numbers and strictly increase. The library's
        version is the newest pack's.
        """
        if not packs:
            raise CatalogError("at least one catalog pack is required")
        palettes: list[Palette] = []
        arrangements: list[Arrangement] = []
        origins: dict[str, str] = {}
        previous: tuple[int, ...] | None = None
        version = ""
        for index, pair in enumerate(packs):
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise CatalogError(f"packs[{index}]: expected a (palettes, arrangements) pair")
            new_palettes, new_arrangements, version = cls._parse_pair(*pair)
            key = _version_key(version)
            if previous is not None and key <= previous:
                raise CatalogError(f"packs[{index}]: catalog versions must increase ({version})")
            previous = key
            palettes.extend(new_palettes)
            arrangements.extend(new_arrangements)
            origins.update({f"palette:{p.id}": version for p in new_palettes})
            origins.update({f"arrangement:{a.id}": version for a in new_arrangements})
        return cls(tuple(palettes), tuple(arrangements), version, origins=origins)

    def versions(self) -> tuple[str, ...]:
        """Every catalog version this library contains, oldest first."""
        found = {self.version, *self.origins.values()}
        try:
            return tuple(sorted(found, key=_version_key))
        except CatalogError:
            return (self.version,)

    def through(self, version: str) -> Library:
        """The catalog as it stood at ``version``: only content introduced by then, same order."""
        if version == self.version:
            return self
        if version not in self.versions():
            raise CatalogError(f"Unknown catalog version {version!r}. Known versions: {', '.join(self.versions())}.")
        limit = _version_key(version)

        def kept(kind: str, item_id: str) -> bool:
            return _version_key(self.origins[f"{kind}:{item_id}"]) <= limit

        return Library(
            palettes=tuple(p for p in self.palettes if kept("palette", p.id)),
            arrangements=tuple(a for a in self.arrangements if kept("arrangement", a.id)),
            version=version,
            origins={k: v for k, v in self.origins.items() if _version_key(v) <= limit},
        )

    def epoch_for(self, preset: Preset) -> str:
        """The catalog version that keys this preset's randomness."""
        if preset.user_id:
            return self.version
        palette = self.origins.get(f"palette:{preset.palette.id}", self.version)
        arrangement = self.origins.get(f"arrangement:{preset.arrangement.id}", self.version)
        if palette == arrangement:
            return palette
        try:
            return max(palette, arrangement, key=_version_key)
        except CatalogError:
            return self.version

    @cached_property
    def _index(self) -> dict[str, Preset]:
        return {preset.id: preset for preset in self.presets}

    def get(self, preset_id: str) -> Preset:
        found = self._index.get(preset_id) if isinstance(preset_id, str) else None
        if found is not None:
            return found
        raise NoMatchingPreset(f"Unknown preset: {preset_id!r}. Browse the catalog for valid IDs.")

    def get_palette(self, palette_id: str) -> Palette:
        for palette in self.palettes:
            if palette.id == palette_id:
                return palette
        raise NoMatchingPreset(f"Unknown style: {palette_id!r}. Browse the catalog for valid styles.")

    def get_arrangement(self, arrangement_id: str) -> Arrangement:
        for arrangement in self.arrangements:
            if arrangement.id == arrangement_id:
                return arrangement
        raise NoMatchingPreset(f"Unknown arrangement: {arrangement_id!r}. Browse the catalog for valid arrangements.")

    def browse(
        self, *, family: str | None = None, tags: Iterable[str] = (),
        energy: str | None = None, exclude_tags: Iterable[str] = (), query: str = "",
    ) -> tuple[Preset, ...]:
        return filter_presets(
            self.presets, family=family, tags=tags, energy=energy, exclude_tags=exclude_tags, query=query,
        )

    def choose(
        self, seed: int, *, family: str | None = None, tags: Iterable[str] = (),
        energy: str | None = None, exclude_tags: Iterable[str] = (), query: str = "",
        recent_ids: Iterable[str] = (), recent_palettes: Iterable[str] = (),
    ) -> Preset:
        _seed(seed)
        ids = _filters(recent_ids, "recent_ids")
        palettes = _filters(recent_palettes, "recent_palettes")
        candidates = tuple(p for p in self.browse(
            family=family, tags=tags, energy=energy, exclude_tags=exclude_tags, query=query,
        ) if p.id not in ids and p.palette.id not in palettes)
        if not candidates:
            raise NoMatchingPreset(
                "No presets remain after filters and recent history. "
                "Change the filters or deliberately shorten the history; exclusions were not relaxed."
            )
        return random.Random(seed).choice(candidates)

    def generate(
        self, preset_id: str, seed: int, *, complexity: str = "layered",
        density: str = "balanced", mode: str = "instrumental", lyrics: str | None = None,
    ) -> SongPrompt:
        _check_treatment(seed, complexity, density, mode, lyrics)
        return self._compile(self.get(preset_id), seed, complexity, density, mode, lyrics)

    def compose(
        self, preset: Preset, seed: int, *, complexity: str = "layered",
        density: str = "balanced", mode: str = "instrumental", lyrics: str | None = None,
    ) -> SongPrompt:
        """Compile any Preset object, including a user's own (``generate`` takes a built-in id)."""
        if not isinstance(preset, Preset):
            raise ValueError("compose needs a Preset object")
        _check_treatment(seed, complexity, density, mode, lyrics)
        return self._compile(preset, seed, complexity, density, mode, lyrics)

    def _compile(
        self, preset: Preset, seed: int, complexity: str, density: str, mode: str, lyrics: str | None,
    ) -> SongPrompt:
        palette, arrangement = preset.palette, preset.arrangement
        if preset.user_id:
            # A saved style is a full copy of its own content, so its choices depend on that content
            # (the digest), its id and the seed, and on nothing else. In particular not on the catalog
            # version: a later catalog release must not change what a saved style composes.
            rng = random.Random(f"user-style:{preset.id}#{preset.digest}:{seed}")
        else:
            rng = random.Random(f"{self.epoch_for(preset)}:{preset.id}:{seed}")
        bpm = rng.randint(*palette.bpm)
        mood = rng.choice(palette.moods)
        lead = rng.choice(palette.leads)
        support = rng.choice(palette.supports)
        motif = rng.choice(palette.motifs)
        style = _write_style(palette, arrangement, bpm, mood, lead, support, motif, complexity, density, mode)
        if len(style) > MAX_STYLE_CHARS:
            raise CatalogError(f"{preset.id}: compiled style exceeds {MAX_STYLE_CHARS} characters")
        return SongPrompt(
            schema_version=1, catalog_version=self.version, preset_id=preset.id,
            palette_id=palette.id, arrangement_id=arrangement.id, seed=seed,
            complexity=complexity, density=density, mode=mode, bpm=bpm,
            meter=palette.meter, title=preset.title, style=style, lyrics=lyrics,
        )


# Catalog increments shipped with the package, oldest first. Files live directly in data/ so the
# package-data pattern "data/*.json" in pyproject.toml covers them.
PACKS = (
    ("palettes.json", "arrangements.json"),
    ("palettes-1.1.0.json", "arrangements-1.1.0.json"),
)


def load_library(through: str | None = None) -> Library:
    """Load the packaged catalog without a network request or dependence on the cwd.

    ``through="1.0.0"`` returns the original 32 x 12 library exactly as first released, which is
    how an old provenance record is replayed. The default is the newest catalog.
    """
    if through is not None and not isinstance(through, str):
        raise ValueError("through must be a catalog version string such as '1.0.0'")
    root = files("myradio.prompts").joinpath("data")
    try:
        packs = [
            (json.loads(root.joinpath(p).read_text(encoding="utf-8")),
             json.loads(root.joinpath(a).read_text(encoding="utf-8")))
            for p, a in PACKS
        ]
    except (OSError, UnicodeError, ValueError) as exc:
        raise CatalogError(f"Cannot read the packaged prompt catalog: {exc}") from exc
    library = Library.from_packs(packs)
    return library if through is None else library.through(through)


_SHARED: dict[str | None, Library] = {}


def shared_library(through: str | None = None) -> Library:
    """A process-wide cached copy of ``load_library`` (the catalog is immutable)."""
    found = _SHARED.get(through)
    if found is None:
        found = _SHARED[through] = load_library(through)
    return found


def replay(record: Mapping[str, object], library: Library | None = None) -> SongPrompt:
    """Re-compose a stored built-in record and prove it is identical.

    The record is what ``SongPrompt.to_dict()`` emitted. The catalog version named in the record
    selects the library to replay against, the prompt is composed again from its inputs, and the
    fingerprint must match. A mismatch means the catalog or compiler changed (or the record was
    edited): it raises ``ValueError`` instead of returning a different prompt.
    """
    if not isinstance(record, Mapping):
        raise ValueError("A prompt record must be an object")
    needed = ("catalog_version", "preset_id", "seed", "complexity", "density", "mode", "fingerprint")
    missing = [key for key in needed if key not in record]
    if missing:
        raise ValueError(f"The record is missing {', '.join(missing)}")
    version = record["catalog_version"]
    if not isinstance(version, str):
        raise ValueError("The record's catalog_version must be text such as '1.0.0'")
    if library is None:
        library = shared_library(version)
    elif library.version != version:
        library = library.through(version)
    prompt = library.generate(
        record["preset_id"], record["seed"], complexity=record["complexity"],
        density=record["density"], mode=record["mode"], lyrics=record.get("lyrics"),
    )
    if prompt.fingerprint != record["fingerprint"]:
        raise ValueError("This record no longer reproduces: the catalog changed or the record was edited")
    return prompt
