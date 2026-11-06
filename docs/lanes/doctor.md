# Lane: doctor ("Is your computer ready?")

Read-only environment checks, as a command line tool and as a station extension. Package: `myradio/doctor/`. Tests: `tests/test_doctor_*.py`. This document is the contract other lanes, the UI and reviewers can rely on.

## 1. Purpose and honest status

**Purpose.** Look at the computer myradio runs on and say, in plain words, what is fine, what is worth a look and what must be fixed first. It powers the "Is your computer ready?" checklist (first-run step 4 and Settings) and gives agents the same answer through one read-only operation.

**What it never does.** It never installs, downloads, writes, deletes, changes permissions, asks for a password, uses `sudo` or reaches the internet. "How to fix it" lines are text for the person to follow; no code path runs them. Programs are started in one place only (`tools.run_tool`): an argument list (never a shell string), a deadline, a bounded read, and the whole process group is killed on timeout. The only thing the package ever opens for writing is the null device (`os.devnull`), and only after its own standard output or error has already failed, so that Python's last flush at exit cannot turn a good result into exit status 120 (section 2). These properties are enforced by tests, including a static scan of the package source (section 7) that pins exactly those descriptor calls.

**Honesty rules it follows.** It reports what it detects, never what it assumes. A graphics device that is found is "Detected", and every such row says that whether music and voices run is decided by their own setup checks. Mac support is reported as detection only ("has not been verified yet"), and **every Mac row is a `warn`, never `ok`**: a row that says "not verified" must not be shown as "Ready", so a Mac cannot read as fully ready and the command exits 1 at best on a Mac until Mac support has been verified. The FFmpeg row says what FFmpeg's own listings contain ("Its own list says it can read ... No real file was tried"), not that a file was read.

### What is real, what is only fake-tested, what is unverified

| Area | Status |
| --- | --- |
| Command line, JSON, text report, exit statuses | **Real**: executed here on Linux (Ubuntu 24.04.4, x86-64, Python 3.12.3), including as a real child process. |
| Output that cannot be delivered (reader gone, output closed, disk full) | **Real**: real child processes with a real pipe whose read end is closed before the report prints, a real closed standard output (`>&-`) and `/dev/full`. Exit status is the report's own in every case; before this fix it was 120 or 1 with a traceback. |
| Each check against this real Linux computer | **Real**: Python, FFmpeg 6.1.1 (real binary: version, encoder and decoder listings parsed), disk, folder permissions (real `chmod` modes), SQLite 3.45.1, file locks, timezone data, clock, memory, cores, Intel graphics detected through `/dev/dri` and sysfs (driver `xe`). |
| Sockets (private connection, busy port) | **Real**: sockets on 127.0.0.1 only. Busy-port detection uses a real listening socket on a port the operating system chose (port 0). **The tests never bind a fixed port.** An earlier version of one test (`test_every_check_survives_being_called_with_nothing_at_all`) did bind 127.0.0.1:8765 twice per run (nothing listened and it was released at once; found by review, now fixed). A guard in `tests/test_doctor_fixtures.py` now refuses any bind to a port the operating system did not hand out and fails the test; an audit of every bind in a whole lane run after the fix (326 tests) found only port 0 and ports the system had just chosen. |
| Process handling (hung tool, flood, orphaned child, cancel) | **Real**: tiny real programs written to a temporary folder; the heartbeat files prove they were stopped. |
| Report time with every tool hanging | **Real, measured**: 4.01 s with the default limits (three runs: 4.01, 4.01, 4.01 s). Healthy: 0.7 to 0.9 s with the real FFmpeg, about 0.02 s with a fake one. |
| Merging other extensions' checks | **Real**: run once inside a real `Station` with all seven built-in extensions loaded (generation, shows, ads, research, hosts, prompts, doctor): 29 ok, 1 warn (`generation.config`), 1 unknown (`research.network`), no leftover threads. Repeated in the review round with the other lanes' current code: the same counts, 0.87 s. Also covered by tests with a stand-in neighbour. |
| macOS (Apple silicon, Intel, Rosetta, `system_profiler`, `sysctl`) | **Verified only against fakes**: the text shapes are hand-written fixtures (`tests/test_doctor_fixtures.py`). No Mac was available. The exact wording of `sysctl: unknown oid` and the keys `SPDisplaysDataType`, `sppci_model`, `sppci_cores` come from knowledge of macOS, not from observation. Every Mac row is a `warn` (detected, not verified). |
| Other Linux families (Debian, Arch, Fedora, Mint), NVIDIA and AMD cards | **Verified only against fakes**: os-release dictionaries and `/dev`, `/sys` trees built in temporary folders. |
| Full disk, missing timezone data, hung network drive, Windows | **Verified only against fakes**: patched `shutil.disk_usage`, patched `zoneinfo`, a patched function that blocks, patched `platform`. |
| Python 3.10 | **Not run** (not installed here). All twelve files parse with the 3.8, 3.9 and 3.10 grammar (checked with `ast.parse(feature_version=...)` in the review round), and the doctor's tests pass on 3.12 and 3.13 (and, in the first round, 3.11). |
| `fix` commands (apt, brew, pacman, chmod, chown, usermod, pip) | **Unverified**: text only. Nobody ran them to confirm they work on every release. |

## 2. Public API

Everything is standard library only. Nothing here raises for a bad environment: failures become `unknown` results.

### The two shapes

A **check** has exactly these keys, in this order:

```json
{"id": "ffmpeg", "title": "FFmpeg", "status": "warn",
 "detail": "FFmpeg was not found on this computer.",
 "hint": "FFmpeg is a free tool that lets myradio read MP3 ... Install it, then restart myradio.",
 "fix": "Open a terminal and run: sudo apt install ffmpeg"}
```

* `status` is one of `ok`, `warn`, `fail`, `unknown`. `detail` says what was found. `hint` says what it means and what to do next: **always non-empty for anything that is not `ok`**. `fix` is optional instruction text for this kind of computer, `""` when there is none. `id` is a short slug of at most 80 characters: the doctor's own ids are lowercase letters, digits and hyphens (`disk-space`), and checks from other extensions arrive as `<extension>.<id>` and may also contain dots and underscores (for example `generation.music_files`). `title` is at most 120 characters, the rest at most 400.
* All text is cleaned: control, direction-override and unassigned characters become `?`, white space is collapsed (not in `fix`), and paths under the home folder are written `~/...`.
* **Shape of `fix`.** A fix is either plain advice (no `: ` in it) or `instruction: command`, for example `Open a terminal and run: chmod 700 ~/.myradio`. The command, when there is one, is **the text after the first `: `, up to a two-space note** (`sudo apt install ffmpeg  (Homebrew ...)` has the note after two spaces). That is the part a Copy button must copy, never the instruction. A command is shown whole or not at all: it is never changed (a path with control characters gets advice instead of a command) and never cut by the 400-character limit (a command that would not fit is replaced by the general sentence `Use a folder just for your station and start myradio with, for example: myradio --data-dir ~/.myradio`, because a command cut in the middle of a quoted path leaves a terminal waiting for the closing quote). The checks tests verify, for every fix they produce, that the command part parses as one shell command (`well_formed` in `tests/test_doctor_checks.py`).

A **report** has exactly these keys, in this order (JSON is plain ASCII, `indent=2` on the command line):

```json
{"schema": 1, "myradio_version": "0.3.2", "generated_at": "2026-11-06T01:18:07+00:00", "duration_ms": 798,
 "overall": "warn", "summary": "Your computer can run myradio. 1 thing is worth a look.",
 "counts": {"ok": 13, "warn": 1, "fail": 0, "unknown": 0}, "partial": false, "checks": [ ... ]}
```

* `overall` is the worst state (fail, then warn, then unknown, then ok). `counts` always lists the four states in the order ok, warn, fail, unknown. `partial` is true when some check did not finish before the deadline (its row is `unknown` and says so).
* `schema` changes only if a key is removed or its meaning changes; new keys may be added at any time, so readers must ignore keys they do not know. The extension adds `cached` (bool) and `age_seconds` (float) at the end.

### Modules

`myradio/doctor/__init__.py` exports `CHECKS`, `DEFAULT_PORT`, `Settings`, `ReportRunner`, `run_report`, `render_text`, `exit_status`.

`myradio/doctor/checks.py` (pure checks; imports only the standard library, and `sqlite3`, `zoneinfo`, `fcntl` lazily so a Python without them still gets an answer):

* `Settings(data_dir=None, port=8765, in_service=False, tool_timeout=3.0, profiler_timeout=4.0, dev_root="/dev", sys_root="/sys", proc_root="/proc", etc_root="/etc", now=time.time, cancel=None)`: frozen dataclass. `data_dir=None` means `MYRADIO_DATA_DIR`, else `~/.myradio` (the service's own default). The `*_root` fields and `now` exist so tests can point the checks at a made-up computer. `cancel` is set by the runner.
* `CHECKS`: tuple of `CheckSpec(id, title, func)` in display order (below). `func(settings=None) -> check`.
* `check_python(settings=None, *, version=None, implementation=None)`, `check_ffmpeg`, `check_disk_space`, `check_data_folder`, `check_loopback`, `check_port`, `check_system`, `check_sqlite`, `check_fcntl`, `check_tzdata`, `check_clock`, `check_memory`, `check_cpu`, `check_accelerator`: each `(settings=None) -> check`. Each is guarded: an unexpected exception inside becomes an `unknown` result.
* Helpers: `platform_family() -> "macos"|"debian"|"arch"|"linux"|"windows"|"other"` (decides which install line to show), `resolve_data_dir(settings)`, `parse_codec_list(text) -> set`.
* Constants worth knowing: `DISK_WARN_BYTES = 2 GiB`, `DISK_FAIL_BYTES = 200 MiB`, `MIN_PYTHON = (3, 10)`, `TESTED_PYTHON_MAX = (3, 13)`, `SQLITE_MIN = (3, 8, 8)`, `MEMORY_WARN_BYTES = 4 GiB`, `MP3_ENCODER = "libmp3lame"`, `IMPORT_DECODERS`.

`myradio/doctor/tools.py`: `run_tool(args, *, timeout, max_bytes=262144, cancel=None) -> ToolResult` (never raises; `ToolResult(ran, exit_code, output, timed_out, truncated, cancelled, reason)` and `.ok`), `clean_text(value, limit=400, *, collapse=True)`, `display_path(path)`.

`myradio/doctor/report.py`:

* `ReportRunner(specs=None, *, monotonic=time.monotonic, clock=time.time)` with `.run(settings=None, *, budget=6.0, extra=None) -> report` (never raises) and `.close(wait=1.0)`. `extra` is a callable returning checks from elsewhere (the extension passes `station.extension_checks`). One runner serialises its own reports.
* `run_report(settings=None, *, budget=6.0, extra=None, specs=None)`: one-shot build, run, close.
* `normalize_check(raw, *, check_id=None, title=None) -> check | None`, `summarize(checks) -> {overall, summary, counts}`, `exit_status(report) -> 0|1|2`, `render_text(report, width=78) -> str`, `iso_utc(epoch) -> str`.

`myradio/doctor/__main__.py`: `main(argv=None) -> int` and `build_parser()`. `python -m myradio.doctor`; the console script is `myradio-doctor` (section 8).

`myradio/doctor/extension.py`: `DoctorExtension(*, monotonic=time.monotonic, runner=None, budget=6.0)`, `build() -> Extension`, `CACHE_SECONDS = 30.0`, `REFRESH_INTERVAL = 5.0`.

### Command line

```
python -m myradio.doctor [--json] [--data-dir DIR] [--port PORT]
```

* Text by default (every state is a word: OK, WARN, FAIL, UNKNOWN; help only under rows that need it). `--json` prints only the report JSON. `--data-dir` is the station folder to look at. `--port` is the control page port to test (default 8765, `0` means any free port).
* **Exit status**: `0` every check ok. `1` nothing failed, but something warned or could not be checked (on a Mac this is the best possible answer for now, because every Mac row is a `warn`). `2` at least one check failed. `64` the command was typed wrongly (nothing is printed on stdout). `130` interrupted. `main()` returns these numbers instead of raising or exiting, so it is safe to call from Python.
* **The status describes the computer, not the printing.** If the report cannot be delivered, the status is still the one the report earned, and the program never ends with a traceback or with Python's own status 120 ("could not flush output on exit"):
  * the reader went away (`myradio-doctor --json | consumer-that-already-quit`): silent, like any command in a pipe;
  * the output is closed (`>&-`) or full (a full disk, `> /dev/full`) or fails in any other way: one plain sentence on standard error, `The report could not be written because the output is closed or the disk is full. Run the command again in a normal terminal window, or free up some space.`;
  * in both cases, if the failing output is the program's own standard output or error, whatever is still waiting in it is sent to the null device (`os.dup2` of `os.devnull` onto that descriptor) so that Python's last flush at exit cannot fail. Only `sys.__stdout__` and `sys.__stderr__` are touched. A caller that runs `main()` inside its own process and finds its standard output broken therefore sees later writes to it silently dropped instead of raising; any other stream a caller passes in (for example a redirected buffer) is never redirected.
  * `--help` and usage mistakes get the same treatment (`--help | consumer-that-already-quit` exits 0).
* It never creates the station folder, and it never opens any file inside it.

## 3. Persistence

None. The doctor keeps no state of its own on disk.

* The host creates an empty `<data>/ext/doctor/` folder because every extension gets one. Nothing is written into it.
* `persistent_files()` is empty: nothing belongs in a backup.
* Nothing the doctor handles is secret. It never opens `control.token`, `agent.json`, `broadcast.json`, the database or any media file (a test makes `open()` fail during the folder and disk checks to prove it). The report does contain facts about the computer (system version, hardware names, tool versions) and paths with the home folder shortened to `~`.
* The cached report and the status summary live in memory only.

## 4. Extension surface

Name: `doctor`. Loaded by `myradio.extensions.builtin_extensions()`.

### Operation

`doctor.report`: "Check whether this computer is ready for myradio and list anything that needs attention. Read-only: nothing is installed or changed."

* Arguments: `refresh` (boolean, optional): check again instead of using the last answer (honoured at most once every 5 seconds). No path, port or command can be passed, by design: an agent cannot point the doctor anywhere.
* Result: the report (above) with `cached` and `age_seconds`, merged into the usual `{"ok": true, "operation": "doctor.report", ...}`.
* Appears in `/api/operations`, so it is in the Operator tool list and the MCP catalog without any change there.

### Route

`GET /api/x/doctor/report` (authenticated and origin-checked by the host like every extension route; `max_body` is the default 32 KiB and unused).

* Optional query `refresh=1` (also `true`, `yes`, `on`). Any other value, or no value, means "use the cached answer".
* Response `200` with the report plus `cached` and `age_seconds`. `400 {"error": "..."}` with a plain sentence if the check could not run (for example "The computer check could not run just now. Try again in a moment."). `401` and `403` come from the host. Other methods and paths are `404`.
* **Cache**: the answer is kept for 30 seconds from the moment it finished. A refresh is honoured only when the stored answer is at least 5 seconds old. Concurrent requests share one run (the others wait for it, then read it). A clock that moves backwards is treated as "expired". Every caller gets its own copy.
* The station folder is always the station's own (`ctx.directory`); nothing in the request changes what is looked at.

### Status

`status()` is O(1) and lock-free: it copies a small snapshot taken when the last report finished. It never runs a check and never calls the station. Before the first report: `{"checked": false}`. After:

```json
{"checked": true, "overall": "warn", "summary": "Your computer can run myradio. 1 thing is worth a look.",
 "counts": {"ok": 29, "warn": 1, "fail": 0, "unknown": 1}, "checked_at": "2026-11-06T01:09:51+00:00",
 "attention": ["generation.config"]}
```

`attention` lists up to 12 ids whose state is `fail` or `warn`; a row that is only `unknown` ("could not be checked") is counted in `counts` but is not listed. The status is `{"checked": false}` until something has asked for a report (the first request to the route or the operation), so a page that wants a badge must request the report once (section 5).

### Doctor checks, events, panels

* `checks()` returns an empty list (`[]`, not a tuple), so the doctor's own checks are not counted twice (they are in the report, not in `station.extension_checks()`).
* Events consumed: none (`on_event` is not overridden, so the host starts no mailbox thread).
* Panels: none. The UI draws the checklist from the route (section 5).
* **Nothing is checked while the station is still starting.** `ExtensionHost.checks()` reports extensions that have not finished starting as failed, so the doctor asks for other extensions' checks only when a request arrives, never in `start()`.
* Other extensions' checks are collected on a worker thread under the same deadline as everything else (a neighbour whose `checks()` hangs produces one `unknown` row, `extensions`, and a partial report). They are cleaned, deduplicated by id, limited to 100, and any id starting with `doctor.` is dropped.

### The checks

Order is the display order. "Needs" means what makes the row not `ok`.

| id | Title | Looks at | ok | warn | fail | unknown |
| --- | --- | --- | --- | --- | --- | --- |
| `python` | Audio engine | `sys.version_info`, implementation | CPython 3.10 to 3.13 | other implementation, or newer than 3.13 | older than 3.10 | (only on an unexpected error) |
| `ffmpeg` | FFmpeg | `ffmpeg` on the search path (else the usual install folders, to explain "not on the path"); runs `-version`, `-encoders`, `-decoders` and `ffprobe -version` side by side. The version is the first line of the output that begins `ffmpeg version`, so a notice printed before the banner does not matter | found, MP3 encoder (`libmp3lame`) listed, MP3, FLAC, Ogg, AAC and M4A, AIFF decoders listed. The message says "Its own list says it can ... No real file was tried": only FFmpeg's listings were read | not found, installed but not on the path, cannot start, does not answer like FFmpeg, no MP3 encoder, a decoder group missing | never (FFmpeg is optional: WAV import and local playout work without it) | timed out, cancelled, listings unreadable or cut short |
| `disk-space` | Disk space | free space where the station folder is (or will be: nearest existing parent) | 2 GiB or more | under 2 GiB | under 200 MiB | cannot be measured |
| `data-folder` | Station folder | existence, type, `os.access`, permission bits | closed to other accounts and usable (or missing but can be created) | group has access | others have any access, not a folder, cannot read/save, cannot be made, cannot be looked at | unexpected operating-system error |
| `loopback` | Private by default | can a socket bind 127.0.0.1 (port 0) | yes | never | no | this program is out of file handles |
| `port` | Control page port | bind 127.0.0.1:PORT with the same options as the service (never listens, never connects) | free; port 0; or skipped inside the running station | busy; not allowed (below 1024: "ports below 1024 are reserved"; any other port: "a firewall or security program may be blocking that port", with the next port as the example) | never | invalid number, other error |
| `system` | Operating system | `platform`, `/etc/os-release`; on an x86 Mac `sysctl -n sysctl.proc_translated` (taken from `/usr/sbin`, `/usr/bin`, `/sbin`, `/bin` first, the search path only as a fallback) | Linux x86-64 or ARM64 | other processor or system, Rosetta, **every Mac** (Apple silicon or Intel: "detected, Mac support has not been verified yet", with a hint that nothing needs fixing) | Windows | a Mac that will not say Intel or Apple silicon |
| `sqlite` | Station database | version, build options (`OMIT_WAL`), foreign keys, in memory | 3.8.8 or newer with WAL and foreign keys | never | module missing, too old, built without them | cannot be tried |
| `fcntl` | One copy at a time | `fcntl.flock` on a read-only handle of the doctor's own source file | works | never | missing or errors | out of file handles |
| `tzdata` | Timezone names | loads UTC, America/New_York, Europe/London, Asia/Tokyo; winter and summer offsets differ | all load, DST visible | none, some, no DST, or no module | never | never |
| `clock` | Clock and timezone | clock window 2024-01-01 to 2100-01-01; zone name from `TZ`, `/etc/localtime`, `/etc/timezone`; offset agreement | plausible, name known and agrees | `TZ` names a zone that does not exist, name and clock offset disagree | clock outside the window | clock unreadable, zone name unknown or not checkable |
| `memory` | Memory | `/proc/meminfo`, else `sysconf`, else `sysctl hw.memsize` | 4 GiB or more total and 512 MiB or more free | under 4 GiB total, or under 512 MiB free | never | not reported |
| `cpu` | Processor | `os.sched_getaffinity`, else `os.cpu_count` | 2 or more | 1 | never | not reported |
| `accelerator` | Graphics hardware | Linux: `/dev/dri/renderD*`, sysfs vendor and driver, `/dev/nvidia*`, tool names `clinfo`, `sycl-ls`, `nvidia-smi` (found by name, **never run**). macOS: Apple silicon plus one guarded `system_profiler -json SPDisplaysDataType` | something detected on Linux | nothing detected; detected but this account may not open it; Intel Mac; **an Apple silicon Mac** (detected, Mac support not verified) | never | unknown Mac chip; other systems |

`fix` text by kind of computer: macOS `brew install ffmpeg` (and `brew reinstall`), Debian family `sudo apt install ffmpeg` and `tzdata`, Arch `sudo pacman -S ...`, others a plain "use your system's package manager or ffmpeg.org" sentence (no guessed commands). Folder fixes use `chmod 700` with the path quoted for a shell (`~` kept for the home folder). `sudo chown -R "$USER"` is offered only when the folder is owned by root, this program is not, **and the folder is inside the person's own home folder** (the usual result of running myradio with sudo once: `~/.myradio`). A system folder that happens to be owned by root (`/usr/local`, `/var/lib`, `/etc/ssl`) and a folder that belongs to any other account get "start myradio with a folder of your own" instead, because a recursive ownership change of a system tree, or taking over someone else's folder, is never advice the doctor gives. A folder that is too general to lock down (the home folder, `/`, `/tmp`) gets "use a folder just for your station" instead, a path containing control characters never appears in a command, and a command longer than the 400-character text limit (a folder path of about 365 characters or more) is replaced by the same general sentence rather than cut short. The hint for a folder that others can open also says that a USB stick or network drive may not be able to keep a folder private and that a folder on the computer's own drive should be chosen (permission bits on such drives always look open, so `chmod` cannot help there).

## 5. UI contract

For the first-run "Is your computer ready?" step and Settings. The doctor owns the wording of title, detail, hint and fix; the UI owns layout.

**Data.** `GET /api/x/doctor/report`. Use the `summary` sentence as the headline and `overall` for the badge. Never show ids to people (they are for code and agents).

**Rows.** Show each check as: state icon plus a state word, title, detail. Never convey state by colour alone. Suggested state words: `ok` "Ready", `warn` "Needs a look", `fail` "Needs fixing", `unknown` "Couldn't check". For anything that is not `ok`, show the `hint` as a paragraph under the detail. If `fix` is not empty, offer a "How to fix it" disclosure that shows it in a monospaced block, labelled "Steps for you to follow. myradio never runs these for you." Do not render `fix` as a button that runs anything. **The Copy button copies the command only, not the whole text**: the command is the part after the first `: ` and before any two-space note (section 2, "Shape of `fix`"); a `fix` with no `: ` is plain advice and gets no Copy button. Copying the whole text would paste a sentence such as "Open a terminal and run:" into a terminal. In the text report (`render_text`) a fix that holds a command is printed on one line without wrapping so that it can be copied whole.

**Which rows first.** The first-run list should show these ids: `python`, `ffmpeg`, `disk-space`, `data-folder`, `loopback`, then rows from other extensions (those with a dot in the id; group them by prefix: `generation` "Music and voices", `shows` "Shows", `ads` "Spots", `research` "Discover", `hosts` "Hosts", `prompts` "Styles"). Put `port`, `system`, `sqlite`, `fcntl`, `tzdata`, `clock`, `memory`, `cpu`, `accelerator` under "More details", and move any of them out of it when its state is `warn` or `fail` (on a Mac, `system` and `accelerator` are always `warn`, so they always come out). Ready rows can be collapsed to "N things are ready" once the list is long.

**Two rows have no dot in their id and belong to none of the groups above.** They are placeholders the doctor adds itself, always `unknown`, and they mean part of the answer is missing, so never hide them: `extensions` ("Other parts of myradio") stands in for the rows of the other extensions when those could not be collected in time or at all (show it first in the group of rows from other extensions, and the report is `partial` when it timed out); `doctor` ("Computer check") appears alone when the whole check could not be built (show it as the only row, with the retry button). Show both like any other `unknown` row, with their `hint`.

**The badge needs one request.** `status.extensions.doctor.status` stays `{"checked": false}` until something has asked for the report. The page should therefore request `GET /api/x/doctor/report` once when it loads (the answer is kept for 30 seconds, so this is cheap) and then use the status block for the badge; do not show "0 things need fixing" for `checked: false`.

**Next-step bar and Operator.** `status.extensions.doctor.status` is enough for a small badge: when `counts.fail > 0` say "N things need fixing" and link to the checklist; `attention` lists which. The Operator can answer "is my computer ready?" by calling `doctor.report`.

**States and wording.**

* Loading (the first request can take a few seconds; the report never takes longer than about 6 seconds): "Checking your computer..."
* Request failed, a non-200 reply or a body that is not a report: "We couldn't check your computer just now. Try again." with a retry button. Do not invent rows.
* `partial` is true: "Some checks took too long. Check again for a complete answer."
* "Check again" calls `?refresh=1`; disable it for 5 seconds after a click (the server also limits it). Show "Checked just now" or "Checked N seconds ago" from `age_seconds`.
* Empty `checks` should never happen; if it does, "Nothing to show yet." plus the retry button.
* The graphics row says "Detected", never "Supported" or "Ready for music". On a Mac the system and graphics rows are `warn` ("Needs a look") and say Mac support has not been verified yet; keep that sentence. Their hint says nothing needs fixing: show it as the explanation of the state, and do not turn a Mac into an alarming screen. The report on a Mac is never "ready", by design.
* Missing optional pieces (FFmpeg, graphics card, timezone data) never block "Start" with the user's own audio: they are `warn`, not `fail`.

## 6. Limits and bounds

| What | Bound |
| --- | --- |
| Time for one tool call | 3 s (`ffmpeg`, `ffprobe`); `sysctl` 2 s when it runs alone (the operating system and memory rows) but 4 s in the graphics row, where it runs beside `system_profiler` (4 s); then the whole process group gets SIGKILL |
| Output read from one tool | 256 KiB; a tool that prints more is stopped and the listing counts as "cut short" |
| Whole report | 6 s budget; checks that are still running at the deadline become `unknown` and `partial` is true. Measured with every tool hanging: 4.01 s |
| Threads | at most one per check (14) plus one for other extensions plus up to 4 short tool helpers per run; a check that is still stuck from an earlier report is not started again (so a dead network drive costs one stuck thread, not one per report); all daemon threads; `close()` cancels tools and joins |
| Reports at once | one per runner (callers wait their turn); the extension also runs one at a time |
| Cache | 30 s; refresh at most every 5 s |
| Text | title 120 characters, detail, hint and fix 400; ids 80. A command that would not fit is replaced by a sentence, never cut |
| Sizes shown | rounded down, with 1 GB = 1,024 MB (as Linux counts; the Finder on a Mac counts 1,000, so a Mac figure can differ by up to 7%). Rounding down means a figure just under a limit never reads as the limit |
| Other extensions' checks | at most 100 accepted |
| Files read | `/proc/meminfo` (64 KiB), sysfs vendor files (64 bytes), `/etc/timezone` (200 bytes), `/etc/localtime` (as a link), its own source file (for the lock test). Folder listings: `/dev/dri` 64 names, `/dev` 2048 names |
| Memory | a few hundred KiB at most |
| Disk and network | nothing written; no outbound connection (a test makes any `connect` fail) |

Thresholds that are judgement rather than fact: memory 4 GiB and 512 MiB free, "1 core", the clock window, the Python test range (the project's stated 3.10 to 3.13), the SQLite floor 3.8.8 (the oldest version that has everything the station and backup code use: WAL, foreign key checks, truncating checkpoints; newer features that optional lanes may use are not tracked). Disk thresholds are the brief's 2 GB and 200 MB, taken as GiB and MiB, and the figures shown are rounded down (1.99 GiB free is shown as "Only 1.9 GB free", never as the 2.0 GB limit; 199.6 MiB as "199 MB").

## 7. Test inventory, commands and measured results

Lane command (runs every test file the lane owns):

```
cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_doctor*.py" -v
```

| File | Tests | What it proves |
| --- | --- | --- |
| `test_doctor_tools.py` | 31 | the tool runner against real misbehaving programs (hang, flood, orphaned child, cancel, bad arguments, no shell, closed stdin, binary output); text clean-up; home-folder shortening |
| `test_doctor_checks.py` | 163 | every check on simulated Linux (Ubuntu, Arch, Fedora, no os-release), macOS Apple silicon, Intel and Rosetta (every Mac row asks for a look and none says "ready"), missing FFmpeg, FFmpeg without an MP3 encoder or decoders, a notice printed before the FFmpeg banner, a hanging FFmpeg, full disk at the exact thresholds with sizes rounded down, the hours-of-music figure and the memory and clock limits written out as numbers and dates, unreadable and open data folders, system folders never offered a `chown`, commands shown whole or not at all (exact 400-character boundary, with and without quotes), the drive note, a busy port (real socket), a refusal on an ordinary port, the Mac tool lookup order, missing and partial tzdata, wrong clock, odd zone names, GPU trees, hostile paths and text; random text through every parser; "nothing raises" under 20 kinds of sabotage; wording is plain, every warn, fail and unknown has a hint and every command in a `fix` parses as one shell command; no check may bind a fixed port (a guard fails the test) |
| `test_doctor_report.py` | 59 | report shape and key order, summary sentences, exit status, guarded parallel execution (raise, hang, nonsense, stuck check not restarted, side by side, concurrent runs, close cancels), other extensions' checks folded in and limited to exactly 100, text rendering (a command is never broken across lines), JSON stable across runs on a fully simulated computer, a hung drive, every tool hanging with the real limits, a whole real run that binds only port 0, and the static scan of the source |
| `test_doctor_cli.py` | 31 | exit status for every state mix, text and JSON output, the port range 0 to 65535 exactly, argument errors (64), `--help`, hostile folder names, non-ASCII terminals, interruption, an output that fails in every way (in process), and with real child processes and real pipes: a reader that has gone before the report prints, a closed output, `/dev/full`, `--help` into a closed pipe, and the real command |
| `test_doctor_extension.py` | 42 | registration with the real `Station`, the route, the 30 s cache and 5 s refresh limit with a hand-moved clock, concurrent requests share one run, merged neighbour checks, nothing asked during start, status shape and copies (an unknown row is not "attention"; status answers at once while a report holds the lock and never calls the station, on the instance the station runs), the operation and its refused arguments, `checks()` is `[]`, stopping closes the runner exactly once and cuts a running program short, a stuck neighbour does not hold the stop up, the real HTTP server (auth, origin, methods) |
| `test_doctor_fixtures.py` | 0 | shared fixtures and fakes, including the port guard (named `test_doctor_*` only to stay inside the lane's paths) |

Static scan (in `test_doctor_report.py`, `ReadOnlyAndOfflineTests`): over every file in `myradio/doctor/` it asserts no write, delete, permission change, process signal (outside `tools.py`), `eval`/`exec`, `shell=` argument, outbound connection call or networking import; every `open()` is read-only; the only `os.open`, `os.dup2` and `os.close` calls are the three in `__main__.py` that point a failed output at the null device (a second write-mode open anywhere fails the scan; checked by changing the code on a scratch copy); `subprocess` is used only in `tools.py`; no argument list starts with `sudo`, a package manager, `rm`, `chmod` and so on; and the only programs ever looked up are `ffmpeg`, `ffprobe`, `sysctl` and `system_profiler` (plus `clinfo`, `sycl-ls`, `nvidia-smi` by name for the hint, never run).

**Measured results** (all on this Linux computer, run under `nice -n 19 ionice -c3`; the first round's figures that were not repeated are marked):

| Command | Result |
| --- | --- |
| Lane command, Python 3.12.3 | 326 tests, 0 failures, 0 errors, 30.8 s |
| Same, Python 3.13.14 (the project's `.venv`) | 326 tests, OK, 31.3 s |
| Same with `-X dev -W default` (3.12.3) | OK, 31.3 s, no warning or traceback in the log |
| Same, Python 3.11 and 3.10 | **not run in this round** (first round: 3.11.15, 284 tests, OK) |
| Same with a tripwire folder first on `PATH` (fake `ffmpeg`, `ffprobe`, `clinfo`, `sycl-ls`, `nvidia-smi`, `system_profiler`, `sysctl` that log if started) | OK; the log stayed empty: no test starts any real system tool |
| Audit of every `socket.bind` in a lane run (audit hook, 326 tests) | before the fix: `127.0.0.1:8765` twice, from `test_every_check_survives_being_called_with_nothing_at_all`; after: none. Only port 0 and ports the system had just handed out |
| Repeats of the files that start processes or threads | `test_doctor_cli.py` and `test_doctor_extension.py`, 10 runs each in a row over the review round (6, then 4 on the final tree), all OK; no doctor process left behind |
| Mutation checks (one change to the code on a scratch copy; none made in the repository) | 26 changes, all noticed by a failing test: `stop()` doing nothing, `stop()` closing twice, `status()` calling the station, `status()` taking the lock, "attention" listing unknown, `checks()` returning a tuple, the hour constant without the channel count, a database file written by `check_sqlite`, port range to 65536, memory limit `<` to `<=`, the 100-check limit to 1000, the clock window start to 2001 and end to 2200, banner search anchored again, banner search over any white space, sizes rounded again, command length not checked, `chown` offered anywhere, every refusal blamed on ports below 1024, a Mac row `ok` again, Mac tool lookup by search path first, the fix wrapped again, the Rosetta wording, the drive note removed, the output handler removed, and the original fixed-port test put back |
| The final 326 tests run against the unfixed code (a copy of the repository with the original doctor modules) | 65 failures and 15 errors; with the fixed code all pass. Findings that were weaknesses in the tests only (the `stop()` test, the status test, the hour constant, the four unpinned limits, the database test) are covered by the mutation row above instead |
| Seam tests `tests/test_extensions.py` and `tests/test_extensions_station.py` | 45 tests, OK |
| Ownership checker for lane `doctor` (final run) | 13 changed files (the six modules, six test files and this document), "No out-of-lane changes". The first run of this round also listed `tests/test_station.py` as a change that belongs to no lane; this lane never touched that file and the final run no longer listed it |
| Stray processes after the runs | none (checked with `ps`) |
| Real command on a private scratch folder (real FFmpeg 6.1.1, `--port 0`) | exit 0, 14 ok, 0.81 to 0.84 s (three runs) |
| Real command on a 0755 scratch folder | exit 2, 13 ok, only `data-folder` failed; the `chmod 700` fix is on one line in the text report |
| Real command on a folder that does not exist | exit 0, the folder was not created |
| Real command with `--port nope` | exit 64, plain message on stderr, nothing on stdout |
| Real command, reader gone before the report prints / output closed / `> /dev/full` / `--help` into a closed pipe (fake FFmpeg first on `PATH`, `--port 0`) | before: 120, 1 with a traceback, 120, 120. After: the report's own status (0 here) in every case; silent for the reader that left, one plain sentence for the other two |
| Report time with every tool hanging (default limits) | 4.01 s, three runs (4.01, 4.01, 4.01) |
| `station.close()` with a neighbour whose check never answers | 1.01 s, three runs; the report that was waiting came back at 6.00 s |
| Whole suite, every lane (`python3 -m unittest discover -s tests -p "test_*.py"`), final tree | 2788 tests in 385 s: OK, 1 skipped, 10 expected failures (the soak lane's recorded core defects, not this lane). All 326 doctor tests, `test_agent_deadline.py` and `test_model_transport.py` passed in it. An earlier whole-suite run in this round (2569 tests, 305 s) had 11 failures and 7 errors, all in `test_ads_*` and `test_hosts_*` while those lanes were being edited by other agents; the same failures appeared with the original doctor modules swapped in (checked on a copy of the tree), so this lane did not cause them, and they were gone in the final run. |

Line counts (`wc -l`): code `myradio/doctor/*.py` 2,008 (checks 1,177, report 314, tools 228, `__main__` 145, extension 134, `__init__` 10); tests `tests/test_doctor*.py` 4,205 (checks 1,780, report 756, extension 619, cli 440, fixtures 309, tools 301).

Tests are deterministic, use disposable temporary folders, bind 127.0.0.1 only, only with port 0 or a port the system has just handed out (a guard fails any test that asks for a fixed port), finish in about 30 s on a busy computer, and clean up threads and processes. Every fake program that must be stopped ends itself with `SIGALRM` after 20 seconds even if the doctor failed to stop it. The tests that need a reader to leave a pipe before the report prints do not rely on timing: the child program waits for a "go" file that the test creates after closing its end.

**Flaky tests found and fixed.** First round: repeating the doctor tests turned up two timing assumptions of my own: a JSON-stability test compared timestamps from two runs that could straddle a second boundary (fixed by giving the runner a fixed clock), and process tests that assumed a fake program starts within a fraction of a second (they now wait for the program's own marker file, or accept that a program that never started is not running). After the first fix, 12 repeat runs in a row passed on Python 3.13; the second fix came after that, and every version then passed once more. One earlier 3.13 run reported a single error that I could not reproduce; the slow-start assumption is the likely cause but this was not confirmed. Review round: a reviewer pointed out a latent flake, not seen in 12 runs: `test_it_never_writes_a_database_file` compared a snapshot of the whole project folder, which other programs write to at the same moment; it now looks in a private empty folder.

## 8. Requested changes to shared files

None of these were made; they are for the owners.

1. **`pyproject.toml`** (orchestrator): add the console script

   ```toml
   [project.scripts]
   myradio-doctor = "myradio.doctor.__main__:main"
   ```

   `main()` returns the exit status as an int, which the generated wrapper passes to `sys.exit`. No package-data entry is needed (`include = ["myradio*"]` already covers `myradio.doctor`). `python -m myradio.doctor` works without it.
2. **`myradio/extensions.py`**, `ExtensionHost.checks()`: carry the optional `fix` key through (cleaned, at most 400 characters). Today only id, title, status, detail and hint survive, so the install instructions another lane writes never reach the UI. Also worth a comment in that method: it reports extensions that have not finished starting as failed, which is why the doctor never calls it from `start()`.
3. **`myradio/web/**`**: draw "Is your computer ready?" from `GET /api/x/doctor/report` as described in section 5, replacing the "(sample reading)" rows of the preview; add the badge from the doctor's status block, after requesting the report once on load (the status is `{"checked": false}` until then); "Check again" uses `?refresh=1`. Three rules from section 5 are easy to miss: the Copy button copies only the command part of a `fix`; the `extensions` and `doctor` placeholder rows have no dot in their id and must still be shown; on a Mac the system and graphics rows are `warn` by design.
4. **Chat / Operator layer**: if there is a way to mark an operation as read-only (no approval prompt), mark `doctor.report`. It takes no path, port or command and changes nothing.
5. **`myradio/server.py`** (optional): the doctor repeats two defaults, port 8765 and `MYRADIO_DATA_DIR` / `~/.myradio`. Exposing them from one place would stop them drifting. The doctor never needs the actual bound port because inside the running station it skips the port test.
6. **`Start myradio.command` and the installer scripts** (optional): run `python3 -m myradio.doctor` (or `myradio-doctor`) before launching and show its output when the exit status is 2.
7. **`README.md`, `docs/HANDOFF.md`, `PRODUCT.md`, `docs/RELEASE-STATUS.md`**: mention `myradio-doctor` and its exit statuses (0, 1, 2, 64); say Mac support is "detected only" (and that on a Mac the best exit status is 1, because every Mac row is a warning until Mac support is verified); record this lane's test results.
8. **Observed, not this lane's: `tests/test_station.py`.** The first ownership-checker run of the review round listed `tests/test_station.py` as a modified file that belongs to no lane. This lane never touched it, and the final run no longer listed it; it is recorded here only so that the orchestrator knows it was seen.

## 9. Known limitations and unverified claims

* **No real Mac was involved.** Every macOS statement (Rosetta, Intel or Apple silicon, `system_profiler`, `sysctl hw.memsize`, fixed `/usr/sbin` lookup, kqueue use for pipes) is checked against hand-written fixtures only. The doctor says so in its own words on a Mac.
* **No Windows.** The doctor reports Windows as unsupported and its other checks degrade to `unknown`, but this was only simulated.
* **Python 3.10 and 3.11** were not run in the latest round (3.12.3 and 3.13.14 were; the first round also ran 3.11). The files use only syntax and library calls that exist in 3.10.
* **Detection is not capability.** Graphics detection reads render nodes, vendor and driver names and looks for tool names; it does not run `clinfo`, `sycl-ls` or `nvidia-smi` and cannot know that a model will run. NVIDIA and AMD detection are fixture-tested only (this computer has an Intel card).
* **Not detected**: network drives (SQLite's write-ahead mode is unsafe on them), access control lists, SELinux or AppArmor limits, container memory or CPU quotas (memory comes from `/proc/meminfo`, cores from the process's CPU set), whether the clock is kept in sync with a time server, whether secure web connections have trusted certificates (the research lane has its own check for that), and the speed of the disk.
* **Locks and WAL are judged indirectly.** The lock test uses a read-only handle on the doctor's own source file, not the station folder's drive. WAL support comes from the SQLite version and build options; no test database is written.
* **The port test is a point in time.** "Free" means the bind worked a moment ago. It mirrors the service's socket options, so on systems where those options let two sockets share a port, the service would behave the same way.
* **Folder privacy comes from permission bits** (owner, group, others). A disk-level access control list could still grant access; it is not read. **Drives that cannot hold file permissions** (FAT and exFAT USB sticks, many NTFS and network mounts) report every file as open to other accounts, so a station folder there is a `fail` with a `chmod 700` fix that cannot work. The doctor does not detect the file system type; instead the hint for such a folder says that a USB stick or network drive may not be able to keep it private and to choose a folder on the computer's own drive. This reasoning rests on how those file systems report modes; no such drive was available to try it on.
* **Thresholds are judgement** (section 6). A computer with under 4 GiB of memory gets a `warn` that says myradio itself should run (it is not promised) and that the model setup screens state the real need. The doctor does not know how much memory YuE2 or Drama 3 need.
* **`unknown` shares exit status 1 with `warn`.** A script that must tell them apart should read the JSON.
* **"myradio does not need ffprobe today"** is written into the FFmpeg row. It was true of this source tree when written (no other file mentions ffprobe); if a lane starts using ffprobe it should add its own check.
* **The tests never probe the default port 8765** (a live station may use it): the default-port path is covered with a patched bind and real sockets on operating-system-chosen ports, and a guard fails any test that binds a fixed port. The command `myradio-doctor` itself, run by a person without `--port`, does try to bind 8765 for a moment (that is its job); the lane's tests and measurements always pass `--port 0`.
* **Stopping the station does not wake a check that is waiting on something other than a program.** Stopping cancels running programs at once (`ReportRunner.close()` sets the cancel flag and the tool runner kills the program's process group), but a check that is waiting on a stuck drive, or on another extension whose `checks()` never returns, is abandoned: the report that was waiting returns at its own deadline (6 s at most), and the abandoned thread is a daemon thread. `DoctorExtension.stop()` itself returns within about one second either way (measured with a neighbour whose check never answers: `station.close()` took 1.01 s three times, and the report that was waiting came back at 6.00 s; `tests/test_doctor_extension.py` covers both the cancelled program and the stuck neighbour).
* **Output redirection at exit.** To keep Python's last flush from turning a good result into exit status 120, a failed write to the program's own standard output or error points that descriptor at the null device. This is a change to the process, made only after the output had already failed and only for `sys.__stdout__` and `sys.__stderr__` (section 2). Tested with real child processes on Linux; not tried on macOS.
* **Prompt injection.** The report holds text that comes from the environment (folder names, tool versions, zone names). It is stripped of control and direction characters and shortened, but a folder named like an instruction is still text an Operator model would read. Treat the report as data.
* The `fix` lines are advice; some (for example `sudo usermod`, `sudo chown`) change the computer when a person runs them. They are never run by myradio, are quoted for a shell, and are withheld when the path is too general or contains control characters.

## 10. What changed after review (for readers of the earlier version)

Behaviour that other lanes, the UI or the Operator can see:

* **Output.** `myradio-doctor` keeps the exit status the report earned when the report cannot be delivered (reader gone, output closed, disk full, `--help` into a closed pipe). Before, it ended with status 120, or 1 and a traceback. A new plain sentence is printed on standard error for a closed or full output (section 2).
* **Every Mac row is `warn`** (operating system, graphics), so a Mac is never "ready" and the best exit status on a Mac is 1. The Rosetta row now carries the same "not verified" sentence, and two statements of fact became "should" (Rosetta "should still work", low memory "should run").
* **FFmpeg row** says what FFmpeg's listings contain and that no real file was tried. The version is found even when a notice is printed before the banner.
* **Folder advice.** `sudo chown -R` only for a root-owned folder inside the person's own home; commands are shown whole or replaced by a sentence (never cut at 400 characters); the hint mentions USB sticks and network drives.
* **Port.** A refusal on a port of 1024 or above is no longer blamed on "ports below 1024", and the example never names the refused port.
* **Sizes** are rounded down (1.99 GiB free reads "1.9 GB", not the 2.0 GB limit).
* **Text report.** A fix that holds a command stays on one line. The UI's Copy button copies only the command part (section 2 and section 5).
* **Extension.** `checks()` returns `[]`. On a Mac the tools `sysctl` and `system_profiler` are taken from their fixed system folders before the search path.
* **Tests.** No test binds a fixed port (guard in the fixtures); `stop()` is proven (the runner is closed exactly once and a running program is cut short); `status()` is tested on the instance the station runs; the limits that were only checked through their own constants (hours of music, 512 MiB, 100 checks, clock window, port range) are written out as numbers and dates.
