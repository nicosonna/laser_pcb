"""Canvas d'aperçu : figure matplotlib intégrée dans Qt."""

from __future__ import annotations

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure
from matplotlib.patches import Circle as MplCircle, PathPatch, Rectangle
from matplotlib.path import Path as MplPath
from shapely.affinity import scale as _scale, translate as _translate
from shapely.geometry import LineString
from shapely.ops import unary_union

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
        self.mpl_connect("scroll_event", self._on_scroll)

    def _on_scroll(self, event):
        """Zoom à la molette, centré sur le curseur."""
        if event.inaxes is None:
            return
        ax = self.ax
        factor = 0.8 if event.button == "up" else 1.25
        x, y = event.xdata, event.ydata
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        ax.set_xlim(x - (x - x0) * factor, x + (x1 - x) * factor)
        ax.set_ylim(y - (y - y0) * factor, y + (y1 - y) * factor)
        self.draw_idle()

    def render_session(self, session, toolpath_items=None, roles=None,
                       mirror=False, burn=False):
        ax = self.ax
        ax.clear()
        ax.set_aspect("equal")
        proj = session.project
        ox, oy = proj.origin
        cx = session.board_center_x() if session.board is not None else 0.0

        def xf(geom):                       # miroir (bottom) puis offset plateau
            if mirror:
                geom = _scale(geom, xfact=-1, yfact=1, origin=(cx, 0))
            if ox or oy:
                geom = _translate(geom, ox, oy)
            return geom

        def xp(x, y):
            if mirror:
                x = 2 * cx - x
            return (x + ox, y + oy)

        # plateau
        ax.add_patch(Rectangle((0, 0), proj.bed_width, proj.bed_height, fill=False,
                               edgecolor="#8a8a8a", linestyle="--", linewidth=1, zorder=0))
        ax.text(1, proj.bed_height - 1, "plateau", color="#8a8a8a", fontsize=7, va="top")

        if session.board is not None and not session.board.is_empty:
            b = xf(session.board)
            bx, by = b.exterior.xy
            ax.fill(bx, by, facecolor="#efe7c8", edgecolor="#7a6a2a", linewidth=1, zorder=1)

        if burn:
            # Aperçu brûlure : là où le laser passe = traits épaissis du kerf, en noir.
            strokes = [LineString(c).buffer(proj.kerf / 2, cap_style="round")
                       for c, _f, _p in (toolpath_items or []) if len(c) >= 2]
            if strokes:
                merged = xf(unary_union(strokes))
                for poly in getattr(merged, "geoms", [merged]):
                    if poly.geom_type == "Polygon" and not poly.is_empty:
                        ax.add_patch(PathPatch(_poly_path(poly), facecolor="black",
                                               edgecolor="none", zorder=3))
        else:
            for layer in session.layers:
                if not layer.config.enabled:
                    continue
                if roles is not None and layer.config.role not in roles:
                    continue
                color = _ROLE_COLOR.get(layer.config.role, "#333333")
                for poly in getattr(layer.labeled.union, "geoms", [layer.labeled.union]):
                    if not poly.is_empty and poly.geom_type == "Polygon":
                        ax.add_patch(PathPatch(_poly_path(xf(poly)), facecolor=color,
                                               edgecolor="none", alpha=0.55, zorder=2))
            for h in session.holes:
                hx, hy = xp(h.x, h.y)
                ax.add_patch(MplCircle((hx, hy), h.diameter / 2, facecolor="white",
                                       edgecolor="#333", linewidth=0.3, zorder=4))
            if toolpath_items:
                segs = [[xp(x, y) for (x, y) in c] for c, _f, _p in toolpath_items]
                ax.add_collection(LineCollection(segs, colors="#d81e1e",
                                                 linewidths=0.6, zorder=5))

        # vue ajustée au PCB positionné (zoomer/dézoomer pour voir tout le plateau)
        if session.board is not None and not session.board.is_empty:
            minx, miny, maxx, maxy = xf(session.board).bounds
            m = max(maxx - minx, maxy - miny) * 0.08 + 2
            ax.set_xlim(minx - m, maxx + m)
            ax.set_ylim(miny - m, maxy + m)
        ax.set_xlabel("mm")
        ax.set_ylabel("mm")
        self.draw_idle()
