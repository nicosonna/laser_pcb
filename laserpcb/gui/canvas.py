"""Canvas d'aperçu : figure matplotlib intégrée dans Qt."""

from __future__ import annotations

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.collections import EllipseCollection, LineCollection
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


def _compound_path(polys, transform=None, tol=0.03):
    """UN seul chemin matplotlib pour TOUS les polygones (bien plus rapide que N
    patches). ``transform`` applique miroir/offset ; ``tol`` simplifie pour l'affichage."""
    verts, codes = [], []
    for poly in polys:
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        if transform is not None:
            poly = transform(poly)
        if tol:
            poly = poly.simplify(tol, preserve_topology=False)
        if poly.is_empty:
            continue
        for ring in [poly.exterior, *poly.interiors]:
            c = list(ring.coords)
            if len(c) < 3:
                continue
            verts.extend(c)
            codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(c) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(verts, codes) if verts else None


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
                       mirror=False, burn=False, burn_geom=None):
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
            for poly in getattr(b, "geoms", [b]):   # contour éventuellement multi (carte panélisée)
                bx, by = poly.exterior.xy
                ax.fill(bx, by, facecolor="#efe7c8", edgecolor="#7a6a2a", linewidth=1, zorder=1)

        if burn:
            # Aperçu brûlure : zone gravée (déjà unionée dans le thread), en noir.
            if burn_geom is not None and not burn_geom.is_empty:
                merged = xf(burn_geom)
                path = _compound_path(getattr(merged, "geoms", [merged]), tol=0.02)
                if path is not None:
                    ax.add_patch(PathPatch(path, facecolor="black", edgecolor="none", zorder=3))
        else:
            for layer in session.layers:
                if not layer.config.enabled:
                    continue
                if roles is not None and layer.config.role not in roles:
                    continue
                color = _ROLE_COLOR.get(layer.config.role, "#333333")
                # transform + simplify sur TOUTE l'union en une fois (rapide), puis 1 patch
                u = xf(layer.labeled.union).simplify(0.03, preserve_topology=False)
                path = _compound_path(getattr(u, "geoms", [u]), tol=0)
                if path is not None:
                    ax.add_patch(PathPatch(path, facecolor=color, edgecolor="none",
                                           alpha=0.55, zorder=2))
            if session.holes:                       # tous les trous en UNE collection
                import numpy as np
                offs = np.array([xp(h.x, h.y) for h in session.holes])
                diam = np.array([h.diameter for h in session.holes])
                ax.add_collection(EllipseCollection(
                    diam, diam, np.zeros_like(diam), units="xy", offsets=offs,
                    offset_transform=ax.transData, facecolors="white",
                    edgecolors="#333", linewidths=0.3, zorder=4))
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
