"""Panneau camera : connexion, flux, reticule, croix laser, verrou de gravure.

Aucune carte sur le reseau : ``FakeCamera`` est injectee par
``client_factory``, comme ``FakeGrbl`` l'est par le transport du sender.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

pytest.importorskip("PySide6")

from PySide6.QtCore import QBuffer, QIODevice, Qt      # noqa: E402
from PySide6.QtGui import QImage, QPixmap              # noqa: E402

from laserpcb.camera import FakeCamera, Found          # noqa: E402
from laserpcb.gui import camera_panel as cp            # noqa: E402
from laserpcb.gui.camera_panel import CameraPanel, CameraView  # noqa: E402

# Originaux a remettre en place : ces tests remplacent la detection reseau et
# le selecteur de fichier, qui demanderaient l'un un reseau, l'autre un clic.
_DISCOVER = cp.discover
_SAVE_NAME = cp.QFileDialog.getSaveFileName


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _vrai_jpeg() -> bytes:
    """Un JPEG valide : le panneau refuse une image qu'il ne sait pas decoder,
    donc un faux tableau d'octets ne prouverait rien."""
    img = QImage(16, 12, QImage.Format_RGB32)
    img.fill(Qt.red)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    assert img.save(buf, "JPG")
    return bytes(buf.data())


def _panneau(qapp, **kw):
    fake = FakeCamera(jpeg=_vrai_jpeg(), **kw)
    p = CameraPanel(client_factory=lambda host, **_: fake.client())
    p.host.setText("fake.local")
    return p, fake


def _attendre(qapp, cond, limite=5.0):
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_le_panneau_souvre_deconnecte(qapp):
    p, _ = _panneau(qapp)
    assert p.client is None
    assert not p.btn_laser.isEnabled(), "rien de pilotable sans connexion"
    assert p.btn_connect.text() == "Connecter"
    p.shutdown()


def test_connexion_puis_image_a_lecran(qapp):
    p, fake = _panneau(qapp, frames=3)
    p.toggle_connect()
    assert p.client is not None
    assert "lasercam" in p.status.text()
    assert p.btn_laser.isEnabled()
    assert _attendre(qapp, lambda: p.view.image is not None), "aucune image recue"
    assert p.view.image.width() == 16
    p.shutdown()


def test_une_camera_injoignable_ne_plante_pas(qapp):
    fake = FakeCamera(fail_on="/status")
    p = CameraPanel(client_factory=lambda host, **_: fake.client())
    p.host.setText("fake.local")
    p.toggle_connect()
    assert p.client is None
    assert "Injoignable" in p.status.text()
    p.shutdown()


def test_la_gravure_coupe_le_flux(qapp):
    """Le G-code et la video se disputent le meme WiFi : pendant une gravure,
    le reseau appartient a la machine."""
    p, _ = _panneau(qapp, frames=200)
    p.toggle_connect()
    assert _attendre(qapp, lambda: p.worker is not None)

    p.set_locked(True)
    assert p.worker is None, "le flux doit etre coupe"
    assert not p.btn_laser.isEnabled() and not p.btn_connect.isEnabled()
    p.start_stream()
    assert p.worker is None, "et ne doit pas pouvoir repartir pendant la gravure"

    p.set_locked(False)
    assert p.btn_laser.isEnabled()
    p.shutdown()


def test_la_croix_laser_est_eteinte_a_la_deconnexion(qapp):
    """Une croix oubliee allumee reste allumee sur la machine."""
    p, fake = _panneau(qapp)
    p.toggle_connect()
    p.btn_laser.setChecked(True)
    assert fake.laser_on
    assert fake.laser_ms > 0, "la carte doit avoir une duree de securite"
    p.disconnect_camera()
    assert not fake.laser_on
    assert not p.btn_laser.isChecked()
    p.shutdown()


def test_eclairage_et_taille_dimage_partent_a_la_carte(qapp):
    p, fake = _panneau(qapp)
    p.toggle_connect()
    assert fake.settings.get("framesize") == p.size.currentData()
    p.size.setCurrentIndex(0)
    assert fake.settings["framesize"] == p.size.currentData()
    p.led.setValue(60)
    p._on_led()
    assert fake.led_level == 60
    p.shutdown()


def test_le_reticule_est_memorise(qapp):
    """C'est une calibration du montage : elle survit a la fermeture."""
    from PySide6.QtCore import QSettings
    cfg = QSettings("laser_pcb", "laser_pcb")
    avant = (cfg.value("cam_cross_x", 0, type=int),
             cfg.value("cam_cross_y", 0, type=int))
    try:
        p, _ = _panneau(qapp)
        p.cross_x.setValue(37)
        p.cross_y.setValue(-12)
        assert p.view.cross == (37, -12)
        assert cfg.value("cam_cross_x", type=int) == 37
        assert cfg.value("cam_cross_y", type=int) == -12
        p.shutdown()
    finally:
        # On ne jette pas la calibration reelle de l'utilisateur.
        cfg.setValue("cam_cross_x", avant[0])
        cfg.setValue("cam_cross_y", avant[1])


def test_detection_mdns_remplit_lhote(qapp):
    p, _ = _panneau(qapp)
    cp.discover = lambda extra=(): [Found("10.0.0.7", "lasercam", -61)]
    try:
        p.scan()
        assert _attendre(qapp, lambda: p._scan is None)
    finally:
        cp.discover = _DISCOVER
    assert p.host.text() == "10.0.0.7"
    assert "lasercam" in p.status.text()
    p.shutdown()


def test_detection_sans_resultat_le_dit(qapp):
    p, _ = _panneau(qapp)
    cp.discover = lambda extra=(): []
    try:
        p.scan()
        assert _attendre(qapp, lambda: p._scan is None)
    finally:
        cp.discover = _DISCOVER
    assert "Aucune caméra" in p.status.text()
    assert p.btn_scan.isEnabled(), "le bouton doit redevenir cliquable"
    p.shutdown()


def test_une_detection_qui_leve_ne_plante_pas(qapp):
    """Un reseau capricieux ne doit pas emporter la fenetre."""
    p, _ = _panneau(qapp)

    def casse(extra=()):
        raise OSError("socket mDNS refuse")

    cp.discover = casse
    try:
        p.scan()
        assert _attendre(qapp, lambda: p._scan is None)
    finally:
        cp.discover = _DISCOVER
    assert "Aucune caméra" in p.status.text()
    p.shutdown()


def test_un_flux_interrompu_est_signale(qapp):
    """La camera qui ferme son flux (reset, WiFi) doit se voir a l'ecran."""
    p, _ = _panneau(qapp, frames=1)
    p.toggle_connect()
    assert _attendre(qapp, lambda: "Flux perdu" in p.status.text())
    assert p.worker is None
    p.shutdown()


def test_une_image_illisible_ne_remplace_pas_la_precedente(qapp):
    p, _ = _panneau(qapp, frames=2)
    p.toggle_connect()
    assert _attendre(qapp, lambda: p.view.image is not None)
    bonne = p.view.image
    p._on_frame(b"\xff\xd8\xff pas vraiment du jpeg \xff\xd9")
    assert p.view.image is bonne, "on garde la derniere image valide"
    p.shutdown()


def test_un_reglage_refuse_est_signale(qapp):
    """La carte peut refuser (capteur occupe, reset en cours) : on le dit au
    lieu de laisser croire que c'est passe."""
    fake = FakeCamera(jpeg=_vrai_jpeg(), fail_on="/led")
    p = CameraPanel(client_factory=lambda host, **_: fake.client())
    p.host.setText("fake.local")
    p.toggle_connect()
    p.led.setValue(40)
    p._on_led()
    assert "Éclairage refusé" in p.status.text()
    p.shutdown()


def test_enregistrement_dune_image(qapp, tmp_path):
    """L'image enregistree vient de /capture, pas du flux : le flux est plus
    comprime, et l'etalonnage se joue au pixel."""
    p, fake = _panneau(qapp)
    p.toggle_connect()
    cible = tmp_path / "cam.jpg"
    cp.QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (str(cible), "JPEG (*.jpg)"))
    try:
        p._save_shot()
    finally:
        cp.QFileDialog.getSaveFileName = _SAVE_NAME
    assert cible.read_bytes() == fake.jpeg
    assert any("/capture" in u for u in fake.requests)
    p.shutdown()


def test_le_mode_point_dacces_est_annonce(qapp):
    """En point d'acces la camera marche, mais la graveuse n'est pas sur ce
    reseau : le dire, sinon l'utilisateur cherchera la machine en vain."""
    fake = FakeCamera(jpeg=_vrai_jpeg(), mode="ap")
    p = CameraPanel(client_factory=lambda host, **_: fake.client())
    p.host.setText("192.168.4.1")
    p.toggle_connect()
    assert p.client is not None
    assert "POINT D'ACCÈS" in p.status.text()
    assert "graveuse" in p.status.text()
    p.shutdown()


def test_la_page_reseau_reste_joignable_deconnecte(qapp):
    """C'est souvent la raison pour laquelle on n'arrive pas a se connecter :
    le bouton doit donc marcher AVANT toute connexion."""
    ouvertes: list[str] = []
    fake = FakeCamera(jpeg=_vrai_jpeg())
    p = CameraPanel(client_factory=lambda host, **_: fake.client(),
                    url_opener=ouvertes.append)
    p.host.setText("fake.local")
    assert p.btn_wifi.isEnabled(), "joignable meme deconnecte"
    p._open_config()
    assert ouvertes == ["http://fake.local:80/wifi"]
    p.shutdown()


def test_la_gravure_verrouille_aussi_la_page_reseau(qapp):
    p, _ = _panneau(qapp)
    p.set_locked(True)
    assert not p.btn_wifi.isEnabled()
    p.set_locked(False)
    assert p.btn_wifi.isEnabled()
    p.shutdown()


def test_sans_camera_trouvee_le_point_dacces_est_suggere(qapp):
    p, _ = _panneau(qapp)
    cp.discover = lambda extra=(): []
    try:
        p.scan()
        assert _attendre(qapp, lambda: p._scan is None)
    finally:
        cp.discover = _DISCOVER
    assert "lasercam-setup" in p.status.text()
    p.shutdown()


def test_la_vue_se_dessine_avec_et_sans_image(qapp):
    """Le reticule est ancre sur l'IMAGE : sans cet ancrage il se deplace par
    rapport a la piece des qu'on redimensionne, et la calibration ne veut plus
    rien dire."""
    v = CameraView()
    v.resize(400, 300)
    v.render(QPixmap(v.size()))           # sans image : juste le fond
    assert v._image_rect() is None

    img = QImage(40, 20, QImage.Format_RGB32)
    img.fill(Qt.blue)
    v.set_image(img)
    rect, scale = v._image_rect()
    # ratio 2:1 dans un widget 400x300 : largeur limitante, image centree
    assert (rect.width(), rect.height()) == (400, 200)
    assert rect.top() == 50 and abs(scale - 10.0) < 1e-9

    v.set_cross(10, -4)
    v.render(QPixmap(v.size()))
    assert v.cross == (10, -4)
