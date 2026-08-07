"""Smoke GUI : la fenêtre s'instancie et importe un PCB (headless offscreen).

Ouvre aucune vraie fenêtre : plateforme Qt ``offscreen``. Sauté si PySide6 absent.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_mainwindow_import_and_layers(qapp):
    from laserpcb.gui.main_window import MainWindow
    w = MainWindow()
    w._import("examples/module_relais")
    assert len(w.session.layers) == 2
    assert len(w._layer_widgets) == 2          # panneau calques reconstruit
    assert w.send_panel is not None            # panneau d'envoi présent
    # le canvas se rend sans erreur
    w.canvas.render_session(w.session)


def test_canvas_handles_multipolygon_board(qapp):
    """Contour de carte panélisé (MultiPolygon) : le rendu ne doit pas planter."""
    from shapely import box
    from shapely.geometry import MultiPolygon
    from laserpcb.gui.canvas import PcbCanvas
    from laserpcb.gui.session import Session
    s = Session()
    s.board = MultiPolygon([box(0, 0, 10, 10), box(20, 0, 30, 10)])
    PcbCanvas().render_session(s)              # ne lève pas AttributeError


def test_refresh_settings_preserves_out_of_range_values(qapp):
    """Ouvrir un projet ne doit pas clamper/réécrire des valeurs hors plage."""
    from laserpcb.gui.main_window import MainWindow
    w = MainWindow()
    w._import("examples/module_relais")
    w.session.project.kerf = 0.005            # sous le minimum du spin-box (0.01)
    w._refresh_settings()
    assert w.session.project.kerf == 0.005    # inchangé (signaux bloqués)


def test_stream_worker_engraves_via_fakegrbl(qapp):
    """Le thread de streaming envoie bien tout le G-code à un FakeGRBL."""
    from laserpcb.gui.send_panel import StreamWorker
    from laserpcb.sender import FakeGrbl, GrblSender

    fake = FakeGrbl()
    sender = GrblSender(fake)
    sender.connect()
    worker = StreamWorker(sender, "G21\nG1 X5 F600 S500\nM5\n")
    worker.start()
    worker.wait(5000)                          # attend la fin du thread (5 s max)
    assert fake.received == ["G21", "G1 X5 F600 S500", "M5"]
