"""Isolation NET-AWARE : coupe seulement entre nets différents, garde les ponts d'un même net.

Prouve, sur l'IC critique de solar_pi, que :
  - le pad central GND et ses "ponts" (spokes thermiques, pistes GND) restent INTACTS,
  - l'axe médian ne coupe QU'ENTRE nets différents (lu depuis la netlist ODB++),
  - aucun court-circuit n'existe (contrôle DRC).

    python scripts/proto_netaware.py output/netaware.png
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MplPath
from scipy.spatial import Voronoi, cKDTree
from shapely import box, segmentize
from shapely.geometry import LineString
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laserpcb.odb import Symbol, _read_surface, _ORIENT  # noqa: E402

BASE = Path("examples/solar_pi_odb/steps/pcb")
ZONE = box(55, 36, 71, 52)          # fenêtre autour de l'IC
KERF = 0.15


def load_net_map():
    cur, feat2net = None, {}
    for line in (BASE / "eda/data").read_text().splitlines():
        m = re.match(r"NET\s+(\S+?);.*", line)
        if m:
            cur = m.group(1); continue
        m = re.match(r"FID\s+C\s+(\d+)\s+(\d+)", line)
        if m and int(m.group(1)) == 3:      # calque 3 = top
            feat2net[int(m.group(2))] = cur
    return feat2net


def parse_top_features(feat2net):
    lines = (BASE / "layers/top/features").read_text().splitlines()
    symbols, feats, i, idx = {}, [], 0, 0
    while i < len(lines):
        raw = lines[i].strip(); i += 1
        if not raw or raw.startswith(("#", "F ", "ID", "UNITS")):
            continue
        if raw.startswith("$"):
            p = raw.split(); symbols[int(p[0][1:])] = Symbol(p[1]); continue
        tok = raw.split(); rec = tok[0]; g = None; pol = "P"
        if rec == "L" and len(tok) >= 7:
            x1, y1, x2, y2 = map(float, tok[1:5]); sym = symbols.get(int(tok[5])); pol = tok[6]
            if sym and sym.width > 0:
                g = LineString([(x1, y1), (x2, y2)]).buffer(sym.width / 2, quad_segs=16)
        elif rec == "P" and len(tok) >= 5:
            x, y = float(tok[1]), float(tok[2]); sym = symbols.get(int(tok[3])); pol = tok[4]
            o = _ORIENT.get(tok[5], 0) if len(tok) > 5 else 0
            if sym:
                g = sym.flash(x, y, o)
        elif rec == "S":
            pol = tok[1] if len(tok) > 1 else "P"; g, i = _read_surface(lines, i)
        else:
            continue
        if g is not None and not g.is_empty and pol == "P":
            feats.append((g, feat2net.get(idx)))
        idx += 1
    return feats


def net_aware_centerline(net_geoms, step=0.07):
    """Axe médian ne gardant que les arêtes séparant deux NETS différents."""
    pts, lab = [], []
    names = list(net_geoms)
    for li, net in enumerate(names):
        g = net_geoms[net]
        for poly in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
            if poly.is_empty:
                continue
            d = segmentize(poly, step)
            for ring in [d.exterior, *d.interiors]:
                for x, y in ring.coords[:-1]:
                    pts.append((x, y)); lab.append(li)
    pts = np.asarray(pts); lab = np.asarray(lab)
    vor = Voronoi(pts); tree = cKDTree(pts)
    gap = ZONE.difference(unary_union(list(net_geoms.values())))
    segs = []
    for (a, b), (v0, v1) in zip(vor.ridge_points, vor.ridge_vertices):
        if lab[a] == lab[b] or v0 < 0 or v1 < 0:
            continue
        p0, p1 = vor.vertices[v0], vor.vertices[v1]
        clr = float(min(tree.query(p0)[0], tree.query(p1)[0]))
        inter = LineString([p0, p1]).intersection(gap)
        for s in (inter.geoms if inter.geom_type == "MultiLineString" else [inter]):
            if s.geom_type == "LineString" and s.length > 0:
                segs.append((list(s.coords), clr))
    return segs


def _poly_path(poly):
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        c = list(ring.coords)
        verts.extend(c); codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(c) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(verts, codes)


def main(out):
    feat2net = load_net_map()
    feats = parse_top_features(feat2net)

    # regroupe le cuivre par net, clippé à la fenêtre de l'IC
    per_net = defaultdict(list)
    for g, net in feats:
        gi = g.intersection(ZONE)
        if not gi.is_empty:
            per_net[net].append(gi)
    net_geoms = {net: unary_union(v) for net, v in per_net.items()}

    # DRC : deux nets différents qui se touchent = court-circuit
    names = [n for n in net_geoms if n not in (None, "$NONE$")]
    shorts = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if net_geoms[names[i]].distance(net_geoms[names[j]]) < 1e-6:
                shorts.append((names[i], names[j]))
    print(f"[i] nets présents dans la zone IC : {len(names)}")
    print(f"[i] courts-circuits (nets différents en contact) : {len(shorts)}")

    segs = net_aware_centerline(net_geoms)
    print(f"[i] segments d'isolation (entre nets différents) : {len(segs)}")

    # rendu
    fig, ax = plt.subplots(figsize=(11, 10))
    cmap = plt.cm.tab20
    color = {}
    k = 0
    for net in net_geoms:
        if net == "GND":
            color[net] = "#1a7d3a"      # GND bien visible
        elif net in (None, "$NONE$"):
            color[net] = "#888888"
        else:
            color[net] = cmap(k % 20); k += 1
    for net, g in net_geoms.items():
        for poly in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
            if not poly.is_empty and poly.geom_type == "Polygon":
                ax.add_patch(PathPatch(_poly_path(poly), facecolor=color[net],
                                       edgecolor="none", zorder=2))
    # traits d'isolation : noir, rouge pointillé si sub-kerf
    normal = [c for c, clr in segs if 2 * clr >= KERF]
    subker = [c for c, clr in segs if 2 * clr < KERF]
    ax.add_collection(LineCollection(normal, colors="black", linewidths=1.4, zorder=6))
    if subker:
        ax.add_collection(LineCollection(subker, colors="red", linewidths=2.6,
                                         linestyles="dotted", zorder=7))

    # légende compacte des nets principaux
    import matplotlib.patches as mp
    big = sorted(net_geoms, key=lambda n: -net_geoms[n].area)[:8]
    handles = [mp.Patch(color=color[n], label=(n or "(sans net)")) for n in big]
    handles.append(plt.Line2D([0], [0], color="black", label="coupe (nets différents)"))
    handles.append(plt.Line2D([0], [0], color="red", ls=":", label="canal < kerf"))
    ax.legend(handles=handles, loc="upper left", fontsize=8, framealpha=0.9)

    ax.set_title("IC net-aware : ponts GND↔centre conservés, coupe seulement entre nets différents")
    ax.set_aspect("equal"); ax.set_xlim(55, 71); ax.set_ylim(36, 52)
    ax.set_xlabel("mm"); ax.set_ylabel("mm")
    fig.tight_layout(); fig.savefig(out, dpi=170)
    print(f"[ok] {out}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "output/netaware.png"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    main(out)
