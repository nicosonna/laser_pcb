"""Objets dessinés : formes, lignes et texte posés par-dessus (ou sans) un PCB.

Modèle PUR — aucune dépendance à Qt, pour qu'il se teste sans interface.

Deux principes, décidés dans ``documentation/dev/editeur-vectoriel.md`` :

1. **Les objets sont paramétriques.** Un rectangle est ``(x, y, w, h)`` et non
   une liste de points ; la géométrie shapely en est *dérivée*. Sans cela un
   texte ne serait plus rééditable après avoir été posé, et chaque
   redimensionnement accumulerait des erreurs d'arrondi.
2. **Les réglages vivent sur le CALQUE**, façon LightBurn : vitesse, puissance,
   passes et mode (trait / remplissage) appartiennent au calque de travail, pas
   à l'objet. Trente objets se règlent alors d'un seul geste.

Ne pas confondre ces *calques de travail* avec les *couches source* du Gerber
(cuivre, sérigraphie), qui sont un tout autre objet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from shapely.affinity import rotate as _rotate
from shapely.geometry import (LineString, MultiLineString, MultiPolygon, Point,
                              Polygon)
from shapely.geometry.base import BaseGeometry

# Modes de gravure d'un calque
MODE_LINE = "trait"
MODE_FILL = "remplissage"
MODE_BOTH = "trait+remplissage"
MODES = (MODE_LINE, MODE_FILL, MODE_BOTH)

# Types d'objets
KIND_RECT = "rect"
KIND_ELLIPSE = "ellipse"
KIND_POLYLINE = "polyline"
KIND_TEXT = "texte"
KIND_PATH = "trace"          # géométrie figée (import SVG/DXF, plus tard)
KINDS = (KIND_RECT, KIND_ELLIPSE, KIND_POLYLINE, KIND_TEXT, KIND_PATH)

# Palette par défaut des calques de travail : des teintes franches, distinctes
# des couleurs de parcours (bleu contour, rouge remplissage, vert trous…).
_PALETTE = ("#e8302a", "#1a9e4b", "#1565d8", "#e58b00", "#8e24aa", "#00868b")


@dataclass
class WorkLayer:
    """Calque de travail : une couleur, un jeu de réglages laser."""
    id: str
    name: str
    color: str = _PALETTE[0]
    speed: float = 1500.0        # mm/min
    power: float = 30.0          # %
    passes: int = 1
    mode: str = MODE_LINE
    enabled: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "color": self.color,
                "speed": self.speed, "power": self.power, "passes": self.passes,
                "mode": self.mode, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, d: dict) -> "WorkLayer":
        return cls(id=str(d.get("id", "c1")), name=str(d.get("name", "Calque")),
                   color=str(d.get("color", _PALETTE[0])),
                   speed=float(d.get("speed", 1500.0)),
                   power=float(d.get("power", 30.0)),
                   passes=int(d.get("passes", 1)),
                   mode=str(d.get("mode", MODE_LINE)),
                   enabled=bool(d.get("enabled", True)))


@dataclass
class Shape:
    """Objet dessiné. ``params`` dépend de ``kind`` :

    - ``rect``     : x, y, w, h          (coin bas-gauche + dimensions)
    - ``ellipse``  : cx, cy, rx, ry      (centre + demi-axes)
    - ``polyline`` : points [[x, y], …], closed (bool)
    - ``texte``    : x, y, text, height  (coin bas-gauche, hauteur en mm)
    - ``trace``    : wkt                 (géométrie figée, pour l'import)

    ``rotation`` est en degrés, autour du centre de l'emprise.
    """
    kind: str
    params: dict
    layer: str = "c1"
    rotation: float = 0.0
    id: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "params": dict(self.params),
                "layer": self.layer, "rotation": self.rotation, "id": self.id}

    @classmethod
    def from_dict(cls, d: dict) -> "Shape":
        return cls(kind=str(d.get("kind", KIND_RECT)),
                   params=dict(d.get("params", {})),
                   layer=str(d.get("layer", "c1")),
                   rotation=float(d.get("rotation", 0.0)),
                   id=str(d.get("id", "")))

    # ---- géométrie dérivée --------------------------------------------------- #
    def geometry(self) -> BaseGeometry:
        """Géométrie shapely de l'objet, rotation comprise.

        Surface fermée -> ``Polygon``/``MultiPolygon`` ; trait ouvert ->
        ``LineString``. C'est cette distinction qui décide si un remplissage a
        un sens.
        """
        g = self._raw()
        if self.rotation and not g.is_empty:
            g = _rotate(g, self.rotation, origin="center", use_radians=False)
        return g

    def _raw(self) -> BaseGeometry:
        p = self.params
        k = self.kind
        if k == KIND_RECT:
            x, y = float(p.get("x", 0.0)), float(p.get("y", 0.0))
            w, h = float(p.get("w", 0.0)), float(p.get("h", 0.0))
            if w <= 0 or h <= 0:
                return Polygon()
            return Polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
        if k == KIND_ELLIPSE:
            cx, cy = float(p.get("cx", 0.0)), float(p.get("cy", 0.0))
            rx, ry = float(p.get("rx", 0.0)), float(p.get("ry", 0.0))
            if rx <= 0 or ry <= 0:
                return Polygon()
            # Discrétisation guidée par la FLÈCHE (5 µm), comme le reste du
            # projet : un nombre de segments fixe donnerait des facettes
            # visibles sur un grand cercle et du gaspillage sur un petit.
            from .geometry import quad_segs_for
            n = max(12, quad_segs_for(max(rx, ry)) * 4)
            pts = [(cx + rx * math.cos(2 * math.pi * i / n),
                    cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]
            return Polygon(pts)
        if k == KIND_POLYLINE:
            pts = [(float(a), float(b)) for a, b in p.get("points", [])]
            if len(pts) < 2:
                return LineString()
            if p.get("closed") and len(pts) >= 3:
                return Polygon(pts)
            return LineString(pts)
        if k == KIND_TEXT:
            from . import toolpath as tp
            return tp.text_polygons(str(p.get("text", "")),
                                    float(p.get("height", 3.0)),
                                    float(p.get("x", 0.0)), float(p.get("y", 0.0)))
        if k == KIND_PATH:
            from shapely import wkt as _wkt
            try:
                return _wkt.loads(str(p.get("wkt", "")))
            except Exception:  # noqa: BLE001
                return Polygon()
        return Polygon()

    def bounds(self):
        g = self.geometry()
        return None if g.is_empty else g.bounds


# --------------------------------------------------------------------------- #
#  Parcours laser
# --------------------------------------------------------------------------- #
def _outline_lines(geom: BaseGeometry) -> list:
    """Contours d'une géométrie, qu'elle soit surface ou trait."""
    out = []
    for g in getattr(geom, "geoms", [geom]):
        if g.is_empty:
            continue
        if isinstance(g, Polygon):
            out.append(LineString(g.exterior.coords))
            out.extend(LineString(r.coords) for r in g.interiors)
        elif isinstance(g, (LineString, MultiLineString)):
            out.extend(getattr(g, "geoms", [g]))
    return out


def shape_items(shape: Shape, layer: WorkLayer, kerf: float = 0.12) -> list:
    """Parcours ``(coords, vitesse, puissance)`` d'un objet, selon son calque.

    Le mode du calque décide : trait (le contour), remplissage (hachures au pas
    du kerf) ou les deux. Un objet OUVERT n'a pas de remplissage possible : on
    se rabat sur le trait plutôt que de ne rien graver.
    """
    from . import gcode as gc
    from . import toolpath as tp

    geom = shape.geometry()
    if geom is None or geom.is_empty:
        return []
    surface = any(isinstance(g, Polygon) for g in getattr(geom, "geoms", [geom]))
    mode = layer.mode if (surface or layer.mode == MODE_LINE) else MODE_LINE

    items = []
    if mode in (MODE_FILL, MODE_BOTH):
        lines = tp.hatch(geom, spacing=max(0.01, kerf), angle=0.0, outline=False)
        items += gc.items_from_paths(lines, layer.speed, layer.power)
    if mode in (MODE_LINE, MODE_BOTH):
        items += gc.items_from_paths(_outline_lines(geom), layer.speed, layer.power)
    return items * max(1, layer.passes)


def translate_shape(shape: Shape, dx: float, dy: float) -> None:
    """Déplace l'objet en modifiant ses PARAMÈTRES (il reste paramétrique)."""
    p, k = shape.params, shape.kind
    if k == KIND_RECT or k == KIND_TEXT:
        p["x"] = float(p.get("x", 0.0)) + dx
        p["y"] = float(p.get("y", 0.0)) + dy
    elif k == KIND_ELLIPSE:
        p["cx"] = float(p.get("cx", 0.0)) + dx
        p["cy"] = float(p.get("cy", 0.0)) + dy
    elif k == KIND_POLYLINE:
        p["points"] = [[float(a) + dx, float(b) + dy] for a, b in p.get("points", [])]
    elif k == KIND_PATH:
        from shapely import wkt as _wkt
        from shapely.affinity import translate as _tr
        try:
            p["wkt"] = _tr(_wkt.loads(str(p.get("wkt", ""))), dx, dy).wkt
        except Exception:  # noqa: BLE001
            pass


def scale_shape(shape: Shape, sx: float, sy: float,
                ox: float = 0.0, oy: float = 0.0) -> None:
    """Redimensionne autour de ``(ox, oy)``, en PARAMÈTRES là encore.

    Un texte n'a qu'une hauteur : une mise à l'échelle non uniforme n'est pas
    représentable sans le dénaturer, on applique donc ``sy`` à sa hauteur. Le
    jour où l'on voudra des textes étirés, il faudra un paramètre de largeur —
    pas un contournement ici.
    """
    sx = sx if abs(sx) > 1e-9 else 1e-9
    sy = sy if abs(sy) > 1e-9 else 1e-9
    p, k = shape.params, shape.kind

    def fx(v):
        return ox + (float(v) - ox) * sx

    def fy(v):
        return oy + (float(v) - oy) * sy

    if k == KIND_RECT:
        x0, y0 = fx(p.get("x", 0.0)), fy(p.get("y", 0.0))
        x1 = fx(float(p.get("x", 0.0)) + float(p.get("w", 0.0)))
        y1 = fy(float(p.get("y", 0.0)) + float(p.get("h", 0.0)))
        p["x"], p["y"] = min(x0, x1), min(y0, y1)
        p["w"], p["h"] = abs(x1 - x0), abs(y1 - y0)
    elif k == KIND_ELLIPSE:
        p["cx"], p["cy"] = fx(p.get("cx", 0.0)), fy(p.get("cy", 0.0))
        p["rx"] = abs(float(p.get("rx", 0.0)) * sx)
        p["ry"] = abs(float(p.get("ry", 0.0)) * sy)
    elif k == KIND_POLYLINE:
        p["points"] = [[fx(a), fy(b)] for a, b in p.get("points", [])]
    elif k == KIND_TEXT:
        p["x"], p["y"] = fx(p.get("x", 0.0)), fy(p.get("y", 0.0))
        p["height"] = max(0.1, abs(float(p.get("height", 3.0)) * sy))
    elif k == KIND_PATH:
        from shapely import wkt as _wkt
        from shapely.affinity import scale as _sc
        try:
            g = _wkt.loads(str(p.get("wkt", "")))
            p["wkt"] = _sc(g, xfact=sx, yfact=sy, origin=(ox, oy)).wkt
        except Exception:  # noqa: BLE001
            pass


def default_layers() -> list:
    """Jeu de départ : un calque de trait, un de remplissage.

    Deux suffisent pour travailler tout de suite, et montrent la différence de
    mode sans qu'il faille ouvrir un réglage.
    """
    return [
        WorkLayer(id="c1", name="Trait", color=_PALETTE[0],
                  speed=1500.0, power=30.0, mode=MODE_LINE),
        WorkLayer(id="c2", name="Remplissage", color=_PALETTE[2],
                  speed=1200.0, power=35.0, mode=MODE_FILL),
    ]


def next_layer_color(existing: list) -> str:
    """Couleur suivante de la palette, en évitant celles déjà prises."""
    used = {l.color for l in existing}
    for c in _PALETTE:
        if c not in used:
            return c
    return _PALETTE[len(existing) % len(_PALETTE)]


def new_id(prefix: str, existing) -> str:
    """Identifiant court et unique (``o1``, ``o2``…)."""
    taken = {getattr(e, "id", e) for e in existing}
    i = 1
    while f"{prefix}{i}" in taken:
        i += 1
    return f"{prefix}{i}"
