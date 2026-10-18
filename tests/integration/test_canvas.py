"""Canevas Qt : repère, cadrage et export.

Le canevas est passé de matplotlib à ``QGraphicsView``. Le piège du portage est
l'axe Y : Qt dessine vers le bas, la machine vers le haut. La vue applique donc
un miroir — mais ``QGraphicsScene.render()`` l'ignore, et l'export PNG est
sorti tête en bas au premier essai.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

pytest.importorskip("PySide6")

from shapely.geometry import box                      # noqa: E402

from laserpcb.gui.canvas import PcbCanvas             # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _session(holes=()):
    """Session minimale : juste ce que ``render_session`` consulte."""
    return SimpleNamespace(
        project=SimpleNamespace(origin=(0.0, 0.0), bed_width=400.0,
                                bed_height=400.0),
        board=box(0, 0, 100, 100),
        board_center_x=lambda: 50.0,
        layers=[],
        holes=list(holes),
        active_side="top",
        silk_geometry=lambda _side: None,
        pads_for=lambda _side: None,
    )


VIS = {"carte": True, "cuivre": False, "serigraphie": False, "pads": False,
       "hole": True}


def test_la_vue_a_y_vers_le_haut(qapp):
    """Miroir vertical : sans lui, la carte s'afficherait tête en bas."""
    c = PcbCanvas()
    c.resize(400, 400)
    c.render_session(_session(), visible=VIS)
    assert c.transform().m22() < 0, "l'axe Y de la vue doit être inversé"
    assert c.transform().m11() > 0, "l'axe X, lui, ne doit pas l'être"


def test_le_rendu_remplit_la_scene(qapp):
    """Un portage cassé donnerait une scène vide sans lever d'exception."""
    c = PcbCanvas()
    c.resize(400, 400)
    c.render_session(_session(), visible=VIS)
    assert len(c.scene().items()) >= 3      # plateau + étiquette + carte


def test_lexport_png_nest_pas_tete_en_bas(qapp, tmp_path):
    """Un trou placé EN HAUT doit se retrouver en haut de l'image.

    ``QGraphicsScene.render()`` ignore la transformation de la vue : c'est
    exactement ce qui avait inversé l'export.
    """
    from PySide6.QtGui import QImage

    hole = SimpleNamespace(x=50.0, y=90.0, diameter=8.0)
    c = PcbCanvas()
    c.resize(400, 400)
    c.render_session(_session(holes=[hole]), visible=VIS)

    out = tmp_path / "apercu.png"
    c.save_image(str(out), width=1200)
    img = QImage(str(out))
    assert not img.isNull() and img.width() == 1200

    # cadrage : carte 100x100, marge 100*0.08+2 = 10 mm -> scène (-10,-10)..(110,110)
    mm2px = img.width() / 120.0

    def at(x_mm, y_mm):
        px = int((x_mm + 10) * mm2px)
        py = int(img.height() - (y_mm + 10) * mm2px)
        return img.pixelColor(px, py)

    haut, bas = at(50, 90), at(50, 10)
    # le trou est blanc, le FR4 est crème (#efe7c8) : le bleu les sépare nettement
    assert haut.blue() > 240, f"pas de trou en haut de l'image : {haut.name()}"
    assert bas.blue() < 230, f"le trou s'est retrouvé en bas : {bas.name()}"


def test_le_cadrage_automatique_suit_le_redimensionnement(qapp):
    """Un rendu fait avant que la fenêtre ait sa taille doit se recadrer ensuite.

    Sinon la carte reste minuscule dans un coin — c'est ce qui se passait.
    """
    def resize(w, h):
        c.resize(w, h)
        qapp.processEvents()                # Qt ne redimensionne qu'un widget VISIBLE
    c = PcbCanvas()
    c.show()
    c.render_session(_session(), visible=VIS)
    resize(900, 700)
    avant = c.transform().m11()
    resize(1800, 1400)                      # deux fois plus large
    assert c.transform().m11() > avant * 1.5

    c.zoom_in()                             # l'utilisateur prend la main
    manuel = c.transform().m11()
    resize(900, 700)
    assert c.transform().m11() == pytest.approx(manuel), \
        "un zoom manuel ne doit plus être écrasé par le redimensionnement"
