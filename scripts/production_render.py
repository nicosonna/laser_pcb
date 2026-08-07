"""Vue production monochrome : cuivre noir sur fond blanc (photoplot).

Bottom rendu en miroir (convention fabrication, vu du dessous).

    python scripts/production_render.py examples/solar_pi_odb output/production.png
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle as MplCircle, PathPatch
from matplotlib.path import Path as MplPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laserpcb import odb  # noqa: E402


def _poly_path(poly):
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        c = list(ring.coords)
        verts.extend(c)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(c) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(verts, codes)


def _draw(ax, copper, board, holes, title, mirror=False):
    (minx, miny, maxx, maxy) = board.bounds
    ax.set_facecolor("white")
    # contour de carte
    ox, oy = board.exterior.xy
    ax.plot(ox, oy, color="black", linewidth=1.0, zorder=1)
    # cuivre noir
    for poly in (copper.geoms if copper.geom_type == "MultiPolygon" else [copper]):
        if not poly.is_empty and poly.geom_type == "Polygon":
            ax.add_patch(PathPatch(_poly_path(poly), facecolor="black",
                                   edgecolor="none", zorder=2))
    # trous percés = blanc cerclé
    for h in holes:
        ax.add_patch(MplCircle((h.x, h.y), h.diameter / 2, facecolor="white",
                               edgecolor="black", linewidth=0.4, zorder=3))
    ax.set_title(title)
    ax.set_aspect("equal")
    ax.set_xlim(minx - 2, maxx + 2)
    ax.set_ylim(miny - 2, maxy + 2)
    if mirror:                       # miroir horizontal (vue de dessous)
        ax.invert_xaxis()
    ax.set_xticks([]); ax.set_yticks([])


def main(job, out):
    top = odb.load_copper(job, "top")
    bottom = odb.load_copper(job, "bottom")
    board = odb.load_outline(job)
    holes = odb.load_drill(job)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(16, 7))
    _draw(a1, top.geometry, board, holes, "TOP (cuivre)")
    _draw(a2, bottom.geometry, board, holes, "BOTTOM (cuivre, miroir)", mirror=True)
    fig.patch.set_facecolor("white")
    fig.tight_layout()
    fig.savefig(out, dpi=300, facecolor="white")
    print(f"[ok] {out}")


if __name__ == "__main__":
    job = sys.argv[1] if len(sys.argv) > 1 else "examples/solar_pi_odb"
    out = sys.argv[2] if len(sys.argv) > 2 else "output/production.png"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    main(job, out)
