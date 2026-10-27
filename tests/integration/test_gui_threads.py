"""Fin de vie des QThread de l'interface.

Lâcher la DERNIÈRE référence d'un ``QThread`` encore en course fait appeler
``qFatal()`` à Qt : le process meurt sur-le-champ, sans exception ni dump. Le
défaut est donc invisible dans les journaux — d'où ces tests.
"""
import os
import re
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

pytest.importorskip("PySide6")

from PySide6.QtCore import QThread          # noqa: E402

from laserpcb.gui import threads            # noqa: E402

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app

ROOT = Path(__file__).resolve().parents[2]

# Attributs qui tiennent un worker. Les lâcher sans passer par ``retire`` est
# mortel dès que le fil n'est pas encore sorti de ``run()``.
WORKER_ATTRS = ("self._state_worker", "self._cmd", "self._conn",
                "self._frame_loop", "self._gen", "self._scan", "self.worker",
                "self._worker")


class _Slow(QThread):
    def run(self):
        time.sleep(0.4)


def test_retire_garde_la_reference_tant_que_le_thread_court(qapp):
    w = _Slow()
    w.start()
    threads._alive.clear()
    threads.retire(w)
    assert threads.pending() == 1, "un thread en course doit rester référencé"
    assert w.wait(5000)
    threads._sweep()
    assert threads.pending() == 0, "un thread terminé doit être libéré"


def test_retire_ignore_un_thread_deja_termine(qapp):
    w = _Slow()
    w.start()
    assert w.wait(5000)
    threads._alive.clear()
    threads.retire(w)
    assert threads.pending() == 0


@pytest.mark.parametrize("module", ["send_panel.py", "main_window.py",
                                    "camera_panel.py"])
def test_aucun_worker_nest_lache_sans_retire(module):
    """Chaque ``self._xxx = None`` sur un worker doit être précédé d'un retire."""
    lines = (ROOT / "laserpcb" / "gui" / module).read_text(
        encoding="utf-8").splitlines()
    fautes = []
    for i, line in enumerate(lines):
        m = re.match(r"\s*(self\.[A-Za-z_]+) = None\b", line)
        if not m or m.group(1) not in WORKER_ATTRS:
            continue
        # le constructeur CRÉE les attributs : il n'y a rien à mettre à la retraite
        enclosing = next((lines[j] for j in range(i, -1, -1)
                          if re.match(r"\s*def ", lines[j])), "")
        if "def __init__" in enclosing:
            continue
        avant = " ".join(lines[max(0, i - 4):i])
        # ``retire()`` met le fil en sursis ; ``wait()`` l'attend carrément —
        # plus fort encore, et c'est ce qu'on fait à la fermeture.
        if "retire(" not in avant and ".wait(" not in avant:
            fautes.append(f"{module}:{i + 1} : {line.strip()}")
    assert not fautes, ("référence de worker lâchée sans retire() ni wait() — "
                        "plantage silencieux garanti :\n" + "\n".join(fautes))


def test_loffset_du_gabarit_ne_touche_que_la_tete(qapp):
    """L'offset déplace le POINT VISÉ, jamais le travail.

    Le gabarit est aligné SUR la tête : décaler la gravure en plus
    doublerait la correction. L'invariant est donc : même G-code quel que soit
    l'offset, et seul ``_goto_piece`` en tient compte.
    """
    from laserpcb.gui.send_panel import SendPanel
    from laserpcb.sender import FakeFluidNC, GrblSender

    gcode = "G21\nG90\nG1 X10.000 Y20.000 F600 S10\nM5\n"
    origin = (143.95, 168.45)
    p = SendPanel(get_gcode=lambda: gcode,
                  get_frame_gcode=lambda: gcode,
                  get_piece_origin=lambda: origin)
    s = GrblSender(FakeFluidNC())
    s.connect()
    vise = []
    s.goto = lambda x, y: vise.append((round(x, 3), round(y, 3)))
    p.sender = s
    p._set_connected(True)

    for jx, jy in ((0.0, 0.0), (-4.5, 4.35), (12.0, -7.0)):
        p.jig_x.setValue(jx)
        p.jig_y.setValue(jy)
        assert p._get_gcode() == gcode, "l'offset ne doit RIEN changer au G-code"
        p._goto_piece()

    assert vise == [(143.95, 168.45), (139.45, 172.8), (155.95, 161.45)], vise


def test_larret_attend_les_fils(qapp):
    """Fermer en laissant un fil en course fait appeler qFatal() à Qt.

    Le plantage survient APRÈS le code de sortie : il n'apparaît que dans le
    journal, d'où son invisibilité jusqu'à ce qu'on journalise Qt lui-même.
    """
    w = _Slow()
    w.start()
    threads._alive.clear()
    threads.retire(w)
    assert threads.pending() == 1
    threads.shutdown()
    assert threads.pending() == 0
    assert w.isFinished(), "shutdown() doit ATTENDRE, pas seulement oublier"


def test_la_fenetre_se_ferme_sans_fil_en_course(qapp):
    """Parcours complet : la fenêtre arrête tout ce qu'elle a lancé."""
    from laserpcb.gui.main_window import MainWindow
    w = MainWindow()
    w.show()
    qapp.processEvents()
    w.close()
    qapp.processEvents()
    assert threads.pending() == 0
