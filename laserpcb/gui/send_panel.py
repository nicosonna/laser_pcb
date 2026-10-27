"""Panneau « Envoyer au laser » : connexion série GRBL, jog/home, streaming.

Règle de concurrence (simple et sûre) :
- **À l'arrêt** : jog / home / unlock / statut (le thread principal possède le port).
- **Pendant la gravure** : seuls pause `!` / reprise `~` / stop (soft-reset) —
  des écritures d'octets uniques, jamais de lecture concurrente du port.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSettings, QThread, QTimer, Signal

from . import threads
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
    QPlainTextEdit, QWidget,
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


class _GcodeWorker(QThread):
    """Genere le G-code d une etape hors du fil graphique : la gravure demande
    plusieurs secondes de calcul, et figerait la fenetre comme le faisait le
    bouton de cadrage."""
    ready = Signal(str, str)        # (gcode, message d erreur)

    def __init__(self, fn, phase: str):
        super().__init__()
        self.fn, self.phase = fn, phase

    def run(self):
        try:
            self.ready.emit(self.fn(self.phase), "")
        except Exception as e:  # noqa: BLE001
            self.ready.emit("", str(e))


class _FrameLoopWorker(QThread):
    """Rejoue le cadre en BOUCLE jusqu a l arret : on tourne autour de la piece
    le temps de la positionner, sans relancer la commande a chaque tour."""
    lap = Signal(int)
    done = Signal(str)

    def __init__(self, sender, gcode: str):
        super().__init__()
        self.sender = sender
        self.gcode = gcode
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        n = 0
        try:
            import time
            while not self._stop:
                # PIEGE (relecture 13.10.2026) : passer should_stop a stream()
                # declenchait un soft-reset en plein deplacement -> ALARM:3,
                # position perdue, re-homing. Un tour fait quatre segments : on
                # le laisse finir et on teste l'arret ENTRE deux tours.
                self.sender.stream(self.gcode)
                n += 1
                self.lap.emit(n)
                # Respiration entre deux tours : sur une vraie machine chaque tour
                # dure des secondes, mais un cadre minuscule (ou un simulateur)
                # ferait tourner cette boucle a vide a pleine vitesse.
                for _ in range(5):
                    if self._stop:
                        break
                    time.sleep(0.05)
            self.sender.laser_off()          # securite : laser eteint a l arret
            self.done.emit("")
        except Exception as e:  # noqa: BLE001
            self.done.emit(str(e))


class _ConnectWorker(QThread):
    """Ouvre la liaison hors du fil graphique : un telnet vers une IP injoignable
    bloque plusieurs secondes et figerait la fenetre, surtout au demarrage."""
    done = Signal(object, str)          # (sender ou None, message d erreur)

    def __init__(self, wifi: bool, target: str, baud: int):
        super().__init__()
        self.wifi, self.target, self.baud = wifi, target, baud

    def run(self):
        try:
            transport = (TelnetTransport(self.target) if self.wifi
                         else SerialTransport(self.target, self.baud))
            sender = GrblSender(transport)
            sender.connect()
            self.done.emit(sender, "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(None, str(e))


class SendPanel(QWidget):
    log_line = Signal(str)      # emis depuis les threads -> ecrit sur le GUI

    def __init__(self, get_gcode: Callable[[], str],
                 get_frame_gcode: Callable[[], str] | None = None,
                 get_phase_gcode: Callable[[str], str] | None = None,
                 get_side: Callable[[], str] | None = None,
                 set_side: Callable[[str], None] | None = None,
                 get_piece_origin: Callable[[], tuple | None] | None = None):
        super().__init__()
        self._get_gcode = get_gcode
        self._get_frame_gcode = get_frame_gcode
        self._get_phase_gcode = get_phase_gcode
        self._get_side = get_side
        self._set_side = set_side
        self._get_piece_origin = get_piece_origin
        self.sender: GrblSender | None = None
        self.worker: StreamWorker | None = None
        # Appele avec True pendant une gravure : la camera de tete y coupe son
        # flux video, qui se disputerait le WiFi avec le G-code.
        self.network_guard: Callable[[bool], None] | None = None

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
        self.btn_frame = QPushButton("⬚ Cadrer en boucle")
        self.btn_frame.setToolTip("Trace le rectangle englobant du job à faible "
                                  "puissance pour positionner la plaque (pas le "
                                  "'Frame' de la machine qui rejoue tout le tracé).")
        self.btn_frame.setCheckable(True)
        self.btn_frame.toggled.connect(self._frame)
        og.addWidget(QLabel("Puissance pointeur"), 0, 0); og.addWidget(self.pointer_pct, 0, 1)
        og.addWidget(self.btn_pointer, 0, 2)
        self.btn_gotopiece = QPushButton("Aller au 0,0 de la pièce")
        self.btn_gotopiece.setToolTip(
            "Amène le laser sur le coin bas-gauche de la CARTE, d'où part la "
            "gravure. Différent de « Aller à 0,0 », qui va à l'origine de "
            "travail : le centrage sur le plateau décale la pièce.")
        self.btn_gotopiece.clicked.connect(self._goto_piece)
        og.addWidget(self.btn_setzero, 1, 0, 1, 2); og.addWidget(self.btn_gotozero, 1, 2)
        og.addWidget(self.btn_gotopiece, 2, 0, 1, 3)
        # --- correction du gabarit ---
        cfg = QSettings("laser_pcb", "laser_pcb")
        self.jig_x = QDoubleSpinBox(); self.jig_x.setRange(-200, 200)
        self.jig_x.setDecimals(2); self.jig_x.setSingleStep(0.05)
        self.jig_x.setSuffix(" mm")
        self.jig_x.setValue(cfg.value("jig_x", -4.5, type=float))
        self.jig_y = QDoubleSpinBox(); self.jig_y.setRange(-200, 200)
        self.jig_y.setDecimals(2); self.jig_y.setSingleStep(0.05)
        self.jig_y.setSuffix(" mm")
        self.jig_y.setValue(cfg.value("jig_y", 4.35, type=float))
        tip = ("Écart entre le repère du gabarit et le point visé par le laser. "
               "Il décale UNIQUEMENT le point où la tête se place pour que tu "
               "alignes le gabarit dessus — la gravure, elle, ne bouge pas. "
               "Mémorisé : c'est une calibration du montage.")
        self.jig_x.setToolTip(tip); self.jig_y.setToolTip(tip)
        for w in (self.jig_x, self.jig_y):
            w.valueChanged.connect(self._on_jig_changed)
        og.addWidget(QLabel("Gabarit X"), 3, 0); og.addWidget(self.jig_x, 3, 1)
        og.addWidget(QLabel("Y"), 3, 2); og.addWidget(self.jig_y, 3, 3)
        og.addWidget(self.btn_frame, 4, 0, 1, 4)
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
        face = QHBoxLayout()
        self._side_label = QLabel("Face")
        face.addWidget(self._side_label)
        self.side = QComboBox()
        self.side.addItem("Top (dessus)", "top")
        self.side.addItem("Bottom (dessous)", "bottom")
        self.side.currentIndexChanged.connect(self._on_side_changed)
        face.addWidget(self.side, 1)
        rl.addLayout(face)
        rl.addWidget(self.btn_send); rl.addWidget(self.bar); rl.addLayout(ctl)

        # Etapes lancables separement, A TOUT MOMENT : leurs reglages viennent de
        # l onglet Calques, et les cases a cocher de cet onglet n ont volontairement
        # aucun effet ici (elles disent ce qui part dans le job complet).
        steps = QGridLayout()
        self.btn_steps = {}
        for i, (phase, label, tip) in enumerate((
                ("gravure", "Gravure du masque",
                 "Contours, remplissage et passes centrées de la face active."),
                ("trous", "Trous",
                 "Perçages seuls (réglages du groupe Trous), même si la case est décochée."),
                ("decoupe", "Découper le print",
                 "Contour FR4 seul (réglages du groupe Découpe), même si la case "
                 "est décochée — lançable à tout moment."),
                ("serigraphie", "Sérigraphie (repères)",
                 "Encombrement des composants et leurs noms (R2, C1…), gravés "
                 "légèrement."),
                ("finition", "Finition pads",
                 "Empreintes de composants : retirer la peinture des zones à "
                 "souder, après le perchlorure."),
                ("dessin", "Dessin",
                 "Formes, lignes et textes dessinés, avec les réglages de leurs "
                 "calques de travail — lançable seul."))):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, ph=phase, lb=label: self._send_phase(ph, lb))
            steps.addWidget(b, i // 2, i % 2)
            self.btn_steps[phase] = b
        rl.addLayout(steps)
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

        # --- terminal : ce que le logiciel envoie et ce que la machine repond ---
        term = QGroupBox("Terminal")
        tl = QVBoxLayout(term)
        self.terminal = QPlainTextEdit(readOnly=True)
        self.terminal.setMaximumBlockCount(500)      # garde la fenetre legere
        self.terminal.setPlaceholderText("Les actions et les reponses machine "
                                         "s affichent ici.")
        f = self.terminal.font(); f.setFamily("Consolas"); f.setPointSize(8)
        self.terminal.setFont(f)
        self.terminal.setMinimumHeight(110)
        btn_clear = QPushButton("Effacer"); btn_clear.clicked.connect(self.terminal.clear)
        tl.addWidget(self.terminal); tl.addWidget(btn_clear)
        root.addWidget(term, 1)
        self.log_line.connect(self._append_log)

        self._on_link()
        self.refresh_ports()
        # sondage periodique de l'etat machine (jamais pendant la gravure)
        self._state_timer = QTimer(self)
        self._state_timer.setInterval(1500)
        self._state_timer.timeout.connect(self._poll_state)
        self._state_worker = None
        self._cmd = None
        self._frame_loop = None
        self._conn = None
        self._gen = None
        self._scan = None
        self._lost = 0              # sondages d'etat rates d'affilee
        # Perte de liaison : sans reconnexion automatique, une coupure laissait
        # l'onglet inutilisable jusqu'a une intervention manuelle.
        self._retry = QTimer(self)
        self._retry.setInterval(5000)
        self._retry.timeout.connect(self._retry_connect)
        self._set_state("")
        self._set_streaming(False)
        self._set_connected(False)

    # ---- indicateur de travail ------------------------------------------------ #
    def shutdown(self) -> None:
        """Arrête proprement sondages et workers AVANT la fermeture.

        À la fermeture, l'interpréteur détruit les objets : un QThread encore
        en course déclenche alors ``qFatal()`` — après coup, une fois le code
        de sortie déjà écrit. Le journal en portait la trace.
        """
        for t in (getattr(self, "_state_timer", None),
                  getattr(self, "_retry", None),
                  getattr(self, "_reaper", None)):
            if t is not None:
                t.stop()
        for w in (getattr(self, "_frame_loop", None), getattr(self, "worker", None)):
            if w is not None and hasattr(w, "request_stop"):
                w.request_stop()
        for nom in ("_state_worker", "_cmd", "_conn", "_gen", "_scan",
                    "_frame_loop", "worker"):
            w = getattr(self, nom, None)
            if w is not None:
                try:
                    w.wait(3000)
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, nom, None)
        threads.shutdown()

    # ---- fin de vie des threads ---------------------------------------------- #
    def _retire(self, th) -> None:
        """Ne lâche pas la référence d'un worker encore en course (cf. threads.py)."""
        threads.retire(th)

    def _set_busy(self, on: bool, label: str = "") -> None:
        """Barre en mode indetermine pendant un travail de duree inconnue.

        Sans ce signal visible, une generation de 20 s ou une connexion de 5 s
        ressemblaient a un gel : rien ne bougeait a l'ecran.
        """
        if on:
            self.bar.setRange(0, 0)          # animation « occupe »
            if label:
                self.status.setText(label)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(0)

    # ---- terminal ------------------------------------------------------------ #
    def _log(self, text: str) -> None:
        """Journalise (utilisable depuis n importe quel thread via le signal)."""
        self.log_line.emit(text)

    def _append_log(self, text: str) -> None:
        from PySide6.QtCore import QTime
        self.terminal.appendPlainText(f"{QTime.currentTime().toString('HH:mm:ss')}  {text}")

    def _hook_log(self, sender) -> None:
        """Branche le journal du sender sur le terminal (appele a la connexion)."""
        sender.log = lambda way, text: self._log(f"{way} {text}")

    # ---- état des widgets -------------------------------------------------- #
    def _set_connected(self, on: bool):
        self.btn_connect.setText("Déconnecter" if on else "Connecter")
        for b in (self.btn_yp, self.btn_ym, self.btn_xp, self.btn_xm,
                  self.btn_home, self.btn_unlock, self.btn_send,
                  self.btn_pointer, self.btn_setzero, self.btn_gotozero,
                  self.btn_gotopiece,
                  self.btn_frame, *self.btn_steps.values()):
            b.setEnabled(on)
        self.ports.setEnabled(not on); self.baud.setEnabled(not on)

    def _set_streaming(self, on: bool):
        if self.network_guard is not None:
            # Point de passage unique de TOUTES les gravures (job complet comme
            # etape seule) : c'est ici qu'on libere le reseau pour le G-code.
            try:
                self.network_guard(on)
            except Exception:  # noqa: BLE001
                pass            # une camera absente ne bloque pas une gravure
        if on:
            self._state_timer.stop()
        elif self.sender:
            self._state_timer.start()
        for b in (self.btn_yp, self.btn_ym, self.btn_xp, self.btn_xm,
                  self.btn_home, self.btn_unlock, self.btn_send, self.btn_connect,
                  self.btn_pointer, self.btn_setzero, self.btn_gotozero,
                  self.btn_gotopiece,
                  self.btn_sd_send, self.btn_sd_run, self.btn_frame,
                  *self.btn_steps.values()):
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
        self._start_connect(wifi, target, int(self.baud.currentText()), manual=True)

    def _start_connect(self, wifi: bool, target: str, baud: int, manual: bool):
        if getattr(self, "_conn", None):
            return
        self.btn_connect.setEnabled(False)
        self._set_busy(True, f"Connexion à {target}…")
        self._conn = _ConnectWorker(wifi, target, baud)
        self._conn.done.connect(
            lambda snd, err: self._on_connected(snd, err, wifi, target, baud, manual))
        self._conn.start()

    def _on_connected(self, sender, err: str, wifi: bool, target: str,
                      baud: int, manual: bool):
        self._retire(self._conn)
        self._conn = None
        self._set_busy(False)
        self.btn_connect.setEnabled(True)
        if sender is None:
            # Un echec de reconnexion AUTO ne doit pas ouvrir de fenetre au
            # demarrage : on le dit dans la barre d etat, le bouton reste la.
            if manual:
                QMessageBox.critical(self, "Connexion", "Échec :" + chr(10) + err)
                self.status.setText("Échec de la connexion.")
            else:
                self.status.setText(f"Graveuse ({target}) non jointe — "
                                    "cliquez sur Connecter.")
            return
        self._retry.stop()
        self.sender = sender
        self._hook_log(sender)
        self._log(f"connecte a {target} ({sender.firmware or 'GRBL'})")
        cfg = QSettings("laser_pcb", "laser_pcb")      # memorise pour la prochaine fois
        cfg.setValue("link", "wifi" if wifi else "usb")
        cfg.setValue("target", target)
        cfg.setValue("baud", baud)
        self._set_connected(True)
        fw = self.sender.firmware or "contrôleur GRBL"
        self.status.setText(f"Connecté à {target} — {fw}")
        self._update_sd_buttons()
        self._state_timer.start()
        self._poll_state()
        # Home auto seulement sur une connexion DEMANDEE : la reconnexion au
        # lancement ne doit pas faire bouger la machine sans un clic.
        if manual and self.home_auto.isChecked():
            self._home()

    def try_auto_connect(self):
        """Reconnecte la derniere graveuse utilisee, si on en a deja connu une.

        Appele par la fenetre principale uniquement : un panneau cree dans un
        test ne doit jamais tenter d ouvrir une liaison reelle.
        """
        import os
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return          # sans ecran : tests. On ne pilote JAMAIS de vraie machine
                            # (une reconnexion auto + "Home auto" ferait bouger le laser)
        cfg = QSettings("laser_pcb", "laser_pcb")
        target = cfg.value("target", "", type=str)
        if not target or self.sender:
            return
        wifi = cfg.value("link", "usb", type=str) == "wifi"
        baud = cfg.value("baud", 115200, type=int)
        self.link.setCurrentIndex(1 if wifi else 0)
        if wifi:
            self.host.setText(target)
        else:
            if self.ports.findText(target) < 0:
                self.ports.addItem(target)
            self.ports.setCurrentText(target)
            self.baud.setCurrentText(str(baud))
        self._start_connect(wifi, target, baud, manual=False)

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
            self._log(f"jog X{sx * d:+.2f} Y{sy * d:+.2f}")
            self.sender.jog(dx=sx * d, dy=sy * d, feed=self.feed.value())

    def _home(self):
        """Origine machine — commande BLOQUANTE, donc en tâche de fond."""
        if not self.sender or self._cmd:
            return
        self._log("prise d origine ($H)")
        self.status.setText("Prise d'origine en cours…")
        self._run_cmd(self.sender.home, "Origine machine faite.")

    def _run_cmd(self, fn, label: str):
        self._set_busy(True)
        self._cmd = _CmdWorker(fn, label)
        self._cmd.done.connect(self._on_cmd)
        for b in (self.btn_home, self.btn_unlock, self.btn_send):
            b.setEnabled(False)
        self._cmd.start()

    def _on_cmd(self, ok: bool, msg: str):
        self._retire(self._cmd)
        self._cmd = None
        self._set_busy(False)
        if not ok and self._looks_disconnected(msg):
            self._lost_connection(msg)
            return
        for b in (self.btn_home, self.btn_unlock, self.btn_send):
            b.setEnabled(self.sender is not None)
        self.status.setText(msg if ok else f"Échec : {msg}")
        self._poll_state()

    # ---- état machine (affiché sur le bouton Home) --------------------------- #
    def _poll_state(self):
        """Demande l'état courant, hors du fil graphique (liaison lente possible)."""
        # Rien pendant une operation qui occupe deja la liaison (gravure,
        # cadrage en boucle, commande bloquante) : deux threads sur la meme
        # liaison, c'est au mieux une reponse volee.
        if (not self.sender or self._state_worker or self.worker
                or self._cmd or self._frame_loop or self._gen):
            return
        self._state_worker = _StateWorker(self.sender)
        self._state_worker.state.connect(self._on_state)
        self._state_worker.start()

    def _on_state(self, state: str):
        self._retire(self._state_worker)
        self._state_worker = None
        if state == "?":
            # La machine ne repond plus : trois sondages rates d'affilee et on
            # considere la liaison perdue (un rate isole arrive, trois non).
            self._lost += 1
            if self._lost >= 3:
                self._lost_connection("plus de reponse")
            return
        self._lost = 0
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
            self._log("definir 0,0 ici (G10 L20 P1)")
            self.sender.set_work_zero()
            self.status.setText("Origine 0,0 définie à la position actuelle")

    def _on_jig_changed(self, *_):
        """Mémorise la calibration du montage (elle ne touche pas au travail)."""
        cfg = QSettings("laser_pcb", "laser_pcb")
        cfg.setValue("jig_x", self.jig_x.value())
        cfg.setValue("jig_y", self.jig_y.value())

    def jig_offset(self) -> tuple:
        return (self.jig_x.value(), self.jig_y.value())

    def _goto_piece(self):
        """Amène le laser au coin de la pièce (coordonnées de travail, absolu)."""
        if not self.sender or self._get_piece_origin is None:
            return
        pt = self._get_piece_origin()
        if pt is None:
            QMessageBox.information(self, "Aller à la pièce", "Ouvrez d'abord un PCB.")
            return
        # L'offset du gabarit s'applique ICI et NULLE PART AILLEURS : on amene la
        # tete au point ou le repere du gabarit doit venir se caler. Une fois la
        # plaque posee, les coordonnees de travail sont justes — la gravure n'est
        # donc pas decalee.
        jx, jy = self.jig_offset()
        x, y = pt[0] + jx, pt[1] + jy
        # Meme controle que la gravure : un point hors course mettrait la machine
        # en alarme au lieu de bouger.
        if self._travel_problem(f"G0 X{x:.3f} Y{y:.3f}"):
            return
        self._log(f"aller au 0,0 de la piece : X{x:.2f} Y{y:.2f}")
        try:
            self.sender.goto(x, y)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Aller à la pièce", str(e))
            return
        self.status.setText(f"Tête au repère du gabarit (X {x:.2f} / Y {y:.2f}) — "
                            f"alignez le gabarit dessus.")

    def _goto_zero(self):
        if self.sender:
            self._log("retour a 0,0")
            self.sender.goto_zero()
            self.status.setText("Retour à 0,0")

    def _travel_problem(self, gcode: str) -> bool:
        """Refuse un job qui sortirait de la course machine, AVANT de l envoyer.

        Sans ce controle la machine grave, puis s arrete en ALARM:2 au premier
        point hors course -- avec une plaque deja entamee. Le decalage de
        l origine de travail (G54) est pris en compte : un job qui tient en
        coordonnees de travail peut sortir en coordonnees machine.
        """
        from ..sender import gcode_bounds
        bounds = gcode_bounds(gcode)          # commentaires exclus (titre du job)
        if not bounds or not self.sender:
            return False
        try:
            problem = self.sender.out_of_travel(bounds)
        except Exception:  # noqa: BLE001
            return False                    # course inconnue : on ne bloque pas
        if problem:
            # On propose la correction plutot que de laisser l utilisateur devant
            # un message : l origine de travail decalee est la cause habituelle.
            rep = QMessageBox.question(
                self, "Hors course machine",
                problem + chr(10) + chr(10) +
                "Remettre l origine de travail sur l origine machine maintenant ?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if rep == QMessageBox.Yes:
                try:
                    self.sender.clear_work_offset()
                except Exception as e:  # noqa: BLE001
                    QMessageBox.critical(self, "Origine", str(e))
                    return True
                self.status.setText("Origine de travail remise a l origine machine.")
                return self._travel_problem(gcode)   # on revalide avec la nouvelle
            self.status.setText("Job hors course machine : annulé.")
            return True
        return False


    def _frame(self, on: bool):
        """Trace le rectangle du job EN BOUCLE tant que le bouton reste enfonce :
        on tourne autour de la piece le temps de la positionner."""
        if not on:
            if self._frame_loop:
                self._frame_loop.request_stop()
                self.status.setText("Arret du cadrage…")
            return
        if not self.sender or self._get_frame_gcode is None:
            self.btn_frame.setChecked(False)
            return
        gcode = self._get_frame_gcode()
        if not gcode.strip():
            QMessageBox.information(self, "Cadrer", "Aucun job à cadrer (ouvrez un PCB).")
            self.btn_frame.setChecked(False)
            return
        if self._travel_problem(gcode):     # meme controle que la gravure
            self.btn_frame.setChecked(False)
            return
        self._log("cadrage en boucle : demarrage")
        self._set_busy(True, "Cadrage en boucle — recliquez pour arrêter.")
        # La liaison est occupee : memes verrous qu'une gravure (sinon un job
        # pouvait etre streame EN MEME TEMPS que le cadre, lignes entrelacees),
        # seul le bouton de cadrage reste actif pour arreter.
        self._set_streaming(True)
        self.btn_frame.setEnabled(True)
        self._frame_loop = _FrameLoopWorker(self.sender, gcode)
        self._frame_loop.lap.connect(
            lambda n: self.status.setText(f"Cadrage en boucle — tour {n}"))
        self._frame_loop.done.connect(self._on_frame_done)
        self._frame_loop.start()

    def _on_frame_done(self, err: str):
        self._retire(self._frame_loop)
        self._frame_loop = None
        self._set_busy(False)
        self._set_streaming(False)
        self.btn_frame.setChecked(False)
        if err:
            self._log(f"cadrage interrompu : {err}")
            QMessageBox.critical(self, "Cadrer", err)
            self.status.setText("Cadrage interrompu.")
        else:
            self._log("cadrage arrete")
            self.status.setText("Cadrage arrêté.")


    # ---- detection reseau ---------------------------------------------------- #
    def scan_network(self):
        """Cherche la graveuse en mDNS et remplit l'adresse IP.

        La detection tourne dans un thread : la recherche prend ~1 a 3 s et ne
        doit pas figer la fenetre.
        """
        self.btn_scan.setEnabled(False)
        self.status.setText("Recherche de la graveuse sur le réseau...")
        threads.retire(self._scan)
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


    # ---- perte de liaison ----------------------------------------------------- #
    @staticmethod
    def _looks_disconnected(msg: str) -> bool:
        """Distingue une coupure de liaison d'une erreur de G-code."""
        low = str(msg).lower()
        return any(w in low for w in ("timeout", "aucune reponse", "aucune réponse",
                                      "connection", "connexion", "socket", "winerror",
                                      "broken pipe", "reset by peer", "closed"))

    def _lost_connection(self, reason: str) -> None:
        """Liaison perdue : on libere tout et on retente en boucle."""
        if not self.sender:
            return
        self._log(f"liaison perdue ({reason}) — reconnexion automatique…")
        self._state_timer.stop()
        try:
            self.sender.close()
        except Exception:  # noqa: BLE001
            pass
        self.sender = None
        self._lost = 0
        self._set_connected(False)
        self._set_state("")
        self.status.setText("Liaison perdue — reconnexion automatique en cours…")
        import os
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            self._retry.start()

    def _retry_connect(self) -> None:
        """Une tentative toutes les 5 s, avec les derniers reglages qui ont marche."""
        if self.sender or self._conn:
            self._retry.stop()
            return
        cfg = QSettings("laser_pcb", "laser_pcb")
        target = cfg.value("target", "", type=str)
        if not target:
            self._retry.stop()
            return
        wifi = cfg.value("link", "usb", type=str) == "wifi"
        self._start_connect(wifi, target, cfg.value("baud", 115200, type=int),
                            manual=False)

    # ---- etapes lancables separement ----------------------------------------- #
    def _on_side_changed(self, *_):
        """Change la face active (synchronise avec la barre d outils)."""
        if self._set_side:
            self._set_side(self.side.currentData())

    # Etapes propres au circuit imprime. « dessin » et tous les outils de
    # POSITIONNEMENT laser restent la quoi qu'il arrive : ils servent autant
    # pour une plaque que pour un circuit.
    _PCB_PHASES = ("gravure", "trous", "decoupe", "serigraphie", "finition")

    def set_pcb_mode(self, pcb: bool) -> None:
        """Masque les etapes de circuit imprime quand il n'y a pas de PCB."""
        for phase in self._PCB_PHASES:
            b = self.btn_steps.get(phase)
            if b is not None:
                b.setVisible(pcb)
        if hasattr(self, "side"):
            self.side.setVisible(pcb)
        if getattr(self, "_side_label", None) is not None:
            self._side_label.setVisible(pcb)

    def sync_side(self, side: str) -> None:
        """Reflete la face choisie ailleurs, sans boucler sur le signal."""
        self.side.blockSignals(True)
        self.side.setCurrentIndex(0 if side == "top" else 1)
        self.side.blockSignals(False)

    def _send_phase(self, phase: str, label: str) -> None:
        if not self.sender or self._get_phase_gcode is None or self._gen:
            return
        self._log(f"{label} : generation du G-code…")
        self._set_busy(True, f"{label} : génération du G-code…")
        for b in self.btn_steps.values():
            b.setEnabled(False)
        self._gen = _GcodeWorker(self._get_phase_gcode, phase)
        self._gen.ready.connect(lambda g, err: self._phase_ready(g, err, label))
        self._gen.start()

    def _phase_ready(self, gcode: str, err: str, label: str) -> None:
        self._retire(self._gen)
        self._gen = None
        self._set_busy(False)
        for b in self.btn_steps.values():
            b.setEnabled(self.sender is not None)
        self.btn_send.setEnabled(self.sender is not None)
        self.btn_frame.setEnabled(self.sender is not None)
        if err:
            self._log(f"{label} : ERREUR {err}")
            QMessageBox.critical(self, label, err)
            self.status.setText(f"{label} : échec.")
            return
        if not gcode.strip() or "G1" not in gcode:
            self._log(f"{label} : rien a graver")
            QMessageBox.information(self, label, "Rien à graver pour cette étape.")
            self.status.setText(f"{label} : rien à graver.")
            return
        if self._link_busy(label) or self._travel_problem(gcode):
            return
        n = len(gcode.splitlines())
        self._log(f"{label} : envoi de {n} lignes")
        self._retire(self.worker)
        self.worker = StreamWorker(self.sender, gcode)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self._set_streaming(True)
        self.status.setText(f"{label} en cours…")
        self.worker.start()

    # ---- carte SD FluidNC --------------------------------------------------- #
    def _sd_send(self):
        """Televerse le job sur la SD puis VERIFIE sa presence et sa taille.

        Generation, televersement et verification sont faits dans un thread :
        chacun peut durer des dizaines de secondes.
        """
        if not self.sender or self._gen:
            return
        name = self.sd_name.text().strip() or "job.gcode"
        host = self.host.text().strip()
        self._set_busy(True, f"Préparation et envoi de {name}…")

        def work(_phase):
            gcode = self._get_gcode()
            if not gcode.strip():
                raise RuntimeError("Aucun G-code à envoyer.")
            fluidnc_upload(host, name, gcode)
            files = self.sender.sd_list()
            path = name if name.startswith("/") else "/" + name
            size = files.get(path, files.get(name, 0))
            if not size:
                raise RuntimeError(
                    f"{name} n'est pas arrivé (ou fait 0 octet)." + chr(10) + chr(10)
                    + "Vérifiez qu'une carte SD formatée en FAT32 est insérée. "
                      "En dernier recours, copiez le fichier depuis le PC.")
            return f"{path} sur la carte SD ({size} octets) — prêt à lancer."

        self._gen = _GcodeWorker(work, "sd")
        self._gen.ready.connect(self._sd_sent)
        self._gen.start()

    def _sd_sent(self, msg: str, err: str) -> None:
        self._retire(self._gen)
        self._gen = None
        self._set_busy(False)
        if err:
            self._log(f"carte SD : ERREUR {err}")
            QMessageBox.critical(self, "Carte SD", err)
            self.status.setText("Échec de l'envoi.")
            return
        self._log(f"carte SD : {msg}")
        self.status.setText(msg)

    def _sd_run(self):
        """Lance la gravure d'un fichier deja present sur la carte SD."""
        if not self.sender:
            return
        name = self.sd_name.text().strip() or "job.gcode"
        try:
            self._log(f"lancement depuis la carte SD : {name}")
            self.sender.sd_run(name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Carte SD", f"Lancement impossible :{chr(10)}{e}")
            return
        self.status.setText(f"Gravure autonome lancee depuis la carte ({name}).")

    # ---- streaming --------------------------------------------------------- #
    def _link_busy(self, label: str) -> bool:
        """Vrai si la liaison est deja occupee (gravure ou cadrage en cours) :
        deux flux sur la meme liaison s'entrelacent ligne a ligne."""
        busy = bool(self._frame_loop) or bool(self.worker and self.worker.isRunning())
        if busy:
            self.status.setText(f"{label} : la machine est déjà occupée "
                                "(gravure ou cadrage en cours).")
        return busy

    def start_stream(self):
        """Envoi du job complet. La GENERATION passe par un thread : sur une carte
        chargee elle demande plusieurs dizaines de secondes, et la faire ici
        figeait la fenetre le temps du calcul."""
        if not self.sender or self._gen:
            return
        if self._frame_loop:
            # Le cadrage tient la liaison : deux flux sur le meme socket, c'est
            # au mieux une reponse volee.
            self.status.setText("Liaison occupée par le cadrage — arrêtez-le d'abord.")
            return
        self._log("job complet : generation du G-code…")
        self._set_busy(True, "Génération du G-code…")
        # Verrouille tout de suite, sans attendre la fin de la generation.
        self.btn_send.setEnabled(False)
        self.btn_frame.setEnabled(False)
        self._gen = _GcodeWorker(lambda _p: self._get_gcode(), "job")
        self._gen.ready.connect(
            lambda g, err: self._phase_ready(g, err, "Job complet"))
        self._gen.start()

    def _on_progress(self, i, n):
        self.bar.setMaximum(n); self.bar.setValue(i)

    def _on_done(self, res):
        self._retire(self.worker)
        self.worker = None                   # sinon _poll_state se tait a jamais
        self._set_streaming(False)
        self._log("gravure arretee" if res.get("stopped") else "gravure terminee")
        self.status.setText("Arrêté." if res.get("stopped") else "Gravure terminée ✔")

    def _on_failed(self, msg):
        self._retire(self.worker)
        self.worker = None
        self._set_streaming(False)
        self._log(f"ERREUR : {msg}")
        if self._looks_disconnected(msg):
            self._lost_connection(msg)
            return
        QMessageBox.critical(self, "Gravure", f"Erreur GRBL :\n{msg}")
        self.status.setText("Erreur.")

    def _machine_cmd(self, action, libelle: str) -> bool:
        """Commande directe à la machine, à l'épreuve d'une liaison coupée.

        Sans ce filet, appuyer sur « Reprendre » après une coupure remontait
        une ``ConnectionResetError`` brute jusqu'à l'utilisateur — alors que la
        reconnexion automatique sait traiter le cas.
        """
        if not self.sender:
            return False
        try:
            action()
        except Exception as e:  # noqa: BLE001
            self._log(f"{libelle} : ERREUR {e}")
            if self._looks_disconnected(str(e)):
                self.status.setText("Liaison perdue — reconnexion…")
                self._lost_connection(str(e))
            else:
                QMessageBox.critical(self, libelle, str(e))
                self.status.setText(f"{libelle} : échec.")
            return False
        return True

    def _pause(self):
        if self._machine_cmd(self.sender.feed_hold if self.sender else None,
                             "Pause"):
            self.status.setText("Pause")

    def _resume(self):
        if self._machine_cmd(self.sender.resume if self.sender else None,
                             "Reprendre"):
            self.status.setText("Gravure en cours…")

    def _stop(self):
        """Arret du travail en cours, avec confirmation : la piece en cours sera
        perdue (la gravure ne reprend pas ou elle s'est arretee)."""
        if not self.worker:
            return
        rep = QMessageBox.question(
            self, "Arrêter la gravure",
            "Arrêter le travail en cours ?" + chr(10) + chr(10)
            + "La machine s'arrête immédiatement et le travail ne pourra pas "
              "reprendre où il en était : il faudra le relancer depuis le début. "
              "La pièce en cours sera probablement à refaire.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if rep != QMessageBox.Yes:
            return
        self._log("ARRET demande par l operateur")
        self.worker.request_stop()
        self.status.setText("Arrêt demandé…")
