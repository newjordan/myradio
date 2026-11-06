# myradio launch: promotional video plan (v2, 2026-11-06)

## The message

**Your own radio station, made on your own computer. Your agent is the DJ.**

Three things the films prove, all with real footage and real airchecks:

1. **It runs on hardware people own.** A Mac mini, a PC with an RTX 4080, a DGX Spark.
2. **Every song is made live.** Each machine kept a station on air for a full hour on songs it made during that hour.
3. **The DJ is part of the station.** On the Spark, one server holds the music model and the DJ voice together (the dual
   loader); your own agent picks the songs, writes the breaks and the station IDs, and runs the show.

Positioning: hook up your favorite agent (Connect your agent) and let it DJ and write the copy. Everything is open source;
the models are on Hugging Face.

## Deliverables

| # | Piece | Format | Use |
| --- | --- | --- | --- |
| 1 | Hero promo film | 75 s, 16:9, 1920×1080 | Launch post, README, model cards, YouTube, X |
| 2 | Vertical cut | 30 s, 9:16, 1080×1920 | Shorts, Reels, TikTok, X |
| 3 | Three machine cards | 15 s each, 16:9 and 1:1 | One per machine in the launch thread |
| 4 | Instruction video | 4 to 6 min, chapters | After the real installer exists (chapters below) |
| 5 | Stills | thumbnail 1280×720, social card 1200×630 | Built on the night-room art |

## Look and sound

- **Ground:** the empty radio room at night (`~/myradio-promo/art/late-night-station-2.png`, wide; `-1.png` for close
  shots). Dark, soft, slow push-ins. Pixel art is scaled by whole numbers with nearest-neighbour so it stays crisp.
- **Picture:** the Console studio as filmed during the runs, the terminal casts replayed as typed animations, the night
  room between scenes. The studio's own palette (ink blue, coral, lime, cream LCD) for type and accents.
- **Sound:** real airchecks only, no stock music: Late Epoch lofi for the calm parts, Skatepark Radio choruses for energy,
  Wren's real voice for the DJ moments.
- **Numbers on screen** come straight from the receipts in `docs/runs/`, exactly as measured.
- **Captions** burned in for silent autoplay. Mixed to -14 LUFS for social platforms.

## Source footage (all recorded on 2026-11-06)

| Run | Station | Footage | Airchecks |
| --- | --- | --- | --- |
| Mac mini M4 Pro | Skatepark Radio (pop punk, hard rock) | `recordings/2026-11-06-mac-mini/`: 4 studio films, 5 casts | 25 songs |
| RTX 4080 (apollo) | Late Epoch (lofi about fine-tuning) | `recordings/2026-11-06-apollo/`: 4 studio films, 5 casts | 25 songs |
| DGX Spark (toymaker), dual loader | Late Epoch with Wren, the DJ | `recordings/2026-11-06-toymaker/`: studio films, 5 casts including `02-dual-loader` | songs, Wren's breaks and station IDs |

The Mac and RTX 4080 recordings are also in the repository as checksummed zips (`evidence/`).

## Hero film, 75 seconds

| Time | Picture | Sound | Source |
| --- | --- | --- | --- |
| 0:00 | The night room, slow push-in; the ON AIR panel glows. | Late Epoch lofi bed, low | art take 2; toymaker `audio/01-Learning-Rate` |
| 0:05 | Cut to the studio LCD: "Learning Rate". Caption: *This song didn't exist a minute ago.* | lofi up | toymaker `studio/06a` |
| 0:10 | Install montage, three terminals stacked: Mac mini, RTX 4080, Spark. Install in seconds, the model downloads, checksums OK. | soft typing ticks | each run's `01-install`, `02-models` casts |
| 0:20 | The dual loader comes up: *One server. Music and DJ voice, both loaded.* | lofi | toymaker `02-dual-loader` cast |
| 0:28 | Connect your agent: one Copy, then the agent's tool calls naming the station and asking for songs. | lofi | `04-agent-first-requests` casts; studio `04` |
| 0:36 | Wren on air: the SPEECH cart lights, her words captioned as she says them. | **Wren's real break** | toymaker `audio/03-Wren--Learning-Rate`; studio film |
| 0:46 | Hard cut to Skatepark Radio: loud chorus, LCD titles flipping across the hour. *25 fresh songs. One hour. Zero failed renders.* | **Skatepark chorus** | Mac `studio/06b`, `06c`; Mac airchecks |
| 0:58 | Three machine cards with each one's measured result. | music continues | `docs/runs/` receipts |
| 1:06 | End card on the night room: **myradio.** *Hook up your favorite agent. It's the DJ.* Repo, models, *Research: help wanted*. | **Wren's station ID** as the sting | toymaker `audio/10-Late-Epoch-ID-1` |

Machine card numbers: Mac mini, 60.6 min of music made in 58.8 min; RTX 4080, 68.9 min made in 19.0 min (3.64× realtime);
DGX Spark, music and DJ voice made together through the hour (figures from the toymaker receipt when the run is written up).

## Vertical cut, 30 seconds

| Time | Picture | Sound |
| --- | --- | --- |
| 0:00 | Night room, vertical crop on the desk and the ON AIR glow | Wren's station ID |
| 0:03 | LCD song title, caption *made live, on this computer* | lofi |
| 0:10 | The agent's tool calls; a cart jumps to the top of the wall | lofi |
| 0:20 | Three machines, one number each | Skatepark chorus |
| 0:27 | End card | last chord |

## Machine cards, 15 seconds each

One per machine, same template: the machine's name and photo-free silhouette, its station's LCD, its measured line from
the receipt, a 10-second aircheck from that machine. Mac mini → Skatepark Radio; RTX 4080 → Late Epoch; DGX Spark → Late
Epoch with Wren.

## Caption script (hero film)

1. This song didn't exist a minute ago.
2. Install. Download the model. Done.
3. One server. Music and DJ voice, both loaded.
4. Connect your agent. It's the DJ.
5. It picks the songs. It writes the breaks.
6. 25 fresh songs. One hour. Zero failed renders.
7. Mac mini. RTX 4080. DGX Spark.
8. myradio. Your station, made on your computer. Open source.

## Instruction video chapters (after the real installer)

1. What you need (Apple silicon Mac, NVIDIA PC or DGX Spark; free disk space).
2. Install myradio.
3. Download the models (music; DJ voice: local Drama 3, or ElevenLabs).
4. Hook up: the checks and what each means.
5. Your first song.
6. Connect your agent: copy, paste, the green light; it DJs and writes the copy.
7. Your station, your way: the cart wall, the desk, the menu sheets.
8. Going live: stream address, the GO LIVE guard.

## How we make it

- Built with HyperFrames (HTML compositions rendered to video) in `~/myradio-promo/film/`, outside the repository.
- **1. Pull assets:** cut exact in/out points from the films and airchecks with FFmpeg; scale the night-room art 2× with
  nearest-neighbour (3344×1882) for pans.
- **2. Animatic:** titles, timing and captions over rough footage; reviewed with you as stills on a private page.
- **3. Polish and render:** 1080p30, captions burned in, loudness -14 LUFS; vertical and card versions from the same project.
- **4. Publish:** with the repository going public (your call on timing).

## Credits on the end card and in descriptions

YuE2 Turbo Radio (YuE2 by Multimodal Art Projection, CC BY-NC 4.0: the songs can be shared freely, not sold). Drama 3 by
Fish Audio, in Frosty40's B70 Turbo build (check its license wording on the model card before publishing). audio.cpp
(Apache-2.0). myradio: open source.

## What the films must not get ahead of

- The onboarding shown in the preview is a walk-through; the real installer (download with resume, graphics card detection,
  automatic hook-up) is next. Film the real path for the instruction video when it exists.
- The local DJ voice is real (Drama 3 on the Spark); the ElevenLabs voice adapter is not built yet.
- Output is Icecast MP3; YouTube and Twitch need an RTMP output that does not exist yet.

## Decisions for you

1. Narrator for the instruction video: Wren (the station's own local voice), your voice, or captions only.
2. Open the hero film calm (Late Epoch) and hit the energy at 0:46 (Skatepark), as above, or open loud?
3. Platforms and release timing (tied to making the repository public).
