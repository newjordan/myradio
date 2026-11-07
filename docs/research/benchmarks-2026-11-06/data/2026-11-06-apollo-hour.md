# Run receipt: Late Epoch, one hour on an RTX 4080 (2026-11-06)

A lofi study station with lyrics about fine-tuning models ("Lofi beats to fine-tune models to."), set up from scratch on
apollo and run for an hour with an agent as the DJ. Every song that aired was made fresh during the run.

## Result

- **3,611 s on air, still playing at the end.** Playing at every one of 237 status checks, no stops.
- **25 songs aired, 25 different titles, all freshly made.** The agent DJ: 27 premieres, 25 song
  requests, 3 fills, 0 errors, 0 reconnects.
- **28 renders, 0 failures.** 68.9 minutes of music made in 19.0 minutes of rendering: **3.64× realtime**
  overall, median 3.64×, best 3.90×. A median song of
  152 s took 41 s to make.
- **First song on air 56 s after the agent asked for it** (Learning Rate: requested 12:18:12, made by 12:18:58, on air
  12:19:08).

## Setup

| | |
| --- | --- |
| Machine | apollo: NVIDIA GeForce RTX 4080 16 GB (driver 595.84), Ubuntu 24.04.4 |
| myradio | `9035b5c`, Python 3.12.15 (installed with `uv`, no admin rights), data dir `~/.myradio-lofi` |
| Model | [YuE2 Turbo Radio](https://huggingface.co/Frosty40/YuE2-Turbo-Radio), downloaded during the run, all `SHA256SUMS` checks OK |
| Runtime | the published full-song CUDA build, `apollo/runtime/full-song/audiocpp_cli` (SHA-256 `54242a0b…d142f58`, identical to the file on Hugging Face) |
| Settings | the model card's accepted runtime: 8 steps, guidance 1.0, `cot=full`; `yue2.vae_tile_frames=1024`, `yue2.packed_projections=true`, `yue2.kv_cache_alignment=256`; `YUE2_NAR_WINDOW_FRAMES=1024`, `YUE2_NAR_OVERLAP_FRAMES=128`, `YUE2_NAR_PREFIX_CACHE=1` |
| Station | 12 original songs (tags and lyrics), about 2:45 asked per song, one render at a time |
| DJ | an agent over myradio's MCP adapter (`python3 -m myradio.mcp`), the same command `Connect your agent` hands out |

The 4080 makes songs more than three times faster than they play, so the agent asked for a new song only while fewer than
three fresh ones were waiting to air.

## Every render

| Song | Length | Render | Realtime |
| --- | --- | --- | --- |
| Checkpoint at 3 AM | 2:51 | 0:44 | 3.86× |
| Checkpoint at 3 AM (take 2) | 2:50 | 0:45 | 3.76× |
| Checkpoint at 3 AM (take 3) | 2:24 | 0:38 | 3.74× |
| Early Stopping | 2:18 | 0:39 | 3.52× |
| Early Stopping (take 2) | 1:47 | 0:32 | 3.38× |
| Frozen Layers | 2:40 | 0:44 | 3.65× |
| Frozen Layers (take 2) | 2:16 | 0:39 | 3.54× |
| Gradient Drift | 2:58 | 0:47 | 3.79× |
| Gradient Drift (take 2) | 2:46 | 0:44 | 3.78× |
| Learning Rate | 2:29 | 0:41 | 3.62× |
| Learning Rate (take 2) | 2:29 | 0:41 | 3.63× |
| Learning Rate (take 3) | 2:07 | 0:36 | 3.53× |
| Loss Curve Lullaby | 3:00 | 0:46 | 3.90× |
| Loss Curve Lullaby (take 2) | 2:48 | 0:45 | 3.76× |
| Loss Curve Lullaby (take 3) | 3:00 | 0:48 | 3.75× |
| Low Rank Love | 1:54 | 0:36 | 3.22× |
| Low Rank Love (take 2) | 1:42 | 0:32 | 3.19× |
| One More Epoch | 2:42 | 0:43 | 3.76× |
| One More Epoch (take 2) | 2:41 | 0:42 | 3.84× |
| One More Epoch (take 3) | 2:34 | 0:41 | 3.76× |
| Small Batch Sunday | 2:22 | 0:42 | 3.41× |
| Small Batch Sunday (take 2) | 2:08 | 0:37 | 3.45× |
| Validation Rain | 2:47 | 0:43 | 3.89× |
| Validation Rain (take 2) | 2:12 | 0:37 | 3.57× |
| Warmup Steps | 2:59 | 0:52 | 3.44× |
| Warmup Steps (take 2) | 1:56 | 0:34 | 3.45× |
| Weights and Measures | 1:40 | 0:30 | 3.38× |
| Weights and Measures (take 2) | 2:35 | 0:41 | 3.79× |

## What aired, by minute

| Minute | Song |
| --- | --- |
| 0.0 | Learning Rate |
| 2.5 | One More Epoch |
| 5.3 | Low Rank Love |
| 7.3 | Warmup Steps |
| 10.1 | Validation Rain |
| 12.9 | Gradient Drift |
| 15.9 | Frozen Layers |
| 18.7 | Early Stopping |
| 21.0 | Small Batch Sunday |
| 23.3 | Weights and Measures |
| 24.8 | Learning Rate (take 2) |
| 27.3 | Loss Curve Lullaby (take 2) |
| 30.1 | One More Epoch (take 2) |
| 32.9 | Checkpoint at 3 AM (take 2) |
| 35.7 | Low Rank Love (take 2) |
| 37.4 | Checkpoint at 3 AM |
| 40.4 | Validation Rain (take 2) |
| 42.4 | Gradient Drift (take 2) |
| 45.2 | Frozen Layers (take 2) |
| 47.5 | Early Stopping (take 2) |
| 49.2 | Small Batch Sunday (take 2) |
| 51.5 | Weights and Measures (take 2) |
| 54.1 | Learning Rate (take 3) |
| 56.1 | Warmup Steps (take 2) |
| 58.2 | One More Epoch (take 3) |

## Files

In the repository as zips under [`evidence/`](../../evidence/README.md): `hour-run-report.json`, `hour-run-log.jsonl` (every status check and DJ
action), `renders.json` (per-job timings from `generation.job` provenance), a 25 s clip of each song as it aired, the
terminal casts of the setup and the agent's tool calls, and studio films (first songs on the way, first song on air, half an
hour in, end of the hour).
