"""Detection automatique de la camera au premier affichage du panneau.

Fichier separe de ``test_camera_panel.py`` : celui-la couvre la connexion, le
flux et l'alignement, celui-ci le seul fait d'arriver sur l'onglet. La
detection reseau est remplacee dans tous les tests — aucun ne cherche une
carte pour de vrai.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

pytest.importorskip("PySide6")

from PySide6.QtCore import QBuffer, QIODevice, Qt      # noqa: E402
from PySide6.QtGui import QImage                       # noqa: E402

from laserpcb.camera import FakeCamera, Found          # noqa: E402
from laserpcb.gui import camera_panel as cp            # noqa: E402
from laserpcb.gui.camera_panel import CameraPanel      # noqa: E402

UNE = Found("10.0.0.7", "lasercam", -58, "sta")
AUTRE = Found("10.0.0.8", "lasercam2", -71, "sta")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _reglages_propres():
    """Reglages vierges a chaque test (deja isoles du systeme par le conftest)."""
    from PySide6.QtCore import QSettings
    QSettings("laser_pcb", "laser_pcb").clear()
    yield


def _vrai_jpeg(w: int = 16, h: int = 12) -> bytes:
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(Qt.red)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    assert img.save(buf, "JPG")
    return bytes(buf.data())


def _detection(monkeypatch, *cams: Found) -> list[tuple]:
    """Remplace la detection reseau et note chaque appel."""
    appels: list[tuple] = []

    def faux(extra=(), **kw):
        appels.append(tuple(extra))
        return list(cams)

    monkeypatch.setattr(cp, "discover", faux)
    return appels


def _panneau(**kw):
    fake = FakeCamera(jpeg=_vrai_jpeg(), **kw)
    return CameraPanel(client_factory=lambda host, **_: fake.client())


def _attendre(qapp, cond, limite=5.0) -> bool:
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _finir(p) -> None:
    p.shutdown()
    p.close()


# --------------------------------------------------------------------------- #
def test_le_premier_affichage_detecte_et_connecte(qapp, monkeypatch):
    """Arriver sur l'onglet suffit : ni « Détecter », ni « Connecter ».

    C'est tout l'objet du changement — la carte repondait depuis le debut, mais
    il fallait deux clics pour s'en apercevoir.
    """
    appels = _detection(monkeypatch, UNE)
    p = _panneau(frames=2)
    p.show()
    assert _attendre(qapp, lambda: p.client is not None), "pas connectee"
    # Le nom du champ part avec la recherche : une carte renommee reste
    # trouvable, et l'utilisateur garde la main sur ce qu'on cherche.
    assert appels == [("lasercam.local",)], "une seule recherche, avec le champ"
    assert p.host.text() == UNE.ip, "l'adresse trouvee remplace la saisie"
    _finir(p)


def test_la_detection_automatique_ne_part_quune_fois(qapp, monkeypatch):
    """Un ``showEvent`` arrive a CHAQUE reaffichage de l'onglet.

    Sans drapeau, chaque va-et-vient entre onglets relancerait plusieurs
    secondes de resolution reseau — et rebrancherait une camera que l'on vient
    peut-etre justement de debrancher.
    """
    appels = _detection(monkeypatch, UNE)
    p = _panneau(frames=2)
    p.show()
    assert _attendre(qapp, lambda: p.client is not None)
    p.disconnect_camera()
    p.hide()
    p.show()
    qapp.processEvents()
    assert len(appels) == 1, "la detection est repartie au reaffichage"
    assert p.client is None, "elle a rebranche une camera debranchee expres"
    _finir(p)


def test_deux_cameras_ne_se_connectent_pas_toutes_seules(qapp, monkeypatch):
    """A deux cartes, c'est a l'utilisateur de dire laquelle voit sa piece."""
    _detection(monkeypatch, UNE, AUTRE)
    p = _panneau()
    p.show()
    assert _attendre(qapp, lambda: "Trouvée" in p.status.text())
    assert p.client is None, "connexion a l'aveugle entre deux cartes"
    assert p.host.text() == UNE.ip, "la mieux placee est quand meme proposee"
    _finir(p)


def test_un_scan_manuel_ne_connecte_pas_tout_seul(qapp, monkeypatch):
    """« Détecter » cherche, et s'arrete la.

    On clique ce bouton justement pour regarder ce qu'il y a sur le reseau ;
    enchainer sur la connexion serait agir dans le dos de l'utilisateur.
    """
    _detection(monkeypatch, UNE)
    p = _panneau()
    p.scan()
    assert _attendre(qapp, lambda: p._scan is None)
    assert p.client is None
    assert "Trouvée" in p.status.text()
    _finir(p)


def test_pendant_une_gravure_le_premier_affichage_ne_scanne_pas(qapp, monkeypatch):
    """Verrou de gravure : le flux est coupe expres, le G-code a besoin du WiFi.

    Un scan declencherait une connexion, donc un flux MJPEG, au pire moment.
    """
    appels = _detection(monkeypatch, UNE)
    p = _panneau()
    p.set_locked(True)
    p.show()
    qapp.processEvents()
    assert appels == [], "une recherche est partie en pleine gravure"
    assert p.client is None
    _finir(p)


def test_la_gravure_finie_le_prochain_affichage_detecte(qapp, monkeypatch):
    """Le verrou retarde la detection, il ne l'annule pas.

    Sinon la camera resterait muette pour le reste de la session a cause d'une
    gravure lancee avant d'avoir ouvert l'onglet.
    """
    _detection(monkeypatch, UNE)
    p = _panneau(frames=2)
    p.set_locked(True)
    p.show()
    qapp.processEvents()
    assert p.client is None
    p.hide()
    p.set_locked(False)
    p.show()
    assert _attendre(qapp, lambda: p.client is not None), "detection perdue"
    _finir(p)


def test_aucune_camera_explique_le_point_dacces(qapp, monkeypatch):
    """Rien trouve : le message doit dire ou chercher, pas seulement echouer.

    Le cas courant est une carte qui n'a pas joint le WiFi et attend sur son
    propre reseau.
    """
    _detection(monkeypatch)
    p = _panneau()
    p.show()
    assert _attendre(qapp, lambda: "Aucune caméra" in p.status.text())
    assert cp.AP_SSID in p.status.text()
    assert p.client is None
    assert p.btn_scan.isEnabled(), "le bouton doit redevenir cliquable"
    _finir(p)
