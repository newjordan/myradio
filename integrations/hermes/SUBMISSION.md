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

## Requirements checklist

Entry requirements:

- [ ] **Catalog PR** adding `plugin-catalog/myradio.yaml` to NousResearch/hermes-agent. A pending PR is enough for
      the form. Draft entry and PR text below. Needs a commit SHA on newjordan/myradio first (see next item).
- [ ] **Code at a public commit.** newjordan/myradio is public. This plugin is on the local branch `agent-plugins`,
      not pushed. Push it (or merge it to main), then pin that 40-character SHA in the entry.
- [ ] **Demo video on X**, tagging @NousResearch, strictly under 90 seconds, showing the plugin actually working.
      Script below.
- [ ] **Form** with the PR link and the X link, before the deadline.

Catalog admission rules (`hermes plugins validate --install-deps`, Hermes 0.23.0, at this folder: 15 of 15 checks
pass, security scan "safe", no warnings):

- [x] Rule 2, exact SHA pin: entry template below; fill in the SHA.
- [x] Rule 3, no self-updating code: none.
- [x] Rule 5, owner submits: newjordan owns newjordan/myradio.
- [x] Rule 6, declared capabilities match: 11 tools and 1 hook, checked by validate ("declared tools/hooks: matches").
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
- [x] Rule 13, disclosure: in the README ("What it touches") and the PR draft.
- [x] Rule 14, `requires_hermes: ">=0.16.0"` in plugin.yaml and the entry. Loaded and exercised on 0.16.0 and 0.23.0.
- [x] Rule 17, no binaries or bytecode: source only (do not commit `__pycache__`).

Judging:

- [x] Beyond a prompt or skill: native tools with a guarded surface, a `/radio` command that runs without a model
      turn, a cron blueprint that keeps the station programmed unattended, and a cron-only guard hook.
- [x] Reliability: every station error comes back as a readable tool result (station down, no token, bad track id);
      tools never raise. Covered by `tests/test_integrations.py` in myradio.
- [ ] Polish: add a banner image (1200x600) and screenshots of `/radio` and a DJ shift for the catalog page
      (optional fields `image`, `screenshots`).

## Demo video plan (under 90 seconds)

1. (0-10 s) The myradio studio playing; one line on screen: "Hermes is the DJ."
2. (10-25 s) Terminal: `hermes plugins enable myradio`, then `/radio`: what is on air, the queue.
3. (25-50 s) Ask Hermes: "Be the DJ for the next hour, keep it mellow, station ID every 15 minutes." Show it calling
   `myradio_status`, queueing songs and writing a lead-in with `myradio_make`; the studio's queue updates and the
   DJ voice speaks the line.
4. (50-70 s) `/blueprint myradio:dj-shift`, every 10 minutes. Cut to a later shift's one-line report.
5. (70-85 s) Ask it to go live: it refuses and points to `/radio live`. Type `/radio live`; GO LIVE lights up.

Record with a real station and a real model. The DJ voice needs a speech maker set up in myradio.

## Draft catalog entry: `plugin-catalog/myradio.yaml`

```yaml
name: myradio
repo: https://github.com/newjordan/myradio
sha: 0000000000000000000000000000000000000000   # replace with the pushed commit
subdir: integrations/hermes
description: >-
  Hermes DJs your own myradio station: plans the queue, writes the DJ's lines for the station's voice, and runs
  unattended DJ shifts from a cron blueprint. Adds 11 tools, /radio (status, skip, start, stop, live, off-air),
  the myradio:dj skill and the myradio:dj-shift blueprint. Going live and deleting audio are not agent tools.
  Disclosure: each tool call starts myradio's own MCP adapter as a short-lived child process (no shell, small
  environment without API keys); that adapter reads the station's control.token from its data folder
  (~/.myradio by default) and talks only to the station on 127.0.0.1. No other network calls, no files written,
  no background process, no telemetry. Needs a running myradio station.
maintainer: newjordan
tier: community
category: tools
title: myradio
requires_hermes: ">=0.16.0"
docs_url: https://github.com/newjordan/myradio/tree/main/integrations/hermes#readme
version: "0.1.0"
platforms: [linux, macos]
capabilities:
  provides_tools: [myradio_status, myradio_queue_add, myradio_queue_move, myradio_queue_remove, myradio_start,
                   myradio_skip, myradio_fade_next, myradio_schedule_add, myradio_make, myradio_operations, myradio_run]
  provides_hooks: [pre_tool_call]
  provides_middleware: []
  requires_env: []
```

`platforms`: tested on Linux only. myradio also runs on macOS; Windows is being ported. Keep `[linux, macos]` only
if you are happy to stand behind macOS without a test run, else `[linux]`.

## Draft PR description

> **Hermes Agent Plugin Hackathon entry.** Adds `plugin-catalog/myradio.yaml` (one file), pinned to `<sha>` in
> https://github.com/newjordan/myradio (`subdir: integrations/hermes`). I own the repository (rule 5).
>
> **What it does.** [myradio](https://github.com/newjordan/myradio) is an open-source radio station that runs on
> your own computer: your library, songs and DJ breaks from local music and voice models, and an optional stream
> to Icecast. This plugin makes Hermes its DJ. Hermes reads what is on air, keeps 3 to 6 songs queued with a planned
> flow, writes the DJ's lead-ins and station IDs and has the station's voice speak them, and can do this unattended
> on a schedule.
>
> **Surfaces (public plugin APIs only).**
> - 11 tools (toolset `myradio`): status, queue add/move/remove, start, skip, fade to next, schedule a track, make
>   audio, plus `myradio_operations` / `myradio_run` for the station's other operations (shows, hosts, sponsor spots).
> - `/radio` slash command: status, skip, start, stop, live, off-air, answered without a model turn.
> - Skill `myradio:dj`; automation blueprint `myradio:dj-shift`; `hermes myradio check`.
> - `pre_tool_call` hook: inside a cron run, blocks every myradio operation outside the DJ shift's list. Outside cron
>   it returns nothing.
>
> **Why a plugin and not a prompt.** Going live is public, so it is not a tool at all: the model is told to ask the
> owner, who types `/radio live`. Unattended shifts are fenced by the hook, not by instructions.
>
> **Disclosures (rule 13).**
> - Child process: each tool call runs `python myradio_mcp.py` (myradio's MCP adapter launcher), argument list, no
>   shell, 30-second timeout, environment limited to PATH/HOME/locale/temp and the three MYRADIO_* settings.
> - Reads outside plugin data: the adapter (myradio's own code) reads the station's `control.token` from the data
>   folder (`~/.myradio` by default, or the `data_dir` setting). The plugin itself never reads it and declares no
>   secrets.
> - Network: HTTP to `127.0.0.1:<port>` only (8765 by default).
> - Writes: none. No background processes, no telemetry, no self-updater, no dependencies.
>
> **Checks.** `hermes plugins validate --install-deps --json` on Hermes 0.23.0 at the pinned tree: 15 of 15 checks,
> security scan safe, no warnings. `hermes plugins doctor --ci`: OK, 11 tools, 1 hook. Exercised end to end against a
> running station in a temporary `HERMES_HOME` on 0.23.0 (tools, `/radio`, skill, blueprint, cron guard) and on
> 0.16.0 (tools, `/radio`, skill). myradio's `tests/test_integrations.py` covers the launcher and the plugin's station
> client against a real station server.
>
> Demo: <X link>

## Draft X post

> Hermes is the DJ now. myradio is an open-source radio station that runs on your own machine; this Hermes
> plugin lets the agent plan the queue, write the DJ's lines and keep the station on air, even on a schedule.
> Going live stays your call: /radio live. @NousResearch
> <video, under 90 s> https://github.com/newjordan/myradio
