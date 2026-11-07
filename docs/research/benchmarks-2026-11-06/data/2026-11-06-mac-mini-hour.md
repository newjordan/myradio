# Run receipt: Skatepark Radio, one hour on a Mac mini (2026-11-06)

A pop punk and hard rock station, set up from scratch on a Mac mini and run for an hour with an agent as the DJ. Every song
that aired was made fresh on the Mac during the run.

## Result

- **3,604 s on air, still playing at the end.** No dead air after Start, no stops, 238 status checks.
- **25 songs aired, all freshly made.** The agent DJ premiered each new song as it arrived: 25 premieres, 25 song requests,
  4 fills, 0 errors, 0 reconnects.
- **26 renders, 0 failures.** 60.6 minutes of music made in 58.8 minutes of rendering: 1.03× realtime
  overall, median 1.04×, best 1.14×.

## Setup

| | |
| --- | --- |
| Machine | Mac mini, Apple M4 Pro, 24 GB, macOS 26.6.2 |
| myradio | `469a025`, Python 3.12, data dir `~/.myradio-skatepark` |
| Model | [YuE2 Turbo Radio](https://huggingface.co/Frosty40/YuE2-Turbo-Radio) (YuE2-3B Q8_0 + F16 VAE) |
| Runtime | `audiocpp_cli --task gen --family yue2 --backend metal --threads 8`, 8 steps, guidance 1.0, `cot=full` |
| Station | 12 original songs (tags and lyrics), about 2:45 asked per song, one render at a time |
| DJ | an agent over myradio's MCP adapter (`python3 -m myradio.mcp`), the same command `Connect your agent` hands out |

## Every render

| Song | Length | Render | Realtime |
| --- | --- | --- | --- |
| Bedroom Floor | 2:27 | 2:22 | 1.03× |
| Bedroom Floor (take 2) | 2:23 | 2:13 | 1.07× |
| Detention Kings | 2:06 | 1:59 | 1.05× |
| Detention Kings (take 2) | 1:53 | 1:45 | 1.07× |
| Engine Heart | 2:44 | 2:32 | 1.08× |
| Engine Heart (take 2) | 2:60 | 3:02 | 0.99× |
| Gas Station Sunrise | 2:03 | 1:56 | 1.06× |
| Gas Station Sunrise (take 2) | 2:25 | 2:26 | 0.99× |
| Half Past Curfew | 1:55 | 1:46 | 1.08× |
| Half Past Curfew (take 2) | 1:30 | 1:19 | 1.14× |
| Half Past Curfew (take 3) | 1:52 | 1:43 | 1.09× |
| Last Show at the Rink | 1:38 | 1:26 | 1.14× |
| Last Show at the Rink (take 2) | 2:08 | 2:05 | 1.02× |
| Mixtape Summer | 2:16 | 2:12 | 1.03× |
| Mixtape Summer (take 2) | 2:04 | 1:55 | 1.08× |
| Postcard from the Basement | 2:31 | 2:29 | 1.01× |
| Postcard from the Basement (take 2) | 1:54 | 1:40 | 1.14× |
| Postcard from the Basement (take 3) | 1:58 | 1:48 | 1.10× |
| Senior Skip Day | 2:20 | 2:19 | 1.01× |
| Senior Skip Day (take 2) | 2:27 | 2:24 | 1.02× |
| Static on the Line | 2:25 | 2:18 | 1.05× |
| Static on the Line (take 2) | 2:38 | 2:40 | 0.99× |
| Ten Feet Tall | 3:00 | 3:08 | 0.96× |
| Ten Feet Tall (take 2) | 3:00 | 3:10 | 0.95× |
| Thunder Road Revival | 2:60 | 3:07 | 0.96× |
| Thunder Road Revival (take 2) | 3:00 | 3:02 | 0.99× |

## What aired, by minute

| Minute | Song |
| --- | --- |
| 0.3 | Half Past Curfew |
| 4.0 | Postcard from the Basement |
| 6.6 | Engine Heart |
| 9.3 | Mixtape Summer |
| 11.3 | Detention Kings |
| 13.6 | Static on the Line |
| 15.9 | Gas Station Sunrise |
| 18.2 | Thunder Road Revival |
| 20.9 | Ten Feet Tall |
| 24.0 | Senior Skip Day |
| 26.2 | Bedroom Floor |
| 28.8 | Half Past Curfew (take 2) |
| 30.3 | Last Show at the Rink |
| 32.0 | Postcard from the Basement (take 2) |
| 33.8 | Engine Heart (take 2) |
| 36.8 | Mixtape Summer (take 2) |
| 38.8 | Detention Kings (take 2) |
| 40.8 | Static on the Line (take 2) |
| 43.4 | Gas Station Sunrise (take 2) |
| 45.9 | Thunder Road Revival (take 2) |
| 48.9 | Ten Feet Tall (take 2) |
| 51.7 | Senior Skip Day (take 2) |
| 54.2 | Bedroom Floor (take 2) |
| 56.8 | Last Show at the Rink (take 2) |
| 58.8 | Half Past Curfew (take 3) |

## Files

In the repository as zips under [`evidence/`](../../evidence/README.md): `hour-run-report.json`, `hour-run-log.jsonl` (every status check and DJ
action), `renders.json` (per-job timings from `generation.job` provenance), a 25 s clip of each song as it aired, the
terminal casts of the setup and the agent's tool calls, and four studio films (first run, first song on air, half an hour in,
end of the hour).
