"""Camera reseau montee sur la tete du laser (ESP32-CAM, firmware ``lasercam``).

Elle sert a **positionner la piece sans gabarit** : on voit le plateau a travers
la tete, et un reticule logiciel marque le point vise. La carte expose du HTTP
(voir ``firmware/lasercam/``) :

* ``:80/capture``  une image JPEG ;
* ``:80/status``   JSON d'identification et d'etat ;
* ``:80/laser``    la croix laser du montage ;
* ``:81/stream``   le flux MJPEG.

Deux choix de conception ici :

**Le decoupage du flux se fait sur les marqueurs JPEG**, pas sur la frontiere
multipart ni sur ``Content-Length``. Les firmwares ESP32-CAM en circulation
annoncent des frontieres differentes et certains omettent la longueur ; un
decoupage sur ``FFD8``/``FFD9`` marche avec tous et se teste sans reseau.

**L'ouverture d'URL est injectable** (``opener``), sur le modele du
``Transport`` de ``sender.py``. ``FakeCamera`` suffit alors a couvrir le client
entier sans materiel ni serveur.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Iterator

from .discover import mdns_resolve, os_resolve

CONTROL_PORT = 80
STREAM_PORT = 81
DEFAULT_NAMES = ("lasercam", "esp32cam", "esp32-cam")
FIRMWARE_TAG = "lasercam"            # marqueur attendu dans /status
# Adresse de la carte quand elle est son PROPRE point d'acces (mode « sans
# reseau » : pas de box, pas de mDNS, la camera fait routeur). On l'essaie en
# dernier, car elle ne repond que si le PC est joint a `lasercam-setup`.
AP_IP = "192.168.4.1"
AP_SSID = "lasercam-setup"

SOI = b"\xff\xd8\xff"                # debut d'image JPEG
EOI = b"\xff\xd9"                    # fin d'image JPEG
MAX_FRAME = 2_000_000                # garde-fou : au-dela, le flux est casse


# --------------------------------------------------------------------------- #
# Decoupage du flux MJPEG
# --------------------------------------------------------------------------- #
def iter_jpeg(read: Callable[[], bytes],
              max_frame: int = MAX_FRAME) -> Iterator[bytes]:
    """Decoupe un flux continu en images JPEG completes.

    ``read`` rend le morceau suivant, et ``b""`` quand le flux est fini. Les
    en-tetes multipart qui separent les images sont simplement ignores : seules
    les bornes ``FFD8``/``FFD9`` comptent. Une image tronquee en fin de flux
    n'est pas rendue — mieux vaut une image en moins qu'une image a moitie
    decodee a l'ecran.
    """
    buf = bytearray()
    while True:
        chunk = read()
        if not chunk:
            return
        buf += chunk
        while True:
            start = buf.find(SOI)
            if start < 0:
                # Aucun debut d'image : on ne garde que la queue, car le
                # marqueur peut etre coupe entre deux morceaux.
                del buf[:-2]
                break
            end = buf.find(EOI, start + len(SOI))
            if end < 0:
                del buf[:start]      # jette l'en-tete multipart qui precede
                break
            yield bytes(buf[start:end + len(EOI)])
            del buf[:end + len(EOI)]
        if len(buf) > max_frame:
            buf.clear()              # flux incoherent : on repart proprement


# --------------------------------------------------------------------------- #
# Detection sur le reseau
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Found:
    """Une camera detectee et **confirmee** (elle a repondu son /status)."""
    ip: str
    name: str = ""
    rssi: int | None = None
    mode: str = ""                   # "sta" (sur le reseau) ou "ap" (la carte
                                     # est son propre point d'acces)

    @property
    def is_ap(self) -> bool:
        return self.mode == "ap"

    def label(self) -> str:
        who = self.name or self.ip
        txt = f"{who} ({self.ip})" if self.name else self.ip
        if self.is_ap:
            # Le signal n'a pas de sens en point d'acces : c'est elle l'antenne.
            return f"{txt} — point d'accès"
        return f"{txt} — {self.rssi} dBm" if self.rssi is not None else txt


def probe(ip: str, port: int = CONTROL_PORT, timeout: float = 1.0,
          opener: Callable | None = None) -> dict | None:
    """Lit ``/status`` et renvoie le JSON si c'est bien une ``lasercam``.

    Comme pour la graveuse, on ne propose que ce qui a VRAIMENT repondu : un
    ``.local`` resolu peut pointer sur une autre carte du reseau.
    """
    try:
        raw = _get(f"http://{ip}:{port}/status", timeout, opener)
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    return data if FIRMWARE_TAG in str(data.get("fw", "")) else None


def discover(extra: tuple[str, ...] = (), timeout: float = 1.2,
             probe_fn: Callable[[str], dict | None] | None = None) -> list[Found]:
    """Cherche les cameras par mDNS, puis confirme chaque candidate.

    Memes trois sources que la detection de la graveuse : requete mDNS brute,
    resolution systeme du ``.local``, et noms supplementaires de l'utilisateur.
    """
    check = probe_fn if probe_fn is not None else (lambda ip: probe(ip))
    names = list(DEFAULT_NAMES) + [n for n in extra if n]
    ips: list[str] = []
    for name in names:
        for ip in list(mdns_resolve(name, timeout)) + list(os_resolve(name)):
            if ip not in ips:
                ips.append(ip)
    # Une IP ecrite a la main passe aussi : l'utilisateur peut court-circuiter
    # un mDNS capricieux.
    for name in extra:
        if name and name[0].isdigit() and name not in ips:
            ips.append(name)
    # En dernier, le point d'acces de la carte : sans box ni mDNS, c'est la
    # seule adresse ou la trouver. Elle ne repond que si le PC est joint a son
    # reseau, donc l'essayer ne coute qu'un refus immediat.
    if AP_IP not in ips:
        ips.append(AP_IP)

    out: list[Found] = []
    for ip in ips:
        data = check(ip)
        if data:
            out.append(Found(ip, str(data.get("host", "")),
                             _as_int(data.get("rssi")),
                             str(data.get("mode", ""))))
    return out


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: float, opener: Callable | None = None) -> bytes:
    resp = _open(url, timeout, opener)
    try:
        return resp.read()
    finally:
        resp.close()


def _open(url: str, timeout: float, opener: Callable | None = None):
    if opener is not None:
        return opener(url, timeout)
    from urllib.request import urlopen
    return urlopen(url, timeout=timeout)       # noqa: S310 — http local assume


class CameraClient:
    """Acces a une ``lasercam`` : image, flux, croix laser, eclairage."""

    def __init__(self, host: str, control_port: int = CONTROL_PORT,
                 stream_port: int = STREAM_PORT, timeout: float = 3.0,
                 opener: Callable | None = None):
        self.host = host
        self.control_port = control_port
        self.stream_port = stream_port
        self.timeout = timeout
        self._opener = opener

    # ---- URL ------------------------------------------------------------- #
    def url(self, path: str, **params) -> str:
        from urllib.parse import urlencode
        port = self.stream_port if path == "/stream" else self.control_port
        base = f"http://{self.host}:{port}{path}"
        return f"{base}?{urlencode(params)}" if params else base

    # ---- lectures -------------------------------------------------------- #
    def status(self) -> dict:
        raw = _get(self.url("/status"), self.timeout, self._opener)
        return json.loads(raw.decode("utf-8", "replace"))

    def snapshot(self) -> bytes:
        """Une image JPEG. C'est ce qu'on utilise pour l'etalonnage : une image
        nette a l'arret vaut mieux qu'une image tiree d'un flux comprime."""
        return _get(self.url("/capture"), self.timeout, self._opener)

    def stream(self, chunk: int = 8192,
               should_stop: Callable[[], bool] | None = None) -> Iterator[bytes]:
        """Rend les images du flux MJPEG, une par une, jusqu'a l'arret.

        ``should_stop`` est teste entre deux images : c'est ce qui permet de
        couper le flux au demarrage d'une gravure, pour ne pas disputer la bande
        passante au G-code qui part vers la machine.
        """
        resp = _open(self.url("/stream"), max(self.timeout, 5.0), self._opener)
        try:
            for frame in iter_jpeg(lambda: resp.read(chunk)):
                if should_stop is not None and should_stop():
                    return
                yield frame
        finally:
            resp.close()

    # ---- commandes ------------------------------------------------------- #
    def laser(self, on: bool, hold_ms: int = 60000) -> dict:
        """Allume ou eteint la croix laser du montage.

        ``hold_ms`` borne la duree cote carte : si la liaison tombe, la croix
        s'eteint toute seule au lieu de rester allumee sur la machine.
        """
        raw = _get(self.url("/laser", on=1 if on else 0, ms=int(hold_ms)),
                   self.timeout, self._opener)
        return json.loads(raw.decode("utf-8", "replace"))

    def led(self, level: int) -> dict:
        """LED blanche de la carte (0-255) : le FR4 et le cuivre ont des
        reflets proches, un peu de lumiere rasante les separe."""
        level = max(0, min(255, int(level)))
        raw = _get(self.url("/led", v=level), self.timeout, self._opener)
        return json.loads(raw.decode("utf-8", "replace"))

    def config_url(self) -> str:
        """Page de configuration reseau de la carte (a ouvrir au navigateur).

        C'est la meme page que celle du portail captif : on la sert aussi sur le
        reseau, pour changer de WiFi sans avoir a oublier le reseau d'abord.
        """
        return self.url("/wifi")

    def control(self, var: str, val: int) -> dict:
        """Reglage capteur : ``framesize``, ``quality``, ``hmirror``, ``vflip``…"""
        raw = _get(self.url("/control", var=var, val=int(val)),
                   self.timeout, self._opener)
        return json.loads(raw.decode("utf-8", "replace"))


# Tailles d'image du firmware (valeurs de ``framesize``), de la plus utile a la
# plus lourde. Au-dela du XGA le flux hache sur un WiFi charge.
FRAMESIZES = (("QVGA 320×240", 5), ("VGA 640×480", 8),
              ("SVGA 800×600", 9), ("XGA 1024×768", 10))


# --------------------------------------------------------------------------- #
# Camera factice (tests, et GUI sans materiel)
# --------------------------------------------------------------------------- #
class FakeCamera:
    """Repond comme le firmware, en memoire.

    Pendant au ``FakeGrbl`` de ``sender.py`` : elle rend tout le client testable
    sans carte ni serveur, et garde une trace des requetes pour qu'un test
    puisse verifier qu'on a bien allume la croix.
    """

    def __init__(self, jpeg: bytes | None = None, frames: int = 3,
                 fail_on: str | None = None, mode: str = "sta"):
        self.jpeg = jpeg if jpeg is not None else SOI + b"fake-image" + EOI
        self.frames = frames
        self.fail_on = fail_on
        self.mode = mode
        self.requests: list[str] = []
        self.laser_on = False
        self.laser_ms = 0
        self.led_level = 0
        self.settings: dict[str, int] = {}

    # ---- etat renvoye par /status ---------------------------------------- #
    def status(self) -> dict:
        ap = self.mode == "ap"
        return {"fw": "lasercam", "version": 2, "mode": self.mode,
                "host": "lasercam",
                "ip": AP_IP if ap else "192.168.1.60",
                "ssid": AP_SSID if ap else "maison",
                "rssi": 0 if ap else -58, "psram": 1,
                "framesize": self.settings.get("framesize", 9),
                "quality": self.settings.get("quality", 12),
                "laser": 1 if self.laser_on else 0, "led": self.led_level,
                "uptime_s": 42}

    # ---- opener a passer a CameraClient ---------------------------------- #
    def opener(self, url: str, timeout: float | None = None):
        self.requests.append(url)
        if self.fail_on and self.fail_on in url:
            raise OSError(f"fake: {self.fail_on} indisponible")
        path, _, query = url.partition("://")[2].partition("/")[2].partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        if path == "status":
            return _FakeResponse(json.dumps(self.status()).encode())
        if path == "capture":
            return _FakeResponse(self.jpeg)
        if path == "stream":
            # Des en-tetes multipart entre les images, comme le vrai firmware :
            # le decoupage doit savoir les ignorer.
            body = b"".join(b"\r\n--lasercamframe\r\nContent-Type: image/jpeg\r\n"
                            b"Content-Length: %d\r\n\r\n" % len(self.jpeg) + self.jpeg
                            for _ in range(self.frames))
            return _FakeResponse(body)
        if path == "laser":
            self.laser_on = params.get("on") == "1"
            self.laser_ms = int(params.get("ms", 0) or 0)
            return _FakeResponse(json.dumps(self.status()).encode())
        if path == "led":
            self.led_level = int(params.get("v", 0) or 0)
            return _FakeResponse(json.dumps(self.status()).encode())
        if path == "control":
            self.settings[params.get("var", "")] = int(params.get("val", 0) or 0)
            return _FakeResponse(json.dumps(self.status()).encode())
        raise OSError(f"fake: route inconnue {path!r}")

    def client(self, **kw) -> CameraClient:
        return CameraClient("fake.local", opener=self.opener, **kw)


class _FakeResponse:
    """Reponse en memoire, avec l'interface minimale d'``urlopen``."""

    def __init__(self, body: bytes):
        self._body = body
        self._pos = 0
        self.closed = False

    def read(self, n: int | None = None) -> bytes:
        if n is None:
            n = len(self._body) - self._pos
        out = self._body[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def close(self) -> None:
        self.closed = True
