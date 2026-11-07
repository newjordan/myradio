# DJ voices for myradio: bake-off and research (2026-11-06)

Goal: a selection of DJ voices that cut the mustard for users, local first, with cloud options for those who want them.
The user's recommendation stands: users connect their favourite agent, which writes the DJ copy; the voice speaks it.

## Already on air: Jessica, a local Qwen voice

Knight Shift Radio (the live station on the Intel Arc Pro B70) has been reading its hourly news desk with a **local
Qwen3-TTS voice the whole time**: Jessica, Qwen3-TTS-12Hz-1.7B CustomVoice, speaker Vivian, run through PyTorch on the B70
(not ElevenLabs), mastered to -14 LUFS and checked for gaps before airing. Her voice card also records a VoiceDesign
description ("a young adult American woman reading a short radio news desk; bright, warm, clear…", seed 17). The desks are
110 to 150 words, written to a beat (open models, local inference, agents, AI music) with every claim checked at its source.

The same model runs in myradio's audio.cpp runtime: reading the 4:00 desk on the DGX Spark, CustomVoice "vivian" made
127.2 s of speech in 47.3 s (**2.69× realtime**; the live take is 126.3 s) and VoiceDesign with her description 101.1 s in
38.3 s (2.64×). 2.8 GB (Q8), Apache-2.0: free to use commercially. myradio's new `speech_http` worker serves her (or any
audio.cpp voice) for the news (`update`) role or as the DJ.

## Local voices (rendered on the DGX Spark, toymaker)

Same runtime as the music (audio.cpp), same box, every voice reading the same hype line and the same song lead-in while a
YuE2 song rendered at the same moment. Speed is the lead-in's realtime factor with the model already loaded. An hour with a
break every 2 or 3 songs needs roughly 3 to 8 minutes of talk, so anything above about 0.2× keeps up. Listening page:
https://claude.ai/artifact/UF4JR17zJtggZ1RdqMPUik (private). Takes and timings: `~/myradio-promo/examples/voice-bakeoff/`.

| Voice | Beside music | Size (Q8) | Licence (commercial use) | How the energy is asked for |
| --- | --- | --- | --- | --- |
| Chatterbox Turbo | 1.72× | 0.7 GB | MIT (Yes) | inline tags such as [laugh]; one built-in voice |
| Chatterbox | 1.42× | 2.1 GB | MIT (Yes) | exaggeration dial; clones a reference voice (task `clon`) |
| VoxCPM2 | 1.03× | 3.0 GB | Apache-2.0 (Yes) | (style description) before the text; clones |
| Kokoro 82M | 11.20× | 0.2 GB | Apache-2.0 (Yes) | 54 built-in voices, speed only |
| Qwen3-TTS CustomVoice | 0.63× | 2.8 GB | Apache-2.0 (Yes) | built-in voices plus an instruction |
| Qwen3-TTS VoiceDesign | 0.62× | 2.8 GB | Apache-2.0 (Yes) | voice described in words (task `vdes`) |
| VibeVoice 1.5B | 0.50× | 3.2 GB | MIT (Yes) | podcast-style speakers from voice samples |
| Drama 3 B70 Turbo | 0.36× | 22.7 GB | Fish Audio Community License (Conditional) | free-form tone tags and wording; clones |
| Maya1 | 0.24× | 4.0 GB | Apache-2.0 (Yes) | voice described in words plus <laugh>-style tags |
| IndexTTS2 | 0.23× | 3.6 GB | bilibili Model Use License (Conditional) | emotion text and strength; clones (`voice_ref`) |

Not taken forward: Higgs, Voxtral TTS, OmniVoice, F5 and Fish S2 (non-commercial only); MOSS-TTSD (two-host dialogue,
12 GB, cloning drifts between turns per its own docs); Drama3-small (its IQ3_XXS format needs a newer audio.cpp).

**Drama 3 is prompt-driven** (the user's point): the same model and line, prompted four ways (voice direction, hype wording
with tags, tags through the line, stadium announcer), gave clearly different takes; hype wording plus tags produced the most
performed reads (16 to 19 s for the line against 11 to 13 s for the others). Good Drama copy is written as a performance.

## Cloud voices (researched from the providers' pages, not rendered)

1. **ElevenLabs Eleven v4** (and v4 Turbo): top of the public speech arena, richest hype tags. The free plan is
   non-commercial; monetised streams need Starter ($6/month) or above. About $0.36 to $0.72 per hour of airtime on v4.
2. **Inworld TTS-2**: free-form hype directions and laughs, low latency, commercial use even on the free plan.
3. **Google Gemini 3.8 Flash TTS**: cheapest good option (about $0.07 to $0.14 per hour of airtime); free tier uses your data;
   price doubles on 2027-01-01.
4. **Cartesia Sonic 3.6**: explicit excited/euphoric emotions; commercial use from the $5 Pro plan.

Going away: Hume's TTS (2026-11-13), PlayHT (closed 2025-12-31), OpenAI's tts models (retiring 2027-01-06).

## The user's ear (2026-11-06)

Local recommendations, in the user's order:

1. **Qwen3-TTS CustomVoice** (Jessica's model): "really good, possibly top choice". 2.8 GB, Apache-2.0, proven on air.
2. **Drama 3 B70 Turbo with prompt style C** (tone tags through the line), seed 202 as the DJ's cloned voice reference.
3. **IndexTTS2**: "really good" (bilibili licence, conditional for very large companies).

Also noted: Chatterbox is "decent" (MIT, clones any voice, 1.42× beside music).

Cloud: the male announcer on the glass promo is "really good" and approved as a recommendation. It is the
**ElevenLabs voice "Guy"**, so it belongs with the cloud voices, not the local ones. For a local male announcer, the
Qwen3-TTS CustomVoice male speakers (Aiden, Dylan, Eric, Ryan, Uncle Fu) were rendered with the same glass script on
toymaker for comparison (`~/myradio-promo/examples/male-audition/`).

## What this means for users

- **Jessica (Qwen3-TTS CustomVoice) is proven on air** and is small (2.8 GB), quick (2.69× on the Spark) and Apache-2.0:
  the natural local news anchor and a strong local DJ default.
- **Everyone gets a voice that runs on their own machine and may be used commercially**: the MIT/Apache models above.
  Chatterbox and Chatterbox Turbo are the fastest expressive ones beside music; Kokoro is the tiny fallback for small
  machines (no emotion control).
- **Drama 3** is the most performative when prompted well, at 23 GB and a conditional licence: the premium local choice
  for big machines.
- **Cloud for the best voice with no GPU cost**: ElevenLabs (paid plan for monetised streams), or Inworld for the simplest
  licence.
