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
        b = QPushButton("Centrer")
        b.setToolTip("Centre la sélection sur la carte — ou sur le "
                     "plateau s'il n'y a pas de carte.")
        b.clicked.connect(self.editor.center_selection_on_board)
        row2.addWidget(b)
        tl.addLayout(row2, 6, 0, 1, 2)

        plates = QHBoxLayout()
        self.lbl_plate = QLabel("Plateau 1/1")
        self.lbl_plate.setToolTip("Chaque plateau est grave separement.")
        plates.addWidget(self.lbl_plate)
        b = QPushButton("◀")
        b.setMaximumWidth(30)
        b.clicked.connect(lambda: self.editor.set_plate(self.editor.active_plate() - 1))
        plates.addWidget(b)
        b = QPushButton("▶")
        b.setMaximumWidth(30)
        b.clicked.connect(lambda: self.editor.set_plate(self.editor.active_plate() + 1))
        plates.addWidget(b)
        b = QPushButton("+ Plateau")
        b.clicked.connect(self.editor.add_plate)
        plates.addWidget(b)
        b = QPushButton("Y mettre la sélection")
        b.setToolTip("Deplace les objets choisis vers le plateau affiche.")
        b.clicked.connect(
            lambda: self.editor.move_selection_to_plate(self.editor.active_plate()))
        plates.addWidget(b)
        tl.addLayout(plates, 7, 0, 1, 2)

        rang = QHBoxLayout()
        self.btn_arrange = QPushButton("Arranger sur les plateaux")
        self.btn_arrange.setToolTip(
            "Range toutes les pieces sans chevauchement, en ouvrant autant de "
            "plateaux qu'il en faut. Les pieces trop grandes sont signalees.")
        self.btn_arrange.clicked.connect(self.arrange_all)
        rang.addWidget(self.btn_arrange)
        self.gap = QDoubleSpinBox()
        self.gap.setRange(0.0, 50.0)
        self.gap.setSingleStep(0.5)
        self.gap.setSuffix(" mm")
        self.gap.setToolTip("Ecart laisse entre deux pieces.")
        self.gap.valueChanged.connect(self._set_gap)
        rang.addWidget(QLabel("Ecart"))
        rang.addWidget(self.gap)
        tl.addLayout(rang, 8, 0, 1, 2)

        self.btn_center_all = QPushButton("Tout centrer (tous les plateaux)")
        self.btn_center_all.setToolTip(
            "Centre le contenu de chaque plateau sur son plateau, en une seule "
            "action annulable.")
        self.btn_center_all.clicked.connect(self.center_all)
        tl.addWidget(self.btn_center_all, 9, 0, 1, 2)

        self.btn_import = QPushButton("Importer… (archive, projet, SVG)")
        self.btn_import.setToolTip("Logo ou dessin fait ailleurs (Inkscape, "
                                   "Illustrator, un CAO…). On choisit sa largeur "
                                   "en mm à l'import ; il arrive sur le calque "
                                   "courant.")
        self.btn_import.clicked.connect(self.import_any_file)
        tl.addWidget(self.btn_import, 10, 0, 1, 2)
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

        editor.plates_changed.connect(self.refresh_plates)
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
        self.gap.setValue(self.editor.project.arrange_gap)
        self.refresh_plates()
        self.list.clear()
        for lay in self.editor.layers():
            it = QListWidgetItem(f"{lay.name}  ({lay.mode})")
            it.setForeground(QColor(lay.color))
            it.setData(Qt.ItemDataRole.UserRole, lay.id)
            self.list.addItem(it)
        self._loading = False
        if self.list.count():
            self.list.setCurrentRow(0)
        cb = getattr(self, "layers_changed", None)
        if cb:
            cb()                      # la barre du haut suit la meme liste

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
        """Choisit un SVG puis l'ajoute au travail en cours."""
        from PySide6.QtWidgets import QFileDialog
        f, _ = QFileDialog.getOpenFileName(self, "Importer un SVG", "",
                                           "Dessin vectoriel (*.svg)")
        if f:
            self._import_svg_path(f)

    def _import_svg_path(self, f):
        """Pose un SVG donné sur le calque courant, à la largeur demandée."""
        from pathlib import Path

        from PySide6.QtWidgets import QMessageBox

        from .. import svgimport as svg
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

    def import_any_file(self):
        """Import UNIVERSEL depuis les outils de dessin.

        Le meme aiguillage que le bouton de la barre principale : l'utilisateur
        n'a pas a savoir quel menu correspond a quel format.
        """
        from PySide6.QtWidgets import QFileDialog
        f, _ = QFileDialog.getOpenFileName(
            self, "Importer",
            filter=("Tout ce qui s'importe "
                    "(*.zip *.tgz *.tar *.tar.gz *.tbz2 *.lpcb *.svg);;"
                    "Dessin vectoriel (*.svg);;"
                    "Archive PCB (*.zip *.tgz *.tar *.tar.gz *.tbz2);;"
                    "Projet laser_pcb (*.lpcb);;Tous (*)"))
        if not f:
            return
        win = self.window()
        if hasattr(win, "import_any"):
            win.import_any(f)
        elif f.lower().endswith(".svg"):
            self._import_svg_path(f)

    def _set_gap(self, v: float):
        if not self._loading:
            self.editor.project.arrange_gap = v

    def arrange_all(self):
        """Range les pieces, puis DIT ce qui s'est passe."""
        from PySide6.QtWidgets import QMessageBox
        if not self.editor.project.shapes:
            QMessageBox.information(self, "Arranger",
                                    "Aucune forme a ranger.")
            return
        res = self.editor.arrange()
        self.refresh_plates()
        msg = (f"{res['placed']} piece(s) rangee(s) sur "
               f"{res['plates']} plateau(x).")
        if res["oversized"]:
            msg += (chr(10) + chr(10) + f"{len(res['oversized'])} piece(s) plus "
                    "grande(s) que le plateau, posee(s) seule(s) : "
                    "reduisez-les ou agrandissez le plateau.")
        QMessageBox.information(self, "Arranger", msg)

    def center_all(self):
        """Centre tous les plateaux, et le DIT."""
        from PySide6.QtWidgets import QMessageBox
        n = self.editor.center_all_plates()
        if not n:
            QMessageBox.information(self, "Tout centrer", "Aucune forme a centrer.")
            return
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(f"{n} plateau(x) centré(s).")

    def refresh_plates(self):
        n, i = self.editor.plate_count(), self.editor.active_plate()
        self.lbl_plate.setText(f"Plateau {i + 1}/{n}")

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


def add_draw_actions(toolbar, editor, panel) -> None:
    """Met les outils de dessin dans la barre du HAUT.

    Le panneau de réglages fait plus de deux mille pixels de haut : un bouton
    placé en bas y est introuvable sans dérouler longuement. Les gestes
    quotidiens — choisir un outil, importer, dupliquer, annuler — doivent être
    sous la main en permanence.
    """
    from PySide6.QtGui import QActionGroup

    toolbar.addSeparator()
    group = QActionGroup(toolbar)
    group.setExclusive(True)
    actions = {}
    for tool, label, tip in _TOOLS:
        a = toolbar.addAction(label)
        a.setCheckable(True)
        a.setToolTip(tip)
        a.triggered.connect(lambda _=False, t=tool: editor.set_tool(t))
        group.addAction(a)
        actions[tool] = a
    actions[TOOL_SELECT].setChecked(True)

    def sync(tool):
        if tool in actions:
            actions[tool].setChecked(True)
    editor.tool_changed.connect(sync)

    # --- calque courant / calque de la selection --------------------------- #
    toolbar.addSeparator()
    toolbar.addWidget(QLabel(" Calque : "))
    combo = QComboBox()
    combo.setToolTip("Calque qui recevra les NOUVELLES formes." + chr(10)
                     + "Avec une selection, le bouton a droite l'y deplace.")
    combo.setMinimumWidth(130)
    toolbar.addWidget(combo)

    def reload_layers():
        bloque = combo.blockSignals(True)
        courant = combo.currentData()
        combo.clear()
        for lay in editor.layers():
            combo.addItem(lay.name, lay.id)
            i = combo.count() - 1
            combo.setItemData(i, QColor(lay.color), Qt.ItemDataRole.ForegroundRole)
        idx = combo.findData(courant)
        combo.setCurrentIndex(max(0, idx))
        combo.blockSignals(bloque)
        editor.set_current_layer(combo.currentData() or "c1")

    combo.currentIndexChanged.connect(
        lambda _i: editor.set_current_layer(combo.currentData() or "c1"))
    reload_layers()
    panel.layers_changed = reload_layers       # le panneau previent la barre

    move = toolbar.addAction("→ calque")
    move.setToolTip("Deplace la selection vers le calque choisi a gauche.")
    move.setEnabled(False)
    move.triggered.connect(lambda: editor.assign_layer(combo.currentData() or "c1"))
    editor.selection_changed.connect(lambda n: move.setEnabled(bool(n)))

    # --- plateaux : un plateau = un job -------------------------------------- #
    toolbar.addSeparator()
    prev = toolbar.addAction("◀")
    prev.setToolTip("Plateau precedent")
    lab_plate = QLabel(" Plateau 1/1 ")
    lab_plate.setToolTip("Chaque plateau est grave separement : on change la "
                         "plaque entre deux.")
    toolbar.addWidget(lab_plate)
    nxt = toolbar.addAction("▶")
    nxt.setToolTip("Plateau suivant")
    add = toolbar.addAction("+ Plateau")
    add.setToolTip("Ouvre un plateau vide.")
    arrange = toolbar.addAction("Arranger")
    arrange.setToolTip("Range toutes les pieces sur autant de plateaux qu'il "
                       "en faut, sans chevauchement.")

    def refresh_plates():
        n, i = editor.plate_count(), editor.active_plate()
        lab_plate.setText(f" Plateau {i + 1}/{n} ")
        prev.setEnabled(i > 0)
        nxt.setEnabled(i < n - 1)

    prev.triggered.connect(lambda: editor.set_plate(editor.active_plate() - 1))
    nxt.triggered.connect(lambda: editor.set_plate(editor.active_plate() + 1))
    add.triggered.connect(editor.add_plate)
    tout = toolbar.addAction("Tout centrer")
    tout.setToolTip("Centre le contenu de CHAQUE plateau sur son plateau.")
    tout.triggered.connect(panel.center_all)
    arrange.triggered.connect(panel.arrange_all)
    editor.plates_changed.connect(refresh_plates)
    editor.changed.connect(refresh_plates)
    refresh_plates()

    toolbar.addSeparator()
    a = toolbar.addAction("Importer…")
    a.setToolTip("Archive PCB, projet .lpcb ou dessin SVG — le type est deduit "
                 "du fichier.")
    a.triggered.connect(panel.import_any_file)
    a = toolbar.addAction("Importer un SVG…")
    a.setToolTip("Logo ou dessin fait ailleurs (Inkscape, Illustrator, un CAO…).")
    a.triggered.connect(panel._import_svg)
    a = toolbar.addAction("Dupliquer")
    a.setToolTip("Ctrl+D")
    a.triggered.connect(editor.duplicate_selection)
    a = toolbar.addAction("Supprimer")
    a.setToolTip("Suppr")
    a.triggered.connect(editor.delete_selection)
    toolbar.addSeparator()
    undo = toolbar.addAction("↶")
    undo.setToolTip("Annuler (Ctrl+Z)")
    undo.triggered.connect(editor.undo.undo)
    undo.setEnabled(False)
    editor.undo.canUndoChanged.connect(undo.setEnabled)
    redo = toolbar.addAction("↷")
    redo.setToolTip("Refaire (Ctrl+Y)")
    redo.triggered.connect(editor.undo.redo)
    redo.setEnabled(False)
    editor.undo.canRedoChanged.connect(redo.setEnabled)
