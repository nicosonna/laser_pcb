"""Les deux étalonnages courts du panneau caméra.

Fichier séparé de ``test_camera_panel.py``, qui couvre la connexion, le flux et
l'assistant complet en quatre pointés. Ici, les deux modes qui évitent de
reprendre cet assistant :

* **le point de tir seul** — un clic, l'échelle déjà mesurée est conservée ;
* **l'offset du gabarit** — mesuré à la caméra, et AJOUTÉ à celui de l'onglet
  Envoi, ce qui évite de deviner le sens de la convention.

La calibration est posée à la main et l'image injectée directement : ces tests
portent sur les deux modes, pas sur le flux MJPEG.
"""

from __future__ import annotations

import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

pytest.importorskip("PySide6")

from PySide6.QtCore import QBuffer, QIODevice, QSettings, Qt   # noqa: E402
from PySide6.QtGui import QImage                               # noqa: E402

from laserpcb.camera import FakeCamera                         # noqa: E402
from laserpcb.camera_cal import PixelMm                        # noqa: E402
from laserpcb.gui import camera_panel as cp                    # noqa: E402
from laserpcb.gui.camera_panel import CameraPanel              # noqa: E402

# Calibration volontairement ronde : 0,1 mm par pixel, sans rotation, tir au
# centre d'une image 800x600. Un clic a +8 px vaut donc +0,80 mm, ce qui rend
# les attentes des tests lisibles sans calcul.
CAL = PixelMm(400.0, 300.0, 0.1, 0.0, 0.0, 0.1, 800, 600)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _reglages_propres():
    QSettings("laser_pcb", "laser_pcb").clear()
    yield


class _FakeMachine:
    """Graveuse de papier, avec l'offset du gabarit en plus.

    Le panneau ne connait la machine qu'a travers ``SendPanelMachine`` : ce
    faux suffit donc, a condition d'offrir les memes gestes.
    """

    def __init__(self, jig=(-4.5, 4.35), ready=True):
        self._ready = ready
        self.jig = jig
        self.jogs: list = []
        self.zeros = 0
        self.pointeur: list = []

    def ready(self) -> bool:
        return self._ready

    def jog(self, dx, dy):
        self.jogs.append((dx, dy))

    def set_zero(self):
        self.zeros += 1

    def pointer(self, on):
        self.pointeur.append(bool(on))

    def jig_offset(self) -> tuple:
        return self.jig

    def set_jig_offset(self, dx, dy) -> None:
        self.jig = (dx, dy)


def _vrai_jpeg(w: int = 16, h: int = 12) -> bytes:
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(Qt.red)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    assert img.save(buf, "JPG")
    return bytes(buf.data())


def _panneau(cal=CAL, machine=True, **kw):
    """Panneau calibre, avec une image a la taille de la calibration.

    L'image est posee directement plutot que recue du flux : le faux JPEG fait
    16x12, et une calibration mesuree a cette taille ne dirait rien de
    lisible.
    """
    fake = FakeCamera(jpeg=_vrai_jpeg(), **kw)
    p = CameraPanel(client_factory=lambda host, **_: fake.client())
    p.host.setText("fake.local")
    p.cal = cal
    img = QImage(CAL.ref_w, CAL.ref_h, QImage.Format_RGB32)
    img.fill(Qt.darkGray)
    p.view.set_image(img)
    m = _FakeMachine() if machine else None
    p.set_machine(m)
    p._refresh_cal_label()
    p._refresh_target()
    return p, m


def _attendre(qapp, cond, limite=5.0) -> bool:
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _oui(monkeypatch):
    monkeypatch.setattr(cp.QMessageBox, "question",
                        staticmethod(lambda *a, **k: cp.QMessageBox.Yes))


def _non(monkeypatch):
    monkeypatch.setattr(cp.QMessageBox, "question",
                        staticmethod(lambda *a, **k: cp.QMessageBox.No))


# --------------------------------------------------------------------------- #
# Point de tir
# --------------------------------------------------------------------------- #
def test_etalonner_le_point_de_tir_garde_lechelle(qapp):
    """Tout l'intérêt du mode : un clic, et l'échelle mesurée est conservée."""
    p, m = _panneau()
    p._spot_toggle()
    assert p._spot_mode, "le mode ne s'est pas ouvert"
    assert m.pointeur == [True], "le point de tir doit être rendu visible"

    p._on_click(420.0, 290.0)
    assert not p._spot_mode, "le mode se ferme sur le clic"
    assert (p.cal.u0, p.cal.v0) == (420.0, 290.0)
    assert (p.cal.a, p.cal.b, p.cal.c, p.cal.d) == (CAL.a, CAL.b, CAL.c, CAL.d)
    assert m.pointeur == [True, False], "le pointeur ne reste pas allumé"
    p.shutdown()


def test_etalonner_le_point_de_tir_ne_bouge_pas_la_tete(qapp):
    """La différence avec l'assistant complet : aucun déplacement.

    C'est ce qui le rend utilisable sur une pièce déjà posée et alignée.
    """
    p, m = _panneau()
    p._spot_toggle()
    p._on_click(415.0, 305.0)
    assert m.jogs == [], "la tête a bougé pendant un simple repointé"
    p.shutdown()


def test_le_reticule_suit_le_point_de_tir_etalonne(qapp):
    """Le réticule découle de la calibration, il n'est pas réglé à tâtons."""
    p, _ = _panneau()
    p._spot_toggle()
    p._on_click(412.0, 293.0)
    assert (p.cross_x.value(), p.cross_y.value()) == (12, -7)
    p.shutdown()


def test_le_point_de_tir_etalonne_est_memorise(qapp):
    p, _ = _panneau()
    p._spot_toggle()
    p._on_click(433.0, 288.0)
    brut = QSettings("laser_pcb", "laser_pcb").value("cam_cal", "", type=str)
    assert json.loads(brut)["u0"] == 433.0
    p.shutdown()


def test_la_cible_visee_est_oubliee_apres_etalonnage(qapp):
    """Un écart en mm lu depuis l'ANCIEN point de tir ne veut plus rien dire."""
    p, _ = _panneau()
    p._on_click(500.0, 400.0)
    assert p._target is not None
    p._spot_toggle()
    p._on_click(420.0, 290.0)
    assert p._target is None, "la cible pointe encore depuis l'ancien tir"
    p.shutdown()


def test_sans_calibration_le_point_de_tir_renvoie_vers_lassistant(qapp):
    """Il n'y a pas d'échelle à conserver : le mode n'a pas de sens."""
    p, m = _panneau(cal=None)
    p._spot_toggle()
    assert not p._spot_mode
    assert "Calibrer" in p.status.text()
    assert m.pointeur == [], "pointeur allumé pour rien"
    p.shutdown()


def test_letalonnage_du_point_de_tir_sannule(qapp):
    p, m = _panneau()
    p._spot_toggle()
    p._spot_toggle()
    assert not p._spot_mode
    assert m.pointeur == [True, False]
    assert "annulé" in p.status.text()
    assert p.cal == CAL, "une annulation ne doit rien changer"
    p.shutdown()


def test_une_gravure_interrompt_letalonnage_du_point_de_tir(qapp):
    """Le flux est coupé pendant la gravure : plus rien à viser.

    Et un pointeur laissé allumé sur la machine ne se verrait pas.
    """
    p, m = _panneau()
    p._spot_toggle()
    p.set_locked(True)
    assert not p._spot_mode
    assert m.pointeur == [True, False]
    assert "point de tir" in p.status.text()
    p.shutdown()


def test_le_bouton_du_point_de_tir_attend_une_calibration(qapp):
    """Sans calibration le bouton est éteint : le message ne suffit pas."""
    p, _ = _panneau(cal=None, frames=2)
    p.toggle_connect()
    assert _attendre(qapp, lambda: p.client is not None)
    assert not p.btn_spot.isEnabled()
    p.cal = CAL
    p._refresh_buttons()
    assert p.btn_spot.isEnabled()
    p.shutdown()


# --------------------------------------------------------------------------- #
# Offset du gabarit
# --------------------------------------------------------------------------- #
def test_loffset_du_gabarit_sajoute_a_lexistant(qapp, monkeypatch):
    """La correction s'AJOUTE, et c'est ce qui la rend sûre.

    La tête est allée au repère avec l'offset courant ; ce que la caméra voit
    encore d'écart EST l'erreur restante. Aucune convention de signe à deviner
    — et si le signe était faux, l'écart doublerait au lieu de s'annuler.
    """
    _oui(monkeypatch)
    p, m = _panneau()
    p._on_click(408.0, 297.0)                 # +8 px / -3 px = +0,80 / -0,30 mm
    p._jig_measure()
    assert m.jig == pytest.approx((-4.5 + 0.80, 4.35 - 0.30))
    p.shutdown()


def test_une_seconde_mesure_a_zero_ne_change_plus_loffset(qapp, monkeypatch):
    """Le critère d'arrêt, et la preuve que la boucle converge."""
    _oui(monkeypatch)
    p, m = _panneau()
    p._on_click(CAL.u0, CAL.v0)               # le repère EST sur le point de tir
    p._jig_measure()
    assert m.jig == pytest.approx((-4.5, 4.35))
    p.shutdown()


def test_un_refus_laisse_loffset_du_gabarit_intact(qapp, monkeypatch):
    """La boîte de dialogue montre l'avant et l'après : elle doit pouvoir dire non."""
    _non(monkeypatch)
    p, m = _panneau()
    p._on_click(408.0, 297.0)
    p._jig_measure()
    assert m.jig == (-4.5, 4.35)
    assert "inchangé" in p.status.text()
    p.shutdown()


def test_sans_calibration_loffset_du_gabarit_ne_se_mesure_pas(qapp, monkeypatch):
    """Sans échelle, un clic ne vaut aucun millimètre."""
    _oui(monkeypatch)
    p, m = _panneau(cal=None)
    p._on_click(408.0, 297.0)
    p._jig_measure()
    assert m.jig == (-4.5, 4.35)
    assert "calibrée" in p.status.text()
    p.shutdown()


def test_sans_graveuse_loffset_du_gabarit_le_dit(qapp, monkeypatch):
    """L'offset vit dans l'onglet Envoi : sans machine, il n'y a rien à écrire."""
    _oui(monkeypatch)
    p, _ = _panneau(machine=False)
    p._on_click(408.0, 297.0)
    p._jig_measure()
    assert "graveuse" in p.status.text()
    p.shutdown()


def test_le_bouton_doffset_du_gabarit_attend_une_cible(qapp):
    p, _ = _panneau()
    assert not p.btn_jig.isEnabled(), "rien n'est visé"
    p._on_click(408.0, 297.0)
    assert p.btn_jig.isEnabled()
    p.shutdown()


def test_loffset_du_gabarit_oublie_la_cible_une_fois_applique(qapp, monkeypatch):
    """La tête doit repartir au repère avant la mesure suivante.

    Garder la cible laisserait croire qu'elle vaut encore quelque chose, et
    une seconde validation ajouterait deux fois la même correction.
    """
    _oui(monkeypatch)
    p, _ = _panneau()
    p._on_click(408.0, 297.0)
    p._jig_measure()
    assert p._target is None
    assert not p.btn_jig.isEnabled()
    p.shutdown()
