# Lane: shows (clocks, shows and the planner)

Package `myradio/shows/`, tests `tests/test_shows*.py`, this file. The extension name is `shows`
(`myradio.shows.extension.build()`), loaded by `myradio.extensions.builtin_extensions()`.

A **show** is a recurring part of the week with a music style, a host, a talk level and rules for spots
and updates. The **week** is a time zone, a set of shows that never overlap and a **default show** that
fills every hour no show is scheduled for. The **planner** turns the week into a deterministic,
time-ordered list of **plan items** (station IDs, host breaks, spot breaks, updates and songs) that the
director later converts into generation jobs and queue entries. A **clock** is the recipe for one hour.

## 1. Purpose and honest status

What this lane is: schedule data, validation, conflict reports, daylight-saving-correct time expansion,
the hour recipe and the planner. What it is not: it makes no audio, runs no model, never touches the
queue, never calls a station action, and never starts anything on a timer. It plans; the director acts.
Nothing in this lane was run against a model, a GPU, a real feed or a real destination. No audio was generated.

**Real: executed in this session and observed.**

| Claim | Evidence |
| --- | --- |
| Show model, validation, week, overlaps, gaps, midnight-crossing, day map | `tests/test_shows_model.py`, `test_shows_week.py` (including 6,000 random payloads that may only ever produce a clean `ShowError`). |
| Daylight saving: gaps skipped, repeats taken once, two zones with two changes each | Hand-derived expected instants for America/New_York and Europe/London, plus an independent brute-force oracle over every wall-clock minute around every 2024 clock change in 10 zones (including half-hour and 45-minute zones). Time is simulated by passing explicit timestamps; nothing waited for a real clock change. |
| `hour_plan` equals the design preview's example hour | The preview's `hourPlan()` is ported line for line in the test and compared for all 24 talk/host/spot/update combinations (roles and non-song seconds equal, song seconds within one second, total 3600). |
| `plan()` deterministic, independent of the call time, stable across restarts | Same seed gives the same output; `time.time`, `time.monotonic`, `random.random` and `os.urandom` are patched to raise while planning; a fresh interpreter with three different `PYTHONHASHSEED` values gives byte-identical output; results are pinned by digest and pass on Python 3.12.3 and 3.13.14. |
| Persistence, migrations, damaged-file handling, caps | Real SQLite files in temporary folders; real corruption (scribbled, truncated, zeroed, oversized files) exercised; a copied main file restores a whole week on another station. |
| Operations, status, doctor checks, routes | Run through a real `Station(start_worker=False, extensions=[...])`, the real `ExtensionHost`, and a real loopback HTTP server (`127.0.0.1`, port 0); 250 random operations keep the saved file and the running extension in agreement. |
| Bounds | 10,000 shows rejected by the cap, 10,000 database rows reduced to 200, a week of plan in 47 ms, hostile overlap data reported in 150 ms (measured, section 7.1). |

**Verified only against fakes.**

| Claim | What stood in for the real thing |
| --- | --- |
| Time-zone detection on macOS | Synthetic symlinks and files in a temporary folder that imitate the macOS and Debian layouts. No Mac was available. |
| The extension host and the seam under the 250-random-operations test | A stub station that refuses every call, used only in that test; the same extension is exercised through a real `Station` in the other extension tests. |
| Hosts, researchers and palettes named by a show | There is nothing to check them against inside this lane, so they are opaque slugs; the starter week's example hosts were only compared by name with the hosts lane's data file, not run together. |

**Unverified.**

| Claim | Why |
| --- | --- |
| Python 3.10 and 3.11 at run time | Every file parses with the Python 3.10 grammar (`ast.parse(feature_version=(3, 10))`) and no 3.11+ API is used, but only 3.12.3 and 3.13.14 ran the tests. |
| A Shows screen | It does not exist yet; section 5 is the contract for it. Nothing was rendered in a browser. |
| The director, generation, ads, hosts and research lanes consuming this | Not integrated. Section 2.4 is the contract they consume. |
| Behavior across a real clock change on a running station, and over days (soak) | Not run; clock changes are covered by the explicit-timestamp tests and the oracle. |
| Native macOS behavior in general | Not run. |

## 2. Public API

Everything below is plain Python, immutable where it can be, safe to call from any thread, and never
reads the clock (except the extension, which uses its context's `clock`).

```python
from myradio.shows import (Show, Style, Week, Airing, PlanItem, ClockItem, ShowError, plan, hour_plan, summarize,
                           default_show, find_overlaps, find_gaps, conflict_report, WEEK_TEMPLATES, CLOCK_TEMPLATES,
                           DEFAULT_SHOW_ID, MAX_SHOWS, ROLES)
```

### 2.1 Show and Style (`myradio/shows/model.py`)

`Show` is a frozen dataclass that is valid by construction: building one with a non-canonical field raises
`ShowError`.

| Field | Type and rule |
| --- | --- |
| `id` | slug `[a-z0-9][a-z0-9-]{0,62}`. `"default"` is reserved for the default show. |
| `name` | 1 to 40 characters, whitespace collapsed. Control and invisible characters and `< > [ ] { } \| \` are refused (so a name can never carry a speech tag such as `<\|speaker:1\|>` or `[laughs]`). |
| `days` | canonical ISO weekday string `"1,2,3,4,5"` (Monday=1). A show that runs past midnight belongs to the day it **starts** on. |
| `start_min`, `length_min` | minute of the day the show starts (0 to 1439) and its length (15 to 1440). Times are wall-clock times in the week's zone. |
| `style` | `Style(palettes, tags, energy, adventurousness, vocals)`: lists of slugs (up to 32 each; empty means "anything"; `palettes` = palette ids, `tags` = tags the music must carry) and integers 0 to 100. |
| `host_id` | slug or `None`. |
| `talk` | `"rare"`, `"some"` or `"often"`. |
| `spots_allowed` | bool. |
| `updates_allowed`, `update_researcher_id` | bool and slug-or-`None`. A researcher implies updates are on. `updates_allowed` is an addition to the brief so the starter week can ask for an update before any researcher exists. |
| `color` | `#rrggbb`, lowercase. |
| `clock` | hour recipe: `classic`, `music-heavy` or `talky`. |
| `is_default` | True only for the default show. |

Derived: `day_set`, `end_min` (1 to 1440, never 0), `crosses_midnight`, `when()` (for example
`"Weekdays, 6 am–10 am"`), `to_dict()`.

`Show.parse(data, base=None, *, taken_ids=(), taken_colors=()) -> Show` accepts what people and agents send:

* `start` and `end` as `"HH:MM"`, `"H:MM"` or a whole hour number. `end` may be `"24:00"` (or `24`) for midnight at the end of the
  day. An `end` earlier than the `start` means the show runs past midnight (`22:00` to `02:00` is four hours). `end` equal to
  `start` is refused; a show of at least 15 minutes and at most 24 hours is required. Only `00:00` to `24:00` can be a full day.
* `days` as `"1,2,3"`, a list of ints (or digit strings), or `everyday`, `daily`, `weekdays`, `weekends`.
* Fields left out keep their value in `base` (a new show gets defaults). With only one of `start`/`end` given, the other **clock
  time** is kept.
* `host_id` and `update_researcher_id` accept `None` or `""` for "none". Turning updates off clears the researcher.
* Unknown fields are refused; read-only fields echoed back (`start_minute`, `duration_minutes`, `crosses_midnight`, `when`,
  `is_default`) are ignored so a show can be sent back unchanged.
* With no `id`, one is made from the name (`"Morning Rise"` becomes `morning-rise`, then `morning-rise-2`).
* The default show may change its name, style, host, talk, spots, updates, color and clock, but has no days, start or end.

`to_dict()` shape (the same object appears in `shows.list`, `GET /week` and `shows.save` results):

```json
{"id": "morning", "name": "Morning Rise", "days": "1,2,3,4,5,6,7", "start": "06:00", "end": "10:00",
 "start_minute": 360, "duration_minutes": 240, "crosses_midnight": false, "when": "Every day, 6 am–10 am",
 "style": {"palettes": ["bossa-courtyard"], "tags": [], "energy": 70, "adventurousness": 35, "vocals": 40},
 "host_id": "dex", "talk": "often", "spots_allowed": true, "updates_allowed": true,
 "update_researcher_id": null, "color": "#8a4d3a", "clock": "classic", "is_default": false}
```

`end` is never `00:00`: midnight at the end of the day is written `24:00`. Names are plain text and must be escaped by
whatever displays them.

### 2.2 Week (`myradio/shows/week.py`)

`Week(timezone, shows=(), default=None)` is an immutable snapshot. It checks the zone, the 200-show cap, duplicate ids, and runs every
show's start time and days through `myradio.scheduling.recurrence()` (the station scheduler's own validator). It does **not**
reject overlaps, so damaged data can still be loaded, planned and reported; saving through the extension refuses overlaps.

| Member | Meaning |
| --- | --- |
| `timezone`, `shows` (sorted by start minute then id), `default`, `get(id)`, `all_shows()` | |
| `with_show(show)`, `without(id)`, `with_timezone(zone)`, `with_shows(shows)` | return a new `Week` |
| `segments(low, high)` | tiles `[low, high)` (epoch seconds, ints) with `Segment(start, end, show, fallback)`; neighbouring pieces of the same show are merged; time with no show is the default show with `fallback=True` |
| `airing(ts) -> Airing(show, fallback, since, until, next_show, next_fallback)` | what is on air at `ts`. `since` is None if the stretch began more than 8 days earlier and `until` is None if nothing changes within 8 days |
| `next_change(ts)` | `(moment, show, fallback)` or `None` |
| `plan(from_ts, to_ts, seed=0)` | `myradio.shows.planner.plan` |

Time semantics (also in `timeline.py`'s docstring):

* A show is written in wall-clock time. `resolve_local` turns `(date, minute)` into UTC with the same rules as
  `scheduling.next_occurrence`: a time that does not exist (clocks go forward) is skipped, a time that happens twice
  (clocks go back) is taken once, at its first occurrence.
* For a recurring *point* in time, skipping means "not that day". A *show* is an interval, so a boundary inside the skipped hour moves to
  the moment the clocks jump (a show lying wholly inside the skipped hour airs for no time that day). Neighbouring shows therefore
  always meet exactly, a show that spans a change lasts an hour less or more than its nominal length, and
  `tests/test_shows_dst.py` states the one place this differs from `next_occurrence` (a start inside the gap: the scheduler
  skips the day, the show starts when the clocks jump).
* Where two stored shows overlap anyway (damaged data), the one that started later wins, then the smaller id.

### 2.3 Clocks (`myradio/shows/clocks.py`)

`hour_plan(show, seed=None, *, template=None, seconds=3600) -> list[ClockItem]` where `ClockItem(role, est_seconds, ordinal)`.

* Items are ordered; `est_seconds` are whole seconds and add up to `seconds` exactly (for any block of a minute or more; shorter blocks are empty).
* No seed returns the **recipe** (the design preview's example hour). A seed (int 0..2^63-1, or text up to 200 characters; an int and its decimal
  text are the same seed) returns a shuffled variant: the order of breaks, how many songs sit between them and song lengths change, the
  ingredients and total do not. It is pure SHA-256 arithmetic, so it is identical across restarts, machines and Python versions.
* One station ID first; `n` host breaks only when the show has a host; one spot break if `spots_allowed`; one update if `updates_allowed`;
  songs fill the rest (`floor(available / song_seconds)` songs, at least one). No two breaks are adjacent and the block ends with a song.

| Template | rare | some | often | ID | spot | update | song avg |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `classic` (the preview) | 1 x 20 s | 2 x 30 s | 3 x 40 s | 10 s | 60 s | 90 s | 225 s |
| `music-heavy` | 1 x 15 s | 1 x 20 s | 2 x 25 s | 10 s | 30 s | 60 s | 240 s |
| `talky` | 2 x 30 s | 3 x 45 s | 5 x 60 s | 10 s | 60 s | 120 s | 210 s |

Blocks other than an hour (a show starting at 21:30 has a 30-minute first block) scale: break counts are proportional to the length,
rounded half up (so a half hour of a 3-break show has 2 breaks, a spot and an update; 15 minutes has 1 break and no spot or update); under 5
minutes is one filler song; under 1 minute is empty. If breaks do not fit the lowest-priority ones (update, spot, last host break) are
dropped. A block can be up to 4 hours (half-hour clock shifts make 90-minute blocks).

`summarize(items)` gives legend rows `{role, label, count, seconds, length}` in the order id, song, host_break, spot, update.
`clock_notes(show)` gives plain sentences ("This show has no host, so it has no talk breaks. Choose a host to add them.").
`hour_plan` raises plain `ValueError` for bad arguments.

### 2.4 The planner and the plan item schema (`myradio/shows/planner.py`) — read this if you consume plans

`plan(week, from_ts, to_ts, seed=0) -> list[PlanItem]`; also `week.plan(...)`, `ShowsExtension.plan(...)` and the `shows.plan` operation.

Rules:

* **Window.** An item is returned when `from_ts <= item.start < to_ts`. Consecutive windows never repeat or lose an item
  (`plan(a, b) + plan(b, c) == plan(a, c)`, tested). At most 168 hours per call; `from_ts` and `to_ts` are epoch seconds between 1970 and 2100.
* **Blocks.** Time is first split into stretches with one show on air, then cut at every top of a local hour into blocks (a full hour is
  3600 s; a show that starts or ends part-way through an hour has a shorter block there). Each block gets its own `hour_plan`, so every block starts with a station ID.
  The block edges are a property of time and the zone, not of the window, so a window that opens in the middle of a block still gets that block's real ids.
* **Seeds.** The block's shuffle is derived from `(seed, show id, block start)`. Different seeds give different but equally valid plans.
  When you do not pass a seed the extension uses the station's saved seed (`plan_seed`), so a plan is the same after a restart.
* **Ids.** `id` is `uuid5(fixed namespace, "seed|show_id|block_start|role|ordinal")` as 32 lowercase hex characters (the shape of a uuid4
  hex, but reproducible). The same slot, role and seed always give the same id. Editing a show never changes the ids of other shows'
  slots, and an item of the edited show keeps its id as long as its block and its place among items of the same role still exist (change the
  start or end and the blocks move, so their ids change; change the host or the talk level and the ids stay, only `signature` changes).

`PlanItem` fields (`to_dict(zone=None)` returns exactly these keys, plus `start_iso` and `slot_start_iso` when a zone is given):

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | str, 32 hex | Stable id of this slot and role under this seed. Use it to deduplicate and as the seed for any per-item random choice (which palette, vocal or instrumental) so re-planning reproduces the same choice. |
| `start` | float | **Soft earliest start**, epoch seconds UTC: the block start plus the estimated lengths of the items before it. Nothing is cut off to meet it; playout never hard-interrupts, so the real start is the first moment at or after this when the previous item has finished. Do not air an item noticeably before `start`. Generate ahead as far as you like. |
| `start_iso` | str | The same instant with the station zone's offset, `2025-01-15T07:30:02-05:00` (route and operation output only). |
| `role` | str | `song`, `host_break`, `id`, `spot`, `update` (the shared vocabulary). |
| `kind` | str | Library kind a finished item imports as: `song` is `music`; `host_break`, `id`, `update` are `speech`; `spot` is `ad`. |
| `show_id` | str | The show that owns the slot; the default show's id (`default`) for unscheduled time. |
| `host_id` | str or null | Voice for speech roles (the show's host). `null` for songs, and `null` on a speech item means "the station's default voice". Unknown ids are the director's to report; nothing here checks them. |
| `est_seconds` | int | Planned length in whole seconds: an estimate that budgets the block, not the length of any real audio. The block's items sum to `slot_seconds`. |
| `constraints` | object | Guidance, never a command. See below. |
| `slot_start`, `slot_start_iso`, `slot_seconds` | float, str, int | The block this item belongs to and its length. |
| `position`, `ordinal` | int | 0-based place in the block, and among items of the same role in the block. |
| `fallback` | bool | True when the default show is filling time no show is scheduled for. |
| `signature` | str, 16 hex | Changes when the show's rules for this item change (host, style, talk level, length). Compare it with the signature stored beside already-made audio to find stale items; `id` stays the same. |
| `label` | str | Singular label for a "coming up" list: `Song`, `Host break`, `Station ID`, `Spot`, `Update`. |

`constraints` by role:

| Role | Keys |
| --- | --- |
| `song` | `palettes` (list of palette ids, empty = any), `tags` (the music must carry all of them), `energy`, `adventurousness`, `vocals` (0 to 100) |
| `host_break` | `talk`, `max_seconds`, `optional: true` |
| `id` | `max_seconds`, `optional: false` |
| `spot` | `max_seconds` (the room for sponsor messages in this break), `optional: true` |
| `update` | `researcher_id` (or null = any), `max_seconds`, `optional: true`, `if_nothing_new: "play_music"` |

Plan items contain **no free text** (only ids, numbers and enumerations), so they are safe to turn into speech jobs. Show names are
operator text; whoever puts a name into a script must still neutralize `<|speaker:N|>`-style and `[event]` tags in it.

Example (one `spot` item):

```json
{"id": "56071d0483ae50c69cdab5d4420fa7ef", "start": 1736944698.0, "role": "spot", "kind": "ad", "show_id": "morning",
 "host_id": "dex", "est_seconds": 60, "constraints": {"max_seconds": 60, "optional": true},
 "slot_start": 1736942400.0, "slot_seconds": 3600, "position": 13, "ordinal": 0, "fallback": false,
 "signature": "ee2f3a9d00dc09b2", "label": "Spot", "start_iso": "2025-01-15T07:38:18-05:00",
 "slot_start_iso": "2025-01-15T07:00:00-05:00"}
```

How a director should use it: ask for the next few hours with a seed it owns (or the saved one); re-ask when `status.revision`
changes; ignore ids it already prepared whose `signature` is unchanged; treat `optional` items as droppable when audio is not ready;
replace an `update` with music when its researcher has nothing new; never read `start` as an exact time.

### 2.5 Conflicts and gaps (`myradio/shows/conflicts.py`)

* `find_overlaps(shows, max_pieces=2000) -> (list[Overlap], truncated)`. The week is a circle (Sunday night runs into Monday); touching is not
  overlapping; a show never overlaps itself. `Overlap.first` is the show that started earlier. Overlaps are grouped by the day they begin on.
* `find_gaps(shows) -> list[Gap(start, length)]` in minutes from Monday 00:00 (a gap may run across midnight and across the end of the week).
* `conflict_report(week, with_fixes=True)` returns the plain-language report used by `GET /conflicts` (shape in 4.2).
* `suggest_fixes(week, overlap)`: up to three fixes, the smallest change in weekly airtime first, and only changes the station would accept as they are (after the
  change the changed show must overlap nothing, which is what `shows.save` demands): shorten the first show, start the second later, stop one airing on the days in
  conflict, and always last, remove the second. Each fix is
  `{"operation": "shows.save", "arguments": {"show": {"id": ..., "end": "09:00"}}, "text": "Make Morning Rise end at 9 am."}`, so it can
  be sent back as is.

### 2.6 Errors

`ShowError` subclasses `ExtensionError` (a `ValueError`), so inside an operation or route handler it reaches the person as a plain message.
Every refusal message in this lane is written for a nontechnical person: what happened, what it means, what to do. No refusal the tests
provoke carries paths, SQL, class names or stack traces (the tests check each one against a list of jargon patterns).

## 3. Persistence

* File: `<data>/ext/shows/shows.sqlite3`, mode 0600, opened with `Context.open_db` (WAL). Nothing is stored in the station database.
* Tables (migration step 1, `PRAGMA user_version = 1`, written with `IF NOT EXISTS` so replaying it after a crash is harmless):
  `shows(id PK, name, days, start_min, length_min, style JSON, host_id, talk, spots_allowed, updates_allowed, update_researcher_id, color, clock, is_default, created, updated)`
  with a partial unique index so there is exactly one default row, and `settings(key PK, value)` holding `timezone`, `zone_source`
  (`configured`, `detected`, `fallback`, `chosen`), `plan_seed` (random, created once) and `revision` (+1 per change).
* Every change is one transaction ending in a revision bump, then `PRAGMA wal_checkpoint(TRUNCATE)`, so **copying `shows.sqlite3` alone is a complete backup**
  (tested by copying only the main file into another data folder and starting a station on it). `persistent_files()` returns `["shows.sqlite3"]`.
* A file written by a newer myradio is refused with a plain message and left untouched. A file that cannot be read as a database (not SQLite,
  scribbled, truncated, failed integrity check) is **moved aside** as `shows.sqlite3.damaged-<UTC stamp>` (plus its log files; never deleted, never
  overwritten, not part of backups), a new empty week is started and the reason appears in status and the doctor checks. A file or log of more than
  64 MiB is treated the same way without being opened (a real one is well under a megabyte). A busy or unwritable file is **not** treated as damage:
  the extension fails with a plain message instead.
* Rows that cannot be read are skipped and counted; at most 200 scheduled shows are ever loaded (the oldest); the rest are reported, not loaded.
* Secret files: none. Nothing in this lane stores a credential, a token or a launch code.

## 4. Extension surface

Name `shows`; no panels (see section 8); no events consumed (so no mailbox thread); no threads created.

### 4.1 Operations

All typed by the host. Errors reach callers as `StationError` with the plain message. Result keys are added to the host's `{"ok": true, "operation": ...}`.

| Operation | Arguments | Result |
| --- | --- | --- |
| `shows.list` | none | `timezone`, `timezone_source`, `revision`, `plan_seed`, `now`, `default_show`, `shows`, `conflicts`, `templates`, `clock_templates`, `limits`, `notes` (the `GET /week` document without `days`). Read-only. |
| `shows.save` | `show` (object) | `show`, `created`, `revision`, `gap_hours`, `notes`, `summary`. Creates or changes; an `id` that exists is changed (missing fields keep their value), otherwise a show is created. **Refused when it would overlap another show**, with a message naming both shows, the days and the times and up to two ways to fix it (or a general hint when no single change would work); nothing changes. Also refused at 200 shows. |
| `shows.remove` | `id` | `removed`, `name`, `revision`, `summary`. The default show cannot be removed. |
| `shows.apply_template` | `name` (`starter` or `empty`), `replace` (bool, optional) | `template`, `shows` (ids), `revision`, `summary`. **Replaces every scheduled show** (the default show is kept) in one transaction. Refused with a plain message unless `replace` is true when shows already exist. Templates are never applied on their own. |
| `shows.set_timezone` | `timezone` (IANA name) | `timezone`, `revision`, `summary`. Show times keep their clock times in the new zone. Beyond the brief: the week needs a zone. |
| `shows.plan` | `hours` (1 to 168, default 3), `from_ts` (epoch seconds, default now), `seed` (default: the saved seed) | `from`, `to`, `from_iso`, `to_iso`, `timezone`, `seed`, `revision`, `items` (plan items with ISO times). Read-only. Beyond the brief: it is how another extension (the director) reads the plan through the seam. |

`shows.list`, `shows.set_timezone` and `shows.plan` are additions to the brief's three operations; each is justified above.
The host has no "read-only" flag on operations, so the Operator's approval step cannot tell the two read-only ones from the rest (section 8).

The `starter` week is the design preview's five sample shows, every day: Overnight Drift 00:00–06:00 (ambient-garden, no host, rare talk),
Morning Rise 06:00–10:00 (bossa-courtyard, host `dex`, often, spots, update), Daytime Focus 10:00–17:00 (chamber-minimalism, host `nova`, rare,
spots), Golden Hour 17:00–20:00 (warm-brush-jazz, host `marlowe`, some, spots, update), Late Night 20:00–24:00 (downtempo-trip-hop, host `nova`, some),
with the preview's colors and energy values (15, 70, 35, 50, 30). The preview had no per-show adventurousness or vocals sliders, so every starter
show uses adventurousness 35, and vocals are 0, 40, 5, 30 and 20 in the order above. The hosts are the preview's example hosts, referenced by id only.

### 4.2 Routes (all `GET`, under `/api/x/shows`, authenticated and origin-checked by the host)

Errors are `{"error": "<plain message>"}` with 400 (bad request) or 404 (unknown show). Query parameters are bounded (100 characters, whole numbers
`[0-9]{1,18}`).

**`GET /week`** — everything the Shows screen needs in one call:
`timezone`, `timezone_source`, `revision`, `plan_seed`, `now` (`at`, `at_iso`, `show_id`, `name`, `host_id`, `fallback`, `since`, `until`, `until_iso`,
`next {show_id, name, fallback}`), `default_show`, `shows` (sorted by start then id), `days` (seven entries `{day, name, blocks}`; each block
`{show_id, fallback, start_minute, end_minute, start, end, continues_from_previous_day, continues_to_next_day}` and the blocks of a day cover 00:00 to
24:00 exactly, with default-show gaps marked `fallback`), `conflicts` (below), `templates` (`name`, `title`, `description`, `show_count`, `shows`),
`clock_templates`, `limits` (`max_shows`, `max_name_length`, `min_minutes`, `talk_levels`, `max_plan_hours`), `notes` (plain sentences about unusual
data found on start).

**`GET /plan?hours=N&from=T&seed=S`** — `hours` 1 to 168 (default 24), `from` epoch seconds or ISO-8601 **with an offset** (default now), `seed` 0..2^63-1
(default the saved seed). Returns `from`, `to`, `from_iso`, `to_iso`, `timezone`, `hours`, `seed`, `revision`, `count`, `items`. A full week is about 3,000
items and 1.5 MB.

**`GET /clock?show_id=ID`** — the example hour of a saved show: `show_id`, `show_name`, `template`, `template_title`, `seed`, `overrides`,
`total_seconds` (3600), `item_count`, `items` (`role`, `kind`, `est_seconds`, `offset_seconds`, `length` as `m:ss`), `summary` (legend rows),
`music_seconds`, `notes`, `templates`. Optional what-if settings change the preview and save nothing: `template`, `seed`, `talk`, `host_id` (an id, or `none`),
`spots_allowed`, `updates_allowed` (`true`/`false`). Use `show_id=default` plus overrides to preview a show that has not been saved yet.

**`GET /conflicts`** — `{ok, overlap_count, overlaps_truncated, overlaps, gaps, gap_hours, summary}`. An overlap entry is
`{kind: "overlap", severity: "error", show_ids, days, start, end, message, fixes}`; a gap entry is
`{kind: "gap", severity: "info", days, start, end, minutes, message, fixes}`. Gaps are information, not errors: the default show plays there.

### 4.3 Status (a cached snapshot, no locks, never calls `station.status()`)

```json
{"ready": true, "timezone": "America/New_York", "timezone_source": "configured", "revision": 2,
 "counts": {"shows": 5, "overlaps": 0, "gap_hours": 0.0},
 "current": {"show_id": "morning", "name": "Morning Rise", "host_id": "dex", "fallback": false, "since": 1736938800.0, "until": 1736953200.0},
 "next_change": {"at": 1736953200.0, "at_iso": "2025-01-15T10:00:00-05:00", "show_id": "daytime", "name": "Daytime Focus", "fallback": false},
 "conflicts": [], "notes": []}
```

`since`/`until`/`next_change` can be `null` (nothing changes within eight days, for example an empty week). `conflicts` holds up to three
overlap messages. The answer is rebuilt when the schedule changes or the show on air changes (and at least daily) and is otherwise returned
as is. This is **not strictly O(1)**: a rebuild expands about 17 days of the schedule (measured on this machine: 0.6 ms for the starter week,
3 ms for 200 shows; a cached call takes well under a microsecond). It never waits for a write (tested while the write lock is held). The
starter week's status is 460 bytes; the tests require under 1.5 KB for it and under 5 KB at the cap.

### 4.4 Doctor checks (`checks()`)

| id | Status | Meaning |
| --- | --- | --- |
| `timezone` | `ok` / `warn` | `warn` when no zone could be detected and UTC is in use ("Choose your time zone in Shows so each show starts at the right local time."). |
| `schedule` | `ok` / `warn` | `warn` when shows overlap (first overlap message and a hint). `ok` with the plain detail otherwise (no shows yet, hours on the default show, or every hour covered). |
| `data` | `warn` | Only when something was set aside or skipped on start. |

### 4.5 Events consumed

None. The extension does not override `on_event`.

### 4.6 In-process API

`ShowsExtension.week()`, `.plan(from_ts, to_ts, seed=None)` and `.airing(ts=None)` read the current snapshot without locking, for code that
holds the instance. Other extensions cannot hold it (the seam gives them only `status()`, `action()`, `import_audio()` and `extension_checks()`), so they use
`shows.plan` and `shows.list` through `ctx.station.action`.

## 5. UI contract (the Shows screen, preview section "WHEN WHAT PLAYS")

Wording below is the intended copy; it follows the design preview and the lane's own messages.

* **Heading.** Eyebrow `WHEN WHAT PLAYS`, title "Your station's week.", "Shows decide the style, the host and the rhythm of each part of the day. Start
  with the defaults and change only what you like."
* **Now line.** From `GET /week` `now`: "On air now: Morning Rise, until 10 am. Next: Daytime Focus." Use `until_iso` for the time; say "(default show)" when `fallback`.
* **Time zone row.** "Times are in America/New_York." with a Change control (a select of zone names, or a text field with suggestions) calling
  `shows.set_timezone`. When `timezone_source` is `fallback`, show: "We couldn't tell your time zone, so times use UTC. Choose yours so shows
  start at the right hour." Changing it keeps clock times ("6 am stays 6 am").
* **A day on the air.** Day chips `Every day`, `Mon`…`Sun` (targets at least 44 px). Draw the 24-hour strip for the chosen day from `days[n].blocks`
  (`start_minute`/`end_minute` give the geometry; `fallback` blocks are drawn plainly, labeled with the default show's name; `continues_*` can add a
  small arrow). Offer `Every day` only when all seven days have identical blocks. The list below shows `name`, `when`, host and an `Edit` button.
  Color is `color`; choose the text color by luminance (the built-in colors all have at least 5.5:1 contrast against the preview's light text
  `#f4f2ed`; a custom color is not contrast-checked). Names are text, never HTML.
* **Gaps.** Under the strip, show each `conflicts.gaps[].message` as quiet information ("Nothing is scheduled every day from 10 am to 5 pm, so Station
  Mix (your default show) plays then.") with an "Add a show" button prefilled from the entry's fix.
* **Empty state.** No scheduled shows: "No shows yet. Station Mix plays all day." with `Use the ready-made week` (`shows.apply_template starter`) and `Add a show`.
  If shows exist, applying a template first asks in the page (not a native box): "Replace your 5 shows with “A day on the air”?" and then sends `replace: true`.
* **Editor** (one show at a time). Labels and fields:

  | Label | Field |
  | --- | --- |
  | Show name (max 40) | `name` |
  | Music style (options from the prompt library's palettes; this lane accepts any slug and does not check it) | `style.palettes` (and later `tags`) |
  | Days | `days` |
  | Starts / Ends | `start` / `end` (hour or 15-minute steps; "12 am" for `24:00` at the end) |
  | Host (first option "No host") | `host_id` |
  | Talk breaks: Rarely / Sometimes / Often | `talk` = `rare` / `some` / `often` |
  | Energy, Calm to Lively | `style.energy` (0 to 100) |
  | Allow spots in this show. "Sponsor messages and your own promos, in short breaks." | `spots_allowed` |
  | Include a short update. "From one of your researchers. If nothing is new, music keeps playing." | `updates_allowed`, optional researcher select → `update_researcher_id` |
  | Hour style (advanced): Classic, More music, More talk | `clock` |

  `Save show` sends `shows.save` with the whole form. Show the server's error text unchanged in a `role="alert"` line (for example "Breakfast Club would
  overlap Morning Rise (Every day, 6 am–10 am) on weekdays from 9 am to 10 am. Start Breakfast Club at 10 am. Or make Morning Rise end at 9 am."). `Remove show` asks "Remove
  “Morning Rise”? Its hours will play Station Mix, your default show, until you add another show." (the preview's older text said the hours would have no show: there is
  always a show). The default show is edited with the same form without days or times and cannot be removed.
* **An example hour.** Card "Inside one hour of Morning Rise", badge `AN EXAMPLE HOUR`. Draw `GET /clock` `items` as arcs (`est_seconds` of 3600), the legend from `summary`
  ("Songs ×14 55:20"), the center "60 min" and `item_count` moments, then the notes. While the form has unsaved changes, call `GET /clock?show_id=<saved id or
  default>&talk=…&host_id=…&spots_allowed=…&updates_allowed=…&template=…` so the picture comes from the same code as the plan (do not re-implement the recipe in the
  browser). Footer: "Your station builds each hour from this recipe, then shuffles within it so no two hours sound alike."
* **Studio "coming up".** The preview's timeline can read `shows.plan` (`label`, `est_seconds`, `start_iso`); readiness ("ready") comes from the director, not from here.
* **Accessibility.** The strip is a group of buttons with names and times in text; selection is not conveyed by color alone; targets are at least 44 px; honour reduced motion.

## 6. Limits and bounds

| Bound | Value | Where it is enforced |
| --- | --- | --- |
| Scheduled shows | 200 (plus the default show); loading reads at most 200 rows | `Week`, `shows.save`, `Store.load` |
| Name | 40 characters; anything over 4,000 characters is refused before any processing | `clean_name` |
| Style lists | 32 palettes and 32 tags, slugs of at most 63 characters | `Style` |
| Show length | 15 minutes to 24 hours | `Show.parse` |
| Plan window | at most 168 hours per call, 1970 to 2100, `to > from` | `plan` |
| Plan size | about 3,000 items and 1.5 MB for a week | measured |
| Seeds | 0 to 2^63-1 (integers) or 1 to 200 characters of text | `hour_plan`, `plan` |
| Overlap search | 2,000 overlapping pieces, 50 reported, fixes for the first 10 | `conflicts` |
| Route and operation arguments | 100-character query values; whole numbers `[0-9]{1,18}`; the host limits objects to 64 KB | `extension`, host |
| Zone names | `[A-Za-z0-9_+-/]{1,64}`, no `..`, must load and be accepted by `scheduling.recurrence` | `check_zone` |
| Memory | one immutable snapshot; caches of at most 32,768 resolved times and 64 zones | `timeline` |
| Files | the shows file or its log over 64 MiB is set aside unopened | `store` |
| Threads | none; operations run on the caller's thread under one write lock; status and reads take no lock | `extension` |
| Time | `plan` for a week: about 50 ms; overlap report on 200 deliberately overlapping shows: about 150 ms | measured |

## 7. Tests

Run (the command given for this lane):

```
cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_shows*.py" -v
```

| File | Tests | Covers |
| --- | --- | --- |
| `test_shows_model.py` | 38 | validation, normalization, names, days, times, midnight, style, hosts and researchers, default show, direct construction, hostile fields, 6,000 random payloads, standard-library-only check, labels |
| `test_shows_clocks.py` | 21 | preview parity, totals for every template × talk × host × spot × update × seed, shuffling, pinned results, scaling of odd block lengths, argument checks |
| `test_shows_week.py` | 45 | caps (10,000 shows), overlaps (days, midnight, end of week, hostile), fixes (smallest change first, only ones the station would accept), gaps, day map, templates, on-air lookups, tiling |
| `test_shows_dst.py` | 28 | spring gap and autumn repeat in New York and London, half-hour zones, agreement with `scheduling.next_occurrence`, an independent oracle over 10 zones, the hour grid, planning across changes, zone and moment parsing |
| `test_shows_planner.py` | 28 | schema, constraints, ordering, determinism, clock independence, other `PYTHONHASHSEED`s, window partition, edits, bounds, fallback, random weeks |
| `test_shows_store.py` | 28 | fresh file, migrations (repeat, replay, newer file), changes, atomic replace, backups by copying one file, bad rows, 10,000 rows, damaged, corrupted and oversized files |
| `test_shows_extension.py` | 66 | real Station and HTTP: seam rules, status (clock, cache, lock-freedom), every operation, refusals, cap, persistence and restore, routes and bounds, checks, zone detection, concurrency, 250 random operations, 400 random payloads through the real operation |
| `test_shows_support.py` | 0 | helpers |

Measured results are recorded in section 7.1.

### 7.1 Measured results

254 tests in 8 files (21 + 28 + 66 + 38 + 28 + 28 + 45; `test_shows_support.py` holds helpers only). Every number below was observed in this session on a busy
shared machine (load average about 12), run under `nice -n 19 ionice -c3`, which makes the fsync-heavy tests slow in wall time and leaves about 6 seconds of CPU.

| Run | Result |
| --- | --- |
| `python3` 3.12.3, the command above (`-v`) | **254 run, 0 failed, 0 errors, 0 skipped**; 40.4 s wall, 6.3 s CPU |
| `.venv/bin/python` 3.13.14, same files | **254 run, 0 failed**; 43.0 s wall, 5.2 s CPU. The pinned digests and the oracle test pass unchanged. |
| 3.12.3 with `-X dev` and deprecation warnings as errors | **254 run, OK, no warnings** |
| Earlier runs of the suite (3.12.3 three times, 3.13.14 twice) before the last small changes | all OK |
| `tests/test_extensions.py` and `tests/test_extensions_station.py` | **45 run, 0 failed** |
| Whole suite, `python3 -m unittest discover -s tests -p "test_*.py"` | see section 7.2 |
| Ownership check (`check_ownership.py --lane shows`) | only paths in this lane changed; "No out-of-lane changes" |
| Python 3.10 grammar check of all 19 files | 0 problems |

Code size, counted with `wc -l`: `myradio/shows/` 2,343 lines in 11 files; `tests/test_shows_*.py` 3,497 lines in 8 files; this document about 500 lines.

Performance measured on the same machine: `plan` for a week (168 h, about 3,000 items) 47 ms; `Week.airing` 0.6 ms (starter) and 3 ms (200 shows, cold cache);
overlap report on 200 deliberately overlapping shows 148 ms; cached `status()` 0.28 microseconds; loading a database that holds 10,000 rows takes 7 ms
(it reads 200 and reports 9,800 as left out).

### 7.2 Whole suite

Command: `cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_*.py"`, run three times during the work
because other lanes were editing at the same time:

| Run | Tree | Result |
| --- | --- | --- |
| 1 | mid-way | 2,101 tests, 8 failures, all in other lanes' tests that were still being written (`test_generation_core` documentation check and seven in `test_hosts_composer`); none in `test_shows*`, `test_extensions*`, `test_agent_deadline` or `test_model_transport`. They passed in the later runs. |
| 2 | started before the spelling and fix-ordering changes (the spelling change landed while it ran) | 2,266 tests, **OK** (1 skipped, 10 expected failures), 317 s. |
| 3 | **the final tree** (no code or test file changed after it started; only this document did) | 2,285 tests, **OK** (1 skipped, 10 expected failures), 331.5 s, exit status 0. |

The one skipped test and the ten expected failures belong to other lanes' tests; this lane's tests have none (254 run, 0 skipped).

## 8. Requested changes to shared files

Nothing was changed outside the owned paths. The following would help, in orchestrator-owned files:

1. **UI.** `myradio/web/panels/shows.js` (and a stylesheet) built against section 5 and the routes in 4.2; once it exists, a one-line `panels()` returning
   `[Panel("shows", "Shows", "◷", "shows.js", 20)]` should be added to `myradio/shows/extension.py` (this lane's file). The preview's Shows view says
   "◇ PLANNED"; the label can go when the panel ships.
2. **Station-wide time zone.** The week keeps its own zone (`shows.set_timezone`). Ads, research and the director need the same zone; a single station
   setting (for example a `timezone` field on `station.update`, or a shared extension setting) would avoid two sources of truth. Until then they should read `timezone` from `shows.list`.
3. **Operations flagged read-only.** `Operation` has no read-only flag, so the Operator's approval step cannot tell `shows.list` and `shows.plan` (harmless reads) from
   `shows.save`, `shows.remove`, `shows.apply_template` and `shows.set_timezone`. A flag would let chat run the reads immediately and keep approval for the changes. MCP calls are
   immediate by design, so an external agent can replace the whole week with `shows.apply_template` and `replace: true`; the `replace` argument is the only guard this lane adds.
4. **`Context.open_db`.** When its own setup statements fail on a damaged file, the half-open connection is dropped unclosed (Python 3.13 reports a `ResourceWarning`).
   Closing it before re-raising would be cleaner. The shows extension avoids most cases by checking the file header first.
5. **Backups.** `ExtensionHost.backup_files()` already lists `shows.sqlite3`. The portable backup and restore code should include it and restore it before extensions start;
   `shows.sqlite3-wal` is not needed (the log is folded into the main file after every change).
6. **Hosts lane.** The starter week names the preview's example hosts `dex`, `nova` and `marlowe`. When this was written, the hosts lane's built-in personas file
   (`myradio/hosts/data/personas.json`, read only, not run) listed `nova`, `dex`, `marlowe` and `professor` among others, so those ids should resolve once the lanes are wired
   together; nothing here checks that. If a station has no host with one of those ids, that show simply has no host from this lane's point of view and the director should use
   the default voice and say so.
7. **Director.** Consume `shows.plan` (or `ShowsExtension.plan`), key prepared audio by `id`, store `signature`, re-plan when `status.revision` changes, and report unknown host ids.
8. **README / docs.** A short "Shows" section: what a show is, the default show, the starter week, and that gaps play the default show.

## 9. Known limitations and unverified claims

* **Verification limits.** See section 1: macOS, Python 3.10 and 3.11 runtime, a browser UI, the other lanes, long-running behavior and a real clock change on a running station were not verified here.
* **Host, researcher, palette and tag ids are not checked** (this lane cannot see the hosts, research or prompts extensions): any well-formed slug is accepted, so a typo is saved and the
  director has to cope with an id it cannot find.
* **One time zone per station**, and it is owned by this lane (request 2). A first-run zone is guessed from `TZ`, `/etc/localtime` and `/etc/timezone` and falls back to UTC
  with a warning; the guess can be wrong inside containers.
* **Weekly recurrence only.** No one-off shows, holidays or date exceptions. A show is at most 24 hours; "every day from 00:00 to 24:00" is the only 24-hour show. Equal
  start and end times are refused instead of meaning 24 hours.
* **Plan items are estimates.** `est_seconds` is not the length of any real audio; songs are modeled at 150 to 420 seconds. Blocks drift from the estimates as real audio plays.
  Blocks that start part-way through an hour use a scaled recipe that is this lane's own design (the preview only shows full hours).
* **Spots and updates are one break per hour** (scaled for part hours). How many spots fit, which ones, and whether an update has anything to say is for the ads and research lanes.
* **Damaged overlapping data** is resolved by "later start wins", then by id. The extension never saves an overlap, so this only matters for hand-edited or restored files.
* **Names** cannot contain `< > [ ] { } | \` or invisible characters, which also rules out some legitimate names (for example "Late Night [Live]"). That is deliberate: a name can never carry a speech tag.
* **`status()` is not strictly O(1)** (section 4.3). It is bounded, lock-free and cached.
* **The day map in `GET /week` is the idealized week** (24 wall-clock hours a day). It does not show the 23- or 25-hour days of a clock change; `shows.plan` and `status` do.
* **A show's id cannot be changed.** To rename the id, create a new show and remove the old one (the name can be changed freely).
* **One process.** The shows file is private to the station process (the station already refuses a second process on the same data folder); nothing here coordinates several writers.
* **Hour numbers must be whole numbers.** `"start": 6` is 06:00, but `6.0` and `6.5` are refused (use `"06:30"`).
* **`GET /plan` for a full week is 1.5 MB.** Ask for fewer hours unless you need them.
* **Unix only**, because the station is (it uses `fcntl`).
* The ids of plan items are reproducible, not random uuid4 values, by design.
