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
    # le canvas se rend sans erreur
    w.canvas.render_session(w.session)
