"""Panneau « Envoyer au laser » : connexion série GRBL, jog/home, streaming.

Règle de concurrence (simple et sûre) :
- **À l'arrêt** : jog / home / unlock / statut (le thread principal possède le port).
- **Pendant la gravure** : seuls pause `!` / reprise `~` / stop (soft-reset) —
  des écritures d'octets uniques, jamais de lecture concurrente du port.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
    QWidget,
)

from ..discover import discover
from ..sender import (GrblSender, SerialTransport, TelnetTransport,
                      fluidnc_upload, list_ports)


class StreamWorker(QThread):
    progress = Signal(int, int)
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, sender: GrblSender, gcode: str):
        super().__init__()
        self.sender = sender
        self.gcode = gcode
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        try:
            res = self.sender.stream(
                self.gcode,
                progress=lambda i, n: self.progress.emit(i, n),
                should_stop=lambda: self._stop,
            )
            self.done.emit(res)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class _ScanWorker(QThread):
    """Detection mDNS en tache de fond (la recherche bloque ~1 a 3 s)."""
    found = Signal(list)

    def run(self):
        try:
            self.found.emit(discover())
        except Exception:  # noqa: BLE001
            self.found.emit([])


class _StateWorker(QThread):
    """Lit l'etat machine (bloquant sur liaison lente) hors du fil graphique."""
    state = Signal(str)

    def __init__(self, sender):
        super().__init__()
        self.sender = sender

    def run(self):
        try:
            self.state.emit(self.sender.status())
        except Exception:  # noqa: BLE001
            self.state.emit("?")


class _CmdWorker(QThread):
    """Execute une commande bloquante (homing, deverrouillage) en tache de fond."""
    done = Signal(bool, str)

    def __init__(self, fn, label: str):
        super().__init__()
        self.fn = fn
        self.label = label

    def run(self):
        try:
            self.fn()
            self.done.emit(True, self.label)
        except Exception as e:  # noqa: BLE001
            self.done.emit(False, str(e))


class SendPanel(QWidget):
    def __init__(self, get_gcode: Callable[[], str],
                 get_frame_gcode: Callable[[], str] | None = None):
        super().__init__()
        self._get_gcode = get_gcode
        self._get_frame_gcode = get_frame_gcode
        self.sender: GrblSender | None = None
        self.worker: StreamWorker | None = None

        root = QVBoxLayout(self)

        # --- connexion ---
        conn = QGroupBox("Connexion (GRBL / FluidNC)")
        cl = QHBoxLayout(conn)
        self.link = QComboBox()
        self.link.addItem("USB", "usb")
        self.link.addItem("WiFi (FluidNC)", "wifi")
        self.link.currentIndexChanged.connect(self._on_link)
        self.ports = QComboBox()
        self.baud = QComboBox(); self.baud.addItems(["115200", "250000", "57600"])
        self.host = QLineEdit(); self.host.setPlaceholderText("192.168.1.50")
        self.host.setToolTip("Adresse IP de la machine FluidNC (telnet, port 23).")
        self.btn_refresh = QPushButton("↻"); self.btn_refresh.clicked.connect(self.refresh_ports)
        self.btn_scan = QPushButton("Détecter")
        self.btn_scan.setToolTip("Cherche la graveuse sur le réseau (mDNS) et "
                                 "vérifie qu'elle répond.")
        self.btn_scan.clicked.connect(self.scan_network)
        self.btn_connect = QPushButton("Connecter"); self.btn_connect.clicked.connect(self.toggle_connect)
        self.lbl_port = QLabel("Port")
        cl.addWidget(self.link)
        cl.addWidget(self.lbl_port); cl.addWidget(self.ports, 1); cl.addWidget(self.host, 1)
        cl.addWidget(self.baud); cl.addWidget(self.btn_refresh)
        cl.addWidget(self.btn_scan); cl.addWidget(self.btn_connect)
        root.addWidget(conn)

        self.status = QLabel("Déconnecté")
        root.addWidget(self.status)

        # --- déplacements (jog) ---
        jog = QGroupBox("Déplacements")
        g = QGridLayout(jog)
        self.step = QDoubleSpinBox(); self.step.setRange(0.01, 50); self.step.setValue(1.0); self.step.setSuffix(" mm")
        self.feed = QDoubleSpinBox(); self.feed.setRange(1, 5000); self.feed.setValue(1000); self.feed.setSuffix(" mm/min")
        self.btn_yp = QPushButton("Y+"); self.btn_ym = QPushButton("Y−")
        self.btn_xp = QPushButton("X+"); self.btn_xm = QPushButton("X−")
        self.btn_home = QPushButton("⌂ Home"); self.btn_unlock = QPushButton("Déverrouiller")
        self.btn_yp.clicked.connect(lambda: self._jog(0, +1))
        self.btn_ym.clicked.connect(lambda: self._jog(0, -1))
        self.btn_xp.clicked.connect(lambda: self._jog(+1, 0))
        self.btn_xm.clicked.connect(lambda: self._jog(-1, 0))
        self.btn_home.clicked.connect(self._home)
        self.btn_unlock.clicked.connect(self._unlock)
        g.addWidget(self.btn_yp, 0, 1); g.addWidget(self.btn_xm, 1, 0)
        g.addWidget(self.btn_home, 1, 1); g.addWidget(self.btn_xp, 1, 2)
        g.addWidget(self.btn_ym, 2, 1)
        g.addWidget(QLabel("Pas"), 0, 3); g.addWidget(self.step, 0, 4)
        g.addWidget(QLabel("Vitesse"), 1, 3); g.addWidget(self.feed, 1, 4)
        g.addWidget(self.btn_unlock, 2, 3, 1, 2)
        self.home_auto = QCheckBox("Home auto à la connexion")
        self.home_auto.setToolTip("Lance l'origine machine dès la connexion. "
                                  "ATTENTION : la machine bouge — assurez-vous que "
                                  "la zone est dégagée.")
        self.home_auto.setChecked(
            QSettings("laser_pcb", "laser_pcb").value("home_auto", False, type=bool))
        self.home_auto.toggled.connect(
            lambda on: QSettings("laser_pcb", "laser_pcb").setValue("home_auto", on))
        g.addWidget(self.home_auto, 3, 0, 1, 5)
        root.addWidget(jog)

        # --- origine de travail (0,0) ---
        org = QGroupBox("Origine (0,0) — travail en absolu")
        og = QGridLayout(org)
        self.pointer_pct = QDoubleSpinBox(); self.pointer_pct.setRange(0.1, 20)
        self.pointer_pct.setValue(1.0); self.pointer_pct.setSuffix(" %")
        self.btn_pointer = QPushButton("Pointeur laser"); self.btn_pointer.setCheckable(True)
        self.btn_pointer.toggled.connect(self._toggle_pointer)
        self.btn_setzero = QPushButton("Définir 0,0 ici"); self.btn_setzero.clicked.connect(self._set_zero)
        self.btn_gotozero = QPushButton("Aller à 0,0"); self.btn_gotozero.clicked.connect(self._goto_zero)
        self.btn_frame = QPushButton("⬚ Cadrer (rectangle du job)")
        self.btn_frame.setToolTip("Trace le rectangle englobant du job à faible "
                                  "puissance pour positionner la plaque (pas le "
                                  "'Frame' de la machine qui rejoue tout le tracé).")
        self.btn_frame.clicked.connect(self._frame)
        og.addWidget(QLabel("Puissance pointeur"), 0, 0); og.addWidget(self.pointer_pct, 0, 1)
        og.addWidget(self.btn_pointer, 0, 2)
        og.addWidget(self.btn_setzero, 1, 0, 1, 2); og.addWidget(self.btn_gotozero, 1, 2)
        og.addWidget(self.btn_frame, 2, 0, 1, 3)
        root.addWidget(org)

        # --- gravure ---
        run = QGroupBox("Gravure")
        rl = QVBoxLayout(run)
        self.btn_send = QPushButton("▶ Envoyer le job"); self.btn_send.clicked.connect(self.start_stream)
        self.bar = QProgressBar()
        ctl = QHBoxLayout()
        self.btn_pause = QPushButton("⏸ Pause"); self.btn_pause.clicked.connect(self._pause)
        self.btn_resume = QPushButton("⏵ Reprise"); self.btn_resume.clicked.connect(self._resume)
        self.btn_stop = QPushButton("⏹ Stop"); self.btn_stop.clicked.connect(self._stop)
        for b in (self.btn_pause, self.btn_resume, self.btn_stop):
            ctl.addWidget(b)
        rl.addWidget(self.btn_send); rl.addWidget(self.bar); rl.addLayout(ctl)
        root.addWidget(run)

        # --- carte SD FluidNC : graver en autonome (pas de coupure possible) ---
        sd = QGroupBox("Carte SD (FluidNC)")
        sd.setToolTip("Envoie le job sur la carte SD de la machine puis le lance en "
                      "autonome : la gravure ne depend plus de la liaison.")
        sl = QHBoxLayout(sd)
        self.sd_name = QLineEdit("job.gcode")
        self.btn_sd_send = QPushButton("Envoyer sur la SD")
        self.btn_sd_send.clicked.connect(self._sd_send)
        self.btn_sd_run = QPushButton("Lancer depuis la SD")
        self.btn_sd_run.clicked.connect(self._sd_run)
        sl.addWidget(QLabel("Fichier")); sl.addWidget(self.sd_name, 1)
        sl.addWidget(self.btn_sd_send); sl.addWidget(self.btn_sd_run)
        root.addWidget(sd)
        root.addStretch(1)

        self._on_link()
        self.refresh_ports()
        # sondage periodique de l'etat machine (jamais pendant la gravure)
        self._state_timer = QTimer(self)
        self._state_timer.setInterval(1500)
        self._state_timer.timeout.connect(self._poll_state)
        self._state_worker = None
        self._cmd = None
        self._set_state("")
        self._set_streaming(False)
        self._set_connected(False)

    # ---- état des widgets -------------------------------------------------- #
    def _set_connected(self, on: bool):
        self.btn_connect.setText("Déconnecter" if on else "Connecter")
        for b in (self.btn_yp, self.btn_ym, self.btn_xp, self.btn_xm,
                  self.btn_home, self.btn_unlock, self.btn_send,
                  self.btn_pointer, self.btn_setzero, self.btn_gotozero, self.btn_frame):
            b.setEnabled(on)
        self.ports.setEnabled(not on); self.baud.setEnabled(not on)

    def _set_streaming(self, on: bool):
        if on:
            self._state_timer.stop()
        elif self.sender:
            self._state_timer.start()
        for b in (self.btn_yp, self.btn_ym, self.btn_xp, self.btn_xm,
                  self.btn_home, self.btn_unlock, self.btn_send, self.btn_connect,
                  self.btn_pointer, self.btn_setzero, self.btn_gotozero, self.btn_frame,
                  self.btn_sd_send, self.btn_sd_run):
            b.setEnabled(not on and (self.sender is not None or b is self.btn_connect))
        if not on:
            self._update_sd_buttons()
        for b in (self.btn_pause, self.btn_resume, self.btn_stop):
            b.setEnabled(on)

    # ---- connexion --------------------------------------------------------- #
    def refresh_ports(self):
        self.ports.clear()
        self.ports.addItems(list_ports() or ["(aucun port)"])

    def toggle_connect(self):
        if self.sender:
            self._disconnect()
            return
        wifi = self.link.currentData() == "wifi"
        if wifi:
            target = self.host.text().strip()
            if not target:
                QMessageBox.information(self, "Connexion",
                                        "Indiquez l'adresse IP de la machine FluidNC.")
                return
        else:
            target = self.ports.currentText()
            if not target or target.startswith("("):
                QMessageBox.information(self, "Connexion", "Aucun port série disponible.")
                return
        try:
            transport = (TelnetTransport(target) if wifi
                         else SerialTransport(target, int(self.baud.currentText())))
            self.sender = GrblSender(transport)
            self.sender.connect()
        except Exception as e:  # noqa: BLE001
            self.sender = None
            QMessageBox.critical(self, "Connexion", f"Échec :\n{e}")
            return
        self._set_connected(True)
        fw = self.sender.firmware or "contrôleur GRBL"
        self.status.setText(f"Connecté à {target} — {fw}")
        self._update_sd_buttons()
        self._state_timer.start()
        self._poll_state()
        if self.home_auto.isChecked():
            self._home()

    def _on_link(self, *_):
        """Bascule les champs selon la liaison choisie (USB ou WiFi)."""
        wifi = self.link.currentData() == "wifi"
        self.lbl_port.setText("IP" if wifi else "Port")
        self.host.setVisible(wifi)
        self.btn_scan.setVisible(wifi)
        for w in (self.ports, self.baud, self.btn_refresh):
            w.setVisible(not wifi)

    def _update_sd_buttons(self):
        """Carte SD : seulement avec FluidNC (l'envoi exige la liaison WiFi)."""
        fluid = bool(self.sender) and self.sender.is_fluidnc
        self.btn_sd_send.setEnabled(fluid and self.link.currentData() == "wifi")
        self.btn_sd_run.setEnabled(fluid)

    def _disconnect(self):
        self._state_timer.stop()
        self._set_state("")
        try:
            self.sender.close()
        except Exception:  # noqa: BLE001
            pass
        self.sender = None
        self._set_connected(False)
        self.status.setText("Déconnecté")

    # ---- jog / home -------------------------------------------------------- #
    def _jog(self, sx, sy):
        if self.sender:
            d = self.step.value()
            self.sender.jog(dx=sx * d, dy=sy * d, feed=self.feed.value())

    def _home(self):
        """Origine machine — commande BLOQUANTE, donc en tâche de fond."""
        if not self.sender or self._cmd:
            return
        self.status.setText("Prise d'origine en cours…")
        self._run_cmd(self.sender.home, "Origine machine faite.")

    def _run_cmd(self, fn, label: str):
        self._cmd = _CmdWorker(fn, label)
        self._cmd.done.connect(self._on_cmd)
        for b in (self.btn_home, self.btn_unlock, self.btn_send):
            b.setEnabled(False)
        self._cmd.start()

    def _on_cmd(self, ok: bool, msg: str):
        self._cmd = None
        for b in (self.btn_home, self.btn_unlock, self.btn_send):
            b.setEnabled(self.sender is not None)
        self.status.setText(msg if ok else f"Échec : {msg}")
        self._poll_state()

    # ---- état machine (affiché sur le bouton Home) --------------------------- #
    def _poll_state(self):
        """Demande l'état courant, hors du fil graphique (liaison lente possible)."""
        if not self.sender or self._state_worker or self.worker or self._cmd:
            return
        self._state_worker = _StateWorker(self.sender)
        self._state_worker.state.connect(self._on_state)
        self._state_worker.start()

    def _on_state(self, state: str):
        self._state_worker = None
        self._set_state(state)

    def _set_state(self, state: str):
        """Le bouton Home porte l'état : une ALARME doit sauter aux yeux."""
        alarm = state.lower().startswith("alarm")
        self.btn_home.setText(f"⌂ Home — {state.upper()}" if alarm else
                              (f"⌂ Home ({state})" if state else "⌂ Home"))
        self.btn_home.setStyleSheet(
            "background:#c0392b; color:white; font-weight:bold;" if alarm else "")
        self.btn_home.setToolTip(
            "Machine EN ALARME : lancez l'origine (ou Déverrouiller) avant de graver."
            if alarm else "Prise d'origine machine.")

    def _unlock(self):
        if self.sender and not self._cmd:
            self._run_cmd(self.sender.unlock, "Machine déverrouillée.")

    # ---- origine de travail ------------------------------------------------ #
    def _toggle_pointer(self, on):
        if not self.sender:
            return
        if on:
            self.sender.laser_on(self.pointer_pct.value())
            self.status.setText(f"Pointeur ON ({self.pointer_pct.value():.0f} %) — repère la position")
        else:
            self.sender.laser_off()
            self.status.setText("Pointeur OFF")

    def _set_zero(self):
        if self.sender:
            self.sender.set_work_zero()
            self.status.setText("Origine 0,0 définie à la position actuelle")

    def _goto_zero(self):
        if self.sender:
            self.sender.goto_zero()
            self.status.setText("Retour à 0,0")

    def _frame(self):
        """Trace le rectangle englobant du job (positionnement) via le streaming."""
        if not self.sender or self._get_frame_gcode is None:
            return
        gcode = self._get_frame_gcode()
        if not gcode.strip():
            QMessageBox.information(self, "Cadrer", "Aucun job à cadrer (ouvrez un PCB).")
            return
        self.worker = StreamWorker(self.sender, gcode)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_frame_done)
        self.worker.failed.connect(self._on_failed)
        self._set_streaming(True)
        self.status.setText("Cadrage…")
        self.worker.start()

    def _on_frame_done(self, res):
        self._set_streaming(False)
        self.status.setText("Arrêté." if res.get("stopped") else "Cadrage terminé ✔")

    # ---- detection reseau ---------------------------------------------------- #
    def scan_network(self):
        """Cherche la graveuse en mDNS et remplit l'adresse IP.

        La detection tourne dans un thread : la recherche prend ~1 a 3 s et ne
        doit pas figer la fenetre.
        """
        self.btn_scan.setEnabled(False)
        self.status.setText("Recherche de la graveuse sur le réseau...")
        self._scan = _ScanWorker()
        self._scan.found.connect(self._on_scan)
        self._scan.start()

    def _on_scan(self, machines):
        self.btn_scan.setEnabled(True)
        if not machines:
            self.status.setText("Aucune graveuse détectée.")
            QMessageBox.information(
                self, "Détection",
                "Aucune machine trouvée.{nl}{nl}"
                "Vérifiez qu'elle est allumée et sur le même réseau. Si son nom "
                "d'hôte n'est pas « fluidnc », saisissez son IP à la main "
                "(visible dans sa WebUI ou sur le routeur).".format(nl=chr(10)))
            return
        if len(machines) == 1:
            m = machines[0]
        else:
            from PySide6.QtWidgets import QInputDialog
            labels = [x.label() for x in machines]
            choice, ok = QInputDialog.getItem(self, "Détection",
                                              "Machines trouvées :", labels, 0, False)
            if not ok:
                self.status.setText("Détection annulée.")
                return
            m = machines[labels.index(choice)]
        self.host.setText(m.ip)
        self.status.setText(f"Trouvée : {m.label()}")


    # ---- carte SD FluidNC --------------------------------------------------- #
    def _sd_send(self):
        """Televerse le job sur la SD (HTTP) puis VERIFIE sa presence et sa taille.

        L'upload FluidNC peut echouer en silence (carte absente/mal formatee ->
        fichier de 0 octet) : on controle avec $SD/List plutot que de faire
        confiance au code HTTP.
        """
        if not self.sender:
            return
        gcode = self._get_gcode()
        if not gcode.strip():
            QMessageBox.information(self, "Carte SD", "Aucun G-code a envoyer.")
            return
        name = self.sd_name.text().strip() or "job.gcode"
        host = self.host.text().strip()
        self.status.setText(f"Envoi de {name} sur la carte SD...")
        try:
            fluidnc_upload(host, name, gcode)
            files = self.sender.sd_list()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Carte SD", f"Envoi impossible :{chr(10)}{e}")
            self.status.setText("Échec de l'envoi.")
            return
        path = name if name.startswith("/") else "/" + name
        size = files.get(path, files.get(name, 0))
        if not size:
            QMessageBox.warning(
                self, "Carte SD",
                f"{name} n'est pas arrive (ou fait 0 octet).{chr(10)}{chr(10)}"
                "Verifiez qu'une carte SD formatee en FAT32 est inseree. "
                "En dernier recours, copiez le fichier sur la carte depuis le PC.")
            self.status.setText("Fichier absent de la carte.")
            return
        self.status.setText(f"{path} sur la carte SD ({size} octets) - pret a lancer.")

    def _sd_run(self):
        """Lance la gravure d'un fichier deja present sur la carte SD."""
        if not self.sender:
            return
        name = self.sd_name.text().strip() or "job.gcode"
        try:
            self.sender.sd_run(name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Carte SD", f"Lancement impossible :{chr(10)}{e}")
            return
        self.status.setText(f"Gravure autonome lancee depuis la carte ({name}).")

    # ---- streaming --------------------------------------------------------- #
    def start_stream(self):
        if not self.sender:
            return
        gcode = self._get_gcode()
        if not gcode.strip():
            QMessageBox.information(self, "Gravure", "Aucun G-code à envoyer.")
            return
        self.worker = StreamWorker(self.sender, gcode)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self._set_streaming(True)
        self.status.setText("Gravure en cours…")
        self.worker.start()

    def _on_progress(self, i, n):
        self.bar.setMaximum(n); self.bar.setValue(i)

    def _on_done(self, res):
        self._set_streaming(False)
        self.status.setText("Arrêté." if res.get("stopped") else "Gravure terminée ✔")

    def _on_failed(self, msg):
        self._set_streaming(False)
        QMessageBox.critical(self, "Gravure", f"Erreur GRBL :\n{msg}")
        self.status.setText("Erreur.")

    def _pause(self):
        if self.sender:
            self.sender.feed_hold(); self.status.setText("Pause")

    def _resume(self):
        if self.sender:
            self.sender.resume(); self.status.setText("Gravure en cours…")

    def _stop(self):
        if self.worker:
            self.worker.request_stop(); self.status.setText("Arrêt demandé…")
