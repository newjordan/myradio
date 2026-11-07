# myradio for omp (oh-my-pi)

Your omp agent DJs your [myradio](https://github.com/newjordan/myradio) station: it plans the queue, writes the
DJ's lines for the station's voice and keeps the music going.

This folder is an omp plugin package. omp picks up its parts by convention:

- **`.mcp.json`**: the `myradio` MCP server, myradio's own adapter. omp names its tools `mcp__myradio_<tool>`
  (`mcp__myradio_station_status`, `mcp__myradio_queue_add`, `mcp__myradio_transport_skip`,
  `mcp__myradio_generation_enqueue`, and everything else the station offers: shows, hosts, sponsor spots).
- **`skills/myradio-dj`**: how to DJ the station (flow, lead-ins, station IDs, what to leave to you).
- **`commands/radio.md`**: `/radio` checks the station; `/radio skip this one` or `/radio queue something
  mellow` asks the agent to do that.

## Install

You need a running myradio station (`myradio`, or `python3 -m myradio.server` from a checkout) and `python3` on
your PATH.

```sh
omp plugin link ~/myradio/integrations/omp
omp plugin list
```

Use your own path in place of `~/myradio`. Start a new omp session (or run `/reload-plugins` in a running one).
`omp plugin disable omp-myradio` turns it off; `omp plugin uninstall omp-myradio` removes the link.

Link rather than copy: the launcher finds myradio through the folder it lives in. A copy still works when
myradio is installed (`myradio-mcp` on PATH) or checked out at `~/myradio`.

## How it finds your station

`myradio_mcp.py` finds myradio (this checkout, the installed `myradio-mcp` command, or `~/myradio`) and starts
myradio's own MCP adapter, the same one the studio's **Connect your agent** sheet sets up. The adapter reads the
station's private control code from the data folder itself. You never paste a token anywhere.

Defaults: data folder `~/.myradio`, port 8765. For others, set `MYRADIO_DATA_DIR` and `MYRADIO_PORT` in the
environment you start omp from, or add an `env` block to the server in `.mcp.json`.

## Try it

```
You:    /radio
You:    Be the DJ for the next hour. Keep it mellow and do a station ID every 15 minutes.
You:    /radio fade into something faster
```

The station's tools act immediately. Going live (`broadcast_start`) makes the station public; the skill and
`/radio` tell the agent to do it only when you say so.
