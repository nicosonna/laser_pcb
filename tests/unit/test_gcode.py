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
