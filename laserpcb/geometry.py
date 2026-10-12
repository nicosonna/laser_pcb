"""Conversion Gerber/Excellon -> géométrie vectorielle shapely.

C'est la *source de vérité* de tout le logiciel : chaque calque cuivre devient un
ensemble de polygones exacts (pistes + pads), le contour devient un polygone de
carte, et les perçages une liste de trous (position + diamètre). À partir de là on
génère indifféremment :

  * le mode NÉGATIF (masque peinture)  -> hachures/remplissage des zones à brûler
  * le mode ISOLATION (contours)       -> offsets shapely autour des pistes

On n'interprète jamais nous-mêmes le format des coordonnées : gerbonara lit les
en-têtes (%FSLAX34Y34%, METRIC/TZ, ...) et nous rend tout en millimètres. Fini les
facteurs d'échelle codés en dur de la v1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from gerbonara import GerberFile, ExcellonFile
from shapely import box
from shapely.affinity import rotate
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

# Finesse d'approximation des cercles/arcs (nb de segments par quart de cercle).
# Discretisation des courbes : on vise un ECART A LA COURBE, pas un pas fixe.
# Un nombre de segments constant donnait 96 segments par cercle quel que soit le
# rayon, soit un pas de 0,010 mm sur un petit via (sous le pas moteur de 0,0125 mm)
# pour une fidelite de 0,08 um dont personne n a besoin.
_CURVE_TOL = 0.005        # mm : ecart maximal tolere entre le polygone et la courbe
_QUAD_SEGS = 24           # plafond (garde la compatibilite des appels existants)


def quad_segs_for(radius: float, tol: float = _CURVE_TOL,
                  lo: int = 2, hi: int = _QUAD_SEGS) -> int:
    """Segments par quadrant pour que l ecart a la courbe reste <= ``tol``.

    Sagitta d une corde : e = r (1 - cos(pi/N)) pour N segments sur le cercle.
    On inverse pour obtenir N, puis on borne (petits rayons -> peu de segments,
    gros rayons -> plafonnes pour ne pas exploser le nombre de points).
    """
    import math
    if radius <= tol:
        return lo
    n_circle = math.pi / math.acos(max(-1.0, min(1.0, 1.0 - tol / radius)))
    return int(min(hi, max(lo, math.ceil(n_circle / 4.0))))


# --------------------------------------------------------------------------- #
# Conversion d'une primitive gerbonara -> polygone shapely
# --------------------------------------------------------------------------- #
def _primitive_to_shapely(prim) -> BaseGeometry | None:
    """Traduit une primitive graphique gerbonara en géométrie shapely (en mm).

    Retourne ``None`` pour les primitives sans surface (ligne de largeur nulle).
    """
    kind = type(prim).__name__

    if kind == "Circle":
        return Point(prim.x, prim.y).buffer(prim.r, quad_segs=quad_segs_for(prim.r))

    if kind == "Line":
        if prim.width <= 0:
            return None
        # Aperture circulaire tirée d'un point à l'autre -> bouts ronds.
        line = LineString([(prim.x1, prim.y1), (prim.x2, prim.y2)])
        return line.buffer(prim.width / 2.0, cap_style="round",
                           quad_segs=quad_segs_for(prim.width / 2.0))

    if kind == "Rectangle":
        b = box(prim.x - prim.w / 2.0, prim.y - prim.h / 2.0,
                prim.x + prim.w / 2.0, prim.y + prim.h / 2.0)
        if prim.rotation:
            # gerbonara exprime la rotation en radians.
            b = rotate(b, prim.rotation, origin=(prim.x, prim.y), use_radians=True)
        return b

    if kind == "ArcPoly":
        # Régions (G36/G37) et pads polygonaux/octogonaux. On aplatit les arcs
        # éventuels puis on construit le polygone à partir du contour.
        try:
            prim = prim.approximate_arcs()
        except Exception:
            pass
        pts = [(float(x), float(y)) for x, y in prim.outline]
        if len(pts) >= 3:
            return Polygon(pts)
        return None

    # Primitive inconnue : on l'ignore silencieusement plutôt que de planter.
    return None


def _objects_to_geometry(primitives: Iterable) -> BaseGeometry:
    """Fusionne des primitives en une géométrie unique en respectant la polarité.

    Polarité *dark* = matière ajoutée (union), *clear* = matière retirée
    (différence). On traite par lots de polarité identique consécutive pour rester
    performant sur les gros calques (le cuivre bottom peut faire des centaines de
    primitives).
    """
    geom: BaseGeometry = Polygon()
    batch: list[BaseGeometry] = []
    batch_dark: bool | None = None

    def flush():
        nonlocal geom, batch
        if not batch:
            return
        merged = unary_union(batch)
        if batch_dark:
            geom = geom.union(merged)
        else:
            geom = geom.difference(merged)
        batch = []

    for prim in primitives:
        poly = _primitive_to_shapely(prim)
        if poly is None or poly.is_empty:
            continue
        dark = getattr(prim, "polarity_dark", True)
        if batch_dark is None:
            batch_dark = dark
        elif dark != batch_dark:
            flush()
            batch_dark = dark
        batch.append(poly)
    flush()

    return geom


def _normalize(geom: BaseGeometry) -> MultiPolygon:
    """Nettoie (buffer 0) et renvoie toujours un MultiPolygon."""
    if geom.is_empty:
        return MultiPolygon()
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    # GeometryCollection éventuelle : on ne garde que les surfaces.
    polys = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    return MultiPolygon(polys)


# --------------------------------------------------------------------------- #
# Modèle de données
# --------------------------------------------------------------------------- #
@dataclass
class Hole:
    x: float
    y: float
    diameter: float  # mm


@dataclass
class CopperLayer:
    """Un calque cuivre (top ou bottom) sous forme de polygones fusionnés."""
    name: str
    geometry: MultiPolygon

    @property
    def bounds(self):
        return self.geometry.bounds  # (minx, miny, maxx, maxy)


@dataclass
class LabeledCopper:
    """Cuivre d'un calque regroupé par net (ou par composante connexe à défaut).

    Structure commune aux deux formats d'entrée : ODB++ fournit les vrais nets
    (via la netlist), le Gerber retombe sur un étiquetage par composante connexe
    (chaque îlot de cuivre = une étiquette). L'isolation net-aware ne coupe qu'entre
    étiquettes différentes ; le DRC ne détecte des courts qu'avec de vrais nets.
    """
    layer: str
    net_geoms: dict[str, BaseGeometry]
    board: Polygon
    has_nets: bool = False  # True si les étiquettes sont de vrais nets (ODB++)

    @property
    def union(self) -> BaseGeometry:
        return unary_union(list(self.net_geoms.values()))


def components(geom: BaseGeometry) -> list[Polygon]:
    """Liste des polygones (composantes) d'une (multi)surface."""
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]


def label_by_component(layer: str, copper: MultiPolygon, board: Polygon) -> LabeledCopper:
    """Étiquetage par composante connexe (fallback sans netlist : Gerber)."""
    net_geoms = {f"c{i}": poly for i, poly in enumerate(components(copper))}
    return LabeledCopper(layer=layer, net_geoms=net_geoms, board=board, has_nets=False)


# --------------------------------------------------------------------------- #
# Chargement public
# --------------------------------------------------------------------------- #
def load_copper(path: str | Path, name: str | None = None) -> CopperLayer:
    """Charge un Gerber de cuivre et le convertit en polygones shapely (mm)."""
    path = Path(path)
    gf = GerberFile.open(str(path))
    prims: list = []
    for obj in gf.objects:
        prims.extend(obj.to_primitives(unit="mm"))
    geom = _normalize(_objects_to_geometry(prims))
    return CopperLayer(name=name or path.stem, geometry=geom)


def load_pads(path: str | Path) -> MultiPolygon:
    """Zones de soudure d'un Gerber cuivre : les **flashs d'aperture** (pads).

    Les pistes sont des tracés (Line/Arc) ; les pads sont des flashs. Sert au job
    de finition (après perchlorure) : retirer la peinture là où l'on soude.
    """
    gf = GerberFile.open(str(path))
    prims: list = []
    for obj in gf.objects:
        if type(obj).__name__ == "Flash":
            prims.extend(obj.to_primitives(unit="mm"))
    return _normalize(_objects_to_geometry(prims))


def load_outline(path: str | Path) -> Polygon:
    """Charge le contour de carte (profile.gbr) en un polygone fermé.

    Le profil est tracé comme des segments : on prend l'enveloppe de leur union et
    on remplit l'intérieur pour obtenir la surface de la carte.
    """
    path = Path(path)
    gf = GerberFile.open(str(path))
    lines = []
    for obj in gf.objects:
        for prim in obj.to_primitives(unit="mm"):
            if type(prim).__name__ == "Line":
                lines.append(LineString([(prim.x1, prim.y1), (prim.x2, prim.y2)]))
    if not lines:
        # Repli : rectangle de la bounding box.
        (minx, miny), (maxx, maxy) = gf.bounding_box()
        return box(minx, miny, maxx, maxy)
    merged = unary_union(lines)
    # polygonize via buffer minuscule pour fermer, puis on prend l'enveloppe pleine.
    from shapely.ops import polygonize
    faces = list(polygonize(merged))
    if faces:
        return unary_union(faces)
    (minx, miny), (maxx, maxy) = gf.bounding_box()
    return box(minx, miny, maxx, maxy)


def load_drill(path: str | Path) -> list[Hole]:
    """Charge un fichier de perçage Excellon en liste de trous (position + Ø, mm)."""
    path = Path(path)
    ef = ExcellonFile.open(str(path))
    holes: list[Hole] = []
    for obj in ef.objects:
        for prim in obj.to_primitives(unit="mm"):
            if type(prim).__name__ == "Circle":
                holes.append(Hole(x=prim.x, y=prim.y, diameter=prim.r * 2.0))
    return holes


def load_labeled_copper(copper_path: str | Path, outline_path: str | Path,
                        name: str | None = None) -> LabeledCopper:
    """Gerber -> LabeledCopper (étiquetage par composante connexe, sans netlist)."""
    layer = name or Path(copper_path).stem
    copper = load_copper(copper_path, layer)
    board = load_outline(outline_path)
    return label_by_component(layer, copper.geometry, board)
