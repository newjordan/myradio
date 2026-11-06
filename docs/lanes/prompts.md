# Lane contract: prompts (the music-style library)

Owner paths: `myradio/prompts/**`, `tests/test_prompts*.py`, `docs/PROMPTS.md`, `docs/lanes/prompts.md`. Extension name: `prompts` (`myradio/prompts/extension.py`, `build()`). User-facing rules, tables and examples are in `docs/PROMPTS.md`; this document is the engineering contract.

## 1. Purpose and honest status

Grow the music-prompt ("word salad") library for YuE2 and make it usable by a nontechnical person: more styles and arrangement designs with real compositional content, rules that stop a style contradicting itself, three studio sliders mapped to library filters, a lyric outline for sung music, saved styles and favourites, and a repeat memory. It writes text prompts only. It never starts a model, makes audio, or touches the network.

**Real: executed in this session and observed**

- The catalog grew from 32 x 12 = 384 presets to **71 palettes x 18 arrangement designs = 1,278 presets** (39 new palettes, 6 new designs). All 23,004 combinations of preset x complexity x density x mode compile, and every one of them is distinct.
- The original 384 presets are unchanged. Three independent proofs: the golden freeze (`tests/test_prompts_golden.py`, made from the untouched catalog before any change); the two original data files are byte-for-byte identical (SHA-256 asserted); and a one-off differential run in which **69,120 prompts** (the 384 old presets x 10 seeds incl. 0, 1, 7, 2^63-1 and six random ones x 18 treatments) compiled by the *original* pre-change source and by the new tree hash to the same value (`058108902dd21757f9448bf08023591dea87127d78dc5376a3978c41499593e0`).
- The second pass changed no built-in prompt: 230,040 compiled records (1,278 presets x 10 seeds x 18 treatments) hash to the same value before and after, and the golden freeze still passes.
- The compatibility checker (27 explicit rules), the slider mapping, the lyric scaffold, the SQLite store (saved styles, favourites, repeat memory), the service, the extension, the CLI additions and the five HTTP routes. The extension was run inside the real `ExtensionHost`, the real `Station`, and the real loopback `Server` (127.0.0.1, port 0, a made-up token).
- 298 tests in my lane pass on Python 3.12.3, on 3.11.15 and on 3.13.14 (the interpreter in the project's own `.venv`; earlier notes said 3.13 had never been run). About 30 s wall and 16 s of CPU under `nice -n 19 ionice -c3`. They run clean with `-X dev -W always::ResourceWarning` (no unclosed connection, file or socket). Python 3.10 is not installed here and was not run.
- Deliberate breakages of the code were each caught by at least one test. First pass: twelve (a moved slider band, a relaxed tempo ceiling, tradition mixing allowed, old presets re-keyed, a scaffold line one syllable short, the host-wording guard disabled, the repeat memory ignored, `status()` reading the database, the content digest left out of a saved style's random key, saved styles skipping the rules, the favourites filter ignored, and an edited original data file). Second pass: 52 more, listed in section 7, all caught.

**Verified only against fakes**

- `ForbiddenStation` (a fake station that raises if `status()`, `import_audio()` or `action()` is ever called) stands in for the station in the host-level tests, which proves the extension never calls them. The HTTP tests use the real station.
- Nothing in this lane depends on an external system, so there are no fakes of model workers, feeds or destinations.

**Second pass (this document describes the code after it).** Two independent reviewers re-examined the lane. Their blocker (a zero-length bar made the lyric outline loop forever), their major findings (saved styles tied to the catalog version, saved styles that could not be written out, error text that echoed the caller's input, wording that was not plain) and their minor ones were fixed, each with a regression test that fails on the code as reviewed. Section 10 lists every finding, the fix and the test.

**Unverified (cannot be checked here, and not claimed)**

- Any musical or audio-quality outcome: whether YuE2 follows the arrangements, renders 7/8, 9/8, 10/8, 11/8 or 12/8, honours a named mode or tuning, sings the generated outline clearly, or prefers `[Verse 1]` over `[verse]`. No model was run. A passing structural, compatibility or syllable test is not an audio-quality claim.
- That the traditions are described the way their practitioners would describe them. The texts were written from published musical features and have **not been reviewed by practitioners**; each is a style direction, not a claim of authenticity.
- Python 3.10 and 3.13, and macOS: not run here (only 3.11 and 3.12 on Linux exist on this machine). The code avoids 3.11+-only features by review, not by execution.
- A built wheel: this machine has no `setuptools` or `pip` and no network, so a wheel could not be built. The new data files sit directly in `data/` where the existing `data/*.json` package-data pattern matches them, but that is not demonstrated by an install.
- Behaviour after power loss mid-write (SQLite WAL with default durability is relied on, not tested).
- No UI panel exists yet (`web/**` is not mine); section 5 is the contract for it.

## 2. Public API

Everything below is importable from `myradio.prompts` unless a module is named. Errors raised on purpose by the service, store and saved-style code (`PromptError`, `StoreError`, `UserPresetError`) are `ValueError` subclasses whose messages are written for a person. The pure library raises plain `ValueError`, `CatalogError` and `NoMatchingPreset` (all `ValueError`) with technical messages; the service translates them.

### Catalog (`library.py`)

| Name | Signature | Notes |
| --- | --- | --- |
| `load_library` | `(through: str \| None = None) -> Library` | Parses the packaged packs. `through="1.0.0"` returns the original 32 x 12 library. Unknown version: `CatalogError`; non-string: `ValueError`. |
| `shared_library` | `(through=None) -> Library` | Process-wide cache of `load_library` (the catalog is immutable). |
| `Library.from_documents` | `(palettes, arrangements) -> Library` | One document pair; unchanged behaviour. |
| `Library.from_packs` | `(packs: Sequence[(palettes_doc, arrangements_doc)]) -> Library` | Oldest first; each pair needs matching, dotted-number, strictly increasing versions; ids unique across packs; every palette meets every design. |
| `Library.versions` / `.through` / `.epoch_for` | `() -> tuple[str, ...]` / `(version) -> Library` / `(preset) -> str` | `epoch_for` is the catalog version that keys a preset's randomness: the later of its palette's and design's introduction. |
| `Library.get`, `.get_palette`, `.get_arrangement` | `(id)` | Raise `NoMatchingPreset`. |
| `Library.browse`, `.choose` | unchanged signatures | Same semantics. `choose` draws from more candidates now, so a seed picks differently than in 1.0.0. |
| `Library.generate` | `(preset_id, seed, *, complexity="layered", density="balanced", mode="instrumental", lyrics=None) -> SongPrompt` | Unchanged. Vocal mode still requires supplied lyrics (which must be text that can be written as UTF-8: half an emoji raises `ValueError`). |
| `longest_style_chars` | `(preset, complexities=COMPLEXITIES, densities=DENSITIES, modes=MODES) -> int` | The most characters the written-out style of a preset can have **for any seed**, over the given treatments (a seed picks one mood, lead, support and motif and a tempo, so the longest uses the longest of each). Exact: a test writes out every combination for every palette. Used when a style is saved and when composing. |
| `FieldsError` | `CatalogError` subclass: `.path`, `.missing`, `.extra` | Raised for parts that are missing or not recognised. Its text quotes at most five unknown names, each cut to 30 characters; the full names stay on the exception. |
| `Library.compose` | same, first argument a `Preset` object | Compiles any preset, including a saved style. |
| `filter_presets` | `(presets, *, family, tags, energy, exclude_tags, query) -> tuple[Preset, ...]` | The one filter used for built-in and saved presets. |
| `replay` | `(record: Mapping, library=None) -> SongPrompt` | Re-composes a stored built-in record against its own catalog version and checks the fingerprint; `ValueError` on mismatch, damage or a missing field. |
| `Preset` | `(palette, arrangement, user_id=None, digest="", user_title=None)` | `.id`, `.title`, `.to_dict()`, cached `.search_text`. Built-in id `palette/arrangement`; saved style id `user:<slug>`. |
| `SongPrompt` | unchanged | Same record shape and fingerprint rule. |

**Text rule** (`Palette.parse` and `Arrangement.parse`, for the shipped catalog and for saved styles alike): every text field is one line with single spaces and nothing at either end, with no control character (NUL, escape and the rest), no line or paragraph separator, no text-direction override and no lone surrogate (half an emoji). The whole shipped catalog already followed this (a test proves it), so no shipped text changed.

### Compatibility rules (`compat.py`, `instruments.py`)

`check_palette(palette) -> list[Finding]`, `check_arrangement(arrangement)`, `check_preset(palette, arrangement)`, `check_library(library) -> {ok, palettes, arrangements, errors[], warnings[], rules}`, `check_library_cached(library)`, `errors(findings)`, `RULES: dict[rule id -> (severity, description)]` (27 rules; the table is in `docs/PROMPTS.md` and a test keeps the two in step). `Finding(rule, severity, subject, field, message)` with `.to_dict()`; `severity` is `"error"` or `"warning"`. `instruments.find_instruments(text) -> list[Instrument]` reads the lexicon (115 patterns with flags: melodic, bass, bass-only, percussion, electric, fixed-pitch, texture, neutral, intense, plus tradition). `parse_meter(meter) -> (beats in a bar, note value, declared grouping | None) | None` is the one reader of meters: one or two ASCII digits, never zero, no leading zero, over 2, 4, 8 or 16, with an optional grouping of non-zero single digits (the lyric scaffold uses it too). `speaks_like_a_host(text) -> bool` and `HOST_PHRASES` are what the `text.speech-markup` rule applies; the phrases are drawn from the lyric guard's list without the words that have an everyday musical sense (`studio`, `radio`, `frequency`, `tune in`, `this is`, `coming up`, `listening to`, `broadcast`). Messages quote at most 60 characters of the style's own text.

### Sliders (`sliders.py`)

`filters_from_sliders(energy: int, adventurousness: int, vocals: int) -> SliderFilters`. Whole numbers 0 to 100 only (a bool, float, string or out-of-range value raises `ValueError("... must be a whole number from 0 to 100")`). `SliderFilters` fields: `energy, adventurousness, vocals, energy_class, density, complexity, vocal_share, style_window, arrangement_window, preset_window`; members `.modes`, `.mode_for(seed) -> "instrumental" | "vocal"`, `.browse_filters() -> {"energy": ...}`, `.summary()`, `.to_dict()`. The mapping table is in `docs/PROMPTS.md`.

### Lyric scaffold (`lyrics.py`)

`lyrics_scaffold(preset: Preset | str, seed: int, *, tag_style="title" | "lower", form=None | "short" | "full") -> LyricScaffold`. `LyricScaffold` fields: `preset_id, seed, tag_style, form, world, syllables_per_line, motif, motif_syllables, sections` (each `ScaffoldSection(tag, kind, scheme, lines)` of `ScaffoldLine(text, syllables, rhyme)`), `source` (always `"scaffold"`); `.text` (model-ready lyrics), `.plan()`, `.to_dict()`. Also `contains_dj_speech(text) -> list[str]` (whole-word phrase guard), `validate_scaffold_text(text) -> list[str]`, `count_syllables(word)` (a heuristic used only to cross-check the word bank), `line_syllables_for_meter(meter)` (it reads the meter with `compat.parse_meter`; a meter that cannot be read, or that has no length, raises `ValueError` and can never loop). Raises `ValueError` for a bad seed, tag style, form or preset.

### Saved styles and memory (`userpresets.py`, `store.py`)

- `build_user_preset(spec, *, library, resolve) -> (UserPresetRecord, warnings)`; raises `UserPresetError(message, findings)`. Spec: `{id, title?, based_on?, palette?, arrangement?}` (see `docs/PROMPTS.md`). The record holds full copies of the palette and design plus a 16-hex content digest. Checks, in order: nesting and size, broken characters, the optional title (one tidy line, no host wording), the schema (with the one-line text rule), every compatibility rule for the style and the design (problems are listed with where they are, one sentence once), and that the style can be written out for every seed within 6,000 characters (`too_long_message`). A saved style's random choices are keyed by `user-style:<id>#<digest>:<seed>` and never by the catalog version.
- `PromptStore.open(path, *, clock=time.time)` (creates the file 0600; refuses a symbolic link) or `PromptStore(conn, *, clock=...)` with a connection from `ctx.open_db`. The store owns the connection it is given: if it cannot use it (a file from a newer myradio, a damaged file, an unexpected fault) it closes it before raising. Methods: `exclusive()` (the lock to hold while choosing from the repeat memory and recording the choice), `close()`, `checkpoint()`, `user_presets() -> tuple[Preset, ...]` (lock-free), `get_user_preset(id)`, `user_row(id) -> {id, slug, title, based_on, digest, revision, created, updated} | None`, `save_user_preset(record) -> row + created_now` (a record that would not read back, for example text the rules refuse, raises `StoreError` before the file is touched), `remove_user_preset(id) -> bool`, `favorites() -> {id: "style" | "preset"}`, `set_favorite(id, kind, on) -> bool`, `remember(prompt, style_key) -> seq`, `recent(limit)`, `recent_sets(preset_window, style_window, arrangement_window)`, `history_count()`, `forget_history()`, `counts()`, `damaged` (slugs set aside). Raises `StoreError`.

### Service (`service.py`)

`PromptService(store=None, *, library=None, clock=time.time, seed_source=None, on_change=None)`; `store=None` is the degraded mode (built-in library works, changes refuse with a plain message). `on_change` is called with no arguments after anything that changes what the store holds (a remembered compose, a saved or removed style, a favourite), after the memory lock is released, and an exception in it is ignored: the extension passes its status refresh, so the status stays right when other code calls `compose` directly. Methods return JSON-ready dicts and raise `PromptError` (a `ValueError` whose `.findings` holds the individual style-rule problems when a saved style was refused for breaking the rules; each `Finding` says which part it is about):

- `catalog(*, view="presets", family, tags, exclude_tags, energy, query, source="all", favorites=False, sort="catalog", offset=0, limit=24)`
- `search(query, *, limit=10)`, `sliders(energy, adventurousness, vocals)`, `lyrics(preset_id, seed, *, tag_style, form)`
- `favorite(id, on)`, `save_custom(spec)`, `remove_custom(id)`
- `compose(request)`, `replay(record)`, `snapshot()`, `resolve(preset_id) -> Preset | None`, `facets()`

The generation lane and the station recipes should call `PromptService.compose` (or the pure functions) directly; they should treat preset ids as opaque strings (`palette/arrangement` or `user:<slug>`) and store the returned `provenance` and `prompt` records with the track. A direct call keeps the extension's status and the doctor's repeat-memory line current (through `on_change`).

### Text helpers (`textutil.py`)

`shown(value, limit=60)` (printable, cut to `limit`), `shown_names(names, limit=5)`, `has_odd_character(text)`, `is_tidy_line(text)`, `encodable(text)`, `broken_text(value)` (walks a JSON-like value without recursion for text that cannot be written as UTF-8) and `BROKEN_TEXT_MESSAGE`. The file is plain ASCII on purpose: special characters are written as code points.

## 3. Persistence

| Item | Where | Notes |
| --- | --- | --- |
| Catalog data | `myradio/prompts/data/palettes.json`, `arrangements.json` (version 1.0.0, **frozen**), `palettes-1.1.0.json`, `arrangements-1.1.0.json` | Read-only package data. Files must stay directly in `data/` (the pattern `data/*.json` does not reach sub-folders). A new increment is a new pair of files plus an entry in `library.PACKS` and a new golden/catalog test update; never edit the 1.0.0 files. |
| Saved styles, favourites, repeat memory | `<data>/ext/prompts/prompts.sqlite3` | WAL mode, mode 0600, `PRAGMA user_version` = 1. Private (it holds the person's own text) but contains **no credentials or tokens**. |

Tables (migration step 1, tracked by `user_version`; a file written by a newer version is refused with "Update myradio before opening them"). The step is a list of `CREATE TABLE IF NOT EXISTS` statements run in **one transaction together with the version number**, so a new file is never left half built, and a file left half built by an older version (some tables made, the version still 0) is finished on the next start:

- `user_presets(slug PK, title, based_on, palette JSON, arrangement JSON, digest, revision, created, updated)`
- `favorites(id PK, kind, at)`
- `history(seq PK AUTOINCREMENT, at, preset_id, style_key, arrangement_id, seed, complexity, density, mode, title, fingerprint, aired)` (`aired` is reserved and never set: station events carry no prompt provenance)

Removing a saved style re-attaches the styles copied from it to its own parent (`based_on` is updated; it is not part of the content digest), so the lineage that decides the "style direction, not authenticity" note is never cut in the middle.

Backup: `persistent_files()` returns `["prompts.sqlite3"]` (the whole file is the extension's state; nothing in it is secret). The database is checkpointed (`wal_checkpoint(TRUNCATE)`) after every saved-style or favourite change and on stop, so a copy taken after `stop()` is complete. A copy taken while the station runs is complete for saved styles and favourites but may lack the newest repeat-memory rows; the backup integration should copy after stopping the service or use SQLite's online backup. Restoring a file into a fresh data directory restores everything.

## 4. Extension surface

### Operations (typed by the host; appear in the Operator and MCP catalogs automatically)

| Name | Arguments | Result |
| --- | --- | --- |
| `prompts.favorite` | `id: string` (1 to 120), `on: boolean` | `{id, favorite, kind: "style" \| "preset"}`. `id` is a style (`warm-brush-jazz`), a preset (`warm-brush-jazz/theme-and-return`) or a saved style (`user:evening-porch`). Switching off twice is fine. |
| `prompts.save_custom` | `preset: object` (at most 64 KB) | `{id: "user:<slug>", title, revision, created, digest, based_on, warnings[]}` |
| `prompts.remove_custom` | `id: string` (`user:` ids only) | `{id, removed: true}` |

Failures are `ExtensionError` with plain messages (examples in section 5). Unexpected faults are reported generically by the host.

### Routes (all authenticated, same-origin loopback, JSON; failures are HTTP 400 `{"error": "..."}`)

`GET /api/x/prompts/catalog` query options (each at most once, unknown options refused): `view` (`presets` default, `styles`, `arrangements`), `family`, `tag` (repeat; all must match), `exclude_tag` (repeat; any excludes), `energy` (`calm`, `steady`, `driving`), `q` (at most 100 characters, 8 words, all must match), `source` (`all`, `builtin`, `user`), `favorites` (`1`/`true`/`yes`/`on` or `0`/`false`/`no`/`off`), `sort` (`catalog`, `name`), `offset` (0 to 100000), `limit` (1 to 100, default 24). Response:

```json
{"view": "presets", "catalog_version": "1.1.0", "total": 1278, "offset": 0, "limit": 24, "next_offset": 24,
 "items": [ ... ], "facets": {"families": [{"id": "african", "count": 3}], "tags": [{"id": "acoustic", "count": 37}], "energy": [{"id": "calm", "count": 21}]}}
```

- preset item: `{id, title, style: {id, name}, arrangement: {id, name}, family, tags, energy, bpm: [lo, hi], meter, source: "builtin" | "user", favorite, style_favorite, heritage_note}`
- style item: `{id, name, family, tags, energy, bpm, meter, moods, leads, supports, bass, source, favorite, presets, introduced, heritage_note}` (a saved style adds `arrangement`, `based_on`, `revision`)
- arrangement item: `{id, name, source, sections: {simple, layered, intricate}, introduced, dynamics, ending}`
- `heritage_note` is `null` or the standing note that the style is a direction, not a claim of authenticity (also for a saved copy of such a style).

`GET /api/x/prompts/search?q=...&limit=10` (limit 1 to 25): `{query, total, styles: [...], user_presets: [...], arrangements: [...]}`; each item is a catalog item plus `matched` (`name`, `tags`, `instruments`, `mood`, `details`); every word must match; names rank above instruments above details.

`GET /api/x/prompts/sliders?energy=&adventurousness=&vocals=` (all three required): `{energy, adventurousness, vocals, energy_class, density, complexity, vocal_share, modes, avoid_repeating: {styles, arrangements, presets}, summary}`.

`GET /api/x/prompts/lyrics?preset=&seed=&tag_style=title|lower&form=short|full`: the scaffold record `{kind: "lyrics-scaffold", source: "scaffold", version, preset_id, seed, target_model, speech: false, tag_style, form, world, syllables_per_line, motif: {text, syllables, returns_in[]}, plan[], lyrics}`. Remembers nothing.

`POST /api/x/prompts/compose` (JSON object, body at most 128 KiB). Every key optional; unknown keys refused:

| Key | Meaning |
| --- | --- |
| `seed` | Whole number 0 to 2^63-1. Omitted: one is drawn and returned, so the result can always be reproduced. |
| `preset` | Exact preset (`palette/arrangement` or `user:<slug>`). Filters that *choose* (`styles`, `tags`, `energy`, ...) are ignored; the treatment settings below still apply. |
| `styles`, `tags`, `exclude_tags`, `family`, `energy`, `query`, `exclude_presets`, `favorites_only`, `include_user` | Choose among matching presets. `energy` is a class (`calm`, `steady`, `driving`), not a number. These are constraints and are never relaxed. |
| `sliders` | `{energy, adventurousness, vocals}`, all three, 0 to 100. Preferences: they set complexity, density, mode and the energy class, and the energy class alone is dropped if it would leave nothing among the styles you named (`relaxed: "slider-energy"`). |
| `complexity`, `density`, `mode` | Explicit treatment; beats the sliders. |
| `lyrics` | Your own words for a sung song (at most 12,000 characters). Passed through untouched; `lyrics_source: "supplied"`. |
| `scaffold` | `true` asks for a generated outline (`lyrics_source: "scaffold"`). A vocal song with neither `lyrics` nor `scaffold: false` gets an outline. `lyrics` together with `scaffold: true`, an instrumental with either, or a sung song with `scaffold: false` and no lyrics, is refused. |
| `tag_style`, `form` | Outline options. |
| `avoid_repeats` (default true), `remember` (default true) | Repeat memory. `remember: false` is a preview and leaves no trace. |

Compose rules worth knowing: a request is refused as a whole, politely, if any text in it contains a broken character (half an emoji); an **exact** `preset` keeps its own energy (`treatment.energy` is the preset's energy, a `warnings` entry says so when `energy` was also sent, and with sliders the `treatment.summary` ends "You named a calm style, so its own energy was kept."); among chosen presets, a saved style that cannot be written out at the requested complexity, density and mode is left out with a warning (and when nothing else matches, the message says every matching saved style is too long, not "no styles match"); with the repeat memory in use (`remember` true), reading the history, choosing and recording happen under `PromptStore.exclusive()`, so concurrent requests cannot pick the same preset.

Response: `{id, prompt, lyrics: {source: "none" | "supplied" | "scaffold", text, scaffold}, selection: {method: "exact" | "chosen", candidates, relaxed, avoided: {presets, styles[], arrangements[]}}, treatment: {complexity, density, mode, energy, from_sliders, summary}, provenance: {preset_id, seed, catalog_version, complexity, density, mode, fingerprint, lyrics_source, user_preset: null | {id, revision, digest}, composed_at}, warnings[], remembered}`. `prompt` is `SongPrompt.to_dict()`. `selection.relaxed` is `null` or one of `arrangements`, `styles`, `older-presets`, `last-preset`, `everything` (history only), `slider-energy`, `memory-unavailable`, or two joined with `+`.

### Status (cached snapshot; O(1), lock-free, never calls `station.status()`)

```json
{"ready": true, "error": null, "catalog_version": "1.1.0", "styles": 71, "arrangements": 18, "presets": 1278,
 "saved_styles": 0, "favorites": 0, "remembered": 0, "last_composed": null, "damaged": [], "store_available": true, "rules_ok": true}
```

`error` carries a plain sentence when the saved-styles file is unavailable; `damaged` lists saved styles that could not be read back (left in the file, never deleted). The snapshot is rebuilt after an operation or a remembered compose, including a direct `PromptService.compose` call (the service's `on_change` callback), not by `status()`.

### Doctor checks (`checks()`; cached, cheap)

`prompts.catalog` (ok), `prompts.rules` (ok, or warn if a built-in style breaks a rule), `prompts.saved-styles` (ok, or warn: damaged styles / the file is unavailable, with the hint "Check that the data folder can be written to, then restart myradio."), `prompts.repeat-memory` (ok; absent when the file is unavailable).

### Events consumed

None. The extension does not override `on_event`, so it has no mailbox thread. The repeat memory records a prompt when it is composed (a reservation), not when it airs, because `track_started`/`track_finished` events carry no prompt provenance.

### Failure isolation

If the catalog cannot load, `start()` raises and the host shows the extension as failed with its reason (the `prompts.start` check). If only the saved-styles file cannot be opened, the extension stays up in degraded mode: browsing, searching and composing work, `remember` is skipped (`remembered: false`), and favourites, saving and removing refuse with "Saved styles are not available right now...".

## 5. UI contract

Nothing is built in `web/` yet; this is what the screens must show. Copy is plain language; show server messages verbatim (they are written to be shown) and never show ids as the only label.

**Pick the sound** (first-run step and Studio > Sound)

- Style chips from `GET /catalog?view=styles&limit=100`, grouped or filtered by `family` using `facets`. Each chip shows `name`; its detail shows the first mood, `leads`, `bpm`, `meter` in words ("about 90 beats a minute, in 4/4") and, when `heritage_note` is set, that note in small text under a plain "About this style" label.
- A heart on every chip calls `prompts.favorite`; show filled/outlined and a text label ("Favourite"), never colour alone.
- Three sliders, each with a label and two end captions: **Energy** (Calm ... Lively), **How adventurous?** (Familiar ... Surprising), **Vocals** (Instrumental ... With lyrics). Under them show `summary` from `GET /sliders`, for example "Calm and spacious; surprising, richly layered writing; instrumental only".
- **Try one** composes with `remember: false` and shows the title, tempo, meter and energy, and a collapsed "What the music maker is told" section with `prompt.style`. **Use this** / **Surprise me** compose with `remember: true`. Say plainly that this makes the written instructions for a song, not the song itself; nothing is generated until the music maker runs.

**Browse** is a search box (`/search`), the family and tag filters, "Favourites only", "Mine only" (`source=user`), and a "Show more" button driven by `next_offset`.

**My styles** lists `source=user`. "Copy and change" opens an editor on a built-in or saved style (Name, tempo from and to, meter, energy, tags, lead/supporting/bass instruments, notes) and calls `prompts.save_custom` with `based_on`. Show the returned `warnings` as notes and any error in an inline alert next to the form (the message already says what to change, and starts each problem with where it is: "In the intricate harmony: ..."). The individual problems with their fields are on `PromptError.findings` for in-process callers; the operation layer turns errors into a message only (see section 8). "Remove" asks for in-page confirmation, then calls `prompts.remove_custom`.

**Sung songs.** When vocals are above 0, show **Suggested lyric outline** (from `/lyrics`) in an editable box labelled "A starting point to edit, not finished lyrics", with a switch "Use my own words". Supplied words are never altered; show `warnings` (missing section markers such as [Verse]; markers for a spoken voice) as notes.

**Empty states (each carries a next step)**

- No favourites: "You have no favourites yet. Tap the heart on a style you like and it will appear here."
- No saved styles: "You have not saved a style of your own yet. Copy one of the built-in styles and change it."
- Search with no result: "Nothing matches \"xyz\". Try one word, such as waltz, flute or late night."
- No styles match the settings: use the server text, "No styles match those settings. Loosen a filter (fewer tags, another energy) or turn off favourites only, then try again."
- Saved styles unavailable: use the check text; the built-in styles stay usable.

**Error wording the routes and operations produce today** (examples): "The tempo range must be two whole numbers between 30 and 240, lowest first." / "This style cannot be saved yet. 1. This style mixes instruments tied to different traditions: ... Keep to one tradition, or add the tag 'fusion' to show the mix is deliberate." / "You have 200 saved styles, which is the most. Remove one you no longer use, then save this one." / "There is no style called \"x\". Check the catalog for the right id." / "Choose either your own lyrics or a suggested outline, not both." / "This style is too long for the music maker. Written out in full, its longest version is N characters and the limit is 6,000. Shorten the harmony, rhythm, production, mood or arrangement notes by about M characters, then save it again." / "The production notes has a line break, extra spaces or an unusual character inside it. Write it on one line with single spaces." / "Some of the text contains a broken character, for example half of an emoji that was cut off by a length limit. Retype that part and try again." / "The harmony needs a simple, a layered and an intricate version, and is missing: intricate." / "The seed must be a whole number from 0 to 9223372036854775807." (the seed is advanced; give it a "Repeat this exact song" affordance rather than asking people to type one).

**Status line**: "71 styles ready, N saved, N favourites" from the status snapshot; the Doctor rows use `title` and `detail` as given.

## 6. Limits and bounds

| Bound | Value |
| --- | --- |
| Catalog page / search results | 1 to 100 / 1 to 25; offset at most 100,000 |
| Search | at most 100 characters and 8 words |
| Tag lists, `exclude_tags` | at most 20 each; `styles` at most 100; `exclude_presets` at most 200 |
| Compose body / lyrics / compiled style / outline | 128 KiB / 12,000 / 6,000 / 2,400 characters |
| Seed | 0 to 2^63-1 |
| Saved styles / one style's size | 200 / 24,000 characters (palette plus design, canonical JSON). The binding limit is the **written-out** style: its worst case over every seed, complexity, density and mode must fit 6,000 characters, checked when the style is saved (the longest any shipped preset can be written out is 2,281). |
| Favourites | 500 |
| Repeat memory | newest 1,000 prompts (history windows per adventurousness band: 100/200/400 presets, 2/6/12 styles, 1/3/5 designs) |
| Text fields | each at most 1,600 characters (schema); per-part instruments at most 3, playing together at most 6 |
| Echoed input in error messages | at most 60 characters per name or quoted fragment, at most 5 names ("and 35 more"); the same for text quoted from a failed rule and for the catalog loader's own messages. Tested on every save path with a 5,000-character key. |
| Instrument-matching cache | 4,096 entries |
| Threads / processes / sockets / files | none started by the extension; one SQLite connection; no network |
| Cost | catalog parse about 5 ms (once, cached); rule report about 0.25 s once per process; compose, catalog and search are milliseconds; one compose is one small insert |

## 7. Test inventory, commands and measured results

Command (all my tests): `cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_prompts*.py" -v`

Measured after the second pass: **298 tests, all passed**, 30.4 s wall under `nice -n 19 ionice -c3` (15.6 s of CPU), Python 3.12.3; also passed on 3.11.15 and 3.13.14 with `-X dev -W always::ResourceWarning` and no warning. `tests/test_extensions.py` plus `tests/test_extensions_station.py`: 45 tests, passed, about 4 s. The whole-suite runs are in the paragraphs below.

| File | Tests | What it proves |
| --- | --- | --- |
| `test_prompts.py` | 34 | The original 17 tests, each run twice: every original assertion against catalog 1.0.0 (`through="1.0.0"` / `--catalog 1.0.0`: 32, 12, 384, 6,912) and against the current catalog (71, 18, 1,278, **23,004 compiled variants, all distinct, none sampled**). |
| `test_prompts_golden.py` | 3 | Frozen id list, order, and per-preset composed-text digest of the original 384 (36 variants each). Generated once from the untouched catalog; never regenerate it. |
| `test_prompts_catalog.py` | 23 | Packs and versions, original files byte-identical, growth meets the brief, per-preset random key, schema and content rules for every new palette and design, no people named or authority claimed, heritage notes, compiled prompts never contradict themselves (one tempo, one meter, one mode), determinism across processes and hash seeds, replay and tamper detection, and the documentation listing every style and design with the right heritage marks. |
| `test_prompts_compat.py` | 19 | The shipped catalog passes (new content: zero findings; original: zero errors and exactly the seven recorded warnings); every one of the 27 rules fires on a broken style (now including a zero-length bar and announcer phrasing); legitimate look-alikes stay quiet; lexicon consistency; rule ids documented. |
| `test_prompts_sliders.py` | 13 | The table (boundaries), the documented table equals the code, all 10,201 energy x adventurousness pairs are independent, calm stays calm, vocal share and monotonicity, validation. |
| `test_prompts_lyrics.py` | 25 | Syllable counts of the whole word bank against an independent counter, rhyme spelling, hooks, no host wording anywhere, exact syllables per line, rhyme plan, one motif returning, format and tags, determinism, separation from supplied lyrics, every preset scaffolds cleanly (all 1,278 at one seed; many seeds on samples, since seeds are unbounded). |
| `test_prompts_store.py` | 34 | Saved-style validation messages (including a hostile nest or flood), namespace, compatibility errors, persistence across restart, revision and digest-keyed reproduction, caps, damaged rows, newer file, symlink, favourites, repeat memory (walk without repeats, windows, restart, bounds, concurrent composes), failure modes. |
| `test_prompts_service.py` | 23 | Catalog paging and filters, views, facets, search ranking, compose validation (32 bad requests) and catalog validation (14), slider and explicit settings, energy preference relaxing, bounded messages, replay. |
| `test_prompts_cli.py` | 11 | `check`, `lyrics`, `sliders`, `generate --scaffold-lyrics` and `--catalog`: output, exit codes, no tracebacks, no partial output, own lyrics versus outline. |
| `test_prompts_extension.py` | 35 | Registration, operations and typed arguments, routes and their 400s, status that never touches the database or the station and does not wait for a write, checks, degraded mode (including a file from a newer myradio that leaves no connection open), failed catalog, restart, status that follows direct service calls, half characters answered with a 400, and the real Station plus loopback HTTP server (401, 403, the compose size limit proved by the server's own message, operations, faulted station, playout clock undisturbed). |
| `test_prompts_regressions.py` | 78 | The second pass (section 10): zero-length bars (the hang case runs in a guarded child process), saved styles across catalog releases, the written-out length (exact to one character), concurrent requests, bounded and plain messages on 16 save paths, broken characters, tidy one-line text, announcer wording, problem lists, a named preset's energy, the heritage lineage, setup that is all or nothing, closed connections, plain file copies, records the store cannot read back, change notification, document claims, and the six behaviours the reviewers' mutants showed to be untested. |

**Do the new tests fail on the code as reviewed?** Run against a copy of the pre-fix tree (with only import stubs for the new helper names, no fixes): 56 of the 78 tests in `test_prompts_regressions.py` fail, 5 of the 7 tests added to `test_prompts_extension.py` fail, and the 2 extended rule tests in `test_prompts_compat.py` fail. The ones that pass there are controls (behaviour that must not change), tests of new helper functions, and *guards*: tests of behaviour that was already right but untested (the six surviving mutants of the first review, and the plain-file-copy promise). A guard cannot fail on correct code, so each was checked the other way, by breaking the fixed code on purpose.

**Deliberate breakages, second pass: 52 of 52 caught.** Each was applied to a scratch copy of the finished tree and the lane tests run with fail-fast; every one failed at least one test. They cover: `exclude_tags`, `query`, `tag_style`/`form`, `include_user`, the design window of the repeat memory (in the service and in the store), the arrangement rules on save, each of the three checkpoints after a change, the route size limit, a meter that allows zero or foreign digits, the loop guard (alone, and with the reader also reverted), the catalog version in a saved style's key, the written-out check on save and the skip in compose, a bound that uses the shortest mood, the memory lock, a setup that is not atomic or not repeatable, each place a refused store must close its connection, half characters in compose, in saved styles and in lyrics, the one-line rule and the odd-character rule, the title rule, the announcer phrases and the name check, problems that do not say where or repeat a sentence, findings dropped from the error, unbounded echoes (three places), a named preset's energy and summary, the heritage lineage and the re-attaching on removal, the store's refusal of a record it cannot read back, and the change callback (not called by compose, by save, called inside the lock, or not passed by the extension). Two survived the first try (a missing assertion that a failed setup closes its connection, and the loop guard that is unreachable while the reader refuses zero); each now has a test, and the run in this paragraph is the repeat. One list entry had a stale pattern and was redefined.

Other evidence (scratch, not in the repo): the 4,000-round sweep of randomly damaged save and compose requests (wrong types, control characters, half emoji, 5,000-character text, announcer phrasing): the only exception raised was the polite one, and no message passed 2,500 characters; a timing comparison (compose preview 1.2 ms to 1.3 ms median, save about 30 ms to 34 ms, the remembered compose about 10.6 ms either way, dominated by the database commit); and a differential run that compiled every shipped preset (1,278) with 10 seeds (0, 1, 7, 2^63-1 and six fixed pseudo-random ones) and all 18 treatments, 230,040 prompts, in a copy of the tree as it was before the second pass and in the finished tree: the SHA-256 over all the records, fingerprints included, is identical (`3f239a96084f293accce8410ea0aa14620489027f6a80f2bcd4ce4116712a2ca`), so no built-in prompt changed.

Whole suite (`cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_*.py"`, one process), two attempts:

1. The first did not finish inside the 900-second limit: it made no progress after 904 tests and was killed, with 4 failures before that point (by position in the run: two in the ads extension tests and two in the generation tests). Other lanes were editing those files while it ran, and every module passed when run on its own afterwards. Nothing in my lane had run yet at that point (its modules come later in the order), and no other lane imports `myradio.prompts`.
2. The second, with stack dumps armed, finished: **2,285 tests in 300.6 s, `FAILED (failures=1, skipped=1, expected failures=10)`**. The one failure is `test_ads_extension.StatusTests.test_status_does_not_wait_for_a_busy_extension` ("status() must not wait for the extension's lock"), which belongs to the ads lane, passes when its module runs alone, and was reported, not touched. The skip is the existing real-Icecast integration test (it needs `MYRADIO_TEST_ICECAST`); the ten expected failures are the soak lane's declared ones. All 213 of my tests were in that run and passed. Run one module at a time, each of the 70 modules that contain tests passed (2,278 tests; seven helper modules contain none).

Second pass, one run of the whole suite (same command, one process, run on the tree as it was just before two one-line edits: a fallback message quote cut from 120 to 60 characters, and the hang limit of the guarded child processes in my tests raised from 8 to 30 seconds; the lane and seam tests were re-run after them): **2,796 tests in 366.9 s, `OK (skipped=1, expected failures=10)`**, no failure anywhere. The skip is the real-Icecast integration test; the ten expected failures are the soak lane's declared ones. Other lanes were being edited while it ran.

Changes to original tests: five original tests asserted size constants (`32`, `12`, `384`, `6912`, and "twelve bossa presets") that cannot stay true when the catalog grows. They now take those constants from the catalog under test and run against both catalogs; no assertion was weakened, deleted or skipped, and the original behaviours (history exhaustion, corrupt data rejected, CLI errors with no partial output, and so on) run twice. Structure: the bodies moved into two mixins (`CatalogChecks`, `CommandChecks`) with `THROUGH`/size attributes and a `--catalog` prefix for the command-line runs; `CatalogTests` and `CommandTests` keep their original names and test ids and now run the bodies against the current catalog, while `LegacyCatalogTests` and `LegacyCommandTests` run the same bodies against 1.0.0; `test_same_seed_replays...` loads the matching catalog.

## 8. Requested changes to shared files

None were made. Requests:

1. **`myradio/web/**` (UI):** a Sound panel following section 5, registered with `Panel(...)` from this extension once the panel file exists (`extension.py` declares none, because it cannot create the file). The routes and operations above are all it needs.
2. **`myradio/backup.py` / backup integration:** include `ExtensionHost.backup_files()` output; for `prompts.sqlite3` copy after stopping the service or use SQLite's online backup (section 3).
3. **`PRODUCT.md`, `README.md`, `docs/HANDOFF.md`, `docs/RELEASE-STATUS.md`:** the "32 palettes x 12 designs = 384 presets, 6,912 combinations" sentences are now stale; use 71 x 18 = 1,278 presets and 23,004 combinations, and keep the "composed, not hand-written" and "no audio-quality claim" wording. `docs/PROMPTS.md` is the source.
4. **`pyproject.toml`:** nothing needed (the `data/*.json` pattern already covers the new files, and `packages.find` already includes the new modules). If a wheel is built, check that `palettes-1.1.0.json` and `arrangements-1.1.0.json` are inside it (not verified here).
5. **Generation lane / recipes extension:** call `PromptService.compose` with `remember: true` at the moment a song is committed to a render (the memory is a reservation), keep `provenance` and the whole `prompt` record with the track, treat preset ids as opaque (`user:` ids exist), and use `filters_from_sliders` for the wizard's three sliders.
6. **Doctor:** nothing; the checks merge automatically.
7. **`myradio/extensions.py`, `Context.open_db`:** when its setup fails after the connection is made, the connection is left open. Checked here: with a damaged (not-a-database) file, `PRAGMA journal_mode=WAL` fails and one connection stayed open after the failed start (Python 3.13 would also warn about it when it is collected). Close the connection before raising. The prompts store now closes any connection it is handed and cannot use, but a failure inside `open_db` never reaches it.
8. **`myradio/extensions.py`, `ExtensionError`:** it carries only a message, so `PromptError.findings` (which part of a saved style each problem is about) cannot reach the browser through an operation. If the studio should mark the fields, add an optional structured `details` to `ExtensionError` and to the error bodies of operations and routes; this lane would then pass the findings as plain dicts.

## 9. Known limitations and unverified claims

- **No audio was generated or evaluated.** Every claim about musical behaviour is a claim about text. The rules and tests prove consistency, not sound.
- **Practitioner review is missing** for all heritage styles; some descriptions (for example the raga, maqam, gamelan and kora entries) are simplified. They are labelled as style directions in the catalog, but the labels do not substitute for review.
- The lexicon recognises 115 instrument patterns; a word it does not know is a warning, not an error, so a rule can be bypassed by an unusual word. The checks read text; they do not understand it. Examples of what they cannot see: a genuinely clashing pairing written in words the lexicon does not know, or a meter claim phrased without numerals or `N-beat`.
- Seven original palettes carry `instrument.duplicate` warnings (one instrument as lead and as support). They are frozen and recorded in `tests/test_prompts_compat.py`.
- The lyric scaffold is an outline: grammatical, rhymed and counted, but uneven as poetry, with a small imagery vocabulary (five worlds) that will repeat across many songs. English only. Syllables are counted per word as a singer would, which is an estimate of timing, not a verified fit to the music. Tag spelling follows the reference lyric files and is unverified for YuE2.
- `Library.choose()` for a given seed now picks a different preset than before because the pool is larger. Replay a record by its preset id, not by choosing again.
- The repeat memory is a reservation recorded at compose time; there is no "aired" tracking and no operation to clear it (`PromptStore.forget_history()` exists but is not exposed). It records only prompts composed through `PromptService`.
- Favouriting a style is not the same as favouriting its presets; the catalog reports both flags.
- A saved style is stored as a full copy, so it never picks up later catalog corrections. That is deliberate for reproducibility. Its tempo, mood, instruments and motif for a given seed no longer depend on the catalog version; the written-out wording around them (the fixed sentences of the compiler) still belongs to the compiler release, and the record's fingerprint names the catalog version that wrote it.
- The announcer-wording check reads phrases, not meaning: a host-like sentence in other words passes, and a deliberately chosen phrase from the list ("welcome back" in a tune about coming home) is refused. The everyday words are left out of the list on purpose.
- The one-line text rule is also applied when a saved style is read back from the file. A style saved by the build before the second pass that has a line break, a doubled space or a control character in a text field is therefore set aside as damaged at the next start (it stays in the file, is listed by the doctor's saved-styles check, and is never deleted); saving it again with tidy text brings it back. Nothing was saved in the wild when this changed, and no shipped text is affected.
- A saved style written before the written-out-length check existed (or put in the file by hand) is not re-checked when the file is read; it is left out of the choice, with a warning, at any setting where it cannot be written out.
- A raw file copy of `prompts.sqlite3` while the station runs may lack the newest repeat-memory rows (section 3).
- Python 3.10 and macOS were not run (3.11, 3.12 and 3.13 on Linux were); the wheel was not built; power-loss behaviour was not tested. The all-or-nothing first-run setup is tested by a statement that is refused part way, not by cutting power.
- The CLI `generate --scaffold-lyrics` and the extension use the same generator, but the CLI does not use the repeat memory.

## 10. Second pass: findings, fixes and the tests that guard them

Two independent reviewers (A and B) found the items below. Nothing was disputed. Each finding about behaviour reproduced here before the fix (the reproduction scripts are in the scratch notes, not in the repo); the findings about gaps in the tests were confirmed by breaking the code on purpose (section 7). One example, run on the code as reviewed and on the fixed code: a style of 11,725 characters (the cap is 24,000) was saved with no warning and then failed to compose for 10 of 12 seeds even at the simplest setting, with "That combination could not be written: user:wordy-one: compiled style exceeds 6000 characters"; on the fixed code the same style is refused when it is saved, with its longest written-out size (6,993) and the limit. "Test" names a class in `tests/test_prompts_regressions.py` unless a file is given. Section 7 says which of the new tests fail on the code as reviewed and how the rest (controls, helper tests and guards for behaviour that was already right but untested) were checked.

| Finding | Fix | Test |
| --- | --- | --- |
| A, **blocker**: `line_syllables_for_meter("0/4")` loops forever; `0/4` was accepted by the meter rule, stored, and then any outline or vocal compose spun a request thread at full speed (digits from other scripts passed too) | One reader of meters, `compat.parse_meter`: ASCII digits, never zero, no leading zero, non-zero grouping digits. `line_syllables_for_meter` uses it and raises instead of looping. `PromptService.lyrics/compose` turn that refusal into a plain message, so even a hand-edited file cannot freeze a request | `ZeroLengthBarTests` (the hang case runs in a guarded child process that is killed on a timeout, so a regression fails instead of freezing the run); `test_prompts_compat.py` gets five `meter.format` examples |
| A, major: a saved style's random key included the catalog version, so the next release would change every saved style's tempo, mood and instruments | Key is `user-style:<id>#<digest>:<seed>`. Nothing was saved in the wild yet, so no stored style changes meaning | `SavedStyleSurvivesCatalogReleasesTests` (catalog 1.0.0, 1.1.0 and a made-up 1.2.0 pack: identical words and tempo; only the named catalog version differs) |
| A, major, and B, major: a saved style that passed every rule could be impossible to write out (6,000 characters), failing for some seeds and not others, with an internal message | `longest_style_chars` measures the worst case exactly; `build_user_preset` refuses with the numbers; `compose` leaves out (with a warning) a saved style that cannot be written at the requested setting, and a named one gets the same plain message | `WrittenOutLengthTests` (refusal, exact to one character, composes at the limit for every seed and setting, bound equals writing out every combination for all 71 palettes), `OlderLongStyleTests` |
| A, minor: repeat avoidance was not atomic across concurrent requests | `PromptStore.exclusive()` is held while reading the history, choosing and recording | `RepeatAvoidanceUnderThreadsTests` (six threads released together, three trials: 18 distinct presets of 18) |
| A, minor, and B, major: error text echoed unbounded caller input (5,000-character keys, long lead text) and used list syntax ("missing ['intricate'] and has unknown parts []"), "needs ... entries" and "expected an object" | `FieldsError` carries the names; `_friendly` words every case in plain language, names the part, caps names at five of 30 characters; rule messages quote at most 60 characters | `BoundedMessagesTests` (16 save paths, all under 300 characters) |
| A, minor: the cached status and the doctor's repeat-memory line went stale when other code called `compose` directly | `PromptService(on_change=...)`; the extension passes its refresh and no longer refreshes by hand | `ChangeNotificationTests`; `StatusFollowsTheServiceTests` in `test_prompts_extension.py` |
| A, minor: the first-run setup could be left half done, and then never opened again | `IF NOT EXISTS` statements and the version number in one transaction | `StoreSetupTests` (a file left half built is finished; a setup that fails part way leaves nothing) |
| A and B, minor: a refused store left its SQLite connection open | The store closes the connection it was given when it cannot use it (newer file, damaged file, unexpected fault) | `ConnectionHygieneTests`; one more in `test_prompts_extension.py` |
| A, minor: ten mutants of the code, six survived the tests (`exclude_tags`, `query`, `tag_style`/`form`, `include_user`, the design window of the repeat memory, the arrangement rules on save) | Tests added for each | `ComposeOptionsAreHonouredTests`, `RepeatMemoryWindowsTests`, `SavedArrangementsFollowTheDesignRulesTests`; the mutants were re-run (section 7) |
| B, minor: half an emoji reached the encoder and became an HTTP 500 | `broken_text` refuses it at every entry (compose requests, saved styles, titles) with a plain message; `_text` and the library's lyrics check refuse it as well | `BrokenCharacterTests`; `BrokenCharacterThroughTheHostTests` in `test_prompts_extension.py` (a 400, not a 500) |
| B, minor: "validated with the same rules" was only partly true (line breaks, escape characters, direction overrides, announcer phrasing were accepted) | One-line rule in `_text` (the shipped catalog already followed it), the same for titles; `text.speech-markup` also catches host phrases (`HOST_PHRASES`) | `TidyTextTests`, `HostWordingTests`; `test_prompts_compat.py` examples |
| B, minor: the list of problems did not say where each one was and repeated sentences | Each problem starts "In the intricate harmony: ...", identical sentences are said once; `PromptError.findings` keeps the findings | `ProblemListTests` |
| B, minor: `treatment.energy` reported the requested energy for an exact preset | Reports the preset's own energy and says so | `NamedPresetEnergyTests` |
| B, minor: the "not a claim of authenticity" note was lost on a copy of a copy, or when the tag was edited out | The note follows the lineage back to the built-in style; removing a saved style re-attaches its copies to its parent | `HeritageLineageTests` |
| Found while fixing (not in the review): `save_user_preset` raised a bare `KeyError` for a record that does not read back, which the stricter text rule makes possible for a hand-built record; and a style saved by the earlier build with a line break must be set aside, not lost | The store refuses such a record politely before touching the file; on reading, the rules apply and a failing style is set aside as damaged (kept in the file, listed by the doctor) | `RecordsTheStoreCannotReadBackTests` |
| B, minor: the lexicon has 115 patterns, not 114 | Documents say 115 | `DocumentationTests` (reads the lexicon) |
| B, minor: "every generated line has exactly that many syllables" is true of rhymed lines only | Documents say the recurring hook keeps its own length (6 or 7 syllables) | `DocumentationTests` |
| B, minor: the route size test could not fail for the reason it states; deleting the checkpoint after a change was not caught | The size test now reads the server's own message and proves a big, valid request passes the size gate; the plain-copy promise has a test | `StationHttpTests` in `test_prompts_extension.py`; `PlainCopyTests` (*guard*, mutation-checked) |

Decisions that differ a little from what the reviewers suggested, and why:

- The written-out check measures the worst case exactly instead of trying 9 or 18 compiles with sample seeds, so "saved" really means "composes for every seed". It costs under a millisecond.
- Digits in the free-text claim detectors (`N-beat`, `5/4`, `120 BPM`) still accept digits from other scripts; they only read claims, and Python's `int()` handles them, so they cannot loop. Only the definition of a valid meter was tightened.
- The announcer list for style text is smaller than the lyric guard's list on purpose (see section 2), because two of its phrases (`tune in`, `studio`) occur in shipped style text with their everyday sense.
- The heritage note follows `based_on` instead of adding a flag column, so the database format (and `PRAGMA user_version`) did not change.
