"""Éditeur vectoriel : tracer, sélectionner, transformer, annuler.

Les gestes sont rejoués au niveau de l'éditeur (coordonnées de SCÈNE), pas en
simulant des clics Qt : on teste la logique, pas le gestionnaire d'événements.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF, Qt                      # noqa: E402
from shapely.geometry import box                            # noqa: E402

from laserpcb import drawing as dr                           # noqa: E402
from laserpcb.gui.canvas import PcbCanvas                    # noqa: E402
from laserpcb.gui.editor import (TOOL_ELLIPSE, TOOL_POLYLINE, TOOL_RECT,  # noqa: E402
                                 TOOL_SELECT, DrawingEditor, ShapeItem)
from laserpcb.project import Project                         # noqa: E402

LEFT = Qt.MouseButton.LeftButton
VIS = {"carte": True, "cuivre": False, "serigraphie": False, "pads": False,
       "hole": False, "dessin": True}


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _session(origin=(0.0, 0.0)):
    proj = Project(name="t")
    proj.origin = origin
    proj.work_layers = dr.default_layers()
    return SimpleNamespace(
        project=proj, board=box(0, 0, 100, 100), board_center_x=lambda: 50.0,
        layers=[], holes=[], active_side="top",
        silk_geometry=lambda _s: None, pads_for=lambda _s: None)


def _setup(qapp, origin=(0.0, 0.0)):
    sess = _session(origin)
    canvas = PcbCanvas()
    canvas.resize(600, 600)
    ed = DrawingEditor(canvas, lambda: sess)
    ed.changed.connect(lambda: canvas.render_session(sess, visible=VIS))
    canvas.render_session(sess, visible=VIS)
    ed._qapp = qapp
    return sess, canvas, ed


def _sync(ed):
    """Le reaffichage est DIFFERE (sinon on detruit l element en cours d'usage)."""
    ed._qapp.processEvents()


def _drag(ed, x0, y0, x1, y1):
    ed.mouse_press(QPointF(x0, y0), LEFT)
    ed.mouse_move(QPointF(x1, y1))
    ed.mouse_release(QPointF(x1, y1), LEFT)
    _sync(ed)


# ---- tracé ----------------------------------------------------------------- #
def test_tracer_un_rectangle(qapp):
    sess, _c, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 10, 20, 40, 50)
    s, = sess.project.shapes
    assert s.kind == dr.KIND_RECT
    assert s.geometry().bounds == (10.0, 20.0, 40.0, 50.0)
    assert s.layer == ed.layers()[0].id          # calque courant
    assert s.id                                   # identifiant attribué


def test_tracer_une_ellipse(qapp):
    sess, _c, ed = _setup(qapp)
    ed.set_tool(TOOL_ELLIPSE)
    _drag(ed, 0, 0, 20, 10)
    s, = sess.project.shapes
    assert s.kind == dr.KIND_ELLIPSE
    assert s.params["rx"] == pytest.approx(10.0)
    assert s.params["ry"] == pytest.approx(5.0)


def test_un_simple_clic_ne_cree_rien(qapp):
    """Sans glisser, il n'y a pas de forme : sinon on sème des objets nuls."""
    sess, _c, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 10, 10, 10, 10)
    assert sess.project.shapes == []


def test_polyligne_point_par_point(qapp):
    sess, _c, ed = _setup(qapp)
    ed.set_tool(TOOL_POLYLINE)
    for pt in ((0, 0), (10, 0), (10, 10)):
        ed.mouse_press(QPointF(*pt), LEFT)
    ed.finish_polyline()
    s, = sess.project.shapes
    assert s.kind == dr.KIND_POLYLINE
    assert len(s.params["points"]) == 3
    assert s.geometry().length == pytest.approx(20.0)


def test_une_polyligne_dun_seul_point_est_abandonnee(qapp):
    sess, _c, ed = _setup(qapp)
    ed.set_tool(TOOL_POLYLINE)
    ed.mouse_press(QPointF(5, 5), LEFT)
    ed.finish_polyline()
    assert sess.project.shapes == []


# ---- repère ---------------------------------------------------------------- #
def test_le_decalage_plateau_est_compense(qapp):
    """Dessiner à l'écran doit poser la forme au bon endroit du MODÈLE.

    La scène porte l'offset plateau ; sans l'inversion, toute forme serait
    décalée d'autant.
    """
    sess, _c, ed = _setup(qapp, origin=(30.0, 40.0))
    ed.set_tool(TOOL_RECT)
    _drag(ed, 40, 50, 60, 70)                    # coordonnées ÉCRAN
    s, = sess.project.shapes
    assert s.geometry().bounds == (10.0, 10.0, 30.0, 30.0)   # MODÈLE


def test_laimantation_cale_sur_la_grille(qapp):
    sess, canvas, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    ed.snap = True
    canvas._grid_step = 5.0
    _drag(ed, 11.3, 19.1, 38.7, 52.4)
    s, = sess.project.shapes
    assert s.geometry().bounds == (10.0, 20.0, 40.0, 50.0)


# ---- sélection et transformations ------------------------------------------ #
def _select_all(canvas):
    items = [i for i in canvas.scene().items() if isinstance(i, ShapeItem)]
    for i in items:
        i.setSelected(True)
    return items


def test_supprimer_la_selection(qapp):
    sess, canvas, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 10, 10)
    _drag(ed, 20, 20, 30, 30)
    assert len(sess.project.shapes) == 2
    _select_all(canvas)[0].setSelected(True)
    items = [i for i in canvas.scene().items() if isinstance(i, ShapeItem)]
    for i in items[1:]:
        i.setSelected(False)
    ed.delete_selection()
    _sync(ed)
    assert len(sess.project.shapes) == 1


def test_redimensionner_par_une_poignee(qapp):
    sess, canvas, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 20, 10)
    _select_all(canvas)
    ed.update_handles()
    ed._begin()
    ed._drag_handle("tr", QPointF(40, 10))       # coin haut-droit : largeur x2
    ed._commit("Redimensionner")
    _sync(ed)
    assert sess.project.shapes[0].params["w"] == pytest.approx(40.0)


def test_tourner_par_la_poignee(qapp):
    sess, canvas, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 20, 4)
    _select_all(canvas)
    ed.update_handles()
    ed._begin()
    ed._drag_handle("rot", QPointF(10, 30))      # au-dessus du centre -> 90°
    ed._commit("Tourner")
    _sync(ed)
    assert sess.project.shapes[0].rotation == pytest.approx(90.0, abs=1.0)


def test_changer_la_selection_de_calque(qapp):
    sess, canvas, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 10, 10)
    _select_all(canvas)
    cible = ed.layers()[1].id
    ed.assign_layer(cible)
    _sync(ed)
    assert sess.project.shapes[0].layer == cible


# ---- annulation ------------------------------------------------------------- #
def test_annuler_et_refaire_un_trace(qapp):
    sess, _c, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 10, 10)
    _drag(ed, 20, 20, 30, 30)
    assert len(sess.project.shapes) == 2
    ed.undo.undo()
    _sync(ed)
    assert len(sess.project.shapes) == 1
    ed.undo.undo()
    _sync(ed)
    assert len(sess.project.shapes) == 0
    ed.undo.redo()
    _sync(ed)
    assert len(sess.project.shapes) == 1
    assert sess.project.shapes[0].geometry().bounds == (0.0, 0.0, 10.0, 10.0)


def test_annuler_une_suppression(qapp):
    sess, canvas, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 10, 10)
    _select_all(canvas)
    ed.delete_selection()
    _sync(ed)
    assert sess.project.shapes == []
    ed.undo.undo()
    _sync(ed)
    assert len(sess.project.shapes) == 1
    assert sess.project.shapes[0].geometry().bounds == (0.0, 0.0, 10.0, 10.0)


def test_annuler_un_redimensionnement(qapp):
    sess, canvas, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 20, 10)
    _select_all(canvas)
    ed.update_handles()
    ed._begin()
    ed._drag_handle("tr", QPointF(40, 10))
    ed._commit("Redimensionner")
    _sync(ed)
    ed.undo.undo()
    _sync(ed)
    assert sess.project.shapes[0].params["w"] == pytest.approx(20.0)


def test_une_transformation_sans_effet_nencombre_pas_lhistorique(qapp):
    sess, _c, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 10, 10)
    n = ed.undo.count()
    ed._begin()
    ed._commit("Rien")                            # aucun changement
    assert ed.undo.count() == n


def test_lediteur_revient_a_la_selection_apres_un_trace(qapp):
    """Après Échap, on retrouve l'outil de sélection — pas de piège modal."""
    _sess, _c, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)

    class _Ev:
        def key(self):
            return Qt.Key.Key_Escape

        def matches(self, _s):
            return False
    assert ed.key(_Ev()) is True
    assert ed.tool == TOOL_SELECT


# ---- confort (étape 4) ------------------------------------------------------ #
def test_dupliquer_decale_la_copie_et_la_selectionne(qapp):
    sess, canvas, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 10, 10)
    _select_all(canvas)
    ed.duplicate_selection(dx=5.0, dy=0.0)
    _sync(ed)
    assert len(sess.project.shapes) == 2
    a, b = sess.project.shapes
    assert b.geometry().bounds == (5.0, 0.0, 15.0, 10.0)
    assert a.id != b.id
    assert ed.selected_ids == {b.id}            # la copie prend la sélection
    ed.undo.undo(); _sync(ed)
    assert len(sess.project.shapes) == 1


def test_aligner_a_gauche(qapp):
    sess, canvas, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 10, 10)
    _drag(ed, 30, 30, 50, 50)
    _select_all(canvas)
    ed.align_selection("gauche")
    _sync(ed)
    assert {round(s.geometry().bounds[0], 6) for s in sess.project.shapes} == {0.0}
    ed.undo.undo(); _sync(ed)
    assert sess.project.shapes[1].geometry().bounds[0] == pytest.approx(30.0)


def test_aligner_demande_au_moins_deux_objets(qapp):
    """Aligner un objet sur lui-même n'a pas de sens et polluerait l'historique."""
    sess, canvas, ed = _setup(qapp)
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 10, 10)
    _select_all(canvas)
    n = ed.undo.count()
    ed.align_selection("droite")
    assert ed.undo.count() == n


def test_centrer_sur_la_carte(qapp):
    sess, canvas, ed = _setup(qapp)      # carte 100 x 100
    ed.set_tool(TOOL_RECT)
    _drag(ed, 0, 0, 20, 10)
    _select_all(canvas)
    ed.center_selection_on_board()
    _sync(ed)
    minx, miny, maxx, maxy = sess.project.shapes[0].geometry().bounds
    assert (minx + maxx) / 2 == pytest.approx(50.0)
    assert (miny + maxy) / 2 == pytest.approx(50.0)


def test_aimantation_sur_un_coin_de_carte(qapp):
    """Les repères de la carte priment sur la grille : c'est eux qu'on vise."""
    sess, canvas, ed = _setup(qapp)
    ed.snap = True
    canvas._grid_step = 10.0
    ed.set_tool(TOOL_RECT)
    _drag(ed, 1.2, 1.4, 40, 40)           # coin (0,0) de la carte tout proche
    assert sess.project.shapes[0].geometry().bounds[0] == pytest.approx(0.0)


# ---- import SVG (étape 5) --------------------------------------------------- #
def test_importer_un_svg_en_une_seule_annulation(qapp, tmp_path):
    """Un logo de N chemins ne doit pas demander N Ctrl+Z."""
    f = tmp_path / "logo.svg"
    f.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40mm" '
                 'height="20mm" viewBox="0 0 40 20">'
                 '<rect x="0" y="0" width="10" height="10"/>'
                 '<rect x="20" y="0" width="10" height="10"/>'
                 '<circle cx="35" cy="15" r="4"/></svg>', encoding="utf-8")
    sess, _c, ed = _setup(qapp)
    n = ed.import_svg(f, width_mm=50.0)
    _sync(ed)
    assert n == 3 and len(sess.project.shapes) == 3
    assert ed.undo.count() == 1
    from shapely.ops import unary_union
    u = unary_union([s.geometry() for s in sess.project.shapes])
    assert u.bounds[2] - u.bounds[0] == pytest.approx(50.0, rel=1e-3)
    cx = (u.bounds[0] + u.bounds[2]) / 2
    assert cx == pytest.approx(50.0, abs=0.5)      # centré sur la carte
    ed.undo.undo(); _sync(ed)
    assert sess.project.shapes == []
