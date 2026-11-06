# myradio — continuation handoff

**Snapshot: 2026-11-05, approximately 23:18 UTC. Workspace: `/home/frosty40/myradio`.**

## Read this first

- **User request at this boundary: make a handoff.** Application work is paused, not declared complete. This handoff pass only inspected the current workspace/jobs, reran one existing regression, and updated documentation/evidence. Do not mistake the earlier conversation's prompt-library-only summary for the current implementation.
- **Source version: 0.3.2 development preview.** There is now a functional local radio studio, playout engine, imports, schedules, Icecast output, chat and MCP—not yet a production one-click station builder.
- **Current source is known red.** The freshly rerun `tests/test_agent_deadline.py` reports **2 tests run, 1 failure and 1 error**. A transport replacement was started before the handoff but is not connected to chat.
- The prior **73 tests / 72 passed / 1 skipped** result is real historical log evidence from the preceding reliability round, **not a green result for the latest tree**.
- **No Git repository or remote exists in this directory.** The user wants a private `myradio` repo. No commit, branch, clean-tree status, or published source release can be claimed.
- **No current-source distribution artifact.** `dist/` currently contains 0.2.0, 0.3.0 and 0.3.1 wheels. A historically tested 0.3.2 wheel is referenced in older docs/reports but is absent at handoff, and that earlier artifact predated Midnight and later reliability changes anyway.
- All **21 notified background studio/Icecast jobs** were inspected and are **stopped (SIGTERM)**. None is an available demo now. Startup logs and process exit do not prove listener reception or acceptance.

## 1. Product direction — preserve these decisions

The goal is a polished, professional **one-click radio station for nontechnical users**: install, describe the station, and operate it without learning a production stack. Do not shrink that goal to a prompt CLI or an imported-file player; those are implementation increments.

- Standalone and private now; potential later contribution to Wasserman's Imagination Studio. Follow its local service/shared UI-and-agent API/MCP approach rather than inventing a separate agent-only control plane.
- **Turbo and Mac are equally important launch targets.** Stage the engineering as necessary, but do not silently relegate Mac to an optional later port. Native hardware/runtime compatibility still requires actual verification.
- **YuE2 makes music:** `Frosty40/YuE2-Turbo-Radio`.
- **Drama 3 voices DJs and anchors:** `Frosty40/Drama-3-B70-Turbo-GGUF`, including spoken links, station IDs and spoken ads. **Drama 3 is not a fallback music generator.** This was an explicit user correction.
- A large library of **musically complex word-salad song prompts** is essential. Complexity means harmony, rhythm, motifs, arrangement development and instrument roles, not arbitrary genre stacking or mandatory loudness.
- User-selectable music/content styles, host personalities, entertainment anchors, easy show/advertisement scheduling, and reliable generate-ahead playout.
- **Interest-first, positive, optional research/news.** Help each user build their own research agents around hobbies, creative work and subjects they care about. No mandatory general-news wire, political feed, or outrage cycle. Respect source/topic exclusions; preserve sourcing and timestamps; do not invent upbeat facts. If research yields nothing suitable, play music rather than pad a bulletin.
- Send audio to the user's livestream or a standard output destination. The current implemented external destination is Icecast MP3. Knightshift's RTMPS/HLS implementation is reference material, not proof those outputs exist in myradio.
- Remote/phone control should primarily use the user's existing agent (Dot, Grok bot, Hermes, Muse, etc.). A custom phone-chat service is not the v1 priority. MCP support does not certify compatibility with every named agent.
- Preserve the functional **Midnight** studio: ink-blue/coral/lime, record artwork, central now-playing deck, Coming up queue, persistent desktop Operator and dedicated mobile Operator view. Do not start another concept-only redesign.
- Existing project notes record operator ownership of `myradio.garden` for promotion/hosting and later Knightshift deployment. This is intent, **not configured DNS, public hosting or production deployment**.

### Reference sources already located

- Private station: `https://github.com/newjordan/knightshiftradio`.
- Private music runtime/release work: `https://github.com/newjordan/yue2-b70`; Drama runtime work: `https://github.com/newjordan/fish`.
- Studio: `https://github.com/drowzeys/Imagination-Studio-by-keys-Wasserman`.
- Model cards: `https://huggingface.co/Frosty40/YuE2-Turbo-Radio` and `https://huggingface.co/Frosty40/Drama-3-B70-Turbo-GGUF`.
- Earlier inspection found optimized runtime/weight releases, not new fine-tunes according to their cards. Verify current revisions, redistribution/commercial rights and model capability before bundling or enabling advertising-supported use. Do not infer music support on Mac from Drama speech support there.
- `.reference/` is ignored, partial research material, not the authoritative app source or a promise that every previously fetched reference remains present. Knightshift's live station was not modified here; no current on-air status was verified in this handoff.

## 2. What exists now

### Local studio and playout

- Persistent SQLite station identity, library, queue, one-shot schedules, recurring programs and recent playout history.
- Station-owned 44.1 kHz / 16-bit stereo PCM clock, independent of browser connections; music rotation and queue priority; explicit browser listening.
- Audio import for native PCM WAV and, with FFmpeg installed, common formats such as MP3/FLAC/Ogg/AAC/M4A/AIFF. Upload and normalized-audio limits are 100 MiB; minimum duration 100 ms. FFmpeg is not bundled.
- Startup remains stopped. Stop/restart preserves interrupted audio at the queue head from its beginning; Skip drops its remainder.
- Missing/corrupt media is quarantined, ready media continues, and empty eligible output is honestly labeled silence. Storage failure latches stopped, disconnects broadcast, and rejects mutations until service restart.
- Recurring per-track schedules use local wall times, IANA timezones and selected weekdays; DST gaps skip and folds occur once. This is **not** a multi-track show builder, hourly grid or exact-time interruption engine.
- Optional supervised 128 kbit/s MP3 Icecast source output with saved private credentials, explicit Go live/stop and reconnect/health handling. This is not bundled internet hosting or an externally certified deployment.
- Offline backup/restore CLI with integrity/schema/media validation, no-clobber restoration and credentials excluded.

### UI and agents

- Responsive browser studio: Studio, Library, Schedule, Broadcast, Settings and Guide, plus mobile Operator. Forms/chat drafts survive view changes, not service/browser restarts.
- A plainly labeled offline command assistant (`start`, `stop`, `skip`, `status`, `name …`). It is not an AI model.
- Optional OpenAI-compatible tool-calling provider configured privately in the dashboard or launch environment. Connected-model mutations require short-lived single-use approval.
- Stdio MCP adapter and authenticated HTTP operations use the same validated station action layer. MCP actions are immediate; the external agent must enforce its own authorization policy.
- The connected chat model cannot currently generate audio, browse, research news or run programs. It is request-driven, not an unattended station-planning agent.

### Prompt library

`myradio/prompts/` contains **32 authored style palettes × 12 arrangement designs = 384 stable composed presets**, not 384 independently hand-written songs. Simple/layered/intricate composition is independent of sparse/balanced/lush density. Seeded reproducibility, style/energy/tag filters, exclusions, repeat avoidance, instrumental and supplied-lyrics vocal music, Python API and JSON/JSONL CLI are implemented. The 6,912 preset/complexity/density/mode combinations have historical software-test coverage; **no audio-quality/listening claim follows from that**.

### Source map

| Area | Files |
| --- | --- |
| Shared actions, persistence, clock, queue, recovery | `myradio/station.py` |
| HTTP/authentication and source launcher | `myradio/server.py`, `Start myradio.command` |
| Chat proposals/approval and saved provider config | `myradio/chat.py`, `myradio/agent_settings.py` |
| **Unintegrated deadline candidate** | `myradio/model_transport.py` |
| MCP adapter | `myradio/mcp.py` |
| Import/media handling | `myradio/importing.py`, `myradio/media.py` |
| Recurrence / external output / archive recovery | `myradio/scheduling.py`, `myradio/broadcast.py`, `myradio/backup.py` |
| Actual Midnight UI | `myradio/web/` |
| Music prompt library | `myradio/prompts/`, `docs/PROMPTS.md` |
| Tests | `tests/test_*.py`; explicit browser scripts `tests/browser_station.py`, `tests/browser_approvals.py` |
| Scope/status/deployment | `PRODUCT.md`, `README.md`, `docs/RELEASE-STATUS.md`, `docs/DEPLOYMENT.md` |

## 3. Exact unfinished work: provider deadlines

### Freshly reproduced on the handoff source

```sh
python3 -m unittest discover -s tests -p 'test_agent_deadline.py' -v
```

**Result:** 2 tests in 2.306s, **FAILED (failures=1, errors=1)**; exit status 1. Captured in `reports/handoff-agent-deadline-current.txt`. Earlier intentional-red evidence is `reports/agent-deadline-9-before.txt`.

1. `test_slow_headers_and_body_release_operator_without_mutation`, subcase `headers`: a provider drip-feeding HTTP headers holds the chat lock beyond the requested total deadline. Assertion: `provider drip-feed held the chat lock beyond the deadline`.
2. `test_redirect_oversize_and_malformed_chunk_are_safe_failures`, subcase `badchunk`: malformed chunk framing escapes as `http.client.IncompleteRead` rather than the safe `StationError` with no station mutation.

### Why the new file is not a fix yet

- `Chat._model` in `myradio/chat.py` still uses `urllib.request.build_opener(NoRedirect).open(..., timeout=45)` and a bounded `response.read`. A socket inactivity timeout is not an absolute wall-clock deadline when bytes keep arriving.
- `Chat._model`'s exception list does not catch `http.client.HTTPException` / `IncompleteRead`.
- `ModelTransport` exists only in `myradio/model_transport.py`; **nothing imports or instantiates it in production**.
- `MODEL_REQUEST_TIMEOUT` currently appears only in the regression test, which patches it with `create=True` so the test can run red against the old implementation.
- The candidate attempts a monotonic request budget, bounded DNS resolver reuse, socket shutdown on deadline, size-limited reads, no redirects/proxies, and timer/socket cleanup. It is unfinished/unverified work, **not a vetted replacement**. Keep it for review, do not discard or claim it fixes the regressions.

### First continuation step

Review and finish the transport integration with one reusable transport per Chat instance (or a better justified bounded design), wire the timeout into the actual request, and normalize protocol/transport/deadline failures to the safe no-mutation error. Preserve serialized chat, stale-proposal clearing, credential privacy, working success/approval behavior and cleanup on every exception.

Pay particular attention to DNS that cannot be cancelled, TLS handshake deadlines, timer/socket races, lock release and thread cleanup. Do not create an unbounded background thread on each timed-out request. This handoff makes no guarantee that the candidate's implementation is correct.

Use the **existing red regression above** as the immediate verifier. Do not add cosmetic tests or weaken assertions to manufacture green. Once the targeted regression is green, do any separately required release checks deliberately; there is currently no operator-bound independent acceptance command.

## 4. Prior reliability work worth preserving

The preceding round fixed `Station._tick` admission when the queue has limited capacity. Previously reverse-prepending also decided admission and could select later events over earlier ones. The implementation now:

1. Merges recurring and one-shot due events and sorts by due time / event ID.
2. Computes free queue slots once and reserves the earliest eligible events.
3. Inserts that accepted subset in reverse to retain chronological playback order at the queue head.

Three deterministic tests cover one-slot mixed event admission, stale/two-slot ordering and full-queue behavior. The round also corrected an obsolete import test to expect `StationError`, verify failed mutation/retry behavior and no orphan audio, and allow importing after a stopped restart. Transaction rollback/storage-fault semantics remain important invariants.

## 5. Verification ledger — do not merge these into a false green claim

| Evidence | Observed result / limitation |
| --- | --- |
| **Handoff rerun, current source** | Deadline test: **2 run; 1 failure, 1 error**. `reports/handoff-agent-deadline-current.txt`. No app code changed in this pass. |
| Previous reliability round | `reports/reliability-8-tests.txt`: **73 run, 72 passed, 1 skipped**, 22.974s. Predates deadline-test/candidate additions. |
| Skipped integration in that round | `test_real_icecast_listener_decode_auth_failure_and_reconnect` needs `MYRADIO_TEST_ICECAST` pointing to an executable plus FFmpeg. Not rerun for this handoff. |
| Historical browser evidence | `reports/midnight-browser-7.txt`: unlock/fragment erasure, MP3 import, local WAV decode, chat, recurrence, quarantine/retry and responsive layout. `reports/midnight-approvals-7.txt`: fixture-model approvals, stale proposal invalidation, inert HTML rendering and 320/390/768px views. **Not rerun here.** |
| Historical screenshots | `docs/screenshots/midnight-*.png`; actual prior Chromium UI captures, not concept mocks or public-broadcast proof. |
| Installed-wheel / destination checks | Older reports describe older artifacts and loopback destinations. They do not certify the current tree, public hosting, a live remote listener or native Mac installation. |
| Model inference | Provider tests used a local fixture. No real paid model/provider certification, YuE2 rendering or Drama DJ inference acceptance in this handoff. |
| Release acceptance | **Not established.** No signed one-click build, native OS matrix, production soak or independent acceptance claim. |

Other prior-round receipts: `reports/reliability-8-baseline.txt`, `reports/reliability-8-scheduler-before.txt`, and the `reports/research-966e77beaf13afd2849ffa79c86fb72a-7.txt` / `-8.txt` reports.

With no Git commit to identify the tree, this pass recorded **31 source/test/package/launcher files** in `reports/handoff-source-manifest.sha256`. This excludes private station data, reports and documentation. Manifest SHA-256 at capture: `6497de91af139498030aa4d2f13fb4b3288b5097030fa37677ebd24e92b7e926`. It is an identity record, not a verifier pass.

## 6. Processes, artifacts and how to resume locally

Inspected stopped process IDs: **1027, 1028, 1029, 1030, 1036, 1037, 1038, 1039, 1040, 1041, 1042, 1043, 1044, 1045, 1046, 1047, 1048, 1050, 1051, 1052, 1053**. All reported SIGTERM. These were studio test servers, installed-preview instances and loopback Icecast fixtures, not current production services. Do not infer anything about unlisted host processes.

Captured server logs contain private launch codes. **Do not copy those values into docs, screenshots, public URLs or a new handoff.** Treat retained test directories as private; use a fresh directory/token for a fresh demo. This pass did not restart services, change the live station, or delete private data.

Requirements for the current source: Python >=3.10 on Linux/macOS, a browser; FFmpeg separately for common-format import / MP3 output. No runtime Python dependencies. `fcntl` prevents claiming Windows support. Python/Linux verification does not constitute native Mac certification.

From the checkout, start a fresh local-only instance using a private data-directory path:

```sh
python3 -m myradio.server --no-browser --port 8765 --data-dir "$HOME/.myradio-handoff"
```

This creates/uses that directory; choose another if it already contains a station you must preserve. Open `http://127.0.0.1:8765/` and unlock with the new code privately. Ctrl+C stops the service. **Start station**, **Listen**, and **Go live** are different actions. Imported authorized audio is needed; the service currently does not synthesize its own programming.

The data directory holds `station.sqlite3`, `media/`, `control.token`, optional `agent.json` and `broadcast.json`, plus the single-writer lock. Back up with the **service stopped**, not merely playout stopped. Use the documented offline backup/restore commands; do not copy credentials into portable archives.

`Start myradio.command` is a source launcher, **not a one-click installer**. The `dist/` wheels present at handoff are **0.2.0, 0.3.0 and 0.3.1 only**, all historical. No replacement wheel was built here. Source install can fetch build dependencies; Python/FFmpeg are not bundled. Generated `build/` is not authoritative source.

## 7. Release work still required, in practical order

1. **Finish the deadline fix and get its existing regression green.** Preserve the known-red evidence and distinguish candidate code from integrated code.
2. **Establish private source version control/release identity.** User requested a private `myradio` repository, but none exists locally yet. Confirm target remote/access before publishing; exclude `.reference`, private data, tokens, credentials, caches and generated artifacts as appropriate. Do not claim a current commit before one exists.
3. **Define release acceptance for both Turbo and Mac.** A nontechnical fresh-install journey should include dependency/model setup, clear download progress, first audible station, scheduling/ads, configured standard output, agent control, recovery, update/uninstall and data retention. Identify native hosts/hardware, not just Python platform branches.
4. **Integrate the actual music and speech workers.** Separate YuE2 and Drama jobs, generate ahead into a ready buffer, persist provenance/errors, isolate inference stalls from playout, expose real model/setup state in the UI. Render and listen to varied prompt-library samples; the library's structural test count is not music-quality evidence.
5. **Complete station-building workflows.** Host/anchor configuration, user-owned interest-first researchers, compound shows/hour-grid scheduling, advertising schedules/reporting, usable first-run defaults, and user-authorized autonomy. Current recurrence is per imported track and current chat is request-driven.
6. **Build an honest new distributable.** Bump to a distinct version, include the actual Midnight/reliability source, bundle/manage dependencies, provide native installer/signing/notarization as appropriate, and verify from outside the checkout. Do not relabel an old wheel as the new app. No native installer acceptance has happened.
7. **Prove unattended broadcasting and recovery.** Intended-host soak, concurrent imports/status polling, bounded queues/memory/disk, real external listener decode, reconnect, actual disk-full/read-only behavior, backup restore and upgrade/rollback. Earlier deterministic fault tests and loopback screenshots are not a multi-day production certification.
8. **Deployment/agent certification.** Keep public product/listener surfaces separate from the private studio. Validate actual external-agent connectors and native browsers (Safari/Firefox remain gaps), rights/licensing, secure secret handling and production supervision before Knightshift deployment. No DNS/TLS/account setup was done here.

## 8. Behavior/security contracts not to break casually

- Bind private control to loopback, validate Host/Origin, require the private token/session, and never expose the admin service as a public `myradio.garden` endpoint.
- Connected-model tool calls are proposals with two-minute, single-use approval; a new question invalidates old proposals. Local explicit commands and external MCP operations are immediate by design. Any autonomy change needs an explicit permission design, not silently removing safeguards.
- The service starts stopped. Do not silently change to automatic on-air restart in the name of convenience; define opt-in behavior and recovery policy with the user.
- Local playout, browser monitoring, source connection and successful external listener decoding are distinct facts. Never label one as proof of another.
- Schedules enqueue after the current track; they do not hard-interrupt. Off-air, >60 seconds late or capacity-excluded occurrences are missed, not replayed as a downtime burst.
- Faulted media is quarantined; storage faults latch stopped and reject mutations until restart. Preserve cleanup and transactional rollback.
- Keep model credentials/source passwords out of station snapshots, reports and screenshots. Retrieved content and model output are untrusted data, not authority to run programs or change the station.
- Do not describe prompt generation as audio generation, fixture inference as real provider validation, or a launchable source checkout as a finished one-click release.

## 9. Status after the 0.4 fan-out (updated 2026-11-06)

Everything in sections 1 to 8 above predates this; read it as history where the two disagree.

- **Built:** nine lanes, each with its contract in `docs/lanes/<lane>.md` and owners in `docs/lanes/OWNERSHIP.md`: `myradio/audio` (read, convert, loudness, quality gate), `myradio/generation` (YuE2 and Drama 3 generate-ahead engine), shows (clocks, week planner), ads (sponsor spots, disclosure, rights gate), `myradio/research` (interest-first researchers, safe fetcher), hosts (personas, scripts that cannot invent facts), prompts (71 palettes, 18 arrangements), doctor (environment check) and soak (stress harness, `tests/soak_*.py`). Library code with tests, run against fakes only.
- **Reviewed:** two independent reviewers per lane, then a recheck. Every blocker and major was fixed and rechecked. The last two majors were finished on 2026-11-06 after a host crash killed their fixers: the music maker's test song (accepted at any length, at most 3 automatic retries, then only **Run a full test**) and the finishing order (convert to 44.1 kHz first, so the peak ceiling holds on air) in `docs/lanes/generation.md`; and feed text that held the interpreter lock for up to 560 ms (now 21 ms or less) in `docs/lanes/research.md` section 10.
- **Security reviews:** `reports/security-review-http.md`, `-media.md`, `-agents.md`. No critical or high findings; 15 medium findings with tested patches, **not applied yet** (the one that matters most on air: status polling copies the library while holding the playout clock's lock).
- **Soak findings:** `reports/soak-findings.md`: 14 engine defects, **not fixed** (the lane could not edit the core). Ten are `expectedFailure` tests in `tests/test_soak_findings.py`; remove each marker when its fix lands. The four medium ones: F10 (an unresolvable time zone stops the station), F9 (a bad character in the station name breaks every status request), F5 (status shows only the 100 furthest schedules), F3 (a full disk during import is not a storage fault).
- **Provider deadlines (section 3):** hardened in that session; evidence in `reports/agent-deadline-11-hardening.txt` and the manifest beside it.
- **Built on 2026-11-06:** the user picked the Console (mixing desk). It is now the real Studio view, with `queue.move` and positioned `queue.add` in the engine; see `docs/RELEASE-STATUS.md` (Console studio section) for what changed and what was verified. The private design preview (https://claude.ai/artifact/J8iKwEUg23ru9wecqr6VjY) is rebuilt from the real assets.
- **Top priority from the user (2026-11-06):** the easy installer and first-time setup matter most: download the models easily, myradio links them up, the person picks a station style, then the DJ sets, then the first songs play. ElevenLabs is the recommended DJ voice for now (no adapter exists yet).
- **Not done:** the first-run setup flow itself; wiring the lanes into one end-to-end flow; applying the security and soak fixes; an ElevenLabs voice adapter. Nothing has run against real YuE2 or Drama 3; that needs a GPU window the operator grants, because the live station owns the only card.
- **Tests:** whole suite on 2026-11-06: 2,817 tests in 335 seconds, 0 failures, 1 skip, 10 expected failures (the soak lane's recorded engine defects). Command: `python3 -m unittest discover -s tests -p "test_*.py"`.
- **Repository:** private `https://github.com/newjordan/myradio` (created 2026-11-06, branch `main`). `reports/` and `.angelX/` are git-ignored and stay local because run logs can hold private launch codes; there are no GitHub Actions workflows.
- **Lost in the crash:** the old session scratchpad under /tmp (pre-fan-out snapshot, ownership checker, concept sources). The concept artifacts themselves are still published.
- **Next step:** design and build the first-run flow above (installer, model download with progress, automatic linking, station style, DJ sets, first songs), then wire the generation, hosts and shows lanes into it; apply the security mediums and soak defects to the engine along the way.
