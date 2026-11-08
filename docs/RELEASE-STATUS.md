# Release status

**Current release: myradio 0.4.0 (2026-11-07), the first public release.** What is in it is listed in
[CHANGELOG.md](../CHANGELOG.md).

- **Install:** `curl -fsSL https://myradio.garden/install.sh | sh`, then `myradio`. Works on a Mac with Apple silicon, Linux
  with an NVIDIA graphics card, and the NVIDIA DGX Spark. Windows runs the studio with your own audio; making songs on
  Windows is still being tested.
- **Checked before release:** the full test suite passes on Linux; an hour on air on an RTX 4080, a DGX Spark and a Mac mini
  ([docs/runs](runs/)); the installer from nothing on a Mac, an RTX 4080 and a DGX Spark.
- **Research:** [the paper](research/benchmarks-2026-11-06/PAPER.md) and [RESEARCH.md](RESEARCH.md).
- **Known gaps:** the studio's first-run screens are not yet wired to the installer's progress; the installer does not yet
  download DJ voices; song-making on Windows is in testing.

Notes from the 0.3 development previews are in [docs/dev/](dev/).
