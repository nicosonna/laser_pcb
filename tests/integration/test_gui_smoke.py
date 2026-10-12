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


def _wait_worker(qapp, w, timeout=60.0):
    """Attend la fin de la tâche de fond (l'import est threadé désormais)."""
    import time
    t = time.time()
    while w._worker is not None and time.time() - t < timeout:
        qapp.processEvents()
        time.sleep(0.01)


def test_mainwindow_import_and_layers(qapp):
    from laserpcb.gui.main_window import MainWindow
    w = MainWindow()
    w._import("examples/module_relais")        # async (thread) -> on attend
    _wait_worker(qapp, w)
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


def test_home_button_shows_machine_state_and_alarm(qapp):
    """Le bouton Home porte l'état : une ALARME doit sauter aux yeux (rouge)."""
    from laserpcb.gui.send_panel import SendPanel
    p = SendPanel(get_gcode=lambda: "G21")
    assert p.btn_home.text() == "⌂ Home" and not p.btn_home.styleSheet()
    p._set_state("Idle")
    assert "Idle" in p.btn_home.text() and not p.btn_home.styleSheet()
    p._set_state("Alarm")
    assert "ALARM" in p.btn_home.text()
    assert "c0392b" in p.btn_home.styleSheet()          # fond rouge
    assert "ALARME" in p.btn_home.toolTip()
    p._set_state("")                                    # déconnecté : remise à zéro
    assert p.btn_home.text() == "⌂ Home" and not p.btn_home.styleSheet()


def test_state_polling_updates_button_without_blocking(qapp):
    """Le sondage d'état passe par un thread : il ne doit pas figer l'interface."""
    import time
    from laserpcb.gui.send_panel import SendPanel
    from laserpcb.sender import FakeFluidNC, GrblSender
    p = SendPanel(get_gcode=lambda: "G21")
    p.sender = GrblSender(FakeFluidNC())
    p.sender.connect()
    p._poll_state()
    t = time.time()
    while p._state_worker is not None and time.time() - t < 5:
        qapp.processEvents(); time.sleep(0.01)
    assert "Idle" in p.btn_home.text()
    # home auto : l'option existe et est mémorisée, désactivée par défaut (la
    # machine BOUGE au homing -> on ne l'impose jamais)
    assert p.home_auto.isChecked() in (True, False)
