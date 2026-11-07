# myradio launch: the full working version (started 2026-11-06)

The user's call (2026-11-06): **no preview release**. "We launch the most complete working version of this thing we can put
together", licensed **Apache-2.0**, with a final **smoke test on a rented rig** (a fresh cloud GPU machine) once everything
is finished. Work happens on branch `spark-dual-loader` until it merges to `main`.

Each item lists what "done" means. Tick it only when that is true and tested.

| # | Item | Done when | Status |
| --- | --- | --- | --- |
| 1 | License | `LICENSE` (Apache-2.0, official text), `NOTICE` (models and runtimes keep their own licenses), package metadata | **done** (`7637785`) |
| 2 | Security fixes | the 15 medium findings in `reports/security-review-*.md` applied with their tests, first the status poll that copies the library under the playout lock | open |
| 3 | Engine defects from the soak run | F3, F5, F9, F10 fixed (`reports/soak-findings.md`), their `expectedFailure` markers removed | open |
| 4 | `myradio setup` | one command: detects the machine (Apple silicon, NVIDIA, DGX Spark), downloads the music model, the runtime and the chosen voice with progress, resume and checksums, writes the generation config (the dual loader where it fits), makes one test song; the hand setups in `docs/runs/` are its script | **music path working** (2026-11-06): `install.sh` + `myradio setup` installed from nothing and made a test song on the Mac mini (Metal, 22.0 s) and the RTX 4080 (CUDA from PyPI libraries, no toolkit, 13.4 s); resumable, checksummed downloads; Spark source build under test; DJ voice download and the studio progress wiring still to do |
| 5 | Runtimes for every platform | Linux + NVIDIA: the published CUDA build (x86-64) or a build from the published source (aarch64/Spark, as on toymaker); Mac: a published Metal build (the user's to publish) or a source build | Linux x86-64: published CUDA binary + NVIDIA PyPI libraries; Mac: Metal binary published 2026-11-06 at `Frosty40/YuE2-Turbo-Radio/mac/runtime/audiocpp_cli` (sha256 8e442b2e…); Spark: source build; Windows: being tested by a contributor |
| 6 | Real first run in the studio | the quest walk-through from the preview, wired to `myradio setup`'s progress: models, hook-up, one prompt, first song on air | open |
| 7 | DJ voice default | Jessica (Qwen3-TTS CustomVoice) as the local default through `speech_http`; an ElevenLabs adapter for the cloud option; the agent writes the copy | open |
| 8 | Release packaging | version 0.4.0, CHANGELOG, public README (quick start, the three receipts, Connect your agent, research help), docs tidied (no links to private repos), wheel built and installed outside the checkout | open |
| 9 | Clean installs | `myradio setup` from nothing on the Mac mini and on a Linux NVIDIA box, first song on air | open |
| 10 | Merge and push | branch merged to `main`, pushed; the repository made public (the user confirms the moment) | open |
| 11 | **Smoke test on a rented rig** | a fresh rented GPU machine (the user picks the provider and approves the cost): install, setup, first song, an hour on air with the agent DJ, receipt written | open, last |
| 12 | Launch film | "Game Night, Sung Live" (`~/myradio-promo/film/STORYLINES.md`) with Omnispace Technologies branding and the NVIDIA Inception badge on the end card | hero film built; Frank's OK given. **"NOW THIS IS MyRadio"** ad (52.6 s, 90s compilation-album parody, Blender title and jewel case, real songs, ElevenLabs "Guy" announcer, Drama 3 station DJ ID, VHS pass, -14 LUFS) rendered 2026-11-06 to `~/myradio-promo/film/now/now-this-is-myradio.mp4` (clean cut and an all-local Drama 3 announcer version beside it); awaiting the user's review |

## Notes

- Plan usage was at 98% of the weekly limit on 2026-11-06 (resets 2026-11-09); the large items (4, 6, 7) are planned
  for after the reset.
- The live station (`~/turbo`) and its B70 stay off limits; build and test on toymaker, apollo and the Mac mini.
- Never push or make the repository public without the user's explicit go-ahead at that moment; no GitHub Actions.
