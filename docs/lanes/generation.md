# Generation lane: the generate-ahead engine for YuE2 music and Drama 3 speech

Package `myradio/generation/`, shipped as the built-in extension named `generation` (`myradio.generation.extension.build()`).
Tests: `tests/test_generation_*.py`. This file is the contract other lanes (prompts, hosts, ads, research, director, doctor, UI) build against.

---

## 1. Purpose and honest status

**Purpose.** Make songs (YuE2) and spoken audio (Drama 3) ahead of time so playout never waits for a model, without ever touching the playout clock.
Producers ask for audio with `generation.enqueue`; the engine runs each job on a worker with a hard deadline, checks and finishes the audio, imports it into the
station library with the right kind, and keeps provenance. It only makes what is asked for: it never invents jobs, and a station with no settings file simply reports
that nothing is set up. The one thing it starts by itself is a short test clip (see 4.6): the voice maker's warm-up request after start and whenever that maker has stopped
answering, the music maker's test song when `smoke_test` is true, and a test clip to prove a maker again after it has failed.

**Status in one table.** Nothing here has run a real model. Read this before relying on anything.

| Claim | Status | How it was checked |
| --- | --- | --- |
| Job store, retries, ttl, idempotency, recovery, history bound, provenance | **Real, executed** | SQLite on disk, closed and reopened in tests |
| Scheduler: resource groups, priority, loaded-worker preference, buffer marks, pause, cancel | **Real, executed** | Threads in the real engine with in-process fake makers |
| Subprocess runner: process-group kill, TERM then KILL, child cleanup, bounded output | **Real, executed** | Real operating-system processes (small Python scripts standing in for the CLI) |
| HTTP runner: absolute deadline, size cap, no redirects, loopback rule | **Real, executed** | Real sockets to a loopback server that misbehaves in about 30 ways |
| Finishing audio (check, trim, fade, level, 44.1 kHz stereo, import) | **Real, executed** | Against the real `myradio.audio` that existed at test time (see 7), and against a stand-in (switch described in 7). The tests measure the finished file itself: loudness against the target (quiet and hot takes), leading silence, fades, and each refusal (abrupt end, distortion, too long); a reviewer's list of eight deliberately broken copies of the gates is now caught |
| Extension seam: operations, routes, status, doctor checks, events, backup list | **Real, executed** | Real `ExtensionHost`, real `Station`, real `Server` over HTTP |
| DramaHttpWorker | **Verified only against a fake server** | The fake follows the request and answer in the Drama 3 model card. The real server's health address, error answers and content type are **unverified** |
| Yue2CliWorker | **Verified only against fake programs** | The example command follows the YuE2 model card. The option names for steps and length in a real build are **unverified** (see 4.7) |
| Warm-up of the first speech request (about 40 s on the real server) | **Unverified** | A fake answers instantly; the logic (a real test clip before the maker counts as ready) is tested |
| Sharing one GPU between the Drama server and YuE2 | **Unverified** | With the default of one slot per group the scheduler never runs them together; whether both fit on one card, and how the operator starts or stops each, is up to the operator |
| The station process being killed outright (SIGKILL, out of memory) | **Not handled** | A running music program has its own process group and is not stopped by that; see 9 |
| Audio quality, voice quality, speed on the target rigs | **Unverified** | No model or GPU was used, by instruction |
| macOS, Python 3.10, Python 3.13 | **Unverified** | Ran on Linux with Python 3.12.3 and 3.11.15 only. No Linux-only calls are used without a fallback |
| Long-run behaviour (days) | **Unverified** | Only short tests ran; the soak lane covers that |

**Fixtures are never real.** `kind: fixture` makes a labelled test tone. It exists only when the environment variable `MYRADIO_ALLOW_FIXTURE_WORKERS` is exactly `1`;
its output is marked as a fixture in provenance (a flag the database refuses to clear), in the library title (`[FIXTURE] ...`), in the library artist
(`FIXTURE - not real music`), in the status, and in the doctor checks, and it never makes `music_generation` or `speech_generation` true. The job's own `fixture` flag is stored with
the job when an attempt starts, so it still says so after the test maker has been taken out of the settings. After it made tones the maker's `evidence` is `test_tones` ("Test tones only;
nothing real has been made"), never `real_job_completed`, and leftover test tones stop counting as ready the moment a real maker takes over that kind of audio.

---

## 2. Public API

Everything under `myradio/generation/`. Python standard library only.

### 2.1 Extension

```python
from myradio.generation.extension import build, GenerationExtension
GenerationExtension(*, kinds=None, clock=None, probe_min_interval=10.0, env=None)
```

`build()` returns `GenerationExtension()`; `myradio.extensions.builtin_extensions()` loads it. `kinds` adds worker kinds (tests use it); `clock` replaces `ctx.clock`;
`env` is a `config.Env` for the safety checks. Settings are read from `<data>/ext/generation/config.json` through `ctx.config()` and nowhere else.

### 2.2 For producers (prompts, hosts, ads, research, director)

Ask through the station's action layer, which validates arguments the same way for the UI, the Operator and MCP:

```python
reply = station.action("generation.enqueue", {
    "role": "host_break",
    "payload": {"text": "Good evening, you are listening to Radio Four.", "speaker": 1, "title": "Evening hello"},
    "priority": 60, "ttl_s": 1800, "idempotency_key": "slot:2026-11-05T18:00:host-break"})
reply["job"]["id"]        # uuid4 hex
reply["created"]          # False when the same key already named live or finished work
job = station.action("generation.job", {"job_id": reply["job"]["id"]})["job"]
job["state"], job["track_id"], job["error"], job["hint"]
```

Helpers a producer may import (all pure functions, no I/O):

| Function | Use |
| --- | --- |
| `payloads.normalize_payload(role, payload, limits=Limits()) -> Normalized` | The exact validation `enqueue` applies. Raises `GenerationError` with a plain reason. Lets a producer check a payload without queueing it |
| `speech.neutralize_speech_tags(text) -> str` | Defuse text from outside the operator (track titles, feed items, advertiser copy): removes every `<\|...\|>` marker and turns `[tag]` into `(tag)` |
| `speech.restrict_speech_tags(text) -> str` | For the station's own scripts: keeps well-formed `<\|speaker:0..31\|>` and `[event]` tags and defuses everything else |
| `speech.split_takes(text, speaker, max_chars) -> list[str]` | The sentence splitter the voice worker uses |
| `payloads.ROLES`, `KIND_FOR_ROLE`, `window_for(role, data)` | Roles, library kinds, and the accepted length window |

**Outside text must be neutralised.** Every speech payload is run through `neutralize_speech_tags` by `enqueue` unless it carries `allow_tags: true`, in which case only
well-formed markers survive. Producers must still neutralise outside text before building a script, and should set `allow_tags: true` only for scripts the station wrote.

### 2.3 Module map and signatures

| Module | Contents |
| --- | --- |
| `extension.py` | `GenerationExtension`, `build()` |
| `engine.py` | `Engine(ctx, cfg, kinds, store, *, clock, http=None, proc=None)`: `start()`, `stop(close_store=True)`, `poke()`, `enqueue(role, payload, priority=50, ttl_s=None, key=None) -> (view, created)`, `get_job(id)`, `job_for_track(id)`, `list_jobs(**filters)`, `cancel(id, wait_s=3.0) -> (view, settled)`, `settle(id, wait_s=3.0) -> (view, settled)`, `retry(id)`, `pause(cancel_running=False)`, `resume()`, `set_buffer(role, low, high)`, `probe(full=False) -> workers`, `request_probe(full=False) -> {workers, full_check_queued, full_check_workers}`, `buffer_view()`, `workers_view()`, `status()`, `checks()`, `on_event(event)` |
| `store.py` | `JobStore(conn, clock, history=2000)`: durable jobs, provenance, settings. All methods are short transactions behind one lock |
| `config.py` | `parse_config(raw, kinds, env=Env(), present=True) -> LoadedConfig`, `check_command`, `check_base_url`, `render_arg`, `EXAMPLE_CONFIG` |
| `payloads.py` | Roles, schemas, `normalize_payload`, `window_for` |
| `speech.py` | Tag handling and sentence splitting |
| `runners.py` | `SubprocessRunner.run(argv, *, cwd, env, deadline_s, cancel=None, max_dir_bytes=None, grace_s=5.0) -> RunResult`, `HttpRunner.request(method, host, port, path, *, body, headers, deadline_s, max_response_bytes, cancel=None, allow_remote=False) -> HttpResponse`, `build_env(...)`, `tree_bytes(...)` |
| `workers_base.py`, `worker_drama.py`, `worker_yue2.py`, `worker_fixture.py`, `workers.py` | `Worker`, `GenerationRequest`, `WorkerOutput`, `ProbeResult`, `WorkerContext`; `DramaHttpWorker`, `Yue2CliWorker`, `FixtureWorker`; `KINDS` |
| `audio_api.py` | `load_audio()`, `finish(audio, pcm, *, role, min_s, max_s, spec, cancel) -> Finished`, `classify(problems, ...)` |
| `buffer.py` | `BufferPolicy`, `choose(...)` |
| `views.py` | The JSON shapes shown to screens and agents |
| `util.py` | `iso`, `short`, `free_bytes`, `finite_number` (a safe "is this a real number" test: a whole number too big for a float, legal in JSON, is a number, not an error) |

### 2.4 Errors

| Type | Meaning | Reaches a person as |
| --- | --- | --- |
| `GenerationError(ExtensionError)` | An operation or route was refused for a reason a person can act on | The message, HTTP 400 |
| `ConfigError(ValueError)` | One setting is unusable | A line in `status.config.problems` and the doctor check `config` |
| `WorkerError(code, message, retryable, detail)` | A maker could not produce usable audio | `job.error` (plain), `job.error_code`, `job.hint`; `detail` is technical and meant for a "Technical details" disclosure |
| `Cancelled` | A job was cancelled or put back in line while running | Never shown |

Any other exception inside an operation is reported by the host as a generic failure; nothing internal leaks. A number that is far too large to be sensible (even a whole number with hundreds of digits, which is
legal in JSON) is refused like any other number outside its range, in payloads, in `config.json` and in a `before` query.

### 2.5 Payloads (frozen field names; extra optional fields are listed)

Roles: `song` (YuE2) imports as library kind `music`; `host_break`, `id`, `update` (Drama 3) import as `speech`; `spot` (Drama 3) imports as `ad`.
Unknown fields are rejected, so a misspelt field never silently does nothing. A payload may not carry commands, addresses or settings of any kind.

**Speech roles** (`host_break`, `id`, `spot`, `update`):

| Field | Type | Rule | Default |
| --- | --- | --- | --- |
| `text` | string, required | 2 to 3000 characters after cleaning; needs at least a couple of spoken characters | |
| `speaker` | integer | 0 to 31, written as `<\|speaker:N\|>` | 0 |
| `language` | string | `en`, `pt-BR` style; recorded in provenance (the voice model infers the language from the text) | `en` |
| `seed` | integer | 0 to 4294967295; pins the seed. Leave out and the engine chooses and records one | none |
| `max_seconds` | number | 1 to 180; the longest accepted result, and the voice model's token limit | host_break 45, id 15, spot 75, update 150 |
| `title` | string | up to 120 characters, the library title | `Host break: <first words>`, `Station ID`, `Sponsor message`, `Update: <first words>` |
| `allow_tags` | boolean | true keeps well-formed speaker and event tags; false defuses all markers | false |
| `meta` | object | up to 16 flat entries (text, number, true/false, empty), about 2 KB; kept with the job, never interpreted | none |

**Song**:

| Field | Type | Rule | Default |
| --- | --- | --- | --- |
| `tags` | string, required | 3 to 800 characters of style description; may not start with a dash | |
| `lyrics` | string | up to 3000 characters; empty or absent means instrumental. Needs at least one section line such as `[verse]` or `[chorus]`; may not start with a dash | instrumental |
| `duration_s` | whole number | 5 up to `limits.max_song_seconds` (default 420, at most 580) | 150 |
| `prompt_id` | string | `[a-z0-9][a-z0-9-]{0,62}`, kept in provenance | none |
| `seed` | integer | as above | none |
| `steps` | integer | 1 to 64 | 8 |
| `title` | string | up to 120 characters | the preset name from `prompt_id`, else `Generated song` |
| `meta` | object | as above | none |

Accepted length of the result (`window_for`): song at least `clamp(0.3 x duration_s, 1, 45)` seconds and at most `min(1.5 x duration_s + 15, 580)`;
speech at least `max(0.4, min(0.35 x expected, 0.8 x max_seconds))` where `expected` is characters divided by 14 per second, and at most `max_seconds + 3`.
A result outside the window is thrown away and retried.

---

## 3. Persistence

Everything lives in `<data>/ext/generation/` (mode 0700) and never in the station database.

| File | Written by | Holds | In backups | Secret |
| --- | --- | --- | --- | --- |
| `config.json` | the operator | the settings in 4.7 (no passwords) | yes | no |
| `generation.sqlite3` (+ `-wal`, `-shm`) | the engine | tables `jobs`, `provenance`, `settings`; `PRAGMA user_version` is the schema version (currently 1); data from a newer version is refused with a plain message. `jobs.fixture` is added in place when it is missing (once, in one transaction, filled in from the provenance rows), so a database written by the first release opens without the number changing | yes: `generation.sqlite3` and `generation.sqlite3-wal` are listed by `persistent_files()` | no, but it contains scripts and lyrics |
| any file named by `auth_token_file` (for example `voice.token`) | the operator, mode 0600 | an access key for a speech server | **no, never** | **yes** |
| `work/gen-*`, `work/probe-*`, `work/smoke-*` | the engine | one private folder per attempt; removed when the attempt ends and swept at start | no | no (contents are transient audio) |

`generation.sqlite3` is opened through `ctx.open_db` (WAL, mode 0600) and checkpointed and closed on a clean stop, so a backup taken with the service stopped is complete.
Generated audio itself is in the station's own `media/` folder like any import.

**Tables.** `jobs`: id, role, state, priority, payload, fingerprint, title, resource, worker, seed, ttl, times, attempts, interruptions, error, attempt log (last 8), track id.
`provenance` (one row per imported track): track id, job id, role, kind, title, worker id/kind/version/hash, `fixture` (NOT NULL, 0 or 1, and a trigger refuses 1 to 0),
prompt id, seed used, parameters (the whole payload), timings (queue wait, worker time, each finishing step, import, real-time factor, measured levels),
attempts, validation notes, SHA-256 of the imported WAV, duration, status `ready | aired | gone`. `jobs.fixture` (0 or 1): whether the maker that ran the job, or will run it while it waits, only makes test tones;
a trigger refuses to clear it on a finished job. `settings`: `paused`, `buffer_overrides`, `buffer_override_bases` (what config.json said for that kind when the override was made, to notice a later edit).

**Retention.** The newest 2000 finished jobs are kept (queued and running jobs are never pruned); the newest 5000 provenance rows are kept.

**Recovery after a restart.** Jobs left `running` become `interrupted` (recorded in the attempt log, attempt refunded) and go back to `queued`, up to `retry.max_requeues`
times (default 2). Beyond that they stay `interrupted` until someone retries them. A **clean stop** puts running work back in line without counting an interruption.
At every start, waiting jobs whose kind of audio has no maker in the settings *and no entry that failed to load* are failed with `worker_missing`; if the maker's entry exists but has a problem, or the whole settings file cannot be read, the jobs keep waiting so a typo does not throw the queue away. Once a track has landed in the library nothing makes it again: if writing the job down as done fails (a locked
database, a value a maker returned that JSON cannot hold) the write is retried, and if it still fails the engine keeps trying about once a second instead of putting the job back in line. Two ways to a duplicate remain,
both rare: a crash in the instant between the track landing and the job being marked finished (the job runs again and the old copy is not removed), and a stop that closes the history file while a very slow
import is still under way (the job is recovered as running and made again). Both are listed in 9; `import_audio` taking an idempotency key (8) would close them.

---

## 4. Extension surface

### 4.1 Operations (`station.action`, MCP, Operator)

None of them accepts a command, an address, a path or a credential.

| Operation | Arguments (host-validated) | Result |
| --- | --- | --- |
| `generation.enqueue` | `role` (enum), `payload` (object), optional `priority` 0 to 100 (default 50), `ttl_s` 5 to 2592000, `idempotency_key` (`[A-Za-z0-9._:/@-]{1,120}`) | `{job, created, deduplicated, message}` |
| `generation.cancel` | `job_id` | `{job, settled, message}`; a waiting job is cancelled at once, a running one is stopped (up to 3 s is waited for) |
| `generation.retry` | `job_id` | `{job, message}`; only for `failed`, `cancelled`, `expired`, `interrupted`; resets attempts and the deadline |
| `generation.pause` | optional `cancel_running` boolean | `{paused, stopping_now, running, message}`; persists across restarts. With `cancel_running` the machine is released at once and the jobs go back in line |
| `generation.resume` | none | `{paused: false, message}` |
| `generation.set_buffer` | `role`, `low` 0 to 7200, `high` 1 to 14400 | `{buffer, message}`; per kind: song 0 to 7200 / 1 to 14400 seconds, others 0 to 50 / 1 to 100 items; `low` may not exceed `high`; persists. `low` means "start again when fewer than this are ready"; `0` means "start again only when nothing is ready". Marks set this way win over `config.json` (`GET /buffer` shows `marks_from: "operation"`) until the person edits that kind's marks in `config.json`: the file was changed later, so the file wins again |
| `generation.job` | `job_id` | `{job}` with payload, attempt log and provenance |

**Idempotency.** Sending the same request with the same key returns the existing job (`created: false`) while that job is waiting, running, succeeded or interrupted. The
key is released when the job fails, is cancelled or expires, and when its audio is deleted from the library, so a producer may send the request again. The same key with a
different request is refused (`That idempotency key was already used for a different request`). The seed is not part of the request's identity unless the producer pinned it.

**Priority and the buffer.** Higher priority runs first, oldest first among equals. A priority at or above `buffer.urgent_priority` (default 80) ignores the buffer marks (exactly 80 included).
The scheduler decides which kinds of audio the buffer allows *before* it reads the queue, so any number of held-back jobs (the queue may hold up to `limits.max_queued`) cannot hide a job that may run.

### 4.2 Routes (`/api/x/generation/...`, authenticated like every extension route)

| Route | Request | Response |
| --- | --- | --- |
| `GET /jobs` | optional `state`, `role`, `limit` (1 to 200, default 50; a larger number is capped), `before` (a job's `seq`: a whole number from 0 up), or `id=<job id>` or `track_id=<library track id>` | list: `{jobs: [job], counts: {state: n}, next_before: seq or null}` (newest first, compact jobs); single: `{job}` with payload, attempt log, provenance. 400 for a bad filter (including a `before` that cannot be a job number), 404 when nothing matches |
| `GET /workers` | none | `{workers: [worker], capabilities, proven, paused, config, fixture_active}` |
| `GET /buffer` | none | `{roles: {role: {label, unit, ready, ready_items, ready_seconds, low, high, marks_from, filling, queued, running, pending, wanted, oldest_ready}}, paused, urgent_priority, overrides}`; `marks_from` is `"settings"` or `"operation"`. `ready` leaves out test tones unless the maker for that kind is itself a test maker |
| `POST /probe` | `{}` or `{"full": true}`; body at most 1 KB | `{workers, capabilities, proven, full_check_queued, full_check_message?, reloaded, settings_changed, note?}`. At most one run per 10 seconds; otherwise `429 {error, retry_after_s}`. `503 {error}` when the new settings would not start and neither would the old ones (restart myradio) |

`POST /probe` re-runs the quick check of every maker that is not busy, and, when the settings file changed, picks the new settings up if nothing is being made
(otherwise it says so in `note`; a settings file that cannot be read is never loaded, the settings from the last start stay in use; new settings that cannot be loaded or started leave the old ones
running, and `note` says so). `full: true` also queues a real test clip for every maker that is answering and not busy (a short clip for the voice maker, a short song for the music maker), whatever its
`smoke_test` says, and it runs as soon as the machine is free, even while the engine is waiting out an earlier failed test. `full_check_queued` is true only when a test clip is queued or already being made;
when it is false, `full_check_message` says why (each maker is busy, not answering, or missing files); while making audio is paused the clip waits for the resume and the message says so. A person's "Run a full test" is the way to prove a maker (and to bring a `degraded` one back, see 4.6).

**Job shape** (compact; the detail form adds `detail`, `payload`, `attempt_log`, `interruptions`, `provenance`):

```json
{"id": "c5aca14770b94ee991f8643d0fb5bc77", "seq": 1, "role": "host_break", "role_label": "Host break",
 "title": "Host break: Good evening, you are listening to", "summary": "Good evening, you are listening to the station.",
 "state": "queued", "state_label": "Waiting", "priority": 60,
 "created": "2026-11-06T01:16:43+00:00", "started": null, "finished": null, "expires": "2026-11-06T02:16:43+00:00", "next_try": null,
 "attempts": 0, "max_attempts": 3, "worker": "voice", "resource": "gpu0", "idempotency_key": "slot:12", "seed": 3224515710,
 "track_id": null, "fixture": false, "error": null, "error_code": null, "hint": "", "note": null, "cancel_requested": false}
```

All times are ISO-8601 with an offset. `state` is one of `queued running succeeded failed cancelled expired interrupted`.

**Worker shape** (`state` is `checking ready warming unreachable missing_files failed blocked degraded misconfigured`):

```json
{"id": "voice", "kind": "drama_http", "kind_label": "Voice maker (Drama 3)", "roles": ["host_break", "id", "spot", "update"], "resource": "gpu0",
 "state": "ready", "state_label": "Ready", "message": "The voice maker is ready (warm-up took 38 seconds).", "hint": "", "ready": true,
 "evidence": "test_clip_made", "evidence_label": "A short test was made", "fixture": false, "version": null, "runtime_sha256": null,
 "last_checked": "...", "last_ok": "...", "last_work_ok": null, "recent_failures": 0, "last_error": null, "warm": true,
 "details": {"address": "http://127.0.0.1:8080", "model": "drama-3", "warm": true, "protects_with_key": false, "remote_allowed": false}}
```

`evidence` says how much a maker has proven: `none`, `setup_checked` (files and address checked, nothing made), `test_clip_made`, `real_job_completed`, and `test_tones` (a test maker made tones; never anything real).
A job's `fixture` is true when a test maker ran it (or, while it waits, will run it); it is stored with the job, so it stays true after the test maker is taken out of the settings.

### 4.3 Status (`station.status()["extensions"]["generation"]["status"]`)

A snapshot rebuilt by the engine's own threads at every change and at least once a second. `status()` itself only returns the last snapshot: no lock, no I/O, no call into the station.
If the snapshot is more than 30 seconds old it carries `"stale": true` and claims nothing: every capability and `proven` flag is false and the headline says the news is out of date.

```text
enabled, generated (ISO time the snapshot was built), configured (a maker is loaded), headline, next_step, paused,
capabilities: {music_generation, speech_generation, roles: {song, host_break, id, spot, update}}   # booleans: set up and checked
proven: {music_generation, speech_generation, roles: {...}}   # booleans: capable, and a test clip or a real job has actually been made
fixture_active, fixture_roles, workers: [worker], resources: {name: {max_concurrent, running, loaded_worker}},
buffer: {role: {unit, ready, low, high, filling, queued, running, wanted}},
jobs: {queued, running, succeeded, failed, cancelled, expired, interrupted}, recent: [up to 5 finished jobs],
audio_tools: {available}, config: {present, problems: [up to 6], changed_since_start}, recovered: {requeued, interrupted}, station_running
```

**Capabilities are true only when all of these hold:** the maker for that kind of audio is not a fixture; its last check passed (and it has not failed three attempts in a row, and its last test clip did not fail);
and the audio tools needed to finish its work exist. `speech_generation` is true when any speech kind is ready; `roles` gives each kind. Nothing is assumed from the settings alone.
Because checks run about every minute, a capability can be up to a minute old; `workers[].last_checked` says when. A maker that is busy making something is not asked to answer a check.

**What a capability proves, and what `proven` adds.** A quick check proves little: for the music maker, that the program and the model files exist (and that its version command runs, when it has one); for the voice maker, that the
address answers. A music maker whose program exists but is broken (a missing library, say) passes that check, so `capabilities.music_generation` is true and its card says `evidence: setup_checked` ("Set up and checked;
nothing made yet") until a test clip or a job has really been made. `proven.<kind>` is true only when the kind is capable **and** its maker has made a test clip or finished a real job. A first-run gate that needs to know
audio can really be made must use `proven`; `capabilities` alone means "set up and checked". The headline `The makers are ready.` follows the capabilities, not `proven`; while a ready maker has made nothing yet its `next_step` says "Nothing has been made yet to prove it. Choose Run a full test to be sure.", and the doctor checks `music` and `voice` carry the same line as their hint.

`filling` means "below the high mark, so waiting jobs of this kind will run"; it does not mean a job is running. `wanted` is how much more a producer should send:
`high - ready - pending` while filling, otherwise 0 (seconds for songs, items for the rest).

### 4.4 Doctor checks (`station.extension_checks()`)

Ids are prefixed `generation.`; statuses are `ok`, `warn`, `fail`, `unknown`. Details and hints are plain language.

| Id | Meaning |
| --- | --- |
| `config` | `warn` when nothing is set up or the settings have problems (listed), else `ok` |
| `music`, `voice` | the maker's last check: `ok` ready, `unknown` checking, `warn` warming up, a fixture, or switched off in the settings, `fail` otherwise (including `degraded`). `ok` means set up and checked; the detail says when nothing has been made yet |
| `music_files`, `voice_files` | `ok` or `fail`: the program, model files and key file the maker needs exist (local, quick) |
| `warmup` | the voice maker has made its first test clip |
| `disk` | free space where audio is made: `fail` below `limits.min_free_disk_mb`, `warn` below four times that |
| `audio` | the audio tools exist |
| `fixture` | `warn` while a test-tone maker is configured |
| `recent` | `warn` when 3 of the last 5 jobs did not finish |
| `start` | added by the host when the extension failed to start (for example a damaged history file) |

### 4.5 Events consumed (`on_event`)

`track_started` and `track_finished`: the track leaves the ready pool (provenance status `aired`). `transport`: remembered as `station_running` for display only.
Generation continues while the station is stopped (so audio can be made before going on air). The ready pool is also reconciled with the library about once a minute through
`ctx.station.status()` so tracks deleted by hand stop counting.

### 4.6 How the engine behaves

* **Resource groups.** Each group has `max_concurrent` slots (default one group `gpu0`, one slot) and one supervisor thread per slot. With the default of one slot, makers in the same group never run together;
  with `max_concurrent` 2 or more, up to that many jobs (of any makers in the group) run at once. Different groups run in parallel. The engine does not start or stop model servers: the operator's setup decides what fits on a card.
* **Choosing the next job.** First the buffer decides which kinds of audio may run (below), and only those kinds, plus urgent jobs, are read from the queue. Then: urgent jobs first; then jobs for the worker that last ran
  in the group (so a card is not swapped between music and speech for every job), unless a job for another worker has waited longer than the group's `switch_after_s` (default 120 s); then priority, then age.
  The first 300 jobs that are allowed to run are looked at, best first, so a long line of held-back jobs never hides one that may run.
* **Buffer.** "Ready" is audio the engine made and imported that has not started playing (test tones left over from a test maker do not count once a real maker has taken over that kind). Below the low mark the engine works
  through waiting jobs of that kind; it stops at the high mark; between the marks it keeps doing what it was doing. A low mark of 0 means "start again when nothing is ready". Only enqueued jobs are ever run, so producers
  watch `GET /buffer` (`wanted`) to know how much to send.
* **Attempts.** Up to `retry.max_attempts` (default 3). After a retryable failure the job waits `min(cap_s, base_s x 2^(attempts-1))` (default 10 s, 20 s, ... capped at 300 s).
  Every retry of an unpinned job uses a new seed (`seed + 7919 x (earlier attempts)` mod 2^32, the first attempt uses the recorded seed), so a bad take is not simply repeated; a producer-pinned seed is reused every time.
  The time one attempt may use is the maker's own limit (`timeout_s` and `plan_timeout_s` for the music maker, `job_timeout_s` for the voice maker); the engine's own ceiling above them is three hours.
* **Deadlines.** A waiting job past its `ttl_s` expires and never runs. A job that finishes after its deadline is thrown away (a stale bulletin is never aired). `retry` restarts the clock.
* **A maker that is down.** The engine does not hold jobs back while a maker is known to be down. Each waiting job for it is tried, fails with `worker_unreachable`, and is retried with the growing waits above; after
  `retry.max_attempts` it is reported as `failed` (about 30 seconds with the defaults). Use **Try again** when the maker is back. The quick check meanwhile looks again every 15 seconds and the card says what is wrong.
* **Quick check** every `limits.probe_interval_s` (60 s; 15 s while a maker is down): the voice maker's address answers; the music maker's program and files exist and its optional version command runs
  (once, not every minute). It proves little (see 4.3).
* **Full check** (a real test clip: for the voice maker the warm-up request, for the music maker a short test song) runs on a free slot, in front of waiting jobs, in these cases:
  the voice maker after start and after it stopped answering (this is the warm-up; `smoke_test: false` turns it off); the music maker at start when its `smoke_test` is true (the maker does not count as ready until it passes); a maker that is `degraded` as soon
  as its quick check passes; and when a person chooses **Run a full test** (`POST /probe {"full": true}`), **whatever `smoke_test` says** (that setting only decides whether the engine tests by itself at start).
  The test song asks for 8 seconds, but the program is told the length only when its command contains `{duration_s}` (the documented example does not), and a real build makes songs of their own natural length, a minute or more.
  So the test song only has to prove that the program runs and writes sound: it is accepted at any length from 1 s up to the longest a real song may be (the window for `limits.max_song_seconds`, 580 s with the defaults).
  Before this, anything over 40 s failed the maker although a real 150 s job passed.
* **A test clip that fails is not repeated at once.** After a failed test clip the engine waits 15 seconds, then 30, 60, 120, 240 and at most 300 seconds before it tries by itself again (the count starts over when a test passes
  or the maker was away and came back). Meanwhile waiting jobs run by the normal rules, so one maker with a failing warm-up cannot starve the others in its group, and a server that answers its address but returns an error is
  asked once, not thousands of times. A person's **Run a full test** is not held back by this wait.
* **Automatic test clips stop after a few failures.** After `auto_test_limit` failed test clips in a row (3 for the music maker, whose test is a whole model run on a shared card; 5 for the voice maker) the engine stops making them
  by itself: the waits, the quick check, **Check again** and the degraded path no longer start one. Only a person's **Run a full test** makes another (one per request); a test that passes, or a maker that was away and came back,
  starts the count over. The card's hint then adds: "N test clips failed in a row, so it has stopped making them by itself; only Run a full test makes another." The failed-test hints say what to do (run the program by hand
  with the same settings, or read the voice maker's own messages) and then **Run a full test**, not **Check again**, which cannot help after a failed test.
* **Degraded.** A maker passes its quick check but is called `degraded` (capability false, state label "Having trouble") when three attempts in a row failed, or when its last test clip failed and nothing has worked since:
  a cheap check cannot overrule a real failure. It comes back when a test clip passes or a real job succeeds. The engine proves it by itself: when a degraded maker's quick check passes it queues one test clip (with the waits above, and only until the automatic limit is reached),
  and **Check again** or **Run a full test** do the same at once. The card's hint says: "Look at the failed jobs for the reason. When it is fixed, choose Run a full test to prove that it works again."
* **Finishing**, in this order, serialised across slots: quality check on the raw result (`validate_generated`), convert to 44.1 kHz stereo, trim silence, fade, normalise to the station level, write a WAV, import
  with `ctx.station.import_audio(data, title, artist, kind)`. Fatal: `silent`, `too_short`, `too_long`, `truncated_tail` (unless `finishing.strict_tail` is false). Kept as notes in
  provenance: `clipped` (fatal only with `finishing.strict_clipping`), `dc_offset`. A failed or invalid take never reaches the library.
  Converting comes first so that the level and the peak ceiling are set on the samples that are broadcast: converting a 48 kHz take after normalising brought its true peak out about 3 dB over the
  ceiling (to the rail with a -1 dBFS ceiling) in the lane test `test_the_peak_ceiling_holds_for_the_converted_samples_that_go_on_air`; now the broadcast samples end at the ceiling.
* **After the import.** Once the audio is in the library it is never made a second time. Writing the job down as done is retried a few times; if the database still refuses, the engine keeps trying about once a second
  and the job stays `running` meanwhile. A library that refuses the audio (`import_failed`) is retried by the normal rules, but the finished result is held in memory (one result at a time, up to 128 MB) so a retry only repeats
  the import and a song is not made again for each try; the held result is let go when the job succeeds, fails for good, is cancelled or expires.
* **Cancel and pause.** If an attempt fails at the moment a cancel arrives, the cancel wins: the job is `cancelled` (or, for a pause that stops running work and for a stop, put back in line without using an attempt).
* **Stop.** `stop()` tells running makers to stop (process groups get TERM, then KILL), puts their jobs back in line, joins every thread and closes the database.

**Error codes** (`job.error_code`, with the plain `error` text and a `hint` for each):

`worker_missing`, `worker_unreachable`, `worker_timeout`, `worker_failed`, `worker_blocked`, `bad_output`, `no_output`, `output_too_large`, `silent_output`, `audio_too_short`,
`audio_too_long`, `audio_cut_off`, `audio_unusable`, `audio_tools_missing`, `low_disk`, `import_failed`, `expired`, `cancelled`, `interrupted`, `internal`.
`low_disk`, `import_failed`, `cancelled`, `expired`, `internal`, `audio_tools_missing` never count against a maker's health. Three failed attempts in a row make a maker `degraded`
(capability false) until a test clip passes or real work succeeds.

### 4.7 Settings reference (`config.json`) and the complete example

Written by the operator at `<data directory>/ext/generation/config.json` (the data directory is the one given to `myradio --data-dir`; the folder is created at the first start; mode 0600 is sensible). Loading is forgiving about *which* part is wrong (a bad maker is reported by name and the good ones still load; unknown settings are reported) and
strict about *what* is accepted. After editing, choose "Check again" (`POST /probe`): it reloads the file when nothing is being made. Without that, restart myradio.

```json
{
  "artist": "My Radio",
  "resources": {
    "gpu0": {
      "max_concurrent": 1,
      "switch_after_s": 120
    }
  },
  "workers": [
    {
      "id": "voice",
      "kind": "drama_http",
      "resource": "gpu0",
      "base_url": "http://127.0.0.1:8080",
      "model": "drama-3",
      "request_timeout_s": 90,
      "warmup_timeout_s": 180,
      "max_take_chars": 380
    },
    {
      "id": "music",
      "kind": "yue2_cli",
      "resource": "gpu0",
      "command": [
        "/opt/yue2/bin/audiocpp_cli",
        "--task",
        "gen",
        "--family",
        "yue2",
        "--model",
        "/opt/yue2/models/Yue2-3B-GGUF",
        "--backend",
        "sycl",
        "--threads",
        "8",
        "--lyrics",
        "{lyrics}",
        "--request-option",
        "style={tags}",
        "--request-option",
        "cot=off",
        "--request-option",
        "guidance_scale=1.0",
        "--seed",
        "{seed}",
        "--out",
        "{output_file}"
      ],
      "timeout_s": 1800,
      "model_files": [
        "/opt/yue2/models/Yue2-3B-GGUF/yue2-3b-q8_0.gguf"
      ],
      "env": {
        "ONEAPI_DEVICE_SELECTOR": "level_zero:gpu"
      },
      "env_passthrough": [
        "ONEAPI_ROOT"
      ]
    }
  ],
  "buffer": {
    "song": {
      "low": 600,
      "high": 1200
    },
    "host_break": {
      "low": 2,
      "high": 4
    }
  },
  "retry": {
    "max_attempts": 3,
    "base_s": 10,
    "cap_s": 300,
    "max_requeues": 2
  },
  "finishing": {
    "target_lufs": -16.0,
    "ceiling_dbfs": -1.0
  },
  "limits": {
    "max_queued": 500,
    "max_song_seconds": 420,
    "min_free_disk_mb": 500,
    "probe_interval_s": 60
  }
}
```

| Setting | Rule |
| --- | --- |
| `artist` | library artist label for generated audio, up to 80 characters, default `myradio` |
| `resources` | up to 4 groups: `max_concurrent` 1 to 4 (default 1), `switch_after_s` 5 to 3600 (120). Default is one group `gpu0` |
| `workers` | up to 8. Common: `id` (`[a-z0-9][a-z0-9_-]{0,30}`), `kind`, `resource` (default: the first group), `roles` (default per kind), `enabled` (`false` switches the maker off: the screen lists it as "Switched off", it is not a problem, and its waiting jobs stay in line until it is switched on again) |
| `kind: drama_http` | speaks only (`host_break`, `id`, `spot`, `update`); asking it for `song` is refused: Drama 3 is never a music fallback. `base_url` (required, `http://` only; this computer unless `allow_remote: true`), `model` (`drama-3`), `speech_path` (`/v1/audio/speech`), `health_path` (`/v1/models`; any answer counts as "up"), `request_timeout_s` 5 to 600 (90), `warmup_timeout_s` 10 to 900 (180), `job_timeout_s` 10 to 1800 (600), `max_take_chars` 80 to 1000 (380), `crossfade_ms` 0 to 500 (60), `max_response_mb` 1 to 100 (60), `auth_token_file` (a file name next to `config.json`), `warmup_text`, `options` (`temperature` 0 to 2, `top_p` 0 to 1, `top_k` 1 to 1000), `smoke_test` (true: the warm-up clip is made by itself and the maker is not "ready" until it passes; false: no clip until a person chooses Run a full test), `model_files` (full paths, presence checked) |
| `kind: yue2_cli` | makes songs only. `command` (required list), optional `plan_command` + `instrumental_command` (instrumental songs only), `version_command`, `timeout_s` 30 to 7200 (1800), `plan_timeout_s` 10 to 3600 (600), `version_timeout_s` 1 to 60 (20), `kill_grace_s` 1 to 30 (5), `max_output_mb` 1 to 200 (100), `env` (fixed values), `env_passthrough` (names to copy; always passed: `PATH HOME LANG LC_ALL LC_CTYPE LD_LIBRARY_PATH TZ`), `model_files`, `smoke_test` (false: no test song unless a person chooses Run a full test; true: an 8 second test song is made by itself at start and the maker is not "ready" until it passes). A `version_command` is recommended: it makes the quick check start the program, so a program that cannot start is noticed without a test song (the right flag depends on the build, see below) |
| `kind: fixture` | test tones; refused unless `MYRADIO_ALLOW_FIXTURE_WORKERS=1`. `max_seconds` 1 to 30 (when written: the longest tone it makes, even if that is too short for the engine to accept; left out: about 6 seconds, longer only when the request needs a longer result), `frequency_hz` dividing 44100 |
| `buffer` | per kind `{low, high}` (`low` 0 means "start again only when nothing is ready") (defaults: song 600/1200 seconds, host_break 2/4, id 1/2, spot 1/3, update 1/2 items) and `urgent_priority` 1 to 100 (80). `set_buffer` overrides these at run time |
| `retry` | `max_attempts` 1 to 6 (3), `base_s` 1 to 3600 (10), `cap_s` 1 to 86400 (300), `max_requeues` 0 to 10 (2) |
| `finishing` | `target_lufs` -30 to -8 (-16), `ceiling_dbfs` -12 to 0 (-1), `strict_clipping` (false), `strict_tail` (true) |
| `limits` | `max_queued` 10 to 2000 (500), `max_song_seconds` 30 to 580 (420), `min_free_disk_mb` (500), `probe_interval_s` 10 to 3600 (60) |

**What validation rejects** (each reported in plain words, naming the maker and the setting):
a command that is one line of text instead of a list of arguments; a program given as a relative path (`./tool`, `bin/tool`) or that is not found by full path or on the PATH
(`shutil.which`); a program that is a shell or interpreter run with a typed-in script (`bash -c`, `python -c`, `perl -e`, ..., also with other letters in the same word such as `bash -lc`, `python3 -Sc`, `perl -le`, and behind `env`, `nice`, `timeout`, `xargs`, `nohup`, `sudo` and similar), an unknown placeholder or a stray brace;
a missing `{output_file}`; any address that is not `http://`, that has a user name, query or `..`, or that points away from this computer without `allow_remote: true`;
timeouts, sizes and counts outside the ranges above (including `true`, text and not-a-number values); two makers for the same kind of audio; `song` for the voice maker;
speech for the music maker; a test-tone maker without the environment switch; unknown top-level settings.
The typed-in script check catches the usual ways of writing a script on the command line. It is a guard against mistakes in a file the operator writes and trusts, **not a security boundary**: anything that can write `config.json` can already run programs as the station.

**Placeholders for `command`, `instrumental_command` (and `plan_command`)**: each argument is a template; a placeholder is replaced inside that one argument and the result is passed as
exactly one argument (no shell, no splitting). Write `{{` and `}}` for literal braces.

| Placeholder | Value |
| --- | --- |
| `{output_file}` | full path where the program must write the finished WAV (required in `command` and `instrumental_command`) |
| `{work_dir}` | the private, empty folder for this attempt (deleted afterwards; also the working directory and `TMPDIR`) |
| `{tags}` | the style description |
| `{lyrics}` | the lyrics, or `[instrumental]` when the song has none |
| `{lyrics_file}` | full path of a text file with the same lyrics |
| `{seed}` | seed, whole number 0 to 4294967295 |
| `{steps}` | refinement steps, whole number |
| `{duration_s}` | target length in seconds, whole number |
| `{instrumental}` | `true` or `false` |
| `{prompt_id}` | the preset name, or an empty argument |
| `{plan_file}` | only in `plan_command` and `instrumental_command` when `plan_command` exists: full path for the plan (non-empty when the plan step ends) |

An argument that is exactly `{tags}`, `{lyrics}` or `{prompt_id}` is refused at run time if its text begins with a dash (it could be mistaken for an option);
payload validation already refuses such tags and lyrics.
**Option names for steps and length depend on the build.** The model card shows `--seed`, `--out`, `--lyrics` and `--request-option style=... cot=off guidance_scale=1.0`; where
your build wants `{steps}` or `{duration_s}` (for example inside `--request-option name={steps}`) check `audiocpp_cli --help` and add them. This was not verifiable here.

**The program runs** with: no shell; its own process group (TERM, then KILL after `kill_grace_s`; leftovers of the group are killed even after a normal exit); working directory
a private temp folder; only the passthrough variables plus `env` and a private `TMPDIR`; no standard input; the tail of standard output and error kept (8 KB each) for the technical
detail; the folder size capped at `max_output_mb`. A daemon that deliberately leaves the process group (double fork with a new session) is outside what can be stopped.

**Speech server contract assumed** (from the Drama 3 model card): `POST <base_url>/v1/audio/speech` with JSON `{"model", "input": "<|speaker:N|> text", "response_format": "wav", "seed", "max_tokens"}`
returns a WAV (any sample rate, mono or stereo). The request is made over a plain socket with an absolute deadline; redirects are not followed; compressed or chunked
answers are handled; answers over `max_response_mb` are refused. `GET <base_url>/v1/models` is used only to see whether the server answers (any HTTP answer counts, a 401 or 403 is reported as a key problem).

---

## 5. UI contract

What a screen (the Settings "Music and voice" page, the Studio next-step bar, the Operator) must show. All strings below are what the API already returns; show them as they are.

**Where the data comes from.** `GET /api/x/generation/workers` for the cards, `GET /api/x/generation/buffer` for "ready to play", `GET /api/x/generation/jobs` for the list, the
extension status for the next-step bar (`headline`, `next_step`, `capabilities`). Poll every few seconds while the page is open; the data is cheap.

**Maker cards** (one per `workers[]`): the `kind_label` ("Voice maker (Drama 3)", "Music maker (YuE2)"), the `state_label` with an icon that is not colour alone
("Ready", "Warming up", "Not answering", "Files missing", "Not working", "Having trouble", "Settings problem", "Switched off", "Checking"), the plain `message`, the `hint` as the next action,
and `evidence_label` in small type: "Set up and checked; nothing made yet" must never be styled as proof that audio can be made. A **Check again** button calls `POST /probe`
(on 429 show `error`). A secondary "Run a full test" calls it with `{"full": true}` and says "This makes a short test clip and takes up to a minute (a song takes longer)."; it really does make a clip, for the music maker too, and
it is the button that brings a "Having trouble" maker back. When the answer has `full_check_queued: false`, show `full_check_message` instead of implying a test is running. A maker with
`evidence: setup_checked` shows its small line "Set up and checked; nothing made yet" next to the button. Any worker with `fixture: true`
carries a persistent warning badge "Test tones only. Not real music or voice." and a small line "Test tones only; nothing real has been made" once it has made tones. A maker the person switched off in the
settings is listed as "Switched off" with the hint that says how to switch it on; it is not a problem and is not counted in the trouble list.

**Empty and first-run states.**
* No settings (`configured: false`, no workers): "No music or voice maker is set up. Your own imported audio works without them. Add them in the generation settings." with a "Set up..." action that explains the settings file.
* Problems in the settings (`config.problems`): list each as a plain sentence under "Fix these, then choose Check again."
* Warming up: "The first test can take about a minute. Nothing to do."
* Paused: a banner "Making audio is paused" with **Resume**.
* The "Something to play" gate should treat `capabilities.music_generation === true` as "the music maker is set up and checked" and nothing weaker. A gate that must know audio can really be made uses
  `proven.music_generation === true` (a test clip or a real job has been made); until then show "Run a full test" as the next step.
* A status with `stale: true` claims nothing (every capability is false); show its `headline` and `next_step` (Check again, then restart if it stays).

**Ready to play** (one line per kind in `buffer.roles`): "3 songs ready (12 minutes)" or "2 host breaks ready", the low and high marks as a quiet range (with "set by you" next to them when `marks_from` is `"operation"`: those marks win over the settings file until the file's marks for that kind are edited),
and when `filling` and `wanted > 0` "Room for 2 more". Never claim something is "being made" unless `running > 0`.

**Job list** (newest first): title, `role_label`, `state_label`, relative time, and for finished-with-trouble jobs the `error` then the `hint` as the action. Buttons: **Cancel** (waiting or running),
**Try again** (`failed`, `cancelled`, `expired`, `interrupted`). A `queued` job with `next_try` shows "Will try again at 18:04". "Technical details" (disclosure, closed by default) shows `detail` and `attempt_log`.
Never show `detail`, error codes or seeds in primary text.

**Wording rules.** Say "maker", "song", "spoken link", "station ID", "sponsor message", "update". Do not say worker, PCM, GGUF, SSRF, HTTP, token, process, job queue (say "waiting"), or show a stack trace.
Errors say what happened, what it means and what to do; every `failed`, `expired` and `interrupted` job already carries a `hint`.

**Pause.** "Pause making audio" with a choice "Let the current one finish" (default) or "Stop now and put it back in line" (`cancel_running`). After pausing, `resources.<group>.running === 0`
means the card is free for something else.

**Accessibility.** Status is never colour alone; every button is keyboard operable and at least 44 px; time-based hints use absolute times as well as relative ones.

---

## 6. Limits and bounds

| What | Bound |
| --- | --- |
| Threads | one per resource slot (at most 4 groups x 4 slots) plus one maintenance thread, plus at most 2 abandoned name lookups; all joined by `stop()` |
| Waiting plus running jobs | `limits.max_queued` (default 500, at most 2000) |
| Payload | 16 KB after cleaning; `text` 3000 characters, `lyrics` 3000, `tags` 800, `meta` 2 KB |
| History | newest 2000 finished jobs; newest 5000 provenance rows; attempt log keeps the last 8 attempts |
| Status | under 12 KB however many jobs exist (one snapshot, 5 recent jobs, at most 6 problems; tested with 300 waiting jobs) |
| Result from a maker | HTTP: `max_response_mb` (at most 100); program: `max_output_mb` (at most 200) for the whole work folder (checked every 0.2 s, so a fast writer can pass the cap by a few seconds of writing before it is stopped) |
| Memory while finishing | one result at a time is finished (serialised); a 420 s stereo song is about 74 MB and a few copies exist at once; results of other slots wait as raw audio. After a library refusal one finished result (at most 128 MB) is held for the retry |
| Time | per attempt: `timeout_s` (program), `job_timeout_s` and `request_timeout_s` (voice); probes 5 s (quick); finishing is bounded by the audio tools, not by a deadline |
| Disk | an attempt refuses to start below `limits.min_free_disk_mb` free where it works |
| HTTP | headers up to 32 KB and 100 lines, chunk trailers up to 64 lines, no redirects, no compression, `http://` only |
| Rates | `POST /probe` once per 10 s; `cancel` waits at most 3 s. After a failed test clip the engine waits 15 s, doubling to at most 300 s, before it tries one by itself again (a person's "Run a full test" is not held back); after 3 failed test songs (5 voice test clips) in a row it stops trying by itself |

---

## 7. Test inventory, commands and measured results

Files (`tests/`): `test_generation_core.py` (payloads, speech text, settings, store, buffer policy, and this document's contract), `test_generation_runners.py` (subprocess and HTTP runners),
`test_generation_workers.py` (Drama, YuE2, fixture workers), `test_generation_engine.py` (scheduler and behaviour: failed test clips, degraded makers, the finished file, what happens after the import),
`test_generation_extension.py` (host, Station and HTTP integration, safe reload, the cost of finishing), `test_generation_fakes.py` (the shared fakes: audio stand-in, misbehaving speech server, fake command line programs,
controllable worker, a plain-library WAV measurer, harness).

Commands:

```sh
cd <the repository folder> && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_generation*.py" -v
nice -n 19 ionice -c3 timeout 600 python3 -m unittest discover -s tests -p "test_extensions*.py"
MYRADIO_GENERATION_TEST_STANDIN=1 python3 -m unittest discover -s tests -p "test_generation*.py"     # force the audio stand-in even when the real module exists
python3.11 -m unittest discover -s tests -p "test_generation*.py"                                        # second Python version
```

Measured on Linux, on a machine shared with a live station and many other test runs (every command under `nice -n 19 ionice -c3`; wall times vary a lot with that load).
The audio tools in use were the files present in `myradio/audio/` at the time (`analysis.py check.py edit.py lanes.py loudness.py pcm.py rates.py synth.py wav.py`, last changed 19:34 on 5 Nov 2026); the tests run against them, and against the stand-in.

| Run | Result |
| --- | --- |
| Python 3.12.3, real `myradio.audio`: `test_generation*.py` | 283 tests, OK, 0 skipped, 81 s (after the second fix round; 279 tests, 87 s, before it) |
| Python 3.11.15, real `myradio.audio`: `test_generation*.py` | 283 tests, OK, 82 s (after the second fix round) |
| Python 3.13.14 (the repository's own `.venv`), real `myradio.audio`: `test_generation*.py` | 283 tests, OK, 80 s (after the second fix round). One `ResourceWarning: unclosed database` comes from the host's `Context.open_db` on a damaged file (request 9 in section 8), not from this lane |
| Python 3.12.3, audio stand-in forced (`MYRADIO_GENERATION_TEST_STANDIN=1`) | 279 tests, OK, 89 s (before the second fix round; not repeated after it) |
| Python 3.12.3 with `-X dev -W error::ResourceWarning` (no leaked file, socket, process or thread handle) | final run before the second fix round: 279 tests, OK, 91 s (not repeated after it). Earlier, at 278 tests, 8 of 9 full runs passed (94 to 117 s; three of them pinned to one core with `taskset`). **One earlier run failed with one failure and I did not keep which test it was** (I cut the output); the eight full runs after it and six runs of the new tests pinned to one core all passed. The likely cause is one of the lane's wall-clock bounds (below) on a busy computer |
| `tests/test_extensions.py` and `tests/test_extensions_station.py` (the seam this lane plugs into) | 45 tests, OK, 4 s |
| whole suite after the second fix round (2026-11-06, run last) | 2817 tests, **OK (skipped=1, expected failures=10)**, exit 0, 335 s |
| whole suite: `python3 -m unittest discover -s tests -p "test_*.py"` | 2800 tests, **OK (skipped=1, expected failures=10)**, exit 0, run last, after every edit in this lane. The runner printed an impossible duration (`345916s`; the run took about 6 minutes), so some test in another lane probably leaves a clock patched; not touched. An earlier run of the whole suite was ended by a SIGTERM from outside after 2708 tests with no failure before it (the other lanes' tests were being edited at the time); this run is the one to go by |
| mutation check (a copy of the tree in a scratch folder, one deliberate bug at a time, the repository untouched) | 49 mutants, all caught by the lane tests: 34 put back a bug the reviewers found, 7 break later additions, 8 weaken the gates the reviewers found untested (loudness, trimming, fades, the loudness target, the urgent boundary, an abrupt end, `strict_clipping`, the length window) |
| the reviewers' own repro scripts, re-run against the fixed code | hot loop: 1 speech request in 4 s (was 2251) and 1 test-song start in 5 s (was 358); 6 of 6 songs finished in 1.3 s beside a failing voice maker (5 of 6 never started); the song ran behind 310 and behind 400 held-back jobs (it never did before); one **Check again** brings a degraded maker back; 1 track for 1 job after a failed save (was 2); the test-tone flag stays true after the test maker is removed; a cancel during a failure ends `cancelled`; enormous numbers refused in plain words; idle cost 0.061% of one core (unchanged); a random cancel/pause/resume/retry stress run left no stuck, lost or duplicated job; no thread, handle or child process left behind |

The wall-clock bounds in the lane tests are few and generous (a worst tick or status time under 1 s in the older tests, a longest gap under 0.6 s while 120 s of audio is finished, retried once, in the newer one); the rest of the new
tests wait for the condition they check, or use a fake clock that the test moves.

By file: `test_generation_core.py` 65, `test_generation_runners.py` 31, `test_generation_workers.py` 48, `test_generation_engine.py` 107, `test_generation_extension.py` 32 (283 in all, up from 213).

Second fix round (after a final recheck): a music program that ignores the asked length passes its test song (killed the old 1 to 40 s window); automatic test songs stop after three failures and only **Run a full test** starts another (killed a mutant with no limit); the wait between automatic tests is 15, 30, 60, 120, 240, then 300 s and never more (killed the cap raised to a day, which had survived every earlier test); and the peak ceiling holds for the converted samples that go on air (killed the old finishing order: with it a -1 dBFS ceiling ended at the rail and a -6 dBFS ceiling at -3 dBFS). Each of these 4 mutants was made in a private copy of the tree and noticed by its test.
Nothing is skipped: the IPv6 loopback test ran because this computer has an IPv6 loopback address (it would skip, with that reason, on one that does not).

Other measurements (real audio tools, this machine): idle cost of the engine 0.06% of one core (three threads); finishing 60 s of stereo audio 1.5 s and 240 s 6.1 s (see section 9 for the effect on a ticking clock).

What the tests cover, by the brief's list: fake speech server with a valid 24 kHz mono WAV, a valid 44.1 kHz stereo WAV, an 8-bit WAV, a slow drip (headers and body), a 500, a truncated body,
oversize answers (declared and streamed), a wrong content type, garbage, silence, plus redirect, busy, forbidden, chunked, interim, compressed and malformed answers; fake programs that succeed, hang, ignore TERM, exit nonzero,
write nothing, write a huge file, spawn a child that must also die, leave a child behind, write garbage or silence; resource-group serialisation and true parallelism between groups; buffer marks, topping up, hysteresis and the
urgent bypass; priority and first-come-first-served; the loaded-worker preference and its starvation guard; idempotency (also under 8 simultaneous callers); ttl expiry, waiting and late; cancel of a waiting and a running job; pause, resume,
release-now and pause across a restart; retry delays, seeds, attempt limits; restart recovery by closing and reopening the database (and a crash simulated by leaving a job running); a damaged history file; the fixture refusal in every form;
unsafe command and address rejection; provenance and the unclearable fixture flag; status honesty (nothing configured, failed check, fixture, partial roles, degraded, audio tools missing); status without locks or station calls;
no leaked threads, no leaked processes; and end to end through a real `Station(start_worker=False, extensions=[...])` with a fake speech server, a fake program and the fixture, including over the real HTTP server, with the clock ticking while a worker hangs.

Added after the first review, each with a test that fails when the fix is taken out again: a failing test clip is retried after 15 s, 30 s, 60 s ... (a voice server that answers its address but returns 500 gets one request, not thousands;
a failing test song is started once; the other makers in the group keep their turn; a person's "Run a full test" is not held back); a degraded maker comes back by **Check again** or **Run a full test**; 310 held-back jobs
cannot hide a job the buffer would run; a job at exactly the urgent priority; the finished file itself (loudness for a quiet and a hot take and for a non-default target, 2 s of leading silence cut to a short pause, faded
ends, an abrupt end refused or noted by `strict_tail`, distortion noted or refused by `strict_clipping`, a spoken result far too long); a failed save after the import, a value JSON cannot hold, a result that contains itself and
a library that refuses the audio do not make anything twice; the orphan reaper; a failure arriving with a cancel or a pause; the test-tone flag of a job after the test maker was removed, a database from the first release
upgraded in place, leftover tones not counted as ready; a switched-off maker; marks set by operation giving way to an edited settings file; enormous whole numbers in payloads, settings and `before`; the typed-in script check
(21 command lines, refused and accepted); safe reload when new settings will not load or start; a status that is out of date claiming nothing; `proven`; finishing 120 s of audio while a 100 ms ticker runs.

---

## 8. Requested changes to shared files

None of these was made; each is for the orchestrator or the named lane.

1. **`myradio/station.py` (`Station.status`).** `capabilities.music_generation` and `speech_generation` are hard-coded `False`. After `result["extensions"] = ...` set them from
   the generation extension's status (false when it is absent), so the core screen shows real readiness and the "Something to play" gate can use it:

   ```python
   caps = ((result["extensions"].get("generation") or {}).get("status") or {}).get("capabilities") or {}
   result["capabilities"]["music_generation"] = bool(caps.get("music_generation"))
   result["capabilities"]["speech_generation"] = bool(caps.get("speech_generation"))
   ```
   A stale extension status already reports every capability as false, so the snippet needs no staleness check. The extension status also has `proven` (same shape): a screen that must not say "ready" before audio
   has really been made can read `proven` from the same place.
2. **`myradio/web/**` (UI).** A "Music and voice" panel and the next-step bar as in section 5. The data is ready; no UI was written in this lane.
3. **`myradio/station.py` events.** A `track_deleted` (or `library_changed`) event would let the engine drop deleted audio from the ready pool at once instead of reconciling about once a minute.
4. **`myradio/station.py` (`import_audio`).** An optional caller-supplied idempotency key (or a `source` field kept with the track) would close the two rare duplicate paths in section 3. Optional.
5. **`myradio/backup.py` / backup integration.** Include `host.backup_files()["generation"]` (`generation.sqlite3`, `generation.sqlite3-wal`, `config.json`) and never any file named by `auth_token_file`. The WAL file is only consistent when the service is stopped.
6. **`README.md`, `docs/DEPLOYMENT.md`.** A short "set up your music and voice makers" section pointing at 4.7. A human-only way to write `config.json` from the UI would need a route that requires an explicit person action; it must not be an operation (agents must not be able to change commands or addresses).
7. **Doctor lane.** Uses the ids in 4.4. `generation.start` appears only when the extension failed to start.
8. **`pyproject.toml`.** No change needed: `packages.find` already includes `myradio*`.
9. **`myradio/extensions.py` (`Context.open_db`).** Close the new connection when one of its first statements fails (a file that is not a database). Today the half-opened connection is left to the garbage collector,
   and Python 3.13 reports it as an unclosed database (seen once in the damaged-history test). Harmless, but noisy; this lane's own start-up path already closes the connection it gets.

---

## 9. Known limitations and unverified claims

* **No real inference was run.** Every statement about real YuE2 and Drama 3 behaviour (speed, quality, memory, the first-request warm-up, option names, error answers, whether both fit on one card) comes from the model cards and the reference station, not from execution.
* **Finishing runs inside the station process** (the audio tools are pure Python). Measured with the real audio tools on this machine under `nice 19`: finishing 60 s of stereo audio took 1.5 s and 240 s took 6.1 s (level measurement is most of it).
  While it ran, a station clock ticking every 100 ms showed per-tick time rising from about 1 ms to about 17 ms on average (30 ms at worst) and a worst start-to-start gap of 129 ms; it never fell behind or faulted.
  A test now measures this: finishing 120 s of audio took 3.2 s while a 100 ms ticker's worst gap was 114 ms (the test allows 600 ms). Finishing is serialised, so it happens once at a time. Running it in a separate process would remove even that, and was left out because the tests install the audio tools in-process; it is the first thing to do if soak testing shows jitter.
* **The size cap on a program's output is checked every 0.2 s**, so a very fast writer can pass it before being stopped. There is no per-process memory limit.
* **A daemon that leaves the process group** (double fork, new session) is not stopped.
* **Cancelling a running speech job** stops waiting and closes the connection; the speech server may finish the take it was making.
* **Capabilities are as old as the last check** (up to about a minute). A maker that is busy is not checked. A status whose snapshot is more than 30 seconds old claims nothing at all (see 4.3).
* **"Ready" can be only "set up and checked".** The default music maker setting makes no test song by itself, so a program that exists but cannot run is shown as ready until a test clip or a job has been made.
  `proven` and the card's `evidence_label` say so; the fix is **Run a full test**, or `smoke_test: true` for a test song at every start (it uses the card for a short song each time).
* **A test song may be a whole song.** Without `{duration_s}` in its command the real program decides the length (the model card shows 1:03 to 4:20), so a test song can take as long as a real one on the shared card. It is accepted at any length up to the longest a real song may be, and at most three are made by themselves in a row after failures. The real program's default length was not measured here (no inference was run).
* **The new finishing order was measured on synthetic signals only** (a 12 kHz burst whose samples sit below its true peak, over a quiet bed). On real YuE2 or Drama 3 output the difference is expected to be smaller, and it was not measured. Trimming and fading now run on the converted 44.1 kHz audio; the existing finishing tests (silence cut to a short pause, faded ends, loudness for quiet and hot takes and for a non-default target) pass unchanged.
* **A dropped station event** (the host's mailbox is bounded and drops the oldest) leaves a track counted as ready until it is deleted; `events_dropped` in the host status shows this.
* **Two rare duplicate paths** (section 3): a crash in the instant between the library taking a track and the job being written down, and a stop that closes the history file while a very slow import is under way.
  In both the job is made again and a second copy lands in the library; the first copy stays. A failure to write a finished job down, and a library that refuses audio, do not make anything twice.
* **The station process being killed outright** (`kill -9`, out of memory, power loss) does not stop a running music program: it has its own process group so that a normal stop can end it, and nothing records it for the
  next start. The restarted station recovers the job and starts a second program on the card while the first may still be running until its `timeout_s`. Not handled here: ending a program found from a recorded process
  number could end an unrelated program if the number was reused, and it cannot be checked the same way on every system. After an unclean stop, look for a leftover music program (for example `ps` and the program's name) and end it.
* **No text-to-speech or music is ever substituted for the other.** If a maker is down, each waiting job is tried and, after `retry.max_attempts` (about 30 seconds with the defaults), reported as failed with a hint to
  try again when the maker is back; nothing falls back to another kind of audio.
* **Licences are the operator's responsibility; nothing here checks them.** The model cards of the models this engine is built for set terms. As read from those cards: YuE2 is under CC BY-NC 4.0 (non-commercial, with attribution)
  and Drama 3 is under the Fish Audio Community License (free for non-commercial use; commercial use is allowed only below a revenue limit and not as a hosted text-to-speech service). This matters most for the `spot` kind
  (sponsor messages are commercial use). Check the current terms of the model cards you actually use before airing generated audio.
* **The typed-in script check is a guard against mistakes, not a security boundary** (see 4.7).
* **Settings are edited by hand** in `config.json` for now; changing them from the UI needs the human-only route in section 8.
* **Python 3.10 and 3.13, and macOS, were not run.** The code uses no 3.11+ syntax (the files compile under 3.11) and no Linux-only call without a fallback, but that is reading, not running.
* **The audio tools were still being written while this lane was built.** Tests ran against the version present at the times listed in section 7; the reviewers' run against the final module is the real check.

### How to check the unverified parts (needs an operator-granted GPU window)

Nothing below was run here. It is the shortest honest path from "verified against fakes" to "verified".

1. Start the speech server as in the Drama 3 model card. Write a `config.json` with only the `voice` maker, then choose **Check again** (or restart). Expect "Warming up", then,
   once the first test clip has been made (up to about 40 seconds on the first request), "Ready" with the evidence line "A short test was made".
2. Enqueue a `host_break` of one sentence. Expect `succeeded`, a library track of kind `speech`, and `provenance.timings.realtime_x` above 1. Listen to it.
3. Add the `music` maker with the real `audiocpp_cli` options. Run `audiocpp_cli --help` to find the options that take the number of steps and the length, and add `{steps}` and `{duration_s}`
   to them. Set `"smoke_test": true` to make a short test song before the maker counts as ready. Enqueue a 30 second instrumental. Listen to it.
4. With both makers in one group, enqueue a song, then a host break, and watch `resources.gpu0.loaded_worker` change. If the two do not fit on the card together, run the speech server only
   while a speech job is wanted, or give each its own group and card.
5. Pause with "Stop now", confirm the program is gone from the process list, resume, and confirm the job finishes.
6. With the music maker configured without `smoke_test`, choose **Run a full test** and confirm a short song is really made (the card then says "A short test was made" and `proven.music_generation` becomes true). Break the program
   (rename it) and choose **Run a full test** again: expect "Having trouble", then **Check again** must not turn it back to "Ready".
7. Stop the voice server while a host break is waiting and watch the job: it should fail with "did not answer" after its attempts (about 30 seconds) and **Try again** should work once the server is back.
8. On a spare machine only: start a song, then end the station process with `kill -9`, and look at the process list. Expect the music program to still be running (section 9). End it by hand before starting the station again.

---

## 10. Review round 1: what was found, what was done, what was not

Two independent reviewers found 27 things; five of them (the failing test clip, the held-back jobs, the huge numbers, the test-tone flag and the leftover music program) were found by both. Every fixed bug has a test that fails when the fix is taken out again (checked on a copy of the tree: 49 deliberate bugs, all caught); the fixes to tests and to this document are said to be so.

| Finding (reviewer) | What was done | Tests |
| --- | --- | --- |
| **Blocker, A and B.** A failed test clip (the voice warm-up, the music maker's test song) was repeated at once, forever, in front of every job | Fixed. After a failed test clip the engine waits 15 s, 30 s, 60 s ... at most 300 s; jobs and the other makers run meanwhile; a person's **Run a full test** is not held back (4.6) | `FullCheckTests` |
| **Major, A.** A degraded maker could not come back by the advice on the screen | Fixed. The hint names **Run a full test**; **Check again** on a degraded maker makes the engine run one test clip by itself | `HonestReadinessTests` |
| **Major, A and B.** More than 300 held-back jobs hid a job the buffer would run | Fixed. The buffer's choice is applied inside the query (4.6) | `SchedulingFixTests`, `StoreTests` |
| **Major, A.** Two tests read the status snapshot before the engine had rebuilt it | Fixed. Both wait for it; `POST /probe` also refreshes the "settings changed" flag before it answers | the two older tests, unchanged in what they assert |
| **Major, A.** The finishing path and the result gates had no test that would notice a wrong gate | Fixed. Tests measure the finished file (level, leading silence, fades) and each refusal; urgent boundary; orphan reaper; the whole test-clip life cycle. The eight deliberately broken gates the reviewer listed are all caught | `FinishingTests`, `SchedulingFixTests`, `FullCheckTests` |
| **Major, B.** The test-tone flag of a job was read from the current settings and was lost when the test maker was removed | Fixed. The flag is stored on the job (a column added in place, once, safely, instead of a numbered migration step, because the host's migration runs the script and sets the version in two steps and a stop between them would make the next start fail); a database from the first release is upgraded and filled in from the provenance rows; leftover test tones stop counting as ready for a real maker | `SchedulingFixTests`, `StoreTests` |
| **Major, B.** A music maker whose program exists but cannot run is shown as ready | Partly fixed (see below). **Run a full test** now really makes a short song or clip whatever `smoke_test` says; `full_check_queued` is true only when a test is queued; the status has `proven`; a stale status claims nothing; the ready headline and the doctor check say when nothing has been made yet; a failed test cannot be overruled by a cheap check | `HonestReadinessTests`, `FullCheckTests`, worker tests |
| Minor, A. Anything failing after the import made the audio a second time | Fixed. Writing the job down is retried and kept trying; a refused import is retried without making the audio again | `AfterTheImportTests` |
| Minor, A and B. A whole number too big for a float (legal JSON) raised an error nobody could read; `GET /jobs?before=` with a huge number was a server error | Fixed everywhere a number is checked, and `before` is checked | core, extension and engine tests |
| Minor, A. A failure arriving with a cancel was retried | Fixed. The cancel wins | `SchedulingFixTests` |
| Minor, A. A low mark of 0 filled once and never again | Fixed by meaning "start again when nothing is ready". Refusing 0 was the reviewer's other suggestion, but an existing test requires 0 to be accepted | `BufferPolicyTests` |
| Minor, A. One attempt was cut to an hour whatever `timeout_s` said | Fixed. The engine's own ceiling is three hours; each maker applies its own limit | `SchedulingFixTests` |
| Minor, A. Marks set by operation always win over `config.json`, with no way back | Fixed differently. They give way when that kind's marks in `config.json` are edited later; `GET /buffer` shows `marks_from`. No new operation: a test fixes the list of operations | `SchedulingFixTests` |
| Minor, A. A fixture song with no length failed `audio_too_short` | Fixed. With no `max_seconds` written the test tone fits the accepted length; a written cap is kept (an existing test requires that) | worker tests |
| Minor, A. The isolation tests were loose and the cost of finishing was untested | Fixed. A test finishes 120 s of audio on a slot while a 100 ms ticker runs | `FinishingCostTests` |
| Minor, A. A reload that failed to start left a stopped engine in place | Fixed. The new engine is built first; if it will not start the old settings are started again; if nothing starts the service says so (`503`) | extension tests |
| Minor, B. A test-tone maker said "Real work has been made" | Fixed. Its evidence is `test_tones` ("Test tones only; nothing real has been made"). Its state label stays "Ready" because existing tests require it; the warning badge and the fixture flag stay | `HonestReadinessTests` |
| Minor, B. The "typed-in script" check was easy to bypass | Fixed for every bypass listed (`bash -lc`, `sh -xc`, `python3 -Sc`, `perl -le`, `env`, `nice`, `timeout`, `xargs`), without new false alarms (`python -E`, `-mcalendar`); documented as a guard against mistakes, not a security boundary | `ConfigTests` |
| Minor, B. A maker switched off with `enabled: false` vanished and its waiting jobs were failed | Fixed. It is listed as "Switched off", is not a problem, and its jobs wait | `SchedulingFixTests`, `ConfigTests` |
| Minor, B. Waiting jobs burn their attempts while a maker is down | **Not changed; the document is corrected.** Holding jobs back, or not counting the attempts, makes `test_a_voice_maker_that_is_down_is_reported_plainly_and_nothing_is_claimed` fail: a job for a maker that is down must fail with `worker_unreachable`. Tried on a copy of the tree: the job never runs and that test fails after waiting 20 s. See 4.6 | |
| Minor, A and B. A hard kill of the station leaves a music program running | **Not fixed; documented** in 9 and the by-hand checks. Ending a program found from a recorded process number can end an unrelated program when the number was reused, and cannot be checked the same way on every system | |
| Minor, B. Several statements in this document were wrong or missing | Fixed: what the engine starts by itself, "never run together", what a full test does, "jobs wait and retry", the duplicate paths, the hard-kill limit, a licence note, a computer-specific folder, the test numbers | `ContractDocTests` |

**What "partly" means for readiness.** `capabilities.music_generation` still means "set up and checked": the brief defines it as "the corresponding worker passed its last probe" and existing tests require it to be true for a maker that has only passed its quick check, and
the headline `The makers are ready.` stays for the same reason. What changed is that a screen or gate can now tell the difference (`proven`, the evidence line, the next step), and that the button that proves a maker really works.
