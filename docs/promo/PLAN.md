# myradio launch: promo film and instruction video (first plan, 2026-11-06)

## The one line

**Your own radio station, made on your own computer. Install it, describe your sound, hear your first song, and let your agent DJ.**

## What we film from (real footage, not mock-ups)

The Mac mini run on 2026-11-06 is the source material. Everything was recorded as it happened:

- `~/myradio-promo/recordings/2026-11-06-mac-mini/terminal/*.cast`: every install and setup step with its real output and
  timing (asciinema v2), plus the agent's MCP tool calls (`04-agent-first-requests`, `06-hour-agent-dj`).
- `.../studio/*.webm` and `.png`: the studio on the Mac, filmed through a tunnel at 1440x900: first run, the first songs
  arriving, the first song on air, half an hour in, and the end of the hour.
- `.../audio/*.wav`: 25 seconds of each song as it actually aired on Skatepark Radio (the station's own stream).
- `.../hour-run-report.json` and `hour-run-log.jsonl`: the proof of the hour (what aired when, render times, no dead air).

The Mac's own desktop was not recorded: macOS refuses screen capture over SSH. For the instruction video, record the Mac
locally (QuickTime or `screencapture -v`, after granting Screen Recording permission) when the real installer exists.

## Promo film: 60 to 75 seconds

| Time | Picture | Sound |
| --- | --- | --- |
| 0:00 | The Console mid-song: the lamp, the LCD with a real title, the paper trace rolling | A real Skatepark Radio chorus, loud |
| 0:06 | Title: "This station didn't exist an hour ago." | music continues |
| 0:10 | Install: the model download bar (4.53 GB) and the hook-up checklist ticking | music under, light tick sounds |
| 0:20 | "Describe your sound": typing "pop punk and hard rock, loud and fast" | |
| 0:28 | The first song arriving in the crate and dropping onto the wall | the first notes of that song |
| 0:36 | "Let your agent DJ": Copy, paste, the agent's tool calls scrolling, a cart moving to the top, the green "Agent DJ" light | music |
| 0:48 | Montage: song titles changing on the LCD across the hour, the clock jumping | quick cuts of 3 or 4 real choruses |
| 0:58 | GO LIVE guard lifting | |
| 1:02 | End card: myradio, the one line, where to get it, and "Research: help wanted" | the last chord |

## Instruction video: 4 to 6 minutes, chapters

1. What you need (a Mac with Apple silicon or a PC with a supported graphics card, about 10 GB free).
2. Install myradio (one command, or paste the setup to your agent).
3. Install the models (YuE2 Turbo Radio; a DJ voice: ElevenLabs, recommended, or local Drama 3).
4. Hook up (the checks, what each one means).
5. Your first song (one prompt; what to expect: a few minutes on a Mac mini for the first song, about real time after that).
6. Connect your agent (copy, paste, the green light; what it may and may not do).
7. Your station, your way (the cart wall, the desk, the menu sheets).
8. Going live (paste a stream address; the GO LIVE guard; checking your host's player).

## How we make it

- Build both films with HyperFrames (HTML compositions rendered to video): title cards, the terminal casts replayed as
  typed animations, the studio captures as layers, captions burned in for silent autoplay.
- Music: the real songs from the run. Everything here is open source and the films promote a free, open release, which fits
  the music model's CC BY-NC 4.0 terms (credit the model; don't sell the songs).
- Voiceover for the instruction video: the station's own DJ voice once it is connected (ElevenLabs credits are currently
  exhausted on the live station; Drama 3 needs the large local model).

## Asking for research help

The launch is also a call for collaborators: `docs/RESEARCH.md` lists the open problems (live serving speed, output quality,
direct extraction and tuning, small controller models for synthesis and vocal manipulation, head control) with the current
receipts and concrete ways to help. Put it on the end card, pin it in the launch post, link it from both model cards, and open
GitHub Discussions when the repository goes public. The Mac mini hour is the first "run a station for an hour" receipt.

## Before release (what the films must not get ahead of)

- The onboarding shown in the preview is a walk-through; the real installer (model download with resume, graphics card
  detection, automatic hook-up) is not built yet. Film the real path when it exists, or label the walk-through as such.
- No DJ voice adapter yet (ElevenLabs recommended): today the agent DJs the music but cannot talk.
- Output is Icecast MP3 only; YouTube and Twitch need an RTMP output that does not exist yet.

## Questions for you

1. The release is open source. Which license for myradio itself (the repository has no LICENSE file yet), and when should
   `newjordan/myradio` go public? Plus a landing page, or knightshiftradio.com as the showcase station?
2. Platforms at launch: Mac first (the run proves Apple silicon), Linux with Intel Arc next, Windows later?
3. Who narrates the instruction video: your voice, an ElevenLabs voice, or captions only?
