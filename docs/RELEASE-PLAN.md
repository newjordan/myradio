# myradio open-source release plan (draft, 2026-11-06)

**What we are releasing:** myradio 0.4, an open-source radio station that makes its own music on your own computer, with
your own agent as the DJ. The models are already public on Hugging Face; this release adds the station around them.

**Order of priority:** the easy installer and first run come first. Everything else in this plan supports a newcomer getting
from download to their own station playing its first song in a few obvious steps.

## The proof we launch with

| Run | Machine | Result |
| --- | --- | --- |
| [Skatepark Radio](runs/2026-11-06-mac-mini-hour.md), pop punk and hard rock | Mac mini, Apple M4 Pro, Metal | One hour on air, 25 fresh songs aired back to back, 60.6 min of music made in 58.8 min, 0 failed renders, agent DJ |
| Late Epoch, lofi study beats about fine-tuning | apollo, RTX 4080, CUDA | Running now (2026-11-06, 12:19 to 13:19); receipt follows |
| Published model numbers | RTX 4080, Intel Arc Pro B70, M4 Pro | See [RESEARCH.md](RESEARCH.md) and the model cards |

Both runs used the real path a newcomer takes: install myradio, download the model from Hugging Face (checksums verified),
hook it up, let an agent name the station and ask for songs, press Start. The recordings (terminal casts, studio films and a
clip of every song as it aired) are the raw material for the launch films.

## The newcomer's path, and what each step needs

| Step | Today | For the release |
| --- | --- | --- |
| 1. Install myradio | `git clone` + `uv venv` (no admin rights; done on both machines in seconds) | One command, later a Mac app |
| 2. Download the music model | Done by hand: Hugging Face download + `SHA256SUMS` check | Built into setup: progress bar, resume, checksum |
| 3. Get the music runtime for this machine | Linux + NVIDIA: a ready CUDA build is published (`apollo/runtime/full-song/audiocpp_cli` in YuE2-Turbo-Radio). Mac: built locally. Intel Arc: built locally | Publish a Mac (Metal) build next to the CUDA one so setup can download it; Intel Arc next |
| 4. Hook up | Done by hand: one settings file per machine (runtime, model files, the published speed settings) | Setup writes it from a per-platform table and runs one test song |
| 5. Pick a station style, first songs | The walk-through in the preview (Menu → Set up your station); agents do it today through Connect your agent | The real first-run flow in the studio |
| 6. DJ | Connect your agent works today (MCP, one Copy) | ElevenLabs voice adapter (recommended voice), local Drama 3 after |

## Checklist

### Must have before going public

- [ ] **`myradio setup`**: detect the machine (Apple silicon, NVIDIA, Intel Arc), download model and runtime with progress,
      resume and checksums, write the music maker settings, make one test song. The two runs today are its script.
- [ ] **Mac runtime published** (Metal `audiocpp_cli` with its SHA-256) so the Mac path needs no compiler.
- [ ] **Real first-run flow in the studio**, following the walk-through: model, hook-up, one prompt, first song, on air,
      then customize.
- [ ] **Security fixes:** apply the 15 medium findings (tested patches in `reports/security-review-*.md`), first the status
      poll that copies the library under the playout lock.
- [ ] **Engine fixes from the soak run:** F3, F5, F9, F10 (`reports/soak-findings.md`); remove each `expectedFailure` as it lands.
- [ ] **LICENSE** for myradio's code (your choice), plus a plain note on the model's CC BY-NC 4.0 terms: free to share the
      songs, not to sell them.
- [ ] **Public README:** what it is, quick start, the machines it has run on with links to the receipts, Connect your agent,
      Research: help wanted.
- [ ] **Version 0.4.0**, CHANGELOG, a wheel, and a clean install verified on the Mac mini and on apollo.
- [ ] **Repository tidy for public eyes:** `docs/HANDOFF.md` links to private repositories (knightshiftradio and the runtime
      repos); the internal lane notes (`docs/lanes/`) could move under `docs/dev/`; the top of `docs/RELEASE-STATUS.md`
      predates the provider-deadline fix and should be replaced by a current summary.
- [ ] **SECURITY.md** (how to report), **CONTRIBUTING.md** (the receipts house rule from RESEARCH.md), issue templates.
      No GitHub Actions.

### Should have

- [ ] ElevenLabs DJ voice adapter.
- [ ] Faster first song with the streaming runtime (first audio in seconds instead of after the whole render).
- [ ] Station presets from the runs: Skatepark Radio and Late Epoch as ready-made example stations (styles plus songs).
- [ ] A Music maker sheet in the studio menu (status, test song, recent renders), so the music maker is visible without an agent.

### Launch assets

- [ ] Promo film, 60 to 75 s (storyboard in [promo/PLAN.md](promo/PLAN.md)): two stations, two machines, real footage and
      real songs; a night-time radio room as the background art.
- [ ] Instruction video in chapters, filmed on the real installer once it exists.
- [ ] Launch post, pinned research call, links from both model cards.

## Checked today (2026-11-06)

- **Secrets:** none in the git history. The only key-shaped strings are test fixtures (a test certificate key and dummy
  `secret` passwords in tests).
- **Size:** largest file in history is a 452 KB screenshot; no build output, wheels or reports are tracked (`reports/` stays
  ignored because run logs can hold launch codes).
- **Personal details in code:** none; the IP addresses in code are the safe fetcher's blocklists and test cases. Docs mention
  local paths and private repository links (listed above).
- **Wording fixed:** the studio footer, the Autopilot pad and the server's start-up message said music generation was not
  available; they now say songs are made once a music maker is set up.

## Decisions for you

1. License for myradio's code (Apache-2.0 matches your engine patches; MIT is the other common choice).
2. Version number for the launch (0.4.0 proposed).
3. When to go public: after `myradio setup` and the first-run flow, or earlier as a developer preview with manual setup steps.
4. Launch platforms: Mac and Linux with NVIDIA first, Intel Arc next?
5. Mac runtime: publish a Metal build on Hugging Face next to the CUDA one.
6. Narrator for the instruction video: your voice, an ElevenLabs voice, or captions only.
