"""Unités : parcours (isolation, négatif, axe médian net-aware) + adaptatif + DRC."""

from __future__ import annotations

import math

from shapely import box
from shapely.geometry import Point
from shapely.ops import unary_union

from laserpcb import toolpath as tp
from laserpcb.geometry import LabeledCopper


# ---- isolation offset / négatif / hachures --------------------------------- #
def test_isolation_rings_offset():
    sq = box(0, 0, 5, 5)
    rings = tp.isolation_rings(sq, kerf=0.2, rings=1)
    assert len(rings.geoms) == 1
    # le contour est décalé de kerf/2 vers l'extérieur -> plus grand que le pad
    assert rings.geoms[0].length > sq.exterior.length


def test_isolation_hugs_copper_but_medial_axis_wanders():
    """INVARIANT QUALITÉ (le test qui manquait) : l'isolation offset reste collée
    au cuivre, alors que l'axe médian traverse le milieu du vide — donc inadapté
    comme tracé par défaut sur une carte entière."""
    kerf = 0.2
    copper = unary_union([box(0, 0, 2, 2), box(18, 0, 20, 2)])   # deux îlots éloignés
    board = box(-1, -1, 21, 3)

    rings = tp.isolation_rings(copper, kerf=kerf, rings=1)
    max_iso = max(Point(p).distance(copper)
                  for line in rings.geoms for p in line.coords)
    assert max_iso < kerf                    # tout le tracé colle au cuivre

    lab = LabeledCopper("t", {"a": box(0, 0, 2, 2), "b": box(18, 0, 20, 2)},
                        board, has_nets=True)
    segs = tp.centerline_isolation(lab, kerf=kerf, step=0.2)
    max_med = max(Point(p).distance(copper) for s in segs for p in s.coords)
    assert max_med > 5                       # l'axe médian passe ~8 mm du cuivre


def test_negative_region_is_board_minus_copper():
    board = box(0, 0, 10, 10)
    copper = box(2, 2, 8, 8)
    neg = tp.negative_region(copper, board)
    assert math.isclose(neg.area, 100 - 36, abs_tol=0.1)


def test_hatch_fills_region():
    region = box(0, 0, 5, 5)
    lines = tp.hatch(region, spacing=0.5, angle=0.0)
    assert len(lines.geoms) >= 8            # ~10 lignes horizontales
    for ln in lines.geoms:
        assert ln.length <= 5.001


def test_hybrid_isolation_single_pass_in_tight_gap():
    """Hybride : gap serré -> passe unique centrée (pas de double contour)."""
    a = box(0, 0, 1, 1)
    b = box(1.2, 0, 2.2, 1)                  # gap serré de 0.2 mm entre a et b
    board = box(-5, -5, 10, 6)
    original = LabeledCopper("t", {"a": a, "b": b}, board, has_nets=True)
    expanded = LabeledCopper("t", {"a": a.buffer(0.05), "b": b.buffer(0.05)},
                             board, has_nets=True)
    rings, tight = tp.hybrid_isolation(expanded, original, kerf=0.1)
    assert len(tight) > 0                     # le gap serré est traité en passe centrée
    xs = [x for s in tight for (x, _y) in s.coords]
    assert any(1.0 < x < 1.2 for x in xs)     # la passe est au milieu du gap
    assert not rings.is_empty                 # contours offset ailleurs


def test_hole_circles_scaled():
    """Le pourcentage rétrécit le Ø du trou gravé (ex. -30 % -> 70 %)."""
    from laserpcb.geometry import Hole
    from shapely.geometry import Polygon
    rings = tp.hole_circles([Hole(0, 0, 2.0)], scale_percent=-30.0)
    ring = list(rings.geoms)[0]
    # Ø 2 mm * 0.7 = 1.4 -> rayon 0.7
    assert abs(Polygon(ring).area - 3.14159 * 0.7 ** 2) < 0.05


def test_clearing_zones_edge_vs_interior():
    """Évidement : liseré près du cuivre + intérieur ouvert, couvrant toute la zone."""
    region = box(0, 0, 10, 10)
    copper = box(4, 4, 6, 6)                # petite piste au centre
    edge, interior = tp.clearing_zones(region, copper, margin=1.0)
    assert not edge.is_empty and not interior.is_empty
    assert interior.area > edge.area        # l'intérieur ouvert domine
    # les deux réunis recouvrent la zone (à epsilon près)
    assert abs((edge.area + interior.area) - region.area) < 0.05


# ---- calibration adaptative ------------------------------------------------ #
def test_calibration_endpoints_and_monotonic():
    cal = tp.Calibration()
    v_t, p_t = cal.params(cal.c_tight)
    v_o, p_o = cal.params(cal.c_open)
    assert math.isclose(v_t, cal.v_slow) and math.isclose(p_t, cal.p_soft)
    assert math.isclose(v_o, cal.v_fast) and math.isclose(p_o, cal.p_full)
    # clairance intermédiaire -> valeurs intermédiaires et croissantes
    v_m, _ = cal.params((cal.c_tight + cal.c_open) / 2)
    assert cal.v_slow < v_m < cal.v_fast
    # en dessous du serré : clampé au minimum
    assert cal.params(0.0)[0] == cal.v_slow


def test_segment_subkerf():
    assert tp.Segment([(0, 0), (1, 0)], clearance=0.05).is_subkerf(0.15) is True
    assert tp.Segment([(0, 0), (1, 0)], clearance=0.20).is_subkerf(0.15) is False


# ---- axe médian net-aware -------------------------------------------------- #
def test_centerline_cuts_between_different_nets(two_pads):
    a, b, board = two_pads
    lab = LabeledCopper("t", {"netA": a, "netB": b}, board, has_nets=True)
    segs = tp.centerline_isolation(lab, kerf=0.15, step=0.05)
    assert len(segs) > 0
    # la coupe passe au milieu de l'interstice (x ~ 1.2) et hors cuivre
    xs = [x for s in segs for (x, _y) in s.coords]
    assert any(1.1 < x < 1.3 for x in xs)


def test_centerline_never_cuts_within_a_net(two_pads):
    """Deux pads du MÊME net -> aucune coupe entre eux (ponts conservés)."""
    a, b, board = two_pads
    lab = LabeledCopper("t", {"gnd": unary_union([a, b])}, board, has_nets=True)
    segs = tp.centerline_isolation(lab, kerf=0.15, step=0.05)
    assert len(segs) == 0


# ---- DRC ------------------------------------------------------------------- #
def test_drc_detects_real_short():
    board = box(0, 0, 10, 10)
    a = box(0, 0, 5, 5)
    b = box(4.5, 0, 9, 5)          # recouvre a sur 0.5 x 5 = 2.5 mm²
    lab = LabeledCopper("t", {"n1": a, "n2": b}, board, has_nets=True)
    res = tp.drc(lab)
    assert len(res.shorts) == 1
    assert {res.shorts[0].net_a, res.shorts[0].net_b} == {"n1", "n2"}


def test_drc_ignores_separated_nets(two_pads):
    a, b, board = two_pads
    lab = LabeledCopper("t", {"n1": a, "n2": b}, board, has_nets=True)
    assert tp.drc(lab).shorts == []


def test_drc_no_shorts_without_nets(two_pads):
    """Sans vrais nets (Gerber, composantes) : aucun court détectable."""
    a, b, board = two_pads
    lab = LabeledCopper("t", {"c0": a, "c1": b}, board, has_nets=False)
    assert tp.drc(lab).shorts == []


def test_drc_component_aware_footprint_vs_short():
    """Chevauchement même composant = footprint (info) ; composants ≠ = vrai court."""
    a = box(0, 0, 2, 2)
    b = box(1.5, 0, 3.5, 2)               # recouvre a sur 0.5 x 2 = 1 mm²
    # même composant -> footprint (pas un vrai court)
    res = tp.drc(features=[(1, a, "N1", "U7"), (2, b, "N2", "U7")])
    assert len(res.shorts) == 1
    assert res.shorts[0].kind == "footprint" and res.shorts[0].component == "U7"
    assert res.real_shorts == [] and res.ok is True
    # composants différents -> vrai court
    res2 = tp.drc(features=[(1, a, "N1", "U7"), (2, b, "N2", "U8")])
    assert res2.shorts[0].kind == "short"
    assert len(res2.real_shorts) == 1 and res2.ok is False


def test_tight_gap_passes_in_narrow_channels():
    """Gap inversé (< kerf) OU traits qui se chevauchent (< 2·kerf) : une passe
    unique CENTRÉE. Gap large : aucune passe (les anneaux suffisent)."""
    board = box(-1, -1, 4, 2)
    # deux pads séparés de 0.06 mm (< kerf 0.10) -> passe centrée à x ~ 1.03
    a, b = box(0, 0, 1, 1), box(1.06, 0, 2.06, 1)
    lab = LabeledCopper("t", {"A": a, "B": b}, board, has_nets=True)
    segs = tp.tight_gap_passes(lab, kerf=0.10)
    assert segs, "gap sous-kerf : il FAUT une passe centrée"
    xs = [x for s in segs for (x, _y) in s.coords]
    assert all(abs(x - 1.03) < 0.02 for x in xs)      # bien au milieu du canal
    # gap 0.15 mm (entre kerf et 2·kerf) : deux traits se chevaucheraient -> UNE passe
    over = LabeledCopper("t", {"A": a, "B": box(1.15, 0, 2.15, 1)}, board, has_nets=True)
    segs = tp.tight_gap_passes(over, kerf=0.10)
    assert segs, "chevauchement : il FAUT une passe centrée unique"
    xs = [x for s in segs for (x, _y) in s.coords]
    assert all(abs(x - 1.075) < 0.02 for x in xs)
    # les mêmes pads écartés de 0.30 mm -> gap franchissable, pas de passe spéciale
    wide = LabeledCopper("t", {"A": a, "B": box(1.30, 0, 2.30, 1)}, board, has_nets=True)
    assert tp.tight_gap_passes(wide, kerf=0.10) == []


def test_combined_rings_vanish_in_overlap_channels():
    """Canal entre kerf et 2·kerf : la fermeture morpho retire les DEUX anneaux du
    canal (un seul passage centré les remplace) ; pads isolés : anneau normal."""
    from shapely.geometry import MultiLineString
    board = box(-1, -1, 4, 2)
    a, b = box(0, 0, 1, 1), box(1.15, 0, 2.15, 1)     # gap 0.15, kerf 0.10
    lab = LabeledCopper("t", {"A": a, "B": b}, board, has_nets=True)
    rings, _wide = tp.combined_isolation(lab, board, kerf=0.10)
    # aucun point d'anneau à l'INTÉRIEUR du canal (x entre 1.02 et 1.13)
    inside = [(x, y) for g in rings.geoms for (x, y) in g.coords
              if 1.02 < x < 1.13 and 0.1 < y < 0.9]
    assert not inside, f"anneaux résiduels dans le canal : {inside[:4]}"
    # les bords extérieurs restent contournés (x ~ -0.05 et ~ 2.20)
    xs = [x for g in rings.geoms for (x, _y) in g.coords]
    assert min(xs) < 0 and max(xs) > 2.15


def test_text_polygons_vectorized_with_letter_holes():
    """Le texte devient des polygones (hauteur ~demandée, calé bas-gauche), et les
    lettres à trou (O) gardent leur trou (règle pair-impair)."""
    geom = tp.text_polygons("BO", height=4.0, x=10.0, y=20.0)
    assert not geom.is_empty
    minx, miny, maxx, maxy = geom.bounds
    assert abs(minx - 10.0) < 1e-6 and abs(miny - 20.0) < 1e-6   # ancré à (x, y)
    assert 2.0 < maxy - miny <= 5.0                              # ~hauteur demandée
    assert any(len(p.interiors) for p in geom.geoms)             # trous des lettres
