"""Unités : conversion Gerber/Excellon -> shapely (source de vérité)."""

from __future__ import annotations

import math

from shapely.geometry import Point

from laserpcb import geometry as geo


def test_load_outline_board_size(gerber_dir):
    """Le contour du profil donne exactement la carte 80 x 135 mm."""
    board = geo.load_outline(gerber_dir / "profile.gbr")
    minx, miny, maxx, maxy = board.bounds
    assert math.isclose(maxx - minx, 80.0, abs_tol=0.01)
    assert math.isclose(maxy - miny, 135.0, abs_tol=0.01)
    assert board.area > 80 * 135 * 0.99


def test_load_copper_nonempty(gerber_dir):
    top = geo.load_copper(gerber_dir / "copper_top.gbr", "top")
    assert top.name == "top"
    assert len(top.geometry.geoms) > 10
    assert top.geometry.is_valid
    # les pistes tiennent dans la carte
    assert top.bounds[2] <= 80.5 and top.bounds[3] <= 135.5


def test_load_drill_holes(drill_file):
    """77 trous détectés, diamètres plausibles (dont les Ø4 mm de fixation)."""
    holes = geo.load_drill(drill_file)
    assert len(holes) == 77
    dias = [round(h.diameter, 2) for h in holes]
    assert max(dias) >= 3.9  # trous de fixation Ø4
    assert min(dias) > 0


# gerbonara identifie ses primitives par le NOM de classe -> on les mime tel quel.
class Circle:
    def __init__(self, x, y, r):
        self.x, self.y, self.r = x, y, r


class Line:
    def __init__(self, x1, y1, x2, y2, width):
        self.x1, self.y1, self.x2, self.y2, self.width = x1, y1, x2, y2, width


def test_primitive_circle_area():
    """Un pad rond (cercle) -> disque de bonne aire, centré."""
    poly = geo._primitive_to_shapely(Circle(0, 0, 0.5))
    assert poly.contains(Point(0, 0))
    assert math.isclose(poly.area, math.pi * 0.25, rel_tol=0.02)


def test_primitive_line_is_stroke():
    """Une piste (ligne d'aperture Ø1) -> rectangle arrondi de largeur 1 mm."""
    poly = geo._primitive_to_shapely(Line(0, 0, 10, 0, 1.0))
    assert poly.contains(Point(5, 0))
    assert not poly.contains(Point(5, 1.0))   # au-delà du demi-trait
    assert poly.area > 10 * 1.0 * 0.95        # ~ longueur x largeur (+ bouts ronds)


def test_label_by_component(gerber_dir):
    bottom = geo.load_copper(gerber_dir / "copper_bottom.gbr", "bottom")
    board = geo.load_outline(gerber_dir / "profile.gbr")
    lab = geo.label_by_component("bottom", bottom.geometry, board)
    assert lab.has_nets is False
    assert len(lab.net_geoms) == len(bottom.geometry.geoms)
    assert lab.union.area > 0
