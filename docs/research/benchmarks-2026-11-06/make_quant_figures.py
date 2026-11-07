#!/usr/bin/env python3
"""Figures for the quantization and serving work (sections 3 and 4 of PAPER.md).

Every number below is copied from the published model cards:
  Drama 3 B70 Turbo  https://huggingface.co/Frosty40/Drama-3-B70-Turbo-GGUF  (Numbers, What changed)
  YuE2 Turbo Radio   https://huggingface.co/Frosty40/YuE2-Turbo-Radio         (latest results, history/README-before-apollo-20261102.md)
Run: uv run --with matplotlib python make_quant_figures.py
"""
from pathlib import Path
import os, tempfile

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "myradio-paper-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = Path(__file__).resolve().parent / "figures"
INK, MUTED, GRID = "#1d2230", "#6b7285", "#e3e5ea"
CORAL, PLUM, AMBER, TEAL, SLATE = "#e8734e", "#6b4f9e", "#d99a2b", "#2f8f8a", "#9aa1b2"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": INK,
                     "xtick.color": INK, "ytick.color": INK, "axes.spines.top": False, "axes.spines.right": False})


def save(fig, name):
    fig.savefig(OUT / f"{name}.png", dpi=200, bbox_inches="tight")
    fig.savefig(OUT / f"{name}.svg", bbox_inches="tight")
    plt.close(fig)


# Figure 1: Drama 3, wall time per 46.4 ms audio frame, stock against Turbo, with Turbo's stage breakdown
fig, ax = plt.subplots(figsize=(7.2, 2.6))
ax.barh([1], [68.1], color=SLATE, height=0.5)
ax.text(68.1 + 1, 1, "68.1 ms  (stock)", va="center", color=INK)
left = 0
for ms, label, colour in [(12.7, "backbone 12.7 ms", PLUM), (7.4, "audio tower 7.4 ms", TEAL), (0.5, "sampler and head 0.5 ms", AMBER), (5.5, "codec and everything else 5.5 ms", CORAL)]:
    ax.barh([0], [ms], left=left, color=colour, height=0.5, label=label)
    left += ms
ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.62), ncol=2, frameon=False, fontsize=8.5)
ax.text(26.1 + 1, 0, "26.1 ms  (Turbo, 2.6× faster)", va="center", color=INK, fontweight="bold")
ax.axvline(46.4, color=INK, lw=1, ls=(0, (3, 3)))
ax.text(46.4, 1.42, "46.4 ms of audio per frame", ha="center", fontsize=8.5, color=MUTED)
ax.set_yticks([1, 0], ["stock", "Turbo"])
ax.set_xlim(0, 92)
ax.set_ylim(-0.45, 1.6)
ax.set_xlabel("wall milliseconds to make one audio frame (lower is faster)")
ax.grid(axis="x", color=GRID)
ax.set_axisbelow(True)
save(fig, "q1_drama3_frame")

# Figure 2: YuE2 speed, times real time, from stock settings on the B70 to the CUDA port
steps = [
    ("B70\nstock settings\n4:20 song", 0.63, SLATE),
    ("B70\nguidance 1.0,\nSYCL ops\n4:20 song", 1.04, AMBER),
    ("B70\nfull recipe\ncity_lights", 3.091, CORAL),
    ("RTX 4080\nCUDA port\ncity_lights", 3.168, PLUM),
    ("RTX 4080\nCUDA port\n4:17 song", 3.322, PLUM),
]
fig, ax = plt.subplots(figsize=(7.2, 3.4))
xs = range(len(steps))
ax.bar(xs, [s[1] for s in steps], color=[s[2] for s in steps], width=0.62)
for x, (_, v, _) in zip(xs, steps):
    ax.text(x, v + 0.06, f"{v:.2f}×", ha="center", color=INK, fontweight="bold")
ax.axhline(1.0, color=INK, lw=1, ls=(0, (3, 3)))
ax.text(-0.45, 1.06, "real time", ha="left", va="bottom", fontsize=8.5, color=MUTED)
ax.set_xticks(list(xs), [s[0] for s in steps], fontsize=8.5)
ax.set_ylabel("times real time (higher is faster)")
ax.set_ylim(0, 3.75)
ax.grid(axis="y", color=GRID)
ax.set_axisbelow(True)
save(fig, "q2_yue2_speed")
print("wrote", OUT / "q1_drama3_frame.png", OUT / "q2_yue2_speed.png")
