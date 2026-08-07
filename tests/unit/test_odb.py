"""Unités : parseur ODB++ (symboles, features, géométrie, net-aware)."""

from __future__ import annotations

import math

from laserpcb import odb


def test_symbol_parsing():
    """Décodage des noms de symboles ODB++ (dimensions en microns -> mm)."""
    assert odb.Symbol("r6200").kind == "round"
    assert math.isclose(odb.Symbol("r6200").dims[0], 6.2)
    assert odb.Symbol("s4000").kind == "square"
    assert math.isclose(odb.Symbol("s4000").dims[0], 4.0)
    r = odb.Symbol("rect1450x930")
    assert r.kind == "rect" and math.isclose(r.dims[0], 1.45) and math.isclose(r.dims[1], 0.93)
    assert odb.Symbol("oct1524x1524x446").kind == "octagon"
    assert odb.Symbol("oval800x320").kind == "oval"
    # largeur de trait = plus petite dimension
    assert math.isclose(odb.Symbol("rect1450x930").width, 0.93)


def test_symbol_flash_shapes():
    from shapely.geometry import Point
    disc = odb.Symbol("r2000").flash(0, 0)          # Ø2 mm
    assert math.isclose(disc.area, math.pi, rel_tol=0.02)
    rect = odb.Symbol("rect2000x1000").flash(5, 5)  # 2 x 1 mm centré en (5,5)
    assert rect.contains(Point(5, 5))
    assert math.isclose(rect.area, 2.0, rel_tol=0.01)


def test_outline_and_holes(odb_root):
    board = odb.load_outline(odb_root)
    minx, miny, maxx, maxy = board.bounds
    assert math.isclose(maxx - minx, 147.0, abs_tol=0.1)
    assert math.isclose(maxy - miny, 97.0, abs_tol=0.1)
    holes = odb.load_drill(odb_root)
    assert len(holes) == 122


def test_iter_features_count(odb_root):
    """Le fichier top déclare 1048 features -> autant d'entités parcourues."""
    feats = list(odb.iter_features(odb_root / "steps/pcb/layers/top/features"))
    assert len(feats) == 1048


def test_labeled_copper_net_aware(odb_root):
    top = odb.load_labeled_copper(odb_root, "top")
    assert top.has_nets is True
    assert "GND" in top.net_geoms
    assert len(top.net_geoms) > 50
    # GND est le plus gros net (plan de masse)
    biggest = max(top.net_geoms, key=lambda n: top.net_geoms[n].area)
    assert biggest == "GND"
    # pads sans net isolés séparément
    assert any(n.startswith("$NONE$") for n in top.net_geoms)
