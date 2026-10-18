"""Import SVG : unités, axe Y et trous.

Trois conversions, trois pièges. Le fichier d'essai est écrit ici même pour que
les valeurs attendues soient vérifiables à la main.
"""

import pytest

pytest.importorskip("svgelements")

from shapely.ops import unary_union                    # noqa: E402

from laserpcb import svgimport as si                   # noqa: E402
from laserpcb.drawing import KIND_PATH                 # noqa: E402

SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm"
     viewBox="0 0 100 50">
  <rect x="0" y="0" width="20" height="10"/>
  <circle cx="60" cy="25" r="10"/>
  <path d="M 10,30 h 20 v 15 h -20 Z M 15,35 h 10 v 5 h -10 Z"/>
  <polyline points="70,5 90,5 90,20" fill="none" stroke="black"/>
</svg>
"""


@pytest.fixture
def fichier(tmp_path):
    f = tmp_path / "essai.svg"
    f.write_text(SVG, encoding="utf-8")
    return f


def test_les_unites_deviennent_des_millimetres(fichier):
    """20 unités dans un viewBox de 100 pour 100 mm font 20 mm."""
    geoms = si.read_svg(fichier)
    rect = next(g for g in geoms if g.area == pytest.approx(200.0, rel=1e-3))
    minx, miny, maxx, maxy = rect.bounds
    assert maxx - minx == pytest.approx(20.0, rel=1e-3)
    assert maxy - miny == pytest.approx(10.0, rel=1e-3)


def test_un_cercle_garde_son_aire(fichier):
    """Discrétisation guidée par la flèche : l'aire ne doit pas fondre."""
    geoms = si.read_svg(fichier)
    disque = next(g for g in geoms if g.area == pytest.approx(314.16, rel=1e-2))
    assert disque.area == pytest.approx(3.14159 * 100, rel=5e-3)


def test_laxe_y_est_retourne(fichier):
    """Le SVG descend, la machine monte : sans retournement tout est en miroir.

    Le rectangle est EN HAUT du fichier (y=0) ; il doit se retrouver en haut du
    résultat, donc aux Y les plus GRANDS.
    """
    geoms = si.read_svg(fichier)
    rect = next(g for g in geoms if g.area == pytest.approx(200.0, rel=1e-3))
    disque = next(g for g in geoms if g.area == pytest.approx(314.16, rel=1e-2))
    assert rect.bounds[1] > disque.bounds[1]


def test_les_trous_sont_creuses(fichier):
    """Règle pair-impair : un carré de 20×15 avec un trou de 10×5 fait 250 mm²."""
    geoms = si.read_svg(fichier)
    perce = next(g for g in geoms if g.area == pytest.approx(250.0, rel=1e-3))
    assert len(perce.interiors) == 1
    assert perce.interiors[0].is_ring


def test_un_trace_ouvert_reste_une_ligne(fichier):
    geoms = si.read_svg(fichier)
    lignes = [g for g in geoms if g.geom_type == "LineString"]
    assert len(lignes) == 1
    assert lignes[0].length == pytest.approx(35.0, rel=1e-3)


def test_taille_naturelle(fichier):
    w, h = si.natural_size(fichier)
    assert (w, h) == (pytest.approx(90.0, rel=1e-3), pytest.approx(45.0, rel=1e-3))


def test_mise_a_lechelle_et_placement(fichier):
    shapes = si.shapes_from_svg(fichier, layer_id="c2", target_width=45.0,
                                at=(5.0, 7.0))
    assert shapes and all(s.kind == KIND_PATH for s in shapes)
    assert all(s.layer == "c2" for s in shapes)
    assert len({s.id for s in shapes}) == len(shapes)      # identifiants uniques
    u = unary_union([s.geometry() for s in shapes])
    minx, miny, maxx, maxy = u.bounds
    assert (minx, miny) == (pytest.approx(5.0), pytest.approx(7.0))
    assert maxx - minx == pytest.approx(45.0, rel=1e-3)
    assert maxy - miny == pytest.approx(22.5, rel=1e-3)    # proportions gardées


def test_un_svg_vide_ne_casse_rien(tmp_path):
    f = tmp_path / "vide.svg"
    f.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10mm" '
                 'height="10mm"></svg>', encoding="utf-8")
    assert si.read_svg(f) == []
    assert si.natural_size(f) == (0.0, 0.0)
    assert si.shapes_from_svg(f) == []


def test_un_objet_importe_se_relit_apres_enregistrement(fichier, tmp_path):
    """Type « trace » : géométrie figée, mais elle doit survivre au .lpcb."""
    from laserpcb.project import Project
    p = Project(name="svg")
    p.shapes = si.shapes_from_svg(fichier, target_width=20.0)
    f = tmp_path / "p.lpcb"
    p.save(f)
    q = Project.load(f)
    a = unary_union([s.geometry() for s in p.shapes])
    b = unary_union([s.geometry() for s in q.shapes])
    assert b.area == pytest.approx(a.area, rel=1e-9)
