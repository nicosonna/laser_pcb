"""Détection auto de la zone fine-pitch + paramètres laser adaptatifs.

- Calcule l'axe médian (Voronoï net-aware) du calque cuivre réel.
- Mesure la clairance locale (distance au cuivre) le long de chaque trait.
- Détecte automatiquement le cluster au pas le plus serré = l'IC critique.
- Dérive vitesse & puissance par segment : serré -> lent + doux, large -> rapide.

    python scripts/proto_adaptive.py <job_odb> top output/adaptive.png
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.patches import Rectangle, PathPatch
from matplotlib.path import Path as MplPath
from scipy.spatial import Voronoi, cKDTree
from shapely import segmentize
from shapely.geometry import LineString
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laserpcb import odb  # noqa: E402

# ---- calibration Falcon 2 (à ajuster une fois pour ta machine) -------------- #
KERF = 0.15
V_FAST, V_SLOW = 1500.0, 300.0     # mm/min
P_FULL, P_SOFT = 100.0, 78.0       # %
C_OPEN, C_TIGHT = 0.60, KERF / 2   # clairance (mm) : large / serrée


def _adaptive(clearance):
    """clearance (demi-largeur du canal, mm) -> (vitesse mm/min, puissance %)."""
    t = np.clip((clearance - C_TIGHT) / (C_OPEN - C_TIGHT), 0, 1)
    return V_SLOW + t * (V_FAST - V_SLOW), P_SOFT + t * (P_FULL - P_SOFT)


def _explode(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        out = []
        for g in geom.geoms:
            out.extend(_explode(g))
        return out
    return []


def centerline_with_clearance(polys, board, step=0.1):
    """Renvoie une liste de (segment, clairance_mm) le long de l'axe médian."""
    pts, lab = [], []
    for i, poly in enumerate(polys):
        d = segmentize(poly, step)
        for ring in [d.exterior, *d.interiors]:
            for x, y in ring.coords[:-1]:
                pts.append((x, y)); lab.append(i)
    pts = np.asarray(pts); lab = np.asarray(lab)
    vor = Voronoi(pts)
    tree = cKDTree(pts)
    gap = board.difference(unary_union(polys))

    out = []
    for (a, b), (v0, v1) in zip(vor.ridge_points, vor.ridge_vertices):
        if lab[a] == lab[b] or v0 < 0 or v1 < 0:
            continue
        p0, p1 = vor.vertices[v0], vor.vertices[v1]
        clr = float(min(tree.query(p0)[0], tree.query(p1)[0]))
        for seg in _explode(LineString([p0, p1]).intersection(gap)):
            out.append((seg, clr))
    return out


def detect_tight_zone(segs, c_floor=0.04, radius=5.0):
    """Repère le cluster de traits à la plus faible clairance -> bbox de la zone.

    :param c_floor: clairance minimale prise au sérieux (mm). En dessous, ce sont
                    des cuivres quasi-jointifs (artefacts / même net) qu'on ignore.
    """
    clrs = np.array([c for _, c in segs])
    mids = np.array([list(s.interpolate(0.5, normalized=True).coords[0])
                     for s, _ in segs])
    valid = clrs >= c_floor
    thr = np.percentile(clrs[valid], 6)          # frontière "serré" robuste
    tight_mask = valid & (clrs <= thr)
    tight = mids[tight_mask]

    tree = cKDTree(tight)
    counts = np.array([len(tree.query_ball_point(p, radius)) for p in tight])
    seed = tight[int(counts.argmax())]
    idx = tree.query_ball_point(seed, radius)
    cluster = tight[idx]
    cluster_clr = clrs[tight_mask][idx]
    m = 1.5
    bbox = (cluster[:, 0].min() - m, cluster[:, 1].min() - m,
            cluster[:, 0].max() + m, cluster[:, 1].max() + m)
    pitch = 2 * float(np.median(cluster_clr))     # pas représentatif (robuste)
    return bbox, pitch


def _poly_path(poly):
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        c = list(ring.coords)
        verts.extend(c)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(c) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(verts, codes)


def main(job, layer, out):
    copper = odb.load_copper(job, layer)
    board = odb.load_outline(job)
    polys = list(copper.geometry.geoms)

    print("[i] Calcul de l'axe médian…")
    segs = centerline_with_clearance(polys, board)
    (zx0, zy0, zx1, zy1), pitch = detect_tight_zone(segs)
    print(f"[i] Pas serré représentatif détecté : {pitch:.3f} mm (clairance {pitch/2:.3f} mm)")
    print(f"[i] Zone IC critique : x[{zx0:.1f},{zx1:.1f}] y[{zy0:.1f},{zy1:.1f}]")

    fig, (axm, axz) = plt.subplots(1, 2, figsize=(15, 7))

    # --- vue d'ensemble : carte + zone détectée ---
    ox, oy = board.exterior.xy
    axm.fill(ox, oy, facecolor="#e7d9a8", edgecolor="#7a6a2a", lw=1, zorder=1)
    for p in polys:
        axm.add_patch(PathPatch(_poly_path(p), facecolor="#111", edgecolor="none", zorder=2))
    axm.add_patch(Rectangle((zx0, zy0), zx1 - zx0, zy1 - zy0, fill=False,
                            edgecolor="red", lw=2, zorder=5))
    axm.annotate("IC au pas le plus serré", (zx0, zy1), color="red",
                 fontsize=10, weight="bold", va="bottom")
    axm.set_title(f"{layer} — détection auto de la zone critique")
    axm.set_aspect("equal"); axm.set_xlabel("mm"); axm.set_ylabel("mm")

    # --- zoom : axe médian coloré par vitesse, alertes sub-kerf ---
    for p in polys:
        axz.add_patch(PathPatch(_poly_path(p), facecolor="#c98a3a",
                                edgecolor="#5a3d15", lw=0.4, zorder=2))
    lines, speeds, warns = [], [], []
    for seg, clr in segs:
        (x0, y0, x1, y1) = seg.bounds
        if x1 < zx0 or x0 > zx1 or y1 < zy0 or y0 > zy1:
            continue
        v, _p = _adaptive(clr)
        coords = list(seg.coords)
        lines.append(coords); speeds.append(v)
        if 2 * clr < KERF:                       # canal plus étroit que le kerf
            warns.append(coords)
    lc = LineCollection(lines, array=np.array(speeds), cmap="RdYlGn",
                        linewidths=1.6, zorder=5)
    axz.add_collection(lc)
    if warns:
        axz.add_collection(LineCollection(warns, colors="red", linewidths=3.2,
                                          linestyles="dotted", zorder=6))
    cb = fig.colorbar(lc, ax=axz, fraction=0.046, pad=0.04)
    cb.set_label("vitesse laser (mm/min)")
    axz.set_title(f"Zone critique — passe unique adaptative (pas {pitch:.2f} mm)")
    axz.set_aspect("equal"); axz.set_xlim(zx0, zx1); axz.set_ylim(zy0, zy1)
    axz.set_xlabel("mm")

    fig.suptitle(f"Falcon 2 : {V_SLOW:.0f}→{V_FAST:.0f} mm/min · "
                 f"{P_SOFT:.0f}→{P_FULL:.0f} % selon la clairance", fontsize=12)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    print(f"[ok] {out}")


if __name__ == "__main__":
    job = sys.argv[1]
    layer = sys.argv[2] if len(sys.argv) > 2 else "top"
    out = sys.argv[3] if len(sys.argv) > 3 else "output/adaptive.png"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    main(job, layer, out)
