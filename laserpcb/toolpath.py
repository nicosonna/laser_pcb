"""Génération des parcours laser depuis la géométrie cuivre (shapely).

Deux stratégies, sélectionnables par calque (le toggle « positif/négatif ») :

  * ISOLATION : on trace un ou plusieurs anneaux autour du cuivre. Le laser creuse
    un sillon qui sépare électriquement les pistes. Pas de chimie. -> contours.

  * NÉGATIF (masque peinture) : on brûle toute la zone *sans* cuivre (carte − cuivre)
    pour retirer la peinture des zones à graver chimiquement. -> remplissage hachuré.

Tout est en millimètres. Les sorties sont des géométries shapely (LineString /
MultiLineString / MultiPolygon) que le générateur de G-code consommera ensuite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import Voronoi, cKDTree
from shapely import segmentize
from shapely.affinity import rotate
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geometry import LabeledCopper, components


# --------------------------------------------------------------------------- #
# ISOLATION — anneaux de contour autour du cuivre
# --------------------------------------------------------------------------- #
def isolation_rings(copper: BaseGeometry, kerf: float = 0.15, rings: int = 1) -> MultiLineString:
    """Génère les contours d'isolation autour du cuivre.

    :param copper:  géométrie cuivre (MultiPolygon).
    :param kerf:    largeur du trait laser en mm (déplace le centre du faisceau
                    de kerf/2 hors du cuivre pour ne pas l'entamer).
    :param rings:   nombre d'anneaux concentriques (1 = isolation minimale ;
                    2-3 = séparation plus large).
    :return:        MultiLineString des trajectoires à graver.
    """
    lines: list[LineString] = []
    for i in range(rings):
        distance = kerf / 2.0 + i * kerf
        offset = copper.buffer(distance, join_style="round")
        lines.extend(_boundaries(offset))
    return MultiLineString(lines) if lines else MultiLineString()


def _boundaries(geom: BaseGeometry) -> list[LineString]:
    """Extrait tous les contours (extérieurs + trous) d'une (multi)surface."""
    out: list[LineString] = []
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    for poly in polys:
        if poly.is_empty:
            continue
        out.append(LineString(poly.exterior.coords))
        for ring in poly.interiors:
            out.append(LineString(ring.coords))
    return out


# --------------------------------------------------------------------------- #
# NÉGATIF — zone à brûler puis remplissage hachuré
# --------------------------------------------------------------------------- #
def negative_region(copper: BaseGeometry, board: Polygon,
                    keepout: float = 0.0) -> MultiPolygon:
    """Zone à ablater en mode masque : la carte moins le cuivre.

    :param keepout: marge conservée autour du cuivre (mm). Élargit virtuellement
                    les pistes pour garantir qu'elles survivent à la gravure.
    """
    protected = copper.buffer(keepout) if keepout else copper
    region = board.difference(protected)
    return _as_multipolygon(region)


def hatch(region: BaseGeometry, spacing: float = 0.12,
          angle: float = 0.0) -> MultiLineString:
    """Remplit une surface de lignes parallèles (balayage laser).

    :param spacing: pas entre lignes en mm (typiquement ~kerf pour un recouvrement
                    complet).
    :param angle:   orientation des hachures en degrés.
    """
    if region.is_empty:
        return MultiLineString()

    # On travaille dans un repère où les hachures sont horizontales : on tourne la
    # surface de -angle, on coupe des lignes horizontales, puis on retourne.
    origin = region.centroid
    work = rotate(region, -angle, origin=origin, use_radians=False)
    minx, miny, maxx, maxy = work.bounds

    lines: list[LineString] = []
    y = miny + spacing / 2.0
    while y <= maxy:
        scan = LineString([(minx - 1, y), (maxx + 1, y)])
        clipped = work.intersection(scan)
        _collect_lines(clipped, lines)
        y += spacing

    if not lines:
        return MultiLineString()
    merged = rotate(MultiLineString(lines), angle, origin=origin, use_radians=False)
    if merged.geom_type == "LineString":
        return MultiLineString([merged])
    return merged


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _collect_lines(geom: BaseGeometry, out: list[LineString]) -> None:
    if geom.is_empty:
        return
    if geom.geom_type == "LineString":
        if geom.length > 0:
            out.append(geom)
    elif geom.geom_type in ("MultiLineString", "GeometryCollection"):
        for g in geom.geoms:
            _collect_lines(g, out)


def _as_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    if geom.is_empty:
        return MultiPolygon()
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    return MultiPolygon(polys)


@dataclass
class LayerJob:
    """Un calque prêt à graver : sa géométrie + son mode + ses réglages laser."""
    name: str
    mode: str  # "isolation" | "negative"
    paths: MultiLineString
    passes: int = 1
    speed: float = 1000.0   # mm/min
    power: float = 100.0    # %


# --------------------------------------------------------------------------- #
# ISOLATION NET-AWARE (axe médian) + paramètres adaptatifs
# --------------------------------------------------------------------------- #
@dataclass
class Calibration:
    """Réglages laser du Falcon 2, interpolés selon la clairance locale."""
    kerf: float = 0.15
    v_fast: float = 1500.0   # mm/min en zone large
    v_slow: float = 300.0    # mm/min en zone serrée
    p_full: float = 100.0    # % en zone large
    p_soft: float = 78.0     # % en zone serrée
    c_open: float = 0.60     # clairance considérée "large" (mm)

    @property
    def c_tight(self) -> float:
        return self.kerf / 2.0

    def params(self, clearance: float) -> tuple[float, float]:
        """clairance (demi-largeur du canal, mm) -> (vitesse mm/min, puissance %)."""
        span = max(self.c_open - self.c_tight, 1e-6)
        t = min(1.0, max(0.0, (clearance - self.c_tight) / span))
        return (self.v_slow + t * (self.v_fast - self.v_slow),
                self.p_soft + t * (self.p_full - self.p_soft))


@dataclass
class Segment:
    """Un tronçon d'axe médian + la clairance mesurée (pilote vitesse/puissance)."""
    coords: list[tuple[float, float]]
    clearance: float

    def geometry(self) -> LineString:
        return LineString(self.coords)

    def is_subkerf(self, kerf: float) -> bool:
        """Canal plus étroit que le faisceau -> le laser rognera le cuivre."""
        return 2 * self.clearance < kerf


def centerline_isolation(labeled: LabeledCopper, kerf: float = 0.15,
                         step: float = 0.08) -> list[Segment]:
    """Axe médian net-aware : une passe centrée entre étiquettes différentes.

    Ne coupe jamais à l'intérieur d'un même net (ponts/spokes conservés). Chaque
    segment porte sa clairance locale, d'où sa vitesse/puissance adaptatives.

    Perf : le vide (carte − cuivre) est calculé une fois, et **tous** les segments
    candidats sont clippés en **un seul** appel GEOS (``MultiLineString ∩ gap``) au
    lieu d'une intersection par segment — ~30× plus rapide sur une carte complète.
    """
    names = list(labeled.net_geoms)
    pts: list[tuple[float, float]] = []
    lab: list[int] = []
    for li, net in enumerate(names):
        for poly in components(labeled.net_geoms[net]):
            dense = segmentize(poly, step)
            for ring in [dense.exterior, *dense.interiors]:
                for x, y in list(ring.coords)[:-1]:
                    pts.append((x, y)); lab.append(li)
    if len(pts) < 4:
        return []

    pts_arr = np.asarray(pts)
    lab_arr = np.asarray(lab)
    vor = Voronoi(pts_arr)
    tree = cKDTree(pts_arr)
    V = vor.vertices

    # arêtes de Voronoï séparant deux nets différents (bornes finies)
    ridges = [(v0, v1) for (a, b), (v0, v1)
              in zip(vor.ridge_points, vor.ridge_vertices)
              if lab_arr[a] != lab_arr[b] and v0 >= 0 and v1 >= 0]
    if not ridges:
        return []

    # clip GROUPÉ au vide : une seule intersection pour tous les segments
    candidate = MultiLineString([LineString([V[v0], V[v1]]) for v0, v1 in ridges])
    gap = labeled.board.difference(labeled.union)
    lines = _explode_lines(candidate.intersection(gap))
    if not lines:
        return []

    # clairance par segment : distance au cuivre mesurée au MILIEU du tronçon
    # (les extrémités clippées touchent le cuivre -> clairance nulle trompeuse)
    mids = np.array([line.interpolate(0.5, normalized=True).coords[0] for line in lines])
    clr, _ = tree.query(mids)
    return [Segment(coords=list(line.coords), clearance=float(c))
            for line, c in zip(lines, clr)]


def _explode_lines(geom: BaseGeometry) -> list[LineString]:
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom] if geom.length > 0 else []
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        out = []
        for g in geom.geoms:
            out.extend(_explode_lines(g))
        return out
    return []


# --------------------------------------------------------------------------- #
# DRC — courts-circuits (inter-nets) et canaux sub-kerf
# --------------------------------------------------------------------------- #
@dataclass
class Short:
    net_a: str
    net_b: str
    x: float
    y: float
    area: float


@dataclass
class DRCResult:
    shorts: list[Short] = field(default_factory=list)
    subkerf: int = 0
    min_clearance: float = float("inf")

    @property
    def ok(self) -> bool:
        return not self.shorts and self.subkerf == 0


def drc(labeled: LabeledCopper, segments: list[Segment] | None = None,
        kerf: float = 0.15, min_short_area: float = 0.004) -> DRCResult:
    """Contrôle avant gravure : nets différents en contact + canaux trop étroits.

    :param min_short_area: aire de recouvrement minimale (mm²) pour qualifier un
        court-circuit. En dessous, c'est un simple contact ponctuel (tessellation
        des courbes) — écarté pour éviter les faux positifs.
    """
    res = DRCResult()

    if labeled.has_nets:
        nets = [n for n in labeled.net_geoms if not n.startswith("$NONE$")]
        geoms = {n: labeled.net_geoms[n] for n in nets}
        bounds = {n: geoms[n].bounds for n in nets}
        for i in range(len(nets)):
            for j in range(i + 1, len(nets)):
                na, nb = nets[i], nets[j]
                if not _bbox_touch(bounds[na], bounds[nb]):
                    continue
                inter = geoms[na].intersection(geoms[nb])
                # Vrai court = recouvrement 2D significatif (pas un point/segment).
                if inter.area >= min_short_area:
                    c = inter.centroid
                    res.shorts.append(Short(na, nb, c.x, c.y, inter.area))

    if segments:
        res.subkerf = sum(1 for s in segments if s.is_subkerf(kerf))
        res.min_clearance = min(s.clearance for s in segments)
    return res


def _bbox_touch(a, b, tol: float = 1e-6) -> bool:
    return not (a[2] < b[0] - tol or b[2] < a[0] - tol
                or a[3] < b[1] - tol or b[3] < a[1] - tol)
