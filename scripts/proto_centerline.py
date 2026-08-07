"""Preuve de concept : isolation par AXE MÉDIAN (Voronoï net-aware).

Compare, sur un footprint fine-pitch synthétique :
  * OFFSET FIXE  : un trait à kerf/2 autour de CHAQUE cuivre -> 2 traits dans un
                   interstice serré + un ruban de cuivre flottant entre les pattes.
  * AXE MÉDIAN   : un SEUL trait centré entre les deux cuivres -> zéro ruban,
                   pattes conservées au maximum, moitié moins de gravure.

    python scripts/proto_centerline.py output/centerline.png
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.spatial import Voronoi
from shapely import box, segmentize
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge, unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --------------------------------------------------------------------------- #
# Cœur : axe médian net-aware
# --------------------------------------------------------------------------- #
def centerline_isolation(copper_polys, board, sample=0.08):
    """Trait d'isolation équidistant entre cuivres *différents* (axe médian).

    On échantillonne le bord de chaque polygone en points étiquetés par leur
    net, on calcule le diagramme de Voronoï, puis on garde les arêtes qui séparent
    deux nets différents : ce sont exactement les lignes équidistantes des deux
    cuivres. Clippées au vide (carte − cuivre), elles donnent une passe unique
    centrée.
    """
    pts, labels = [], []
    for i, poly in enumerate(copper_polys):
        dense = segmentize(poly, sample)
        for ring in [dense.exterior, *dense.interiors]:
            for x, y in ring.coords[:-1]:
                pts.append((x, y)); labels.append(i)
    pts = np.array(pts); labels = np.array(labels)

    vor = Voronoi(pts)
    segs = []
    for (a, b), (v0, v1) in zip(vor.ridge_points, vor.ridge_vertices):
        if labels[a] == labels[b]:
            continue                    # arête interne à un même cuivre -> ignorée
        if v0 < 0 or v1 < 0:
            continue                    # arête partant à l'infini -> ignorée
        segs.append(LineString([vor.vertices[v0], vor.vertices[v1]]))

    gap = board.difference(unary_union(copper_polys))
    clipped = unary_union(segs).intersection(gap)
    merged = linemerge(clipped) if not clipped.is_empty else clipped
    if merged.geom_type == "LineString":
        return MultiLineString([merged])
    if merged.geom_type == "MultiLineString":
        return merged
    return MultiLineString([g for g in getattr(merged, "geoms", []) if g.geom_type == "LineString"])


def fixed_offset(copper_polys, kerf):
    """Isolation classique : contour à kerf/2 autour de chaque cuivre."""
    lines = []
    for poly in copper_polys:
        ring = poly.buffer(kerf / 2, join_style="round").exterior
        lines.append(LineString(ring.coords))
    return MultiLineString(lines)


# --------------------------------------------------------------------------- #
# Footprint fine-pitch synthétique (type SSOP/TQFP, pas 0.65 mm)
# --------------------------------------------------------------------------- #
def make_footprint():
    pads = []
    pw, ph, pitch = 0.30, 1.10, 0.65
    n = 6
    x0 = -(n - 1) * pitch / 2
    for k in range(n):
        cx = x0 + k * pitch
        pads.append(box(cx - pw / 2, 2.5 - ph / 2, cx + pw / 2, 2.5 + ph / 2))  # rangée haut
        pads.append(box(cx - pw / 2, 0.0 - ph / 2, cx + pw / 2, 0.0 + ph / 2))  # rangée bas
    return pads


def draw_lines(ax, mls, color, lw):
    for line in getattr(mls, "geoms", []):
        xs, ys = line.xy
        ax.plot(xs, ys, color=color, linewidth=lw, solid_capstyle="round", zorder=5)


def main(out):
    kerf = 0.15
    pads = make_footprint()
    board = box(-2.5, -1.4, 2.5, 3.9)

    off = fixed_offset(pads, kerf)
    cen = centerline_isolation(pads, board)

    off_len = sum(l.length for l in off.geoms)
    cen_len = sum(l.length for l in cen.geoms)
    print(f"[i] Offset fixe : {len(off.geoms)} contours, longueur gravée {off_len:.1f} mm")
    print(f"[i] Axe médian  : {len(cen.geoms)} traits,   longueur gravée {cen_len:.1f} mm")
    print(f"[i] Gain gravure : -{100*(1-cen_len/off_len):.0f} %")

    fig, axes = plt.subplots(1, 2, figsize=(13, 7))
    for ax, title in ((axes[0], "OFFSET FIXE — 2 traits + ruban de cuivre flottant"),
                      (axes[1], "AXE MÉDIAN — 1 passe centrée, cuivre maximal")):
        for p in pads:
            xs, ys = p.exterior.xy
            ax.fill(xs, ys, facecolor="#c98a3a", edgecolor="#5a3d15", linewidth=0.5, zorder=2)
        ax.set_title(title, fontsize=11)
        ax.set_aspect("equal"); ax.set_xlim(-2.5, 2.5); ax.set_ylim(-1.4, 3.9)
        ax.set_xlabel("mm")
    draw_lines(axes[0], off, "#d81e1e", 1.4)
    draw_lines(axes[1], cen, "#0a8f2a", 1.4)

    fig.suptitle(f"Isolation fine-pitch (pas 0.65 mm, kerf {kerf} mm)", fontsize=13)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    print(f"[ok] {out}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "output/centerline.png"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    main(out)
