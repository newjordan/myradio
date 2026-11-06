# Lane: hosts (on-air personalities and the script composer)

Owner paths: `myradio/hosts/**`, `tests/test_hosts*.py`, `docs/lanes/hosts.md`. Loaded by `myradio.extensions.builtin_extensions()` as the extension named `hosts` (`myradio/hosts/extension.py`, `build()`).

## 1. Purpose and honest status

Hosts are **fictional characters**. This lane gives the station a cast of them and writes the **text** a host will say. It never speaks, never makes audio, never runs a model and makes no network call (a test checks every module's imports and patches the network off while composing). The generation lane turns a script into speech with Drama 3.

What exists, and how far it was checked:

| Piece | Status |
| --- | --- |
| 16 built-in hosts (`data/personas.json`), 8 script styles (`data/voice-*.json`), 960 hand-written templates, 400+ extra lines | **Real.** Loaded, validated and linted by the tests below. |
| Template composer: seeded, repetition memory, length control, effect tags, fail-closed grounding | **Real**, exercised by 330 tests (the whole hosts suite). |
| `grounding_lint`, `neutralize`, `audit_speech_text` | **Real.** Table tests, fuzz tests and hostile-input tests. |
| User-made hosts, hidden built-ins, repetition memory in SQLite | **Real** SQLite, restart-tested. |
| Extension operations and routes | **Real**: exercised through the real `ExtensionHost`, a real `Station`, and the real HTTP `Server` on `127.0.0.1` (port 0). |
| Brief to update script and the verbatim-extract reader | **Real** against briefs in the shape defined in section 4 and against the research lane's own `Brief` (`docs/lanes/research.md`), which `brief_from_research` maps. The research-shaped briefs in the tests are built by hand and checked with that lane's `check_brief`; no brief from a real research run (which needs the network) was ever used. |
| `LlmComposer` (optional writing assistant) | **Verified only against fakes.** No language model was ever called. Every scenario (tags, invented numbers, too long, exception, timeout, repeat) uses a scripted fake function. |
| Speaker marker `<|speaker:N|>` and effect tags `[laughs] [whispers] [excited]` | **As named in the product brief; not verified against a real Drama 3 build.** The effect names can be renamed or switched off in `config.json` without code changes. The speaker range 0 to 15 is an assumption. |
| Words-per-second estimates (slow 2.1, steady 2.5, brisk 2.9) | **Explicit constants, not measured** against Drama 3 audio on any hardware. `est_seconds` is an estimate. |
| Text quality | Read and revised by the author, never listened to and never reviewed by listeners. |

Not verified in this session: Python 3.10 and 3.13 (the suite passes on 3.12.3 and 3.11.15), macOS, a real listener, real speech, a real research brief from a real research run, the Team screen (it belongs to the UI lane).

## 2. Public API

Everything below is importable from `myradio.hosts` unless a module is named. Standard library only.

```python
from myradio.hosts import HostsService
service = HostsService()                 # no database: nothing is saved (tests, scripts)
script = service.compose("nova", "song_intro", {"next_title": "Paper Lantern Weather"}, seed=7)
script.text, script.speaker              # what the generation lane needs
```

### `HostsService(conn=None, *, clock=time.time, tag_map=None, library=None, timezone=None)`

Thread-safe. `conn` is an `sqlite3.Connection` (the extension passes its own file); without one nothing persists.

| Method | Returns / raises |
| --- | --- |
| `compose(persona_id, segment=None, facts=None, *, format=None, seed=None, target_seconds=None, dry_run=False, use_ai=False)` | `Script`. Exactly one of `segment` or `format`. `dry_run=True` reads but does not change the repetition memory or the counters. |
| `compose_update(persona_id, brief, facts=None, *, seed=None, target_seconds=None, dry_run=False)` | `Script` with `citations`. Raises `NothingToRead` when no item can be read. |
| `personas(include_hidden=False)`, `persona(id)` | `Persona` objects. Hidden built-in hosts still resolve by id (so a show that uses one keeps working). |
| `save_persona(raw)`, `remove_persona(id)`, `restore_persona(id)` | dicts, see section 4. |
| `catalog(include_hidden=False)`, `formats()`, `public(persona)`, `template_count(persona_id, kind)` | JSON-ready dicts used by the routes; each host's public view lists how many templates it has per kind. |
| `set_writer(complete_or_None, *, timeout=20.0)` | Connects or disconnects the optional writing assistant. |
| `live_facts(station_status, override=None)`, `note_event(event)` | Facts from the station's current state and the music seen in events. |
| `status()` | The cached summary (O(1), lock-free). `checkpoint()`, `close()` maintain the database file. |

### `Script` (frozen dataclass, `to_dict()` is JSON-ready)

| Field | Meaning |
| --- | --- |
| `text` | **Complete script for Drama 3**: begins with exactly one `<|speaker:N|>` marker, then the words, with at most two allowed `[effect]` tags. |
| `speaker` | The host's Drama speaker index `N`. |
| `role` | Shared vocabulary: `id` (station ID), `host_break`, `spot`, `update`. |
| `library_kind` | `speech` or `ad`: the kind to use with `station.import_audio` once the speech exists. |
| `plain_text` | The same words with no marker or tags (for screens and linting). |
| `segment`, `persona`, `seed`, `template` | What was asked for, who says it, the seed used (reported even when random; a seed the station makes up is always below 2^53, so it survives JSON in a browser), and the template id (`warm.song_intro.07`, or `null` for plain/assistant/format output). |
| `est_seconds`, `target_seconds` | Estimated length of `text`, and the length steered towards (`null` for sponsor messages, which are as long as the sponsor's words). |
| `facts_used` | Names of the Facts fields whose values appear in the text (for example `["next_title", "next_artist"]`). For template scripts this follows the values the chosen lines used; for writing-assistant text (`source: llm`) it is found by looking for each fact in the words themselves (whole words, any case; the time as spoken or as digits; the count as a digit or a word). The host's own name is listed only if the caller supplied `host`. |
| `warnings` | Plain-language notes: facts that were cleaned or dropped, a length that could not be met (or that a line was borrowed to avoid a repeat), a fall back to a plainer script. For `source: llm` only what was cleaned, clamped or left out on the way in; nothing is carried over from a template script that was set aside. Empty on a normal run. |
| `source` | `template`, `plain` (fell back), `llm` (assistant text accepted) or `format`. |
| `fictional`, `format` | `true` and the format id for entertainment formats. |
| `citations`, `skipped` | Updates only: see section 4. |
| `variants` | How many ready-made lines fitted the request's length (more means more variety; used by the repetition guarantee). `0` for plain, assistant and format output. |

### Other entry points

* `TemplateComposer(library=None, memory=None, config=ComposerConfig(), tag_map=None).compose(persona, segment, facts=None, *, seed=None, target_seconds=None, remember=True) -> Script`; `.prepare(persona, segment, facts=None, *, seed=None, target_seconds=None) -> Request` does the checking and cleaning that both writers share (it raises the plain `HostsError` for a request that cannot be answered honestly) and returns the cleaned facts, the seed, the target length and the warnings so far
* `grounding_lint(text, facts, persona=None, *, extra=()) -> list[str]` returns plain-language warnings, empty when every number, capitalised name and web address in `text` is supported by `facts` (a `Facts`, a raw mapping, a list of strings or a string) or by the host's own name and phrases. Speaker markers and effect tags are ignored. Only the first 6,000 characters are examined; a longer text gets one extra warning saying so (at most 12 warnings in all). `myradio.hosts.lint.has_address(text)` tells whether a text holds a web, e-mail or spoken address by the same rule.
* `Facts.from_any(raw) -> (Facts, warnings)` (a mapping, `None` or a `Facts` object; a hand-built `Facts` is cleaned exactly like a mapping, and a clean one comes back as the same object); `Facts.used_in(text, host=True)`; `neutralize(value, limit) -> Cleaned(text, flags)`; `neutralize_quote(value, limit) -> Cleaned` (the gentler cleaner used for a brief item's words, see section 4); `audit_speech_text(text, speaker, allowed_tags) -> list[str]` (empty means safe to send); `estimate_seconds(text, wps)`; `spoken_time(hour, minute)`; `number_words(n)`.
* `compose_format(composer, persona, format_id, facts=None, *, seed=None, target_seconds=None, remember=True)`; `compose_update(composer, persona, brief, facts=None, *, seed=None, target_seconds=None, remember=True)`; `parse_brief(raw) -> Brief`; `brief_from_research(raw)` (maps the research lane's `Brief`, see section 4); `verbatim_extract(text, max_words=45)`.
* `LlmComposer(complete, fallback, *, timeout=20.0, max_inflight=1, tolerance=0.35).compose(persona, segment, facts=None, *, seed=None, target_seconds=None, remember=True)` and `.prompts(persona, segment, facts, target) -> (system, user)`.

Errors: `HostsError` (subclass of `myradio.extensions.ExtensionError`, so the message reaches operators and agents unchanged) and `NothingToRead(HostsError)`. All messages are plain language.

Other lanes call the composer through the seam, not by importing it: `ctx.station.action("hosts.compose", {...})` returns the `Script` fields at the top level (`{"ok": true, "operation": "hosts.compose", "text": ..., "speaker": ..., ...}`).

## 3. Persistence

All state is in the extension's own directory `<data>/ext/hosts/` (created `0700` by the host).

| File | Contents | Backups | Secret |
| --- | --- | --- | --- |
| `hosts.sqlite3` (mode `0600`, WAL) | `personas(id, data JSON, created_at, updated_at)` for the operator's own hosts; `hidden(id, at)` for hidden built-ins; `memory(seq, persona, kind, units JSON, digest, at)` for repetition memory. `PRAGMA user_version` tracks the schema (currently 1; `migrate()` refuses a newer file). | Yes (`persistent_files()` lists it). Saving a host checkpoints the log, so a copy of this one file is complete. | No. The memory table holds template ids and a 16-character hash of each script, never a title or any station fact. |
| `config.json` (optional, written by the operator) | `{"timezone": "Europe/London", "event_tags": {"laughs": "laugh", "excited": null}}`. `timezone` is used for the local time in live previews; `event_tags` renames the effect tags to what the speech model expects, or switches one off with `null`. | Yes, if present. | No. |

Built-in hosts and templates ship as package data and are never written to the database. A damaged or unreadable saved host is skipped and counted (shown by the doctor check), never fatal. Writes to the repetition memory are best effort: a full disk never stops a script being written. The file is opened with `synchronous=NORMAL` (safe against corruption in WAL mode); a power cut can lose the last few memory records, which only means a template may repeat once.

## 4. Extension surface

### Vocabulary, as used here

* **Kinds of script** (`segment`): `station_id`, `song_intro`, `song_back_announce`, `time_check`, `sign_on`, `sign_off`, `spot_wrapper`, `update_intro`, `update_outro`, `filler`. Whole sourced updates are `update` (see below).
* **Now and next.** In Facts, `now_*` is the song that has just played (or is playing); `next_*` is the song about to be heard. `song_intro` introduces `next_*`; `song_back_announce` names `now_*`.
* Roles map to the shared vocabulary: `station_id` is `id`; `spot_wrapper` is `spot` (library kind `ad`); `update_*` and `update` are `update`; every other kind is `host_break`. Songs (`song`, YuE2) are not this lane's.

| kind | role | library kind | needs from the caller | default seconds (terse to chatty host) | accepted seconds | may be stretched with extra lines |
| --- | --- | --- | --- | --- | --- | --- |
| `station_id` | `id` | `speech` | `station` | 2.5 to 5 | 2 to 7 | no |
| `song_intro` | `host_break` | `speech` | nothing | 4 to 18 | 3 to 30 | yes |
| `song_back_announce` | `host_break` | `speech` | nothing | 4 to 16 | 3 to 25 | yes |
| `time_check` | `host_break` | `speech` | `local_time` | 2.5 to 5 | 2 to 7 | no |
| `sign_on` | `host_break` | `speech` | nothing | 6 to 22 | 4 to 30 | yes |
| `sign_off` | `host_break` | `speech` | nothing | 6 to 22 | 4 to 30 | yes |
| `spot_wrapper` | `spot` | `ad` | `sponsor`, `sponsor_copy` | as long as the sponsor's words | 4 to 40 | no |
| `update_intro` | `update` | `speech` | nothing | 3.5 to 7 | 3 to 10 | no |
| `update_outro` | `update` | `speech` | nothing | 3 to 6 | 3 to 9 | no |
| `filler` | `host_break` | `speech` | nothing | 5 to 20 | 3 to 30 | yes |
| `update` (whole bulletin) | `update` | `speech` | a brief | 20 to 75 | 8 to 180 | n/a |

A host's talkativeness (`verbosity` low and high, 1 to 5) picks where in the default range one script lands.

### Facts

`Facts.from_any(raw)` accepts a mapping, `None` or a `Facts` object. Unknown keys are ignored with a note. Every text value is neutralized (below), capped, and dropped with a plain-language warning if nothing speakable is left. A `Facts` object built by hand is cleaned the same way (an effect tag or a speaker marker in one of its values is removed, a 1 MB value is cut to its cap before it is examined); an object that was already clean comes back unchanged.

| Field | Type | Cap | Notes |
| --- | --- | --- | --- |
| `station`, `host` | text | 80, 60 | `host` defaults to the persona's name. |
| `tagline` | text | 160 | Spoken as its own sentence. |
| `now_title`, `now_artist`, `next_title`, `next_artist` | text | 120 each | |
| `local_time` | `"HH:MM"` 24-hour | | Spoken in words ("nine oh seven at night", "a quarter to ten in the morning"). Invalid values are dropped. |
| `daypart` | `overnight`, `morning`, `afternoon`, `evening`, `night` | | Derived from `local_time` when not given; a few aliases such as "late night" are accepted. Never guessed otherwise. |
| `weekday` | name, or 1 (Monday) to 7 | | |
| `notes` | list of text | 6 notes, 240 each | Operator notes, spoken whole. A note that mentions a topic the host avoids is left out (see Topics to avoid). |
| `sponsor`, `sponsor_copy` | text | 80, 600 | `spot_wrapper` only. |
| `topic`, `item_count`, `sources` | text, 0 to 99, list of up to 8 names (80 each) | | Updates. A topic the host avoids is left out, with a note. |

### Neutralizing (every fact, every host field, every assistant reply)

`neutralize` applies Unicode NFKC, removes control and invisible characters (including bidi controls and the tag block), removes `<|...|>` markers, `<...>` tags and `[...]` effects (repeating until nothing is left to reveal), replaces stray `< > [ ] { } | \ \` * _ ~ ^` and parentheses with spaces, collapses runs of punctuation, cuts at a word boundary, and is idempotent. A title that is only markup becomes empty and the fact is dropped. Template syntax in a value (`{station}`) is never expanded: values are inserted in a single pass. Look-alike characters (full-width `＜｜speaker:3｜＞`, `［laughs］`) are folded to their plain form first, so they are removed like the real thing.

`neutralize_quote` is used for the words of a brief item, which are read back as somebody else's words and so must not lose any. It removes speaker markers, HTML-style tags, stage directions in brackets (`[laughs]`, `[applause]`, `[inaudible]` and the like) and control characters; it keeps the words inside any other square brackets ("would [not] help" is read "would not help", with a note) and keeps parentheses. A `<` or `>` that is not part of a tag ("5 < 7") cannot be read without changing the meaning, so an item that holds one is left out (reason `symbols`).

### Fail-closed chain

1. The chosen template is filled, effect tags the host may not use are removed, and the result must pass `grounding_lint` (and, for sponsor messages, still say it is sponsored) and `audit_speech_text`. The audit is the last line of defence: even if cleaning ever failed, a script with a second speaker marker or any stray voice code never leaves the package.
2. If it fails, the **plain script** for that kind is used (for example `Up next, {next_title} by {next_artist}.`) and `warnings` says so.
3. If that fails too, a fixed sentence with no facts is used (`Here is the next song.`). For `spot_wrapper` there is no step 3: the call raises `HostsError`, because a sponsor's words are never dropped.

### Anti-repetition

Per host and kind of script, the memory remembers the last 30 compositions (template id, extra lines used, a hash of the text). A template is not reused for the next 8 compositions of that kind. The choice is made in this order:

1. Among the templates that fit the request (their facts are present, they can reach the requested length within the tolerance, and for `song_intro`, `song_back_announce` and `update_intro` they use the song or topic when the caller supplied it), take one that was not used in the last 8 compositions.
2. If every one of those was used lately and fewer than 9 fit, **borrow the nearest-length templates that were not used lately** rather than say a line again. A borrowed line may miss the length a little, and the script's `warnings` say why ("The closer song intro lines were used a moment ago, and repeating one would be worse."). This is rare: 17 scripts in a measured 4,320 (30 compositions for each of the 16 hosts and 9 kinds, at the default lengths).
3. If every template that can be used at all was used inside the window (a host with very few facts), the window shrinks to the lines there are: the one used longest ago is taken, so each usable line is used once before any is said again.

`Script.variants` reports how many templates fitted the length (step 1's pool), so callers can see how much room there was. With very few facts only a few lines fit (a song intro with no song named has three or four), so a line can come round again within 8 scripts; no line ever comes twice in a row while a second one can be used. Extra lines used in the last 2 scripts come last in line. Previews (`dry_run`) read the memory and never change it. Seeds make choices repeatable only for the same memory state.

**Seeds.** A seed the station makes up is a whole number below 2^53, so it survives `JSON.parse` in a browser unchanged and a script shown in the Team screen can be repeated from the seed the screen sends back. Seeds a caller supplies may be any whole number from 0 to 2^63 - 1, but a JavaScript client cannot send one above 9007199254740991 exactly. Do not pass a hosts seed to the generation lane: its seeds stop at 4294967295 and are a separate thing.

### Length control

`est_seconds = words / wps + 0.30 s per sentence + 0.12 s per comma, semicolon or colon + 0.40 s per effect tag` (digits count for the words they are read as). `wps` comes from the host's pace: slow 2.1, steady 2.5, brisk 2.9. The composer adds up line lengths and chooses which of a template's optional lines (and, for kinds marked above, up to 6 extra lines) land nearest the target; the allowed miss is `max(1.5 s, 15%)`. An effect tag the host may not use (or that `config.json` switches off) is removed from the finished script, so the pause it would have taken is not counted either when lines are chosen. A target outside the accepted range is clamped with a note. A length the host cannot reach (a minimalist asked for 30 seconds) is reported honestly in `warnings`; nothing is padded with invented facts.

### Operations (registered as `hosts.<name>`, validated by the host before a handler runs)

| Operation | Arguments | Result (merged next to `ok` and `operation`) |
| --- | --- | --- |
| `hosts.save` | `persona` (object, required) | `{"persona": <public host>, "created": bool, "warnings": [...]}`. Creates or updates one of the operator's own hosts. Without an `id` it always creates (a unique id is derived from the name); with an `id` it updates that host, or creates a host with that id if none exists yet. Built-in ids and duplicate names are refused. |
| `hosts.remove` | `id` (string) | `{"id", "removed", "hidden", "message"}`. Your own host is deleted (and its memory). **A built-in host cannot be removed: it is hidden** (`removed: false, hidden: true`). Unknown id is an error. |
| `hosts.restore` | `id` | `{"id", "restored", "message"}`. Un-hides a built-in host (idempotent). |
| `hosts.list` | `include_hidden` (boolean, optional) | `{"hosts": [{id, name, role, blurb, voice, speaker, builtin, hidden}], "count": n}` |
| `hosts.compose` | `persona` (string); `segment` (one of the kinds) **or** `format`; optional `facts` (object), `seed` (integer, 0 or more), `target_seconds` (number), `dry_run`, `use_ai` | A `Script` as a flat object (fields in section 2). |
| `hosts.compose_update` | `persona`, `brief` (object: the research lane's `Brief` as it is, or the plain shape under Briefs and updates); optional `facts`, `seed`, `target_seconds`, `dry_run` | A `Script` with `citations` and `skipped`. |

The persona object for `hosts.save`: `name` (1 to 40), `role` (to 60), `blurb` (to 200), `traits` (to 5 words), `speaker` (0 to 15, the Drama voice slot), `pace` (`slow`/`steady`/`brisk`), `verbosity` (`[low, high]`, each 1 to 5), `language` (`en` only), `allowed_tags` (a list, or one name, from `laughs`, `whispers`, `excited`), `prohibited_topics` (to 12), `voice` (a script style), `lexicon` (optional lists, up to 8 phrases each, for `audience`, `sign_off`, `vibe`, `place`, `aside`; missing ones come from the style), `tone` (optional `#rrggbb` for the avatar), `id` (optional slug `[a-z0-9][a-z0-9-]{0,62}`). A text that suggests a real person or a copied voice ("sounds like", "voice of", "clone", "impersonate", "in the style of", "real person") is refused. This is a coarse guard, not a guarantee. A value of the wrong kind in any field (a list for `pace`, a number for `allowed_tags`, and so on) is refused with the field's own plain sentence ("Pace must be slow, steady or brisk."), never with the generic failure message.

### The 16 built-in hosts and 8 script styles

Every host draws on the 12 or more templates per kind of its **script style**, then personalizes them with its own phrases (`audience`, `sign_off`, `vibe`, `place`, `aside`), pace, talkativeness and allowed effects. Hosts that share a style differ in wording choices and length habits, not in sentence structure. Every effect a built-in host is allowed to use is written into lines of its style (a test checks the data, and another that each host really says each of its effects within a few hundred scripts); effects are still uncommon, about one script in ten to fifty, as a sound effect should be.

| id | name | role | style | speaker | pace | talks (1-5) | effects |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `nova` | Nova | Late-night host | warm | 1 | slow | 2 to 4 | whispers |
| `dex` | Dex | Morning host | bright | 2 | brisk | 2 to 4 | excited, laughs |
| `marlowe` | Marlowe | Anchor for updates | anchor | 3 | steady | 1 to 3 | none |
| `professor` | Professor Static | Dry-wit curator | dry | 4 | steady | 2 to 3 | none |
| `juniper` | Juniper | Community-radio volunteer | community | 5 | steady | 2 to 4 | laughs |
| `sable` | Sable | Jazz-café host | smooth | 6 | slow | 2 to 4 | whispers |
| `wren` | Wren | Maker-shop host | workshop | 7 | steady | 2 to 3 | laughs |
| `quill` | Quill | Deep-focus minimalist | hush | 8 | slow | 1 to 2 | none |
| `hazel` | Hazel | Sunday-morning gardener | warm | 9 | slow | 2 to 4 | laughs |
| `dusty` | Dusty | Overnight road host | smooth | 10 | steady | 2 to 4 | laughs |
| `rae` | Rae | Friday countdown host | bright | 11 | brisk | 2 to 4 | excited, laughs |
| `crate` | Crate | Record-collector curator | dry | 12 | steady | 2 to 3 | none |
| `corin` | Corin | Explainer anchor | anchor | 13 | brisk | 1 to 3 | none |
| `comet` | Comet | Space-age announcer | bright | 14 | steady | 2 to 4 | excited |
| `fuzz` | Uncle Fuzz | Friendly storyteller | community | 15 | steady | 3 to 5 | laughs |
| `lull` | Lull | Sleep-hours whisperer | hush | 0 | slow | 1 to 2 | whispers |

Styles: `warm` (Warm and unhurried), `bright` (Bright and quick), `dry` (Dry and deadpan), `anchor` (Clear and steady), `community` (Friendly community radio), `smooth` (Smooth and low), `workshop` (Practical and hands-on), `hush` (Hushed and minimal). The ids `nova`, `dex`, `marlowe` and `professor` match the design preview's cast. `speaker` is a Drama speaker index; mapping an index to an actual voice is the generation lane's job and is unverified here.

### Routes (all under `/api/x/hosts`, authenticated and origin-checked by the host)

`GET /personas?hidden=1` returns everything a Team screen needs:

```json
{"personas": [{"id": "nova", "name": "Nova", "role": "Late-night host", "blurb": "...", "traits": ["Warm", "Calm", "Curious"],
               "speaker": 1, "allowed_tags": ["whispers"], "pace": "slow", "wps": 2.1, "verbosity": [2, 4], "language": "en",
               "prohibited_topics": ["politics"], "voice": "warm", "voice_label": "Warm and unhurried",
               "lexicon": {"audience": ["night owls"], "sign_off": [], "vibe": [], "place": [], "aside": []},
               "tone": "#d8edac", "builtin": true, "hidden": false, "fictional": true,
               "templates": {"station_id": 12, "song_intro": 12, "song_back_announce": 12, "time_check": 12, "sign_on": 12,
                             "sign_off": 12, "spot_wrapper": 12, "update_intro": 12, "update_outro": 12, "filler": 12}}],
 "voices": [{"id": "warm", "label": "Warm and unhurried", "summary": "..."}],
 "segments": [{"id": "song_intro", "role": "host_break", "library_kind": "speech", "label": "Song intro", "summary": "...",
               "requires": [], "default_seconds": [4, 18], "min_seconds": 3, "max_seconds": 30}],
 "paces": {"slow": 2.1, "steady": 2.5, "brisk": 2.9}, "event_tags": ["laughs", "whispers", "excited"], "languages": ["en"],
 "limits": {"custom_hosts": 60, "name": 40, "role": 60, "blurb": 200, "traits": 5, "trait": 24, "topics": 12, "topic": 40,
            "phrases": 8, "phrase": 60, "speaker_max": 15},
 "lexicon_keys": ["audience", "sign_off", "vibe", "place", "aside"], "note": "Hosts are made-up characters. ..."}
```

`GET /formats` returns `{"formats": [{"id", "label", "summary", "disclosure", "fictional": true, "uses": [...], "default_seconds", "min_seconds", "max_seconds"}], "note": ...}`.

`POST /preview` (JSON body up to 64 KB) is a **dry run that returns text only**:

```json
{"persona": "nova", "segment": "song_intro", "facts": {"next_title": "Paper Lantern Weather"}, "seed": 5,
 "target_seconds": 10, "live": false, "use_ai": false}
```

`segment` may be a kind or `"update"` (with a `brief`); or send `"format"` instead of `segment`. `"live": true` fills the facts from the station's current state (station name, tagline, the song playing or just played, the next music in the queue, the local time) and lets `facts` override. The reply is the `Script` fields plus `"preview": true, "audio": false, "note": "Text only. No audio was made."`. A preview never changes the memory or the counters. Bad requests are `400` with `{"error": "..."}` in plain language.

### Status (`station.status()["extensions"]["hosts"]["status"]`)

O(1) and lock-free; it never calls `station.status()`.

```json
{"ready": true, "personas": {"builtin": 16, "custom": 0, "hidden": 0, "visible": 16}, "segments": 10, "formats": 3, "voices": 8,
 "templates": {"total": 960, "fewest_per_kind": 12, "required": 12}, "writer": "templates",
 "composed": {"total": 0, "template": 0, "plain": 0, "assistant": 0, "format": 0, "update": 0, "last_at": null},
 "unreadable_saved_hosts": 0, "storage_problems": 0}
```

`composed` counts real (non-dry-run) compositions since the service started.

### Doctor checks (merged by the doctor lane as `hosts.<id>`)

`cast` ("On-air hosts": ok, "16 built-in hosts and N of yours are ready."), `writing` ("Script writing": ok; says whether a writing assistant is connected), `storage` ("Saved hosts": ok, or warn "N saved host(s) could not be read and were left out" with the hint "Check free disk space. Your built-in hosts are not affected."), and one warn per problem found in `config.json` ("Fix or remove the setting in the hosts config.json file."): the first is `config`, the next `config-2`, `config-3` and so on, because the doctor keeps only the first check of any id. At most 8 are shown; the last says how many more were ignored. A damaged script library makes the extension fail to start, which the host reports as `hosts.start`.

### Events consumed

`track_started` events whose `track.kind` is `music` are kept (title and artist, last 8) so a back announcement can name the song that just played even while a spoken break is current. `transport`, `track_finished` and everything else are ignored.

### Briefs and updates (how research feeds the composer)

Two shapes are accepted by `hosts.compose_update` / `compose_update`. The hosts lane owns the mapping from the research lane's shape to this one (`brief_from_research`), so the research lane keeps handing over its `Brief` as it is.

**1. The research lane's own `Brief`** (`docs/lanes/research.md`). Only the `*_speech` fields are used, as that lane asks. Per item: `extract_speech` is the text (or `headline_speech` when the source gave only a title, or when the extract was cut off before its first whole sentence); `source_name_speech` is the source; `url`, `published_at` and `id` carry over. `kind` maps one to one: `reported` to `reported`, `commentary` to `commentary`, `announcement` to `announcement`, and **`unverified` to `uncertain`**, which is introduced as not confirmed and never as plain reporting. A kind this lane does not know is read as `uncertain` too. When `extract_truncated` is true and the extract does not end like a sentence, the unfinished last sentence is not read. At brief level, `researcher_name` is the topic unless `topic` is given, `created_at` is `generated_at`, and a brief whose `outcome` is not `ready` has nothing to read. This lane does not check `airable`, `review` or `fresh_until_ts`: choosing a brief that may air is the director's job (`claim_airable`).

**2. The plain shape**:

```json
{"id": "brief-1", "topic": "Backyard astronomy", "generated_at": "2026-11-05T07:42:00+00:00",
 "items": [{"id": "i1", "text": "A bright meteor was reported ... Observers called it unusually slow.", "source": "Example Astronomy Club newsletter",
            "url": "https://example.org/meteor", "published_at": "2026-11-05T06:10:00+00:00", "kind": "reported"}]}
```

`kind` is `reported` (the default when none is given), `commentary`, `announcement` or `uncertain` (`unverified` is accepted as another word for it). **A kind that is given but not recognised is read as `uncertain`, with a note**: a claim of unknown reliability never gets the strongest introduction. Times are epoch seconds or ISO-8601 with an offset, read by one explicit rule on every Python version (`2026-11-05T09:30:00+02:00`, `...+0200`, `...Z`, with or without seconds or a fraction; a time with no offset is dropped with a note). Up to 20 items; text is kept to 1,200 characters.

The rule, in order:

1. Everything is cleaned. Labels (topic, source, ids) go through `neutralize`; an item's text goes through `neutralize_quote` (section 4, Neutralizing), which keeps its words. An item is **never read** when it has no `source` (a bulletin must be attributable), when its source is only a web address or contains one (`example.org`, `https://...`, `user@example.org`; give the source a name to include it), when it has no words, when its text holds a `<` or `>` that cannot be read as written (`symbols`), when it repeats another item, when its text or its source's name mentions a topic the host avoids, when its first sentence holds a web address of any kind (`.gov`, `.edu`, `.garden`, `.ai`, e-mail, "dot com" and so on), when its first sentence is over 60 words, or when its first sentence is only an abbreviation or a number ("Sen.", "12."). Each is listed in `skipped` with a reason (`no_source`, `source_link`, `no_text`, `symbols`, `duplicate`, `topic`, `link`, `too_long`, `fragment`, `time`, `extra`, `not_an_item`) and a plain sentence.
2. What is read for an item is a **verbatim extract**: its first sentence(s), copied unchanged from the start of the cleaned text, stopping at a sentence end (45 words), before any sentence that holds a web address, or at the first sentence if that alone is longer. Nothing is paraphrased, shortened mid-sentence or added. Abbreviations do not end a sentence: titles ("Dr.", "Sen.", "Gov.", "Lt. Col."), months and weekdays ("Nov.", "Sat."), company words ("Inc.", "Corp.", "Bros."), "No. 5", "Fig. 3", initials ("J. K.", "D.C."), decimals, and any short capitalised word straight before a number ("Nov. 5"); in lower case such words do end a sentence ("He sat."). A first reading of fewer than four words is joined to the next sentence when there is one, so a lone title or number is never read by itself; one that ends like an abbreviation and cannot be joined is not read. Only the first 4,000 characters of an item are looked at.
3. Each extract follows a short attribution that names the source, worded for the kind: "According to {source}:", "{source} reports:", "{source} announced:", "Commentary from {source}:", "This is not confirmed. {source} says:", and so on. Web addresses are never spoken; they travel in `citations`. When an extract does not end like a sentence (a headline, a quotation that stops at a closing quote, a snippet), the host ends it with a full stop of its own before its next words, so the source's words never run on into the host's. The citation keeps the extract exactly as found.
4. Items are taken in the brief's order while they fit the target (default 20 to 75 s by the host's talkativeness, 11 s reserved for the opening and closing). Every item that fits is read at least by its first sentence; spare time lets earlier items run on to their fuller extract. The opening (`update_intro`) and closing (`update_outro`) come from the host's own templates, told the topic (unless the host avoids it), the number of items actually read and their source names ("one item" and "three items" agree with the wording around them). If the whole still runs past 125% of the target, or past the 3,000 characters the speech step accepts, the time given to items is reduced by 15% and the choice is made again (up to 12 times), so later items drop out first (`time`) and the update is never too long for the speech step. A note says so when the character limit was the reason.
5. If nothing can be read, **no update is made**: `NothingToRead` ("There is nothing to read from this brief, so no update was made. Music keeps playing.").
6. Before returning, the whole text is linted against the brief itself and each extract is checked to appear exactly as written. If either check fails, nothing is returned, with a plain sentence ("This update could not be checked against its sources, so none was made. Try again, or choose a different host or length.").

`citations` lists, for every item read, in speaking order: `position`, `brief_id`, `item_id`, `source`, `url`, `published_at` (ISO in UTC), `kind` (`reported`, `commentary`, `announcement` or `uncertain`), `extract` (the exact words spoken, after cleaning) and `words`. The text never states freshness ("new", "today", "latest"); that judgement belongs to the research lane.

**Topics to avoid.** A host's `prohibited_topics` are matched by their first letters: a word of five letters or more matches every word that starts with all but its last two letters (never fewer than four; a long word ending in "ation" loses three), a shorter word matches itself. So "politics" avoids "Politicians" and "political" (not "polite" or "policy"), "religion" avoids "religious", "speculation" avoids "speculate", and "medical advice" avoids "medication advice". Words with no common start are not matched ("election" needs its own entry). A topic with symbols in it is matched as written. The check is applied to operator notes, the topic of an update, brief items and the names of their sources, and to what a writing assistant returns. It is not applied to song titles, the station's tagline or a sponsor's copy.

### Entertainment formats

`GET /formats`: `word-salad-poem` (a surreal poem built from word lists, uses no facts), `station-almanac` (playful sayings mixed with facts the station really has: name, motto, host, songs, time, weekday, a note) and `airwave-forecast` (a pretend weather report for the music). Each is `fictional: true` and its text **starts with a fixed one-line plain disclosure**: "This next bit is a made-up poem, and none of it is real." / "This almanac is playful, so the sayings are made up and only the facts are real." / "This forecast is made up for fun and is not a real weather report." They pass the same grounding check (their own word lists are allowed vocabulary; they contain no number, name or address).

### The optional writing assistant (`LlmComposer`)

Caller-injected `complete(system, user) -> str`. It runs in one daemon thread per call, never more than one at a time (a stuck call makes later calls fall back immediately, so threads cannot pile up), with a default timeout of 20 s. Instructions are strict and facts are passed as labelled one-line data. The reply is accepted only if it is text of at most 1,600 characters with **no** markup or hidden characters, within 35% (plus 1.5 s) of the target length, at least three words, passes `grounding_lint`, mentions no avoided topic and is not a repeat of a recent script. Otherwise the template script for the same seed is returned with a plain warning (for example "The writing assistant's text was not used (it included a detail that is not in your facts), so the built-in script was used."). Sponsor messages never go through it. The warning never contains the assistant's error text. If the machine cannot start the thread for a call, or the function fails in any way, the built-in script is used and the writing assistant is free again for the next call (a failure never leaves it marked busy). Notes about a topic the host avoids are not sent to it. Facts are cleaned again before they go into the instructions (`prompts()` included), whatever the caller built. When its text is accepted the result describes **that text**: `facts_used` lists the facts the words really contain, `variants` is 0, and `warnings` holds only what was cleaned, clamped or left out on the way in (nothing is carried over from the template script that was set aside). Wiring a real model into `complete` is the caller's job and is not part of this lane.

## 5. UI contract (Team view)

The Team screen is built by the UI lane from these routes and operations. Words below are the intended on-screen copy.

* **Heading and note.** "Your on-air team." Under it: "Hosts are made-up characters, not real people. Scripts are built from facts the station has, like your station name and song titles."
* **Host card** (one per visible host): avatar = first letter on `tone`, name, role, blurb, trait chips, "On shift: ..." or "Not on a show yet." (from the shows lane), and **Hear a sample**: `POST /preview` with `{"persona": id, "segment": "song_intro", "live": true}`. Show `plain_text`, "about N seconds" from `est_seconds`, any `warnings`, and "Text only. No audio is made here." The audio sample stays disabled with "Needs voices set up." until the generation lane reports the voice ready.
* **Create a host**: Name, Role, "Describe their personality" (blurb), "How they sound" (script style: the `voices` labels and summaries), Pace (Slow, Steady, Brisk), "How chatty" (two numbers 1 to 5), "Sound effects" (checkboxes from `event_tags`; none by default), "Topics to avoid" (with the hint "Words that start like these are avoided too: 'politics' also avoids 'political'. List other words, like 'election', separately."), optional "Their own phrases". Save with `hosts.save`; show `warnings` after saving. Use the API's error sentences as they are.
* **Built-in versus yours.** Your hosts have Edit and Remove ("Remove Rhea? This cannot be undone."). Built-in hosts have Hide ("Nova is built in, so it is hidden instead of deleted. You can bring it back at any time.") and a "Show hidden hosts" switch with Bring back. Hiding a host that a show uses does not stop the show.
* **Empty states.** No hosts of your own: "No hosts of your own yet. Create one, or use the built-in team." Nothing visible: "All built-in hosts are hidden. Show hidden hosts to bring one back."
* **Formats.** A "Just for fun" row lists `/formats` with its `label`, `summary` and the sentence "Every piece starts by saying it is made up."
* **Update preview** (Discover view): `POST /preview` with `segment: "update"` and the brief. List `citations` as "Source, time, link" (a citation's `kind` is `reported`, `commentary`, `announcement` or `uncertain`; show `uncertain` as "Not confirmed") and `skipped` as the plain sentences returned. If the call returns the "nothing to read" sentence, show it with "Music keeps playing."
* **Error wording.** Messages are written to be shown as they are, for example: "Add the station's name so the host can say it.", "There is no host with that id. Open the Team page to see the available hosts.", "Your host could not be saved. Check that the disk has room, then try again." Never show a stack trace or an exception name.

## 6. Limits and bounds

| Thing | Bound |
| --- | --- |
| Fact text | station 80, host 60, tagline 160, titles and artists 120, sponsor 80, sponsor words 600, topic 80, note 240 (6 notes), source name 80 (8 sources); anything longer is cut at a word boundary with a note. A 5 MB string is cut before it is examined. |
| Work per compose | About 1 ms to 2 ms; 12 templates, at most 1,024 line combinations each; lists are scanned only up to a small multiple of their cap. |
| Hosts | 60 of your own; 16 built in; 12 topics, 5 traits, 8 phrases per list. |
| Repetition memory | 30 records per host and kind (in memory and in the database); the table is also capped at 20,000 rows at startup. No facts are stored. |
| Effect tags | At most 2 per script, only the host's allowed ones. |
| Briefs | 20 items, 1,200 characters each (4,000 are ever looked at), 45-word extracts, a 60-word first sentence at most, an update of at most 180 s and 3,000 characters. |
| Writing assistant | One call in flight, 20 s default timeout, 1,600 characters accepted, one daemon thread at most. |
| Caches | Grounding vocabularies (256 entries), line-length estimates (4,096 entries) and the patterns for topics to avoid (512 entries), all small and keyed by content. |
| Lint | The first 6,000 characters of a text are examined; a longer text gets a note saying so. |
| Seeds | Made up: below 2^53. Accepted: 0 to 2^63 - 1. |
| Threads | None created by the composer. The extension host gives the extension one mailbox thread for events (`myradio-ext-hosts`), joined on stop. |
| Request body | `POST /preview` 64 KB; operation object arguments 64 KB (host-enforced). |
| Script text | At most 3,000 characters (the speech step refuses more; the speaker marker counts); the audit refuses anything longer. Only an update can come near it. |

## 7. Test inventory, commands and measured results

Command (runs every test file this lane owns):

```
cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_hosts*.py" -v
```

Measured in this session (after the review fix pass): **330 tests, 0 failures, 0 errors, 0 skipped**, about 45 s on Python 3.12.3 and about 43 s on Python 3.11.15 (`/home/frosty40/.local/bin/python3.11`), both under `nice`. Python 3.10 and 3.13 were not available and were not run. The seam tests `tests/test_extensions.py` and `tests/test_extensions_station.py` were run alongside and are green. Before the fix pass the suite had 253 tests; every new test was also run against the code as it was before the fixes (all fail there except the ones that guard behaviour that was already right), and 28 small deliberate breaks of the fixed code (a changed line, a removed check) were each caught by the test written for them, while a change that alters nothing was not flagged.

| File | Tests | Covers |
| --- | --- | --- |
| `test_hosts_safety.py` | 42 | neutralizer attacks (speaker markers, effect tags, full-width look-alikes checked for any surviving look-alike markup as well as ASCII, zero-width and bidi characters, nested markers, template syntax, tag-block smuggling), 1,500 random nasty strings (idempotent, bounded, clean), multi-megabyte inputs under 0.5 s, `audit_speech_text` accept and reject table, `Facts` (None, non-strings, nested values, NaN, huge lists, time/day/count validation, status adapter, **hand-built `Facts` with effect tags, markers, wrong types and 1 MB values**), spoken times (a table, and **every minute of the day read back into the clock time**), every host and kind under attack facts, the quote cleaner (clean, bounded and idempotent over random nasty strings; real tags removed, comparisons kept), and the last-line audit shown to stop voice codes even when cleaning is bypassed |
| `test_hosts_lint.py` | 26 | digits and spelled numbers (years, ordinals, "No. 1", thousands separators, dozens, decades), dates, hyphenated names, accents and possessives, sentence starts, addresses of any ending (`.gov`, `.edu`, `.garden`, `.ai`, e-mail, spoken "dot garden") and what is not an address (`e.g.`, `a.m.`, `3.5`, a missing space after a full stop), the full stop after an address, the 6,000-character note, the time grounded from `local_time`, persona grounding, false-positive guards ("one of my favourites", "first up"), a 1,200-sentence fuzz both ways, pathological inputs including the address pattern |
| `test_hosts_personas.py` | 36 | wrong types in every field and a 3,000-case random fuzz (only plain refusals), all 16 built-in hosts validate, product-brief archetypes present, schema errors in plain language, markup in every field, impersonation guard, limits, create/update/hide/restore/remove, restart persistence, unreadable rows, newer database refused, database failure messages, concurrency, damaged or missing shipped data files (reported in plain words, extension fails visibly) |
| `test_hosts_composer.py` | 80 | topics to avoid (stems, many seeds, topic of an update), seeds below 2^53, effects present in every style and really said, the window of 8 at default lengths and with few facts, borrowed lines that stay near the length asked for, sponsor re-check with a lower-case look-alike, sponsor copy ending in an address, library invariants and loader refusals, every line of every style filled and linted for every host and phrase variant (over 5,000 distinct lines), whole scripts for 16 hosts x 10 kinds x 5 fact sets, determinism (pinned random values, and a two-process check that output does not depend on hash randomization), 200-composition repetition runs and the window rule for every host, length control within tolerance for every host and kind, effects, sponsor disclosure and verbatim copy, fail-closed paths, no network |
| `test_hosts_briefs.py` | 72 | explicit time parsing, `parse_brief` (kinds, brackets, symbols, source addresses), sentence splitting (titles, months, weekdays, numbers), verbatim extracts (prefix property fuzz, addresses of any ending, fragments, hostile input), update building: attribution, citations, skips, budgets, a fuzz of hostile briefs, items that do not end like a sentence for all 16 hosts, the 3,000-character limit, long openings and closings, plural agreement, nothing-to-read, markup in briefs; the research lane's own `Brief` (checked with its `check_brief`) through all 16 hosts; the three formats: disclosure, fictional flag, grounding, determinism, lengths |
| `test_hosts_llm.py` | 33 | accepted text and what describes it (`facts_used`, `variants`, warnings), instructions content, notes the host avoids kept from the writer, a writer that failed once, a thread that cannot start, a reply too short for the length asked, hostile facts in prompts, fallbacks for tags, invented numbers, names and addresses, too much and too little text, non-text, avoided topics, exceptions, timeouts, a stuck call never piling up threads, repeats, sponsor bypass, no network, no network imports in any module of the package |
| `test_hosts_extension.py` | 41 | real `ExtensionHost` (catalog, typed arguments, round trips, plain errors including wrong types, concurrency, status is cheap and lock-free, doctor checks, persistence, a copy of only the main database file, shutdown leaves no threads, blocked database), `config.json` handling (one check per problem, each with its own id), events, routes including previews, and the real `Station` plus the real HTTP `Server` on loopback (auth, origin check, size limit, library untouched), a damaged database file |
| `test_hosts_support.py` | 0 | shared fixtures |

## 8. Requested changes to shared files

1. **`pyproject.toml`** (orchestrator, **still open**): add `"myradio.hosts" = ["data/*.json"]` under `[tool.setuptools.package-data]`. Without it a built wheel lacks `personas.json` and `voice-*.json`, and the extension would report itself as failed (the source checkout is unaffected).
2. **UI** (`myradio/web/**`): the Team view and a panel registration (`Panel("team", "Team", ..., "team.js")`) belong to the UI lane. This lane registers no panel because the script would live outside its paths. Routes, copy and states are in section 5.
3. **Backups** (`backup.py`): include `ExtensionHost.backup_files()["hosts"]` (`hosts.sqlite3`, `config.json`). Nothing in them is secret.
4. **Generation and director lanes:** call `ctx.station.action("hosts.compose", {...})` (or `hosts.compose_update`) and build the speech job from the result like this: `{"text": result["text"], "speaker": result["speaker"], "allow_tags": true}`. The script is the station's own, so its one speaker marker and its allowed effects must survive; **without `allow_tags` the generation lane defuses the marker and turns `[excited]` into the spoken word "(excited)"**. A script with no effects can be sent as `plain_text` with `speaker` instead. A script is never over 3,000 characters (the generation lane's limit), but an update may run up to 180 s while the generation lane's default `max_seconds` for `update` is 150: pass `max_seconds` of at least `est_seconds` (up to 180). The hosts `seed` is for repeating a script; do not pass it as the generation `seed` (0 to 4294967295). Use `role` for the job role and `library_kind` for `station.import_audio`. Treat `warnings` as informational.
5. **Research lane and director:** hand over the research `Brief` as it is (a dict from `claim_airable`/`next_airable`); this lane's `brief_from_research` owns the mapping, so no change is needed there. Whether a brief may air (`airable`, `review`, `fresh_until_ts`) is checked by the director, not here. `citations` is what to persist next to the generated audio. A source whose `source_name_speech` is only a web address (the research lane falls back to the site's host name when a feed has no title) is not read: name the source in the researcher's settings.
6. **`myradio/extensions.py` (nice to have):** a way to mark an operation read-only, so the Operator chat can run `hosts.compose` with `dry_run` without asking for approval each time.
7. **`docs/RELEASE-STATUS.md` / README:** mention that hosts write text only and that the speech model, not this lane, makes audio.

## 9. Known limitations and unverified claims

* No audio exists. Nothing here proves a script sounds right in Drama 3, that `<|speaker:N|>` and the effect names are accepted, or that the pace constants match real speech. Operators can rename or disable effects in `config.json`; the constants and the speaker range are code.
* The writing assistant path is checked against fake functions only. How a real model behaves against the validator (how often it would be rejected) is unknown.
* Scripts are English only. A host with another `language` is refused when saved. Templates are fixed text; there is no learning from listener feedback.
* Hosts that share a script style share sentence structure. Their differences are phrases, pace, talkativeness and effects.
* The grounding lint is conservative and heuristic. It checks digits, spelled numbers, capitalised names (not lowercase names), and web addresses. Anything shaped like `name.ending` counts as an address when the ending is lower case or capitals, so a file name (`notes.txt`) or a full stop with no space after it and no capital letter (`end.then`) is flagged too, which for template output cannot matter and for assistant output falls back to the template. A spoken address is found only for the usual endings ("dot com", "dot garden", "dot ai", ...); only the first 6,000 characters of a text are examined, and a longer text is reported as such. A host's own words may name things in lowercase; sentence-initial words are accepted if they are ordinary English, so an invented name that is also a common word at the start of a sentence would pass. For template output this cannot matter (templates contain no names); for assistant output a false alarm falls back to the template, which is the safe direction.
* "Topics to avoid" match by the first letters of each word (section 4, Topics to avoid) on operator notes, the topic of an update, brief items and their source names, and on what a writing assistant returns. They do not filter song titles, the station's own tagline or a sponsor's copy, and related words with no common start ("election" for "politics") are not matched unless listed. A short topic matches every word that starts with it ("war" also matches "warm"), and a stem can match a word you meant to allow ("gossip" also matches "gossamer").
* The impersonation guard looks for phrases, not for real names. Writing a real person's name into a host's name cannot be detected.
* `est_seconds` ignores pauses a speech model may add and the effect of long numbers or names. A script can run longer or shorter than estimated.
* Hidden built-in hosts still resolve by id, so a show that names one keeps working; removing one of your own hosts makes later requests for it fail with "There is no host with that id." The caller (shows, director) must handle that.
* The repetition guarantee is relative to the pool of lines that fit the request and to the memory the process can see. Two services pointed at the same database file at the same time are not coordinated.
* The speaker marker is placed directly before the first word with no space; whether Drama 3 prefers a space is unverified.
* Anything an agent or operator puts in `notes`, `sponsor_copy` or a brief is spoken as written once cleaned. Who may call `hosts.compose` is the station's authorization policy (chat proposals need approval, MCP and the dashboard are immediate); this lane only guarantees that what is spoken cannot carry voice codes, an unsupported number, name or address that the caller did not supply, or a missing sponsorship notice.
* The verbatim-extract rule assumes the cleaned item text is already the sentences the research lane vouches for. This lane does not check facts, freshness or truth. "Verbatim" means the words as the cleaner left them: speaker markers, HTML-style tags, stage directions in brackets and control characters are removed, other square brackets are dropped with their words kept, and whitespace is tidied. An item with a `<` or `>` that is not a tag is not read at all rather than read wrongly.
* Sentence splitting is by rule, not by understanding. Unknown abbreviations are caught only by the short-reading rule (a reading of fewer than four words is joined to the next sentence), so an unusual abbreviation in the middle of a sentence can still end an extract early, at a point that is still a full stop in the source. Short sentences are sometimes read two at a time because of that rule.
* The research mapping reads `researcher_name` as the topic, which is a label the operator chose and may not read well after "an update on"; pass `facts.topic` to override it. `commentary` is introduced as opinion, `announcement` as an announcement and `uncertain` (the research lane's `unverified`) as not confirmed; the wording is heuristic like the research lane's kind rules.
* With very few facts only a few lines fit (section 4, Anti-repetition), so a line can come round within 8 scripts; 44 of the 144 host and kind pairs do so when only the station name and the time are known. The library was not widened for that case.
* A brief item's source that is only a web address is left out rather than guessed into a name. A source such as `Space.com` (a name that is also an address) is treated the same way: give the source another name to include it.

## 10. What changed in the review fix pass (what other lanes may notice)

Independent reviewers found these; each is fixed and has a test that failed before the fix.

1. **Hand-built `Facts` are cleaned** like a mapping (effect tags, speaker markers, huge values); before, they went straight into the script.
2. **Writing-assistant results describe their own text**: `facts_used` is found in the words, `variants` is 0, and no warning is carried over from the set-aside template script.
3. **Briefs.** The research lane's `Brief` is read directly (`brief_from_research`). An unrecognised `kind` is now `uncertain` (it was `reported`), `unverified` is `uncertain`, and `announcement` is a kind of its own, so `citations[].kind` can now be `announcement`. New skip reasons: `source_link`, `symbols`, `fragment`. Titles, months and weekdays no longer end a sentence; an item that does not end like a sentence is ended by the host before its next words; words in square brackets are no longer deleted; web addresses of any ending are never read, and a source that is only an address is left out; times are parsed by one rule on every Python version. One existing assertion was changed to match: `test_odd_values_are_reported_not_trusted` now expects `uncertain` where it expected `reported`.
4. **Script length.** A script is at most 3,000 characters (the speech step's limit; it was 6,000), and an update is kept under it by reading fewer items, with a note.
5. **Seeds** the station makes up are below 2^53 (they were below 2^62, which a browser cannot hold exactly). The accepted range is unchanged.
6. **Topics to avoid** match by word stems, and also apply to the topic of an update and to source names.
7. **Repetition**: the window of 8 now holds at the default lengths by borrowing the nearest-length lines (rarely, and with a note), and with very few facts every usable line is used before any is repeated.
8. **Effects**: lines carrying `[whispers]` and `[laughs]` were added to the `warm`, `workshop` and `smooth` styles (by changing existing lines; the template count is still 960), so Nova, Hazel, Wren and Dusty can use the effects they are listed with. Lines are now timed without the effects a host may not use.
9. **Doctor**: each problem in `config.json` has its own check id.
10. **`hosts.save`** answers a value of the wrong kind in `pace` or `allowed_tags` with the field's plain sentence instead of the generic failure message.
11. **Lint**: addresses of any ending are found; the full stop after an address stays a sentence end; a text over 6,000 characters is reported as only partly checked.
12. **Writer**: a thread that cannot start no longer leaves the assistant marked busy forever.
