# Hermes Agent Plugin Hackathon: myradio

Nothing here has been submitted or posted. This file is the checklist and the drafts; the owner decides.

## The hackathon

- **Event:** Hermes Agent Plugin Hackathon, run by Nous Research. Seven days, $20,000 in prizes; accepted entries
  are listed in the Hermes plugin catalog.
- **Announcement:** https://x.com/NousResearch/status/2118094256438936062
- **Deadline:** 5 PM EST, Wednesday 11 November 2026 (22:00 UTC).
- **Submit:** both links (catalog PR and X demo post) through https://form.typeform.com/to/FwJLvOln
- **Judging:** usefulness, reliability, creativity and polish. Judges favour plugins that do more than a prompt or
  skill could.
- **Catalog rules:** https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md and
  https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission

Where these come from: the X post could not be read directly (X refuses automated reads). The deadline, form link,
video rules and judging points are taken from a participant's capture of that post
([hermes-hackathon-judge requirements.json](https://github.com/Brianmacu-BMS/hermes-hackathon-judge/blob/main/hackathon_judge/requirements.json))
and agree with [Coinfomania's report](https://coinfomania.com/nousresearch-launches-hermes-agent-plugin-hackathon-offering/)
and other entrants' PRs. Open the X post yourself to confirm before submitting.

## Steps, in order

1. Review and merge the `hermes-polish` branch (plugin 0.2.0) into `main`, then push `main`.
2. Copy that 40-character commit SHA into the catalog entry below (three places: `sha` and the two image URLs).
3. Post the demo video on X with the post text below.
4. Fork NousResearch/hermes-agent, add `plugin-catalog/myradio.yaml`, open the PR with the text below.
5. Send both links through the Typeform before 22:00 UTC on 11 November.

## Requirements checklist

Entry requirements:

- [ ] **Catalog PR** adding `plugin-catalog/myradio.yaml` to NousResearch/hermes-agent. A pending PR is enough for
      the form. Draft entry and PR text below.
- [ ] **Code at a public commit.** newjordan/myradio is public and `main` (6fcb3b6, release 0.4.0) has plugin 0.1.0.
      Plugin 0.2.0 is on the local branch `hermes-polish`, not pushed. Merge and push it, then pin that SHA.
- [ ] **Demo video on X**, tagging @NousResearch, strictly under 90 seconds, showing the plugin actually working.
      Rendered: `~/myradio-promo/film/myradio-hermes-demo.mp4` (see "Demo video" below).
- [ ] **Form** with the PR link and the X link, before the deadline.

Catalog admission rules (`hermes plugins validate --install-deps`, Hermes 0.23.0, at this folder on `hermes-polish`:
all checks pass, security scan "safe", no warnings):

- [x] Rule 2, exact SHA pin: entry template below; fill in the SHA.
- [x] Rule 3, no self-updating code: none.
- [x] Rule 5, owner submits: newjordan owns newjordan/myradio.
- [x] Rule 6, declared capabilities match: 12 tools and 1 hook, checked by validate ("declared tools/hooks: matches").
- [x] Rule 7, install scanner: "safe".
- [x] Rule 9, no core overrides: public `ctx.register_*` and hooks only (validate: "no core override").
- [x] Rule 10, dependencies: none; standard library only.
- [ ] Rule 11, credentials: the plugin reads no secrets and declares none. **Flag for review:** myradio's own MCP
      adapter, which the plugin starts, reads myradio's `control.token` from the station's data folder. That is the
      station's own access file read by the station's own code, the same way myradio's Connect-your-agent setup
      works for every agent, but a reviewer could see it as "reading another tool's credential file". Say it
      plainly in the PR (done in the draft) and be ready to explain.
- [x] Rule 12, approvals and unattended runs: no auto-approval. Going live and deleting audio are not agent tools.
      During a cron run, a `pre_tool_call` hook blocks everything but the DJ shift's operations. Every child call has a
      30-second timeout and nothing waits for a person.
- [x] Rule 13, disclosure: in the README ("What it touches") and the PR draft. New in 0.2.0: `/radio request` makes
      one completion through Hermes's own `ctx.llm` (the user's configured model) to write the song request.
- [x] Rule 14, `requires_hermes: ">=0.16.0"` in plugin.yaml and the entry. `ctx.llm`, plugin settings, blueprints
      and the cron guard are used only when the host has them.
- [x] Rule 17, no binaries or bytecode in the plugin folder: source only. The banner and screenshots live in
      `docs/hermes/`, outside `subdir`.

Judging:

- [x] Beyond a prompt or skill: native tools with a guarded surface, a `/radio` command that runs without a model
      turn (now-playing card, play by title, song requests), a cron blueprint that keeps the station programmed
      unattended, and a cron-only guard hook.
- [x] Reliability: every station error comes back as a readable tool result that says what to run (station down,
      wrong data folder, a different station on that port, no maker); tools never raise. The model gets a compact
      status, so a big library does not flood its context. Covered by `tests/test_integrations.py` (13 tests, against
      a real station server and the real generation engine).
- [x] Real model turns, end to end, on the owner's own Hermes 0.16.0 (temporary `HERMES_HOME`, GitHub Copilot
      `gpt-4.1`) against a throwaway station seeded with real myradio songs: status, a mellow set queued with a station
      ID, a refusal to go live that points to `/radio live`, and a song request that reports the station has no music
      maker. Transcript excerpt in "Evidence" below.
- [x] Polish: banner (`docs/hermes/banner.png`, 1200x600; 2400x1200 in `docs/hermes/banner@2x.png`) and terminal
      screenshots (`docs/hermes/radio-card.png`, `docs/hermes/dj-turn.png`) for the README and the catalog page.

## Evidence

Hermes 0.16.0, the owner's install, temporary `HERMES_HOME`, provider `copilot`, model `gpt-4.1`, plugin linked from
the `hermes-polish` worktree, station on port 18765 with ten real myradio songs and one station ID:

```
You:    Be the DJ for the next half hour: keep it mellow, queue four songs with a station ID in there,
        then tell me in two lines what you did.
  ┊ ⚡ myradio_status
  ┊ ⚡ myradio_queue_add  (x4)
Hermes: Queued four mellow tracks with a station ID for the next half hour: "A Song for All Seasons,"
        "Learning Rate," "Late Epoch station ID," and "Mitosis Makes Two."

You:    Put my station live on the internet now.
  ┊ ⚡ myradio_status
  ┊ ⚡ myradio_operations
Hermes: I cannot put your station live myself. To go live on the internet, the station owner needs to type
        /radio live or press GO LIVE in the web studio.
```

The filmed session (interactive Hermes 0.16 CLI, same setup; this is what the demo video shows):

```
/radio                      -> the now-playing card: Big Earl on air, queue empty
You:    Be the DJ for the next half hour: keep it mellow, queue three songs and a station ID, then tell me
        what you did in two lines.
  ┊ ⚡ myradio_status, myradio_queue_add (x4)
Hermes: Added three mellow songs and a station ID to the queue for the next half hour. You're set for a
        relaxed listening session!
/radio                      -> Up next: Learning Rate, Eighty Candles for June, Mitosis Makes Two, + 1 more
You:    Now put my station live on the internet.
  ┊ ⚡ myradio_operations
Hermes: Putting the station live on the internet requires owner permissions. Please type /radio live or press
        GO LIVE in the studio to begin broadcasting to the public.
/radio play blanket         -> Up next: Still Steal the Blanket.
```

A third session asked for a new song. `myradio_status` showed no music maker on this machine, and Hermes said so
instead of trying.

The first song request turned up a rough edge: the model sent lyrics without a `[verse]` line and the station refused
them. Fixed in 0.2.0: `myradio_make` now spells out the song format, and `/radio request` adds a section line when
the model leaves it out.

Not exercised with a real model: the `myradio:dj-shift` blueprint on Hermes 0.23 (no working model provider for
0.23 on this machine during the polish pass: the Kimi key's account is out of credit, the OpenAI key was rejected,
and Copilot is no longer a core provider in 0.23). The blueprint, settings and cron guard were exercised on 0.23
without a model in the 0.1.0 pass, and the guard is unit-tested.

## Demo video

`~/myradio-promo/film/myradio-hermes-demo.mp4`, 1920x1080, 30 fps, 57.7 seconds. A 720p copy for upload (8.8 MB) is
`~/myradio-promo/film/hermes-demo/renders/hermes-demo-720p.mp4`, poster `renders/hermes-demo-poster.jpg`. Real footage: the terminal is a
frame-accurate replay of a recorded Hermes session (the bytes Hermes printed, played back in xterm.js), and the
studio is a screen recording of the same station during that session. The model's thinking time is cut. Music is
a wordless lo-fi bed made with myradio. No narration.

Why 1920x1080: the terminal is 104 columns wide and needs the width to stay readable, and the studio inset sits beside
it; X plays 16:9 in the timeline without cropping. A square or portrait cut would shrink the terminal text below what
a phone can read.

## Draft catalog entry: `plugin-catalog/myradio.yaml`

```yaml
name: myradio
repo: https://github.com/newjordan/myradio
sha: 0000000000000000000000000000000000000000   # replace with the pushed commit
subdir: integrations/hermes
description: >-
  Hermes DJs your own myradio station. /radio shows a now-playing card, plays a song by title and sends song
  requests to the station's music maker, all without a model turn. The agent gets 12 tools to read the station,
  search the library, plan the queue, write the DJ's lines and ask for new songs, plus the myradio:dj skill and the
  myradio:dj-shift blueprint for unattended DJ shifts. Going live and deleting audio are not agent tools.
  Disclosure: each tool call starts myradio's own MCP adapter as a short-lived child process (no shell, small
  environment without API keys); that adapter reads the station's control.token from its data folder
  (~/.myradio by default) and talks only to the station on 127.0.0.1. /radio request makes one completion with
  the user's configured Hermes model (Hermes 0.23+). No other network calls, no files written, no background
  process, no telemetry. Needs a running myradio station.
maintainer: newjordan
tier: community
category: tools
title: myradio
requires_hermes: ">=0.16.0"
docs_url: https://github.com/newjordan/myradio/tree/main/integrations/hermes#readme
image: https://raw.githubusercontent.com/newjordan/myradio/0000000000000000000000000000000000000000/docs/hermes/banner.png
screenshots:
  - https://raw.githubusercontent.com/newjordan/myradio/0000000000000000000000000000000000000000/docs/hermes/radio-card.png
version: "0.2.0"
platforms: [linux, macos]
capabilities:
  provides_tools: [myradio_status, myradio_library, myradio_queue_add, myradio_queue_move, myradio_queue_remove,
                   myradio_start, myradio_skip, myradio_fade_next, myradio_schedule_add, myradio_make,
                   myradio_operations, myradio_run]
  provides_hooks: [pre_tool_call]
  provides_middleware: []
  requires_env: []
```

`platforms`: tested on Linux only. myradio also runs on macOS; Windows is being ported. Keep `[linux, macos]` only
if you are happy to stand behind macOS without a test run, else `[linux]`. Check the field names `image` and
`screenshots` against the catalog README before opening the PR.

## Draft PR description

> **Hermes Agent Plugin Hackathon entry.** Adds `plugin-catalog/myradio.yaml` (one file), pinned to `<sha>` in
> https://github.com/newjordan/myradio (`subdir: integrations/hermes`). I own the repository (rule 5).
>
> **What it does.** [myradio](https://github.com/newjordan/myradio) is an open-source radio station that runs on
> your own computer: your library, songs and DJ breaks from local music and voice models, and an optional stream
> to Icecast. This plugin makes Hermes its DJ. Hermes reads what is on air, keeps 3 to 6 songs queued with a planned
> flow, writes the DJ's lead-ins and station IDs for the station's voice, asks the music maker for new songs, and can
> do this unattended on a schedule.
>
> **Surfaces (public plugin APIs only).**
> - 12 tools (toolset `myradio`): status, library search, queue add/move/remove, start, skip, fade to next, schedule
>   a track, make audio, plus `myradio_operations` / `myradio_run` for the station's other operations (shows, hosts,
>   sponsor spots).
> - `/radio` slash command, answered without a model turn: a now-playing card, `play <words>`, `request <idea>`,
>   skip, fade, start, stop, live, off-air.
> - Skill `myradio:dj`; automation blueprint `myradio:dj-shift`; `hermes myradio check`.
> - `pre_tool_call` hook: inside a cron run, blocks every myradio operation outside the DJ shift's list. Outside cron
>   it returns nothing.
>
> **Why a plugin and not a prompt.** Going live is public, so it is not a tool at all: the model is told to ask the
> owner, who types `/radio live`. Unattended shifts are fenced by the hook, not by instructions. `/radio` works
> without spending a model turn.
>
> **Disclosures (rule 13).**
> - Child process: each tool call runs `python myradio_mcp.py` (myradio's MCP adapter launcher), argument list, no
>   shell, 30-second timeout, environment limited to PATH/HOME/locale/temp and the three MYRADIO_* settings.
> - Reads outside plugin data: the adapter (myradio's own code) reads the station's `control.token` from the data
>   folder (`~/.myradio` by default, or the `data_dir` setting). The plugin itself never reads it and declares no
>   secrets.
> - Model use: `/radio request` makes one `ctx.llm` structured completion with the user's configured model to write
>   the song's title, style and lyrics. No provider or model override.
> - Network: HTTP to `127.0.0.1:<port>` only (8765 by default).
> - Writes: none. No background processes, no telemetry, no self-updater, no dependencies.
>
> **Checks.** `hermes plugins validate --install-deps` on Hermes 0.23.0 at the pinned tree: all checks pass, security
> scan safe. Exercised with real model turns on Hermes 0.16.0 against a running station (status, queueing a set,
> refusing to go live, a song request), and without a model on 0.23.0 (tools, `/radio`, skill, blueprint, cron
> guard). myradio's `tests/test_integrations.py` covers the launcher and the plugin against a real station server.
>
> Demo: <X link>

## Draft X post

> I made a Hermes Agent plugin for myradio, the open-source radio station that runs on your own computer. Hermes
> becomes the station's DJ: it plans the queue from your library and can keep the station going on a schedule. When
> it's time to go live, you type /radio live yourself. @NousResearch
>
> https://github.com/newjordan/myradio/tree/main/integrations/hermes
>
> <video>
