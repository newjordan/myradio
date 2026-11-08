> **Historical.** Release notes from the 0.3 development previews, kept for contributors. The current release is
> described in [docs/RELEASE-STATUS.md](../RELEASE-STATUS.md).

# myradio 0.3.2 — source development preview

**Current source: 0.3.2 preview, including the Midnight studio redesign and storage-fault handling.** Earlier wheel observations below are historical, not certification of this source. No replacement wheel was built in the latest round. See [HANDOFF.md](HANDOFF.md) for the current continuation plan.

## Handoff update — current source is known red

On 2026-11-05, `python3 -m unittest discover -s tests -p 'test_agent_deadline.py' -v` was rerun on the handoff source: **2 tests in 2.306s; 1 failure and 1 error; exit 1**. Drip-fed headers hold the operator lock beyond the intended total deadline, and malformed chunk framing escapes as `http.client.IncompleteRead`. `myradio/model_transport.py` is an unfinished replacement candidate; `Chat._model` still uses the old urllib path. No app code was changed to hide or fix these failures during the handoff. Evidence: [current regression log](../reports/handoff-agent-deadline-current.txt).

The 73-test green report below predates this new regression/candidate and is **not** verification of the latest tree. The 21 notified background verification services were inspected and are all stopped (SIGTERM); their startup logs are not acceptance results. `dist/` currently contains only historical 0.2.0, 0.3.0 and 0.3.1 wheels, not the previously reported 0.3.2 wheel. No current-source artifact, independent acceptance or native Mac/installer verification is claimed. [The handoff](HANDOFF.md) records the precise continuation point and source manifest.

## Previous source reliability round

Fixed queue-capacity admission: when space is scarce, the earliest eligible events across recurring programs and one-shot schedules now get the remaining slots. Accepted events retain chronological playback order; stale/off-air/full-queue events are still missed. Previously reverse insertion order incorrectly also decided admission, sacrificing earlier events for later ones. Queue occupancy is read once per due batch rather than once per eligible event.

Corrected an outdated import regression test to expect the storage fail-safe's `StationError`, enforce its restart requirement and verify retry after restart. The unchanged baseline ran 70 tests with one error and one skip. Final `python3 -m unittest discover -s tests -v`: **73 run, 72 passed, 1 skipped** in 22.974s on this host. The skipped test needs `MYRADIO_TEST_ICECAST` and FFmpeg; actual destination integration was not rerun. No browser, new wheel or native-OS certification is claimed for this round. Evidence: [reliability report](../reports/research-966e77beaf13afd2849ffa79c86fb72a-8.txt).

## Recurring-program update (current source checkout)

Added persistent weekly clock rules for imported music, speech and advertisements, with named IANA timezones and selected weekdays. The same `program.add` / `program.remove` operations are available through authenticated HTTP, MCP and approval-gated connected chat. The dashboard supports create/list/remove, shows next due time and last queued/missed outcome, and protects tracks referenced by rules from deletion.

DST gaps are skipped; folds air once at their first occurrence. Due rules merge with one-shot schedules in due-time order, ahead of the ordinary queue but without interrupting current audio. Off-air, stale (>60 seconds), or queue-full occurrences are missed; downtime never produces a catch-up burst. Removing a rule does not retract audio already queued. This remains a per-track recurring scheduler, not an hour-grid editor, compound show builder, hard-time interrupt, or advertising proof-of-play report.

Verification for this source update: `python3 -m unittest discover -s tests -p 'test_station.py' -v` and `tests/browser_station.py`. See `reports/research-966e77beaf13afd2849ffa79c86fb72a-3.txt` for measured outcomes and scope. The prior wheel hash and performance figures below describe the previous artifact, **not this changed source checkout**. A new wheel/clean-machine certification is still needed before distribution.

## Decision

**Not a complete station-in-a-box release yet.** This iteration establishes working local playout, persistent station control and the operator-chat surface. It must not be marketed as a one-click, self-generating public radio station. The shipped wheel requires Python on Linux/macOS; there is no signed installer or bundled interpreter. Windows is not supported by the current `fcntl` process lock.

## Delivered

- Dependency-free Python service, local-only authenticated API and packaged responsive dashboard.
- Persistent SQLite station identity, audio library, queue, one-shot schedules, recurring programs and recent playout history.
- Real stereo 44.1 kHz/16-bit PCM playout with mono import normalization. Explicit WAV import only; no decoder subprocesses or arbitrary media execution.
- Independent station clock; music rotation visits the complete library. Speech and ads play only when explicitly queued/scheduled, never as filler.
- Bounded listener queues and connection limits; slow listeners disconnect instead of holding the playout clock.
- Stop/restart recovery preserves the interrupted track at the front of the queue, restarting the track from its beginning. The service always restarts stopped.
- Right-hand operator chat: labeled limited offline commands, or optional OpenAI-compatible tool-calling model. Model changes require short-lived, single-use approval. No model is downloaded or silently selected.
- MCP stdio adapter with status and station tools over the exact same authenticated API. MCP actions are immediate; the connecting agent must enforce operator authorization.
- Checkout launcher, wheel, run instructions and saved screenshots from the functioning UI.

## Observed verification

### Python, HTTP and protocol boundaries

`python3 -m unittest discover -s tests -v`: **29 tests passed** on Python 3.12.3 after initial implementation (17 prompt-catalog tests plus 12 station tests). Subsequent authentication/token changes and idle-work optimization were checked with the focused station suite: **12 passed**. The latest focused result is a host-runtime observation, not independent harness certification.

Covered: WAV validation/mono normalization, sample-level track boundaries, queue priority, full-library rotation, excluding speech/ads from rotation, timezone validation, scheduling priority/missed events, restart recovery, deletion guards, process lock, slow-listener eviction, mocked model approval, session reuse, malformed credentials, origin restrictions, actual HTTP audio bytes, shared MCP state, and JSON-RPC initialize/notifications. Model tests use a mock endpoint, not a real provider.

`node --check myradio/web/app.js` passed before the final small resize-observer adjustment. The final JavaScript was then executed in the installed-wheel browser test below with no page errors.

### Browser and installed artifact

Built `dist/myradio-0.2.0-py3-none-any.whl`, installed it into a fresh `/tmp/myradio-release-venv`, and launched its console entry point from `/tmp` outside the source tree. Verified CLI entry points and prompt catalog validation (384 presets, 6,912 compiled variants, no audio evaluation).

Final wheel SHA-256:

```text
e69d7374b6fe0bae95dbe79d8349f994df13442deaed1fafc74941accc43973a
```

Ran `tests/browser_station.py` against that installed wheel in headless Chromium on Linux. It passed launch-token unlock, fragment removal, file upload, station start, actual browser decoding/progression of the live WAV stream, command chat, desktop/mobile layouts, no mobile horizontal overflow, chat scroll preservation on resize, stop and lock. No JavaScript page errors. This proves browser decoding, not a listening evaluation or physical sound-device output.

Screenshots:

- [Desktop](screenshots/local-station-desktop.png)
- [Mobile](screenshots/local-station-mobile.png)

Early browser attempts exposed verification-environment issues (Playwright expected a newer browser than installed; string predicates conflicted with the deliberate CSP) and an asynchronous test assertion. These were corrected without weakening CSP. Screenshot review also identified chat scroll loss on resize; the implementation now preserves bottom-pinning unless the user has scrolled up, and the browser test checks it.

### Small efficiency spot check

Measured Linux process CPU time from `/proc`, not estimated. Installed wheel, CPython 3.13.14, Ryzen 5950X, one 3-second WAV, zero listener connections, polling status every 0.5 seconds:

| State | Wall time | Polls | CPU seconds | One-core CPU | RSS |
|---|---:|---:|---:|---:|---:|
| Stopped | 10.025 s | 20 | 0.020 s | 0.20% | 49,852 KiB |
| Rotating music | 10.025 s | 20 | 0.010 s | 0.10% | 49,852 KiB |

These are short, coarse, scheduler-dependent spot samples. The lower running value is noise, not evidence that running is cheaper. They do not measure import peaks, large libraries, listener capacity, latency, underruns, day-long reliability, or inference. No GPU was used. Listener caps are defensive implementation limits, not proven capacity claims.

## Release blockers and next acceptance work

1. **Real generation adapters.** Integrate the separate music and DJ speech models specified in PRODUCT.md. Verify access, distribution/commercial licensing, storage, supported runtimes and the exact Turbo/Mac rigs. Need measured generation throughput, reproducibility, cancellation and quality checks, not merely successful model loading.
2. **Actual agent connection.** Exercise a real tool-calling provider and a real existing MCP agent client end-to-end, including ambiguous requests, refusal/cancellation, stale proposals and provider downtime. Current fake-provider tests establish protocol mechanics only. Implement station research agents separately with source/citation and publishing approval controls.
3. **Public output certification.** Icecast MP3 output is now implemented with private credentials, bounded backpressure, explicit go-live, retry/backoff and a real local decoded-listener check (increment below). Remote TLS hosts/mount policies, native FFmpeg packaging and sustained continuity still need certification. SHOUTcast/AAC and dynamic song metadata remain absent. The dashboard monitor is deliberately not a public broadcast server.
4. **Programming.** Weekly per-track rules with named zones/DST and one-shot events now work. Remaining: hourly grid/compound shows, duration/conflict previews, exact-time breaks, ad campaign rules/proof-of-play, loudness normalization/limiting, preview/cue, and safe fallback content. All scheduled audio is non-interrupting.
5. **Audio import/usability.** Disk quotas, bulk import, library pagination, search and queue reorder, model setup and progress UI, first-run onboarding, and a proper recovery/status screen. Common formats now use a bounded local FFmpeg conversion path (increment below); native PCM WAV remains dependency-free.
6. **Appliance reliability.** Long-duration playout/monitor rollover beyond WAV size limits; suspend/resume, missing/corrupt files, full disk, failed transactions, power loss, backup/restore, application migration, error recovery, stale-session revocation, and monitoring. SQLite I/O still shares the Python playout thread; this is not hard-real-time machinery.
7. **Distribution/accessibility.** Signed/notarized Mac installer plus agreed Turbo package, bundled runtimes, updates and rollback; clean-machine installs and uninstall/data retention; Safari/Firefox/mobile audio, keyboard and screen-reader checks. No target-platform certification yet.

The next useful milestone is a **real agent-controlled generated segment reaching a verified external output**, with an explicit preview/approval/go-live path and failure recovery—not additional cosmetic dashboard claims.

## Icecast increment (current source, 2026-11-05)

- New private configuration route and supervisor encode the station PCM clock as 128 kbit/s stereo MP3 for an Icecast destination. No source credentials in process args/environment, agent status, or encoder diagnostics. HTTPS required remotely; local HTTP is for testing only.
- Dashboard controls explicitly separate local start from Go live and show source health, retries and bytes. Confirmation displays the saved destination; saved passwords are write-only to the browser. Reload never goes live. Stop station disconnects; independent broadcast disconnect keeps local rotation running. Agent chat proposals still require approval; MCP operates immediately under its caller’s policy.
- Command: `MYRADIO_TEST_ICECAST=/tmp/myradio-icecast/root/usr/bin/icecast2 python3 -m unittest discover -s tests -p 'test_station.py' -v` — **21 passed**, no skips (17.530 seconds). Real Icecast 2.4.4 was downloaded/extracted, not installed as a privileged service. This adds six tests over the previous station suite. After a cancellation-state refinement, the final five BroadcastTests were rerun and passed in 14.010 seconds; the sixth new test is an HTTP/MCP integration case from the 21-test run.
- Real listener fetched MP3 from the Icecast mount; FFmpeg decoded 44,100 mono samples. For a 440 Hz test tone: RMS 5278.1. A later run exposed variable MP3 encoder priming (429 versus 430 whole-second crossings); the final test checks the last half-second after priming and measured exactly 220 positive crossings, as expected for 440 Hz. This is a signal-presence/continuity spot check, not perceptual audio certification. Incorrect source auth stopped with no retry storm. Killing/restarting the destination reconnected while local playout continued.
- Cancellation of drip-fed HTTP response headers, cancellable DNS waiting with a single outstanding resolver, bounded PCM handoff, private file mode, approval gating and a configuration-disk-write/clock lock inversion were specifically checked.
- Chromium 150 desktop/mobile workflow passed the previous local checks plus broadcast setup/save, cleared password, destination on reload, cancelled and confirmed Go live, live status, disabled editing while live, disconnect while local audio continues, reconnect, stop disconnect and lock. No page errors or horizontal mobile overflow. Screenshots: `docs/screenshots/broadcast-station-{desktop,mobile}.png`.
- Environment caveat: this machine’s `ffmpeg` is a Docker wrapper around FFmpeg 6.1.1/libmp3lame; the listener test fetches on the host and passes MP3 to FFmpeg stdin to avoid the wrapper’s isolated loopback namespace. Do not infer native target efficiency or packaging results from that wrapper. Encoder destination credentials are handled by Python, not the container.
- Remaining: remote TLS hosting certification, sustained-soak/network-blackhole tests, efficiency measurements with native FFmpeg, source metadata, packaged encoder/runtime/hosting setup, and all non-broadcast release blockers above. Current tests do not certify remote delivery, number of listeners, latency, or crash-free day-long operation.

## Common-format import increment (current source, 2026-11-05)

- Dashboard/API accept MP3, FLAC, Ogg, AAC/M4A, AIFF and non-native WAV using FFmpeg, normalizing to the existing PCM library format; native PCM WAV remains usable without FFmpeg. Input and decoded output are capped at 100 MiB, conversion deadline is 90 seconds, and only one HTTP import runs at once. No shell, URL, playlist or remote protocol support. Temporary input is private and seekable (M4A indexes); output is bounded in memory. Conversion runs outside the playout lock.
- `python3 -m unittest discover -s tests -p 'test_station.py' -k ImportTests -v`: 2 passed in the final 7.448-second run, using actual FFmpeg round trips for MP3, FLAC, Ogg, AAC, M4A, AIFF and 48 kHz PCM WAV. Also checked missing encoder, invalid metadata, invalid files, playlist rejection, oversized decoded output, forced timeout, no partial library entry and staging cleanup. Initial tests inspected lossy codec priming silence; corrected to inspect samples after 100 ms instead of pretending the first sample must contain signal.
- `python3 -m unittest discover -s tests -p 'test_station.py' -k upload_converts -v`: 1 passed in 1.044 seconds. Real HTTP conversion, response type, bad input status, upload-slot release and unchanged library after failure. The HTTP fixture now retains PATH/HOME while clearing model environment settings; its previous environment clearing hid FFmpeg and caused an explicit skip, corrected before this result.
- Real Chromium 150 workflow passed with `MYRADIO_TEST_IMPORT_FORMAT=mp3`: upload MP3, local rotation, browser stream decoding, operator chat, recurring scheduling persistence, desktop/mobile layout, stop and lock. No page errors or mobile horizontal overflow. Existing local screenshots were refreshed from that workflow.
- The AIFF fixture must be written seekably to finalize its container header (a pipe-generated fixture was correctly rejected as incomplete). These tests are not hostile-media sandboxing, a capacity/efficiency benchmark, or disk-failure recovery certification. Runtime remains separately installed; this is not yet a one-click installer.

## Offline recovery increment (current source, 2026-11-05)

- Added `python3 -m myradio.backup create/restore` and packaged `myradio-backup` entry point. Backup requires the service’s exclusive data-directory lock; restore requires a new directory and stages privately before atomic no-clobber publication. Linux renameat2 is exercised; macOS renamex_np is implemented but not platform-certified.
- Portable data only: allowlisted settings/library/queue/schedules/programs/history and normalized media. Credentials, session data, broadcast destination and model environment are not included. Restore creates schema from shipped code and binds validated rows; no raw backup SQL is executed. Media sizes, SHA-256, WAV format/duration and database integrity are checked. No encryption/signature/authenticity claim.
- `python3 -m unittest discover -s tests -p 'test_backup.py' -v`: **7 passed in 1.508 seconds**. Includes CLI roundtrip/invalid archive exit codes, private file/directory modes, full state and signal preservation, interrupted-track recovery without autoplay, regenerated token, credentials exclusion, running-service refusal, output refusal, missing-media rejection, checksum corruption, traversal/extra credential entries, symlink rejection, invalid row references/times/metadata/sizes, staging cleanup and a destination-creation race.
- Initial schedule fixture used year 2099 and was rejected by the actual station’s 366-day scheduling guard. Corrected to a relative seven-day date; no production scheduling limit was weakened.
- Import follow-up: failure injection exposed a library insert/history-insert transaction hazard; import now rolls back under its station lock before media cleanup, preventing later ticks from committing a ghost row. The three ImportTests passed in 7.656 seconds after that change.
- This is offline recovery, not a dashboard wizard or proof of power-loss/full-disk resilience. Portable limits are documented in README. Large-library memory/time scaling, target Mac behavior and crash interruption during archive creation/restore remain unmeasured.

## Private dashboard operator setup (current source, 2026-11-05)

- An authenticated dashboard form saves OpenAI-compatible URL/model/key to atomic private `agent.json` without restart. Remote URL requires HTTPS; embedded credentials/query/fragments and invalid ports are rejected. Password/key values are never returned to the browser or station/model snapshots. Environment-managed setups disable dashboard editing. Saved connection is not a verified-access claim. Existing messages/proposals are cleared on reconfiguration; per-action approval is unchanged. Disconnect forgets the file and returns to command mode. Model settings are excluded from portable archives.
- Focused HTTP settings test passed (0.644 seconds): authentication, invalid configuration, private file permissions, read-only summary without key, restart load, header-only key transfer, no secret/provider URL in model body, approval before mutation, chat/config concurrency exclusion, disconnect cleanup and environment override.
- A separate actual HTTP exchange against a deterministic local provider passed (1.149 seconds): saved settings -> real /v1/chat/completions request -> proposal -> explicit approval -> station mutation, then provider shutdown -> clear failure with no additional station mutation/proposals. This is a test provider, not an actual model certification.
- Chromium 150 passed dashboard setup/privacy consent, API-key clearing, reload persistence, failed endpoint with no changes, disconnect/forget and command-mode fallback, together with MP3 import, browser audio decoding, recurring program persistence and mobile/desktop layout. No page errors/overflow.
- Remaining: certified real provider/model and actual third-party MCP client, robust request-wide deadlines against a drip-feeding model endpoint, target-platform private storage, keychain integration, and provider-backed audio generation/research agents. Current model client has inactivity timeout and response byte cap, not absolute response deadline; it never holds the playout lock but a malicious model server could occupy chat until it times out.

## Preview packaging checkpoint: 0.3.1

- Full current source command: `MYRADIO_TEST_ICECAST=/tmp/myradio-icecast/root/usr/bin/icecast2 python3 -m unittest discover -s tests -v` — **51 passed in 31.176 seconds, no skips** (17 prompt catalog, 27 station/HTTP/import/broadcast, 7 backup). No claim of independent platform certification.
- Built `dist/myradio-0.3.1-py3-none-any.whl` with `uv build --wheel --out-dir dist`, installed into a fresh `/tmp/myradio-031-installed`, launched its installed command with working directory `/tmp` outside source. Full real Chromium workflow passed private operator setup/failure/forget, MP3 import, local stream decode/chat/program persistence, Icecast confirmation/live/disconnect/reconnect/stop, and desktop/mobile layout. No page errors or horizontal overflow. A deterministic provider test is not actual AI quality certification.
- Wheel SHA-256: `84780770e0c206ba07d2a3fcbe599dc32212eb592d0dfaf5f43841afe2a8d64b`. Prior 0.2.0/0.3.0 wheels remain historical artifacts; use 0.3.1 for this checkpoint.
- Earlier independent 0.3.0 installed-wheel backup/restore check preserved the browser-imported MP3 signal and omitted source credentials. 0.3.1 retains that recovery implementation and adds private dashboard model setup; current full suite checks exclusion of `agent.json`.
- The wheel contains Python code/dashboard assets, not Python itself, FFmpeg, an inference model, hosting or a signed OS installer. This is a reproducible development preview, not the complete one-click station-in-a-box release.


## 0.3.2 resilience preview — current iteration

- Missing, unreadable, wrong-format, symlink/FIFO and truncated native media are quarantined; playout continues with other ready tracks instead of stopping the station. A canonical 44-byte WAV header preflight avoids unbounded ancillary-chunk parsing; this is not a full-content corruption detector.
- Quarantine survives restart and avoids retry storms. At most sixteen candidate transitions are processed per 100 ms block. Mid-track read failures quarantine the track and preserve the clock. All-unavailable stations explicitly report waiting and stream silence.
- Dashboard, local status chat, API, MCP and approval-gated AI tools share health and `library.retry`. Retry validates repaired media before clearing quarantine. Browser UI labels unavailable tracks and offers the explicit repair retry action.
- Cancelled one-shot schedules now round-trip through backup/restore; previously a valid cancelled state made backup validation fail. Restores validate media, and quarantine is intentionally local, not portable.
- The installed 0.3.2 wheel passed **60 tests, no skips, in 33.106 s**, including real Icecast listener decoding and nine new recovery tests. This run preceded the separately focused private-proxy change below; it is not claimed as a full-suite run of subsequent edits. Log: `reports/release-032-installed-tests.txt`.
- Fresh installed wheel passed the real Chromium MP3 import/playout/audio decode/chat/program/reload/mobile/lock workflow, plus actual file removal → visible waiting warning → repair → explicit retry → resumed browser audio decode. No page errors. Log: `reports/playout-recovery-browser.txt`; screenshot: `docs/screenshots/media-unavailable-mobile.png`.
- Explicit `--public-origin` allows one HTTPS origin behind a private reverse proxy while still binding 127.0.0.1 and requiring authentication. The targeted HTTP security test passed (0.625 s): denied by default; exact origin allowed; wrong Host/Origin, missing token and cross-site requests denied; loopback remains valid. Do not trust arbitrary Forwarded headers.
- Requested private Tailscale preview was enabled on a separate :8443 route, preserving the existing default route. Actual remote HTTPS Chromium session passed unlock, fragment erasure, start, WAV decoding and stop without JS errors. Screenshot: `docs/screenshots/tailscale-preview-desktop.png`. Preview process lifetime is session-bound; no public Funnel or production service installation.
- Short engine-only samples (installed wheel, one synthetic 1 s track, no listeners/model): playing 5.000 s used 0.00902 CPU s (0.180% of one core), peak RSS 19.19 MiB; all-unavailable waiting 5.000 s used 0.00735 CPU s (0.147%), peak RSS 19.50 MiB. These are tiny diagnostics, not soak, dashboard-load, streaming-capacity or whole-service RSS guarantees.

Remaining release gates are not waived: signed native one-click installers/bundled dependencies, actual Windows/macOS verification (current Unix file locking remains), production service supervision/upgrade/rollback, long unattended/load/low-disk testing, optional voice generation/mixing, and independent acceptance. No declaration of a complete production release.

## Storage fault containment and Midnight FM concept (iteration 6)

- Fixed a demonstrated storage-failure hazard: `_run` previously attempted history/settings writes and commit while already handling a database error. It could terminate its clock, strand the reader and leave the broadcaster running. Actions/ticks also lacked rollback boundaries, allowing failed work to be committed by a later operation.
- Mutations now roll back within the shared lock on failure. SQLite/OSError faults latch a stopped state, independently attempt broadcaster shutdown, reader closure and rollback, and require a service restart rather than allowing a false successful transport start. Fault-mode ticks continue bounded silence to existing local monitors without writing storage. Broadcast disconnect remains available.
- Status read failures expose a fault and an explicitly stale cached snapshot instead of silently claiming a healthy station. Cleanup failures are independently guarded. Storage errors use a fixed operator-facing message, not filesystem/SQL detail.
- `python3 -m unittest discover -s tests -p 'test_storage_faults.py' -v`: 9 passed in 1.426 s. Covers actual SQLite read-only and page-limit SQLITE_FULL, schedule/action/import rollback, injected commit/read/rollback/cleanup faults, worker survival, silence delivery, restart rejection and emergency broadcast stop. The first run had two incorrect test assertions against a nonexistent reader attribute; corrected to check the owned file's closed state. Broadcast cleanup is mocked in these tests; this is not another real Icecast end-to-end check. No new full-suite/installed-wheel/native-platform claim.
- User requested a more exciting visual direction before rebuilding. Created editable `docs/concepts/midnight-fm.html`, Chromium-rendered `docs/concepts/midnight-fm.png`, and interaction/migration notes in `docs/concepts/MIDNIGHT-FM.md`. Opened the image on Stage. Image content is illustrative, not a functioning redesigned dashboard or a generated-service output. The renderer required explicitly selecting installed Chromium because this environment's Playwright version expected a different browser revision.
- Confirmed the existing preview returns HTTPS 200 at `https://turbo.tail2d8af4.ts.net:8443/` and supplied the full copyable address. It remains an older installed 0.3.2 instance, not the new concept or the source storage fix. Access is tailnet-only and may require unlocking.

Next: operator visual feedback, then the working dashboard rebuild with truthful broadcast state, retained agent approvals, functional navigation, mobile/accessibility checks and browser workflow regression. Earlier release blockers remain open; this is not a complete production-release declaration.

## Midnight studio implementation — source increment, 2026-11-05

- Replaced the old crowded single-page layout with a working ink-blue/coral/lime
  studio: decorative original record art, real track progress, transport,
  Coming up, monitor and quick navigation. No fake waveform, audience count or
  album-art retrieval claim. External output state is explicitly separate from
  local playout. Streaming means source connection, not listener reception.
- Desktop operator remains alongside the desk. Mobile has a dedicated Operator
  view. Library/import, schedule, broadcast configuration, identity/history and
  a practical guide are genuine keyboard-operable views. Mounted forms retain
  drafts when navigating. Visible global errors remain outside view containers.
- Full source Chromium workflow passed: MP3 import, monitor WAV decoding,
  command chat, provider setup/privacy/failure/forget, schedule creation and
  restart-free reload persistence/removal, local Icecast save/confirmation/
  connect/disconnect/reconnect, stop and lock. File-removal quarantine, visible
  waiting warning, repair/retry and resumed browser decode also passed. No page
  errors. Log: `reports/midnight-browser-7.txt`. This browser run checks source
  Icecast state, not independent listener-side MP3 decode.
- All desktop views exercised by keyboard with active states; chat and identity
  drafts survived navigation. All seven mobile views fit at 320px. A separate
  loopback fake-provider browser contract passed actual approval UI: identity
  unchanged until approval, mobile-navigation persistence, one-shot application,
  stale proposal disabled on a new question, disconnect clearing proposals, and
  model HTML inert as text. No real model compatibility claim. Log:
  `reports/midnight-approvals-7.txt`. It additionally checks 320/390/768px layouts
  and that content can scroll clear of fixed bottom navigation.
- Early diagnostics retained in `reports/midnight-*-attempt.txt`: fixed a real
  body/nav data-attribute collision; corrected test waits for hash routing and
  exact provider-status wording. Final workflow and approval runs are green.
- Actual screenshots: `docs/screenshots/midnight-studio-desktop.png`,
  `midnight-studio-mobile.png`, `midnight-operator-mobile.png`,
  `midnight-unavailable-mobile.png`, `midnight-approval-mobile.png`. Opened
  source desktop/mobile images on Stage rather than relying on Tailscale.
- Recorded operator-owned myradio.garden product/hosting intent and future
  Knightshift deployment, with public website/listener/private-control boundaries
  in `docs/DEPLOYMENT.md`. No DNS, public hosting or remote reachability claim.

These are focused source/browser checks, not a new full-suite, wheel, OS installer,
load/soak or production-readiness certification. The old 0.3.2 wheel does not
contain this redesign. Native Mac/Windows install verification, bundled runtime/
FFmpeg, service supervision/upgrade/rollback, long unattended/resource/low-disk
checks, optional music/voice generation and independent acceptance remain open.

## Console studio and queue arrangement — source increment, 2026-11-06

- The Studio view is now the Console the operator picked: a meter bridge (on-air lamp, now-playing display, level meter and
  a scrolling paper trace with the current and next three titles), the cart wall (the queue) with a library crate and a bin,
  and a control desk fixed to the bottom of every view on desktop (Start, Stop, Skip, the GO LIVE guard, the browser monitor
  with volume, and DROP NEXT pads and Autopilot marked PLANNED and disabled). The desk stays at the bottom of the screen at every
  size.
- One page, as the operator asked after seeing it: no navigation rail and no operator column. A masthead carries the station
  name and tagline with Add audio, Operator and Menu; the Library, Schedule, Broadcast, Settings, Guide and the operator chat open
  as sheets (native modal dialogs, full screen on phones) over the studio, and × or Escape returns to it. Routes such as
  `#library` and `#import` still work and open their sheet. The private design preview is built by
  `tools/preview/build_preview.py` from the real files plus the preview layer in that folder (demo backend, planned
  designs, and the clickable first-run walk-through).
- Honesty rules kept: the lamp says LIVE only while the broadcast state is `streaming` (stream connected, not listeners); the
  display says "PLAYING · LOCAL PLAYOUT" otherwise. The level meter is measured from the audio this browser's monitor is
  playing and reads MONITOR OFF when the monitor is off; it never animates on its own. The paper trace is decorative motion
  (hidden from screen readers, pausable, off under reduced motion); it is not a waveform of the audio. No listener count.
- GO LIVE is a guard dialog that shows the saved destination and stays locked, with the reason, until a host is saved,
  FFmpeg is present and playout is running. The Broadcast view's Go live button opens the same guard.
- Engine: `queue.move` (queue_id, position) and an optional `position` on `queue.add`. The queue now plays in a `pos` order
  column; ids never change. Front inserts (Stop, due schedules, a recovered track) and appends keep play order equal to id
  order until someone moves an entry, and a trigger gives any row written without a position the place its id meant, so
  older tools and fixtures stay correct. Existing stations are upgraded on open. Backups write the queue in play order.
  Agents and MCP see `queue.move` with typed arguments; `position` is optional for `queue.add` in every catalog.
- Verification run on 2026-11-06: `python3 -m unittest tests.test_queue_order` (11 tests, including an upgrade from the old
  table and a backup round trip); the soak, station, backup and recovery suites (167 tests, the 10 recorded soak findings
  still expected failures); a Node Playwright acceptance run against two real servers on 127.0.0.1 (47 of 47 checks: first
  run at desktop and phone width, drag to reorder, crate into a slot, bin and Undo back to the same slot, keyboard move and
  cancel, Start, real meter level from a -11 dBFS test tone at 77%, the guard locked without a host, Stop from another view,
  phone actions, no sideways scrolling, no text under 11px, no script errors). `tests/browser_station.py` was updated for the
  guard dialog and new readouts but not run: Python Playwright is not installed on this machine.
  Whole suite, `python3 -m unittest discover -s tests -p "test_*.py"`: 2,828 tests in 342 s, OK (1 skipped, the 10 soak
  findings as expected failures).
- Not done: the first-run setup flow (download the models, link them, choose the station style and DJ sets, first songs) is
  still a planned design in the private preview; ElevenLabs, recommended for DJ voices, has no adapter yet; the DROP NEXT pads
  and Autopilot are not wired; the lanes built on 2026-11-05 are not connected to the studio.

## Connect your agent — source increment, 2026-11-06

- A **Connect your agent** sheet (Menu, the desk until a stream is saved, and the operator chat) with one **Copy** button.
  The copied text gives an MCP-capable agent a ready configuration for this computer (`/api/connect` supplies the real
  Python, myradio folder, data folder and port), a DJ brief and optional stream-setup steps. It never contains the control
  code. A status card shows when an agent last used the station and what it did (the MCP adapter and the connect command
  label their requests with `X-Myradio-Client`; only those are recorded, and odd operation names are shown as "request").
  The desk adds "Agent DJ seen N min ago".
- `python3 -m myradio.connect stream|status`: saves an Icecast destination with the password from an environment variable
  or file, never the command line, never printed; refuses a password embedded in the URL; the station's own rules still apply
  (remote destinations need HTTPS). The sheet also takes a pasted `icecast://` or `https://` source address and reads it
  back without the password. Saving never goes live.
- Honest limits: the agent can program the station (queue, order, skip, schedule) but cannot speak new lines until the DJ
  voice is connected; external agents act without the built-in operator's approval step, as before; only Icecast-compatible
  MP3 output exists (no YouTube or other RTMP output yet).
- Verified: `tests/test_connect.py` (5 tests: the connect data needs the launch code and never contains it, only agent
  requests are recorded, the MCP adapter moves a cart and the studio sees it, the command saves a stream without printing the
  password and refuses a missing or embedded one); the browser acceptance run (70 checks) copies the text from the studio,
  runs the exact MCP command it contains as a separate process, moves a cart through it, and sees the studio's agent light
  turn green; then pastes a stream address and saves it, still private.
