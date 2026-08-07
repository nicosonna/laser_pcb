"""Simulateur : reparse un .gcode et le dessine (validation bout-en-bout).

G1 (gravure) coloré par vitesse F ; G0 (trajets) en gris pointillé.

    python scripts/gcode_view.py output/adaptive_qfn.gcode output/gcode_view.png
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection


def parse(path):
    x = y = 0.0
    f = 0.0
    cuts, feeds, travels = [], [], []
    rx = re.compile(r"X(-?[\d.]+)"); ry = re.compile(r"Y(-?[\d.]+)"); rf = re.compile(r"F([\d.]+)")
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        code = line.split()[0]
        if code not in ("G0", "G1"):
            continue
        nx, ny = x, y
        mx, my = rx.search(line), ry.search(line)
        if mx: nx = float(mx.group(1))
        if my: ny = float(my.group(1))
        mf = rf.search(line)
        if mf: f = float(mf.group(1))
        seg = [(x, y), (nx, ny)]
        if code == "G1":
            cuts.append(seg); feeds.append(f)
        else:
            travels.append(seg)
        x, y = nx, ny
    return cuts, feeds, travels


def main(path, out):
    cuts, feeds, travels = parse(path)
    length = sum(np.hypot(b[0]-a[0], b[1]-a[1]) for a, b in cuts)
    print(f"[i] {len(cuts)} coupes ({length:.0f} mm), {len(travels)} trajets")

    fig, ax = plt.subplots(figsize=(10, 10))
    if travels:
        ax.add_collection(LineCollection(travels, colors="#cccccc", linewidths=0.4,
                                         linestyles="dotted", zorder=1))
    lc = LineCollection(cuts, array=np.array(feeds), cmap="RdYlGn", linewidths=1.3, zorder=3)
    ax.add_collection(lc)
    cb = fig.colorbar(lc, ax=ax, fraction=0.046, pad=0.04)
    cb.set_label("vitesse F (mm/min)")
    ax.autoscale(); ax.set_aspect("equal")
    ax.set_title(f"Relecture du G-code : {Path(path).name}")
    ax.set_xlabel("mm"); ax.set_ylabel("mm")
    fig.tight_layout(); fig.savefig(out, dpi=170)
    print(f"[ok] {out}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "output/adaptive_qfn.gcode"
    out = sys.argv[2] if len(sys.argv) > 2 else "output/gcode_view.png"
    main(path, out)
