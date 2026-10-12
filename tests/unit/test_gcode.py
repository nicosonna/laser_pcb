"""Unités : génération G-code GRBL Falcon 2 (constante et adaptative)."""

from __future__ import annotations

import math

from shapely.geometry import LineString, MultiLineString

from laserpcb import gcode as gc, toolpath as tp


def test_program_structure():
    items = gc.items_from_paths(MultiLineString([[(0, 0), (10, 0)]]), speed=800, power=90)
    prog = gc.build(items, gc.GCodeConfig(), passes=1, title="t")
    assert "G21" in prog and "G90" in prog          # mm, absolu
    assert "M4 S0" in prog                           # laser armé, éteint
    assert prog.rstrip().endswith("M2")              # fin de programme
    assert "M5" in prog                              # laser coupé en fin
    assert "S900" in prog                            # 90 % de 1000


def test_items_filter_degenerate():
    mls = MultiLineString([[(0, 0), (5, 0)], [(2, 2), (2, 2)]])  # 2e = longueur nulle
    items = gc.items_from_paths(mls, speed=500, power=50)
    assert len(items) == 1


def test_adaptive_feed_and_power_vary():
    cal = tp.Calibration()
    segs = [tp.Segment([(0, 0), (1, 0)], clearance=cal.c_open),   # large -> rapide/plein
            tp.Segment([(1, 0), (2, 0)], clearance=cal.c_tight)]  # serré -> lent/doux
    items = gc.items_from_segments(segs, cal)
    feeds = [f for _c, f, _p in items]
    powers = [p for _c, _f, p in items]
    assert feeds[0] > feeds[1]                       # large plus rapide que serré
    assert powers[0] > powers[1]


def test_emitter_skips_zero_length_moves():
    # deux segments jointifs : aucun G0 nul inséré entre eux
    items = [([(0, 0), (1, 0)], 600, 100), ([(1, 0), (2, 0)], 600, 100)]
    prog = gc.build(items, gc.GCodeConfig(optimize_order=False, return_to_origin=False),
                    title="t")
    g0_count = sum(1 for l in prog.splitlines() if l.startswith("G0 X"))
    assert g0_count == 1                             # un seul G0 initial, rien entre


def test_estimate_minutes():
    items = gc.items_from_paths([LineString([(0, 0), (600, 0)])], speed=600, power=100)
    assert math.isclose(gc.estimate_minutes(items), 1.0, rel_tol=1e-6)  # 600 mm / 600 mm/min


def test_cut_length():
    items = gc.items_from_paths([LineString([(0, 0), (3, 4)])], speed=600, power=100)
    assert math.isclose(gc.cut_length(items), 5.0, rel_tol=1e-6)


def test_boustrophedon_orders_into_bands():
    """Le serpentin ordonne les parcours en bandes Y croissantes (trajets réduits)."""
    # ordre initial volontairement dispersé (par colonnes)
    items = [([(x, y), (x + 0.5, y)], 600, 100)
             for x in range(20) for y in range(20)]
    ordered = gc._boustrophedon(items, 2.0)             # serpentin seul (bande fixée)
    bands = [int(it[0][0][1] // 2.0) for it in ordered]
    assert bands == sorted(bands)                       # bandes non décroissantes
    # au sein d'une bande, on progresse en X sans revenir en arrière — mesuré sur
    # l'ÉTENDUE du parcours (chaque tronçon peut être retourné par l'orientation)
    band0 = [it[0] for it in ordered if int(it[0][0][1] // 2.0) == 0]
    lefts = [min(p[0] for p in c) for c in band0]
    assert lefts == sorted(lefts)
    # et le serpentin réduit nettement les trajets face à l'ordre dispersé
    assert gc._travel_length(ordered) < gc._travel_length(items) * 0.7


def test_optimize_orients_paths_and_cuts_travel():
    """Le serpentin doit RETOURNER les parcours selon le point courant : sans ça,
    dans une bande parcourue à rebours on saute la longueur de chaque segment."""
    # 40 lignes horizontales empilées : le trajet idéal = ~0 (serpentin parfait)
    items = [([(0.0, i * 0.1), (5.0, i * 0.1)], 1000, 50) for i in range(40)]
    opt = gc.optimize(items, max_nn=0)          # force le serpentin
    travel = gc._travel_length(opt)
    assert travel < 5.0                          # avant le fix : ~195 mm
    assert len(opt) == len(items)                # aucun parcours perdu
    # chaque ligne est bien gravée entière (longueur conservée)
    assert abs(gc.cut_length(opt) - gc.cut_length(items)) < 1e-9


def test_simplify_stays_within_tolerance_and_drops_no_geometry():
    """INVARIANT : aucun point du tracé d'origine ne s'écarte de plus de la
    tolérance du tracé simplifié, et aucun segment de longueur nulle ne subsiste."""
    import numpy as np
    from shapely.geometry import LineString, Point

    tol = 0.006
    # tracé de référence : demi-cercle sur-discrétisé (pas de 0,04 mm) + 2 droites
    ang = np.linspace(0.0, math.pi, 400)
    arc = [(5.0 * math.cos(a), 5.0 * math.sin(a)) for a in ang]
    ref = arc + [(-5.0, -3.0), (-5.0, -6.0)]
    ref.insert(1, ref[1])                       # doublon volontaire
    out = gc.simplify_items([(ref, 1500, 30)], tol)[0][0]

    simple = LineString(out)
    assert max(Point(p).distance(simple) for p in ref) <= tol + 1e-9
    a = np.asarray(out, dtype=float)
    assert np.hypot(*(a[1:] - a[:-1]).T).min() > 0.0      # aucun segment nul
    assert out[0] == ref[0] and out[-1] == ref[-1]        # extrémités conservées
    assert len(out) < len(ref) / 3                        # et ça simplifie vraiment


def test_build_reports_stats_and_keeps_m2_last():
    """Le bilan est renseigné et écrit en en-tête : M2 reste la dernière ligne."""
    items = [([(0.0, 0.0), (1.0, 0.0005), (2.0, 0.0), (2.0, 2.0)], 1500, 30)]
    st = {}
    prog = gc.build(items, gc.GCodeConfig(), stats=st)
    assert st["segments_before"] == 3 and st["segments_after"] == 2
    assert st["length_after_mm"] > 0 and st["shortest_mm"] > 0
    assert st["lines"] == len(prog.splitlines())
    assert prog.rstrip().endswith("M2")
    assert "Douglas-Peucker" in prog and "plus petit segment" in prog


def test_no_duplicate_consecutive_lines_after_rounding():
    """Deux points distincts qui s'écrivent pareil à 3 décimales : une seule ligne."""
    items = [([(10.0, 20.0), (10.0006, 20.0), (10.0009, 20.0), (11.0, 20.0)], 1500, 30)]
    prog = gc.build(items, gc.GCodeConfig(simplify_tolerance=0.0))
    moves = [l for l in prog.splitlines() if l[:2] in ("G0", "G1")]
    assert len(moves) == len({m.split(" F")[0].split(" S")[0] for m in moves})
