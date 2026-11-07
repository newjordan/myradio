# Run receipt: Late Epoch with a DJ, one hour on a DGX Spark (toymaker, 2026-11-06)

The dual loader: one audio.cpp server on an NVIDIA GB10 holding the music model (YuE2 Turbo Radio) and the DJ voice (Drama 3
B70 Turbo) at the same time. The agent DJ (Claude, through myradio's MCP adapter) wrote every word the host said, asked for
the songs and the breaks, and ran the show. Wren is a fictional host; her voice is cloned from one reference take.

## Result

- **3,610 s on air, still playing at the end.** Playing at every one of 237 status checks after Start, no stops.
- **24 songs aired (23 different) and 12 DJ clips: lead-ins, links and station IDs.** Every song and every
  clip was made during the hour on the same box.
- **40 renders, 0 failures, music and voice side by side.** 26 songs: 61.8 min of music made in 27.0 min,
  **2.29× realtime** (median 2.35×) while the DJ voice shared the GPU. 11 DJ breaks and 3 station IDs:
  2.8 min of speech made in 3.7 min.
- **Memory:** both models resident in one server used 24 GB of the GB10's 121 GB (measured before and after loading).

Benchmark on the same box before the run (one song and three DJ lines, alone and together): music 2.34× alone and 1.72×
while speech rendered; speech 1.50× alone and 0.62× while a song rendered.

## Setup

| | |
| --- | --- |
| Machine | toymaker: NVIDIA DGX Spark (GB10, compute capability 12.1, 121 GB unified memory), CUDA 13.0 |
| Runtime | the published source `apollo/source-full-song.tar.gz` (SHA-256 `173bb8de…`), built for sm_121 with YuE2 and Drama 3; `audiocpp_server` SHA-256 `f4550bf2…` |
| Music | YuE2 Turbo Radio, 8 steps, the model card's accepted settings |
| DJ voice | Drama 3 B70 Turbo (`drama-3-b70-turbo-q4k-q8tower.gguf`); Wren cloned from one reference take as the server's default voice |
| myradio | branch `spark-dual-loader` (`42f1364`): the new `yue2_http` worker and the Drama worker in two resource groups, so a song and a break render at once |
| DJ | the agent writes the lines, asks for songs, lead-ins, links and IDs, places a break every 2 or 3 songs and an ID about every 15 minutes |

## Notes from the hour

- Two breaks were missed: the harness asked for speech at a priority the station's speech buffer holds back once two unused
  clips are ready, so new lines waited. The agent stepped in twice with urgent requests. The harness now asks at urgent
  priority, keeps one general link in stock, and plays a song next when its lead-in is ready.
- Three DJ clips (two lead-ins and a link) were put back in the queue from the studio during the hour and aired a second
  time; myradio never queues speech by itself.
- A short voice audition (six takes on a second, temporary voice server, about 47 s of GPU time) ran during the hour.
- The first start ended after 20 s on a harness bug (a duplicated log argument); its log is kept in `aborted-start/` and the
  hour was restarted cleanly.

## Every render

| Role | Title | Length | Render | Realtime |
| --- | --- | --- | --- | --- |
| song | Checkpoint at 3 AM | 2:30 | 1:08 | 2.20× |
| song | Checkpoint at 3 AM (take 2) | 2:18 | 0:59 | 2.35× |
| song | Early Stopping | 1:59 | 0:51 | 2.35× |
| song | Early Stopping (take 2) | 2:04 | 0:53 | 2.35× |
| song | Frozen Layers | 2:13 | 0:53 | 2.52× |
| song | Frozen Layers (take 2) | 2:30 | 1:03 | 2.39× |
| song | Gradient Drift | 2:45 | 1:15 | 2.20× |
| song | Gradient Drift (take 2) | 2:45 | 1:18 | 2.12× |
| song | Learning Rate | 2:20 | 0:59 | 2.35× |
| song | Learning Rate (take 2) | 2:19 | 0:56 | 2.46× |
| song | Learning Rate (take 3) | 2:05 | 0:51 | 2.46× |
| song | Loss Curve Lullaby | 2:41 | 1:17 | 2.08× |
| song | Loss Curve Lullaby (take 2) | 2:15 | 0:54 | 2.52× |
| song | Loss Curve Lullaby (take 3) | 2:08 | 0:53 | 2.39× |
| song | Low Rank Love | 2:28 | 1:02 | 2.37× |
| song | Low Rank Love (take 2) | 1:55 | 0:53 | 2.17× |
| song | One More Epoch | 2:31 | 1:08 | 2.21× |
| song | One More Epoch (take 2) | 2:24 | 1:05 | 2.20× |
| song | Small Batch Sunday | 2:10 | 0:54 | 2.40× |
| song | Small Batch Sunday (take 2) | 2:22 | 0:59 | 2.42× |
| song | Validation Rain | 2:38 | 1:08 | 2.33× |
| song | Validation Rain (take 2) | 2:45 | 1:11 | 2.33× |
| song | Warmup Steps | 2:30 | 1:00 | 2.48× |
| song | Warmup Steps (take 2) | 2:05 | 0:53 | 2.35× |
| song | Weights and Measures | 2:45 | 1:31 | 1.81× |
| song | Weights and Measures (take 2) | 2:26 | 1:05 | 2.27× |
| station ID | Late Epoch ID 1 | 0:05 | 0:09 | 0.53× |
| station ID | Late Epoch ID 2 | 0:06 | 0:11 | 0.56× |
| station ID | Late Epoch ID 3 | 0:04 | 0:03 | 1.30× |
| DJ break | Wren: Checkpoint at 3 AM | 0:14 | 0:24 | 0.60× |
| DJ break | Wren: Learning Rate | 0:17 | 0:28 | 0.61× |
| DJ break | Wren: Loss Curve Lullaby | 0:16 | 0:36 | 0.44× |
| DJ break | Wren: Low Rank Love | 0:14 | 0:10 | 1.44× |
| DJ break | Wren: One More Epoch | 0:13 | 0:21 | 0.62× |
| DJ break | Wren: Validation Rain | 0:12 | 0:09 | 1.33× |
| DJ break | Wren: Weights and Measures | 0:13 | 0:09 | 1.44× |
| DJ break | Wren: link 1 | 0:12 | 0:20 | 0.61× |
| DJ break | Wren: link 2 | 0:14 | 0:22 | 0.61× |
| DJ break | Wren: link 3 | 0:14 | 0:09 | 1.46× |
| DJ break | Wren: link 4 | 0:12 | 0:08 | 1.45× |

## What aired, by minute

| Minute | | Title |
| --- | --- | --- |
| 0.3 | song | Learning Rate |
| 2.3 | song | Loss Curve Lullaby |
| 5.1 | DJ | Wren: Learning Rate |
| 5.4 | song | Learning Rate |
| 7.7 | song | Warmup Steps |
| 10.2 | song | Low Rank Love |
| 12.8 | DJ | Wren: One More Epoch |
| 13.0 | song | One More Epoch |
| 15.5 | song | Gradient Drift |
| 18.1 | DJ | Late Epoch ID 1 |
| 18.4 | song | Validation Rain |
| 20.9 | song | Early Stopping |
| 22.9 | song | Small Batch Sunday |
| 25.2 | song | Frozen Layers |
| 27.2 | DJ | Wren: Checkpoint at 3 AM |
| 27.4 | DJ | Wren: link 1 |
| 27.7 | DJ | Wren: One More Epoch |
| 28.0 | DJ | Wren: Learning Rate |
| 28.2 | song | Checkpoint at 3 AM |
| 30.7 | song | Loss Curve Lullaby (take 2) |
| 33.0 | DJ | Wren: link 1 |
| 33.2 | song | Learning Rate (take 2) |
| 35.5 | song | Checkpoint at 3 AM (take 2) |
| 38.0 | song | Low Rank Love (take 2) |
| 39.8 | DJ | Wren: link 2 |
| 40.1 | song | One More Epoch (take 2) |
| 42.3 | song | Warmup Steps (take 2) |
| 44.6 | DJ | Wren: Weights and Measures |
| 44.8 | song | Weights and Measures |
| 47.6 | song | Frozen Layers (take 2) |
| 50.1 | song | Early Stopping (take 2) |
| 52.1 | song | Gradient Drift (take 2) |
| 54.9 | DJ | Wren: link 3 |
| 55.1 | song | Learning Rate (take 3) |
| 57.1 | song | Validation Rain (take 2) |
| 59.9 | DJ | Wren: link 4 |
