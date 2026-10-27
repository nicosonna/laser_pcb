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
from laserpcb.camera_cal import PixelMm                # noqa: E402
from laserpcb.gui import camera_panel as cp            # noqa: E402
from laserpcb.gui.camera_panel import CameraPanel, CameraView  # noqa: E402

# Originaux a remettre en place : ces tests remplacent la detection reseau et
# le selecteur de fichier, qui demanderaient l'un un reseau, l'autre un clic.
_DISCOVER = cp.discover
_SAVE_NAME = cp.QFileDialog.getSaveFileName
_QUESTION = cp.QMessageBox.question

# Reglages touches par ces tests : ce sont de VRAIS reglages utilisateur
# (calibration d'un montage physique), on ne les jette pas.
_CLES = ("cam_cross_x", "cam_cross_y", "cam_cal")


@pytest.fixture(autouse=True)
def _protege_les_reglages():
    """Rend ses reglages a l'utilisateur apres chaque test.

    Sans ca, lancer la suite effacerait la calibration reelle de son montage —
    un quart d'heure de pointes a refaire, et rien ne le dirait.
    """
    from PySide6.QtCore import QSettings
    cfg = QSettings("laser_pcb", "laser_pcb")
    avant = {k: cfg.value(k) for k in _CLES}
    yield
    for k, v in avant.items():
        if v is None:
            cfg.remove(k)
        else:
            cfg.setValue(k, v)


class _FakeMachine:
    """Graveuse de papier : note ce qu'on lui demande, ne bouge rien.

    Le panneau camera ne connait la machine qu'a travers ``SendPanelMachine``,
    quatre methodes : ce faux suffit donc a tester tout l'alignement.
    """

    def __init__(self, ready=True, fail_jog=False):
        self._ready = ready
        self.fail_jog = fail_jog
        self.jogs: list = []
        self.zeros = 0
        self.pointeur: list = []

    def ready(self) -> bool:
        return self._ready

    def jog(self, dx, dy):
        if self.fail_jog:
            raise OSError("liaison perdue")
        self.jogs.append((dx, dy))

    def set_zero(self):
        self.zeros += 1

    def pointer(self, on):
        self.pointeur.append(bool(on))


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _vrai_jpeg(w: int = 16, h: int = 12) -> bytes:
    """Un JPEG valide : le panneau refuse une image qu'il ne sait pas decoder,
    donc un faux tableau d'octets ne prouverait rien."""
    img = QImage(w, h, QImage.Format_RGB32)
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


# --------------------------------------------------------------------------- #
# Alignement : clic-pour-aller et calibration pixel <-> mm
# --------------------------------------------------------------------------- #
def _panneau_aligne(qapp, w=320, h=240, machine=None, brancher=True):
    """Panneau connecte, image a l'ecran, machine branchee : l'etat dans
    lequel l'alignement a un sens."""
    jpeg = _vrai_jpeg(w, h)
    fake = FakeCamera(jpeg=jpeg)
    p = CameraPanel(client_factory=lambda host, **_: fake.client())
    p.host.setText("fake.local")
    p.toggle_connect()
    p.stop_stream()                 # l'image est fournie a la main : pas de course
    p._on_frame(jpeg)
    m = machine if machine is not None else _FakeMachine()
    if brancher:
        p.set_machine(m)
    return p, m


def _demarre_calibration(p) -> None:
    """Lance l'assistant en acceptant sa demande de confirmation."""
    cp.QMessageBox.question = staticmethod(lambda *a, **k: cp.QMessageBox.Yes)
    try:
        p._cal_toggle()
    finally:
        cp.QMessageBox.question = _QUESTION


# Quatre pointes qui decrivent un montage a 0,1 mm/px, sans rotation, laser
# au centre d'une image 320x240 : la tete avance de 10 mm, le detail fixe
# recule donc de 100 px.
_TIR = (160.0, 120.0)
_POINTES = [(200.0, 140.0),      # detail, avant de bouger
            (100.0, 140.0),      # apres +10 mm en X
            (100.0, 40.0)]       # apres +10 mm en Y


def _calibre(p) -> None:
    _demarre_calibration(p)
    for pt in (_TIR, *_POINTES):
        p.view.clicked.emit(*pt)


def test_un_clic_ne_bouge_jamais_la_machine_tout_seul(qapp):
    """Une image cliquable qui lance la tete au premier clic distrait serait
    dangereuse : le clic pose une cible, un bouton la valide."""
    p, m = _panneau_aligne(qapp)
    p.view.clicked.emit(200.0, 140.0)
    assert p.view.marker == (200.0, 140.0)
    assert m.jogs == [], "aucun deplacement sans appui sur un bouton"
    assert "calibrez" in p.lbl_target.text().lower()
    p.shutdown()


def test_aller_au_point_sans_calibration_le_dit(qapp):
    p, m = _panneau_aligne(qapp)
    p.view.clicked.emit(200.0, 140.0)
    p._move_to_target(False)
    assert m.jogs == []
    assert "Calibrez" in p.status.text()
    p.shutdown()


def test_calibration_complete_puis_clic_pour_aller(qapp):
    """Le parcours entier : quatre pointes, puis un clic qui amene la tete."""
    p, m = _panneau_aligne(qapp)
    _calibre(p)

    assert p.cal is not None, p.status.text()
    assert p.cal.mm_per_px() == pytest.approx((0.1, 0.1))
    assert p.cal.rotation_deg() == pytest.approx(0.0)
    # La tete est revenue d'ou elle vient : son 0,0 n'a pas bouge.
    assert m.jogs == [(10.0, 0.0), (0.0, 10.0), (-10.0, -10.0)]
    # Le reticule n'est plus un reglage a tatons : il decoule de la mesure.
    assert (p.cross_x.value(), p.cross_y.value()) == (0, 0)
    assert m.pointeur == [True, False], "le pointeur ne reste pas allume"

    m.jogs.clear()
    p.view.clicked.emit(260.0, 120.0)          # 100 px a droite du tir
    assert "+10.00" in p.lbl_target.text()
    p._move_to_target(False)
    assert m.jogs == [pytest.approx((10.0, 0.0))]
    assert p.view.marker is None, "la cible est atteinte : plus de marqueur"
    p.shutdown()


def test_definir_zero_au_point_vise(qapp):
    """C'est ce geste qui remplace le gabarit : on vise un coin de la piece a
    l'ecran, et il devient le 0,0 du travail."""
    p, m = _panneau_aligne(qapp)
    _calibre(p)
    m.jogs.clear()
    p.view.clicked.emit(160.0, 220.0)          # 100 px sous le tir
    p._move_to_target(True)
    assert m.jogs == [pytest.approx((0.0, 10.0))]
    assert m.zeros == 1
    assert "0,0" in p.status.text()
    p.shutdown()


def test_une_calibration_annulee_eteint_le_pointeur_et_revient(qapp):
    """Abandonner en cours de route ne doit laisser ni faisceau allume, ni
    tete decalee d'un pas que l'utilisateur ignore."""
    p, m = _panneau_aligne(qapp)
    _demarre_calibration(p)
    p.view.clicked.emit(*_TIR)                 # pointeur releve
    p.view.clicked.emit(*_POINTES[0])          # declenche le pas en X
    assert m.jogs == [(10.0, 0.0)]
    p._cal_toggle()                            # annule
    # L'annulation reeteint sans rien supposer de l'etat courant : ce qui
    # compte est qu'on l'ait allume une fois, et qu'il finisse eteint.
    assert m.pointeur.count(True) == 1 and m.pointeur[-1] is False
    assert m.jogs == [(10.0, 0.0), (-10.0, 0.0)]
    assert p.cal is None
    assert "annulée" in p.status.text()
    p.shutdown()


def test_la_gravure_interrompt_la_calibration(qapp):
    """L'assistant fait bouger la tete : pendant une gravure, c'est exclu."""
    p, m = _panneau_aligne(qapp)
    _demarre_calibration(p)
    p.view.clicked.emit(*_TIR)
    p.view.clicked.emit(*_POINTES[0])
    p.set_locked(True)
    assert p._cal_step == 0
    assert m.pointeur[-1] is False
    assert m.jogs[-1] == (-10.0, 0.0), "la tete est ramenee"
    assert "gravure" in p.status.text().lower()
    p.shutdown()


def test_la_deconnexion_abandonne_la_calibration(qapp):
    """Sans image, plus rien a pointer — et le pointeur resterait allume."""
    p, m = _panneau_aligne(qapp)
    _demarre_calibration(p)
    p.view.clicked.emit(*_TIR)
    p.disconnect_camera()
    assert p._cal_step == 0
    assert m.pointeur[-1] is False
    p.shutdown()


def test_des_pointes_identiques_sont_refuses(qapp):
    """Quatre clics au meme endroit n'apprennent rien : il faut le dire, pas
    enregistrer une calibration qui enverra la tete n'importe ou."""
    p, m = _panneau_aligne(qapp)
    _demarre_calibration(p)
    for _ in range(4):
        p.view.clicked.emit(160.0, 120.0)
    assert p.cal is None
    assert "refusée" in p.status.text()
    assert m.jogs[-1] == (-10.0, -10.0), "la tete revient quand meme"
    p.shutdown()


def test_un_deplacement_demesure_est_refuse(qapp):
    """Une calibration fausse mais plausible enverrait la tete a l'autre bout
    du plateau : on refuse au-dela d'un seuil, en disant pourquoi."""
    p, m = _panneau_aligne(qapp)
    p.cal = PixelMm(160.0, 120.0, 1.9, 0.0, 0.0, 1.9, 320, 240)
    p.view.clicked.emit(319.0, 120.0)          # 159 px x 1,9 = 302 mm
    p._move_to_target(False)
    assert m.jogs == []
    assert "refusé" in p.status.text() and "calibration" in p.status.text()
    p.shutdown()


def test_changer_de_taille_dimage_ne_decale_pas_la_visee(qapp):
    """Le piege silencieux : la calibration est en pixels, et passer en XGA
    les multiplie tous. Le meme point physique doit donner les memes mm."""
    p, _ = _panneau_aligne(qapp)
    p.cal = PixelMm(160.0, 120.0, 0.1, 0.0, 0.0, 0.1, 320, 240)
    p.view.clicked.emit(240.0, 120.0)          # 80 px -> 8 mm
    assert "+8.00" in p.lbl_target.text()
    # Meme point physique, image deux fois plus grande.
    p._on_frame(_vrai_jpeg(640, 480))
    p.view.clicked.emit(480.0, 240.0)
    assert "+8.00" in p.lbl_target.text(), p.lbl_target.text()
    p.shutdown()


def test_la_calibration_est_memorisee(qapp):
    """C'est une mesure du montage physique : elle ne se refait pas a chaque
    ouverture de la fenetre."""
    p, _ = _panneau_aligne(qapp)
    _calibre(p)
    resume = p.cal.summary()
    p.shutdown()

    fake = FakeCamera(jpeg=_vrai_jpeg())
    p2 = CameraPanel(client_factory=lambda host, **_: fake.client())
    assert p2.cal is not None, "calibration perdue a la reouverture"
    assert p2.cal.summary() == resume
    assert "mm/px" in p2.lbl_cal.text()
    p2.shutdown()


def test_un_reglage_reticule_a_la_main_deplace_le_point_de_tir(qapp):
    """Le reticule et le point de tir sont la MEME chose : les laisser
    diverger ferait croire la croix alors que la tete irait ailleurs."""
    p, _ = _panneau_aligne(qapp)
    _calibre(p)
    p.cross_x.setValue(20)
    assert p.cal.u0 == pytest.approx(20 + 320 / 2)
    assert p.cal.v0 == pytest.approx(0 + 240 / 2)
    p.shutdown()


def test_une_calibration_memorisee_illisible_est_ignoree(qapp):
    """Un reglage d'une version anterieure ne doit pas empecher la fenetre de
    s'ouvrir, ni piloter la machine avec des chiffres douteux."""
    from PySide6.QtCore import QSettings
    QSettings("laser_pcb", "laser_pcb").setValue("cam_cal", "{pas du json")
    fake = FakeCamera(jpeg=_vrai_jpeg())
    p = CameraPanel(client_factory=lambda host, **_: fake.client())
    assert p.cal is None
    assert "Non calibrée" in p.lbl_cal.text()
    p.shutdown()


def test_sans_machine_lalignement_sexplique(qapp):
    """Le panneau sert quand meme a voir et regler : il ne doit pas planter,
    juste dire ce qui manque."""
    p, _ = _panneau_aligne(qapp, brancher=False)
    p.view.clicked.emit(200.0, 140.0)
    p._move_to_target(False)
    assert "graveuse" in p.status.text()
    p._cal_toggle()
    assert p._cal_step == 0 and "graveuse" in p.status.text()
    p.shutdown()


def test_un_clic_hors_de_limage_ne_vise_rien(qapp):
    """L'image est centree dans le widget : les marges noires n'ont pas de
    coordonnees, et un clic dedans ne doit pas viser le bord de la piece."""
    v = CameraView()
    v.resize(400, 300)
    img = QImage(40, 20, QImage.Format_RGB32)
    img.fill(Qt.blue)
    v.set_image(img)
    # Image 400x200 centree : les 50 premieres lignes sont de la marge.
    assert v._to_image(200, 10) is None
    assert v._to_image(200, 290) is None
    dedans = v._to_image(200, 150)
    assert dedans == pytest.approx((20.0, 10.0)), dedans
