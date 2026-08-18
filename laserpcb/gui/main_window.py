"""Fenêtre principale de laser_pcb (PySide6)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QProgressDialog, QPushButton, QScrollArea, QSpinBox, QToolBar, QToolButton,
    QVBoxLayout, QWidget,
)

from ..project import (LayerConfig, MODE_COMBINED, MODE_ISOLATION, MODE_NEGATIVE)
from .canvas import PcbCanvas
from .session import Session


class _Worker(QThread):
    """Exécute une fonction lourde dans un thread de fond (garde la GUI réactive)."""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))

_MODES = [("Isolation + évidement", MODE_COMBINED),
          ("Isolation offset", MODE_ISOLATION),
          ("Négatif (masque)", MODE_NEGATIVE)]


class LayerWidget(QGroupBox):
    """Réglages d'un calque cuivre."""
    changed = Signal()

    def __init__(self, config: LayerConfig):
        super().__init__(f"Calque {config.name} ({config.role})")
        self.config = config
        self.setCheckable(True)
        self.setChecked(config.enabled)
        self.toggled.connect(self._on_enabled)

        form = QFormLayout(self)
        self.mode = QComboBox()
        for label, val in _MODES:
            self.mode.addItem(label, val)
        self.mode.setCurrentIndex([v for _, v in _MODES].index(config.mode))
        self.mode.currentIndexChanged.connect(self._apply)

        self.adaptive = QCheckBox("Vitesse/puissance adaptatives")
        self.adaptive.setChecked(config.adaptive)
        self.adaptive.stateChanged.connect(self._apply)

        self.crosshatch = QCheckBox("Trame croisée H+V (négatif) — trait franc, sans oscillation")
        self.crosshatch.setChecked(config.crosshatch)
        self.crosshatch.stateChanged.connect(self._apply)

        self.passes = QSpinBox(); self.passes.setRange(1, 20); self.passes.setValue(config.passes)
        self.passes.valueChanged.connect(self._apply)
        self.speed = QDoubleSpinBox(); self.speed.setRange(1, 10000); self.speed.setValue(config.speed)
        self.speed.setSuffix(" mm/min"); self.speed.valueChanged.connect(self._apply)
        self.power = QDoubleSpinBox(); self.power.setRange(0, 100); self.power.setValue(config.power)
        self.power.setSuffix(" %"); self.power.valueChanged.connect(self._apply)
        self.rings = QSpinBox(); self.rings.setRange(1, 6); self.rings.setValue(config.rings)
        self.rings.valueChanged.connect(self._apply)

        form.addRow("Mode", self.mode)
        form.addRow(self.adaptive)
        form.addRow(self.crosshatch)
        form.addRow("Passes", self.passes)
        form.addRow("Vitesse", self.speed)
        form.addRow("Puissance", self.power)
        form.addRow("Anneaux (offset)", self.rings)

    def _on_enabled(self, on):
        self.config.enabled = on
        self.changed.emit()

    def _apply(self, *_):
        self.config.mode = self.mode.currentData()
        self.config.adaptive = self.adaptive.isChecked()
        self.config.crosshatch = self.crosshatch.isChecked()
        self.config.passes = self.passes.value()
        self.config.speed = self.speed.value()
        self.config.power = self.power.value()
        self.config.rings = self.rings.value()
        self.changed.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("laser_pcb")
        self.session = Session()
        self._burn_geom = None            # cache aperçu brûlure (invalidé si géométrie change)
        self._worker = None                # thread de tâche lourde en cours
        # Anti-recalcul-par-frappe : on temporise le rendu tant que l'utilisateur
        # tape (ex. saisir « 20 » ne doit pas recalculer à « 2 » puis « 20 »).
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(self._render)
        self.resize(1100, 750)

        from matplotlib.backends.backend_qtagg import NavigationToolbar2QT
        central = QWidget()
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(0, 0, 0, 0)
        self.canvas = PcbCanvas()
        self.nav = NavigationToolbar2QT(self.canvas, self)
        vbox.addWidget(self.nav)
        vbox.addWidget(self.canvas)
        self.setCentralWidget(central)
        self._build_toolbar()
        self._build_dock()
        self._build_send_dock()
        self._build_menu()
        self.statusBar().showMessage("Ouvrez un PCB (dossier, ZIP, TGZ) ou un projet .lpcb pour commencer.")

    def _build_toolbar(self):
        tb = QToolBar("Barre principale", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        # Bouton « Ouvrir » unifié (fichier / archive / dossier / projet)
        open_btn = QToolButton()
        open_btn.setText("📂 Ouvrir")
        open_btn.setPopupMode(QToolButton.MenuButtonPopup)
        menu = QMenu(open_btn)
        menu.addAction("Fichier ou archive (ZIP / TGZ / TAR)…", self.open_file)
        menu.addAction("Dossier PCB (ODB++ / Gerber)…", self.import_folder)
        menu.addAction("Projet .lpcb…", self.open_project)
        open_btn.setMenu(menu)
        open_btn.clicked.connect(self.open_file)   # clic direct = fichier/archive
        tb.addWidget(open_btn)

        tb.addSeparator()
        tb.addWidget(QLabel(" Face : "))
        self.side = QComboBox()
        self.side.addItem("Top (dessus)", "top")
        self.side.addItem("Bottom (dessous)", "bottom")
        self.side.currentIndexChanged.connect(self._on_side)
        tb.addWidget(self.side)
        self.mirror = QCheckBox("Miroir (dessous)")
        self.mirror.setChecked(True)
        self.mirror.stateChanged.connect(self._on_mirror)
        tb.addWidget(self.mirror)
        tb.addSeparator()
        self.burn_chk = QCheckBox("Aperçu brûlure (noir)")
        self.burn_chk.stateChanged.connect(self._render)
        tb.addWidget(self.burn_chk)
        tb.addSeparator()
        self._time_label = QLabel("  ⏱ —  ")
        self._time_label.setToolTip("Durée de gravure estimée de la face active "
                                    "(trajets inclus). « Aperçu parcours » pour recalculer.")
        tb.addWidget(self._time_label)

    # ---- construction UI ---------------------------------------------- #
    def _build_send_dock(self):
        from PySide6.QtWidgets import QDockWidget
        from .send_panel import SendPanel
        dock = QDockWidget("Envoyer au laser", self)
        self.send_panel = SendPanel(
            get_gcode=lambda: self.session.generate_gcode(),
            get_frame_gcode=lambda: self.session.frame_gcode(power=1.0))
        dock.setWidget(self.send_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        if getattr(self, "_layers_dock", None):
            self.tabifyDockWidget(self._layers_dock, dock)
            self._layers_dock.raise_()

    def _build_dock(self):
        from PySide6.QtWidgets import QDockWidget
        dock = QDockWidget("Calques", self)
        self._layers_dock = dock
        panel = QWidget(); self._panel_layout = QVBoxLayout(panel)

        kerf_box = QGroupBox("Général")
        kf = QFormLayout(kerf_box)
        self.kerf = QDoubleSpinBox(); self.kerf.setRange(0.01, 2.0); self.kerf.setSingleStep(0.01)
        self.kerf.setValue(self.session.project.kerf); self.kerf.setSuffix(" mm")
        self.kerf.valueChanged.connect(self._on_kerf)
        kf.addRow("Kerf (trait laser)", self.kerf)
        self.margin = QDoubleSpinBox(); self.margin.setRange(0, 0.3); self.margin.setSingleStep(0.01)
        self.margin.setValue(self.session.project.copper_margin); self.margin.setSuffix(" mm")
        self.margin.valueChanged.connect(self._on_margin)
        kf.addRow("Marge cuivre (laser+chimie)", self.margin)
        self._panel_layout.addWidget(kerf_box)

        # --- Trous (réglages liés dynamiquement à la session courante) ---
        p = self.session.project
        holes_box = QGroupBox("Trous")
        holes_box.setCheckable(True)
        holes_box.setChecked(p.hole_enabled)
        holes_box.toggled.connect(lambda on: self._set_hole("hole_enabled", on))
        hf = QFormLayout(holes_box)
        self.hole_pct = QDoubleSpinBox(); self.hole_pct.setRange(-90, 50)
        self.hole_pct.setValue(p.hole_scale_percent); self.hole_pct.setSuffix(" %")
        self.hole_pct.valueChanged.connect(lambda v: self._set_hole("hole_scale_percent", v))
        self.hole_speed = QDoubleSpinBox(); self.hole_speed.setRange(1, 10000)
        self.hole_speed.setValue(p.hole_speed); self.hole_speed.setSuffix(" mm/min")
        self.hole_speed.valueChanged.connect(lambda v: self._set_hole("hole_speed", v))
        self.hole_power = QDoubleSpinBox(); self.hole_power.setRange(0, 100)
        self.hole_power.setValue(p.hole_power); self.hole_power.setSuffix(" %")
        self.hole_power.valueChanged.connect(lambda v: self._set_hole("hole_power", v))
        self.hole_passes = QSpinBox(); self.hole_passes.setRange(1, 20)
        self.hole_passes.setValue(p.hole_passes)
        self.hole_passes.valueChanged.connect(lambda v: self._set_hole("hole_passes", v))
        hf.addRow("Taille (± % du Ø)", self.hole_pct)
        hf.addRow("Vitesse", self.hole_speed)
        hf.addRow("Puissance", self.hole_power)
        hf.addRow("Passes", self.hole_passes)
        self._holes_box = holes_box
        self._panel_layout.addWidget(holes_box)

        # --- Découpe du contour (FR4), optionnelle ---
        cut_box = QGroupBox("Découpe contour (FR4)")
        cut_box.setCheckable(True)
        cut_box.setChecked(p.cut_outline_enabled)
        cut_box.toggled.connect(lambda on: self._set_proj("cut_outline_enabled", on))
        cf = QFormLayout(cut_box)
        self.cut_speed = QDoubleSpinBox(); self.cut_speed.setRange(1, 10000)
        self.cut_speed.setValue(p.cut_outline_speed); self.cut_speed.setSuffix(" mm/min")
        self.cut_speed.valueChanged.connect(lambda v: self._set_proj("cut_outline_speed", v))
        self.cut_power = QDoubleSpinBox(); self.cut_power.setRange(0, 100)
        self.cut_power.setValue(p.cut_outline_power); self.cut_power.setSuffix(" %")
        self.cut_power.valueChanged.connect(lambda v: self._set_proj("cut_outline_power", v))
        self.cut_passes = QSpinBox(); self.cut_passes.setRange(1, 100)
        self.cut_passes.setValue(p.cut_outline_passes)
        self.cut_passes.valueChanged.connect(lambda v: self._set_proj("cut_outline_passes", v))
        cf.addRow("Vitesse", self.cut_speed)
        cf.addRow("Puissance", self.cut_power)
        cf.addRow("Passes", self.cut_passes)
        self._cut_box = cut_box
        self._panel_layout.addWidget(cut_box)

        # --- Finition (après perchlorure) : pads + trous + texte ---
        fin_box = QGroupBox("Finition (après perchlorure)")
        fin_box.setToolTip("Job séparé : retire la PEINTURE restante là où l'on soude "
                           "(pads), aux trous, et grave un texte de marquage. À lancer "
                           "après le bain de perchlorure, plaque remise au même endroit.")
        ff = QFormLayout(fin_box)
        self.fin_pads = QCheckBox("Zones de soudure (pads)")
        self.fin_pads.setChecked(p.finish_pads)
        self.fin_pads.toggled.connect(lambda on: setattr(self.session.project, "finish_pads", on))
        self.fin_holes = QCheckBox("Trous")
        self.fin_holes.setChecked(p.finish_holes)
        self.fin_holes.toggled.connect(lambda on: setattr(self.session.project, "finish_holes", on))
        self.fin_text = QLineEdit(p.finish_text)
        self.fin_text.setPlaceholderText("Texte à graver (optionnel)")
        self.fin_text.textChanged.connect(lambda t: setattr(self.session.project, "finish_text", t))
        self.fin_text_h = QDoubleSpinBox(); self.fin_text_h.setRange(0.5, 50)
        self.fin_text_h.setValue(p.finish_text_height); self.fin_text_h.setSuffix(" mm")
        self.fin_text_h.valueChanged.connect(lambda v: setattr(self.session.project, "finish_text_height", v))
        self.fin_text_x = QDoubleSpinBox(); self.fin_text_x.setRange(-500, 500)
        self.fin_text_x.setValue(p.finish_text_x); self.fin_text_x.setSuffix(" mm")
        self.fin_text_x.valueChanged.connect(lambda v: setattr(self.session.project, "finish_text_x", v))
        self.fin_text_y = QDoubleSpinBox(); self.fin_text_y.setRange(-500, 500)
        self.fin_text_y.setValue(p.finish_text_y); self.fin_text_y.setSuffix(" mm")
        self.fin_text_y.valueChanged.connect(lambda v: setattr(self.session.project, "finish_text_y", v))
        self.fin_speed = QDoubleSpinBox(); self.fin_speed.setRange(1, 10000)
        self.fin_speed.setValue(p.finish_speed); self.fin_speed.setSuffix(" mm/min")
        self.fin_speed.valueChanged.connect(lambda v: setattr(self.session.project, "finish_speed", v))
        self.fin_power = QDoubleSpinBox(); self.fin_power.setRange(0, 100)
        self.fin_power.setValue(p.finish_power); self.fin_power.setSuffix(" %")
        self.fin_power.valueChanged.connect(lambda v: setattr(self.session.project, "finish_power", v))
        self.fin_passes = QSpinBox(); self.fin_passes.setRange(1, 20)
        self.fin_passes.setValue(p.finish_passes)
        self.fin_passes.valueChanged.connect(lambda v: setattr(self.session.project, "finish_passes", v))
        self.btn_finish = QPushButton("Exporter G-code finition…")
        self.btn_finish.clicked.connect(self.export_finish)
        ff.addRow(self.fin_pads)
        ff.addRow(self.fin_holes)
        ff.addRow("Texte", self.fin_text)
        ff.addRow("Hauteur texte", self.fin_text_h)
        ff.addRow("Texte X", self.fin_text_x)
        ff.addRow("Texte Y", self.fin_text_y)
        ff.addRow("Vitesse", self.fin_speed)
        ff.addRow("Puissance", self.fin_power)
        ff.addRow("Passes", self.fin_passes)
        ff.addRow(self.btn_finish)
        self._fin_box = fin_box
        self._panel_layout.addWidget(fin_box)

        # --- Plateau / position ---
        bed_box = QGroupBox("Plateau / Position")
        bf = QFormLayout(bed_box)
        self.bed_w = QDoubleSpinBox(); self.bed_w.setRange(10, 2000); self.bed_w.setValue(p.bed_width); self.bed_w.setSuffix(" mm")
        self.bed_w.valueChanged.connect(lambda v: self._set_bed("bed_width", v))
        self.bed_h = QDoubleSpinBox(); self.bed_h.setRange(10, 2000); self.bed_h.setValue(p.bed_height); self.bed_h.setSuffix(" mm")
        self.bed_h.valueChanged.connect(lambda v: self._set_bed("bed_height", v))
        self.org_x = QDoubleSpinBox(); self.org_x.setRange(-2000, 2000); self.org_x.setValue(p.origin[0]); self.org_x.setSuffix(" mm")
        self.org_x.valueChanged.connect(self._on_origin)
        self.org_y = QDoubleSpinBox(); self.org_y.setRange(-2000, 2000); self.org_y.setValue(p.origin[1]); self.org_y.setSuffix(" mm")
        self.org_y.valueChanged.connect(self._on_origin)
        self.btn_center = QPushButton("Centrer sur le plateau"); self.btn_center.clicked.connect(self._center)
        self.mark_chk = QCheckBox("Graver le pourtour au début (pause pour poser le PCB)")
        self.mark_chk.setToolTip("Au lancement du job : grave le rectangle de la zone du "
                                 "PCB (puissance de gravure du calque), puis PAUSE (M0). "
                                 "Posez la plaque dans la zone marquée et appuyez sur "
                                 "Reprendre pour graver.")
        self.mark_chk.setChecked(p.mark_zone_enabled)
        self.mark_chk.toggled.connect(lambda on: self._set_proj("mark_zone_enabled", on))
        bf.addRow("Largeur plateau", self.bed_w)
        bf.addRow("Hauteur plateau", self.bed_h)
        bf.addRow("Position X (PCB)", self.org_x)
        bf.addRow("Position Y (PCB)", self.org_y)
        bf.addRow(self.btn_center)
        bf.addRow(self.mark_chk)
        self._panel_layout.addWidget(bed_box)

        self._layers_container = QVBoxLayout()
        self._panel_layout.addLayout(self._layers_container)
        self._panel_layout.addStretch(1)

        btns = QHBoxLayout()
        self.btn_preview = QPushButton("Aperçu parcours"); self.btn_preview.clicked.connect(self.preview_toolpaths)
        self.btn_gcode = QPushButton("Générer G-code…"); self.btn_gcode.clicked.connect(self.export_gcode)
        self.btn_frame = QPushButton("Exporter le cadre…"); self.btn_frame.clicked.connect(self.export_frame)
        self.btn_frame.setToolTip("G-code de positionnement : trace le rectangle du job "
                                  "à faible puissance (pour la carte SD).")
        btns.addWidget(self.btn_preview); btns.addWidget(self.btn_gcode); btns.addWidget(self.btn_frame)
        self._panel_layout.addLayout(btns)

        scroll = QScrollArea(); scroll.setWidget(panel); scroll.setWidgetResizable(True)
        dock.setWidget(scroll)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self._layer_widgets: list[LayerWidget] = []

    def _build_menu(self):
        m = self.menuBar().addMenu("&Fichier")
        m.addAction("Ouvrir un fichier / archive…", self.open_file)
        m.addAction("Ouvrir un dossier PCB…", self.import_folder)
        m.addAction("Ouvrir un projet .lpcb…", self.open_project)
        m.addSeparator()
        m.addAction("Enregistrer le projet…", self.save_project)
        m.addAction("Exporter le G-code (face active)…", self.export_gcode)
        m.addAction("Exporter la finition (pads/trous/texte)…", self.export_finish)
        m.addSeparator()
        m.addAction("Quitter", self.close)

    def _rebuild_layers(self):
        for w in self._layer_widgets:
            w.setParent(None)
        self._layer_widgets = []
        for layer in self.session.layers:
            w = LayerWidget(layer.config)
            w.changed.connect(self._invalidate_toolpaths)   # mode/passes/... change les parcours
            self._layers_container.addWidget(w)
            self._layer_widgets.append(w)

    # ---- actions ------------------------------------------------------- #
    def _on_kerf(self, v):
        self.session.project.kerf = v
        self._invalidate_toolpaths()       # le kerf change les parcours

    def _on_margin(self, v):
        self.session.project.copper_margin = v
        self._invalidate_toolpaths()       # la marge élargit le cuivre

    def _set_hole(self, attr, v):
        setattr(self.session.project, attr, v)
        self._invalidate_toolpaths()       # les trous font partie des parcours

    def _set_proj(self, attr, v):
        setattr(self.session.project, attr, v)
        self._invalidate_toolpaths()

    def _active_roles(self):
        return [self.session.active_side]

    def _on_side(self, *_):
        self.session.active_side = self.side.currentData()
        self._invalidate_toolpaths()       # autre face = autres parcours

    def _on_mirror(self, *_):
        self.session.mirror_bottom = self.mirror.isChecked()
        self._render()

    def _set_bed(self, attr, v):
        setattr(self.session.project, attr, v)
        self._debounce.start()             # temporisé (frappe)

    def _on_origin(self, *_):
        self.session.project.origin = (self.org_x.value(), self.org_y.value())
        self._debounce.start()             # temporisé (frappe)

    def _center(self):
        self.session.center_on_bed()
        ox, oy = self.session.project.origin
        for spin, val in ((self.org_x, ox), (self.org_y, oy)):
            spin.blockSignals(True); spin.setValue(val); spin.blockSignals(False)
        self._render()

    def open_file(self):
        """Ouvre un fichier : archive PCB (ZIP/TGZ/TAR) ou projet .lpcb (auto)."""
        f, _ = QFileDialog.getOpenFileName(
            self, "Ouvrir un fichier",
            filter="PCB & projets (*.zip *.tgz *.tar *.tar.gz *.tbz2 *.lpcb);;Tous (*)")
        if not f:
            return
        if f.lower().endswith(".lpcb"):
            self._open_project_path(f)
        else:
            self._import(f)

    def import_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Dossier PCB (Gerber ou ODB++)")
        if d:
            self._import(d)

    def _refresh_settings(self):
        """Synchronise les widgets avec la session, **sans** réécrire les valeurs
        (signaux bloqués : le clamping des spin-box ne doit pas muter le projet)."""
        p = self.session.project
        pairs = [
            (self.kerf, p.kerf), (self.margin, p.copper_margin),
            (self.hole_pct, p.hole_scale_percent),
            (self.hole_speed, p.hole_speed), (self.hole_power, p.hole_power),
            (self.hole_passes, p.hole_passes), (self.bed_w, p.bed_width),
            (self.bed_h, p.bed_height), (self.org_x, p.origin[0]), (self.org_y, p.origin[1]),
            (self.cut_speed, p.cut_outline_speed), (self.cut_power, p.cut_outline_power),
            (self.cut_passes, p.cut_outline_passes),
            (self.fin_text_h, p.finish_text_height), (self.fin_text_x, p.finish_text_x),
            (self.fin_text_y, p.finish_text_y), (self.fin_speed, p.finish_speed),
            (self.fin_power, p.finish_power), (self.fin_passes, p.finish_passes),
        ]
        for widget, val in pairs:
            widget.blockSignals(True); widget.setValue(val); widget.blockSignals(False)
        for widget, state in ((self._holes_box, p.hole_enabled),
                              (self._cut_box, p.cut_outline_enabled),
                              (self.mark_chk, p.mark_zone_enabled),
                              (self.fin_pads, p.finish_pads),
                              (self.fin_holes, p.finish_holes),
                              (self.mirror, self.session.mirror_bottom)):
            widget.blockSignals(True); widget.setChecked(state); widget.blockSignals(False)
        self.fin_text.blockSignals(True)
        self.fin_text.setText(p.finish_text)
        self.fin_text.blockSignals(False)
        self.side.blockSignals(True)
        self.side.setCurrentIndex(0 if self.session.active_side == "top" else 1)
        self.side.blockSignals(False)

    def _import(self, path):
        def load():
            s = Session()
            s.import_source(path)
            return s
        self.run_busy(load, self._on_session_loaded, "Ouverture du PCB…")

    def _on_session_loaded(self, session):
        self.session = session
        self._burn_geom = None
        self._time_label.setText("  ⏱ —  ")
        self._refresh_settings()
        self._rebuild_layers()
        self._render()
        self.statusBar().showMessage(
            f"{self.session.project.source_kind.upper()} — {len(self.session.layers)} calques, "
            f"{len(self.session.holes)} trous.")

    def _invalidate_toolpaths(self, *_):
        """La géométrie des parcours a changé : recalcul nécessaire au prochain rendu.
        Temporisé (debounce) pour ne pas recalculer à chaque frappe."""
        self._burn_geom = None
        self._time_label.setText("⏱ …")       # obsolète tant qu'on n'a pas recalculé
        self._debounce.start()                 # (re)lance le compte à rebours

    def run_busy(self, fn, on_done, title="Calcul en cours…"):
        """Exécute ``fn`` (calcul lourd) dans un thread, avec un dialogue de chargement
        animé — la fenêtre ne gèle plus. ``on_done(result)`` s'exécute sur la GUI."""
        dlg = QProgressDialog(title, None, 0, 0, self)   # 0,0 = barre indéterminée
        dlg.setWindowTitle("laser_pcb")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setCancelButton(None)
        dlg.setMinimumDuration(0)
        dlg.show()

        worker = _Worker(fn)

        def finish(result=None, err=None):
            dlg.close()
            self._worker = None
            if err:
                QMessageBox.critical(self, "Erreur", err)
            elif on_done:
                on_done(result)

        worker.done.connect(lambda r: finish(result=r))
        worker.failed.connect(lambda m: finish(err=m))
        worker.finished.connect(worker.deleteLater)
        self._worker = worker            # garde une réf (sinon GC)
        worker.start()

    def _render(self, *_):
        burn = self.burn_chk.isChecked()
        if burn and self.session.layers and self._burn_geom is None:
            # calcul lourd (union des traits) -> thread + dialogue de chargement
            self.run_busy(self.session.burn_geometry, self._burn_ready, "Aperçu brûlure…")
            return
        self.canvas.render_session(self.session, roles=self._active_roles(),
                                   mirror=self.session.is_mirrored(), burn=burn,
                                   burn_geom=self._burn_geom if burn else None)

    def _burn_ready(self, geom):
        self._burn_geom = geom
        self._render()                   # cache rempli -> rendu immédiat

    def preview_toolpaths(self):
        if not self.session.layers:
            return
        def compute():
            tagged = self.session.active_items_tagged()
            minutes = self.session.estimate_minutes([(c, f, p) for (c, f, p, _k) in tagged])
            return tagged, minutes
        self.run_busy(compute, self._on_preview_ready, "Calcul des parcours…")

    def _on_preview_ready(self, result):
        items, minutes = result
        self.canvas.render_session(self.session, toolpath_items=items,
                                   roles=self._active_roles(),
                                   mirror=self.session.is_mirrored())
        from .. import gcode as gc
        self._time_label.setText(f"  ⏱ {gc.format_duration(minutes)}  ")
        n_tight = sum(1 for it in items if len(it) > 3 and it[3] == "tight")
        warn = (f"  —  ⚠ {n_tight} passes centrées (canal étroit : gap inversé "
                f"ou traits qui se chevauchent)" if n_tight else "")
        self.statusBar().showMessage(
            f"{self.session.active_side} — {len(items)} segments — "
            f"~{gc.format_duration(minutes)} de gravure.{warn}")

    def export_gcode(self):
        if not self.session.layers:
            QMessageBox.information(self, "G-code", "Ouvrez d'abord un PCB.")
            return
        side = self.session.active_side
        f, _ = QFileDialog.getSaveFileName(
            self, f"Exporter le G-code — face {side} (fichier ou carte SD)",
            f"{self.session.project.name}_{side}.gcode",
            filter="G-code (*.gcode *.nc)")
        if not f:
            return
        def compute():
            items = self.session.active_items()          # calculé une seule fois
            return (self.session.generate_gcode(items=items),
                    self.session.estimate_minutes(items))
        self.run_busy(compute, lambda r: self._write_gcode(f, r, side),
                      "Génération du G-code…")

    def _write_gcode(self, path, result, side):
        prog, minutes = result
        Path(path).write_text(prog, encoding="utf-8")
        from .. import gcode as gc
        self._time_label.setText(f"  ⏱ {gc.format_duration(minutes)}  ")
        self.statusBar().showMessage(
            f"G-code face {side} écrit : {path} ({len(prog.splitlines())} lignes, "
            f"~{gc.format_duration(minutes)} de gravure)")

    def export_finish(self):
        """Exporte le G-code de finition (pads / trous / texte, après perchlorure)."""
        if not self.session.layers:
            QMessageBox.information(self, "Finition", "Ouvrez d'abord un PCB.")
            return
        side = self.session.active_side
        f, _ = QFileDialog.getSaveFileName(
            self, f"Exporter la finition — face {side}",
            f"{self.session.project.name}_{side}_finition.gcode",
            filter="G-code (*.gcode *.nc)")
        if not f:
            return
        self.run_busy(self.session.generate_finish_gcode,
                      lambda prog: self._write_finish(f, prog, side),
                      "Génération de la finition…")

    def _write_finish(self, path, prog, side):
        if not prog.strip() or "G1" not in prog:
            QMessageBox.information(self, "Finition",
                                    "Rien à graver (pads/trous/texte vides ?).")
            return
        Path(path).write_text(prog, encoding="utf-8")
        self.statusBar().showMessage(
            f"Finition face {side} écrite : {path} ({len(prog.splitlines())} lignes)")

    def export_frame(self):
        """Exporte le G-code de cadrage (rectangle du job, faible puissance)."""
        if not self.session.layers:
            QMessageBox.information(self, "Cadre", "Ouvrez d'abord un PCB.")
            return
        side = self.session.active_side
        f, _ = QFileDialog.getSaveFileName(
            self, f"Exporter le cadre — face {side}",
            f"{self.session.project.name}_{side}_cadre.gcode",
            filter="G-code (*.gcode *.nc)")
        if not f:
            return
        prog = self.session.frame_gcode(power=1.0)
        if not prog.strip():
            QMessageBox.information(self, "Cadre", "Aucun job à cadrer.")
            return
        Path(f).write_text(prog, encoding="utf-8")
        b = self.session.job_bounds()
        dims = f"{b[2] - b[0]:.1f} × {b[3] - b[1]:.1f} mm" if b else "?"
        self.statusBar().showMessage(f"Cadre écrit : {f}  (rectangle {dims})")

    def save_project(self):
        if not self.session.layers:
            return
        f, _ = QFileDialog.getSaveFileName(self, "Enregistrer le projet",
                                           f"{self.session.project.name}.lpcb",
                                           filter="Projet laser_pcb (*.lpcb)")
        if f:
            self.session.save_project(f)
            self.statusBar().showMessage(f"Projet enregistré : {f}")

    def open_project(self):
        f, _ = QFileDialog.getOpenFileName(self, "Ouvrir un projet", filter="Projet (*.lpcb)")
        if f:
            self._open_project_path(f)

    def _open_project_path(self, f):
        self.run_busy(lambda: Session.open_project(f),
                      lambda s: self._on_project_opened(s, f), "Ouverture du projet…")

    def _on_project_opened(self, session, f):
        self.session = session
        self._burn_geom = None
        self._time_label.setText("  ⏱ —  ")
        self._refresh_settings()
        self._rebuild_layers()
        self._render()
        self.statusBar().showMessage(f"Projet ouvert : {Path(f).name}")
