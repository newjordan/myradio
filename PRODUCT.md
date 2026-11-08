# myradio — Product brief

> **Original product brief,** written during 0.3 development. The released version is 0.4.0: see [README.md](README.md) and [CHANGELOG.md](CHANGELOG.md). The sections below describe the target product, not a list of what ships today.

## Product promise

Install it, describe your station, and go on air. Let a person run their own radio station from their home rig without becoming a broadcast engineer.

The station should help people enjoy their own interests, discover things worth caring about, and make room for creativity and positivity. It is not a compulsory general-news feed with music wrapped around it.

## Confirmed direction

- **Product domain:** operator-owned `myradio.garden` is the intended advertising/hosting home. Keep public product and listener pages separate from authenticated station control; domain ownership alone is not deployed hosting. Use the completed product for Knightshift later, not as a claim of present production readiness.

- Build standalone.
- **Music:** `Frosty40/YuE2-Turbo-Radio`. **DJ/anchor speech:** `Frosty40/Drama-3-B70-Turbo-GGUF`. Drama 3 voices the DJs, news/entertainment anchors, station IDs, and spoken ads; it is not a fallback music generator. These are separate production roles, not a primary/backup model pair.
- Turbo and Mac are equally important launch targets. The exact Turbo environment, supported hardware, and Mac requirements still need verification.
- Provide one-click installation and an approachable, substantial web UI.
- Be agent-first: an agent and a human using the UI must be able to operate the same station capabilities.
- Support content styles, host/anchor personalities, word-salad radio runners, news and entertainment segments, and easy advertising schedules.
- Send audio to a user's existing livestream or a standard broadcast output. Do not turn v1 into a new hosted-radio platform.
- Use the existing KnightshiftRadio station as the reference for current broadcasting and word-salad behavior. Its source has been inspected: style-salad/autopilot music, separate spoken links, queued playout, RTMPS output, and an optional outbound HLS publisher. The public site is configured for a YouTube channel; that configuration alone does not verify that a broadcast is currently live.
- Remote control should connect to the user's existing agent, such as Dot, a Grok bot, Hermes, or Muse. A separate custom remote-control/chat service is not the v1 priority.

## Interest-first news and research

### What “news” means here

News is an optional stream of useful updates about what the station owner and audience actually enjoy: music, art, science, games, making things, hobbies, local projects, emerging tools, and other user-selected subjects.

There is no mandatory wire-service subscription, mainstream publication list, politics feed, or breaking-news cycle. Users can choose any supported sources, including publications they personally value; the app should not impose an editorial source preference.

Default discovery should favor curiosity, constructive developments, creativity, and practical usefulness—not outrage, fear, or engagement bait. Users can exclude topics and sources, and can turn news off altogether.

### User-owned research agents

Help users create their own research/news agents with a simple form or a chat request. Each agent has:

- A name and a purpose: what to find out, for whom, and why it belongs on this station.
- Interests, specific research questions, preferred sources, and excluded topics/sources.
- A research cadence and freshness window.
- A desired segment length, presentation style, and anchor/personality.
- Scheduling rules, plus a choice between automatic airing and review before broadcast.

Keep the researcher distinct from the on-air anchor: an agent gathers and assesses material; the anchor presents the resulting brief in the station's chosen voice and style.

Example request: “Give me a short, upbeat daily update on independent electronic music, backyard astronomy, and people building useful things. Skip national politics.”

### Trust without imposing a news agenda

- Preserve source links and publication/research timestamps in the research brief and UI; the spoken segment need not read every citation aloud.
- Distinguish sourced reporting, commentary, uncertain claims, and fictional entertainment.
- A positive tone must not invent good news, erase material qualifications, or turn speculation into fact.
- If research fails or nothing worthwhile is fresh, skip the bulletin or fill the slot with explicitly non-news programming. Do not present stale research as a new report.
- Treat retrieved material as untrusted source content, not as instructions that can operate the station or obtain its credentials.

## Station experience

### Onboarding

Guide the user through model access, hardware/storage checks, station identity, content interests, style, and output connection. Installation and first launch should handle dependencies and provide clear model-download progress rather than require a command-line setup ritual.

The first useful milestone is a working station with an audible, understandable “now playing / up next” view—not a dashboard of unconfigured integrations.

### Programming and word-salad runners

Allow users to define a station style and reusable host/anchor personalities. Word salad here means the music-prompt/autopilot system, not DJ chatter. Build on KnightshiftRadio's compatible style and instrument combinations, but add a large reusable library with genuine compositional development.

The library must describe arrangements, harmonic movement, rhythmic detail, instrument roles, motifs and their development, dynamics, production, and satisfying endings—not merely concatenate genre labels. Offer simple, layered, and intricate treatments independently of texture density: complex does not have to mean loud, frantic, or overcrowded. Keep generated instrumental prompts free of DJ speech; supplied song lyrics belong to YuE2, while hosting scripts belong to Drama 3.

Users and agents should be able to browse the same presets, constrain styles and energy, exclude unwanted material, reproduce a selection by seed, and avoid recently used presets. Preserve stable prompt IDs and provenance with each generated track. The app must eventually allow favorite, edited, and user-authored presets as well as built-ins.

The first standalone component now implements 32 authored palettes and 12 arrangement designs, composed into 384 stable presets, in `myradio/prompts/`. It exposes the same library through a Python API and JSON/JSONL CLI. Simple/layered/intricate writing is separate from sparse/balanced/lush texture. Instrumental prompts and supplied-lyrics vocal music both target YuE2; neither routes to Drama 3. See `docs/PROMPTS.md` for the contract and limitations. The focused host test suite passed 17 tests, including compilation of all 6,912 preset/complexity/density/mode combinations; no audio was rendered or evaluated.

### Easy scheduler

Provide a visual, timezone-aware schedule for recurring programs, research bulletins, entertainment, runners, and ads. Show conflicts and upcoming playback clearly. Let chat operate the same schedule instead of maintaining a separate agent-only plan.

### Continuous broadcast

Plan for variable home-rig inference speeds. Generate ahead into a playback buffer, surface generation failures, and retain ready-to-play music and spoken links when appropriate. YuE2 and Drama 3 serve distinct roles; neither is a substitute for the other's content. Failed generation must not interrupt already prepared playout.

### Output

Connect to an existing livestream or standard audio/broadcast destination. Select initial concrete output protocols after inspecting KnightshiftRadio and the current integration targets; do not claim an untested live connection.

### Agent and phone control

Expose documented, shared station operations that external agents can use: inspect status, change programming, manage research agents, edit schedules, and control broadcast playback, subject to user authorization.

A QR code or link may simplify pairing where an integration supports it. Phone access should primarily travel through the user's existing agent, not require a new myradio mobile app or publicly exposed admin panel. Use explicit pairing, scoped permissions, and revocable credentials; do not assume the named agents all support the same connector.

## Implementation gates

This standalone workspace now includes the prompt library, functional local studio and optional Icecast output described above. It is not a complete station-in-a-box release: there is no Git repository/remote yet, actual music/speech generation is not integrated, native installers are not implemented, and public deployment and cross-platform acceptance are unverified. The current provider-deadline regression must be resolved; old green test logs do not certify the latest source.

Before locking the stack and implementation plan:

1. Follow the inspected Imagination Studio mechanism: a local service shared by the web UI and an MCP adapter for existing agents. Reuse KnightshiftRadio's operational lessons without changing the live station.
2. Integrate the exact Hugging Face releases listed above through separate music and speech adapters. The cards describe optimized runtime/weight packages, not new fine-tunes. Verify licensing for the intended distribution and advertising-supported use, and measure inference on both target platforms.
3. Identify the Turbo environment and representative Turbo/Mac hardware. Treat both as first-class acceptance targets, not Mac as a later port.
4. Identify the first existing-agent connector and first broadcast output using actual available interfaces.
5. Build a thin end-to-end slice: configure a station, generate a segment, queue and play it through a real output, and control it through the same interface used by the UI and an agent.

Success is a fresh-install demonstration on both agreed targets: a user configures their interests, creates a research agent, schedules programming and ads, starts a continuous stream to a supported destination, and controls the station through an authorized existing agent. Test commands and performance budgets must be defined from the chosen implementation and actual target rigs, not guessed in advance.

## Implemented increment: optional Icecast output

An optional supervised 128 kbit/s MP3 source output now connects to Icecast with separate explicit go-live/stop controls, private credentials, dashboard health, approval-gated chat operations and shared MCP start/stop tools. Local Icecast 2.4.4 listener decoding and failure/reconnect behavior have been exercised; this is not bundled public hosting or a general platform certification. See README for operating instructions and reports/research-966e77beaf13afd2849ffa79c86fb72a-4.txt for this increment’s evidence and limitations. It does not complete the broader generation, agent, packaging and reliability release gates.

## Implemented increment: common-format audio import

The dashboard now converts MP3, FLAC, Ogg, AAC/M4A, AIFF and non-native WAV locally through FFmpeg into the persistent PCM library. Native PCM WAV import is still dependency-free. Upload/decoded-size limits, a conversion deadline, single HTTP import slot and cleanup guard conversion failures. MP3 browser import/playout and real codec round trips have been exercised; see RELEASE-STATUS.md for the measured evidence and unverified cases. Bulk import, tags/artwork, cancellation and embedded runtime setup remain open.

## Implemented increment: offline portable recovery

A portable backup/restore command now preserves identity, audio, queue, schedules, programs and history without carrying control/broadcast credentials. Integrity/schema/media validation and atomic no-clobber publication guard restoration into a new directory. Linux roundtrip, failure and unsafe-archive tests pass; dashboard workflow, macOS certification, power-loss/full-disk resilience and large-library scaling remain release gates. See README for safe usage.

## Implemented increment: private operator setup in dashboard

An operator can now save an OpenAI-compatible model connection directly from the chat window after privacy consent, with private key storage, environment override behavior, disconnect/forget and unchanged per-action approval. HTTP tests exercise a local test provider; Chromium tests cover setup, persistence, failure and fallback. Real provider/model quality, billing behavior and third-party MCP client certification remain open.
