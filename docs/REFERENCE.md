# myradio reference

The technical details that used to live in the README: running the studio by hand, scheduling, the agent and HTTP interfaces, limits, importing, backups, Icecast broadcasting, verification and the prompt library. For the quick start, see the [README](../README.md).

## Start your studio

Requirements: **Linux, macOS or Windows 10/11, Python 3.10+**, a browser, and audio you have permission to use. No runtime Python dependencies (on Windows, optionally `tzdata` for recurring programs: see [Windows](#windows)), GPU, account, model download, or network installation is required to run this checkout.

```sh
python3 -m myradio.server
```

Or open **Start myradio.command** (where your desktop permits executable launchers), or **myradio.cmd** on Windows. This is a source launcher, **not** a signed one-click installer. It opens `http://127.0.0.1:8765/` and unlocks your dashboard using a private launch code in the URL fragment, which the app immediately removes. If your browser does not open, visit that address and enter the code printed in the terminal. Keep that terminal running; Ctrl+C stops the service.

1. **Import your audio**: WAV, MP3, FLAC, Ogg, AAC/M4A or AIFF. FFmpeg converts common formats locally; native 44.1 kHz, 16-bit PCM WAV works without it. Select music, speech, or advertisement. Minimum 100 ms; maximum 100 MiB input and 100 MiB normalized stereo audio.
2. **Start station** starts the station clock and music rotation. Queue items override rotation. Speech and advertisements play only when explicitly queued or scheduled.
   The studio is one page, laid out like a mixing desk: the on-air lamp, now-playing display and level meter across the top, the **cart wall** (your queue) in the middle, and the **control desk** (Start, Stop, Skip, the GO LIVE guard and the browser monitor) fixed at the bottom. Drag a cart by its grip to reorder the queue, drag one in from the library crate to drop it into a slot, or drop a cart on the bin to remove it (with Undo). Keyboard: focus a cart, Space to pick it up, arrow keys to move it, Space to put it down, Escape to cancel, Delete to remove.
   There are no side panels. **＋ Add audio**, **✳ Operator** and **☰ Menu** at the top open the Library, the operator chat, Schedule, Broadcast, Settings and the Guide as sheets over the studio; × or Escape returns to it.
3. **Listen** connects this browser to the local output. Starting playout and listening are separate actions. Closing a browser does not stop the station.
4. Use the **Operator chat** to check status, start, stop, skip, or rename the station. Connect an actual AI operator as described below for natural-language programming.

The dashboard automatically converts common audio formats when FFmpeg is installed (not bundled). Without it, import native 44.1 kHz, 16-bit mono/stereo PCM WAV. You can also convert manually:

```sh
ffmpeg -i input.mp3 -ar 44100 -ac 2 -c:a pcm_s16le output.wav
```

No wheel matching the current source is available. At handoff, `dist/` contains historical 0.2.0, 0.3.0 and 0.3.1 wheels; the previously reported 0.3.2 wheel is absent and would not contain the current redesign/reliability changes. To install the development source, `python3 -m pip install .` makes `myradio`, `myradio-mcp`, and `myradio-prompts` available. Packaging currently requires setuptools; installation may fetch it. Python 3.10+ and FFmpeg remain separate. The source launcher does not install anything.

Options:

```sh
python3 -m myradio.server --no-browser --port 8765 --data-dir "$HOME/.myradio"
```

The data directory contains `station.sqlite3`, normalized WAVs in `media/`, `control.token`, optional `agent.json` with provider credentials and `broadcast.json` with source credentials, and a single-writer lock. Back up the directory **with the service stopped**. Keep the token and provider/broadcast settings private. On Windows the default data directory is `%USERPROFILE%\.myradio`.

### Windows

Windows support is new. The same source runs on Windows 10 and 11 with only platform switches: the single-writer lock uses Windows file locks instead of POSIX `flock`, FFmpeg pipes are read by helper threads (Windows cannot `select()` on pipes), and model workers and doctor tools are stopped with `taskkill /T` instead of process groups. So far it has been run on one Windows 11 computer with Python 3.12: WAV import, playout, the live browser monitor, the studio UI, the single-copy lock, backup and restore, the launcher, the doctor, and the FFmpeg import and Icecast output pipes with a stand-in encoder and a loopback test server. It has not yet been tried with a real FFmpeg build or a real Icecast host on Windows; please report anything that goes wrong.

1. Install **Python 3.10 or later** from [python.org](https://www.python.org/downloads/windows/) and tick **Add python.exe to PATH**. (The `python` that Windows offers to fetch from the Microsoft Store is a placeholder until you install it.)
2. Optional but recommended: install **FFmpeg** and put its `bin` folder on your `PATH`, for MP3/FLAC/Ogg/AAC imports and Icecast output. For example, in PowerShell: `winget install Gyan.FFmpeg`, then open a new terminal.
3. Optional: recurring programs and ad flights use IANA timezones, which Windows does not ship. Install them once with `py -m pip install tzdata` (installing this checkout with `pip install .` pulls it in automatically on Windows).
4. Double-click **myradio.cmd** in the checkout folder, or from PowerShell or Command Prompt in that folder:

```powershell
py -m myradio.server
py -m myradio.server --no-browser --port 8765 --data-dir "$env:USERPROFILE\.myradio"
```

Use `py` (the Python launcher) or `python` wherever this reference says `python3`. The studio opens in your default browser exactly as on other platforms; Ctrl+C in the terminal stops the service. The **Connect your agent** sheet writes Windows paths and a PowerShell form of the stream command when the station runs on Windows. In an MCP configuration use the full path of `python.exe`, for example `"command": "C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"`.

Differences on Windows: the `0600`/`0700` private file modes are not enforced (Windows uses access lists; a data folder inside your user profile is private to your account by default), so keep the data directory inside your profile. Symbolic links in the media folder are refused by a check before opening rather than by `O_NOFOLLOW`. A model worker that starts other programs and then exits by itself can leave those programs running, because Windows has no process groups to sweep afterwards; workers stopped for timeout or cancellation are stopped together with their children. The developer test suite runs on Windows (`python -m unittest discover -s tests`, with `tzdata` installed) but is not yet clean there: the remaining failures come from POSIX assumptions in the tests themselves (`0600` modes, symbolic links, which need administrator rights on Windows, `/bin/sh`, `/dev/full`, the `resource` module, `\n` versus `\r\n` in text output, and temporary folders removed while a test still has a database open).

## What works today

- Persistent station identity, audio library, queue, one-shot schedules, recurring programs, and recent playout history.
- Continuous local PCM output at 44.1 kHz / 16-bit stereo; automatic music rotation visits the whole library. Queued tracks take priority; boundaries are filled without inserting a deliberate silence block.
- A station-owned clock independent of connected browsers. Output is silence when stopped or without eligible audio. Slow listeners are disconnected instead of blocking the clock. Monitor limit: 16 clients.
- Restart recovery preserves the interrupted track at the head of the queue. Startup **never automatically starts playout**. Stop requeues the current track from its beginning; Skip discards its remainder.
- One-shot schedules require an explicit timestamp/timezone. They enqueue at the front when due; they **do not interrupt the current track**. Events due while stopped or more than 60 seconds late are marked missed. Recurring programs are described below; there is no hourly grid editor yet.
- A responsive browser studio with real readiness labels, explicit monitoring, upload, queue, scheduling, history and chat.
- The UI, connected chat model and existing external agent all use the same validated station operations.

## Schedule a recurring show, ID, or advertisement

In **Recurring programs**, select an imported track, its local start time, an IANA timezone (for example `America/New_York` or `Europe/London`), and the days it should repeat. The form starts with your browser's timezone. Your computer needs system IANA timezone data; unknown zones are rejected with an actionable message.

- Programs persist across service restarts. Creating one does **not** start playout. The station must be running at the due time.
- Due audio goes to the front of the queue **after the current track**, not at an exact second or over music. Simultaneous events queue in stable order. This is not a multi-track show builder or exact-time commercial break system.
- More than 60 seconds late, stopped, or full queue (1,000 entries): the occurrence is marked **missed**. After downtime, the station advances to the next future occurrence without replaying a backlog.
- If only some queue slots remain, the earliest eligible occurrences get them across both one-shot schedules and recurring programs. Accepted events play in due-time order (event ID breaks ties); existing queued audio is retained behind them.
- Daylight saving: nonexistent local times are skipped. A repeated local time airs once, at its first occurrence. The local wall time stays the same as UTC offsets change.
- The dashboard shows the next occurrence in **your browser's timezone**, plus the rule's local time/zone and its last due result. `queued` means queued, **not proof of airing**; the station log records actual playback starts.
- Remove a program to stop future repetitions. This does not remove an occurrence already in the queue; remove that queue item separately. Tracks in recurring programs cannot be deleted until their rules are removed. Limit: 1,000 recurring programs.

The shared API operation is `program.add`, with string arguments `track_id`, `timezone`, `local_time` (`HH:MM`), and `weekdays` (comma-separated ISO numbers, Monday=`1` through Sunday=`7`). `program.remove` takes `program_id`. MCP exposes these as `program_add` and `program_remove`; connected-model proposals still require approval. Example authorized HTTP request body:

```json
{"operation":"program.add","arguments":{"track_id":"<library track id>","timezone":"Europe/London","local_time":"09:00","weekdays":"1,2,3,4,5"}}
```

## Have an agent man the station

### Chat with a connected model

Open **Connect an AI operator** in the chat panel. Enter your provider’s OpenAI-compatible base URL (usually `/v1`), exact tool-calling model ID and API key (blank only for a keyless local server). Review the privacy/billing notice, check consent and save. Send a status question to test access: saving alone makes no provider request and does **not** verify access. This is not a built-in/free AI service or certified compatibility with a specific provider.

Settings are saved in private mode 0600 `agent.json`; the key is never returned to the browser or included in station/model snapshots. Reload retains the connection and clears the key field. To change settings, supply the key again. **Disconnect and forget saved connection** removes that file and clears server-side chat/proposals. It does not revoke provider keys, undo approved changes, stop audio/broadcast, or erase other browsers’ visible chat. Portable archives exclude the settings. Environment variables override saved settings and disable dashboard editing until restarted without them.

The launch-environment method remains available:

Without a model, chat is clearly labeled **Local command assistant · no AI connected**. It understands `start`, `stop`, `skip`, `status`, and `name Your Station`. These explicit offline commands act immediately; they are not open-ended AI.

For a real tool-calling model, set these variables **before starting myradio**:

```sh
export MYRADIO_AGENT_URL="http://127.0.0.1:1234/v1"
export MYRADIO_AGENT_MODEL="your-loaded-model-id"
# For a remote provider, use an HTTPS endpoint and set its key privately:
# export MYRADIO_AGENT_KEY="..."
python3 -m myradio.server
```

The endpoint must support OpenAI-compatible `/chat/completions` and function tool calls. Non-local endpoints require HTTPS. Requests send your station metadata, recent chat and up to 100 library entries to the endpoint; consider that provider's privacy and billing policies. Keys are not sent in station snapshots. The current HTTP path sets a 45-second socket timeout, **not a proven total request deadline**: drip-fed headers can hold the operator busy, and malformed chunked replies are not yet safely normalized. See the handoff's failing regression. There are no automatic retries; redirects are disabled.

The model can inspect state and propose station operations, but **proposals do not run until you approve each action** in the chat panel. Proposals expire after two minutes and a new message invalidates outstanding proposals. This prevents a hallucinated tool call from silently changing playout. The model cannot generate audio, browse, or run programs. It can propose starting/stopping a previously configured broadcast, with the same per-action approval. Never paste source passwords into chat. It is request-driven, not an autonomous background agent.

### Connect your agent in one copy

In the studio, open **☰ Menu → Connect your agent** (the desk also offers it until a stream is saved). Press **Copy** and paste the text into Claude Code, Codex or any agent with MCP on this computer. The text holds a ready MCP configuration with this computer's real Python, myradio folder, data folder and port, a DJ brief (keep 3 to 6 carts queued, plan the flow with `queue_add`, `queue_move`, `queue_remove` and `transport_skip`, queue the station's spoken carts about every 15 minutes, never delete audio or go live unless asked), and optional steps for setting up the outbound stream. It contains nothing secret: the adapter reads the control code from the data folder. The panel shows when your agent last used the station and what it did, and whether a stream is saved. Spoken DJ lines need the DJ voice, which is not connected yet; until then the agent programs the station and queues existing spoken carts.

### Connect your existing MCP agent

Start the station, then configure your MCP-capable agent with a stdio server:

```json
{
  "mcpServers": {
    "myradio": {
      "command": "python3",
      "args": ["-m", "myradio.mcp", "--data-dir", "/absolute/path/to/.myradio"],
      "cwd": "/absolute/path/to/myradio"
    }
  }
}
```

Ready-made plugins for Hermes Agent, omp (oh-my-pi) and angelX, built on this adapter, are in `integrations/`; each folder's README has its install steps.

Client configuration syntax varies. If installed in a virtual environment, use its absolute Python executable. Match `--port` if you changed the station port. The adapter reads the private token file; it does not launch a second station or model. Its tools inspect status and update identity, transport, queue, one-shot schedules, recurring programs, library and broadcast start/stop. Credentials must be saved privately in the dashboard first. **External-agent tools act immediately.** Restrict access and configure that agent's approval policy before allowing unattended operation. MCP protocol negotiation and tool calls are integration-tested; compatibility with specific third-party agent apps remains to be certified.

### Shared HTTP interface

All control requests need `X-Myradio-Token: <launch code>` or a dashboard session cookie. The service binds only to loopback and rejects unrecognized Host/Origin headers. There is no remote-admin mode or CORS bypass.

- `GET /api/status`: station state, library, queue, schedules, recurring `programs`, history, capabilities.
- `POST /api/action`: `{"operation":"queue.add","arguments":{"track_id":"..."}}` appends; add `"position": 0` to play it next (any position, 0 = next). `{"operation":"queue.move","arguments":{"queue_id":"12","position":0}}` moves a queued entry; a position past the end moves it to the end. Queue ids never change when entries move.
- `POST /api/upload?title=...&artist=...&kind=music`: raw WAV body.
- `POST /api/chat`: `{"message":"..."}`; `/api/approve`: `{"id":"..."}`.
- `POST /api/session`: `{}` with token to create a browser session; `/api/logout`: `{}` to revoke it.
- `GET /stream.wav`: authenticated live WAV monitor, not a downloadable finite file.

Operation schemas live in `myradio/station.py` and are exposed as MCP tool schemas. Never put control tokens in a public URL, chat transcript, screenshot or repository.

## Boundaries before a production release

**This is not yet one-click radio for everyone.** Bundled public hosting, SHOUTcast/AAC output, AI music/speech generation, research/news ingestion, hour-grid editing, crossfades/loudness processing, ad reporting, signed installers, updates, licensing review, backup/restore UI and multi-day reliability certification are not implemented.

The monitor is uncompressed PCM (about **1.41 Mbit/s per listener before overhead**); it is intentionally local, not an efficient public distribution format. The WAV header uses a streaming length sentinel. Chromium decoding is tested; Safari and Firefox still need testing, and very long continuous sessions need rollover handling. This engine is ordinary Python/SQLite, not a hard-real-time broadcast appliance. A single slow filesystem can still stall its clock. Upload conversion uses bounded memory; status polls return the whole library and are not yet designed for enormous collections.

Do not expose the Python admin server to the internet. Localhost authentication is not isolation from other processes running under your OS account. Generated and imported audio still require appropriate rights and broadcast permissions.

## Importing your audio

Select WAV, MP3, FLAC, Ogg, AAC/M4A or AIFF in **Import audio**. FFmpeg converts other sample rates/formats locally to 44.1 kHz, 16-bit stereo PCM. The dashboard reports whether FFmpeg is available. Native mono/stereo PCM WAV at that rate and width needs no encoder installation. All content types—music, spoken links/IDs and ads—use the same import path; only music enters automatic rotation.

Limits apply to both the uploaded file and decoded PCM: **100 MiB each**, or about **9 minutes 54 seconds** of stored stereo audio per track. Short compressed files can expand past that limit; they are rejected without partial import. One HTTP import at a time, 90-second conversion deadline, bounded decoder output, private temporary files, and no network media or playlists. Uploads stay on your machine. The decoder is not a malware sandbox: keep FFmpeg patched and import trusted media. Metadata is manually entered; embedded tags/artwork are not imported. There is no bulk import, conversion cancellation/progress percentage or larger-file workflow yet. Disk-full/transaction recovery still needs broader reliability work.

## Portable backup and restore

Stop the **service** (Ctrl+C in its terminal, not just Stop in the dashboard) before backing up. From the source folder:

```sh
python3 -m myradio.backup create --data-dir "$HOME/.myradio" "$HOME/station-backup.myradio.zip"
python3 -m myradio.backup restore "$HOME/station-backup.myradio.zip" --data-dir "$HOME/.myradio-restored"
python3 -m myradio.server --data-dir "$HOME/.myradio-restored"
```

Installed packages also provide `myradio-backup`. Restore requires a **new, nonexistent directory**; it never replaces your current station. Confirm the restored library/settings and listen locally before switching over. The original data is untouched; return to its data directory to roll back. Archives already present at the output path are never overwritten.

Portable backups include identity, normalized audio, queue, schedules, programs and history. They **exclude control tokens, source passwords, broadcast URLs, browser sessions, model environment settings and chat proposals**. On launch, restored stations generate new unlock credentials and keep local/outbound playout stopped. Reconfigure broadcast and model credentials separately. A track interrupted at backup time is requeued on launch. Stale scheduled programs follow the normal no-catch-up policy.

Each media file has a size and SHA-256 checksum. Restore verifies the manifest, schema/types, foreign references, normalized WAVs and media checksums in a private staging directory, builds a fresh database from allowlisted data (never archive SQL), then atomically publishes without overwriting a destination created concurrently. Extra paths, traversal, duplicate ZIP entries, symlinks, corrupt audio and unexpected settings are rejected. Failed restores clean staging and do not publish a partial station. Archives are private mode 0600 but **not encrypted or signed**; checksums detect damage, not a malicious author who changes both content and checksum. Keep backups private and use trusted archives.

Current portable limits: 10,000 tracks, 100,000 rows per table, 16 MiB metadata and 20 GiB media total. Archives use stored ZIP for predictable CPU and size; you need free space for a complete archive/restore. Tested on Linux; macOS atomic publication support is implemented but not yet target-tested. There is no dashboard backup wizard, encryption, cloud backup or in-place restore. Power-loss durability and full-disk behavior still require certification.

## Optional real broadcast: Icecast

The dashboard’s **Broadcast to the world** card sends the same station clock to an Icecast host as **128 kbit/s, 44.1 kHz stereo MP3**. Local monitoring still works without FFmpeg or an Icecast subscription/server. This is an output connector, not bundled public hosting or a bundled Icecast server. [Working broadcast dashboard](screenshots/broadcast-station-desktop.png) · [mobile](screenshots/broadcast-station-mobile.png).

1. Install FFmpeg with the `libmp3lame` encoder in your PATH, then restart myradio. FFmpeg is not bundled. Use an Icecast 2-compatible destination you control.
2. Enter your mount URL (e.g. `https://radio.example.org:8443/live.mp3`), source username (usually `source`), and source password in the card—not chat. Remote destinations require HTTPS with a trusted certificate. Plain HTTP is allowed only to loopback for local testing. No embedded credentials, query tokens or redirects are supported.
3. Save, import audio you have broadcast rights to, and start the station locally. Review the displayed saved destination. **Go live…** requires a separate confirmation and may make your station public. Saved configuration and local start never connect automatically.
4. Verify audio through your host’s public listener player. The dashboard shows source state, bytes sent, connections, retries and a sanitized failure reason—not listener counts or delivery confirmation. The mount is sent with `Ice-Public: 0` (no directory-listing request); that does **not** make its listener URL private.
5. **Disconnect broadcast** leaves local playout running. **Stop station** disconnects both. Locking/closing the dashboard leaves the service and an existing broadcast running. A service restart never resumes outbound transmission automatically.

From a terminal or an agent, `python3 -m myradio.connect --data-dir <data folder> --port <port> stream --url https://host:8443/live.mp3` saves the destination with the password taken from `MYRADIO_SOURCE_PASSWORD` (or `--password-file`), never from the command line, and never prints it; `... status` shows what is saved. Set the variable from wherever the password already is, not by typing it into a command, so it never lands in an agent's chat. The studio's **Connect your agent** sheet also takes a pasted `icecast://source:PASSWORD@host:port/mount` address. Saving never goes live.

Credentials are atomically saved with mode 0600 in `broadcast.json`, not exposed in agent status, encoder arguments, encoder environment, or encoder diagnostics. Your authenticated browser can review the destination URL but never retrieves the saved password. Local OS-account access is not isolated; protect backups. Change credentials only while disconnected. They currently cannot be forgotten via the UI; stop the service and remove `broadcast.json` to forget them.

Network/encoder work runs off the playout thread with a bounded ~3-second PCM handoff. A slow destination or encoder discards stale output and reconnects with fresh audio, not an ever-growing backlog. Temporary failures back off (1, 2, 4, 8, then 16 seconds); authentication/refusal errors stop and require intervention. Connection/TLS/response setup has an absolute deadline; stop interrupts pending sockets and DNS waiting. A blocked OS DNS call may leave at most one daemon resolver until the OS returns; retries never spawn more for that instance. Stop does not wait for network shutdown on the station clock. This is not a promise of uninterrupted remote delivery or a multi-day reliability certification. MP3 output has no dynamic song metadata, loudness processing or crossfades yet. Public-host TLS interoperability, proxies and hosted mount policies still need certification.

Local Icecast integration verification (optional separate test dependency):

```sh
MYRADIO_TEST_ICECAST=/absolute/path/to/icecast2 python3 -m unittest discover -s tests -p 'test_station.py' -v
```

Without that environment variable the real-destination case is explicitly skipped, not counted as a broadcast verification. The remaining broadcast safety tests do not require Icecast; encoder tests require FFmpeg.

## Verification

```sh
python3 -m unittest discover -s tests -v
node --check myradio/web/app.js
```

The suite covers the existing prompt catalog plus persistent recovery, WAV validation, complete-library rotation, queue priority, sample-level boundaries, schedule ordering/missed events, recurring program persistence/DST/merged queue ordering, slow-listener eviction, authentication, origin restrictions, real HTTP audio and MCP operations. Model behavior is tested with a mock endpoint, **not** evidence of a live provider or generated audio.

An optional destructive browser smoke test (use a separate empty test data directory) is in `tests/browser_station.py`. It requires Playwright and Chromium, a running station on port 18765, and `MYRADIO_TEST_TOKEN`. `MYRADIO_TEST_URL` and `MYRADIO_TEST_BROWSER` override the URL and browser executable. It imports test tones, operates playout/chat, tests browser stream decode and mobile overflow, and writes screenshots. It must not be pointed at your live station.

## Music-prompt library

The existing separate prompt tool remains available:

```sh
python3 -m myradio.prompts validate
python3 -m myradio.prompts list
python3 -m myradio.prompts --help
```

See [docs/PROMPTS.md](PROMPTS.md) for catalog and generation usage. These are text prompts, not generated or quality-evaluated audio. [PRODUCT.md](PRODUCT.md) records the larger product vision and platform/model decisions still required.


### Unavailable audio and recovery

If an imported file goes missing, is truncated or no longer matches its native PCM format, playout quarantines it and continues with ready audio. Quarantine persists across restart. The dashboard and operator status show the problem; unavailable music is not automatically retried every clock tick. If no ready audio remains, the clock and live stream continue with silence and an explicit **Waiting for playable audio** warning—not a claim that music is playing.

Repair the original library file and choose **Retry repaired audio**, or remove unused references, delete the unavailable entry and import a replacement. Retry validates format and length before clearing quarantine. The same `library.retry` action is available to authenticated API clients, MCP tools (`library_retry`), and approval-gated AI proposals. Backups revalidate audio on restore; they do not carry machine-specific quarantine records.

### Private Tailscale preview

The dashboard still binds only to loopback and requires its unlock code. To opt in to one exact HTTPS reverse-proxy origin:

```sh
myradio --no-browser --port 18769 --public-origin https://YOUR-HOST.YOUR-TAILNET.ts.net:8443
tailscale serve --bg --https=8443 http://127.0.0.1:18769
```

Use your actual Tailscale hostname in `--public-origin`. This allows that exact Host/Origin combination, not arbitrary forwarded headers. Keep the unlock code private. Tailscale Serve is tailnet-only; **do not use Funnel** to expose station controls publicly. Stop just this route with `tailscale serve --https=8443 off`. The existing default HTTPS route is unaffected. The preview process must remain running; this is not an installed system service.
