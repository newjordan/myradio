# Changelog

## Unreleased

- Stopping the station while the queue is full no longer stores a 1,001st cart. The track that was on air returns to the front, and the last cart is dropped if that is the only way to stay within 1,000.
- A library delete whose file removal fails no longer leaves that file behind after the next start. An import whose commit was saved and then reported failure keeps the audio file for the saved row.
- Cancelled schedules are pruned when they are cancelled, not only when some other event comes due. Status lists pending events soonest first, so the next one can be seen and cancelled.
- A queue id SQLite cannot store, and a title, artist, station name or tagline with a lone surrogate, are refused in plain language. A client that resets its connection does not print a traceback.
- One recurring program whose time zone cannot be resolved no longer stops the station clock.
- Opening a directory as a media file no longer leaks a file descriptor.
- The installed wheel includes the host persona and voice files.
- The studio shows maker status, a working Autopilot DJ switch, and the monitor volume on a narrow screen.
- GitHub Actions runs the unit suite. `pip install -e ".[dev]"` installs what the packaging test needs.
- Two generation tests wait until a retry is actually back in the queue, and until a catch-up song has taken the music worker, so a busy run does not skip a backoff or miss a buffer dip.

## 0.5.2 (2026-11-22)

- Dashboard sessions are no longer cookies. A browser sends a cookie for 127.0.0.1 to every port on the host, so a
  service run by someone else on the same computer could receive a dashboard session and reuse it. `/api/session` now
  returns the session in its reply; the dashboard keeps it in `sessionStorage` (scoped to its own origin, port
  included) and sends it as `X-Myradio-Session`. Cookies are never accepted, and one left by an older version is
  cleared. The live monitor uses a stream ticket (`POST /api/stream-ticket`, then `/stream.wav?ticket=`), valid for 30
  seconds, because an audio element cannot send headers. A reload keeps the session; a new tab unlocks again.

## 0.5.1 (2026-11-22)

- `POST /api/shutdown` (authorized) shuts the studio down as Ctrl+C does, so a launcher can take the station off air
  and close it. Send `transport.stop` first if the station should not resume playing on its next start.
- The launcher opens the dashboard through a private page with a one-time login ticket instead of putting the launch
  code in the browser's command line.

## 0.5.0 (2026-11-22)

### Signed requests

- A client can sign each request with the control token instead of sending it. It asks for a one-time challenge
  (`GET /api/challenge`, no token needed) and sends `X-Myradio-Auth: v1 <challenge> <HMAC-SHA256>` over the method,
  path, Host, challenge and the body's SHA-256 (`X-Myradio-Body-SHA256`). Each challenge works once, within 30 seconds,
  and only at the studio that issued it, so a captured request cannot be replayed. The studio checks that the body
  matches its hash before using it, and signs its reply (`X-Myradio-Response-Auth`). A listener that is not the studio,
  such as another account holding the port or the far end of an SSH tunnel while the studio is down, never learns the
  token. The `X-Myradio-Token` header and dashboard sessions work as before.
- `POST /api/login-ticket` (authorized) returns a login ticket that `POST /api/session` trades once, within 60 seconds,
  for a dashboard session (`X-Myradio-Ticket`). A launcher can open the dashboard at `/#ticket=<ticket>` without giving
  the browser the launch code.
- The dashboard keeps the launch code only until it has a session cookie, and no longer stores it in session storage or
  sends it with later requests.

### Autopilot DJ and news on request

- New director extension, the autopilot DJ. While it is on, every few queued songs get an intro from a host, a station
  ID goes out on a schedule, and approved research updates are read as news. Each clip is placed in front of the song
  it belongs to; an intro whose song already played is deleted instead of aired late. Off by default: turn it on with
  `director.configure` or `"enabled": true` in `ext/director/config.json`.
- `director.news_now` looks a topic up on request: a dedicated researcher reads a Google News search for it, the chosen
  host reads the sourced headlines, naming each publisher, and the update plays next.
- Research gains `research.claim` and `research.release`, so a director can air an approved update exactly once and
  give it back if its speech fails.

### One graphics card for music and voices

- A resource group that makes both songs and speech now puts music first: speech waits while the song buffer is
  filling, runs as a batch once music is ahead, and gives the card back when music runs low. Urgent requests are never
  held. Automatic when one group has both kinds of maker; set `"music_first": false` on the group to turn it off.

### Voices

- `speech_http` voices take `speaker_voices`, a voice per speaker slot, so every host keeps their own voice on one
  server (for example ZONOS3-Mini's built-in voices).
- `speech_http` voices take `fade_out_ms`, sent to servers that support it, for voices whose last frame can end loud
  enough to fail the abrupt-ending check.

### Hermes plugin 0.2.0

- `/radio` shows a now-playing card: what is on air, time left, up next, the stream and the makers.
- `/radio play <words>` finds a library song and plays it next; `/radio request <idea>` asks the music maker for a
  new song, with the title, style and lyrics written by your Hermes model on Hermes 0.23 and newer; `/radio fade`.
- New `myradio_library` tool to search the library. `myradio_status` gives the model a compact status, so a big
  library no longer fills its context.
- Plain error messages with the exact command to run when the station is not running, the data folder is wrong or
  the plugin points at a different station.
- The DJ-shift blueprint can nod to the owner's day from Hermes memory, in general words and never while live.

## 0.4.0 (2026-11-07)

The first public release. myradio is a radio station that runs at home on your graphics card: it makes new songs in
any genre, to your own lyrics, and your AI agent can be the DJ.

### Install

```
curl -fsSL https://myradio.garden/install.sh | sh
myradio
```

The installer works on a Mac with Apple silicon, Linux with an NVIDIA graphics card, and the NVIDIA DGX Spark. It
downloads the 4.5 GB YuE2 music model, checks every file, writes the settings and makes a test song. Windows runs the
studio with your own audio; making songs on Windows is still being tested.

### New

- `myradio setup` and `install.sh`: one command from nothing to a station that can make music.
- Songs made on your own computer with YuE2 Turbo Radio through audio.cpp, on CUDA and Metal.
- DJ voices: ElevenLabs, or local voices (Qwen3-TTS, Drama 3, IndexTTS2). The DGX Spark runs the music model and a
  DJ voice together.
- Connect your agent: one copy sets up the MCP server, so an agent can DJ the station and keep it supplied.
- The studio: one page laid out like a mixing desk, with Coming up, the library crate (music, DJ clips, spots), fade
  to next, a GO LIVE guard, and Dial, the station pet, with levels and trophies.
- Windows 10/11 support for the studio (thanks to Frank).
- Apache-2.0 licence.

### Speed

Minutes of music made per minute, over an hour on air: 3.64× on an NVIDIA RTX 4080, 3.09× on an Intel Arc Pro B70,
2.29× on an NVIDIA DGX Spark with the DJ voice, and 1.03× on an Apple Mac mini (M4 Pro). The paper is at
https://myradio.garden/research/.

### Licences

myradio is Apache-2.0. Songs made with YuE2 are CC BY-NC 4.0 (non-commercial). Each model keeps its own licence.
