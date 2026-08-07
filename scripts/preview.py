"""Aperçu de développement : charge un jeu de Gerbers et rend un PNG.

Usage :
    python scripts/preview.py examples/module_relais out.png

Sert uniquement à valider le socle géométrique avant la GUI PySide6.
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle as MplCircle, PathPatch
from matplotlib.path import Path as MplPath

# Permet d'exécuter le script sans installer le paquet.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laserpcb import geometry as geo  # noqa: E402


def _ring_codes(coords):
    codes = [MplPath.MOVETO] + [MplPath.LINETO] * (len(coords) - 2) + [MplPath.CLOSEPOLY]
    return codes


def _polygon_to_path(poly):
    """Construit un chemin matplotlib composé : contour + trous (règle even-odd).

    Les trous deviennent de vraies zones transparentes — ils ne *peignent* rien,
    donc ils ne peuvent pas recouvrir un îlot de cuivre voisin (bug de la v1 de
    l'aperçu).
    """
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        coords = list(ring.coords)
        verts.extend(coords)
        codes.extend(_ring_codes(coords))
    return MplPath(verts, codes)


def _plot_multipolygon(ax, mp, facecolor, alpha=1.0, zorder=2):
    for poly in getattr(mp, "geoms", [mp]):
        if poly.is_empty:
            continue
        patch = PathPatch(_polygon_to_path(poly), facecolor=facecolor,
                          edgecolor="none", alpha=alpha, zorder=zorder)
        ax.add_patch(patch)


def main(folder: str, out: str):
    folder = Path(folder)
    gerber_dir = folder / "GerberFiles"
    drill_dir = folder / "DrillFiles"

    top = geo.load_copper(gerber_dir / "copper_top.gbr", "Top")
    bottom = geo.load_copper(gerber_dir / "copper_bottom.gbr", "Bottom")
    outline = geo.load_outline(gerber_dir / "profile.gbr")

    holes = []
    if drill_dir.exists():
        for xln in drill_dir.glob("*.xln"):
            holes.extend(geo.load_drill(xln))

    print(f"[i] Top    : {len(top.geometry.geoms)} polygones")
    print(f"[i] Bottom : {len(bottom.geometry.geoms)} polygones")
    print(f"[i] Contour: bounds={tuple(round(v, 2) for v in outline.bounds)}")
    print(f"[i] Trous  : {len(holes)}")

    fig, axes = plt.subplots(1, 2, figsize=(14, 8))
    for ax, layer, color, title in (
        (axes[0], top, "#111111", "TOP (vue du dessus)"),
        (axes[1], bottom, "#111111", "BOTTOM (vue du dessus)"),
    ):
        # Contour de carte
        ox, oy = outline.exterior.xy
        ax.fill(ox, oy, facecolor="#d9c27a", edgecolor="#7a6a2a",
                linewidth=1.0, zorder=1)
        # Cuivre
        _plot_multipolygon(ax, layer.geometry, facecolor=color)
        # Trous
        for h in holes:
            ax.add_patch(MplCircle((h.x, h.y), h.diameter / 2,
                                   facecolor="white", edgecolor="red",
                                   linewidth=0.4, zorder=4))
        ax.set_title(f"{title} — {layer.name}")
        ax.set_aspect("equal")
        ax.set_xlabel("mm")
        ax.set_ylabel("mm")
        ax.grid(True, linewidth=0.2, alpha=0.4)

    fig.tight_layout()
    fig.savefig(out, dpi=150)
    print(f"[ok] Aperçu écrit : {out}")


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else "examples/module_relais"
    out = sys.argv[2] if len(sys.argv) > 2 else "output/preview.png"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    main(folder, out)
