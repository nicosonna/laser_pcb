"""Modèle du dessin vectoriel : objets paramétriques et calques de travail.

Ce modèle est la fondation de l'éditeur (étape 3). Les deux décisions à
protéger : les objets restent PARAMÉTRIQUES (donc rééditables et exacts), et
les réglages laser vivent sur le CALQUE, pas sur l'objet.
"""

import json

import pytest
from shapely.geometry import LineString, Polygon

from laserpcb.drawing import (KIND_ELLIPSE, KIND_PATH, KIND_POLYLINE, KIND_RECT,
                              KIND_TEXT, MODE_BOTH, MODE_FILL, MODE_LINE, Shape,
                              WorkLayer, default_layers, new_id,
                              next_layer_color, shape_items)
from laserpcb.project import Project


# ---- géométrie dérivée ---------------------------------------------------- #
def test_rectangle_exact():
    g = Shape(KIND_RECT, {"x": 10, "y": 20, "w": 30, "h": 15}).geometry()
    assert isinstance(g, Polygon)
    assert g.bounds == (10.0, 20.0, 40.0, 35.0)
    assert g.area == pytest.approx(450.0)


def test_ellipse_discretisee_selon_la_fleche():
    """Le pas suit le rayon : pas de facettes visibles sur un grand cercle."""
    petite = Shape(KIND_ELLIPSE, {"cx": 0, "cy": 0, "rx": 1, "ry": 1}).geometry()
    grande = Shape(KIND_ELLIPSE, {"cx": 0, "cy": 0, "rx": 50, "ry": 50}).geometry()
    assert len(grande.exterior.coords) > len(petite.exterior.coords)
    assert grande.area == pytest.approx(3.14159 * 50 * 50, rel=1e-3)


def test_polyligne_ouverte_ou_fermee():
    pts = [[0, 0], [10, 0], [10, 10]]
    assert isinstance(Shape(KIND_POLYLINE, {"points": pts}).geometry(), LineString)
    ferme = Shape(KIND_POLYLINE, {"points": pts, "closed": True}).geometry()
    assert isinstance(ferme, Polygon) and ferme.area == pytest.approx(50.0)


def test_texte_vectorise_au_point_demande():
    g = Shape(KIND_TEXT, {"x": 5, "y": 7, "text": "R12", "height": 3}).geometry()
    assert not g.is_empty
    minx, miny, _maxx, maxy = g.bounds
    assert minx == pytest.approx(5.0, abs=0.01)     # coin bas-gauche calé
    assert miny == pytest.approx(7.0, abs=0.01)
    assert maxy - miny <= 3.01                      # jamais plus haut que demandé


def test_rotation_autour_du_centre():
    droit = Shape(KIND_RECT, {"x": 0, "y": 0, "w": 10, "h": 2})
    tourne = Shape(KIND_RECT, {"x": 0, "y": 0, "w": 10, "h": 2}, rotation=90)
    assert droit.geometry().area == pytest.approx(tourne.geometry().area)
    bx = tourne.bounds()
    assert bx[2] - bx[0] == pytest.approx(2.0)      # largeur et hauteur échangées
    assert bx[3] - bx[1] == pytest.approx(10.0)


def test_un_objet_reste_reeditable():
    """Paramétrique : changer une valeur suffit, la géométrie suit."""
    s = Shape(KIND_TEXT, {"x": 0, "y": 0, "text": "AB", "height": 4})
    avant = s.geometry().bounds
    s.params["text"] = "ABCDEF"
    assert s.geometry().bounds[2] > avant[2]        # plus large
    s.params["height"] = 8
    assert s.geometry().bounds[3] > avant[3]        # plus haut


# ---- calques de travail ---------------------------------------------------- #
def test_le_mode_du_calque_decide_du_parcours():
    rect = Shape(KIND_RECT, {"x": 0, "y": 0, "w": 20, "h": 10})
    trait = shape_items(rect, WorkLayer("c1", "T", mode=MODE_LINE), kerf=0.1)
    plein = shape_items(rect, WorkLayer("c1", "R", mode=MODE_FILL), kerf=0.1)
    deux = shape_items(rect, WorkLayer("c1", "D", mode=MODE_BOTH), kerf=0.1)
    assert len(trait) == 1
    assert len(plein) > 50                           # hachures au pas du kerf
    assert len(deux) == len(trait) + len(plein)


def test_un_trait_ouvert_ne_se_remplit_pas():
    """Plutôt que de ne rien graver, on se rabat sur le trait."""
    ligne = Shape(KIND_POLYLINE, {"points": [[0, 0], [10, 0], [10, 10]]})
    assert len(shape_items(ligne, WorkLayer("c1", "R", mode=MODE_FILL))) == 1


def test_les_reglages_viennent_du_calque():
    rect = Shape(KIND_RECT, {"x": 0, "y": 0, "w": 5, "h": 5})
    lay = WorkLayer("c1", "T", speed=777.0, power=42.0, mode=MODE_LINE)
    (_coords, speed, power), = shape_items(rect, lay)
    assert (speed, power) == (777.0, 42.0)


def test_les_passes_du_calque_repetent_le_parcours():
    rect = Shape(KIND_RECT, {"x": 0, "y": 0, "w": 5, "h": 5})
    lay = WorkLayer("c1", "T", mode=MODE_LINE, passes=4)
    assert len(shape_items(rect, lay)) == 4


def test_couleurs_et_identifiants_uniques():
    lays = default_layers()
    assert len({c.color for c in lays}) == len(lays)
    assert next_layer_color(lays) not in {c.color for c in lays}
    assert new_id("o", [Shape(KIND_RECT, {}, id="o1"),
                        Shape(KIND_RECT, {}, id="o2")]) == "o3"


# ---- persistance ----------------------------------------------------------- #
def test_le_dessin_survit_a_lenregistrement(tmp_path):
    p = Project(name="essai")
    p.work_layers = default_layers()
    p.shapes = [Shape(KIND_RECT, {"x": 1, "y": 2, "w": 3, "h": 4}, "c1", 15.0, "o1"),
                Shape(KIND_TEXT, {"x": 0, "y": 0, "text": "Hé ç", "height": 2},
                      "c2", 0.0, "o2")]
    f = tmp_path / "p.lpcb"
    p.save(f)
    q = Project.load(f)

    assert [c.to_dict() for c in q.work_layers] == [c.to_dict() for c in p.work_layers]
    assert [s.to_dict() for s in q.shapes] == [s.to_dict() for s in p.shapes]
    assert q.shapes[0].rotation == 15.0
    assert q.shapes[1].geometry().bounds == p.shapes[1].geometry().bounds


def test_un_projet_ancien_se_lit_toujours():
    """Un .lpcb écrit avant le dessin ne doit pas casser."""
    vieux = json.loads(json.dumps(Project(name="v1").to_dict()))
    vieux.pop("work_layers", None)
    vieux.pop("shapes", None)
    vieux.pop("drawing_enabled", None)
    q = Project.from_dict(vieux)
    assert q.work_layers == [] and q.shapes == [] and q.drawing_enabled is True


def test_une_geometrie_figee_se_relit(tmp_path):
    """Type « trace » : la voie d'entrée des imports SVG/DXF (étape 5)."""
    s = Shape(KIND_PATH, {"wkt": "POLYGON ((0 0, 4 0, 4 3, 0 3, 0 0))"})
    assert s.geometry().area == pytest.approx(12.0)
    assert Shape.from_dict(s.to_dict()).geometry().area == pytest.approx(12.0)
