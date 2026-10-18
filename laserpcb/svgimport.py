"""Import SVG : un dessin fait ailleurs devient des objets gravables.

Modèle PUR (aucun Qt). C'est la voie d'entrée des logos — ils ne se dessinent
pas aux primitives, ils viennent d'un fichier.

Trois conversions à faire, et chacune est un piège classique :

1. **Les unités.** Le SVG raisonne en « user units » ; la machine en
   millimètres. Un point vaut 25,4/96 mm à 96 ppp, et ``svgelements`` applique
   déjà ``width``/``height``/``viewBox`` pour nous.
2. **L'axe Y.** Le SVG descend, la machine monte. Sans retournement, tout
   arrive en miroir vertical.
3. **Les trous.** Le « O » d'un logo est un contour EXTÉRIEUR plus un contour
   intérieur. La règle pair-impair se rend exactement par des différences
   symétriques successives — c'est plus court et plus sûr que de deviner qui
   contient qui.
"""

from __future__ import annotations

import math
from pathlib import Path as _FsPath

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from . import drawing as dr

MM_PER_PX = 25.4 / 96.0          # 96 points par pouce, défaut du SVG
_SAG = 0.01                      # flèche tolérée à la discrétisation (mm)


def _flatten(path, step_px: float) -> tuple:
    """Chemin svgelements -> (anneaux fermés, tracés ouverts), en points SVG."""
    rings, lines, cur = [], [], []

    def close(force: bool):
        if len(cur) >= 2:
            if force:
                if cur[0] != cur[-1]:
                    cur.append(cur[0])
                if len(cur) >= 4:
                    rings.append(list(cur))
            else:
                lines.append(list(cur))

    for seg in path.segments():
        kind = type(seg).__name__
        if kind == "Move":
            close(False)
            cur = [(seg.end.x, seg.end.y)]
        elif kind == "Close":
            close(True)
            cur = []
        elif kind == "Line":
            if seg.end is not None:
                cur.append((seg.end.x, seg.end.y))
        elif seg.end is not None:                   # Arc, Bézier cubique/quadratique
            try:
                length = float(seg.length(error=1e-3))
            except Exception:  # noqa: BLE001
                length = math.hypot(seg.end.x - seg.start.x, seg.end.y - seg.start.y)
            n = max(2, min(400, int(length / max(step_px, 1e-6)) + 1))
            for i in range(1, n + 1):
                p = seg.point(i / n)
                cur.append((p.x, p.y))
    close(False)
    return rings, lines


def _even_odd(rings: list) -> object:
    """Anneaux -> surface, règle PAIR-IMPAIR (les trous se creusent tout seuls)."""
    out = None
    for ring in rings:
        try:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)       # auto-intersections : on régularise
        except Exception:  # noqa: BLE001
            continue
        if poly.is_empty:
            continue
        out = poly if out is None else out.symmetric_difference(poly)
    return out


def read_svg(path, ppi: float = 96.0) -> list:
    """Lit un SVG et renvoie une liste de géométries shapely, en MILLIMÈTRES.

    Repère de sortie : coin bas-gauche de l'ensemble à (0, 0), Y vers le haut.
    """
    from svgelements import SVG, Shape as SvgShape

    svg = SVG.parse(str(path), ppi=ppi, reify=True)
    step_px = max(_SAG / MM_PER_PX, 1e-3)

    geoms, lines = [], []
    for el in svg.elements():
        if not isinstance(el, SvgShape):
            continue
        try:
            from svgelements import Path as SvgPath
            p = SvgPath(el)
        except Exception:  # noqa: BLE001
            continue
        if len(p) == 0:
            continue
        rings, open_lines = _flatten(p, step_px)
        surf = _even_odd(rings)
        if surf is not None and not surf.is_empty:
            geoms.append(surf)
        for coords in open_lines:
            if len(coords) >= 2:
                lines.append(LineString(coords))

    allg = geoms + lines
    if not allg:
        return []

    # --- repère : millimètres, Y vers le haut, origine en bas à gauche ------- #
    minx = min(g.bounds[0] for g in allg)
    maxy = max(g.bounds[3] for g in allg)

    from shapely.affinity import affine_transform
    k = MM_PER_PX * (96.0 / ppi) if ppi else MM_PER_PX
    # x' = (x - minx) * k ; y' = (maxy - y) * k  -> retournement vertical compris
    mat = [k, 0.0, 0.0, -k, -minx * k, maxy * k]
    out = []
    for g in allg:
        t = affine_transform(g, mat)
        if not t.is_empty:
            out.append(t)
    return out


def shapes_from_svg(path, layer_id: str = "c1", existing: list | None = None,
                    target_width: float | None = None,
                    at: tuple = (0.0, 0.0)) -> list:
    """Objets ``trace`` prêts à poser, mis à l'échelle et positionnés.

    :param target_width: largeur voulue en mm (``None`` = taille naturelle).
    :param at: coin bas-gauche visé, en coordonnées modèle.
    """
    geoms = read_svg(path)
    if not geoms:
        return []
    merged = unary_union(geoms)
    minx, miny, maxx, maxy = merged.bounds
    w = maxx - minx
    scale = (target_width / w) if (target_width and w > 1e-9) else 1.0

    from shapely.affinity import affine_transform
    mat = [scale, 0.0, 0.0, scale, at[0], at[1]]

    shapes, taken = [], list(existing or [])
    for g in geoms:
        t = affine_transform(g, mat)
        if t.is_empty:
            continue
        s = dr.Shape(dr.KIND_PATH, {"wkt": t.wkt}, layer=layer_id,
                     id=dr.new_id("o", taken))
        taken.append(s)
        shapes.append(s)
    return shapes


def natural_size(path) -> tuple:
    """Dimensions du dessin en mm, telles qu'il est écrit dans le fichier."""
    geoms = read_svg(path)
    if not geoms:
        return (0.0, 0.0)
    g = unary_union(geoms)
    minx, miny, maxx, maxy = g.bounds
    return (maxx - minx, maxy - miny)


def is_svg(path) -> bool:
    return _FsPath(str(path)).suffix.lower() == ".svg"
