# myradio for angelX

Your angelX agent DJs your [myradio](https://github.com/newjordan/myradio) station: it plans the queue, writes
the DJ's lines for the station's voice and keeps the music going.

What it adds to angelX:

- **The `myradio` MCP server**: myradio's own adapter, so angelX gets the station's tools as `myradio__<tool>`
  (`myradio__station_status`, `myradio__queue_add`, `myradio__transport_skip`, `myradio__generation_enqueue`, and
  everything else the station offers: shows, hosts, sponsor spots), plus angelX's `myradio__mcp` surface tool.
- **The `myradio-dj` skill**: how to DJ the station (flow, lead-ins, station IDs, what to leave to you).
  Load it with `/skills myradio-dj`, or the agent picks it up from the skill catalog.

angelX has no plugin installer of its own; it reads MCP servers from `~/.angelX/mcp.json` and user skills from
`~/.angelX/skills`. `install.py` writes to exactly those two places.

## Install

You need a running myradio station (`myradio`, or `python3 -m myradio.server` from a checkout).

```sh
python3 ~/myradio/integrations/angelx/install.py --check
```

Use your own path in place of `~/myradio`. Then restart angelX. The script:

- adds a `myradio` entry to `~/.angelX/mcp.json` (or `$ANGEL_MCP_CONFIG`), keeping your other servers and saving
  the old file as `mcp.json.bak`;
- links `skills/myradio-dj` into `~/.angelX/skills` (or `$ANGEL_SKILLS_DIR`); `--copy` copies it instead;
- with `--check`, starts the adapter once and prints the station's status.

`--dry-run` shows the changes without making them; `--uninstall` takes both out again.

To do it by hand, add this to `~/.angelX/mcp.json` (angelX does not expand `~` or variables here, so use full
paths):

```json
{
  "mcpServers": {
    "myradio": {
      "command": "/usr/bin/python3",
      "args": ["/home/you/myradio/integrations/angelx/myradio_mcp.py"]
    }
  }
}
```

and link the skill: `ln -s ~/myradio/integrations/angelx/skills/myradio-dj ~/.angelX/skills/myradio-dj`.

## How it finds your station

`myradio_mcp.py` finds myradio (this checkout, the installed `myradio-mcp` command, or `~/myradio`) and starts
myradio's own MCP adapter, the same one the studio's **Connect your agent** sheet sets up. The adapter reads the
station's private control code from the data folder itself. You never paste a token anywhere.

Defaults: data folder `~/.myradio`, port 8765. For others, run `install.py --data-dir /path --port 8766`; that
writes `MYRADIO_DATA_DIR` and `MYRADIO_PORT` into the server's `env` entry.

## Try it

```
You:    /skills myradio-dj
You:    What's on my radio station right now?
You:    Queue three upbeat songs, then a station ID, and fade to the next one.
```

The station's tools act immediately. Going live (`broadcast_start`) makes the station public; the skill tells
the agent to do it only when you say so.
