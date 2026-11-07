# Agent plugins

Plugins that let an agent harness DJ and run a myradio station, packaged the way each harness installs things.
Each folder stands on its own and has its own README with install steps.

| Folder | Harness | What you install |
|---|---|---|
| [`hermes/`](hermes/) | [Hermes Agent](https://github.com/NousResearch/hermes-agent) 0.16+ | A native plugin: 12 tools, `/radio` (now playing, play, request a song), the `myradio:dj` skill, a DJ-shift cron blueprint |
| [`omp/`](omp/) | [omp (oh-my-pi)](https://github.com/can1357/oh-my-pi) | A plugin package: the MCP server, a DJ skill, `/radio` |

Both reuse what the studio's **Connect your agent** sheet sets up: myradio's own MCP adapter
(`python -m myradio.mcp`), which reads the station's private control code from its data folder by itself. No
plugin asks for, stores or prints a token. `myradio_mcp.py`, identical in each folder, finds myradio and starts
that adapter:

- myradio: `MYRADIO_HOME` if set, else the checkout the plugin sits in, else the `myradio-mcp` command the
  installer puts on PATH, else `~/myradio`.
- Station: `MYRADIO_DATA_DIR` (default `~/.myradio`) and `MYRADIO_PORT` (default 8765).

Start the station first. Any other MCP agent can use the same adapter: see **Connect your agent** in the studio
or `docs/REFERENCE.md`.
