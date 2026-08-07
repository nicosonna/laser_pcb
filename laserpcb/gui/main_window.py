"""Fenêtre principale de laser_pcb (PySide6)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..project import (LayerConfig, MODE_CENTERLINE, MODE_ISOLATION, MODE_NEGATIVE)
from .canvas import PcbCanvas
from .session import Session

_MODES = [("Axe médian (net-aware)", MODE_CENTERLINE),
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
        self.resize(1100, 750)

        self.canvas = PcbCanvas()
        self.setCentralWidget(self.canvas)
        self._build_dock()
        self._build_menu()
        self.statusBar().showMessage("Importez un PCB (Gerber ou ODB++) pour commencer.")

    # ---- construction UI ---------------------------------------------- #
    def _build_dock(self):
        from PySide6.QtWidgets import QDockWidget
        dock = QDockWidget("Calques", self)
        panel = QWidget(); self._panel_layout = QVBoxLayout(panel)

        kerf_box = QGroupBox("Général")
        kf = QFormLayout(kerf_box)
        self.kerf = QDoubleSpinBox(); self.kerf.setRange(0.01, 2.0); self.kerf.setSingleStep(0.01)
        self.kerf.setValue(self.session.project.kerf); self.kerf.setSuffix(" mm")
        self.kerf.valueChanged.connect(self._on_kerf)
        kf.addRow("Kerf (trait laser)", self.kerf)
        self._panel_layout.addWidget(kerf_box)

        self._layers_container = QVBoxLayout()
        self._panel_layout.addLayout(self._layers_container)
        self._panel_layout.addStretch(1)

        btns = QHBoxLayout()
        self.btn_preview = QPushButton("Aperçu parcours"); self.btn_preview.clicked.connect(self.preview_toolpaths)
        self.btn_gcode = QPushButton("Générer G-code…"); self.btn_gcode.clicked.connect(self.export_gcode)
        btns.addWidget(self.btn_preview); btns.addWidget(self.btn_gcode)
        self._panel_layout.addLayout(btns)

        scroll = QScrollArea(); scroll.setWidget(panel); scroll.setWidgetResizable(True)
        dock.setWidget(scroll)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self._layer_widgets: list[LayerWidget] = []

    def _build_menu(self):
        m = self.menuBar().addMenu("&Fichier")
        m.addAction("Importer un dossier PCB…", self.import_folder)
        m.addAction("Importer un ZIP Gerber…", self.import_zip)
        m.addSeparator()
        m.addAction("Ouvrir un projet…", self.open_project)
        m.addAction("Enregistrer le projet…", self.save_project)
        m.addSeparator()
        m.addAction("Exporter le G-code…", self.export_gcode)
        m.addSeparator()
        m.addAction("Quitter", self.close)

    def _rebuild_layers(self):
        for w in self._layer_widgets:
            w.setParent(None)
        self._layer_widgets = []
        for layer in self.session.layers:
            w = LayerWidget(layer.config)
            w.changed.connect(self._render)
            self._layers_container.addWidget(w)
            self._layer_widgets.append(w)

    # ---- actions ------------------------------------------------------- #
    def _on_kerf(self, v):
        self.session.project.kerf = v

    def import_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Dossier PCB (Gerber ou ODB++)")
        if d:
            self._import(d)

    def import_zip(self):
        f, _ = QFileDialog.getOpenFileName(self, "ZIP Gerber", filter="ZIP (*.zip)")
        if f:
            self._import(f)

    def _import(self, path):
        try:
            self.session = Session()
            self.session.import_source(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Import", f"Échec de l'import :\n{e}")
            return
        self.kerf.setValue(self.session.project.kerf)
        self._rebuild_layers()
        self._render()
        self.statusBar().showMessage(
            f"{self.session.project.source_kind.upper()} — {len(self.session.layers)} calques, "
            f"{len(self.session.holes)} trous.")

    def _render(self, *_):
        self.canvas.render_session(self.session)

    def preview_toolpaths(self):
        if not self.session.layers:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.statusBar().showMessage("Calcul des parcours… (peut prendre un moment)")
        try:
            items = []
            for layer in self.session.layers:
                if layer.config.enabled:
                    items.extend(self.session.layer_items(layer))
            self.canvas.render_session(self.session, toolpath_items=items)
            self.statusBar().showMessage(f"{len(items)} segments de parcours.")
        finally:
            QApplication.restoreOverrideCursor()

    def export_gcode(self):
        if not self.session.layers:
            QMessageBox.information(self, "G-code", "Importez d'abord un PCB.")
            return
        f, _ = QFileDialog.getSaveFileName(self, "Exporter le G-code (fichier ou carte SD)",
                                           f"{self.session.project.name}.gcode",
                                           filter="G-code (*.gcode *.nc)")
        if not f:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            prog = self.session.generate_gcode()
            Path(f).write_text(prog, encoding="utf-8")
        finally:
            QApplication.restoreOverrideCursor()
        self.statusBar().showMessage(f"G-code écrit : {f} ({len(prog.splitlines())} lignes)")

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
        if not f:
            return
        try:
            self.session = Session.open_project(f)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Projet", f"Échec de l'ouverture :\n{e}")
            return
        self.kerf.setValue(self.session.project.kerf)
        self._rebuild_layers()
        self._render()
        self.statusBar().showMessage(f"Projet ouvert : {Path(f).name}")
