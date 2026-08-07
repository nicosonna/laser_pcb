"""Canvas d'aperçu : figure matplotlib intégrée dans Qt."""

from __future__ import annotations

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure
from matplotlib.patches import Circle as MplCircle, PathPatch
from matplotlib.path import Path as MplPath

_ROLE_COLOR = {"top": "#b5651d", "bottom": "#1d6fb5"}


def _poly_path(poly):
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        c = list(ring.coords)
        verts.extend(c)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(c) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(verts, codes)


class PcbCanvas(FigureCanvasQTAgg):
    def __init__(self):
        self.fig = Figure(figsize=(6, 6))
        super().__init__(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_aspect("equal")

    def render_session(self, session, toolpath_items=None):
        ax = self.ax
        ax.clear()
        ax.set_aspect("equal")
        if session.board is not None and not session.board.is_empty:
            ox, oy = session.board.exterior.xy
            ax.fill(ox, oy, facecolor="#efe7c8", edgecolor="#7a6a2a", linewidth=1, zorder=1)

        for layer in session.layers:
            if not layer.config.enabled:
                continue
            color = _ROLE_COLOR.get(layer.config.role, "#333333")
            for poly in getattr(layer.labeled.union, "geoms", [layer.labeled.union]):
                if not poly.is_empty and poly.geom_type == "Polygon":
                    ax.add_patch(PathPatch(_poly_path(poly), facecolor=color,
                                           edgecolor="none", alpha=0.55, zorder=2))

        for h in session.holes:
            ax.add_patch(MplCircle((h.x, h.y), h.diameter / 2, facecolor="white",
                                   edgecolor="#333", linewidth=0.3, zorder=4))

        if toolpath_items:
            segs = [it[0] for it in toolpath_items]
            ax.add_collection(LineCollection(segs, colors="#d81e1e", linewidths=0.6, zorder=5))

        if session.board is not None and not session.board.is_empty:
            minx, miny, maxx, maxy = session.board.bounds
            ax.set_xlim(minx - 2, maxx + 2)
            ax.set_ylim(miny - 2, maxy + 2)
        ax.set_xlabel("mm")
        ax.set_ylabel("mm")
        self.draw_idle()
