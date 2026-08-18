"""Panneau « Envoyer au laser » : connexion série GRBL, jog/home, streaming.

Règle de concurrence (simple et sûre) :
- **À l'arrêt** : jog / home / unlock / statut (le thread principal possède le port).
- **Pendant la gravure** : seuls pause `!` / reprise `~` / stop (soft-reset) —
  des écritures d'octets uniques, jamais de lecture concurrente du port.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from ..sender import GrblSender, SerialTransport, list_ports


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
        conn = QGroupBox("Connexion GRBL")
        cl = QHBoxLayout(conn)
        self.ports = QComboBox()
        self.baud = QComboBox(); self.baud.addItems(["115200", "250000", "57600"])
        self.btn_refresh = QPushButton("↻"); self.btn_refresh.clicked.connect(self.refresh_ports)
        self.btn_connect = QPushButton("Connecter"); self.btn_connect.clicked.connect(self.toggle_connect)
        cl.addWidget(QLabel("Port")); cl.addWidget(self.ports, 1)
        cl.addWidget(self.baud); cl.addWidget(self.btn_refresh); cl.addWidget(self.btn_connect)
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
        root.addStretch(1)

        self.refresh_ports()
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
        for b in (self.btn_yp, self.btn_ym, self.btn_xp, self.btn_xm,
                  self.btn_home, self.btn_unlock, self.btn_send, self.btn_connect,
                  self.btn_pointer, self.btn_setzero, self.btn_gotozero, self.btn_frame):
            b.setEnabled(not on and (self.sender is not None or b is self.btn_connect))
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
        port = self.ports.currentText()
        if not port or port.startswith("("):
            QMessageBox.information(self, "Connexion", "Aucun port série disponible.")
            return
        try:
            transport = SerialTransport(port, int(self.baud.currentText()))
            self.sender = GrblSender(transport)
            self.sender.connect()
        except Exception as e:  # noqa: BLE001
            self.sender = None
            QMessageBox.critical(self, "Connexion", f"Échec :\n{e}")
            return
        self._set_connected(True)
        self.status.setText(f"Connecté à {port}")

    def _disconnect(self):
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
        if self.sender:
            self.status.setText("Homing…")
            self.sender.home()
            self.status.setText("Prêt")

    def _unlock(self):
        if self.sender:
            self.sender.unlock()

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
