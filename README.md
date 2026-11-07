# myradio

**Your own radio station. New songs about your life, made on your computer.**

Everyone gets their own bard. myradio makes songs while you game, study, throw a party or cook dinner, and plays them
on your own station, with a DJ between songs. Hook up your favourite AI agent and it becomes the DJ.

https://myradio.garden

## Install

```sh
curl -fsSL https://myradio.garden/install.sh | sh
```

That's it. It installs myradio, downloads the music model (4.5 GB) and plays a short test song. Then open your studio:

```sh
myradio
```

**Works on:** a Mac with Apple silicon (M1 or newer) · a Linux PC with an NVIDIA graphics card (12 GB or more) ·
an NVIDIA DGX Spark. On Windows 10/11 the studio, playout and broadcasting run from a source checkout today, with
your own audio; song-making and the one-line installer are not set up there yet. See [Windows](docs/REFERENCE.md#windows).

## Make it yours

1. **Connect your agent.** In the studio, open **Menu → Connect your agent**, press **Copy** and paste it into
   Claude Code, Codex or any agent with MCP.
2. **Tell your agent what your station should sound like**: "late-night lo-fi about my exams", "pop punk for the
   skatepark". It asks for the songs and writes what the DJ says.
3. **Press Start station**, then **Listen**.

Your station is private until you choose to go live.

## Ideas

- Songs about your game, made live while you stream
- Your study notes, sung back to you
- A birthday song with the family's inside jokes
- A song for the one you love

## Proof

We ran a station for a full hour on three computers, every song made during the hour:

| Computer | Speed (minutes of music per minute of work) |
| --- | --- |
| Mac mini (M4 Pro) | 1.03× realtime |
| RTX 4080 desktop | 3.64× realtime |
| DGX Spark | 2.29× realtime, with the DJ voice running beside it |

The details are in [docs/runs](docs/runs/) and the [research paper](docs/research/benchmarks-2026-11-06/PAPER.md).

## More

- [Reference](docs/REFERENCE.md): running it by hand, scheduling, the agent and HTTP interfaces, backups, broadcasting
- [Research](docs/RESEARCH.md): help make it faster and better

## License

myradio is open source under Apache-2.0. Songs made with YuE2 are CC BY-NC 4.0: share them, don't sell them.
Made by Omnispace Technologies, a member of the NVIDIA Inception program.
