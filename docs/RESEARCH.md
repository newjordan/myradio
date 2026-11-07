# Research: help wanted

myradio is open source, and so are the models behind it. The station is also a research bench: every song it makes is timed,
logged and kept, so improvements to the models show up as real airplay, not just benchmark numbers. If any of the problems
below interest you, you can help.

**House rule: a number comes with a receipt.** A measurement counts when it states the hardware, the exact build and command,
the audio length and the wall time, and when the audio is kept so someone else can listen. Claims without receipts are welcome
as ideas, not as results.

## Where things stand (published measurements)

| Model | Hardware | Measured |
| --- | --- | --- |
| [YuE2 Turbo Radio](https://huggingface.co/Frosty40/YuE2-Turbo-Radio) (music, 4.53 GB, YuE2-3B Q8_0 + F16 VAE) | RTX 4080 16 GB, CUDA | 3.17× realtime for a 69 s song, 3.32× for a 4:17 song (8 steps); streaming first audio 8.6 to 9.4 s including cold start; 16 songs in a row at 2.6 to 3.0× |
| same | RTX 4080 16 GB, CUDA, in myradio | a live station for a full hour: 25 fresh songs aired, 68.9 min of music made in 19.0 min (3.64× realtime), first song on air 56 s after the request, 0 failed renders ([receipt](runs/2026-11-06-apollo-hour.md)) |
| same, plus [Drama 3 B70 Turbo](https://huggingface.co/Frosty40/Drama-3-B70-Turbo-GGUF) as the DJ voice | DGX Spark (GB10), CUDA, dual loader in myradio | music and DJ voice in one server (24 GB): an hour on air with 24 songs and 12 DJ clips, 61.8 min of music made in 27.0 min (2.29× realtime) while the voice shared the GPU, 0 failed renders ([receipt](runs/2026-11-06-toymaker-dual-loader-hour.md)) |
| same | Intel Arc Pro B70, SYCL | 3.09× session realtime (8 steps, Nov 1), up from 0.63× on stock settings; streaming first audio 7.6 s; a 30-minute queue; then ran the live Knight Shift Radio station |
| same | Apple M4 Pro (Mac mini), Metal | keeps a live station fed for a full hour: 25 fresh songs aired back to back, 60.6 min of music made in 58.8 min, 0 failed renders ([receipt](runs/2026-11-06-mac-mini-hour.md)) |
| Qwen3-TTS 1.7B CustomVoice (speech, 2.8 GB Q8): Jessica, the news voice | Intel Arc Pro B70 (live station) and DGX Spark | reads Knight Shift Radio's hourly news desk on air from the B70; through audio.cpp on the Spark, a 127 s desk in 47 s (2.69× realtime) ([notes](research/dj-voices.md)) |
| [Drama 3 B70 Turbo](https://huggingface.co/Frosty40/Drama-3-B70-Turbo-GGUF) (speech, 24.3 GB) | Intel Arc Pro B70 | 26.1 ms to make a 46.4 ms audio frame (0.56 real-time factor, 2.6× faster than stock); streaming first audio 0.27 to 0.35 s |

The model cards hold the receipts. myradio adds one more kind: an hour of real airplay, with each song's render time and a
clip of what aired (the first one was [run on a Mac mini on 2026-11-06](runs/2026-11-06-mac-mini-hour.md)).

## Open problems

1. **Live and dynamic serving speed.** Taking the speed above to every platform people own, with first audio fast enough to
   feel live, while the same machine also runs the station.
2. **Quality of the output.** Repeatable ways to say one take is better than another: lyric adherence, vocal clarity,
   structure (does the chorus come back), clipping and abrupt endings, loudness. myradio already checks every take for
   silence, clipping and cut-off endings before it airs.
3. **Direct extraction and tuning.** Reading useful structure straight out of the models (the semantic tokens can already be
   exported) and tuning the models from it.
4. **Synthesizers with small models as controllers.** Music synthesis and vocal manipulation where a small embedded model is
   the controller: a knob, a pad, a voice or a gesture steering generation or a vocal chain in real time. The studio's desk is
   where these would live.
5. **Head control.** Part of the author's own research plans; a description is coming.

## How to help

- **Reproduce a measurement on your hardware** (another Mac, another NVIDIA or AMD card, another Intel Arc) and share the
  receipt. Different hardware is the fastest way to find out what generalizes.
- **Run a station for an hour** and share the log: `generation` records every job's timings in its provenance.
- **Propose and measure a speed change** (kernels, scheduling, caching, streaming windows) with before and after receipts on
  the same machine.
- **Build a quality check** that agrees with listeners, and show where it disagrees.
- **Prototype a controller** through myradio's extension interface (`myradio/extensions.py`) or the station operations an agent
  uses (`Connect your agent` in the studio).

Open an issue or a discussion with what you want to try. Credit goes to whoever does the work, and everything stays open.
