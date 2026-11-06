# Lane: ads (sponsor messages, "spots")

Package `myradio/ads/`, tests `tests/test_ads*.py`, this file. Loaded by `myradio.extensions.builtin_extensions()` as the extension named `ads`.

## 1. Purpose and honest status

**Purpose.** Let an operator, the Operator chat, or an outside agent run sponsor messages and the station's own promos: define campaigns with schedule and frequency rules, choose the messages for an ad break (and say why others were left out), keep an honest as-run log of what actually started on air, and report plays against what was booked. Spoken (written-script) spots and any paid use are held behind two confirmations that only an authenticated HTTP route can record.

**Real (executed in this session, results in section 7).**

- The pure selection engine `build_break`: every limit, ranking, budget, house fallback, determinism and an independent 300-station invariant sweep. Every limit that depends on time is judged at the moment each message would start (the break's start plus the messages ahead of it), checked by a second independent check on 700 random breaks.
- Flight, day, hour and zone arithmetic, including New York spring-forward and fall-back days, a 30-minute change (Lord Howe), Tokyo, Auckland, Kolkata, London and Los Angeles.
- The SQLite store: migrations, persistence across a reopen, retention, uniqueness.
- Reports against hand-computed numbers; CSV round-tripped through Python's `csv` module.
- The extension inside the real `ExtensionHost`, and inside the real `Station` and the real loopback `Server` (port 0): real playout ticks produce real `track_started` / `track_finished` events from imported silent WAV files marked `ad`; the as-run log, reports, the authentication of `POST /acknowledge`, a three-session restart that recovers plays from the station's own persisted history, a recording that was played from the library before any spot used it (not credited to the spot made afterwards), and a station switched off in the middle of an ad that then plays it again (one cut-off play and one complete play, not two plays).
- A real server process (`python3 -m myradio.server --no-browser --port 0` on a throwaway data folder, with every built-in extension loading) was started once, exercised over loopback HTTP (extension running, the nine operations in the catalog, the confirmation refused as an operation and accepted on its route, the preview, the CSV header) and then stopped; this was a one-off check by a script kept in the scratchpad, not a test in the suite. It was made before the independent review and was not repeated after the fixes; the real-`Station` and real-`Server` tests above cover that ground now.
- Hostile input: the suite feeds random junk to the parser (3,000 inputs), the operations (1,000 calls), the routes (800 requests) and the event handler (800 events); a larger scratch run (60,000 parser inputs and 6,000 operation calls over 40 and 10 seeds) was also clean. The fuzzing found one real bug (an unhashable `frequency` value crashed the parser with a raw error); it is fixed and is a regression test now.
- Small deliberate breakages of the code ("mutations") were each caught by at least one test, except changes that cannot be observed (see section 7). After the independent review, the six breakages the reviewers found surviving and one for each fix made in response were run again (section 7).

**Verified only against fakes.**

- The host-level tests (`test_ads_extension`, `test_ads_events`, `test_ads_hostile`) use a `FakeStation` that only answers `status()` with a library, a history and a name. Library durations, availability and history rows there are fabricated.
- Reconciliation was exercised on fabricated history rows and on the real station's persisted history (the restart tests, and the two scenarios added after the review).

**Unverified / not done.**

- **No audio was generated and no model ran.** `ads.spot_speech` only prepares the words and `ads.spot_audio` only links a finished library track. Real Drama 3 speech for spots, and the lane that would call these, were not integrated or run. A written spot has no sound until another lane makes it.
- Nothing here proves a listener heard anything. A "play" is the station's own audio clock starting an ad track. Local playout, the browser monitor, the source connection and a listener's decode are different facts and none of the last three is measured.
- The code and tests were run on Python 3.12.3, on 3.11.15 (the interpreter at `~/.local/bin/python3.11`, on the path here) and on the project's `.venv` 3.13.14 with `-W error::ResourceWarning`; all passed (section 7). No 3.10 interpreter is available here, so 3.10 is covered only by parsing every source and test file with `ast.parse(..., feature_version=(3, 10))`, which found nothing. macOS was not tried.
- The wording of the two confirmations follows the licence notes given in the task (voice model: no hosted voice service, no copying a real person without consent; music model card: CC BY-NC 4.0). It has not been reviewed by a lawyer and is a reminder, not legal advice.
- No browser/UI exists for this lane yet; section 5 is the contract for the UI work.

## 2. Public API

Everything is standard library only. Plain-language failures are `myradio.ads.model.AdsError` (a `ValueError`) in the pure modules; the extension turns them into `myradio.extensions.ExtensionError` with the same message.

### `myradio.ads.engine`

```python
build_break(now: float, budget_seconds: float, context: BreakContext, seed="") -> BreakPlan
```

Pure and deterministic: it reads only its arguments (no clock, files, network or global random numbers), and the result does not depend on the order campaigns or history arrive in. `now` is the epoch second the break is expected to start; `budget_seconds` is clamped to 0..600 and the chosen messages never add up to more (`math.fsum`, no tolerance: an exact fit is allowed). Raises `AdsError` for a non-finite number, a time outside years 1970..9999, or a context of the wrong type.

| Type | Fields |
| --- | --- |
| `BreakContext` (frozen) | `campaigns`, `history` (`Play`s: recent plays plus plays already planned), `delivered_totals` (campaign id -> plays delivered so far), `audio` (track id -> `Audio`), `show_id=None`, `max_per_break=3`, `max_per_hour=8`, `voice_rights=False`, `commercial_use=False`, `fill_with_house=False`, `ready_only=True` |
| `Play` | `campaign_id`, `creative_id`, `at` (epoch s), `category=None`, `interrupted=False` |
| `Audio` | `duration` (s), `available=True` |
| `BreakPlan` (frozen) | `at`, `seed`, `show_id`, `budget_seconds`, `seconds_used`, `items`, `skipped`; `.creatives` (= `items`), `.lines` (plain sentences), `.to_dict(skipped_limit=60)`; iterating, `len()` and indexing read through to `items` |
| `BreakItem` | `position` (1-based), `campaign_id`, `campaign` (name), `advertiser`, `house`, `creative_id`, `title`, `kind` (`recorded`/`script`), `track_id` (None while a written message has no recording), `duration`, `estimated` (length is a reading-time guess), `ready`, `offset` (seconds into the break), `why`, `role` (always `"spot"`) |
| `Skip` | `campaign_id`, `campaign`, `reason`, `structural`, `text` (a plain sentence), `creative_id` (set when only one message of a spot was left out), `detail` |

Rules, in order. Fixed checks (first failure is the reason): paused, flight dates, weekday, hours, show, paid-use confirmation (paid spots only), voice-rights confirmation (written messages), recording exists and is playable (a written message with no recording is `audio_not_ready` unless `ready_only=False`). Passing spots are ranked by priority (higher first), then furthest behind its even pace (or, for `asap`, most plays still booked), then longest since its last play, then a seeded hash of the campaign id. Each is then tried against: plan already met, plays in the last 60 minutes, in the local day, in the Monday-to-Sunday week, minimum gap since its own last play (either side of the message's start), minimum gap from the same category (the larger of the two spots' settings, so it is symmetric; messages already chosen for this break count), even pace, most spots in a break, most spots in an hour, seconds left. Station promos ("house") are tried only if no sponsor message was chosen, or when `fill_with_house=True`, under exactly the same limits. A spot appears at most once per break and rotates through its messages (least recently played first).

**Where a message starts.** A message goes at the end of the break, so it starts at `now` plus the seconds of the messages already chosen (its `offset`). Every limit that depends on time is judged at that moment, not at the start of the break: the plays in the hour before it (spot limit and station limit, counting plays already planned for later but before it), the gap to its own plays on either side, and the gap to similar spots (history, planned plays and the messages ahead of it in this break). So two similar spots that ask for a 5-minute gap are at least 5 minutes apart inside one break too. And a spot that must keep 20 minutes from its own plays is not placed 60 seconds into a break when one of its plays is planned 20.5 minutes from now (that would be only 19.5 minutes apart); placed first, it would be 20.5 minutes ahead and is fine. Dates, weekday, hours, shows, the day and week counts and the even pace are still judged once, at the start of the break (see section 9).

Skip reasons (`structural` ones only mean "not scheduled for this moment"; they are explained in a preview but never counted as "held back" in reports):

| Reason | Structural | Meaning |
| --- | --- | --- |
| `paused`, `not_started`, `ended`, `wrong_day`, `outside_hours`, `wrong_show`, `house_standby` | yes | not scheduled now (`house_standby`: a promo waits while a sponsor plays) |
| `voice_rights_needed`, `commercial_ack_needed` | no | a confirmation is missing |
| `no_creative`, `audio_not_ready`, `audio_missing`, `audio_unavailable`, `timezone_unknown` | no | nothing playable, or its timezone is not known to this computer |
| `plan_met`, `hour_cap`, `day_cap`, `week_cap`, `too_soon_campaign`, `too_soon_category`, `ahead_of_pace`, `no_room`, `break_full`, `hour_limit_station` | no | a limit |

### `myradio.ads.model`

```python
parse_campaign(raw, *, today: date, timezone="UTC", station_name="this station", campaign_id=None,
               own_creatives=frozenset(), paused=False, new_id=..., check_zone=True, check_names=True) -> (Campaign, warnings: list[str])
```

Validates and normalises a campaign object (see the "campaign object" table in section 4). Frozen dataclasses: `Campaign`, `Creative`, `Flight`, `Caps`, `Separation`, `Plan`. `Campaign.to_dict()` returns the shape `parse_campaign` accepts, so a stored spot can be sent back as it is. `check_zone=False` and `check_names=False` are for reading stored spots back (a timezone the computer has lost, or a name saved before a naming rule existed, must not make a spot vanish). With `check_names` a spot with a written message is refused when its sponsor name has no letters or numbers left once speech markers are removed (`sponsor_name_ok`), because the sponsorship line would name nobody. Spoken-script helpers: `DEFAULT_DISCLOSURE` (`"This message is sponsored by {sponsor}"`), `validate_disclosure(template, sponsor)`, `disclosure_ok(rendered, sponsor)`, `render_disclosure(template, sponsor)` (always returns a sentence, so a preview never fails; `ads.spot_speech` refuses a nameless sponsor instead), `spoken_text(sponsor, copy, template=None)`, `neutralize_tags(text)`, `estimate_seconds(text)`. Also `clean_text`, `zone_for`, `count_weekdays`, `FREQUENCY_PRESETS`, and `house_name_ok(advertiser, station_name)` (the station-promo naming rule used by `ads.campaign_save`, section 4.1).

### `myradio.ads.flights`, `reports`, `words`, `store`, `terms`, `extension`

- `flights`: `window_state(campaign, now, show_id) -> Window`, `campaign_state(campaign, now) -> "paused"|"running"|"scheduled"|"ended"`, `next_change(campaign, now)`, `day_bounds(day, zone)`, `week_bounds(day, zone)`, `pace_target`, `expected_by_now`, `day_fraction`, `active_days_total`, `active_days_before`, `is_active_day`.
- `reports`: `build_report(store, campaigns, *, now, first=None, last=None, campaign_id=None, max_days=366, max_cells=20000) -> dict`, `report_csv(report) -> str`, `csv_cell(value) -> str`, `csv_header()`, `planned_for_day(campaign, day)`.
- `words`: `skip_text`, `plan_lines`, `schedule_text`, `REASON_LABELS`, `STRUCTURAL`.
- `store.AdsStore(conn, steps=STEPS)`: the SQLite layer (callers hold `store.lock` around read-then-write).
- `terms`: `TERMS_VERSION`, `TERMS` (the confirmation wording; shown only by `GET /acknowledge`).
- `extension`: `build() -> AdsExtension`.

## 3. Persistence

Everything lives in the extension's own folder, `<data>/ext/ads/` (mode 0700).

| File | Contents | Back up | Secret |
| --- | --- | --- | --- |
| `ads.sqlite3` (mode 0600, WAL: `-wal` and `-shm` appear while running and are checkpointed away when the extension stops) | campaigns, messages, the as-run log, planned plays, held-back counts, the confirmation log | **yes** (with the service stopped) | no |
| `config.json` (optional, written by the operator) | `timezone` (default for new spots, IANA name), `max_spots_per_break` (1-20, default 3), `max_spots_per_hour` (1-100, default 8) | yes | no |

Nothing in this lane is a credential. The confirmations (with their time and wording version) are in the database and are part of the backup on purpose: they are the audit trail. `persistent_files()` returns `["ads.sqlite3", "config.json"]`.

Tables (`PRAGMA user_version` = 2; `PRAGMA foreign_keys=ON`):

- `campaigns` (id, name, advertiser, category, house, priority, pacing, timezone, `settings` JSON for flights/caps/separation/plan/frequency label, paused, `commercial_ack_id`, created/updated/removed times). Removal is soft: the row stays so past reports keep their names.
- `creatives` (id, campaign, position, kind `recorded`/`script`, title, `track_id`, body, disclosure template, voice, `removed_at`). A partial unique index allows a library track to belong to one live message only.
- `acknowledgements` (id, kind `voice_rights`/`commercial_use`, acknowledged 0/1, wording `version`, time). Every change is a new row, so history is kept.
- `plays` (the as-run log: campaign, creative, track, title, category snapshot, `paid`, `spoken`, `started_at`, `ended_at`, `outcome` `finished`/`interrupted`/NULL, `detail`, `source` `event`/`reconciled`, links to the planned play and to the confirmations in force). `UNIQUE(track_id, started_at)`. `ended_at` is NULL exactly while the ending has not been seen; a play closed because the station restarted carries the moment the restart was noticed.
- `planned` (plays chosen for coming breaks that have not aired yet).
- `skips` (day, campaign, reason, count: how often a due spot was held back).
- `attachments` (step 2: message id, the recording attached to it and when it was attached; set when a spot is saved with a new or different recording and by `ads.spot_audio`, kept while the same recording stays, forgotten when the recording or the message goes). A play of a library file found afterwards in the station's history is only credited to a spot if it started after the spot was made and after the recording was attached to its message.

Migrations use the seam's `migrate()` (steps applied once each, newer data refused with "written by a newer myradio"). Each step is a single transaction written to be safe to run twice (`IF NOT EXISTS`, `INSERT OR IGNORE`), so running one again after a crash between the script and the version number is harmless (tested: version reset to 0 on a filled database, data intact after the reopen; and version reset to 1 with the step-2 table filled in, no time reset). Step 2 gives every existing recording the day its spot was made as its attachment time (tested on a database dropped back to version 1). A step list longer than the shipped one is applied to an older file with its data intact, and a file from a newer version is refused (tested).

Retention: plays and held-back counts 800 days, planned plays 2 days (and at most 500), confirmation log trimmed above 1,000 rows (never rows that a play or a spot points to), 2,000 removed spots (oldest forgotten with their history). The cut-off is worked out from the earlier of today and a day after the newest thing ever written, so a computer whose date is far ahead (a dead clock battery, a restored virtual machine) cannot delete the history; with a normal clock this is the same as today. A saved spot that cannot be read back (damaged row) is skipped and counted (`checks()` warns) instead of stopping the extension; a spot whose timezone the computer has since lost stays visible and says so.

## 4. Extension surface

### 4.1 Operations (all validated by the host first; typed arguments)

There is deliberately **no operation that sets a confirmation**, and `campaign_save` rejects any field it does not know, so a confirmation cannot be smuggled in with a spot.

| Operation | Arguments | Result |
| --- | --- | --- |
| `ads.campaign_save` | `campaign` (object, 64 KB max, see the table below). Include `id` to change a spot: only the fields sent change, except that lists (`flights`, `creatives`) are replaced whole; a new `frequency` replaces the limits the old preset gave unless limits are sent too. A written message keeps its voice recording only while the words it was made from stay the same (see "Recordings and edits" below). | `{created, warnings[], campaign: <view>}` |
| `ads.campaign_get` | `campaign` (id) | `{campaign: <view>}` |
| `ads.campaign_pause` / `ads.campaign_resume` | `campaign` (id) | `{campaign, name, state}`; asking twice is harmless |
| `ads.campaign_remove` | `campaign` (id) | `{campaign, name, removed: true}` (history stays in reports) |
| `ads.break_build` | `seconds` (1-600), optional `show_id`, `at` (ISO-8601 with offset, from an hour ago to two days ahead), `seed`, `record` (default **false**), `ready_only` (default true) | the plan (`BreakPlan.to_dict`) plus `break_id` (null unless recorded) and `recorded`. Never queues anything. With `record: true` the plan is remembered so the next break counts it, and the reasons spots were held back feed reports; the default seed is `"<show_id>\|<five-minute bucket>"` so a preview and the real break agree. |
| `ads.spot_speech` | `campaign`, `creative` | `{role: "spot", campaign, creative, sponsor, text, voice, estimated_seconds, includes_sponsorship_line: true}`. `text` is the sponsorship line first, then the copy, with `<\|speaker:N\|>` markers removed and `[event tags]` made harmless. Refused until voice rights are confirmed (and, for a paid spot, paid use). |
| `ads.spot_audio` | `campaign`, `creative`, `track_id` | `{campaign, creative, track_id, duration}`. Links a finished library track (kind `ad`) to a written message. Same gates as `spot_speech`. |
| `ads.report` | optional `campaign`, `from_date`, `to_date` (YYYY-MM-DD) | the report object (see `GET /report`), limited to 31 days and 200 spot-days (about 40 KB: it is read by a model; ask for one spot, or fewer days, when it is refused as too large) |

Campaign object accepted by `ads.campaign_save` (unknown keys anywhere are refused with the key's name):

| Field | Meaning |
| --- | --- |
| `id` | existing spot to change (32 hex). Leave out to create. |
| `advertiser` | who it is for: a name, or `{name, category}`. Required unless `house`. |
| `name` | label (default: the advertiser). `category`: short tag for keeping similar spots apart (lower-cased to a slug). |
| `house` | true for the station's own promo. No paid-use confirmation, so it may carry **only the station's own name** (capitals and spacing ignored; no `advertiser` means the station's name). A business cannot be filed as a promo, neither when the spot is made nor by editing a paid spot: that is refused with "A station promo is named after your station. If a business is paying for this spot, turn "station promo" off, so it counts as a paid spot." A promo saved under an earlier station name keeps that name while it is left alone. Only the name is checked, not what the recording or the copy says (section 9). |
| `priority` | 1-10 (default 5). `pacing`: `even` (default) or `asap`. |
| `frequency` | `light`, `regular` (default; `light` for promos), `often` or `custom`: presets for the limits below. |
| `timezone` | IANA name; default from `config.json`, else UTC. |
| `flights` | list (<=12, non-overlapping, only the last may be open-ended) of `{start: "YYYY-MM-DD" (default today in the spot's zone), end (inclusive; null = no end), days: "1,2,3,4,5" (ISO, Monday=1), dayparts: [{from: "HH:MM", to: "HH:MM" or "24:00"}] (<=6, no crossing midnight), show_ids: [slug]}`. |
| `caps` | `{per_hour, per_day, per_week}`: most plays in any 60 minutes, in a local day, in a Monday-to-Sunday week. Missing = the preset; explicit `null` = no limit. |
| `separation` | `{campaign_minutes, category_minutes}`: least gap between plays of this spot, and from any spot of the same category. |
| `plan` | `{total: n}` (spread evenly over the playable days of closed flights, or as soon as possible with `asap`) or `{per_day: n}`. Plays stop when the plan is met. |
| `creatives` | 1-10 of `{kind: "recorded", track_id (a library track of kind ad)}` or `{kind: "script", text (<=400), voice (persona slug), disclosure}`; optional `id`, `title`. |
| `paused` | kept as it was when not sent. |

The sponsorship line (`disclosure`, default `This message is sponsored by {sponsor}`) may be edited only to wording that still contains the sponsor's name (as a whole word, or `{sponsor}`) and the word "sponsored" (the sponsor's own name cannot supply it), and that does not deny it ("not sponsored by", "unsponsored", "never ...sponsored", "without being sponsored"). A blank line means the default; no line cannot be saved. If a stored line stops passing (the sponsor was renamed), the default is read instead, so a spoken spot never goes out without it. The sponsor's name must also be something the voice can say: a written spot whose name is empty once speech markers are removed (`<|speaker:1|>`, `[ ]`, `...`) is refused with "The voice can't read that name aloud, and a spoken message has to say who is paying. Use a name with some letters or numbers in it, for example the name of the business." A recorded spot is not held to that, because nothing is read aloud.

**Recordings and edits.** A voice recording is one reading of one exact text. When a spot is saved with an `id`, each written message that already had a recording is compared with what is now sent, by the text the voice would read (the sponsorship line with the sponsor's name, then the copy) and by its voice. If those are the same, the recording stays, **whether or not the sender sent `track_id` back** (so a change of title or priority never silently unlinks it, and an omitted `voice` keeps the voice it was made with). If the words, the sponsorship line, the sponsor's name or the voice changed, the recording is taken off **even if the sender sent `track_id` back** (an editor sends a message back as it was read), the message goes back to "The voice recording isn't made yet.", and `warnings[]` says so in plain words: `The words for "<title>" changed, so its voice recording was taken off. Make a new recording for the new words.` A *different* library recording named in the same request is a new recording and is kept. A recorded message turned into a written one does not become that recording's voice either. The old file stays in the library and can be attached again with `ads.spot_audio`.

Campaign view (`campaigns[]` in `GET /campaigns`, and `campaign` in the operation results): the object above plus `state` (`running` = inside a flight's dates, `scheduled`, `paused`, `ended`), `schedule_text` ("Weekdays, 7 am – 7 pm, Nov 12 to Dec 31"), `plays_last_7_days`, `last_played_at`, `created_at`, `waiting_for` (plain sentences naming a missing confirmation), `problem` (plain sentence or null), and per message `{id, kind, title, track_id, ready, problem, duration}` plus, for written messages, `text`, `disclosure`, `voice`, `spoken_text` (exactly what the voice would read) and `estimated_seconds`.

### 4.2 Routes (under `/api/x/ads`, authenticated and same-origin by the host)

| Route | Request | Response |
| --- | --- | --- |
| `GET /campaigns` | optional `campaign=<id>` | `{campaigns[], now, acknowledgements, limits: {max_spots_per_break, max_spots_per_hour, max_spots}, timezone, problem}` |
| `GET /break` | `seconds` (1-600, default 60), `show_id`, `at`, `ready_only` | the plan plus `break_id: null`, `recorded: false`. Never queues anything and never records the break. Like every screen that reads the library it may first catch up on plays the live signal missed (the same look at the station's history as a report makes, at most every 30 s), and that can add recovered lines to the as-run log. |
| `GET /report` | `campaign`, `from`, `to` (dates; default the last 7 days in each spot's zone; at most 366 days and 20,000 spot-days) | `{from, to, generated_at, campaigns[], reasons{code: label}, notes[]}`; each campaign: `{id, name, advertiser, house, removed, timezone, plan, from, to, totals, days[]}`; `totals` and each day: `{plays, interrupted, recovered, unconfirmed, planned, delivered_share, held_back{reason: n}, held_back_total}` (`date` per day); `unconfirmed` is how many of the `plays` have no ending on record (still on air, or the end signal was missed): they are counted as played and shown so nobody has to guess; one-spot reports add `plays[]` (<=200 lines: `at`, `title`, `outcome` `finished`/`interrupted`/`unconfirmed`, `detail`, `source` `live`/`recovered`) |
| `GET /report.csv` | same query | `text/csv; charset=utf-8`, CRLF line endings, one row per spot and day. Columns: `Date, Spot, Advertiser, Type, Plays, Cut off, Recovered, Planned plays, Delivered %, Held back (total)`, one `Held back: <reason>` column per reason, then `Ending not confirmed` and `Basis`. `Basis` says `Station plays (not listeners)` on every row, so the file explains what a play is after it has been passed on (the notes of the JSON report are not in the file). Text that a spreadsheet could run as a formula (starting with `=`, `+`, `-`, `@`, tab, carriage return, line feed, their full-width forms, or whitespace followed by them) gets a leading apostrophe; cells with commas, quotes or line breaks are quoted. Numbers are never prefixed. |
| `GET /acknowledge` | none | `{version, voice_rights: {title, text, acknowledged, at, needs_reconfirm}, commercial_use: {...}}`. The only place the wording is served. |
| `POST /acknowledge` | JSON, at most 1 KB: `{voice_rights: true\|false, commercial_use: true\|false, version: <wording version>}`; at least one kind; nothing else; booleans only | `{ok: true, ...the same as GET}`. `true` records the confirmation with the current time and wording version, `false` withdraws it; repeating a state changes nothing. **`version` is required whenever a kind is set to `true`** (it is the wording version the screen showed, from `GET /acknowledge`), and a `version` that is not the current one is refused for any request, so a screen cannot confirm wording it has not shown. A withdrawal needs no version. The newest request wins whatever the computer's clock says: a withdrawal made after the clock was put back still closes the gate. |

Errors are `400 {"error": "<plain sentence>"}`. Any other failure is the host's generic 500 with no internals (checked: the fuzz tests accept only 200 or 400).

### 4.3 Status shape (`station.status()["extensions"]["ads"]["status"]`)

```json
{"running": 1, "scheduled": 0, "paused": 0, "ended": 0,
 "campaigns": [{"id": "...", "name": "Corner Bike Co.", "state": "running", "house": false}],
 "campaigns_total": 1,
 "acknowledgements": {"voice_rights": {"acknowledged": true, "at": "2026-11-05T15:00:00+00:00", "needs_reconfirm": false},
                      "commercial_use": {"acknowledged": false, "at": null, "needs_reconfirm": false}},
 "last_play_at": null, "plays_logged": 0, "problem": "only present when the log could not be written"}
```

At most 25 campaigns are named, and fewer when their names are long (the names share about 3,000 characters), so the whole entry stays under the 4,000 characters the station's assistant will read (it replaces a longer status with `{"truncated": true}`, and with it the ids). `campaigns_total` always counts all of them; the rest are reachable with `ads.report` or `GET /campaigns`. After a restart `last_play_at` is the newest play in the log. The legal wording is never in the status (tested against the wording itself). `status()` answers from memory: it never calls `station.status()`, takes no lock and touches no database (tested: the fake station's call counter does not move over 500 calls, and a call returns while another thread holds the extension's lock). The counts are recomputed in memory (at most 200 campaigns) only when the list changes or a flight starts or ends; a clock set backwards is noticed.

### 4.4 Doctor checks (`ads.<id>`)

`spots` (ok: how many spots exist, or "No spots are set up. That's fine..."), `voice_rights` (warn: spoken spots exist but are not confirmed), `commercial_use` (warn: paid spots exist but are not confirmed), `timezones` (warn), `storage` (warn: a write failed or saved spots could not be read back), `settings` (warn: `config.json` problem). If the extension cannot start, the host reports `ads.start` as `fail` with a plain message (damaged file, or data from a newer version).

### 4.5 Events consumed

`track_started` for tracks of kind `ad` that belong to a live message: one as-run line (`source: event`) with the confirmations in force at that moment. `track_finished` closes the open line: `finished`, or `interrupted` for `skipped`, `stopped` and `playback failed` (a stop returns the ad to the queue head, so it can start again: two lines, one delivered). A finish with no start recorded (the start was missed) writes the line from the finish time minus the ad's length (`source: reconciled`) when it finished normally, unless that start was before the spot was made or before its recording was attached (below). Other events, other kinds, ads that no spot uses (counted in memory only), unknown results, malformed events and events after stop are ignored without error.

**The same play, or another one.** The same event twice is one line (`UNIQUE(track_id, started_at)`, plus a start-time tolerance of half the ad's length, at most 3 s). A start inside that tolerance of an earlier line is a repeat of it only while that line has no end on record or the new start is not later than its end. An ad that was stopped (the end is on record) and started again a moment later is a second play of its own, so `stopped` after 1 s and started again after 2.5 s gives one cut-off play and one delivered play.

**A station that stops in the middle of an ad.** When the extension starts, after the look at the station's history (which may close plays properly), every play that still has no ending is closed as `interrupted` with the detail `station restarted`: nothing can be on air as the station starts. The ad that then plays again from the top is a new play, so the report shows one cut-off and one complete play instead of two plays.

**Reconciliation** (`_reconcile`: at start, and whenever the station's library and history are fetched, which every operation that needs them and every report does, and the list and preview screens do at most every 10 s; never more often than every 30 s): compares the station's own history (`status()["history"]`: title, ISO time, event; newest 30 rows) with the as-run log. A `playing` row whose title belongs to exactly one library track that belongs to a live message becomes a `reconciled` line unless a line for that track within the tolerance already exists; `finished`/`skipped`/`stopped`/`playback failed` rows close the open line. **A row is only credited to a spot if it started at or after the moment the spot was made and at or after the moment that recording was attached to the message** (`attachments`): a recording test-played from the library before the spot existed, or before a written message got its voice recording, was not a play of the spot, and is not reported as one (nor as a paid play "without confirmation"). Running it any number of times adds nothing new. Ambiguous titles (two library tracks with the same title, any kind) are left alone, never guessed.

### 4.6 How the director and the generation lane should use this lane

1. **Look ahead (no side effects):** `ads.break_build {seconds, show_id, at, ready_only: false}`. Items with `ready: false` and `kind: "script"` need a voice recording.
2. For each: `ads.spot_speech {campaign, creative}` returns the exact `text` and `voice` (refused until voice rights are confirmed). Submit a generation job with role `spot` and that text; text from this operation contains no speaker markers or event tags, so a speech formatter may add its own. Import the result with `station.import_audio(data, title, artist, "ad")`, then `ads.spot_audio {campaign, creative, track_id}`.
3. **At air time:** `ads.break_build {seconds, show_id, at, record: true}` (the default `ready_only: true` lists only what can play now), then `queue.add` each `items[].track_id` in order. This lane never queues by itself.
4. The as-run log needs nothing from the director: it follows `track_started` events, so plays queued by hand count too.

An audio file imported as an ad outside `spot_speech` / `spot_audio` is treated like any other recorded spot: this lane cannot know how it was made.

## 5. UI contract (the Spots section of the Team view in the design)

Render every string from this lane as text, never as HTML (names are operator text). Poll `GET /campaigns` when the view opens and after each action, and `GET /break` at most every 10 seconds while the card is visible.

**Spot rows** (one per `campaigns[]`): the name; a badge `RUNNING` / `SCHEDULED` / `PAUSED` / `ENDED` (never colour alone); a meta line `<Frequency> · <schedule_text> · <plays_last_7_days> plays in the last 7 days`; the first written message's text in quotes with the hint `Always read with "sponsored by".`; for a recorded message its title. Buttons: `Pause` / `Resume`, `Remove` (confirm in the page: "Remove the spot for <name>? Its past plays stay in your reports." then `ads.campaign_remove`). If `waiting_for` or `problem` or a message `problem` is set, show it under the row in plain words with its next step (below). Empty state: **"No spots yet. Add one below."**

**Add a spot** (the form in the design) maps to one `ads.campaign_save`:

| Screen | Field |
| --- | --- |
| "Who is it for?" | `advertiser` |
| "How often?" Light / Regular / Often | `frequency` |
| "What should it say?" (up to 400 characters) | `creatives[0] = {kind: "script", text}` |
| "Say that it's sponsored" (always on, disabled) | nothing is sent: the line is always added |
| "I'm allowed to use this voice" and, for paid spots, "I'm allowed to run paid spots" | two separate confirmations, see below |
| "Or use a recorded spot from the Library" | `creatives[0] = {kind: "recorded", track_id}`; list only library tracks of kind `ad` |
| (advanced, later) days, hours, dates, shows, plan | `flights`, `plan`, `priority`, `category` |

Send the station's own timezone as `timezone`; without it the spot uses UTC days.

**Confirmation sheets.** Fetch `GET /acknowledge`; show each `title` and `text` unchanged, a switch "I have read this and confirm", the date it was confirmed (`at`), and a `Withdraw` button (in-page confirm) that posts `false`. Post `{"<kind>": true, "version": <version>}` only from the person's click; the request is refused without the `version` that `GET /acknowledge` gave. If `needs_reconfirm` is true say "This note was updated. Please read it again." The voice note is required before any written message; the paid-use note before any non-promo spot. Recorded promos need neither.

**Next to the spot list, "What would play next"**: show `lines` from `GET /break` as a short list; the first line is the headline. Empty states come from the server ("You have no spots yet. Add one to fill your breaks." / "No spot is scheduled for this time."). Offer a seconds selector (30 / 60 / 90 / 120) and the current show.

**Reports.** A table per spot and day with the plain column names of the CSV (`Plays`, `Cut off`, `Planned plays`, `Delivered %`, `Held back (total)`, `Ending not confirmed`), the `notes` underneath unchanged (they say what a play does and does not prove), and `Download spreadsheet` which fetches `GET /report.csv` with the same query and saves it as a file. A figure that is `null` is shown as a dash, never as 0. "Held back" explains: "Times a spot that was due was left out of a break, by reason." "Ending not confirmed" explains: "Plays that started but whose end was never seen. They are counted as played."

**Error wording.** Show the server's sentence as it is, in the form's error area. Do not wrap it in codes. These have a next step the screen should offer:

| Server sentence starts with | Offer |
| --- | --- |
| "Paid spots need one confirmation first..." | open the paid-use sheet, then retry |
| "A station promo is named after your station..." | switch "station promo" off (it is a paid spot), or put the station's name back |
| "The voice can't read that name aloud..." | ask for a name with letters or numbers in it |
| "Spoken messages stay on hold until..." | open the voice sheet, or switch the message to a recording |
| "That recording isn't in your library..." / "...not as an ad" | open the Library import with kind Ad |
| "The station isn't answering right now..." | "Try again in a moment" (do not retry automatically) |

A save can also come back with a warning (not an error): `The words for "<title>" changed, so its voice recording was taken off. Make a new recording for the new words.` Show it under the message, with the way to make a new recording.

Messages that sit on a spot: `Waiting for your paid-use confirmation.`, `Waiting for your voice-rights confirmation.`, `The voice recording isn't made yet.` (if the station's `capabilities.speech_generation` is false, add: "The voice maker isn't set up, so this written spot can't play yet. Set it up in Settings, or use a recorded spot."), `The recording is no longer in your library.`, `The recording can't be played right now. Check it in the Library.`, `This spot uses a timezone this computer doesn't know. Edit it and pick another.`

**Next-step bar** hints from status: no campaigns -> "Add your first spot"; `acknowledgements.*.needs_reconfirm` -> "Read the updated note"; a spoken spot waiting -> "Confirm voice rights"; `problem` -> show it.

## 6. Limits and bounds

| Item | Bound |
| --- | --- |
| Spots (not removed) | 200; removed kept 2,000 |
| Messages per spot / flights / hours windows / shows per flight | 10 / 12 / 6 / 50 |
| Text lengths | name 60, advertiser 80, copy 400, sponsorship line 160 |
| Flight length | 10 years; plan total 100,000; plan per day 1,000; caps 1..100,000; gaps 0..1,440 minutes |
| A campaign argument | 64 KB (host); `POST /acknowledge` 1 KB |
| A break | 0-600 seconds, 1-20 spots, one per spot; station limits from `config.json` |
| History a break reads | 9 days back, 1 day ahead, plus planned plays (45-minute grace after their time) |
| Reports | 366 days, 20,000 spot-days (route), 31 days and 200 spot-days (operation, about 40 KB); 200 plays listed. The route's answer for 200 spots over 7 days is about 320 KB of JSON. |
| Status | at most 25 campaigns named, and no more than about 3,000 characters of names; the whole entry stays under 4,000 characters (tested with 60-character names) |
| Threads | none of its own; events arrive on the host's one mailbox thread for this extension |
| Memory | the campaign list (<= 200) and a library snapshot reduced to five fields per track, refreshed at most every 10 s for read-only screens |
| Time (CPU time, this machine, test harness) | Measured after the review fixes with 200 spots and about 4,200 plays in the last 7 days, on a busy machine (load average about 9), Python 3.12.3; the first figure is the median, the second the worst of 8 to 15 runs each (200 for the cached `status()`). `build_break` with 200 spots and 5,000 plays: 7.8 / 9.9 ms; saving a spot: 5.0 ms; `GET /campaigns` 4.0 / 4.7 ms; `GET /break` 19 / 31 ms; `GET /report` (200 spots, 7 days) 27 / 31 ms; its CSV 46 / 52 ms; `ads.break_build` 15 ms; `status()` 0.003 ms cached and 0.6 ms right after a change. The same script on the code from before the fixes gave the same figures within noise. Earlier versions of this document quoted lower figures for the screens (`GET /break` 4 ms, `GET /report` 18 ms, its CSV 37 ms, `GET /campaigns` 2.6 ms). They were not reproduced, here or by a reviewer, and why they differ was not investigated; treat the figures above as the measured ones. |
| Library calls | `campaign_save`, `break_build`, `spot_audio` and every report fetch a fresh `station.status()`; the list, the preview and `campaign_get` reuse one up to 10 s old; pause, resume, remove and `spot_speech` make no call |

All numbers in the last two rows were measured with relaxed `fsync` in the test harness, so a real save will be a few milliseconds slower.

## 7. Test inventory, commands and measured results

Command (as given): `cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_ads*.py" -v`.

Result: **368 tests, 0 failures, 0 errors, 0 skipped, 27 s** on Python 3.12.3 (the machine was busy, load average about 9). The same command on Python 3.11.15: **368 tests OK, 22 s**. With the project's `.venv` Python 3.13.14 and `-W error::ResourceWarning`: **368 tests OK, 26 s**. `tests/test_extensions.py` and `tests/test_extensions_station.py`: 45 tests OK. Size: 3,245 lines of code in `myradio/ads/` and 4,860 lines of tests (`wc -l`, comments and blank lines included). Of the 368 tests, 288 existed before the independent review and 80 were added by it; no test was deleted, skipped or loosened (the changes to existing tests are listed in section 10).

| File | Tests | Covers |
| --- | --- | --- |
| `test_ads_engine.py` | 65 | table of 24 limit cases (hour, day, week, gap, plan, cut-short plays, zones), categories both ways, station limits, even pace, ranking, rotation, budget (exact fit, skip-and-continue), fixed checks, house fallback, determinism and purity (shuffled inputs, patched clocks), explanations, odd inputs, 300 random stations re-checked independently, and limits judged where each message starts (the reviewers' cases, an exact-gap case, and 700 random breaks checked by a separate oracle) |
| `test_ads_flights.py` | 24 | DST spring-forward/fall-back flight edges, repeated and skipped hours, 23/25/23.5-hour days, weeks, zones (Tokyo, LA, Auckland, Kolkata), shows, even-pace arithmetic, and a property check that local days tile time exactly (280,000 quarter-hour instants over a year in nine zones, including Havana's missing midnight, Lord Howe's half-hour change and Samoa's skipped day) |
| `test_ads_model.py` | 32 | defaults and presets, 49 refusal cases (plain wording), disclosure rule (accept/refuse tables, including "without being sponsored"), speech-tag neutralization, names the voice can read, the station-promo naming rule |
| `test_ads_words.py` | 9 | times, schedules, every sentence, moments-away wording, counts of one |
| `test_ads_store.py` | 41 | migrations (new, reopen, later step, newer data, version-1 file gaining attachment times, step 2 run twice), persistence across reopen, uniqueness, soft removal, retention (and a clock far in the future), attachment times, closing plays that never ended, confirmation log (and a clock put back) |
| `test_ads_reports.py` | 21 | hand-computed week (plays 11 of 15 planned, 73.33%), total spread (30 over 10 days), zone and DST day bucketing, notes, unconfirmed endings, CSV escaping (22 hostile cells), the closing CSV columns, round trip |
| `test_ads_extension.py` | 115 | operations catalog, confirmations unreachable as operations, wording version required, gates (including a withdrawal after the clock was put back), station-promo naming, spoken-script rules, stale recordings, nameless sponsors, save/edit/pause/remove, breaks (preview vs recorded, planned plays and their times, held-back counts, shows, config), weekly caps through the whole extension (UTC and two far-off zones), routes, status (no station call, lock-free, bounded, under 4,000 characters), report size, doctor checks, lost timezone, concurrent recorded breaks |
| `test_ads_events.py` | 38 | as-run logging, idempotence, finish/skip/stop, quick stop and restart, short ads, missed start, plays from before a spot existed, restart in the middle of an ad, reconciliation (fabricated history), start-up recovery, 4 worker threads plus events, failure handling |
| `test_ads_hostile.py` | 8 | random junk through the parser, the operations, the routes and the event handler |
| `test_ads_station.py` | 15 | real `Station` and `Server`: loader, real ticks and events, skip, stray ads, playout not waiting for the extension, HTTP authentication of confirmations, action endpoint, three-session restart recovery, a recording test-played before its spot existed, a restart in the middle of an ad |
| `test_ads_support.py` | 0 | shared helpers (fake station, harness, clock) |

Other checks run (not part of the suite, scripts kept in the scratchpad): 49 single-line breakages of the code ("mutations": removed gates, off-by-one limits, a fixed 24-hour day, a UTC-only date check, unescaped CSV, missing tag removal, a lock removed, status asking the station, ...). Every one made at least one test fail except `pace uses >= instead of >`, which cannot be observed because the engine already adds a 1e-9 tolerance. A 60,000-input parser fuzz and 6,000-call operation fuzz with other seeds found no raw exception after the one fix.

After the independent review, a second run of **41 single-line breakages** was made on a scratch copy: the six that the reviewers reported as surviving (planned plays used up newest first, the 3-second duplicate window ignoring the ad's length, every message of a recorded break planned at the break's start, the "without" denial, the paid-use half of the voice gate, a 3-day history window) and 35 aimed at the fixes (each time-based limit judged at the break start again, confirmations ordered by the clock again, the station-promo rule or its exemption removed, recordings always or never kept on edit, the voice or sponsor left out of the comparison, recovered plays not held to the spot's start, attachment times not written, open plays left open after a restart, pruning trusting the clock, the nameless-sponsor checks removed, the status without its size budget, and so on). All 41 are caught by at least one test. (One of them first survived because a new status test listed the long-named spots after 45 short ones, so they were never in the named part; the test was split and the breakage re-run: caught.) The independent oracle the reviewers used for the engine (4,000 random breaks, judged at each message's own start) found 123 violations in the code before the fix and none after.

Whole suite (`cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_*.py"`), run once after the last edit: **2,788 tests in 384 s, OK**, with 1 skipped and 10 expected failures that were already in the suite and belong to other lanes (no ads test is skipped or expected to fail). No failure anywhere, including `test_agent_deadline.py` and `test_model_transport.py`. The other lanes' files were still being edited by other people at the time, so this describes that moment.

## 8. Requested changes to shared files

None are required for the lane to work. Recommended, each for its owner:

1. `myradio/extensions.py`, `Context.open_db`: close the half-opened connection when its first PRAGMA fails (for example a file that is not a database). On Python 3.13 the leftover shows as `ResourceWarning: unclosed database` (reproduced by `test_a_file_that_is_not_a_database_is_reported_in_plain_words`, which silences it locally).
2. `myradio/extensions.py`, `migrate`: the script and the `user_version` bump are separate commits, so a crash between them leaves tables created at version 0. The ads step is written to be safe to run again (tested: the version number was reset on a filled database and the reopen kept the data); other extensions' steps may not be. Wrapping each step and its version bump in one transaction would close the gap. (Reasoned from the code; the crash itself was not reproduced.)
3. `myradio/station.py`, `status()["history"]`: rows carry a title but no track id, and only the newest 30 are returned, so reconciliation matches by a title that belongs to exactly one library track and reaches back about 15 tracks. Adding `track_id` to those rows (and returning more of the 200 that are kept) would make it exact and longer.
4. `myradio/web/**` (UI shell): a Spots section following section 5. This lane ships no web assets and `panels()` returns nothing, so no dangling script is declared. Two things changed since the first version of section 5 and matter to whoever builds it: `POST /acknowledge` now refuses a `true` without the `version` that `GET /acknowledge` returned, and `GET /report` rows (and the CSV) carry `unconfirmed` / `Ending not confirmed`.
5. Director / generation lane: follow section 4.6. In particular a spoken spot's text must come from `ads.spot_speech`.
6. `myradio/chat.py` and `myradio/mcp.py`: none. The operations appear in the catalog as `ads.<name>`; `ads.campaign_save`'s argument description documents the object for a model. The brief names four operations; this lane adds five more: `break_build`, `spot_speech` and `spot_audio` (the director and the generation lane can only reach this lane through operations) and `campaign_get` and `report` (an agent cannot call extension GET routes, so this is its only way to read a spot or its numbers). Trim the list if the Operator's tool list gets too long; nothing else depends on `campaign_get` or `report`.
7. `README.md` / `docs/RELEASE-STATUS.md`: mention the two confirmations, that written spots need a speech lane to have sound, and that counts show station playout, not listeners.
8. Backup (`backup.py`): include the files from `persistent_files()` with the service stopped; the database is in WAL mode and is checkpointed when the extension stops.
9. Whoever builds the first-run flow (recipes): write `ext/ads/config.json` with the station's `timezone`; there is no settings route in this lane.
10. `myradio/extensions.py` and `myradio/server.py`: tell a route how the caller authenticated (a page of this station with its own session, or only the launch code in a header). `Request` carries nothing about it today, so `POST /acknowledge` cannot refuse a caller that holds only the launch code (an outside agent that was given it). With that flag the route could insist on a page session; until then this limit stays as written in section 9.

## 9. Known limitations and unverified claims

- Counts are what started on the station's clock. They say nothing about listeners or an outside stream.
- A written spot has no sound until another lane produces it; none was produced or heard here.
- Reconciliation reaches only the newest 30 history rows and needs a title that is unique in the library; plays outside that are lost, and a title shared by two library tracks is skipped. Ad files used by no spot are not logged.
- A recorded spot's contents cannot be checked, so the sponsorship line is guaranteed only in the words `ads.spot_speech` returns for a written spot: what the finished recording really says is not checked (there is no speech recognition here). This lane does take a voice recording off a written message when its words, voice or sponsor change, so the screen and the air agree about the words, but whether a recording that was attached matches the text it was made from is up to whoever made it. A reminder warning is returned when a paid spot has a recording. The disclosure rule is lexical (sponsor name, the word "sponsored", a small negation guard) and checks the line, not the copy around it. A recording attached before this lane learned to take stale recordings off is not detected as stale.
- "Station promo" skips the paid-use confirmation, so it is only accepted under the station's own name. That is the whole check: nothing here can tell whether a recording or a script filed as a promo is really somebody's paid message. It stops a one-field shortcut, not a determined operator.
- Confirmations are recorded through an authenticated route, which anyone holding the launch code (or an agent given it) can still call over HTTP. What this lane guarantees is that no operation, tool call or chat approval can do it.
- Pacing works on the wall clock: on the two daylight-saving days a year the share of the day that has passed can be off by up to an hour. Hours windows cannot cross midnight (use two windows). "Running" means inside a flight's dates, not inside its hours.
- A break is judged for the dates, weekday, hours, show, day and week counts and pace as they are at its start. The limits that look at the last hour and at gaps between plays are judged at each message's own start (section 2), but a break that begins a minute before a spot's hours end can still place that spot a few minutes past the end of its hours.
- A play counts as played from the moment it starts. One whose end was never seen (still on air, or the end signal was missed) is counted as played and also shown as "Ending not confirmed"; when the station starts again, plays that never ended are closed as cut off.
- Plays and recordings already in a database before the attachment times existed count from the day their spot was made; anything credited wrongly before then stays in the log.
- A plan total counts every delivered play since the spot was created, not only those inside the current flights; book a new run as a new spot.
- Station limits (most per break and per hour) and the default timezone come from `config.json`; there is no screen or route to set them yet.
- A recorded break (`record: true`) holds a planned play for 45 minutes after its time; a caller can therefore hold a spot back for that long, and at most 500 planned plays are kept.
- Reports are per spot and day; per-message figures exist in the log but not in the report.
- Library durations come from the station's library snapshot; a broken or stale snapshot (up to 10 s on read-only screens) gives a stale length.
- The fuzz and mutation results describe this code at this moment; they are evidence, not proof.

## 10. Changes made after the independent review

Two reviewers read this lane independently and reported the findings below. Each was fixed unless noted, with a test that fails on the code as it was before and passes now (checked by running the new tests against a copy of the earlier source). A few tests only add coverage that was missing (the weekly limits through the extension, the order and times of planned plays, a 2-second ad, "without", the paid half of the voice gate): they pass on both, and each was shown to fail when the code it guards is broken (section 7).

| Finding | What changed | Tests |
| --- | --- | --- |
| Time-based limits were judged at the start of the break, not where each message starts: similar spots 30 s apart, a spot placed before a play planned for later (major) | section 2, "Where a message starts"; the oracle of the 300-station sweep now judges each item at its own start | `StartOfEachMessageTests`, `InvariantSweepTests` |
| "Station promo" with a paying sponsor skipped the paid-use confirmation, new or by editing a paid spot (major, found twice) | a promo may only carry the station's own name; the paid-use refusal no longer says promos are exempt | `StationPromoNamingTests`, `SponsorNameTests` |
| A voice recording stayed on air after the words or the sponsor changed, or was dropped by an edit that did not touch the words (major, found twice) | section 4.1, "Recordings and edits" | `StaleRecordingTests` |
| Plays from before a spot (or before its recording) existed were credited to it by the look at the station's history (major) | migration step 2 (`attachments`); recovered plays are held to the spot's start | `ReconcileTests`, `AsRunLogTests`, `AttachmentTests`, `MigrationTests`, `RestartTests` (real station) |
| Weekly limits were never tested through the extension (major) | tests only: UTC, a zone far ahead and a zone far behind, the week before not counting | `WeeklyCapTests` |
| A withdrawal was ignored after the clock was put back (minor, found twice) | confirmations are ordered by when they were written | `ConfirmationTests`, `ConfirmationsAreNotOperationsTests` |
| A sponsor name made only of speech markers gave "sponsored by ." (minor, found twice) | refused on save for a written spot; `ads.spot_speech` refuses a stored one. `render_disclosure` still returns a sentence rather than failing a preview (the reviewers suggested raising; that would break the preview of a break for any spot saved before the rule) | `NamelessSponsorTests`, `SponsorNameTests` |
| "Held back" counted spots that did play through another message (minor, found twice) | only a spot that was left out is counted | `BreakTests` |
| A play whose end was never seen was counted as played with no sign of it; a restart in the middle of an ad gave two plays (minor, found twice) | plays still open when the station starts are closed as cut off; reports and the spreadsheet show `unconfirmed` / `Ending not confirmed`; the spreadsheet says what a play is on every row | `AsRunLogTests`, `CsvTests`, `HandComputedReportTests`, `RestartTests` |
| Stopping an ad and starting it again within 3 s lost the complete play (minor) | an earlier play with an end on record is not a repeat of a later start | `AsRunLogTests` |
| `prune()` trusted the clock (minor, found twice) | cut-off worked out from the newest record | `LogTests` |
| "played 0 minutes ago", "all 1 times", `last_play_at` empty after a restart (minor) | `words.distance_text`; the last play is read from the log at start | `SentenceTests`, `AsRunLogTests` |
| `version` on `POST /acknowledge` was optional (minor) | required when a confirmation is given; the document line was true only for screens that sent it | `ConfirmationsAreNotOperationsTests` |
| The status was dropped by the assistant above 4,000 characters; `ads.report` returned about 290 KB (minor) | at most 25 names and 3,000 characters in the status; 200 spot-days for the operation | `StatusTests`, `RouteTests` |
| Mutations that survived and two test nits (minor) | assertions for planned-play order, planned times, the 2-second ad, "without", the paid half of the voice gate; a fixed name instead of `hash()`; a lock around `FakeStation.add_track` | `LogTests`, `BreakTests`, `AsRunLogTests`, `DisclosureTests`, `GateTests` |
| Document: Python 3.11 was available, timings not reproduced, `GET /break` "never writes", the sponsorship line "guaranteed only for written spots", missing request about how the caller authenticated, the meaning of `house` | sections 1, 2, 4, 6, 8, 9 | n/a |
| `tests/test_station.py` shown as changed by the ownership checker (minor) | not an ads-lane file or defect; nothing changed here | n/a |

Existing tests that were changed, and why (none was deleted, skipped or loosened):

- Every test that confirms through `POST /acknowledge` now sends the wording `version` (`Harness.confirm`, five in `test_ads_station.py`, one in `test_ads_extension.py`): the route requires it now.
- `ReconcileTests` makes its two spots two hours old in `setUp` (`LogCase.made_at`), because the plays they recover are fabricated history from the last quarter hour and a recovered play only counts after its spot was made. Every assertion in them is unchanged.
- `test_a_daily_plan_over_a_working_week` expects `"unconfirmed": 11` in the totals (the test writes starts only, so none of its 11 plays has an ending), and `test_the_header_names_every_reason_in_plain_words` expects the two closing columns.
- `InvariantSweepTests.check` is stricter: hourly limits, gaps and similar spots are judged at each message's own start (`start_complaints`), and a skip reason must hold at one of the moments the message could have been weighed.
- Strengthened: `test_planned_plays_wait_to_be_matched_and_are_forgotten_after_the_grace` (which planned play is used up), the disclosure refusal table (two more rows), `test_every_spoken_message_carries_the_line_whatever_it_says` (fixed names).
