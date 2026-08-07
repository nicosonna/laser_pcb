"""Image de contrôle : PCB coloré par net (top+bottom) + trous + courts-circuits DRC.

    python scripts/control_render.py examples/solar_pi_odb output/control.png
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
from laserpcb import odb, toolpath as tp  # noqa: E402


def _poly_path(poly):
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        c = list(ring.coords)
        verts.extend(c)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(c) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(verts, codes)


def _color_for(net, cache, cmap):
    if net == "GND":
        return "#1a7d3a"
    if net.startswith("$NONE$"):
        return "#8a8a8a"
    if net not in cache:
        cache[net] = cmap(len(cache) % 20)
    return cache[net]


def _draw_layer(ax, labeled, board, holes, title, shorts):
    ox, oy = board.exterior.xy
    ax.fill(ox, oy, facecolor="#efe7c8", edgecolor="#7a6a2a", linewidth=1.2, zorder=1)
    cache, cmap = {}, plt.cm.tab20
    for net, g in labeled.net_geoms.items():
        col = _color_for(net, cache, cmap)
        for poly in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
            if not poly.is_empty and poly.geom_type == "Polygon":
                ax.add_patch(PathPatch(_poly_path(poly), facecolor=col,
                                       edgecolor="none", zorder=2))
    for h in holes:
        ax.add_patch(MplCircle((h.x, h.y), h.diameter / 2, facecolor="white",
                               edgecolor="#333", linewidth=0.3, zorder=4))
    for s in shorts:
        real = s.kind == "short"
        col = "red" if real else "#e8820c"
        label = f"{s.net_a}/{s.net_b}" + ("" if real else f"\n(footprint {s.component})")
        ax.add_patch(MplCircle((s.x, s.y), 1.4, fill=False, edgecolor=col,
                               linewidth=1.8, linestyle="-" if real else "--", zorder=6))
        ax.annotate(label, (s.x, s.y), color=col, fontsize=7,
                    weight="bold", ha="center", va="bottom",
                    xytext=(0, 6), textcoords="offset points", zorder=7)
    ax.set_title(title)
    ax.set_aspect("equal"); ax.set_xlabel("mm"); ax.set_ylabel("mm")
    ax.grid(True, linewidth=0.2, alpha=0.3)


def main(job, out):
    top = odb.load_labeled_copper(job, "top")
    bottom = odb.load_labeled_copper(job, "bottom")
    board = top.board
    holes = odb.load_drill(job)

    shorts_top = tp.drc(features=odb.load_features_by_net(job, "top")).shorts
    shorts_bot = tp.drc(features=odb.load_features_by_net(job, "bottom")).shorts
    reals = sum(1 for s in shorts_top + shorts_bot if s.kind == "short")
    fps = sum(1 for s in shorts_top + shorts_bot if s.kind == "footprint")
    print(f"[i] nets top={len(top.net_geoms)} bottom={len(bottom.net_geoms)}, trous={len(holes)}")
    print(f"[i] DRC : {reals} vrais courts (rouge), {fps} footprints même composant (orange)")

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(17, 7))
    _draw_layer(a1, top, board, holes, f"TOP — {len(top.net_geoms)} nets", shorts_top)
    _draw_layer(a2, bottom, board, holes, f"BOTTOM — {len(bottom.net_geoms)} nets", shorts_bot)
    fig.suptitle("PCB par net · rouge = vrai court inter-composants · orange = footprint (même composant)",
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(out, dpi=170)
    print(f"[ok] {out}")


if __name__ == "__main__":
    job = sys.argv[1] if len(sys.argv) > 1 else "examples/solar_pi_odb"
    out = sys.argv[2] if len(sys.argv) > 2 else "output/control.png"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    main(job, out)
