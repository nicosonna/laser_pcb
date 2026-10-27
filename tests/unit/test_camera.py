"""Unites : camera reseau de la tete (decoupage MJPEG, client HTTP, detection).

Tout passe par ``FakeCamera`` : aucun test ne demande de carte ni de serveur.
"""

from __future__ import annotations

import json

from laserpcb import camera as c

JPEG = c.SOI + b"image" + c.EOI


def _reader(chunks):
    """Lecteur a la ``read()`` : rend les morceaux puis b'' (fin de flux)."""
    it = iter(list(chunks) + [b""])
    return lambda: next(it, b"")


# --------------------------------------------------------------------------- #
# Decoupage du flux
# --------------------------------------------------------------------------- #
def test_iter_jpeg_une_image_seule():
    assert list(c.iter_jpeg(_reader([JPEG]))) == [JPEG]


def test_iter_jpeg_ignore_les_entetes_multipart():
    """Le decoupage se fait sur FFD8/FFD9, pas sur la frontiere multipart :
    c'est ce qui le rend compatible avec les firmwares qui l'ecrivent
    autrement ou omettent Content-Length."""
    flux = b"".join(b"\r\n--lasercamframe\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG
                    for _ in range(3))
    assert list(c.iter_jpeg(_reader([flux]))) == [JPEG] * 3


def test_iter_jpeg_image_coupee_entre_deux_morceaux():
    """Une image a cheval sur deux lectures doit etre recollee — y compris
    quand la coupure tombe au milieu d'un marqueur."""
    flux = b"junk" + JPEG + b"sep" + JPEG
    for taille in (1, 2, 3, 5, 7, 11):
        morceaux = [flux[i:i + taille] for i in range(0, len(flux), taille)]
        assert list(c.iter_jpeg(_reader(morceaux))) == [JPEG, JPEG], taille


def test_iter_jpeg_laisse_tomber_une_image_tronquee():
    """Mieux vaut une image en moins qu'une demi-image a l'ecran."""
    assert list(c.iter_jpeg(_reader([JPEG, c.SOI + b"moitie"]))) == [JPEG]


def test_iter_jpeg_ne_garde_pas_le_bruit_en_memoire():
    """Sans debut d'image, le tampon ne doit pas gonfler indefiniment."""
    bruit = [b"x" * 1000 for _ in range(50)]
    assert list(c.iter_jpeg(_reader(bruit))) == []


def test_iter_jpeg_repart_apres_un_flux_incoherent():
    """Au-dela de ``max_frame`` sans fin d'image, on vide et on repart : un
    flux casse ne doit pas manger la memoire ni bloquer les images suivantes."""
    enorme = c.SOI + b"y" * 400
    suite = list(c.iter_jpeg(_reader([enorme, JPEG]), max_frame=100))
    assert suite == [JPEG]


# --------------------------------------------------------------------------- #
# Construction des URL
# --------------------------------------------------------------------------- #
def test_url_le_flux_est_sur_un_autre_port():
    """Le firmware sert le flux sur un SECOND serveur : un seul httpd ESP-IDF
    traite une requete a la fois, et un MJPEG ne se termine jamais."""
    cam = c.CameraClient("lasercam.local")
    assert cam.url("/status") == "http://lasercam.local:80/status"
    assert cam.url("/stream") == "http://lasercam.local:81/stream"
    assert cam.url("/laser", on=1, ms=5000) == \
        "http://lasercam.local:80/laser?on=1&ms=5000"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def test_status_et_snapshot():
    fake = c.FakeCamera(jpeg=JPEG)
    cam = fake.client()
    assert cam.status()["fw"] == "lasercam"
    assert cam.snapshot() == JPEG
    assert any("/capture" in u for u in fake.requests)


def test_stream_rend_chaque_image():
    fake = c.FakeCamera(jpeg=JPEG, frames=4)
    assert list(fake.client().stream()) == [JPEG] * 4


def test_stream_sarrete_sur_demande():
    """``should_stop`` est ce qui coupe le flux au demarrage d'une gravure."""
    fake = c.FakeCamera(jpeg=JPEG, frames=10)
    vues = []
    for img in fake.client().stream(should_stop=lambda: len(vues) >= 2):
        vues.append(img)
    assert len(vues) == 2


def test_la_croix_laser_borne_toujours_sa_duree():
    """Si la liaison tombe, la carte doit eteindre toute seule : le client
    envoie donc toujours un ``ms``."""
    fake = c.FakeCamera()
    cam = fake.client()
    cam.laser(True)
    assert fake.laser_on and fake.laser_ms > 0
    assert cam.laser(False)["laser"] == 0
    assert not fake.laser_on


def test_eclairage_borne_entre_0_et_255():
    """La LED de la carte chauffe : une valeur folle ne doit pas partir."""
    fake = c.FakeCamera()
    cam = fake.client()
    cam.led(400)
    assert fake.led_level == 255
    cam.led(-10)
    assert fake.led_level == 0


def test_reglage_capteur():
    fake = c.FakeCamera()
    fake.client().control("framesize", 8)
    assert fake.settings["framesize"] == 8


def test_une_route_en_panne_remonte_lerreur():
    fake = c.FakeCamera(fail_on="/capture")
    cam = fake.client()
    assert cam.status()["fw"] == "lasercam"       # le reste marche encore
    try:
        cam.snapshot()
    except OSError:
        pass
    else:
        raise AssertionError("une capture impossible doit lever")


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def test_probe_ne_retient_que_le_bon_firmware():
    """Un ``.local`` resolu peut pointer sur une autre carte du reseau : on ne
    propose que ce qui annonce bien ``lasercam``."""
    fake = c.FakeCamera()
    assert c.probe("1.2.3.4", opener=fake.opener)["fw"] == "lasercam"

    def autre(url, timeout=None):
        return c._FakeResponse(json.dumps({"fw": "esp32-cam-webserver"}).encode())

    assert c.probe("1.2.3.4", opener=autre) is None

    def casse(url, timeout=None):
        return c._FakeResponse(b"<html>pas du json</html>")

    assert c.probe("1.2.3.4", opener=casse) is None

    def morte(url, timeout=None):
        raise OSError("injoignable")

    assert c.probe("1.2.3.4", opener=morte) is None


def test_discover_confirme_chaque_candidate(monkeypatch):
    monkeypatch.setattr(c, "mdns_resolve",
                        lambda n, t=1.2: ["10.0.0.5"] if n == "lasercam" else [])
    monkeypatch.setattr(c, "os_resolve", lambda n: ["10.0.0.9"])
    repond = {"10.0.0.5": {"fw": "lasercam", "host": "lasercam", "rssi": -58}}
    found = c.discover(probe_fn=repond.get)
    assert [f.ip for f in found] == ["10.0.0.5"], "10.0.0.9 n'a pas repondu"
    assert found[0].name == "lasercam" and found[0].rssi == -58


def test_discover_accepte_une_ip_ecrite_a_la_main(monkeypatch):
    """Un mDNS capricieux ne doit pas empecher de viser la carte directement."""
    monkeypatch.setattr(c, "mdns_resolve", lambda n, t=1.2: [])
    monkeypatch.setattr(c, "os_resolve", lambda n: [])
    found = c.discover(extra=("192.168.1.60",),
                       probe_fn={"192.168.1.60": {"fw": "lasercam",
                                                  "host": "cam"}}.get)
    assert [f.ip for f in found] == ["192.168.1.60"]


def test_discover_essaie_le_point_dacces_en_dernier(monkeypatch):
    """Mode « sans reseau » : sans box ni mDNS, la carte EST le routeur, et son
    adresse fixe est le seul endroit ou la trouver."""
    monkeypatch.setattr(c, "mdns_resolve", lambda n, t=1.2: [])
    monkeypatch.setattr(c, "os_resolve", lambda n: [])
    vus: list[str] = []

    def probe_fn(ip):
        vus.append(ip)
        return {"fw": "lasercam", "host": "lasercam", "mode": "ap"} \
            if ip == c.AP_IP else None

    found = c.discover(probe_fn=probe_fn)
    assert vus[-1] == c.AP_IP, "le point d'acces s'essaie en dernier"
    assert [f.ip for f in found] == [c.AP_IP]
    assert found[0].is_ap


def test_une_camera_en_point_dacces_sannonce_comme_telle():
    fake = c.FakeCamera(mode="ap")
    st = fake.client().status()
    assert st["mode"] == "ap" and st["ip"] == c.AP_IP
    assert st["ssid"] == c.AP_SSID


def test_label_lisible():
    assert c.Found("10.0.0.5", "lasercam", -58).label() == \
        "lasercam (10.0.0.5) — -58 dBm"
    assert c.Found("10.0.0.5").label() == "10.0.0.5"
    # En point d'acces le RSSI n'a pas de sens : c'est elle l'antenne.
    ap = c.Found(c.AP_IP, "lasercam", 0, "ap")
    assert ap.is_ap and ap.label() == "lasercam (192.168.4.1) — point d'accès"


def test_url_de_configuration():
    assert c.CameraClient("lasercam.local").config_url() == \
        "http://lasercam.local:80/wifi"
