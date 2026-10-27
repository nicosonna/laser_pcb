"""Panneau camera : voir le plateau a travers la tete, reticule compris.

Le reticule est **logiciel**, et c'est volontaire : une croix dessinee a la
position exacte du tir vaut mieux qu'un faisceau physique, qui a une largeur, de
la parallaxe, et qu'il faut viser mecaniquement. La croix laser du montage reste
utile pour voir ou on en est **sans regarder l'ecran** : elle se pilote d'ici.

Le decalage du reticule (``Reticule X/Y``, en pixels) place la croix sur le
point que le laser brule reellement — c'est la forme en pixels de l'ecart
camera↔laser, et c'est une calibration du montage : elle est memorisee.

Deux regles de fond :

* le flux tourne dans un **thread** : lire le reseau sur le fil graphique
  figerait la fenetre entre deux images ;
* le flux est **coupe pendant une gravure** (``set_locked``). Le G-code part en
  telnet sur le meme WiFi, et la gigue d'un MJPEG peut affamer le tampon de la
  machine. La fumee masque la vue de toute facon.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QSettings, QThread, Signal
from PySide6.QtGui import QImage, QPainter, QPen, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                               QGroupBox, QLabel, QPushButton, QComboBox,
                               QLineEdit, QSlider, QSpinBox, QFileDialog,
                               QMessageBox, QSizePolicy)

from ..camera import AP_SSID, FRAMESIZES, CameraClient, discover
from .threads import retire


def _browse(url: str) -> None:
    """Ouvre une URL dans le navigateur du systeme."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl(url))


class _StreamWorker(QThread):
    """Tire le flux MJPEG hors du fil graphique."""
    frame = Signal(bytes)
    failed = Signal(str)

    def __init__(self, client: CameraClient):
        super().__init__()
        self.client = client
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        try:
            for img in self.client.stream(should_stop=lambda: self._stop):
                self.frame.emit(img)
            if not self._stop:
                self.failed.emit("Flux interrompu par la caméra.")
        except Exception as e:  # noqa: BLE001
            if not self._stop:
                self.failed.emit(str(e))


class _ScanWorker(QThread):
    """Detection mDNS (1 a 3 s de blocage : surtout pas sur le fil graphique)."""
    found = Signal(list)

    def __init__(self, extra: str = ""):
        super().__init__()
        self.extra = extra

    def run(self):
        try:
            self.found.emit(discover(extra=(self.extra,) if self.extra else ()))
        except Exception:  # noqa: BLE001
            self.found.emit([])


class CameraView(QWidget):
    """Derniere image recue, reticule par-dessus.

    Le reticule est ancre sur l'IMAGE, pas sur le widget : sinon il se deplace
    par rapport a la piece des qu'on redimensionne la fenetre, et la calibration
    ne veut plus rien dire.
    """

    def __init__(self):
        super().__init__()
        self.image: QImage | None = None
        self.cross = (0.0, 0.0)          # decalage en pixels d'image
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_image(self, img: QImage | None) -> None:
        self.image = img
        self.update()

    def set_cross(self, dx: float, dy: float) -> None:
        self.cross = (dx, dy)
        self.update()

    def _image_rect(self):
        """Rectangle d'affichage de l'image, ratio conserve et centre."""
        from PySide6.QtCore import QRect
        if self.image is None or self.image.isNull():
            return None
        iw, ih = self.image.width(), self.image.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = int(iw * scale), int(ih * scale)
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h), scale

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1b1b1f"))
        got = self._image_rect()
        if got is None:
            p.setPen(QPen(QColor("#888")))
            p.drawText(self.rect(), Qt.AlignCenter, "Pas d'image")
            return
        rect, scale = got
        p.drawImage(rect, self.image)

        cx = rect.center().x() + self.cross[0] * scale
        cy = rect.center().y() + self.cross[1] * scale
        # Double trait : un noir large sous un rouge fin, pour rester visible
        # autant sur le cuivre clair que sur le FR4 sombre.
        for pen in (QPen(QColor(0, 0, 0, 160), 3), QPen(QColor(255, 40, 40), 1)):
            p.setPen(pen)
            p.drawLine(int(cx), rect.top(), int(cx), rect.bottom())
            p.drawLine(rect.left(), int(cy), rect.right(), int(cy))
        p.setPen(QPen(QColor(255, 40, 40), 1))
        p.drawEllipse(int(cx) - 12, int(cy) - 12, 24, 24)


class CameraPanel(QWidget):
    """Connexion, flux, reticule, croix laser et eclairage."""

    def __init__(self, client_factory=CameraClient, url_opener=None):
        """``client_factory`` est injectable comme le ``Transport`` du sender :
        c'est ce qui rend le panneau testable sans carte sur le reseau.
        ``url_opener`` de meme, pour ne pas ouvrir un navigateur en test."""
        super().__init__()
        self._make_client = client_factory
        self._open_url = url_opener if url_opener is not None else _browse
        self.client: CameraClient | None = None
        self.worker: _StreamWorker | None = None
        self._scan: _ScanWorker | None = None
        self._locked = False
        self._last: bytes | None = None
        cfg = QSettings("laser_pcb", "laser_pcb")

        root = QVBoxLayout(self)

        # --- connexion ---
        conn = QGroupBox("Caméra de tête (ESP32-CAM)")
        cl = QHBoxLayout(conn)
        self.host = QLineEdit(cfg.value("cam_host", "lasercam.local", type=str))
        self.host.setPlaceholderText("lasercam.local ou 192.168.1.60")
        self.btn_scan = QPushButton("Détecter")
        self.btn_scan.setToolTip("Cherche la caméra sur le réseau (mDNS) et "
                                 "vérifie qu'elle répond son /status.")
        self.btn_scan.clicked.connect(self.scan)
        self.btn_connect = QPushButton("Connecter")
        self.btn_connect.clicked.connect(self.toggle_connect)
        self.btn_wifi = QPushButton("Réseau…")
        self.btn_wifi.setToolTip(
            "Ouvre la page de configuration WiFi de la caméra dans le "
            "navigateur. C'est la même page que son portail captif : utile "
            "pour la faire changer de réseau sans la reflasher.")
        self.btn_wifi.clicked.connect(self._open_config)
        cl.addWidget(self.host, 1)
        cl.addWidget(self.btn_scan)
        cl.addWidget(self.btn_wifi)
        cl.addWidget(self.btn_connect)
        root.addWidget(conn)

        self.status = QLabel("Déconnectée")
        root.addWidget(self.status)

        # --- image ---
        self.view = CameraView()
        root.addWidget(self.view, 1)

        # --- réglages ---
        box = QGroupBox("Réglages")
        g = QGridLayout(box)
        self.size = QComboBox()
        for label, val in FRAMESIZES:
            self.size.addItem(label, val)
        self.size.setCurrentIndex(2)                  # SVGA par defaut
        self.size.setToolTip("Plus grand = plus précis à viser, mais plus de "
                             "bande passante volée au G-code.")
        self.size.currentIndexChanged.connect(self._on_size)
        g.addWidget(QLabel("Taille d'image"), 0, 0)
        g.addWidget(self.size, 0, 1, 1, 2)

        self.led = QSlider(Qt.Horizontal)
        self.led.setRange(0, 255)
        self.led.setToolTip("LED blanche de la carte. Le cuivre et la résine "
                            "ont des reflets proches : un peu de lumière les "
                            "sépare. Elle chauffe — restez sous 80 en continu.")
        self.led.sliderReleased.connect(self._on_led)
        g.addWidget(QLabel("Éclairage"), 1, 0)
        g.addWidget(self.led, 1, 1, 1, 2)

        self.btn_laser = QPushButton("✛ Croix laser")
        self.btn_laser.setCheckable(True)
        self.btn_laser.setToolTip("Croix laser du montage (GPIO13). Elle "
                                  "s'éteint seule après 60 s : une liaison qui "
                                  "tombe ne doit pas laisser un faisceau allumé.")
        self.btn_laser.toggled.connect(self._on_laser)
        g.addWidget(self.btn_laser, 2, 0, 1, 2)
        self.btn_shot = QPushButton("Enregistrer l'image…")
        self.btn_shot.clicked.connect(self._save_shot)
        g.addWidget(self.btn_shot, 2, 2)

        # --- réticule : la calibration du montage ---
        self.cross_x = QSpinBox(); self.cross_x.setRange(-2000, 2000)
        self.cross_y = QSpinBox(); self.cross_y.setRange(-2000, 2000)
        tip = ("Décalage du réticule, en pixels d'image, pour le poser sur le "
               "point que le laser brûle vraiment. C'est l'écart caméra↔laser "
               "vu par l'image — une calibration du montage, donc mémorisée.")
        for w in (self.cross_x, self.cross_y):
            w.setSuffix(" px")
            w.setToolTip(tip)
            w.valueChanged.connect(self._on_cross)
        self.cross_x.setValue(cfg.value("cam_cross_x", 0, type=int))
        self.cross_y.setValue(cfg.value("cam_cross_y", 0, type=int))
        g.addWidget(QLabel("Réticule X"), 3, 0)
        g.addWidget(self.cross_x, 3, 1)
        g.addWidget(self.cross_y, 3, 2)
        root.addWidget(box)

        self.view.set_cross(self.cross_x.value(), self.cross_y.value())
        self._set_connected(False)

    # ---- état des widgets -------------------------------------------------- #
    def _set_connected(self, on: bool) -> None:
        self.btn_connect.setText("Déconnecter" if on else "Connecter")
        for w in (self.size, self.led, self.btn_laser, self.btn_shot):
            w.setEnabled(on and not self._locked)
        self.btn_connect.setEnabled(not self._locked)
        self.btn_scan.setEnabled(not on and not self._locked)
        # La page reseau reste joignable meme deconnecte : c'est souvent la
        # raison pour laquelle on n'arrive pas a se connecter.
        self.btn_wifi.setEnabled(not self._locked)
        self.host.setEnabled(not on)

    def set_locked(self, on: bool) -> None:
        """Verrouille le panneau pendant une gravure, et coupe le flux.

        Le G-code et la video se disputent le meme WiFi : un MJPEG en cours
        peut affamer le tampon de la machine en pleine passe.
        """
        self._locked = on
        if on:
            self.stop_stream()
            self.status.setText("Flux coupé pendant la gravure "
                                "(le G-code a besoin du réseau).")
        self._set_connected(self.client is not None)

    def shutdown(self) -> None:
        """Coupe le flux et eteint la croix AVANT la fermeture de la fenetre.

        Un fil encore en course quand l'interpreteur detruit les objets fait
        appeler ``qFatal()`` a Qt ; et une croix laser oubliee allumee reste
        allumee sur la machine, l'application fermee.
        """
        w, scan = self.worker, self._scan
        self.disconnect_camera()
        for th in (w, scan):
            if th is not None:
                th.wait(2000)

    # ---- détection --------------------------------------------------------- #
    def scan(self) -> None:
        self.status.setText("Recherche de la caméra…")
        self.btn_scan.setEnabled(False)
        retire(self._scan)
        self._scan = _ScanWorker(self.host.text().strip())
        self._scan.found.connect(self._scanned)
        self._scan.start()

    def _scanned(self, cams: list) -> None:
        retire(self._scan)
        self._scan = None
        self.btn_scan.setEnabled(not self._locked)
        if not cams:
            # Le cas le plus courant d'une camera introuvable : elle n'a pas
            # reussi a joindre le WiFi et attend sur son propre reseau.
            self.status.setText(
                f"Aucune caméra trouvée. Si elle n'a pas pu joindre le WiFi, "
                f"elle attend sur son réseau « {AP_SSID} » : rejoignez-le, "
                f"puis « Réseau… » pour la configurer.")
            return
        self.host.setText(cams[0].ip)
        self.status.setText(f"Trouvée : {cams[0].label()}")

    # ---- connexion --------------------------------------------------------- #
    def toggle_connect(self) -> None:
        if self.client:
            self.disconnect_camera()
            return
        host = self.host.text().strip()
        if not host:
            self.status.setText("Donnez le nom ou l'IP de la caméra.")
            return
        client = self._make_client(host)
        try:
            st = client.status()
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"Injoignable : {e}")
            return
        self.client = client
        QSettings("laser_pcb", "laser_pcb").setValue("cam_host", host)
        self._set_connected(True)
        self.status.setText(self._describe(st, host))
        self._on_size()
        self.start_stream()

    @staticmethod
    def _describe(st: dict, host: str) -> str:
        """Ligne d'etat. Le mode compte autant que le signal : en point d'acces,
        la camera marche mais la machine n'est pas sur le meme reseau."""
        who = st.get("host", host)
        if st.get("mode") == "ap":
            return (f"Connectée à {who} en POINT D'ACCÈS — la graveuse n'est "
                    f"pas sur ce réseau. « Réseau… » pour la rattacher au WiFi.")
        rssi = st.get("rssi")
        warn = "  ⚠ signal faible" if isinstance(rssi, int) and rssi < -75 else ""
        ssid = st.get("ssid")
        where = f" sur {ssid}" if ssid else ""
        return f"Connectée — {who}{where}, {rssi} dBm{warn}"

    def _open_config(self) -> None:
        """Ouvre la page reseau de la carte au navigateur.

        La configuration se fait LA, pas ici : c'est la meme page que le portail
        captif, donc un seul endroit a maintenir — et elle reste accessible
        depuis un telephone quand le PC n'est pas sur le bon reseau.
        """
        host = self.host.text().strip()
        if not host:
            self.status.setText("Donnez le nom ou l'IP de la caméra.")
            return
        url = (self.client.config_url() if self.client
               else self._make_client(host).config_url())
        self._open_url(url)
        self.status.setText(f"Configuration réseau ouverte : {url}")

    def disconnect_camera(self) -> None:
        self.stop_stream()
        if self.client and self.btn_laser.isChecked():
            # On ne laisse pas la croix allumee derriere soi.
            try:
                self.client.laser(False)
            except Exception:  # noqa: BLE001
                pass
        self.btn_laser.setChecked(False)
        self.client = None
        self.view.set_image(None)
        self._set_connected(False)
        self.status.setText("Déconnectée")

    # ---- flux -------------------------------------------------------------- #
    def start_stream(self) -> None:
        if self.client is None or self.worker is not None or self._locked:
            return
        self.worker = _StreamWorker(self.client)
        self.worker.frame.connect(self._on_frame)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def stop_stream(self) -> None:
        if self.worker is None:
            return
        self.worker.request_stop()
        retire(self.worker)
        self.worker = None

    def _on_frame(self, data: bytes) -> None:
        img = QImage()
        if not img.loadFromData(data, "JPG"):
            return                         # image tronquee : on garde l'ancienne
        self._last = data
        self.view.set_image(img)

    def _on_failed(self, msg: str) -> None:
        retire(self.worker)
        self.worker = None
        self.status.setText(f"Flux perdu : {msg}")

    # ---- commandes --------------------------------------------------------- #
    def _on_size(self, *_) -> None:
        if self.client is None:
            return
        try:
            self.client.control("framesize", self.size.currentData())
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"Réglage refusé : {e}")

    def _on_led(self) -> None:
        if self.client is None:
            return
        try:
            self.client.led(self.led.value())
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"Éclairage refusé : {e}")

    def _on_laser(self, on: bool) -> None:
        if self.client is None:
            return
        try:
            self.client.laser(on)
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"Croix laser refusée : {e}")

    def _on_cross(self, *_) -> None:
        cfg = QSettings("laser_pcb", "laser_pcb")
        cfg.setValue("cam_cross_x", self.cross_x.value())
        cfg.setValue("cam_cross_y", self.cross_y.value())
        self.view.set_cross(self.cross_x.value(), self.cross_y.value())

    def _save_shot(self) -> None:
        """Enregistre une image PRISE A L'INSTANT, pas une image du flux : le
        flux est plus comprime, et l'etalonnage se joue au pixel."""
        if self.client is None:
            return
        try:
            data = self.client.snapshot()
        except Exception as e:  # noqa: BLE001
            data = self._last
            if data is None:
                QMessageBox.warning(self, "Caméra", f"Capture impossible : {e}")
                return
        path, _ = QFileDialog.getSaveFileName(self, "Enregistrer l'image",
                                              "camera.jpg", "JPEG (*.jpg)")
        if not path:
            return
        with open(path, "wb") as f:
            f.write(data)
        self.status.setText(f"Image enregistrée : {path}")
