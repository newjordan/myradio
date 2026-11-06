# Evidence: two one-hour station runs (2026-11-06)

Everything recorded during the two hour-long runs behind [the Mac mini receipt](../docs/runs/2026-11-06-mac-mini-hour.md)
and [the RTX 4080 receipt](../docs/runs/2026-11-06-apollo-hour.md). Each run is split into three zips that unpack into the
same folder.

| Run | Machine | Hour on air | Fresh songs aired | Renders | Music made / render time |
| --- | --- | --- | --- | --- | --- |
| Skatepark Radio (pop punk, hard rock) | Mac mini, Apple M4 Pro, Metal | 3,604 s, playing at the end | 25 | 26, 0 failed | 60.6 min / 58.8 min (1.03× realtime) |
| Late Epoch (lofi, lyrics about fine-tuning) | RTX 4080 16 GB, CUDA | 3,611 s, playing at the end | 25 | 28, 0 failed | 68.9 min / 19.0 min (3.64× realtime) |

## What is in each zip

**`…-proof.zip`** (about 1 MB)
- `RECEIPT.md`: the receipt, with every render and what aired by minute.
- `report/renders.json`: **the per-song benchmark**: song length, render time, realtime factor and seed for every render,
  read from each job's `generation.job` provenance.
- `report/hour-run-log.jsonl`: every status check (every 15 s), every DJ action and every song change, timestamped.
- `report/hour-run-report.json` and `hour-run-console.txt`: the end-of-hour summary and the run's own console.
- `terminal/*.cast`: asciinema recordings of each step with real output and timing: install (Mac 1.8 s, 4080 machine 2.8 s),
  model download from Hugging Face (Mac 119 s, 4080 machine 142 s, checksums verified on the 4080 machine), hook-up, the agent
  naming the station and asking for its first songs, and the agent's tool calls through the whole hour.
- `screenshots/`: the studio at each filmed moment.
- `settings/generation-config.json`: the exact music maker settings (runtime command, model files, speed settings).
- `songs/`: the station's songs (style tags and lyrics) as the agent requested them.
- `harness/`: the scripts that ran the hour, with a note on how they differ between the two runs.
- `MANIFEST.sha256`: the SHA-256 of every file in all three zips.

**`…-films.zip`**: the studio filmed through each run (1440×900 WebM): first songs, first song on air, half an hour in, end
of the hour.

**`…-audio.zip`**: a 25 s clip of every song as it aired, taken from the station's own stream. Stored as FLAC, which is
lossless: `ORIGINAL-WAV-CHECKSUMS.txt` lists each original WAV's SHA-256 and the MD5 of its decoded audio, which the FLAC
decodes to exactly.

## Checking it

```sh
sha256sum -c SHA256SUMS                     # the zips, from this folder
for z in *.zip; do unzip -q "$z"; done
cd 2026-11-06-rtx4080-hour && sha256sum -c proof/MANIFEST.sha256
```

The control tokens and launch codes never appear in any of these files (checked before packing; the hook-up casts show
`Launch code: (hidden)`).
