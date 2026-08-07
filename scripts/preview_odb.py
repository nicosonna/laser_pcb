"""Aperçu ODB++ : prouve que le même modèle shapely marche sur un job ODB++.

    python scripts/preview_odb.py <job_root> out.png
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


def _polygon_to_path(poly):
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        coords = list(ring.coords)
        verts.extend(coords)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(coords) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(verts, codes)


def _draw(ax, mp, color, z=2):
    for poly in getattr(mp, "geoms", [mp]):
        if not poly.is_empty:
            ax.add_patch(PathPatch(_polygon_to_path(poly), facecolor=color,
                                   edgecolor="none", zorder=z))


def main(job_root, out):
    top = odb.load_copper(job_root, "top")
    bottom = odb.load_copper(job_root, "bottom")
    outline = odb.load_outline(job_root)
    holes = odb.load_drill(job_root)

    print(f"[i] Contour: bounds={tuple(round(v, 1) for v in outline.bounds)}")
    print(f"[i] Top    : {len(top.geometry.geoms)} polygones")
    print(f"[i] Bottom : {len(bottom.geometry.geoms)} polygones")
    print(f"[i] Trous  : {len(holes)}")

    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    for ax, layer, title in ((axes[0], top, "TOP"), (axes[1], bottom, "BOTTOM")):
        ox, oy = outline.exterior.xy
        ax.fill(ox, oy, facecolor="#d9c27a", edgecolor="#7a6a2a", linewidth=1, zorder=1)
        _draw(ax, layer.geometry, "#111111")
        for h in holes:
            ax.add_patch(MplCircle((h.x, h.y), h.diameter / 2, facecolor="white",
                                   edgecolor="red", linewidth=0.4, zorder=4))
        ax.set_title(f"{title} (ODB++) — {layer.name}")
        ax.set_aspect("equal"); ax.set_xlabel("mm"); ax.set_ylabel("mm")
        ax.grid(True, linewidth=0.2, alpha=0.3)

    fig.tight_layout()
    fig.savefig(out, dpi=150)
    print(f"[ok] Aperçu ODB++ écrit : {out}")


if __name__ == "__main__":
    job_root = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "output/preview_odb.png"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    main(job_root, out)
