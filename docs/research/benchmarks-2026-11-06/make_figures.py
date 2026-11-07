#!/usr/bin/env python3
"""Rebuild the paper's figures and numerical ledger from the run data.

Run from this directory: ./venv/bin/python make_figures.py
No network access or additional packages are needed beyond matplotlib and numpy.
"""

from pathlib import Path
import json
import os
import re
import tempfile

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "myradio-paper-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import MultipleLocator
import numpy as np


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "figures"
MACHINES = ("mac-mini", "apollo", "toymaker")
LABELS = {"mac-mini": "Mac mini", "apollo": "RTX 4080", "toymaker": "DGX Spark"}
COLORS = {"mac-mini": "#2878B5", "apollo": "#D47816", "toymaker": "#25866F"}
RECEIPTS = {
    "mac-mini": "2026-11-06-mac-mini-hour.md",
    "apollo": "2026-11-06-apollo-hour.md",
    "toymaker": "2026-11-06-toymaker-dual-loader-hour.md",
}
VOICE_IDS = {
    "Chatterbox Turbo": "chatterbox-turbo", "Chatterbox": "chatterbox",
    "VoxCPM2": "voxcpm2", "Kokoro 82M": "kokoro",
    "Qwen3-TTS CustomVoice": "qwen3-customvoice",
    "Qwen3-TTS VoiceDesign": "qwen3-voicedesign", "VibeVoice 1.5B": "vibevoice",
    "Drama 3 B70 Turbo": "drama3", "Maya1": "maya1", "IndexTTS2": "index-tts2",
}


def read_all():
    """Parse every data file before producing any output."""
    files = {}
    for p in sorted(DATA.rglob("*")):
        if not p.is_file():
            continue
        key = p.relative_to(DATA).as_posix()
        raw = p.read_text()
        if p.suffix == ".jsonl":
            value = [json.loads(line) for line in raw.splitlines() if line.strip()]
        elif p.suffix == ".json":
            value = json.loads(raw)
        else:
            value = raw
        files[key] = {"raw": raw, "value": value}
    return files


def extract(pattern, text, cast=float):
    match = re.search(pattern, text)
    if not match:
        raise ValueError(f"Receipt field not found: {pattern}")
    return cast(match.group(1).replace(",", ""))


def receipt_values(text):
    clean = text.replace("**", "")
    return {
        "seconds": extract(r"([\d,]+) s on air", clean, int),
        "songs_aired": extract(r"(\d+) songs aired", clean, int),
        "checks": extract(r"(\d+) status checks", clean, int),
        "renders": extract(r"(\d+) renders, 0 failures", clean, int),
        "audio_min": extract(r"([\d.]+) min(?:utes)? of music made", clean),
        "render_min": extract(r"music made in ([\d.]+) min", clean),
        "overall": extract(r"([\d.]+)× realtime", clean),
        "median": extract(r"median ([\d.]+)×", clean),
    }


def get_runs(files):
    runs = {}
    for machine in MACHINES:
        raw_renders = files[f"{machine}/renders.json"]["value"]
        report = files[f"{machine}/hour-run-report.json"]["value"]
        logs = files[f"{machine}/hour-run-log.jsonl"]["value"]
        receipt = files[RECEIPTS[machine]]["raw"]
        pub = receipt_values(receipt)
        songs = [r for r in raw_renders if r.get("role", "song") == "song"]
        speech = [r for r in raw_renders if r.get("role", "song") != "song"]
        start = next(r["t"] for r in logs if r["kind"] == "on_air")
        ticks = [(i, r) for i, r in enumerate(logs, 1) if r["kind"] == "tick"]
        aired = [(i, r) for i, r in enumerate(logs, 1) if r["kind"] == "now_playing"]
        requests = [(i, r) for i, r in enumerate(logs, 1) if r["kind"] == "dj_request"]
        pairs = []
        for req_line, req in requests:
            candidates = [(i, r) for i, r in aired
                          if r["title"] == req["title"] and r["t"] >= req["t"]]
            if candidates:
                air_line, air = candidates[0]
                pairs.append({"title": req["title"], "request_line": req_line,
                              "air_line": air_line, "request_t": req["t"],
                              "air_t": air["t"], "seconds": air["t"] - req["t"]})
        a = np.array([r["audio_s"] for r in songs])
        w = np.array([r["render_s"] for r in songs])
        # The receipts reproduce the stored two-decimal ratios. Recompute the
        # quotient for the audit, but plot the published per-song values.
        speeds = np.array([r["realtime_x"] for r in songs])
        for r in songs:
            assert re.search(rf"\| {re.escape(r['title'])} \|.*\| {r['realtime_x']:.2f}× \|", receipt)
        assert len(raw_renders) == pub["renders"] == report["jobs"]["succeeded"]
        assert len(ticks) == pub["checks"]
        assert report["seconds"] == pub["seconds"]
        assert round(float(a.sum() / 60), 1) == pub["audio_min"]
        assert round(float(w.sum() / 60), 1) == pub["render_min"]
        assert round(float(a.sum() / w.sum()), 2) == pub["overall"]
        assert round(float(np.median(a / w)), 2) == pub["median"]
        assert report["jobs"]["failed"] == 0
        assert all(r["running"] for _, r in ticks)
        assert len(aired) == len(report["played"])
        assert all(x["title"] == y["title"] and x["minute"] == y["minute"]
                   for (_, x), y in zip(aired, report["played"]))
        music_air = [(i, r) for i, r in aired if r.get("track_kind", "music") == "music"]
        assert len(music_air) == pub["songs_aired"]
        runs[machine] = dict(pub=pub, songs=songs, speech=speech, report=report,
                             logs=logs, ticks=ticks, aired=aired, start=start,
                             requests=requests, pairs=pairs, a=a, w=w, speeds=speeds,
                             fit=np.polyfit(a, w, 1))
    return runs


def get_voices(files):
    successful = {}
    for path, entry in files.items():
        if path.startswith("voices/"):
            for index, r in enumerate(entry["value"]["voices"]):
                if r.get("line") == "intro" and "error" not in r:
                    assert r["music_rendering"]
                    # Earlier attempts include task errors; the successful retry
                    # is retained, with its exact source pointer.
                    successful[r["model"]] = (path, index, r)
    voices = []
    for line, text in enumerate(files["dj-voices.md"]["raw"].splitlines(), 1):
        cells = [s.strip() for s in text.strip("|").split("|")]
        if cells[0] not in VOICE_IDS:
            continue
        label, speed, size, licence = cells[:4]
        path, index, raw = successful[VOICE_IDS[label]]
        published = float(speed.replace("×", ""))
        assert raw["realtime_x"] == published
        kind = "MIT" if licence.startswith("MIT") else (
            "Apache-2.0" if licence.startswith("Apache") else "Conditional")
        voices.append(dict(label=label, speed=published, size=float(size.split()[0]),
                           licence=licence, kind=kind, path=path, index=index,
                           line=line, raw=raw))
    assert len(voices) == len(VOICE_IDS)
    return voices


def save(fig, name):
    fig.savefig(OUT / f"{name}.png", dpi=200, facecolor="white")
    fig.savefig(OUT / f"{name}.svg", facecolor="white")
    plt.close(fig)


def style():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 13,
        "axes.titlesize": 15, "axes.titleweight": "bold",
        "axes.labelsize": 13, "xtick.labelsize": 12, "ytick.labelsize": 12,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#ABB2B8", "text.color": "#222222",
        "axes.labelcolor": "#222222", "xtick.color": "#333333",
        "ytick.color": "#333333", "figure.facecolor": "white",
        "axes.facecolor": "white", "svg.fonttype": "none",
    })


def figures(runs, voices, dual):
    style()
    fig, ax = plt.subplots(figsize=(11, 4.6), layout="constrained")
    for i, m in enumerate(MACHINES):
        run = runs[m]
        # Deterministic jitter keeps all song observations visible.
        jitter = np.random.default_rng(i).uniform(-0.15, 0.15, len(run["songs"]))
        ax.scatter(run["speeds"], i + jitter, color=COLORS[m], s=45, alpha=.75)
        median = run["pub"]["median"]
        ax.plot([median, median], [i-.25, i+.25], color=COLORS[m], lw=3)
        ax.text(median+.12, i-.27, f"median {median:.2f}×", color=COLORS[m], fontsize=13)
    ax.axvline(1, color="#666666", lw=1, ls="--", zorder=0)
    ax.text(1.03, 2.48, "playback speed", color="#555555", fontsize=11)
    ax.set(yticks=range(len(MACHINES)), yticklabels=[LABELS[m] for m in MACHINES],
           ylim=(-.6, 2.7), xlim=(.7, 4.25), xlabel="Realtime factor (audio seconds / render second, ×)",
           title="Median song speed was above playback speed on each machine")
    ax.invert_yaxis()
    save(fig, "01_song_speed")

    fig, axes = plt.subplots(3, 2, figsize=(13.2, 8.6), sharex=True, layout="constrained")
    fig.suptitle("Song starts continued throughout each run as queue depth changed", fontsize=18, weight="bold")
    for row, m in enumerate(MACHINES):
        run = runs[m]
        left, right = axes[row]
        for kind, ls, label in [("music", "-", "Song starts"), ("speech", "--", "DJ clip starts")]:
            events = [r for _, r in run["aired"] if r.get("track_kind", "music") == kind]
            if not events:
                continue
            ts = [(r["t"]-run["start"])/60 for r in events]
            left.step([0, *ts, run["pub"]["seconds"]/60],
                      [0, *range(1, len(ts)+1), len(ts)], where="post",
                      color=COLORS[m], ls=ls, lw=2.3, label=label)
        left.set(ylabel=f"{LABELS[m]}\nObserved starts (count)", ylim=(0, 27))
        left.yaxis.set_major_locator(MultipleLocator(10))
        ts = [(r["t"]-run["start"])/60 for _, r in run["ticks"]]
        depths = [len(r["queue"]) for _, r in run["ticks"]]
        right.step(ts, depths, where="post", color=COLORS[m], lw=1.8)
        right.set(ylabel="Queued entries (count)", ylim=(0, 10))
        right.yaxis.set_major_locator(MultipleLocator(3))
        for ax in (left, right):
            ax.set_xlim(0, 61)
            ax.grid(axis="y", color="#ECEEEF", lw=.8)
        if row == 0:
            left.set_title("Observed airplay")
            right.set_title("Queue depth at status checks")
        if row == 2:
            left.legend(loc="upper left", frameon=False, fontsize=11)
    for ax in axes[-1]:
        ax.set_xlabel("Time since Start (min)")
    save(fig, "02_run_timeline")

    fig, ax = plt.subplots(figsize=(10, 6), layout="constrained")
    for m in MACHINES:
        run = runs[m]
        ax.scatter(run["a"], run["w"], s=40, color=COLORS[m], alpha=.72)
        xs = np.linspace(run["a"].min(), run["a"].max(), 100)
        ax.plot(xs, np.polyval(run["fit"], xs), color=COLORS[m], lw=2.2, label=LABELS[m])
    ax.set(xlabel="Song length (audio s)", ylabel="Render wall time (s)",
           title="Longer songs took more time to render within each run", ylim=(0, None))
    ax.legend(frameon=False)
    save(fig, "03_length_and_render")

    fig, axes = plt.subplots(1, 2, figsize=(12.4, 5.2), layout="constrained", gridspec_kw={"width_ratios": [1.65, 1]})
    ax, air = axes
    x = np.arange(len(MACHINES))
    for i, m in enumerate(MACHINES):
        pub = runs[m]["pub"]
        for dx, key, hatch in [(-.18, "audio_min", None), (.18, "render_min", "///")]:
            rect = ax.bar(i+dx, pub[key], .32, color=COLORS[m], edgecolor="white", hatch=hatch)
            ax.bar_label(rect, labels=[f"{pub[key]:.1f}"], padding=4, fontsize=12)
        rect = air.bar(i, pub["seconds"], .55, color=COLORS[m])
        air.bar_label(rect, labels=[f"{pub['seconds']:,}"], padding=4, fontsize=12)
    ax.set(xticks=x, xticklabels=[LABELS[m] for m in MACHINES], ylim=(0, 83),
           ylabel="Duration (min)", title="Music output exceeded render time")
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, facecolor="#888888", label="Music made"),
                       plt.Rectangle((0, 0), 1, 1, facecolor="#888888", edgecolor="white", hatch="///", label="Render time")],
              frameon=False, fontsize=11, ncols=2, loc="upper left")
    air.set(xticks=x, xticklabels=[LABELS[m] for m in MACHINES], ylim=(0, 4100),
            ylabel="On-air interval (s)", title="Each station stayed on for an hour")
    save(fig, "04_output_and_airtime")

    fig, ax = plt.subplots(figsize=(9.8, 5.2), layout="constrained")
    published = dual["published"]
    for j, (condition, hatch) in enumerate([("alone", None), ("together", "///")]):
        values = [published[f"music_{condition}"], published[f"speech_{condition}"]]
        rect = ax.bar(np.arange(2) + (j-.5)*.34, values, .31,
                      color=COLORS["toymaker"], edgecolor="white", hatch=hatch,
                      alpha=1 if j == 0 else .65, label="Alone" if j == 0 else "Together")
        ax.bar_label(rect, labels=[f"{v:.2f}×" for v in values], padding=5)
    ax.axhline(1, color="#666666", lw=1, ls="--", zorder=0)
    ax.set(xticks=[0, 1], xticklabels=["YuE2 music", "Drama 3 speech"], ylim=(0, 2.85),
           ylabel="Realtime factor (audio s / wall s, ×)",
           title="With the DJ voice rendering beside it, music stayed above playback speed")
    ax.legend(frameon=False, ncols=2)
    save(fig, "05_dual_loader")

    fig, ax = plt.subplots(figsize=(12.4, 7.5), layout="constrained")
    license_colors = {"MIT": "#5269B5", "Apache-2.0": "#248878", "Conditional": "#B2604E"}
    offsets = {
        "Chatterbox Turbo": (-12, 35), "Chatterbox": (15, 18),
        "VoxCPM2": (15, 14), "Kokoro 82M": (12, -6),
        "Qwen3-TTS CustomVoice": (-18, 30), "Qwen3-TTS VoiceDesign": (-18, -14),
        "VibeVoice 1.5B": (18, -6), "Drama 3 B70 Turbo": (-12, 10),
        "Maya1": (14, -5), "IndexTTS2": (-14, -23),
    }
    for v in voices:
        dx, dy = offsets[v["label"]]
        ax.scatter(v["size"], v["speed"], s=80, color=license_colors[v["kind"]], zorder=3)
        ax.annotate(v["label"], (v["size"], v["speed"]), xytext=(dx, dy),
                    textcoords="offset points", ha="left" if dx > 0 else "right", fontsize=12,
                    arrowprops=dict(arrowstyle="-", lw=.7, color="#777777"))
    ax.set(xscale="log", yscale="log", xlim=(.14, 33), ylim=(.14, 18),
           xlabel="Reported model size (GB; log scale)",
           ylabel="Lead-in speed beside music (audio s / wall s, ×; log scale)",
           title="Warmed lead-in speed and model size varied across DJ voices")
    ax.set_xticks([.2, .7, 2, 4, 10, 23], labels=["0.2", "0.7", "2", "4", "10", "23"])
    ax.set_yticks([.2, .5, 1, 2, 5, 10], labels=["0.2", "0.5", "1", "2", "5", "10"])
    ax.minorticks_off()
    ax.axhline(1, color="#888888", lw=1, ls="--", zorder=0)
    ax.legend(handles=[Line2D([], [], marker="o", ls="", color=c, label=k)
                       for k, c in license_colors.items()], frameon=False, ncols=3, loc="upper right", fontsize=11)
    save(fig, "06_dj_voices")

    fig, ax = plt.subplots(figsize=(11.5, 5), layout="constrained")
    for i, m in enumerate(MACHINES):
        pairs = runs[m]["pairs"]
        vals = np.array([p["seconds"] for p in pairs])
        jitter = np.random.default_rng(i+10).uniform(-.14, .14, len(vals))
        ax.scatter(vals, i+jitter, color=COLORS[m], s=42, alpha=.75)
        median = np.median(vals)
        ax.plot([median, median], [i-.23, i+.23], color=COLORS[m], lw=3)
        ax.text(median+30, i-.25, f"median {median:.1f} s; n={len(vals)}", fontsize=12, color=COLORS[m])
    ax.set(yticks=range(3), yticklabels=[LABELS[m] for m in MACHINES], ylim=(-.6, 2.6),
           xlim=(0, 2450), xlabel="Recorded request to first observed airing (s)",
           title="Request-to-airing time included rendering and queue decisions")
    ax.invert_yaxis()
    save(fig, "07_request_to_air")


def table(headers, rows):
    return ["| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *["| " + " | ".join(str(c) for c in r) + " |" for r in rows]]


def write_numbers(files, runs, voices, dual):
    lines = [
        "# Numerical provenance", "",
        "Generated by `./venv/bin/python make_figures.py` from `./data`. Paths below are relative to this directory. JSON pointers use zero-based array indices; JSONL line numbers are one-based. The paper's section, table, figure and reference numbers are editorial identifiers, not measurements.", "",
        "## Rules and definitions", "",
        "Realtime factor in this paper means audio seconds divided by render wall seconds. Larger values mean faster generation; 1.0× is playback speed. Overall speed is the ratio of summed durations, not an unweighted mean. Minutes are seconds / 60. These are definitions and unit conversions, not additional observations.", "",
        "Figure numbering, axis ticks and limits, deterministic point jitter, image dimensions and output resolution are presentation settings rather than benchmark observations. Machine colours stay constant across figures; the voice scatter uses licence colours instead.", "",
        "Published hour summaries and per-song speed points come from the matching receipts. Raw `audio_s` and `render_s` are recomputed below. All summary values agree at the receipts' displayed precision. Per-song times in the receipts are rounded to whole seconds and sometimes display `2:60`; analysis uses numeric seconds from `renders.json` instead of parsing those clock strings.", "",
        "## Product, hardware and protocol numbers", "",
    ]
    metadata = [
        ("2026-11-06; three hour runs", "data/FACTS-2026-11-06.md:1,8–10; all receipt titles"),
        ("Mac mini; Apple M4 Pro; 24 GB; macOS 26.6.2; commit 469a025; Python 3.12", "data/2026-11-06-mac-mini-hour.md:18–19"),
        ("RTX 4080; 16 GB; Ubuntu 24.04.4; commit 9035b5c; Python 3.12.15", "data/2026-11-06-apollo-hour.md:21–22"),
        ("DGX Spark; GB10; 121 GB unified; CUDA 13.0; sm_121; commit 42f1364", "data/2026-11-06-toymaker-dual-loader-hour.md:24–28"),
        ("24 GB jointly resident", "data/2026-11-06-toymaker-dual-loader-hour.md:15 (before/after loading measurement)"),
        ("YuE2-3B Q8_0 + F16 VAE; 8 steps; guidance 1.0; cot=full; Mac threads 8", "data/2026-11-06-mac-mini-hour.md:20–21; data/2026-11-06-apollo-hour.md:25; data/2026-11-06-toymaker-dual-loader-hour.md:26"),
        ("12 original song descriptions per Mac / RTX station; approximately 2:45 requested; one music render at a time", "data/2026-11-06-mac-mini-hour.md:22; data/2026-11-06-apollo-hour.md:26; log running-job counters corroborate serial music"),
        ("Spark song outputs 114.67–165.00 s", "data/toymaker/renders.json: role=song audio_s min/max; 165 s is an observed maximum, not an explicitly logged request"),
        ("RTX request threshold: fewer than 3 fresh songs waiting", "data/2026-11-06-apollo-hour.md:29–30"),
        ("Break every 2 or 3 songs; station ID about every 15 min", "data/2026-11-06-toymaker-dual-loader-hour.md:29"),
        ("11 DJ breaks + 3 station IDs = 14 speech renders", "data/2026-11-06-toymaker-dual-loader-hour.md:13–14; data/toymaker/renders.json role counts"),
        ("2 deferred breaks; speech buffer holds at 2 unused clips; 3 requeued speech clips; 6 audition takes; about 47 s GPU time; aborted start 20 s", "data/2026-11-06-toymaker-dual-loader-hour.md:33–40"),
        ("Apache-2.0 app; model licences separate; Qwen3-TTS, Drama 3 B70 Turbo, IndexTTS2, Kokoro 82M, VibeVoice 1.5B are model names", "data/FACTS-2026-11-06.md:3–5; data/dj-voices.md:28–37"),
        ("200 dpi PNG", "User-specified output setting; not a benchmark measurement"),
    ]
    lines += table(["Number or identifier", "Source"], metadata)
    lines += ["", "## Hour summaries (Table 2; Figures 1 and 4)", ""]
    rows = []
    for m in MACHINES:
        r = runs[m]; p = r["pub"]
        rows.append((LABELS[m], p["seconds"], p["checks"], len(r["songs"]), p["renders"],
                     p["songs_aired"], sum(x.get("track_kind")=="speech" for _, x in r["aired"]),
                     p["audio_min"], p["render_min"], f"{p['overall']:.2f}", f"{p['median']:.2f}",
                     f"data/{RECEIPTS[m]}:8–15; data/{m}/hour-run-report.json /seconds, /jobs, /played"))
    lines += table(["Machine", "On air s", "Checks", "Music renders", "All renders", "Song plays", "DJ plays", "Music min", "Render min", "Overall ×", "Median ×", "Sources"], rows)
    lines += ["", "Zero failures: each report `/jobs/failed` is 0; all per-render states present in `renders.json` are succeeded. All log ticks have `running=true`; each report `/running_at_end=true` and `/health_at_end/state=playing`. Receipt no-stop statements: each receipt line 8 or 9.", ""]
    lines += table(["Machine", "Sum audio s", "Sum render s", "Raw overall ×", "Raw median ×", "Raw best ×", "Source / operation"], [
        (LABELS[m], f"{r['a'].sum():.2f}", f"{r['w'].sum():.2f}", f"{r['a'].sum()/r['w'].sum():.9f}",
         f"{np.median(r['a']/r['w']):.9f}", f"{max(r['a']/r['w']):.9f}",
         f"data/{m}/renders.json: sum audio_s, sum render_s, ratio of sums, median / max individual ratios; role=song")
        for m, r in runs.items()])
    lines += ["", "## Reconciliation and scope", "",
        "- Hour totals, overall speeds and medians agree with receipts after rounding. Dividing the already rounded minute totals can give a different result; overall speeds use summed seconds before rounding.",
        "- `renders.json` stores times to two decimal places. Its `realtime_x` values and the receipt's per-song ratios reflect the published calculation. Recomputing from the rounded seconds can change the last displayed digit (for example RTX Gradient Drift take 2: 166.21 / 43.91 = 3.78524, versus published 3.78). Figure 1 uses the receipt-matching `realtime_x`; Figure 3 uses raw numeric durations. Both values are listed per render below.",
        "- The Mac report's `/renders` contains a recent subset with null render times. Its `/jobs/succeeded=26` and the complete `renders.json` determine render counts and timing sums. No null values are imputed.",
        "- Spark's receipt says playing at every one of 237 checks. The log has 236 playing ticks and one initial waiting tick (line 4), with running=true at all 237. Mac has 237 playing ticks and one initial waiting tick among 238 checks; RTX has 237 playing ticks. The paper retains the receipt's no-stop result and identifies the sampled states without treating a waiting tick as measured silence.",
        "- The Spark receipt counts 24 song plays (23 distinct titles) and 12 DJ clip plays (9 distinct clip titles). Repeated plays are included. These are observed `now_playing` events, not a count of unique rendered items or sample-accurate playback segments.",
        "- Queue depth is the length of the tick's `queue`, including repeated fill entries and speech entries. It excludes the current item and is not a count of fresh or unique ready songs. The report's `ready_tracks` is the library total and is not used as a buffer measure.",
        "- Voice lead-in ratios use the published table in `dj-voices.md`. In particular Kokoro: rounded raw 10.50 / 0.94 = 11.17021, while stored and published speed is 11.20. The finer original timing is absent; the receipt value takes precedence. Other last-digit differences from rounded times are handled identically. Failed task-configuration attempts in voice JSON files are excluded from successful speed points, not counted as hour-run failures.",
        "- Voice model sizes are the bake-off table's values, including Drama 3 at 22.7 GB. `RESEARCH.md:21` also describes a 24.3 GB Drama build, and `dj-voices.md:79` rounds it to 23 GB. The figure uses the bake-off's 22.7 GB consistently; the hour run's 24 GB jointly resident measurement describes a different quantity.",
        "- Startup 56 s is from the RTX receipt, lines 14–15: requested 12:18:12; made by 12:18:58; on air 12:19:08. Clock subtraction gives 56 s. The hour log begins after that request, so this startup value is not inferred from `start` or `on_air` events.",
        "- The Spark receipt describes three DJ lines in the pre-run benchmark. Raw `together_speech` has four takes, including a repeated 79-character line; `speech_alone` has three. The paper reports the actual take counts and preserves the receipt's aggregate ratios, which agree with pooled raw durations.",
        "", "## Figure 3 fit coefficients", "",
        "Ordinary least squares, render_s = slope × audio_s + intercept, fitted separately to each run's music renders with numpy.polyfit. Lines span each run's observed song-length range. These describe the samples, not independent performance measurements.", ""]
    lines += table(["Machine", "n", "Slope (s/s)", "Intercept (s)", "Min audio s", "Max audio s", "Source"], [
        (LABELS[m], len(r["songs"]), f"{r['fit'][0]:.9f}", f"{r['fit'][1]:.9f}", f"{r['a'].min():.2f}", f"{r['a'].max():.2f}", f"data/{m}/renders.json role=song audio_s, render_s") for m,r in runs.items()])
    lines += ["", "## Dual-loader benchmark (Figure 5)", "",
        "Source for all rows: `data/toymaker/bench-dual.json`, the named array's `audio_s` and `wall_s`. Pooled speed = sum audio_s / sum wall_s. Published values: `data/2026-11-06-toymaker-dual-loader-hour.md:17–18`.", ""]
    rows=[]
    for key, rr in dual["raw"].items():
        a=sum(x["audio_s"] for x in rr); w=sum(x["wall_s"] for x in rr)
        rows.append((key, len(rr), f"{a:.2f}", f"{w:.2f}", f"{a/w:.9f}"))
    lines += table(["Array", "Takes", "Sum audio s", "Sum wall s", "Pooled ×"], rows)
    lines += ["", "Published music alone/together: 2.34× / 1.72×; speech alone/together: 1.50× / 0.62×. Speech arrays are pooled by duration, not averaged over their `realtime_x` fields.", ""]
    rows=[]
    for key, rr in dual["raw"].items():
        for i,r in enumerate(rr):
            rows.append((f"/{key}/{i}", r.get("chars", "—"), r["audio_s"], r["wall_s"], r["realtime_x"]))
    lines += table(["JSON pointer", "Characters", "Audio s", "Wall s", "Stored ×"], rows)
    r=runs["toymaker"]; a=sum(x["audio_s"] for x in r["speech"]); w=sum(x["render_s"] for x in r["speech"])
    lines += ["", f"Hour-run speech: {len(r['speech'])} renders; {a:.2f} audio s / 60 = {a/60:.9f} min → published 2.8 min; {w:.2f} render s / 60 = {w/60:.9f} min → published 3.7 min. Source: data/toymaker/renders.json role=host_break or id; receipt lines 13–14.", "",
              "## DJ voice points (Figure 6; Table 3)", "",
              "Each point is the successful `intro` take with `music_rendering=true`. Source pointers below refer to the `voices` array. Size and licence come from the stated line of `data/dj-voices.md`; published speed agrees with the successful take's `realtime_x`.", ""]
    lines += table(["Voice", "Size GB", "Published ×", "Audio s", "Wall s", "Recomputed ×", "Licence", "Sources"], [
        (v["label"], v["size"], f"{v['speed']:.2f}", v["raw"]["audio_s"], v["raw"]["wall_s"],
         f"{v['raw']['audio_s']/v['raw']['wall_s']:.9f}", v["licence"],
         f"data/dj-voices.md:{v['line']}; data/{v['path']} /voices/{v['index']}") for v in voices])
    lines += ["", "Listener picks in order: Qwen3-TTS CustomVoice, Drama 3 with style C, IndexTTS2; Chatterbox also noted. Source: data/dj-voices.md:59–65. These are user preferences, not numerical quality scores.", "",
              "## Recorded request to first observed airing (Figure 7)", "",
              "Match each `dj_request` to the earliest later `now_playing` with exactly the same title (including take suffix). Subtract JSONL `t` values in seconds. Unmatched requests are excluded; no completion times are inferred. This measures the recorded programming path, including queue waits. It is not streaming first-audio latency.", ""]
    lines += table(["Machine", "Recorded requests", "Matched / plotted", "Unmatched", "Median s", "Min s", "Max s"], [
        (LABELS[m], len(r["requests"]), len(r["pairs"]), len(r["requests"])-len(r["pairs"]),
         f"{np.median([p['seconds'] for p in r['pairs']]):.1f}",
         f"{min(p['seconds'] for p in r['pairs']):.1f}", f"{max(p['seconds'] for p in r['pairs']):.1f}") for m,r in runs.items()])
    for m,r in runs.items():
        lines += ["", f"### {LABELS[m]} matched requests", "", f"Source: `data/{m}/hour-run-log.jsonl`, indicated lines and `t` fields.", ""]
        lines += table(["Title", "Request line", "Air line", "Request t (s)", "Air t (s)", "Difference s"], [
            (p["title"], p["request_line"], p["air_line"], p["request_t"], p["air_t"], f"{p['seconds']:.1f}") for p in r["pairs"]])
    lines += ["", "## Complete per-render inputs (Figures 1 and 3)", ""]
    for m,r in runs.items():
        lines += ["", f"### {LABELS[m]} renders", "", f"Source: `data/{m}/renders.json`; pointer gives array index. Audio/render columns use `audio_s`/`render_s`. Figure 1 uses music rows' published `realtime_x`; speech rows contribute to speech totals.", ""]
        all_renders=files[f"{m}/renders.json"]["value"]
        lines += table(["Pointer", "Role", "Title", "Audio s", "Render s", "Published ×", "Recomputed ×"], [
            (f"/{i}", x.get("role","song"), x["title"], f"{x['audio_s']:.2f}", f"{x['render_s']:.2f}", f"{x['realtime_x']:.2f}", f"{x['audio_s']/x['render_s']:.9f}") for i,x in enumerate(all_renders)])
    lines += ["", "## Complete timeline inputs (Figure 2)", "",
              "Time zero is the log's `on_air.t` (the harness Start marker). Plot time = (tick.t − on_air.t) / 60. Queue = len(tick.queue), with duplicate entries retained. Airplay curves accumulate observed `now_playing` events by track_kind (music assumed for Mac and RTX logs). Each curve extends flat to receipt seconds / 60; queue traces end at the final recorded tick. No sample-accurate transition times or queue values after the final tick are invented.", ""]
    for m,r in runs.items():
        lines += ["", f"### {LABELS[m]} timeline", "", f"Source: `data/{m}/hour-run-log.jsonl`; `on_air.t` = {r['start']} s at line 2. Queue observed range: {min(len(x['queue']) for _,x in r['ticks'])}–{max(len(x['queue']) for _,x in r['ticks'])} entries.", ""]
        lines += table(["Tick line", "t (s)", "Time since Start min", "Queue entries"], [
            (i,x["t"], f"{(x['t']-r['start'])/60:.6f}",len(x["queue"])) for i,x in r["ticks"]])
        counts={"music":0,"speech":0}; event_rows=[]
        for i,x in r["aired"]:
            kind=x.get("track_kind","music"); counts[kind]+=1
            event_rows.append((i,x["t"],f"{(x['t']-r['start'])/60:.6f}",kind,x["title"],counts[kind]))
        lines += [""]+table(["Event line", "t (s)", "Time since Start min", "Kind", "Title", "Cumulative kind count"],event_rows)
    lines += ["", "## Read manifest", "", "Every data file is read and parsed before figure generation. Markdown line counts include all lines; JSONL counts include every record.", ""]
    lines += table(["File", "Lines", "Parsed content"], [
        (f"data/{p}", len(e["raw"].splitlines()), "JSONL records" if p.endswith(".jsonl") else "JSON" if p.endswith(".json") else "Markdown") for p,e in files.items()])
    (ROOT/"NUMBERS.md").write_text("\n".join(lines)+"\n")


def main():
    files = read_all()
    runs = get_runs(files)
    voices = get_voices(files)
    raw_dual=files["toymaker/bench-dual.json"]["value"]
    receipt=files[RECEIPTS["toymaker"]]["raw"]
    p=re.search(r"music ([\d.]+)× alone and ([\d.]+)×\s+while speech rendered; speech ([\d.]+)× alone and ([\d.]+)×",receipt)
    if not p:
        raise ValueError("Dual-loader receipt values not found")
    published=dict(zip(("music_alone","music_together","speech_alone","speech_together"),map(float,p.groups())))
    for source, field in [("music_alone","music_alone"),("together_music","music_together"),
                          ("speech_alone","speech_alone"),("together_speech","speech_together")]:
        rows=raw_dual[source]
        assert round(sum(r["audio_s"] for r in rows)/sum(r["wall_s"] for r in rows),2)==published[field]
    dual=dict(raw=raw_dual,published=published)
    OUT.mkdir(exist_ok=True)
    figures(runs, voices, dual)
    write_numbers(files, runs, voices, dual)
    print(f"Read {len(files)} data files. Wrote 7 PNG/SVG figure pairs at 200 dpi and NUMBERS.md.")


if __name__ == "__main__":
    main()
