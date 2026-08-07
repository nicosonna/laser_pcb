"""Unités : parcours (isolation, négatif, axe médian net-aware) + adaptatif + DRC."""

from __future__ import annotations

import math

from shapely import box
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
