"""One object that ties the music-prompt library to a person's saved styles and the repeat memory.

``PromptService`` is what the station extension calls and what other parts of myradio may import
(the generation worker, the station recipes). It never starts a model, makes audio or touches the
network: it only chooses a style and writes a prompt, and keeps a bounded record of what it chose.

Every error it raises on purpose is a ``PromptError`` whose message is written for a person.
"""

from __future__ import annotations

import contextlib
import random
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Callable, Iterable

from .library import (
    COMPLEXITIES, DENSITIES, ENERGIES, MAX_LYRICS_CHARS, MAX_SEED, MAX_STYLE_CHARS, MODES, Arrangement,
    CatalogError, Library, NoMatchingPreset, Palette, Preset, SongPrompt, _filters, filter_presets,
    longest_style_chars, replay, shared_library,
)
from .lyrics import FORMS, TAG_STYLES, LyricScaffold, contains_dj_speech, lyrics_scaffold
from .sliders import SliderFilters, filters_from_sliders
from .store import PromptStore, StoreError
from .textutil import BROKEN_TEXT_MESSAGE, broken_text, shown, shown_names
from .userpresets import USER_PREFIX, UserPresetError, build_user_preset, too_long_message

MAX_PAGE = 100
MAX_OFFSET = 100_000
MAX_QUERY = 100
MAX_TERMS = 8
MAX_LIST = 100

# Built-in styles named after a tradition or a regional scene. Their entries carry a plain note
# that they are a style direction, not a claim of authenticity. New styles carry the tag
# "heritage-inspired"; these older ones predate the tag.
HERITAGE_NOTE_IDS = frozenset({
    "afro-cuban-session", "ethiopian-jazz-evening", "bossa-courtyard", "highlife-guitar",
    "desert-guitar", "balkan-chamber", "dub-reggae", "tango-nuevo",
})
HERITAGE_NOTE = ("A style direction inspired by this tradition, written from its published musical features. "
                 "It is not a claim of authenticity, and it cannot replace listening to the real thing.")

_COMPOSE_KEYS = frozenset({
    "seed", "preset", "styles", "tags", "exclude_tags", "family", "energy", "query", "complexity", "density",
    "mode", "lyrics", "scaffold", "sliders", "avoid_repeats", "remember", "favorites_only", "include_user",
    "exclude_presets", "tag_style", "form",
})
_SPEECH_TAGS = re.compile(r"<\|[^|>]*\|>|\[(?:laughs?|whispers?|excited|sighs?|gasps?|coughs?)\]", re.IGNORECASE)
_SECTION_TAG = re.compile(r"^\[[^\]\n]{2,60}\]\s*$", re.MULTILINE)


class PromptError(ValueError):
    """An expected failure with a message for the person using the studio.

    When a saved style is refused because it breaks the style rules, ``findings`` holds the
    individual problems (each says which part of the style it is about) so a screen can mark them.
    """

    def __init__(self, message: str, findings: tuple = ()):
        super().__init__(message)
        self.findings = tuple(findings)


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat(timespec="seconds")


def _text_list(value: object, name: str, *, limit: int = MAX_LIST, item_limit: int = 100) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise PromptError(f"{name} must be a list of at most {limit} words.")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > item_limit:
            raise PromptError(f"{name} must contain only short, non-empty text.")
        result.append(item.strip())
    return result


def _choice(value: object, options: tuple[str, ...], name: str) -> str | None:
    if value is None:
        return None
    if value not in options:
        raise PromptError(f"{name} must be one of: {', '.join(options)}.")
    return value  # type: ignore[return-value]


def _flag(value: object, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PromptError(f"{name} must be true or false.")
    return value


def _count(value: object, name: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if type(value) is not int or not low <= value <= high:
        raise PromptError(f"{name} must be a whole number from {low} to {high}.")
    return value


def _names(titles: list[str]) -> str:
    """Up to three saved-style titles, each cut short, for a message."""
    text = ", ".join(shown(title, 40) for title in titles[:3])
    return text + (f" and {len(titles) - 3} more" if len(titles) > 3 else "")


def _query(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_QUERY:
        raise PromptError(f"Search words must be text of at most {MAX_QUERY} characters.")
    if len(value.split()) > MAX_TERMS:
        raise PromptError(f"Use at most {MAX_TERMS} search words.")
    return value.strip()


class PromptService:
    def __init__(
        self, store: PromptStore | None = None, *, library: Library | None = None,
        clock: Callable[[], float] = time.time, seed_source: Callable[[], int] | None = None,
        on_change: Callable[[], None] | None = None,
    ):
        """``on_change`` is called (with no arguments) after anything that changes what the store
        holds: a remembered compose, a saved or removed style, a favourite. The station extension
        uses it to keep its status current when other code calls ``compose`` directly."""
        self.library = library or shared_library()
        self.store = store
        self._clock = clock
        self._seed_source = seed_source or (lambda: secrets.randbelow(MAX_SEED + 1))
        self._on_change = on_change
        self._palette_presets = {p.id: len(self.library.arrangements) for p in self.library.palettes}
        self._fit_cache: dict[tuple, bool] = {}

    def _changed(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # the change is already made; a status refresh must never turn it into a failure
            pass

    # ---- helpers -----------------------------------------------------------------------
    def _favorites(self) -> dict[str, str]:
        return self.store.favorites() if self.store else {}

    def _user_presets(self) -> tuple[Preset, ...]:
        return self.store.user_presets() if self.store else ()

    def _need_store(self) -> PromptStore:
        if self.store is None:
            raise PromptError("Saved styles are not available right now, so this change was not made. "
                              "The built-in styles still work. Restart myradio, and check the data folder if this keeps happening.")
        return self.store

    @staticmethod
    def style_key(preset: Preset) -> str:
        return preset.id if preset.user_id else preset.palette.id

    def resolve(self, preset_id: str) -> Preset | None:
        if not isinstance(preset_id, str):
            return None
        if preset_id.startswith(USER_PREFIX):
            return self.store.get_user_preset(preset_id) if self.store else None
        try:
            return self.library.get(preset_id)
        except NoMatchingPreset:
            return None

    def _note(self, palette: Palette, based_on: str | None = None) -> str | None:
        """The "style direction, not authenticity" note: styles of a tradition and every copy of one.

        A copy keeps the note however many times it was copied and whatever its tags say now: the
        lineage (``based_on``) is followed back, through saved styles, to the built-in style it began as.
        """
        seen: set[str] = set()
        current_palette, current = palette, based_on
        while True:
            if current_palette.id in HERITAGE_NOTE_IDS or "heritage-inspired" in current_palette.tags:
                return HERITAGE_NOTE
            if not isinstance(current, str) or current in seen or len(seen) >= 50:
                return None
            seen.add(current)
            if current.startswith(USER_PREFIX):
                ancestor = self.store.get_user_preset(current) if self.store else None
                if ancestor is None:
                    return None
                row = self.store.user_row(current)
                current_palette, current = ancestor.palette, (row["based_on"] if row else None)
            else:
                try:
                    current_palette, current = self.library.get(current).palette, None
                except NoMatchingPreset:  # a style copied from one this catalog does not have: go by its name
                    return HERITAGE_NOTE if current.split("/")[0] in HERITAGE_NOTE_IDS else None

    def _fits(self, preset: Preset, complexity: str, density: str, mode: str) -> bool:
        """Whether a saved style can be written out at this treatment whatever the seed picks.

        Saved styles are checked for this when they are saved; this keeps one saved before that check
        existed (or changed in the file by hand) from failing a whole request. Built-in presets are
        proven to compile by the test suite and are not re-measured.
        """
        if not preset.user_id:
            return True
        key = (preset.id, preset.digest, complexity, density, mode)
        found = self._fit_cache.get(key)
        if found is None:
            found = longest_style_chars(preset, (complexity,), (density,), (mode,)) <= MAX_STYLE_CHARS
            if len(self._fit_cache) >= 4096:
                self._fit_cache.clear()
            self._fit_cache[key] = found
        return found

    def _origin(self, kind: str, item_id: str) -> str:
        return self.library.origins.get(f"{kind}:{item_id}", self.library.version)

    # ---- catalog items -----------------------------------------------------------------
    def _style_item(self, palette: Palette, favorites: dict[str, str]) -> dict:
        return {
            "id": palette.id, "name": palette.name, "family": palette.family, "tags": list(palette.tags),
            "energy": palette.energy, "bpm": list(palette.bpm), "meter": palette.meter,
            "moods": list(palette.moods), "leads": list(palette.leads), "supports": list(palette.supports),
            "bass": palette.bass, "source": "builtin", "favorite": palette.id in favorites,
            "presets": self._palette_presets.get(palette.id, 1), "introduced": self._origin("palette", palette.id),
            "heritage_note": self._note(palette),
        }

    def _preset_item(self, preset: Preset, favorites: dict[str, str]) -> dict:
        palette = preset.palette
        user = preset.user_id is not None
        return {
            "id": preset.id, "title": preset.title,
            "style": {"id": preset.id if user else palette.id, "name": palette.name},
            "arrangement": {"id": preset.arrangement.id, "name": preset.arrangement.name},
            "family": palette.family, "tags": list(palette.tags), "energy": palette.energy,
            "bpm": list(palette.bpm), "meter": palette.meter, "source": "user" if user else "builtin",
            "favorite": preset.id in favorites,
            "style_favorite": (not user) and palette.id in favorites,
            "heritage_note": self._note(palette, self._based_on(preset)),
        }

    def _based_on(self, preset: Preset) -> str | None:
        row = self.store.user_row(preset.id) if (preset.user_id and self.store) else None
        return row["based_on"] if row else None

    def _user_item(self, preset: Preset, favorites: dict[str, str]) -> dict:
        item = self._style_item(preset.palette, favorites)
        row = self.store.user_row(preset.id) if self.store else None
        item.update({
            "id": preset.id, "name": preset.title, "source": "user", "favorite": preset.id in favorites,
            "presets": 1, "introduced": None, "heritage_note": self._note(preset.palette, self._based_on(preset)),
            "arrangement": {"id": preset.arrangement.id, "name": preset.arrangement.name},
            "based_on": row["based_on"] if row else None, "revision": row["revision"] if row else None,
        })
        return item

    def _arrangement_item(self, arrangement: Arrangement) -> dict:
        return {
            "id": arrangement.id, "name": arrangement.name, "source": "builtin",
            "sections": {level: len(arrangement.sections.at(level)) for level in COMPLEXITIES},
            "introduced": self._origin("arrangement", arrangement.id),
            "dynamics": arrangement.dynamics, "ending": arrangement.ending,
        }

    # ---- browsing -----------------------------------------------------------------------
    def facets(self) -> dict:
        families: dict[str, int] = {}
        tags: dict[str, int] = {}
        energies = {name: 0 for name in ENERGIES}
        for palette in self.library.palettes:
            families[palette.family] = families.get(palette.family, 0) + 1
            energies[palette.energy] += 1
            for tag in palette.tags:
                tags[tag] = tags.get(tag, 0) + 1
        top_tags = sorted(tags.items(), key=lambda pair: (-pair[1], pair[0]))[:40]
        return {
            "families": [{"id": k, "count": v} for k, v in sorted(families.items())],
            "tags": [{"id": k, "count": v} for k, v in top_tags],
            "energy": [{"id": k, "count": v} for k, v in energies.items()],
        }

    def catalog(
        self, *, view: str = "presets", family: str | None = None, tags: Iterable[str] = (),
        exclude_tags: Iterable[str] = (), energy: str | None = None, query: str = "", source: str = "all",
        favorites: bool = False, sort: str = "catalog", offset: int = 0, limit: int = 24,
    ) -> dict:
        if view not in ("presets", "styles", "arrangements"):
            raise PromptError("view must be presets, styles or arrangements.")
        if source not in ("all", "builtin", "user"):
            raise PromptError("source must be all, builtin or user.")
        if sort not in ("catalog", "name"):
            raise PromptError("sort must be catalog or name.")
        if energy is not None and energy not in ENERGIES:
            raise PromptError(f"energy must be one of: {', '.join(ENERGIES)}.")
        offset = _count(offset, "offset", 0, 0, MAX_OFFSET)
        limit = _count(limit, "limit", 24, 1, MAX_PAGE)
        query = _query(query)
        for name, value in (("tags", tags), ("exclude_tags", exclude_tags)):
            if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
                raise PromptError(f"The filters were not understood: {name} must be a list of words, not one piece of text.")
            if len(value) > 20:
                raise PromptError(f"The filters were not understood: use at most 20 {name}.")
        try:
            wanted = _filters(list(tags), "tags")
            excluded = _filters(list(exclude_tags), "exclude_tags")
            if family is not None and (not isinstance(family, str) or not family.strip()):
                raise ValueError("family must be a nonempty string")
        except ValueError as exc:
            raise PromptError(f"The filters were not understood: {exc}.") from None
        fav = self._favorites()
        users = self._user_presets() if source != "builtin" else ()
        builtin = source != "user"

        if view == "arrangements":
            terms = query.casefold().split()
            items = [] if not builtin else [
                a for a in self.library.arrangements
                if all(t in " ".join((a.id, a.name, a.dynamics, a.ending, *(a.development.at(l) for l in COMPLEXITIES))).casefold() for t in terms)
            ]
            if sort == "name":
                items.sort(key=lambda a: a.name.casefold())
            page = items[offset:offset + limit]
            body = [self._arrangement_item(a) for a in page]
            total = len(items)
        elif view == "styles":
            terms = query.casefold().split()
            wanted_family = family.strip().lower() if family else None

            def keep(palette: Palette, key: str) -> bool:
                if wanted_family and palette.family != wanted_family:
                    return False
                if energy and palette.energy != energy:
                    return False
                if not wanted.issubset(palette.tags) or excluded.intersection(palette.tags):
                    return False
                if favorites and key not in fav:
                    return False
                return all(t in self._style_text(palette, key) for t in terms)

            rows = [(p.name, self._style_item, p) for p in self.library.palettes if builtin and keep(p, p.id)]
            rows += [(u.title, self._user_item, u) for u in users if keep(u.palette, u.id)]
            if sort == "name":
                rows.sort(key=lambda row: row[0].casefold())
            total = len(rows)
            body = [make(obj, fav) for _, make, obj in rows[offset:offset + limit]]
        else:
            pool: list[Preset] = list(self.library.presets) if builtin else []
            pool.extend(users)
            try:
                matched = filter_presets(pool, family=family, tags=wanted, energy=energy,
                                         exclude_tags=excluded, query=query)
            except ValueError as exc:
                raise PromptError(f"The filters were not understood: {exc}.") from None
            if favorites:
                matched = tuple(p for p in matched if p.id in fav or self.style_key(p) in fav)
            if sort == "name":
                matched = tuple(sorted(matched, key=lambda p: p.title.casefold()))
            total = len(matched)
            body = [self._preset_item(p, fav) for p in matched[offset:offset + limit]]

        end = offset + len(body)
        return {
            "view": view, "catalog_version": self.library.version, "total": total, "offset": offset,
            "limit": limit, "next_offset": end if end < total else None, "items": body,
            "facets": self.facets(),
        }

    def _style_text(self, palette: Palette, key: str) -> str:
        return " ".join((
            key, palette.name, palette.family, *palette.tags, palette.meter, *palette.moods, *palette.leads,
            *palette.supports, palette.bass, *palette.motifs, palette.production,
            *(palette.harmony.at(level) for level in COMPLEXITIES), *(palette.rhythm.at(level) for level in COMPLEXITIES),
        )).casefold()

    def search(self, query: str, *, limit: int = 10) -> dict:
        query = _query(query)
        limit = _count(limit, "limit", 10, 1, 25)
        terms = query.casefold().split()
        if not terms:
            raise PromptError("Type a word or two to search for, such as 'waltz', 'flute' or 'late night'.")
        fav = self._favorites()

        def score(groups: list[tuple[str, int, str]]) -> tuple[int, list[str]]:
            total, hits = 0, []
            for term in terms:
                best, where = 0, None
                for label, weight, text in groups:
                    if term in text and weight > best:
                        best, where = weight, label
                if not best:
                    return 0, []
                total += best
                if where and where not in hits:
                    hits.append(where)
            return total, hits

        def style_groups(palette: Palette, key: str, name: str) -> list[tuple[str, int, str]]:
            return [
                ("name", 5, " ".join((key, name, palette.family)).casefold()),
                ("tags", 4, " ".join(palette.tags).casefold()),
                ("instruments", 3, " ".join((*palette.leads, *palette.supports, palette.bass)).casefold()),
                ("mood", 2, " ".join((*palette.moods, *palette.motifs)).casefold()),
                ("details", 1, " ".join((palette.meter, palette.production,
                                         *(palette.harmony.at(l) for l in COMPLEXITIES),
                                         *(palette.rhythm.at(l) for l in COMPLEXITIES))).casefold()),
            ]

        styles = []
        for index, palette in enumerate(self.library.palettes):
            points, hits = score(style_groups(palette, palette.id, palette.name))
            if points:
                styles.append((-points, index, {**self._style_item(palette, fav), "matched": hits}))
        mine = []
        for index, preset in enumerate(self._user_presets()):
            points, hits = score(style_groups(preset.palette, preset.id, preset.title))
            if points:
                mine.append((-points, index, {**self._user_item(preset, fav), "matched": hits}))
        arrangements = []
        for index, arrangement in enumerate(self.library.arrangements):
            groups = [
                ("name", 5, f"{arrangement.id} {arrangement.name}".casefold()),
                ("details", 1, " ".join((arrangement.dynamics, arrangement.ending,
                                         *(arrangement.development.at(l) for l in COMPLEXITIES),
                                         *(s for l in COMPLEXITIES for s in arrangement.sections.at(l)))).casefold()),
            ]
            points, hits = score(groups)
            if points:
                arrangements.append((-points, index, {**self._arrangement_item(arrangement), "matched": hits}))
        result = {name: [item for _, _, item in sorted(found, key=lambda x: (x[0], x[1]))[:limit]]
                  for name, found in (("styles", styles), ("user_presets", mine), ("arrangements", arrangements))}
        result["query"] = query
        result["total"] = len(styles) + len(mine) + len(arrangements)
        return result

    # ---- sliders and lyrics --------------------------------------------------------------
    def sliders(self, energy: object, adventurousness: object, vocals: object) -> dict:
        try:
            return filters_from_sliders(energy, adventurousness, vocals).to_dict()  # type: ignore[arg-type]
        except ValueError as exc:
            raise PromptError(str(exc) + ".") from None

    def lyrics(self, preset_id: str, seed: int, *, tag_style: str = "title", form: str | None = None) -> dict:
        preset = self.resolve(preset_id)
        if preset is None:
            raise PromptError(f"There is no style called \"{shown(preset_id)}\". Check the catalog for the right id.")
        if type(seed) is not int or not 0 <= seed <= MAX_SEED:
            raise PromptError(f"The seed must be a whole number from 0 to {MAX_SEED}.")
        _choice(tag_style, TAG_STYLES, "tag_style")
        _choice(form, FORMS, "form")
        return self._outline(preset, seed, tag_style, form).to_dict()

    @staticmethod
    def _outline(preset: Preset, seed: int, tag_style: str, form: str | None) -> LyricScaffold:
        try:
            return lyrics_scaffold(preset, seed, tag_style=tag_style, form=form)
        except ValueError:
            raise PromptError(
                "A lyric outline could not be written for this style, because its meter could not be read. "
                "Open the style, set the meter to something like 4/4 or 7/8, and save it again."
            ) from None

    # ---- saved styles ------------------------------------------------------------------
    def favorite(self, item_id: str, on: bool) -> dict:
        store = self._need_store()
        if not isinstance(item_id, str) or not item_id.strip():
            raise PromptError("Say which style or preset to mark as a favourite.")
        item_id = item_id.strip()
        if item_id.startswith(USER_PREFIX):
            if store.get_user_preset(item_id) is None:
                if not on and item_id in store.favorites():  # a leftover mark can always be cleared
                    store.set_favorite(item_id, "preset", False)
                    self._changed()
                    return {"id": item_id, "favorite": False, "kind": "preset"}
                raise PromptError(f"There is no saved style called \"{shown(item_id)}\".")
            kind = "preset"
        elif "/" in item_id:
            if self.resolve(item_id) is None:
                raise PromptError(f"There is no preset called \"{shown(item_id)}\". Check the catalog for the right id.")
            kind = "preset"
        else:
            try:
                self.library.get_palette(item_id)
            except NoMatchingPreset:
                raise PromptError(f"There is no style called \"{shown(item_id)}\". Check the catalog for the right id.") from None
            kind = "style"
        try:
            store.set_favorite(item_id, kind, on)
        except StoreError as exc:
            raise PromptError(str(exc)) from None
        self._changed()
        return {"id": item_id, "favorite": bool(on), "kind": kind}

    def save_custom(self, spec: object) -> dict:
        store = self._need_store()
        try:
            record, warnings = build_user_preset(spec, library=self.library, resolve=self.resolve)
            row = store.save_user_preset(record)
        except UserPresetError as exc:
            raise PromptError(str(exc), exc.findings) from None
        except StoreError as exc:
            raise PromptError(str(exc)) from None
        self._changed()
        return {
            "id": record.id, "title": record.title, "revision": row["revision"], "created": row["created_now"],
            "digest": record.digest, "based_on": record.based_on,
            "warnings": [item.message for item in warnings],
        }

    def remove_custom(self, preset_id: str) -> dict:
        store = self._need_store()
        if not isinstance(preset_id, str) or not preset_id.startswith(USER_PREFIX):
            raise PromptError("Only styles you saved yourself can be removed. Their ids start with \"user:\".")
        try:
            removed = store.remove_user_preset(preset_id)
        except StoreError as exc:
            raise PromptError(str(exc)) from None
        if not removed:
            raise PromptError(f"There is no saved style called \"{shown(preset_id)}\". Nothing was removed.")
        self._changed()
        return {"id": preset_id, "removed": True}

    # ---- composing ---------------------------------------------------------------------
    def _lyrics_warnings(self, text: str) -> list[str]:
        notes = []
        if _SPEECH_TAGS.search(text):
            notes.append("These lyrics contain markers for a spoken voice. The music maker would sing them as words, "
                         "so take them out. Spoken parts belong to the host voices.")
        if contains_dj_speech(text):
            notes.append("Some wording sounds like a radio host or announcer. Check that the song should really say it.")
        if not _SECTION_TAG.search(text):
            notes.append("The lyrics have no section markers such as [Verse] or [Chorus] on their own lines. "
                         "The music maker follows the song's shape better with them.")
        return notes

    def compose(self, request: object) -> dict:
        if not isinstance(request, dict):
            raise PromptError("Send the settings as an object, for example {\"seed\": 7}.")
        unknown = sorted(set(request) - _COMPOSE_KEYS)
        if unknown:
            raise PromptError(f"These settings are not recognised: {shown_names(unknown)}.")
        if broken_text(request):
            raise PromptError(BROKEN_TEXT_MESSAGE)
        seed = request.get("seed")
        if seed is None:
            seed = self._seed_source()
        elif type(seed) is not int or not 0 <= seed <= MAX_SEED:
            raise PromptError(f"The seed must be a whole number from 0 to {MAX_SEED}.")

        sliders: SliderFilters | None = None
        if request.get("sliders") is not None:
            raw = request["sliders"]
            if not isinstance(raw, dict) or set(raw) != {"energy", "adventurousness", "vocals"}:
                raise PromptError("Give all three sliders: energy, adventurousness and vocals (whole numbers from 0 to 100).")
            sliders = filters_from_sliders_checked(raw)
        complexity = _choice(request.get("complexity"), COMPLEXITIES, "complexity")
        density = _choice(request.get("density"), DENSITIES, "density")
        mode = _choice(request.get("mode"), MODES, "mode")
        energy = _choice(request.get("energy"), ENERGIES, "energy")
        tag_style = _choice(request.get("tag_style"), TAG_STYLES, "tag_style") or "title"
        form = _choice(request.get("form"), FORMS, "form")
        avoid = _flag(request.get("avoid_repeats"), "avoid_repeats", True)
        remember = _flag(request.get("remember"), "remember", True)
        favorites_only = _flag(request.get("favorites_only"), "favorites_only", False)
        include_user = _flag(request.get("include_user"), "include_user", True)
        scaffold_flag = request.get("scaffold")
        if scaffold_flag is not None and not isinstance(scaffold_flag, bool):
            raise PromptError("scaffold must be true or false.")
        lyrics = request.get("lyrics")
        if lyrics is not None:
            if not isinstance(lyrics, str) or not lyrics.strip():
                raise PromptError("The lyrics must be text. Leave them out to get an instrumental or a suggested outline.")
            if len(lyrics) > MAX_LYRICS_CHARS or "\x00" in lyrics:
                raise PromptError(f"The lyrics must be at most {MAX_LYRICS_CHARS:,} characters.")
        if lyrics is not None and scaffold_flag:
            raise PromptError("Choose either your own lyrics or a suggested outline, not both.")

        # treatment: explicit settings beat the sliders, which beat the library defaults
        effective_energy = energy or (sliders.energy_class if sliders else None)
        final_complexity = complexity or (sliders.complexity if sliders else "layered")
        final_density = density or (sliders.density if sliders else "balanced")
        if mode is not None:
            final_mode = mode
        elif lyrics is not None or scaffold_flag:
            final_mode = "vocal"
        elif sliders is not None:
            final_mode = sliders.mode_for(seed)
        else:
            final_mode = "instrumental"
        if final_mode == "instrumental" and (lyrics is not None or scaffold_flag):
            raise PromptError("You asked for an instrumental but also gave lyrics. Choose one or the other.")
        if final_mode == "vocal" and lyrics is None and scaffold_flag is False:
            raise PromptError("A sung song needs words. Give your own lyrics, or allow a suggested outline.")

        # Which preset. When the repeat memory is used, choosing and recording the choice happen under one
        # lock, so two requests at the same moment cannot both read the same history and pick the same preset.
        guard = self.store.exclusive() if (remember and self.store is not None) else contextlib.nullcontext()
        warnings: list[str] = []
        with guard:
            chosen_pool = 0
            relaxed = None
            avoided: dict = {}
            preset_id = request.get("preset")
            if preset_id is not None:
                if not isinstance(preset_id, str):
                    raise PromptError("preset must be the id of a style, such as warm-brush-jazz/theme-and-return.")
                preset = self.resolve(preset_id)
                if preset is None:
                    raise PromptError(f"There is no style called \"{shown(preset_id)}\". Check the catalog for the right id.")
                if not self._fits(preset, final_complexity, final_density, final_mode):
                    raise PromptError(too_long_message(
                        longest_style_chars(preset, (final_complexity,), (final_density,), (final_mode,))))
                method = "exact"
                chosen_pool = 1
                # Choosing settings are ignored for a style that was named, so report the style's own energy.
                if energy is not None and energy != preset.palette.energy:
                    warnings.append(f"You named this style, so its own energy ({preset.palette.energy}) was kept "
                                    f"instead of {energy}.")
                effective_energy = preset.palette.energy
            else:
                method = "chosen"
                styles = _text_list(request.get("styles"), "styles")
                tags = _text_list(request.get("tags"), "tags", limit=20)
                exclude_tags = _text_list(request.get("exclude_tags"), "exclude_tags", limit=20)
                excluded_presets = set(_text_list(request.get("exclude_presets"), "exclude_presets", limit=200, item_limit=120))
                family = request.get("family")
                if family is not None and (not isinstance(family, str) or not family.strip() or len(family) > 60):
                    raise PromptError("family must be a short word such as jazz.")
                query = _query(request.get("query"))
                pool: list[Preset] = list(self.library.presets)
                if include_user:
                    pool.extend(self._user_presets())
                everything = pool
                fav = self._favorites() if favorites_only else {}
                too_long: list[str] = []  # saved styles that match but cannot be written out at this treatment

                def narrow(energy_class: str | None) -> list[Preset]:
                    try:
                        found = list(filter_presets(everything, family=family, tags=tags, energy=energy_class,
                                                    exclude_tags=exclude_tags, query=query))
                    except ValueError as exc:
                        raise PromptError(f"The filters were not understood: {exc}.") from None
                    if styles:
                        allowed = {s.lower() for s in styles}
                        found = [p for p in found if self.style_key(p).lower() in allowed or p.id.lower() in allowed]
                    if favorites_only:
                        found = [p for p in found if p.id in fav or self.style_key(p) in fav]
                    if excluded_presets:
                        found = [p for p in found if p.id not in excluded_presets]
                    usable, left_out = [], []
                    for p in found:
                        (usable if self._fits(p, final_complexity, final_density, final_mode) else left_out).append(p)
                    too_long[:] = [p.title for p in left_out]
                    return usable

                pool = narrow(effective_energy)
                if not pool and energy is None and sliders is not None:
                    # The energy slider is a preference, unlike an explicit energy or the other filters.
                    pool = narrow(None)
                    if pool:
                        relaxed, effective_energy = "slider-energy", None
                if not pool:
                    if too_long:
                        raise PromptError(
                            f"Every saved style that matches ({_names(too_long)}) is too long to be written out at these "
                            "settings. Shorten its notes, or ask for a simpler arrangement, then try again.")
                    raise PromptError("No styles match those settings. Loosen a filter (fewer tags, another energy) "
                                      "or turn off favourites only, then try again.")
                if too_long:
                    warnings.append(f"{len(too_long)} saved style{'s' if len(too_long) != 1 else ''} ({_names(too_long)}) "
                                    f"{'were' if len(too_long) != 1 else 'was'} left out because "
                                    f"{'they are' if len(too_long) != 1 else 'it is'} too long to be written out at these settings.")
                preset, history_relaxed, avoided = self._select(pool, seed, avoid, sliders)
                relaxed = "+".join(part for part in (relaxed, history_relaxed) if part) or None
                chosen_pool = len(pool)

            # lyrics
            lyric_info: dict = {"source": "none", "text": None, "scaffold": None}
            lyric_text: str | None = None
            if final_mode == "vocal":
                if lyrics is not None:
                    lyric_text = lyrics
                    lyric_info = {"source": "supplied", "text": lyrics, "scaffold": None}
                    warnings.extend(self._lyrics_warnings(lyrics))
                else:
                    outline: LyricScaffold = self._outline(preset, seed, tag_style, form)
                    lyric_text = outline.text
                    lyric_info = {"source": "scaffold", "text": outline.text, "scaffold": outline.to_dict()}
            try:
                prompt: SongPrompt = self.library.compose(
                    preset, seed, complexity=final_complexity, density=final_density, mode=final_mode, lyrics=lyric_text,
                )
            except CatalogError:
                raise PromptError(too_long_message(
                    longest_style_chars(preset, (final_complexity,), (final_density,), (final_mode,)))) from None
            except ValueError:
                raise PromptError("That combination could not be written. Try different settings.") from None

            remembered = False
            if remember and self.store is not None:
                try:
                    self.store.remember(prompt, self.style_key(preset))
                    remembered = True
                except StoreError as exc:
                    warnings.append("The repeat memory could not be updated, so this choice may come round again soon. " + str(exc))
            user_info = None
            if preset.user_id and self.store is not None:
                row = self.store.user_row(preset.id)
                user_info = {"id": preset.id, "revision": row["revision"] if row else None, "digest": preset.digest}
            summary = sliders.summary() if sliders else None
            if summary and method == "exact" and sliders.energy_class != preset.palette.energy:
                summary += f". You named a {preset.palette.energy} style, so its own energy was kept."
            result = {
                "id": preset.id,
                "prompt": prompt.to_dict(),
                "lyrics": lyric_info,
                "selection": {"method": method, "candidates": chosen_pool, "relaxed": relaxed, "avoided": avoided},
                "treatment": {"complexity": final_complexity, "density": final_density, "mode": final_mode,
                              "energy": effective_energy, "from_sliders": sliders is not None,
                              "summary": summary},
                "provenance": {
                    "preset_id": preset.id, "seed": seed, "catalog_version": prompt.catalog_version,
                    "complexity": final_complexity, "density": final_density, "mode": final_mode,
                    "fingerprint": prompt.fingerprint, "lyrics_source": lyric_info["source"],
                    "user_preset": user_info, "composed_at": _iso(self._clock()),
                },
                "warnings": warnings,
                "remembered": remembered,
            }
        if remembered:
            self._changed()
        return result

    def _select(self, pool: list[Preset], seed: int, avoid: bool, sliders: SliderFilters | None) -> tuple[Preset, str | None, dict]:
        """Choose one preset from the pool, avoiding what was composed recently.

        History is relaxed in steps when it would leave nothing; the person's own filters are never
        relaxed. With no history, or with avoidance off, this is exactly ``Library.choose``.
        """
        if not avoid or self.store is None:
            return random.Random(seed).choice(pool), None, {}
        windows = (sliders.preset_window, sliders.style_window, sliders.arrangement_window) if sliders else (200, 6, 3)
        try:
            recent_presets, recent_styles, recent_arrangements = self.store.recent_sets(*windows)
        except StoreError:
            return random.Random(seed).choice(pool), "memory-unavailable", {}
        rungs = (
            (None, recent_presets, recent_styles, recent_arrangements),
            ("arrangements", recent_presets, recent_styles, ()),
            ("styles", recent_presets, (), ()),
            ("older-presets", recent_presets[:10], (), ()),
            ("last-preset", recent_presets[:1], (), ()),
            ("everything", (), (), ()),
        )
        for name, presets, styles, arrangements in rungs:
            ids, style_set, arrangement_set = set(presets), set(styles), set(arrangements)
            candidates = [p for p in pool if p.id not in ids and self.style_key(p) not in style_set
                          and p.arrangement.id not in arrangement_set]
            if candidates:
                return random.Random(seed).choice(candidates), name, {
                    "presets": len(ids), "styles": sorted(style_set), "arrangements": sorted(arrangement_set),
                }
        raise PromptError("No styles match those settings.")  # unreachable: the last rung keeps the whole pool

    def replay(self, record: dict) -> dict:
        """Re-compose a stored prompt record and prove it is unchanged (built-in presets only)."""
        preset_id = record.get("preset_id") if isinstance(record, dict) else None
        if isinstance(preset_id, str) and preset_id.startswith(USER_PREFIX):
            raise PromptError("A song made from one of your own styles can be replayed with compose, using the same preset and seed.")
        try:
            return replay(record, None).to_dict()
        except (ValueError, KeyError) as exc:
            raise PromptError(str(exc)) from None

    def snapshot(self) -> dict:
        """Counts for the status panel and the doctor (reads the store, so not for status() itself)."""
        counts = self.store.counts() if self.store else {"user_presets": 0, "favorites": 0, "history": 0, "last_composed": None}
        return {
            "catalog_version": self.library.version, "styles": len(self.library.palettes),
            "arrangements": len(self.library.arrangements), "presets": len(self.library.presets),
            "saved_styles": counts["user_presets"], "favorites": counts["favorites"],
            "remembered": counts["history"],
            "last_composed": _iso(counts["last_composed"]) if counts["last_composed"] else None,
            "damaged": list(self.store.damaged) if self.store else [],
            "store_available": self.store is not None,
        }


def filters_from_sliders_checked(raw: dict) -> SliderFilters:
    try:
        return filters_from_sliders(raw["energy"], raw["adventurousness"], raw["vocals"])
    except ValueError as exc:
        raise PromptError(str(exc) + ".") from None
