"""Panneau de dessin : palette d'outils et calques de travail.

Les réglages laser vivent sur le CALQUE (modèle LightBurn) : c'est donc ici
qu'on règle vitesse, puissance, passes et mode, pas objet par objet.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QColorDialog, QComboBox,
                               QDoubleSpinBox, QGridLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from .. import drawing as dr
from .editor import (TOOL_ELLIPSE, TOOL_POLYLINE, TOOL_RECT, TOOL_SELECT,
                     TOOL_TEXT)

_TOOLS = ((TOOL_SELECT, "Sélection", "Cliquer pour choisir, glisser pour déplacer. "
                                     "Rectangle de sélection dans le vide."),
          (TOOL_RECT, "Rectangle", "Glisser pour tracer un rectangle."),
          (TOOL_ELLIPSE, "Ellipse", "Glisser pour tracer une ellipse."),
          (TOOL_POLYLINE, "Ligne", "Cliquer chaque point ; double-clic, Entrée "
                                   "ou Échap pour terminer."),
          (TOOL_TEXT, "Texte", "Cliquer où poser le texte."))


class DrawingPanel(QWidget):
    """Outils + calques. Se recharge depuis le projet à chaque ouverture."""

    def __init__(self, editor, on_change=None):
        super().__init__()
        self.editor = editor
        self._on_change = on_change
        self._loading = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # ---- outils ---------------------------------------------------------- #
        tools = QGroupBox("Outils de dessin")
        tl = QGridLayout(tools)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for i, (tool, label, tip) in enumerate(_TOOLS):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, t=tool: self.editor.set_tool(t))
            self._group.addButton(b)
            b.setProperty("tool", tool)
            tl.addWidget(b, i // 2, i % 2)
            if tool == TOOL_SELECT:
                b.setChecked(True)
        self.snap = QCheckBox("Aimanter à la grille")
        self.snap.setToolTip("Les points se calent sur le pas de la grille "
                             "affichée, et les rotations tous les 15°.")
        self.snap.toggled.connect(self._set_snap)
        tl.addWidget(self.snap, 3, 0, 1, 2)

        row = QHBoxLayout()
        self.btn_undo = QPushButton("Annuler")
        self.btn_undo.setToolTip("Ctrl+Z")
        self.btn_undo.clicked.connect(editor.undo.undo)
        self.btn_redo = QPushButton("Refaire")
        self.btn_redo.setToolTip("Ctrl+Y")
        self.btn_redo.clicked.connect(editor.undo.redo)
        self.btn_del = QPushButton("Supprimer")
        self.btn_del.setToolTip("Suppr")
        self.btn_del.clicked.connect(editor.delete_selection)
        for b in (self.btn_undo, self.btn_redo, self.btn_del):
            row.addWidget(b)
        tl.addLayout(row, 4, 0, 1, 2)
        align = QGridLayout()
        for i, (label, how, tip) in enumerate((
                ("⇤", "gauche", "Aligner les bords gauches"),
                ("⇥", "droite", "Aligner les bords droits"),
                ("⇧", "haut", "Aligner les bords hauts"),
                ("⇩", "bas", "Aligner les bords bas"),
                ("↔", "centre-h", "Centrer horizontalement"),
                ("↕", "centre-v", "Centrer verticalement"))):
            b = QPushButton(label)
            b.setToolTip(tip + " (au moins deux objets)")
            b.setMaximumWidth(34)
            b.clicked.connect(lambda _=False, h=how: self.editor.align_selection(h))
            align.addWidget(b, 0, i)
        tl.addLayout(align, 5, 0, 1, 2)

        row2 = QHBoxLayout()
        b = QPushButton("Dupliquer")
        b.setToolTip("Ctrl+D")
        b.clicked.connect(self.editor.duplicate_selection)
        row2.addWidget(b)
        b = QPushButton("Centrer sur la carte")
        b.clicked.connect(self.editor.center_selection_on_board)
        row2.addWidget(b)
        tl.addLayout(row2, 6, 0, 1, 2)

        self.btn_import = QPushButton("Importer un SVG…")
        self.btn_import.setToolTip("Logo ou dessin fait ailleurs (Inkscape, "
                                   "Illustrator, un CAO…). On choisit sa largeur "
                                   "en mm à l'import ; il arrive sur le calque "
                                   "courant.")
        self.btn_import.clicked.connect(self._import_svg)
        tl.addWidget(self.btn_import, 7, 0, 1, 2)
        root.addWidget(tools)

        # ---- calques de travail ---------------------------------------------- #
        lay_box = QGroupBox("Calques de travail (dessin)")
        ll = QVBoxLayout(lay_box)
        self.list = QListWidget()
        self.list.setToolTip("Le calque sélectionné reçoit les nouvelles formes.")
        self.list.currentRowChanged.connect(self._pick_layer)
        self.list.setMaximumHeight(110)
        ll.addWidget(self.list)

        btns = QHBoxLayout()
        for label, slot, tip in (
                ("Ajouter", self._add_layer, "Nouveau calque de travail."),
                ("Couleur…", self._set_color, "Couleur d'affichage du calque."),
                ("Renommer", self._rename, ""),
                ("Supprimer", self._remove, "Impossible s'il reste des objets dessus.")):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btns.addWidget(b)
        ll.addLayout(btns)

        form = QGridLayout()
        self.mode = QComboBox()
        self.mode.addItems(list(dr.MODES))
        self.mode.setToolTip("Trait : suit le contour. Remplissage : hachures au "
                             "pas du kerf. Les deux : contour puis remplissage.")
        self.speed = QDoubleSpinBox(); self.speed.setRange(1, 10000)
        self.speed.setSuffix(" mm/min")
        self.power = QDoubleSpinBox(); self.power.setRange(0, 100)
        self.power.setSuffix(" %")
        self.passes = QSpinBox(); self.passes.setRange(1, 50)
        self.enabled = QCheckBox("Calque actif")
        for w, sig in ((self.mode, self.mode.currentTextChanged),
                       (self.speed, self.speed.valueChanged),
                       (self.power, self.power.valueChanged),
                       (self.passes, self.passes.valueChanged),
                       (self.enabled, self.enabled.toggled)):
            sig.connect(self._apply_layer)
        form.addWidget(QLabel("Mode"), 0, 0); form.addWidget(self.mode, 0, 1)
        form.addWidget(QLabel("Vitesse"), 1, 0); form.addWidget(self.speed, 1, 1)
        form.addWidget(QLabel("Puissance"), 2, 0); form.addWidget(self.power, 2, 1)
        form.addWidget(QLabel("Passes"), 3, 0); form.addWidget(self.passes, 3, 1)
        form.addWidget(self.enabled, 4, 0, 1, 2)
        ll.addLayout(form)

        self.btn_assign = QPushButton("Mettre la sélection sur ce calque")
        self.btn_assign.clicked.connect(
            lambda: self.editor.assign_layer(self._current_id()))
        ll.addWidget(self.btn_assign)
        self.info = QLabel("Aucune sélection")
        self.info.setStyleSheet("color:#777;")
        ll.addWidget(self.info)
        root.addWidget(lay_box)

        editor.selection_changed.connect(self._on_selection)
        editor.tool_changed.connect(self._on_tool)
        editor.undo.canUndoChanged.connect(self.btn_undo.setEnabled)
        editor.undo.canRedoChanged.connect(self.btn_redo.setEnabled)
        self.btn_undo.setEnabled(False)
        self.btn_redo.setEnabled(False)
        self.reload()

    # ---- synchronisation ------------------------------------------------------ #
    def reload(self) -> None:
        """Recharge la liste depuis le projet (après ouverture d'un fichier)."""
        self._loading = True
        self.list.clear()
        for lay in self.editor.layers():
            it = QListWidgetItem(f"{lay.name}  ({lay.mode})")
            it.setForeground(QColor(lay.color))
            it.setData(Qt.ItemDataRole.UserRole, lay.id)
            self.list.addItem(it)
        self._loading = False
        if self.list.count():
            self.list.setCurrentRow(0)

    def _current_id(self) -> str:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else "c1"

    def _current(self):
        return self.editor.layer_by_id(self._current_id())

    def _pick_layer(self, _row: int) -> None:
        lay = self._current()
        if lay is None:
            return
        self.editor.set_current_layer(lay.id)
        self._loading = True
        self.mode.setCurrentText(lay.mode)
        self.speed.setValue(lay.speed)
        self.power.setValue(lay.power)
        self.passes.setValue(lay.passes)
        self.enabled.setChecked(lay.enabled)
        self._loading = False

    def _apply_layer(self, *_):
        if self._loading:
            return
        lay = self._current()
        if lay is None:
            return
        lay.mode = self.mode.currentText()
        lay.speed = self.speed.value()
        lay.power = self.power.value()
        lay.passes = self.passes.value()
        lay.enabled = self.enabled.isChecked()
        row = self.list.currentRow()
        self.list.item(row).setText(f"{lay.name}  ({lay.mode})")
        self._changed()

    # ---- actions sur les calques ---------------------------------------------- #
    def _add_layer(self):
        lays = self.editor.layers()
        lay = dr.WorkLayer(id=dr.new_id("c", lays), name=f"Calque {len(lays) + 1}",
                           color=dr.next_layer_color(lays))
        lays.append(lay)
        self.reload()
        self.list.setCurrentRow(self.list.count() - 1)
        self._changed()

    def _set_color(self):
        lay = self._current()
        if lay is None:
            return
        c = QColorDialog.getColor(QColor(lay.color), self, "Couleur du calque")
        if c.isValid():
            lay.color = c.name()
            self.reload()
            self._changed()

    def _rename(self):
        lay = self._current()
        if lay is None:
            return
        name, ok = QInputDialog.getText(self, "Renommer", "Nom :", text=lay.name)
        if ok and name.strip():
            lay.name = name.strip()
            self.reload()

    def _remove(self):
        lay = self._current()
        lays = self.editor.layers()
        if lay is None or len(lays) <= 1:
            return
        used = sum(1 for s in self.editor.project.shapes if s.layer == lay.id)
        if used:
            self.info.setText(f"« {lay.name} » porte {used} objet(s) : videz-le d'abord.")
            return
        lays.remove(lay)
        self.reload()
        self._changed()

    # ---- import ---------------------------------------------------------------- #
    def _import_svg(self):
        """Pose un SVG sur le calque courant, à la largeur demandée."""
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from .. import svgimport as svg
        f, _ = QFileDialog.getOpenFileName(self, "Importer un SVG", "",
                                           "Dessin vectoriel (*.svg)")
        if not f:
            return
        try:
            w_nat, h_nat = svg.natural_size(f)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Importer un SVG",
                                 f"Lecture impossible :{chr(10)}{e}")
            return
        if w_nat <= 0:
            QMessageBox.information(
                self, "Importer un SVG",
                "Ce fichier ne contient aucune forme exploitable." + chr(10) + chr(10)
                + "Les textes doivent être convertis en chemins dans le logiciel "
                  "de dessin ; une image matricielle incluse n'est pas vectorielle.")
            return
        largeur, ok = QInputDialog.getDouble(
            self, "Importer un SVG",
            f"{Path(f).name}{chr(10)}Taille d'origine : "
            f"{w_nat:.1f} × {h_nat:.1f} mm{chr(10)}{chr(10)}Largeur voulue (mm) :",
            round(w_nat, 1), 0.5, 2000.0, 1)
        if not ok:
            return
        self.editor.import_svg(f, largeur)

    # ---- retours de l'éditeur -------------------------------------------------- #
    def _set_snap(self, on: bool):
        self.editor.snap = on

    def _on_tool(self, tool: str):
        for b in self._group.buttons():
            if b.property("tool") == tool:
                b.setChecked(True)

    def _on_selection(self, n: int):
        self.info.setText("Aucune sélection" if not n
                          else f"{n} objet(s) sélectionné(s)")
        self.btn_del.setEnabled(bool(n))
        self.btn_assign.setEnabled(bool(n))

    def _changed(self):
        if self._on_change:
            self._on_change()
