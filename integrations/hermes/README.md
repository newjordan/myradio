# myradio for Hermes Agent

Your Hermes agent DJs your [myradio](https://github.com/newjordan/myradio) station: it plans the queue, writes
the DJ's lines for the station's voice, and can run unattended DJ shifts on a schedule. Going live on the
internet stays your decision.

What it adds to Hermes:

- **11 tools** (toolset `myradio`): `myradio_status`, `myradio_queue_add`, `myradio_queue_move`,
  `myradio_queue_remove`, `myradio_start`, `myradio_skip`, `myradio_fade_next`, `myradio_schedule_add`,
  `myradio_make` (ask the music or voice maker for audio), plus `myradio_operations` and `myradio_run` for
  everything else your station offers (shows, hosts, sponsor spots, the next hours' plan).
- **`/radio`** in any session: `/radio` shows what is on air; `/radio skip`, `start`, `stop`; `/radio live` and
  `/radio off-air` connect and disconnect your saved stream. These run straight away, without a model turn.
- **Skill `myradio:dj`**: how to DJ the station (flow, lead-ins, station IDs, what to leave to you).
- **Blueprint `myradio:dj-shift`** (Hermes 0.23 and newer): a cron job that checks the station every few
  minutes, keeps 3 to 6 songs queued and writes DJ lines. During a scheduled run the plugin only allows checking,
  queueing, skipping, fading and making audio; anything else waits for you.
- **`hermes myradio check`**: shows whether Hermes can reach your station.

The agent cannot go live, go off air or delete audio. Those are `/radio live`, `/radio off-air` and the studio.

## Install

You need a running myradio station (`myradio`, or `python3 -m myradio.server` from a checkout) and Hermes
0.16 or newer.

```sh
ln -s ~/myradio/integrations/hermes ~/.hermes/plugins/myradio    # or copy the folder there
hermes plugins enable myradio
hermes myradio check
```

Use your own path in place of `~/myradio`. Copying instead of linking works too: the plugin then uses the
installed `myradio-mcp` command or a myradio folder at `~/myradio`. Start a new Hermes session to load the tools.

## How it finds your station

It uses myradio's own MCP adapter (`myradio_mcp.py` starts `python -m myradio.mcp`), the same one the studio's
**Connect your agent** sheet sets up. The adapter reads the station's private control code from the data folder
itself. You never paste a token anywhere, and this plugin never reads it.

Defaults: data folder `~/.myradio`, port 8765. If yours differ, set the plugin settings (Hermes 0.23 and newer):

```sh
hermes config set plugins.entries.myradio.settings.data_dir /path/to/data
hermes config set plugins.entries.myradio.settings.port 8766
hermes config set plugins.entries.myradio.settings.myradio_home /path/to/myradio   # only for a git checkout elsewhere
```

On older Hermes, set `MYRADIO_DATA_DIR`, `MYRADIO_PORT` or `MYRADIO_HOME` in the environment Hermes runs in.

## Try it

```
You:    What's on my radio station right now?
You:    Be the DJ for the next hour: keep it mellow, and do a station ID every 15 minutes.
You:    /blueprint myradio:dj-shift
```

## What it touches

- Starts a short-lived child process per tool call (`python myradio_mcp.py`, no shell) with a small
  environment: no API keys are passed on.
- That child reads `control.token` from the station's data folder and talks only to `127.0.0.1:<port>`.
- No other network calls, no files written, no background process, no telemetry.
