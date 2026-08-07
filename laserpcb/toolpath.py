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


def hole_circles(holes, scale_percent: float = -30.0, min_diameter: float = 0.15,
                 quad_segs: int = 16) -> MultiLineString:
    """Contours de perçage à graver, diamètre ajusté par un pourcentage.

    Un gros trou gravé à son diamètre exact risque de couper une piste voisine ;
    un pourcentage négatif le rétrécit (et réduit le temps de coupe). ``scale_percent``
    = -30 → cercle à 70 % du Ø nominal (comportement de la v1).
    """
    from shapely.geometry import Point
    lines = []
    for h in holes:
        d = max(h.diameter * (1 + scale_percent / 100.0), min_diameter)
        ring = Point(h.x, h.y).buffer(d / 2.0, quad_segs=quad_segs).exterior
        lines.append(LineString(ring.coords))
    return MultiLineString(lines)


def clearing_zones(region: BaseGeometry, copper: BaseGeometry,
                   margin: float) -> tuple[BaseGeometry, BaseGeometry]:
    """Découpe la zone à évider en un liseré près du cuivre et un intérieur ouvert.

    Permet de graver **lentement et proprement près des pistes** (le liseré, largeur
    ``margin``) et **rapidement au milieu des grandes zones** (l'intérieur), tout en
    couvrant toute la surface à retirer.

    :return: (liseré_près_du_cuivre, intérieur_ouvert)
    """
    grown = copper.buffer(margin)
    edge = region.intersection(grown)
    interior = region.difference(grown)
    return edge, interior


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
    """Réglages laser interpolés selon la clairance locale.

    Défauts calés sur le **Creality Falcon 2 22 W** (spot ~0,08×0,1 mm, diode) :
    point de départ raisonnable pour de la gravure PCB, à affiner sur plaque test.
    """
    kerf: float = 0.10       # spot focalisé du Falcon 2 22 W (~0,08–0,1 mm)
    v_fast: float = 3000.0   # mm/min en zone large
    v_slow: float = 600.0    # mm/min en zone serrée
    p_full: float = 100.0    # % en zone large
    p_soft: float = 60.0     # % en zone serrée
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


def combined_isolation(labeled: LabeledCopper, board, kerf: float = 0.10,
                       tight_factor: float = 3.0, step: float = 0.08):
    """Isolation + évidement, avec **une seule passe** entre pistes/pads proches.

    - Gaps serrés (clairance ≤ ``tight_factor*kerf/2``) : **passe unique centrée**
      (un seul trait de laser entre deux cuivres, pas deux).
    - Reste des zones sans cuivre : **évidement** (à hachurer par le caller).

    S'adapte au design : sur une carte à plan de masse, seules les fines clairances
    sont serrées → passes uniques (isolation) ; sur une carte "pistes seules", les
    grandes zones sont évidées.

    :return: (passes_uniques: list[Segment], zone_à_évider: MultiPolygon)
    """
    union = labeled.union
    region = board.difference(union)                # tout le non-cuivre
    tight_clear = tight_factor * kerf / 2.0

    segs = centerline_isolation(labeled, kerf=kerf, step=step)
    tight = [s for s in segs if s.clearance <= tight_clear]

    if tight:
        covered = unary_union([LineString(s.coords).buffer(tight_clear + kerf)
                               for s in tight])
        wide = region.difference(covered)           # zones larges hors passes serrées
    else:
        wide = region
    return tight, _as_multipolygon(wide)


def expand_copper(labeled: LabeledCopper, margin: float,
                  min_gap: float) -> LabeledCopper:
    """Élargit chaque net de ``margin`` **sans jamais ponter un autre net**.

    Chaque net grandit dans l'espace libre, mais reste à ≥ ``min_gap`` (le kerf) de
    tout autre net → aucun pont, quelle que soit la marge (le fine-pitch est
    automatiquement limité). Un net ne devient jamais plus petit que l'original.
    """
    from shapely import STRtree

    if margin <= 0:
        return labeled
    names = list(labeled.net_geoms)
    geoms = [labeled.net_geoms[n] for n in names]
    tree = STRtree(geoms)
    out: dict = {}
    for i, (name, gi) in enumerate(zip(names, geoms)):
        near = [geoms[int(j)] for j in tree.query(gi.buffer(margin + min_gap))
                if int(j) != i]
        grown = gi.buffer(margin)
        if near:
            # On reste (margin+min_gap) du cuivre d'origine des autres nets : comme
            # eux s'élargissent aussi de `margin`, il reste toujours >= min_gap entre
            # les deux cuivres ÉLARGIS -> aucun pont (symétrique).
            forbidden = unary_union(near).buffer(margin + min_gap)
            grown = grown.difference(forbidden)
        out[name] = gi.union(grown)                         # jamais plus petit que l'origine
    return LabeledCopper(labeled.layer, out, labeled.board, labeled.has_nets)


def hybrid_isolation(expanded: LabeledCopper, original: LabeledCopper,
                     kerf: float = 0.10, tight_factor: float = 3.0, step: float = 0.08):
    """Isolation hybride : contours offset propres + **passe unique centrée** (axe
    médian) dans les gaps serrés — évite le double trait sur le fine-pitch.

    - ``original`` : cuivre non élargi → sert à **détecter les gaps serrés** (qui
      existent encore) et à y calculer la passe centrée. La passe re-sépare les
      pads que la marge aurait fait fusionner.
    - ``expanded`` : cuivre élargi de la marge → sert aux **contours offset** (pistes
      plus larges pour survivre au laser + perchlorure).

    Un gap est "serré" si sa demi-largeur (clairance) ≤ ``tight_factor * kerf/2``.

    :return: (contours_offset: MultiLineString, passes_centrées: list[Segment])
    """
    tight_clear = tight_factor * kerf / 2.0

    # Passes centrées : détectées sur le cuivre d'origine (les gaps y sont intacts).
    segs = centerline_isolation(original, kerf=kerf, step=step)
    tight = [s for s in segs if s.clearance <= tight_clear]

    # Contours offset du cuivre élargi (à kerf/2 des pistes élargies)
    union = expanded.union
    rings = isolation_rings(union, kerf=kerf, rings=1)

    # On retire des contours les portions situées dans les zones serrées, pour ne
    # pas doubler la passe centrée qui y est déjà.
    if tight:
        tzone = unary_union([LineString(s.coords).buffer(tight_clear + kerf)
                             for s in tight])
        kept: list[LineString] = []
        for line in rings.geoms:
            kept.extend(_explode_lines(line.difference(tzone)))
        rings = MultiLineString(kept)

    return rings, tight


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
    kind: str = "short"          # "short" (vrai court inter-composants) | "footprint"
    component: str | None = None  # référence si chevauchement intra-composant

    @property
    def is_real(self) -> bool:
        """Vrai court à corriger (par opposition à un artefact d'empreinte)."""
        return self.kind == "short"


@dataclass
class DRCResult:
    shorts: list[Short] = field(default_factory=list)
    subkerf: int = 0
    min_clearance: float = float("inf")

    @property
    def real_shorts(self) -> list[Short]:
        return [s for s in self.shorts if s.is_real]

    @property
    def footprint_overlaps(self) -> list[Short]:
        return [s for s in self.shorts if not s.is_real]

    @property
    def ok(self) -> bool:
        return not self.real_shorts and self.subkerf == 0


def drc(labeled: LabeledCopper | None = None, segments: list[Segment] | None = None,
        kerf: float = 0.15, min_short_area: float = 0.004,
        features: list | None = None) -> DRCResult:
    """Contrôle avant gravure : nets différents en contact + canaux trop étroits.

    Deux modes :
    - **features** fournis (ODB++ : liste ``(index, geom, net, composant)``) →
      détection *feature-level* + **classification composant** : un chevauchement
      entre deux features du **même composant** est un artefact d'empreinte
      (``kind="footprint"``, informationnel), pas un vrai court.
    - sinon **labeled** (Gerber ou repli) → recouvrement par paire de nets.

    :param min_short_area: aire de recouvrement minimale (mm²) pour retenir un
        contact — sous ce seuil, simple contact ponctuel (tessellation) écarté.
    """
    res = DRCResult()
    if features is not None:
        res.shorts = _feature_level_shorts(features, min_short_area)
    elif labeled is not None and labeled.has_nets:
        res.shorts = _netpair_shorts(labeled, min_short_area)

    if segments:
        res.subkerf = sum(1 for s in segments if s.is_subkerf(kerf))
        res.min_clearance = min(s.clearance for s in segments)
    return res


def _feature_level_shorts(features: list, min_area: float) -> list[Short]:
    """Recouvrements inter-nets au niveau feature, classés footprint vs court."""
    from collections import defaultdict
    from shapely import STRtree

    geoms = [g for _i, g, _n, _c in features]
    tree = STRtree(geoms)
    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"inters": [], "same_comp": True, "comps": set()})

    for i, (_fi, gi, ni, ci) in enumerate(features):
        if ni.startswith("$NONE$"):
            continue
        for j in tree.query(gi):
            j = int(j)
            if j <= i:
                continue
            _fj, gj, nj, cj = features[j]
            if nj.startswith("$NONE$") or ni == nj:
                continue
            if not gi.intersects(gj):
                continue
            inter = gi.intersection(gj)
            if inter.is_empty or inter.area <= 0:
                continue
            key = tuple(sorted((ni, nj)))
            g = groups[key]
            g["inters"].append(inter)
            same = ci is not None and ci == cj
            g["same_comp"] = g["same_comp"] and same
            g["comps"].add(ci if same else None)

    out: list[Short] = []
    for (na, nb), g in groups.items():
        u = unary_union(g["inters"])
        if u.area < min_area:
            continue
        c = u.centroid
        footprint = g["same_comp"] and g["comps"] == {next(iter(g["comps"]))} and None not in g["comps"]
        comp = next(iter(g["comps"])) if footprint else None
        out.append(Short(na, nb, c.x, c.y, u.area,
                         kind="footprint" if footprint else "short", component=comp))
    out.sort(key=lambda s: -s.area)
    return out


def _netpair_shorts(labeled: LabeledCopper, min_area: float) -> list[Short]:
    """Repli sans composants : recouvrement significatif par paire de nets."""
    nets = [n for n in labeled.net_geoms if not n.startswith("$NONE$")]
    geoms = {n: labeled.net_geoms[n] for n in nets}
    bounds = {n: geoms[n].bounds for n in nets}
    out: list[Short] = []
    for i in range(len(nets)):
        for j in range(i + 1, len(nets)):
            na, nb = nets[i], nets[j]
            if not _bbox_touch(bounds[na], bounds[nb]):
                continue
            inter = geoms[na].intersection(geoms[nb])
            if inter.area >= min_area:
                c = inter.centroid
                out.append(Short(na, nb, c.x, c.y, inter.area))
    return out


def _bbox_touch(a, b, tol: float = 1e-6) -> bool:
    return not (a[2] < b[0] - tol or b[2] < a[0] - tol
                or a[3] < b[1] - tol or b[3] < a[1] - tol)
