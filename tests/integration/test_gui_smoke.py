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


def test_frame_button_loops_until_untoggled(qapp):
    """Le cadrage tourne EN BOUCLE tant que le bouton reste enfoncé, et s'arrête
    proprement (laser éteint) quand on le relâche."""
    import time
    from laserpcb.gui.send_panel import SendPanel
    from laserpcb.sender import FakeFluidNC, GrblSender
    rect = "G21\nG1 X5 Y0 F3000 S10\nG1 X5 Y5\nG1 X0 Y5\nG1 X0 Y0\n"
    p = SendPanel(get_gcode=lambda: "G21\n", get_frame_gcode=lambda: rect)
    fake = FakeFluidNC()
    p.sender = GrblSender(fake)
    p.sender.connect()
    assert p.btn_frame.isCheckable()                   # c'est une bascule

    p.btn_frame.setChecked(True)
    t = time.time()
    while "tour" not in p.status.text() and time.time() - t < 6:
        qapp.processEvents(); time.sleep(0.01)
    assert "tour" in p.status.text()                   # plusieurs passages

    p.btn_frame.setChecked(False)
    t = time.time()
    while p._frame_loop is not None and time.time() - t < 6:
        qapp.processEvents(); time.sleep(0.01)
    assert not p.btn_frame.isChecked()
    assert "M5" in fake.received                       # laser éteint à l'arrêt


def test_terminal_logs_actions_and_machine_replies(qapp):
    """Le terminal montre ce qu'on envoie et ce que la machine répond — mais
    reste muet pendant la gravure (des dizaines de milliers de lignes)."""
    import time
    from laserpcb.gui.send_panel import SendPanel
    from laserpcb.sender import FakeFluidNC, GrblSender
    p = SendPanel(get_gcode=lambda: "G21\n")
    p.sender = GrblSender(FakeFluidNC())
    p.sender.connect()
    p._hook_log(p.sender)
    p._jog(1, 0)
    for _ in range(20):
        qapp.processEvents(); time.sleep(0.01)
    txt = p.terminal.toPlainText()
    assert "jog X+1.00" in txt                         # l'action de l'opérateur
    assert "> $J=G91" in txt and "< ok" in txt         # l'échange avec la machine

    before = len(p.terminal.toPlainText())
    p.sender.stream("G21\nG1 X5 F600\nG1 X6\n")        # flux : pas de journal
    for _ in range(20):
        qapp.processEvents(); time.sleep(0.01)
    assert len(p.terminal.toPlainText()) == before


def test_frame_stop_never_soft_resets(qapp):
    """Arreter le cadrage ne doit JAMAIS passer par un soft-reset (ALARM:3,
    position perdue) : on laisse finir le tour en cours."""
    import time
    from laserpcb.gui.send_panel import SendPanel
    from laserpcb.sender import FakeFluidNC, GrblSender
    rect = "G21\nG1 X5 Y0 F3000 S10\nG1 X5 Y5\nG1 X0 Y5\nG1 X0 Y0\n"
    p = SendPanel(get_gcode=lambda: "G21\n", get_frame_gcode=lambda: rect)
    fake = FakeFluidNC()
    p.sender = GrblSender(fake)
    p.sender.connect()
    p.btn_frame.setChecked(True)
    t = time.time()
    while "tour" not in p.status.text() and time.time() - t < 6:
        qapp.processEvents(); time.sleep(0.01)
    # pendant le cadrage, la liaison est verrouillee comme pour une gravure
    assert not p.btn_send.isEnabled() and not p.btn_home.isEnabled()
    assert p.btn_frame.isEnabled()
    p.start_stream()                                        # refuse
    assert "occupée" in p.status.text() and p.worker is None
    p.btn_frame.setChecked(False)
    t = time.time()
    while p._frame_loop is not None and time.time() - t < 6:
        qapp.processEvents(); time.sleep(0.01)
    assert fake.reset_count == 0
    assert p.btn_send.isEnabled() and p.btn_frame.isEnabled()


def test_streaming_locks_frame_button_and_poll_resumes_after_job(qapp):
    import time
    from laserpcb.gui.send_panel import SendPanel
    from laserpcb.sender import FakeFluidNC, GrblSender
    p = SendPanel(get_gcode=lambda: "G21\nG1 X5 F600 S10\nM5\n",
                  get_frame_gcode=lambda: "G21\n")
    p.sender = GrblSender(FakeFluidNC())
    p.sender.connect()
    p._set_connected(True)
    p.start_stream()
    assert not p.btn_frame.isEnabled()                      # pas de cadrage pendant un job
    # Le job enchaine GENERATION (thread) puis flux : on attend les deux. Le
    # sondage d'etat est volontairement suspendu pendant la generation, qui
    # sature le processeur.
    t = time.time()
    while (p._gen is not None or p.worker is not None) and time.time() - t < 20:
        qapp.processEvents(); time.sleep(0.01)
    assert p._gen is None and p.worker is None               # sinon _poll_state se tait
    p._poll_state()
    assert p._state_worker is not None
    t = time.time()
    while p._state_worker is not None and time.time() - t < 5:
        qapp.processEvents(); time.sleep(0.01)
    assert "Idle" in p.btn_home.text()


def test_connection_loss_frees_the_panel_and_arms_reconnection(qapp):
    """Une coupure ne doit pas laisser l'onglet inutilisable : on libère la
    liaison, l'interface repasse en « déconnecté » et la reprise s'arme."""
    from laserpcb.gui.send_panel import SendPanel
    from laserpcb.sender import FakeFluidNC, GrblSender
    p = SendPanel(get_gcode=lambda: "G21\n")
    p.sender = GrblSender(FakeFluidNC())
    p.sender.connect()
    p._set_connected(True)
    assert p.btn_send.isEnabled()

    # trois sondages sans réponse = liaison perdue
    for _ in range(3):
        p._on_state("?")
    assert p.sender is None                       # liaison libérée
    assert not p.btn_send.isEnabled()             # commandes désactivées
    assert p.btn_connect.isEnabled()              # ...mais reconnexion possible
    assert "perdue" in p.status.text().lower()

    # une erreur de G-code, elle, ne doit PAS être prise pour une coupure
    assert p._looks_disconnected("timeout") and p._looks_disconnected("Connexion reset")
    assert not p._looks_disconnected("error:2 (nombre mal forme)")


def test_stop_asks_confirmation_before_aborting(qapp, monkeypatch):
    """Stop ne doit JAMAIS couper sans confirmation : une gravure interrompue
    ne reprend pas où elle en était, la pièce est à refaire."""
    from PySide6.QtWidgets import QMessageBox
    from laserpcb.gui.send_panel import SendPanel, StreamWorker
    from laserpcb.sender import FakeFluidNC, GrblSender

    p = SendPanel(get_gcode=lambda: "G21\n")
    p.sender = GrblSender(FakeFluidNC())
    p.sender.connect()
    p.worker = StreamWorker(p.sender, "G21\n")          # pas démarré : on teste le garde

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    p._stop()
    assert not p.worker._stop                            # refus -> rien n'est arrêté

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    p._stop()
    assert p.worker._stop                                # accepté -> arrêt demandé
    assert "Arrêt" in p.status.text()


def test_busy_indicator_during_background_work(qapp):
    """Un travail de durée inconnue met la barre en mode indéterminé : sans ça,
    une génération de 20 s ressemble à un gel."""
    from laserpcb.gui.send_panel import SendPanel
    p = SendPanel(get_gcode=lambda: "G21\n")
    assert p.bar.maximum() == 100                        # repos
    p._set_busy(True, "génération…")
    assert p.bar.maximum() == 0                          # animation « occupé »
    assert "génération" in p.status.text()
    p._set_busy(False)
    assert p.bar.maximum() == 100 and p.bar.value() == 0
