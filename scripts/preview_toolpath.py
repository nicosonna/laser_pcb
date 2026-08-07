"""Aperçu des parcours laser : compare ISOLATION vs NÉGATIF sur un calque.

    python scripts/preview_toolpath.py examples/module_relais copper_bottom output/toolpath.png
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laserpcb import geometry as geo          # noqa: E402
from laserpcb import toolpath as tp            # noqa: E402


def _draw_copper(ax, copper, board):
    ox, oy = board.exterior.xy
    ax.fill(ox, oy, facecolor="#e7d9a8", edgecolor="#7a6a2a", linewidth=1.0, zorder=1)
    for poly in copper.geoms:
        xs, ys = poly.exterior.xy
        ax.fill(xs, ys, facecolor="#c98a3a", edgecolor="none", alpha=0.5, zorder=2)


def _draw_lines(ax, mls, color, lw=0.6):
    if mls.is_empty:
        return
    for line in mls.geoms:
        xs, ys = line.xy
        ax.plot(xs, ys, color=color, linewidth=lw, zorder=5)


def main(folder, layer_name, out):
    folder = Path(folder)
    copper = geo.load_copper(folder / "GerberFiles" / f"{layer_name}.gbr", layer_name)
    board = geo.load_outline(folder / "GerberFiles" / "profile.gbr")

    kerf = 0.15
    iso = tp.isolation_rings(copper.geometry, kerf=kerf, rings=1)
    region = tp.negative_region(copper.geometry, board, keepout=0.0)
    hatch = tp.hatch(region, spacing=kerf, angle=0.0)

    print(f"[i] Isolation : {len(iso.geoms)} contours")
    print(f"[i] Négatif   : {len(region.geoms)} zones, {len(hatch.geoms)} lignes de hachure")

    fig, axes = plt.subplots(1, 2, figsize=(14, 9))

    axes[0].set_title(f"ISOLATION (kerf {kerf} mm, 1 anneau) — {layer_name}")
    _draw_copper(axes[0], copper.geometry, board)
    _draw_lines(axes[0], iso, color="#d81e1e", lw=0.5)

    axes[1].set_title(f"NÉGATIF / masque (hachures {kerf} mm) — {layer_name}")
    ox, oy = board.exterior.xy
    axes[1].fill(ox, oy, facecolor="#e7d9a8", edgecolor="#7a6a2a", linewidth=1.0, zorder=1)
    _draw_lines(axes[1], hatch, color="#111111", lw=0.3)

    for ax in axes:
        ax.set_aspect("equal")
        ax.set_xlabel("mm")
        ax.set_ylabel("mm")
        ax.grid(True, linewidth=0.2, alpha=0.3)

    fig.tight_layout()
    fig.savefig(out, dpi=150)
    print(f"[ok] Aperçu parcours écrit : {out}")


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else "examples/module_relais"
    layer = sys.argv[2] if len(sys.argv) > 2 else "copper_bottom"
    out = sys.argv[3] if len(sys.argv) > 3 else "output/toolpath.png"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    main(folder, layer, out)
