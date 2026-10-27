"""Panneau camera : voir le plateau a travers la tete, reticule compris.

Le reticule est **logiciel**, et c'est volontaire : une croix dessinee a la
position exacte du tir vaut mieux qu'un faisceau physique, qui a une largeur, de
la parallaxe, et qu'il faut viser mecaniquement. La croix laser du montage reste
utile pour voir ou on en est **sans regarder l'ecran** : elle se pilote d'ici.

Le decalage du reticule (``Reticule X/Y``, en pixels) place la croix sur le
point que le laser brule reellement — c'est la forme en pixels de l'ecart
camera↔laser, et c'est une calibration du montage : elle est memorisee. Une
fois l'assistant passe, elle n'est plus un reglage a tatons : elle DECOULE de
la calibration (``camera_cal``), qui mesure au passage combien de millimetres
vaut un pixel. C'est ce qui permet le clic-pour-aller, et donc de se passer du
gabarit.

Trois regles de fond :

* le flux tourne dans un **thread** : lire le reseau sur le fil graphique
  figerait la fenetre entre deux images ;
* le flux est **coupe pendant une gravure** (``set_locked``). Le G-code part en
  telnet sur le meme WiFi, et la gigue d'un MJPEG peut affamer le tampon de la
  machine. La fumee masque la vue de toute facon ;
* un clic ne deplace **jamais** la machine tout seul : il pose une cible, et
  c'est un bouton qui declenche le mouvement. Une image cliquable qui lance la
  tete au premier clic distrait serait dangereuse.
"""

from __future__ import annotations

import json
from dataclasses import replace

from PySide6.QtCore import Qt, QSettings, QThread, Signal
from PySide6.QtGui import QImage, QPainter, QPen, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                               QGroupBox, QLabel, QPushButton, QComboBox,
                               QLineEdit, QSlider, QSpinBox, QDoubleSpinBox,
                               QFileDialog, QMessageBox, QSizePolicy)

from ..camera import AP_SSID, FRAMESIZES, CameraClient, discover
from ..camera_cal import CalibrationError, PixelMm, solve, warnings_for
from .threads import retire

# Un clic dans l'image ne peut pas legitimement demander un deplacement de
# cette ampleur : au-dela, c'est la calibration qui est fausse, et mieux vaut
# refuser que lancer la tete a l'autre bout du plateau.
JOG_MAX_MM = 150.0


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

    clicked = Signal(float, float)       # en pixels d'IMAGE, pas de widget

    def __init__(self):
        super().__init__()
        self.image: QImage | None = None
        self.cross = (0.0, 0.0)          # decalage en pixels d'image
        self.marker: tuple | None = None  # dernier point pointe, en pixels d'image
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.CrossCursor)

    def set_image(self, img: QImage | None) -> None:
        self.image = img
        self.update()

    def set_cross(self, dx: float, dy: float) -> None:
        self.cross = (dx, dy)
        self.update()

    def set_marker(self, pt: tuple | None) -> None:
        self.marker = pt
        self.update()

    def _to_image(self, x: float, y: float):
        """Coordonnees widget -> pixels d'image, ou ``None`` hors de l'image.

        Sans cette conversion, un clic serait exprime dans la taille du
        widget : la calibration ne tiendrait pas un redimensionnement de la
        fenetre.
        """
        got = self._image_rect()
        if got is None:
            return None
        rect, scale = got
        if not rect.contains(int(x), int(y)):
            return None                  # clic dans la marge noire
        return ((x - rect.left()) / scale, (y - rect.top()) / scale)

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        pos = ev.position()
        pt = self._to_image(pos.x(), pos.y())
        if pt is not None:
            self.clicked.emit(pt[0], pt[1])

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

        # Point pointe : cyan, pour ne pas le confondre avec le reticule rouge
        # — l'un est la ou le laser tire, l'autre la ou on veut qu'il aille.
        if self.marker is not None:
            mx = int(rect.left() + self.marker[0] * scale)
            my = int(rect.top() + self.marker[1] * scale)
            for pen in (QPen(QColor(0, 0, 0, 160), 3), QPen(QColor(60, 220, 255), 1)):
                p.setPen(pen)
                p.drawLine(mx - 9, my, mx + 9, my)
                p.drawLine(mx, my - 9, mx, my + 9)


class SendPanelMachine:
    """Pont vers la graveuse, vu depuis le panneau camera.

    Le panneau camera ne connait de la machine que ces quatre gestes. C'est
    volontaire : une interface aussi etroite se remplace par un faux en test,
    et elle evite au panneau de dependre du sender et de ses etats.
    """

    def __init__(self, panel):
        self.panel = panel

    def ready(self) -> bool:
        return getattr(self.panel, "sender", None) is not None

    def jog(self, dx: float, dy: float) -> None:
        self.panel.sender.jog(dx=dx, dy=dy, feed=self.panel.feed.value())

    def set_zero(self) -> None:
        self.panel.sender.set_work_zero()

    def pointer(self, on: bool) -> None:
        """Pointeur basse puissance, par le BOUTON et non par le sender.

        Ainsi son etat reste visible dans l'onglet Envoi : si l'assistant est
        quitte autrement que prevu, l'utilisateur voit que le pointeur est
        allume et peut l'eteindre.
        """
        self.panel.btn_pointer.setChecked(on)


class CameraPanel(QWidget):
    """Connexion, flux, reticule, croix laser, eclairage et clic-pour-aller."""

    # L'assistant en quatre pointes. Le point de tir se releve au pointeur ;
    # la matrice se mesure sur un detail fixe qu'on voit reculer pendant que la
    # tete avance — on n'a jamais besoin de savoir OU est ce detail.
    _CAL_STEPS = (
        "1/4 — Pointeur allumé : cliquez le point lumineux sur la pièce. "
        "Coupez l'éclairage s'il le noie.",
        "2/4 — Cliquez un détail fixe et net de la pièce : un coin, une "
        "pastille, un trait de crayon.",
        "3/4 — La tête a bougé en X. Cliquez LE MÊME détail.",
        "4/4 — La tête a bougé en Y. Cliquez LE MÊME détail.",
    )

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
        # Pont vers la graveuse, pose apres coup par la fenetre : le panneau
        # camera marche tout seul, sans machine (voir, viser, regler).
        self.machine = None
        self.cal: PixelMm | None = None
        self._target: tuple | None = None
        self._cal_step = 0
        self._cal_obs: list = []
        self._cal_spot: tuple | None = None
        self._cal_moved = (0.0, 0.0)
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

        # --- alignement : ce qui remplace vraiment le gabarit ---
        align = QGroupBox("Alignement")
        al = QGridLayout(align)
        self.btn_cal = QPushButton("Calibrer…")
        self.btn_cal.setToolTip(
            "Mesure combien de millimètres vaut un pixel, et où le laser tire "
            "dans l'image. Quatre clics, pièce posée à la hauteur de gravure. "
            "À refaire si la lentille tourne ou si la caméra bouge.")
        self.btn_cal.clicked.connect(self._cal_toggle)
        self.cal_step = QDoubleSpinBox()
        self.cal_step.setRange(1.0, 50.0)
        self.cal_step.setValue(10.0)
        self.cal_step.setSuffix(" mm")
        self.cal_step.setToolTip(
            "Distance dont la tête bouge entre deux pointés. Plus c'est grand, "
            "plus la mesure est précise — mais le détail visé doit rester "
            "dans l'image.")
        al.addWidget(self.btn_cal, 0, 0)
        al.addWidget(QLabel("Pas"), 0, 1)
        al.addWidget(self.cal_step, 0, 2)

        self.lbl_cal = QLabel()
        self.lbl_cal.setWordWrap(True)
        al.addWidget(self.lbl_cal, 1, 0, 1, 3)

        self.btn_goto = QPushButton("Aller au point")
        self.btn_goto.setToolTip("Amène la tête sur le point cliqué.")
        self.btn_goto.clicked.connect(lambda: self._move_to_target(False))
        self.btn_zero = QPushButton("Définir 0,0 ici")
        self.btn_zero.setToolTip("Amène la tête sur le point cliqué, puis y "
                                 "place l'origine de travail 0,0.")
        self.btn_zero.clicked.connect(lambda: self._move_to_target(True))
        al.addWidget(self.btn_goto, 2, 0)
        al.addWidget(self.btn_zero, 2, 1, 1, 2)

        self.lbl_target = QLabel()
        self.lbl_target.setWordWrap(True)
        al.addWidget(self.lbl_target, 3, 0, 1, 3)
        root.addWidget(align)

        # Un clic ne bouge JAMAIS la machine tout seul : il pose une cible, et
        # c'est un bouton qui declenche le deplacement. Un clic distrait dans
        # une image ne doit pas lancer la tete.
        self.view.clicked.connect(self._on_click)

        raw = cfg.value("cam_cal", "", type=str)
        if raw:
            try:
                self.cal = PixelMm.from_dict(json.loads(raw))
            except (CalibrationError, ValueError, TypeError):
                self.cal = None    # reglage perime ou abime : on repart sans

        self.view.set_cross(self.cross_x.value(), self.cross_y.value())
        self._refresh_cal_label()
        self._refresh_target()
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
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        """Etat des commandes d'alignement.

        On ne grise que sur ce que le panneau SAIT : camera connectee, cible
        posee, gravure en cours. L'etat de la liaison machine, lui, change
        ailleurs et sans nous avertir — un bouton grise pour cette raison
        resterait grise apres la connexion, sans rien expliquer. Il reste donc
        cliquable, et c'est le message qui dit ce qui manque.
        """
        aimed = self._target is not None and self._cal_step == 0
        self.btn_goto.setEnabled(aimed and not self._locked)
        self.btn_zero.setEnabled(aimed and not self._locked)
        # Pendant l'assistant, le bouton sert a annuler : il reste cliquable.
        self.btn_cal.setEnabled(bool(self._cal_step) or
                                (self.client is not None and not self._locked))
        self.cal_step.setEnabled(self._cal_step == 0 and not self._locked)

    def set_machine(self, machine) -> None:
        """Branche la graveuse. Sans elle, le panneau sert quand meme a voir,
        viser et regler — il ne sait juste pas deplacer la tete."""
        self.machine = machine
        self._refresh_buttons()

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
            if self._cal_step:
                # L'assistant fait bouger la tete d'un pas connu : impossible
                # pendant une gravure. On le dit APRES, pour que le message de
                # gravure n'efface pas l'annulation.
                self._cal_cancel("Gravure en cours : flux coupé et calibration "
                                 "interrompue (le G-code a besoin du réseau).")
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
        if self._cal_step:
            # Sans image, plus rien a pointer : et l'assistant laisserait le
            # pointeur allume sur la machine.
            self._cal_cancel("Calibration abandonnée (caméra déconnectée).")
        self._target = None
        self.view.set_marker(None)
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
        if self.cal is not None:
            # Le reticule et le point de tir de la calibration sont la MEME
            # chose. Un reglage a la main doit donc deplacer les deux, sinon la
            # croix et le point d'arrivee divergeraient en silence — et c'est
            # la croix qu'on croit.
            self.cal = replace(self.cal,
                               u0=self.cross_x.value() + self.cal.ref_w / 2.0,
                               v0=self.cross_y.value() + self.cal.ref_h / 2.0)
            self._store_cal()
            self._refresh_target()

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

    # ---- viser un point ---------------------------------------------------- #
    def _on_click(self, u: float, v: float) -> None:
        """Clic dans l'image : un pointe de l'assistant, ou une cible."""
        if self._cal_step:
            self._cal_click(u, v)
            return
        self._target = (u, v)
        self.view.set_marker(self._target)
        self._refresh_target()

    def _image_size(self) -> tuple | None:
        img = self.view.image
        if img is None or img.isNull():
            return None
        return (img.width(), img.height())

    def _target_mm(self) -> tuple | None:
        """Ecart en mm entre le point de tir et la cible, ou ``None``.

        La taille d'image courante est passee a la calibration : un changement
        de ``framesize`` depuis la mesure ne doit pas fausser le resultat.
        """
        size = self._image_size()
        if self.cal is None or self._target is None or size is None:
            return None
        return self.cal.offset_mm(self._target[0], self._target[1], *size)

    def _refresh_target(self) -> None:
        off = self._target_mm()
        if off is not None:
            self.lbl_target.setText(f"Point visé : X {off[0]:+.2f} / "
                                    f"Y {off[1]:+.2f} mm depuis le tir.")
        elif self._target is not None:
            self.lbl_target.setText("Point visé — calibrez pour savoir combien "
                                    "de millimètres cela représente.")
        else:
            self.lbl_target.setText("Cliquez dans l'image pour viser un point.")
        self._refresh_buttons()

    def _move_to_target(self, then_zero: bool) -> None:
        """Amene la tete sur la cible, et y pose 0,0 si demande."""
        if self.machine is None or not self.machine.ready():
            self.status.setText("Connectez la graveuse pour déplacer la tête.")
            return
        off = self._target_mm()
        if off is None:
            self.status.setText("Calibrez d'abord : sans échelle, un clic ne "
                                "dit pas de combien bouger.")
            return
        dx, dy = off
        if max(abs(dx), abs(dy)) > JOG_MAX_MM:
            self.status.setText(
                f"Déplacement refusé (X {dx:+.0f} / Y {dy:+.0f} mm) : au-delà "
                f"de {JOG_MAX_MM:.0f} mm, c'est la calibration qui est fausse, "
                f"pas la pièce qui est loin.")
            return
        try:
            self.machine.jog(dx, dy)
            if then_zero:
                self.machine.set_zero()
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"Déplacement refusé : {e}")
            return
        bouge = f"X {dx:+.2f} / Y {dy:+.2f} mm"
        self.status.setText(f"0,0 défini au point visé (tête déplacée de "
                            f"{bouge})." if then_zero else
                            f"Tête déplacée de {bouge}.")
        # La cible est maintenant sous le tir : garder le marqueur ferait
        # croire qu'il reste quelque chose a faire.
        self._target = None
        self.view.set_marker(None)
        self._refresh_target()

    # ---- assistant de calibration ------------------------------------------ #
    def _cal_toggle(self) -> None:
        if self._cal_step:
            self._cal_cancel("Calibration annulée.")
            return
        self._cal_start()

    def _cal_start(self) -> None:
        if self.machine is None or not self.machine.ready():
            self.status.setText("Connectez la graveuse : la calibration la "
                                "fait bouger d'un pas connu.")
            return
        if self._image_size() is None:
            self.status.setText("Aucune image : connectez la caméra d'abord.")
            return
        pas = self.cal_step.value()
        rep = QMessageBox.question(
            self, "Calibrer la caméra",
            "Quatre pointés, sur une pièce posée à la hauteur de gravure :\n\n"
            "1. le pointeur laser s'allume à faible puissance — cliquez le "
            "point lumineux dans l'image ;\n"
            "2. cliquez un détail fixe de la pièce (coin, pastille, trait) ;\n"
            f"3. la tête avance de {pas:g} mm en X — recliquez ce détail ;\n"
            f"4. elle avance de {pas:g} mm en Y — recliquez-le.\n\n"
            "La tête revient ensuite d'où elle vient. Continuer ?")
        if rep != QMessageBox.Yes:
            return
        self._cal_obs = []
        self._cal_spot = None
        self._cal_moved = (0.0, 0.0)
        self._cal_step = 1
        try:
            self.machine.pointer(True)
        except Exception as e:  # noqa: BLE001
            self._cal_cancel(f"Pointeur refusé : {e}")
            return
        self._cal_show()

    def _cal_show(self) -> None:
        self.btn_cal.setText("Annuler")
        self.status.setText(self._CAL_STEPS[self._cal_step - 1])
        self._refresh_buttons()

    def _cal_click(self, u: float, v: float) -> None:
        self.view.set_marker((u, v))
        if self._cal_step == 1:
            self._cal_spot = (u, v)
            try:
                self.machine.pointer(False)
            except Exception:  # noqa: BLE001
                pass           # le point est releve : ne pas tout perdre ici
            self._cal_step = 2
            self._cal_show()
            return
        self._cal_obs.append((self._cal_moved, (u, v)))
        if self._cal_step == 2:
            if self._cal_jog(self.cal_step.value(), 0.0):
                self._cal_step = 3
                self._cal_show()
        elif self._cal_step == 3:
            if self._cal_jog(0.0, self.cal_step.value()):
                self._cal_step = 4
                self._cal_show()
        else:
            self._cal_finish()

    def _cal_jog(self, dx: float, dy: float) -> bool:
        try:
            self.machine.jog(dx, dy)
        except Exception as e:  # noqa: BLE001
            self._cal_cancel(f"Déplacement refusé : {e}")
            return False
        self._cal_moved = (self._cal_moved[0] + dx, self._cal_moved[1] + dy)
        return True

    def _cal_back(self) -> None:
        """Ramene la tete ou elle etait avant l'assistant.

        Sans ca, l'utilisateur recupere une tete decalee d'un pas sans savoir
        lequel — et son 0,0 avec.
        """
        mx, my = self._cal_moved
        self._cal_moved = (0.0, 0.0)
        if (mx or my) and self.machine is not None:
            try:
                self.machine.jog(-mx, -my)
            except Exception:  # noqa: BLE001
                pass

    def _cal_cancel(self, msg: str) -> None:
        if self.machine is not None:
            try:
                self.machine.pointer(False)
            except Exception:  # noqa: BLE001
                pass
            self._cal_back()
        self._cal_step = 0
        self._cal_obs = []
        self._cal_spot = None
        self.btn_cal.setText("Calibrer…")
        self.view.set_marker(None)
        self.status.setText(msg)
        self._refresh_buttons()

    def _cal_finish(self) -> None:
        size = self._image_size()
        if size is None:
            self._cal_cancel("Image perdue en cours de calibration : à refaire.")
            return
        try:
            cal = solve(self._cal_spot, self._cal_obs, size)
        except CalibrationError as e:
            self._cal_cancel(f"Calibration refusée — {e}")
            return
        self._cal_back()
        self._cal_step = 0
        self._cal_obs = []
        self.btn_cal.setText("Calibrer…")
        self.view.set_marker(None)
        self._target = None
        self.cal = cal
        self._store_cal()
        # Le reticule DECOULE de la calibration : la croix se pose d'elle-meme
        # sur le point de tir, et les deux champs « Réticule » cessent d'etre
        # un reglage a tatons.
        for w, val in ((self.cross_x, cal.cross_offset()[0]),
                       (self.cross_y, cal.cross_offset()[1])):
            w.blockSignals(True)
            w.setValue(int(round(val)))
            w.blockSignals(False)
        self._on_cross()
        doutes = warnings_for(cal)
        self.status.setText("Calibrée — " + cal.summary() +
                            ("    ⚠ " + " ; ".join(doutes) if doutes else ""))
        self._refresh_cal_label()
        self._refresh_target()

    def _refresh_cal_label(self) -> None:
        self.lbl_cal.setText(
            self.cal.summary() if self.cal else
            "Non calibrée — un clic ne sait pas encore combien de "
            "millimètres il vaut.")

    def _store_cal(self) -> None:
        QSettings("laser_pcb", "laser_pcb").setValue(
            "cam_cal", json.dumps(self.cal.to_dict()) if self.cal else "")
