"""Assistant de création de boîtes, casiers et armoires.

Fenêtre à part, avec aperçu immédiat. L'idée est celle de boxes.py : on décrit
le meuble par ses cotes, le logiciel en déduit tous les panneaux et leurs
assemblages à tenons.

Deux choses que l'assistant fait pour l'opérateur et qu'on oublie sinon :

- **l'épaisseur commande la gravure.** 4 mm de contreplaqué demandent 5 passes
  à 80 % ; le calque de découpe est réglé tout seul à partir de l'épaisseur
  saisie, et le chiffre est affiché avant de lancer quoi que ce soit ;
- **le décor est de la GRAVURE.** Motifs et textes partent sur un calque à
  faible puissance — les confondre avec la découpe ruinerait la façade.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QGraphicsScene,
                               QGraphicsView, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QSpinBox, QVBoxLayout, QWidget)
from PySide6.QtCore import QPointF

from .. import boxgen as bg

_MOTIFS = ("aucun", "rayures", "croisillons", "chevrons", "losanges", "cadre")
_CIBLES = (("facades", "Façades seulement (avant, porte)"),
           ("toutes", "Toutes les pièces"),
           ("aucune", "Aucun décor"))


class _Preview(QGraphicsView):
    """Aperçu des panneaux : contours, ouvertures et décor."""

    def __init__(self):
        self._scene = QGraphicsScene()
        super().__init__(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setBackgroundBrush(QBrush(QColor("#ffffff")))
        self.setMinimumSize(430, 330)
        self.scale(1, -1)                    # Y vers le haut, comme la machine

    def show_pieces(self, pieces: list) -> None:
        self._scene.clear()
        if not pieces:
            return
        for p in bg.layout(pieces):
            self._add(p.outline, "#1f3d7a", fill="#dce6f7", width=1.4)
            for t in p.holes:
                self._add(t, "#2b6cb0", fill="#ffffff", width=1.0)
            for d in p.decor:
                self._add(d, "#c0392b", fill=None, width=1.0)
        r = self._scene.itemsBoundingRect()
        if not r.isEmpty():
            m = max(r.width(), r.height()) * 0.03
            self.fitInView(r.adjusted(-m, -m, m, m),
                           Qt.AspectRatioMode.KeepAspectRatio)

    def _add(self, geom, stroke: str, fill=None, width: float = 1.0) -> None:
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        trace = False
        for g in getattr(geom, "geoms", [geom]):
            if g.is_empty:
                continue
            if g.geom_type == "Polygon":
                for ring in [g.exterior, *g.interiors]:
                    pts = [QPointF(x, y) for (x, y) in ring.coords]
                    if len(pts) >= 3:
                        path.addPolygon(QPolygonF(pts))
                        path.closeSubpath()
                        trace = True
            elif g.geom_type in ("LineString", "LinearRing"):
                pts = [QPointF(x, y) for (x, y) in g.coords]
                if len(pts) >= 2:
                    path.moveTo(pts[0])
                    for q in pts[1:]:
                        path.lineTo(q)
                    trace = True
        if not trace:
            return
        item = self._scene.addPath(path)
        pen = QPen(QColor(stroke))
        pen.setWidthF(width)
        pen.setCosmetic(True)
        item.setPen(pen)
        item.setBrush(QBrush(QColor(fill)) if fill else QBrush(Qt.BrushStyle.NoBrush))


class BoxWizard(QDialog):
    """Décrire un meuble, le voir, puis l'insérer dans le projet."""

    def __init__(self, parent=None, thickness: float = 4.0, kerf: float = 0.1):
        super().__init__(parent)
        self.setWindowTitle("Créer une boîte, un casier ou une armoire")
        self.setModal(False)                 # on peut garder l'app sous les yeux
        self.pieces: list = []

        root = QHBoxLayout(self)
        gauche = QVBoxLayout()
        root.addLayout(gauche, 0)

        # ---- modèle ---------------------------------------------------------- #
        self.kind = QComboBox()
        for cle, (nom, _fn) in bg.GENERATORS.items():
            self.kind.addItem(nom, cle)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        g0 = QGroupBox("Modèle")
        f0 = QFormLayout(g0)
        f0.addRow("Type", self.kind)
        gauche.addWidget(g0)

        # ---- cotes ----------------------------------------------------------- #
        self.box_dims = QGroupBox("Cotes extérieures")
        fd = QFormLayout(self.box_dims)
        self.x = self._mm(500.0, 20, 3000)
        self.y = self._mm(200.0, 20, 3000)
        self.h = self._mm(400.0, 20, 3000)
        fd.addRow("Largeur", self.x)
        fd.addRow("Profondeur", self.y)
        fd.addRow("Hauteur", self.h)
        self.heights = QLineEdit("100, 100, 100")
        self.heights.setToolTip("Hauteurs UTILES des compartiments, du bas vers "
                                "le haut, séparées par des virgules.")
        self.lbl_heights = QLabel("Compartiments (mm)")
        fd.addRow(self.lbl_heights, self.heights)
        self._dim_form = fd
        gauche.addWidget(self.box_dims)

        # ---- options du modèle ----------------------------------------------- #
        self.opts = QGroupBox("Options")
        fo = QFormLayout(self.opts)
        self.closed = QCheckBox("Boîte fermée (avec dessus)")
        self.cols = QSpinBox(); self.cols.setRange(1, 20); self.cols.setValue(2)
        self.rows = QSpinBox(); self.rows.setRange(1, 20); self.rows.setValue(2)
        self.back = QCheckBox("Dos"); self.back.setChecked(True)
        self.shelves = QSpinBox(); self.shelves.setRange(0, 20); self.shelves.setValue(1)
        self.door = QCheckBox("Porte"); self.door.setChecked(True)
        self.hinge_n = QSpinBox(); self.hinge_n.setRange(0, 8); self.hinge_n.setValue(2)
        self.hinge_d = self._mm(4.0, 1.0, 20.0)
        self.hinge_d.setToolTip("Diamètre des vis de la charnière MÉTALLIQUE que "
                                "vous monterez. Les trous sont percés à la même "
                                "hauteur sur la porte et sur le côté.")
        self.handle = QCheckBox("Poignée (trou Ø10)"); self.handle.setChecked(True)
        for libelle, w in (("", self.closed), ("Colonnes", self.cols),
                           ("Rangées", self.rows), ("", self.back),
                           ("Tablettes", self.shelves), ("", self.door),
                           ("Trous de charnière", self.hinge_n),
                           ("Ø vis charnière", self.hinge_d), ("", self.handle)):
            fo.addRow(libelle, w) if libelle else fo.addRow(w)
        self._opt_form = fo
        gauche.addWidget(self.opts)

        # ---- matériau --------------------------------------------------------- #
        gm = QGroupBox("Matériau")
        fm = QFormLayout(gm)
        self.t = self._mm(thickness, 0.5, 30.0)
        self.t.setToolTip("Épaisseur réelle du bois : elle commande à la fois "
                          "les assemblages et le nombre de passes.")
        self.kerf = self._mm(kerf, 0.0, 2.0, step=0.01, dec=2)
        self.kerf.setToolTip("Largeur du trait laser. Sans compensation, les "
                             "tenons sortent trop petits et flottent.")
        self.finger = self._mm(0.0, 0.0, 100.0)
        self.finger.setToolTip("Largeur d'un tenon. 0 = automatique (deux fois "
                               "l'épaisseur).")
        fm.addRow("Épaisseur", self.t)
        fm.addRow("Trait laser (kerf)", self.kerf)
        fm.addRow("Tenons", self.finger)
        gauche.addWidget(gm)

        # ---- décor ------------------------------------------------------------ #
        gd = QGroupBox("Décor — GRAVÉ, jamais découpé")
        fdd = QFormLayout(gd)
        self.motif = QComboBox(); self.motif.addItems(_MOTIFS)
        self.step = self._mm(12.0, 1.0, 200.0)
        self.text = QLineEdit("")
        self.text.setPlaceholderText("Texte à graver (optionnel)")
        self.text_h = self._mm(20.0, 2.0, 300.0)
        self.where = QComboBox(); self.where.addItems(("centre", "haut", "bas"))
        self.cible = QComboBox()
        for cle, lib in _CIBLES:
            self.cible.addItem(lib, cle)
        fdd.addRow("Motif", self.motif)
        fdd.addRow("Pas du motif", self.step)
        fdd.addRow("Texte", self.text)
        fdd.addRow("Hauteur du texte", self.text_h)
        fdd.addRow("Position", self.where)
        fdd.addRow("Appliqué à", self.cible)
        gauche.addWidget(gd)
        gauche.addStretch(1)

        # ---- aperçu ------------------------------------------------------------ #
        droite = QVBoxLayout()
        root.addLayout(droite, 1)
        self.preview = _Preview()
        droite.addWidget(self.preview, 1)
        self.info = QLabel("—")
        self.info.setStyleSheet("color:#334; background:#eef2ff; padding:6px;"
                                " border:1px solid #c3cbe8; border-radius:3px;")
        self.info.setWordWrap(True)
        droite.addWidget(self.info)

        self.buttons = QDialogButtonBox()
        self.buttons.addButton("Insérer dans le projet",
                               QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton("Fermer", QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        droite.addWidget(self.buttons)

        # recalcul temporisé : saisir « 400 » ne doit pas tout recalculer 3 fois
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self._rebuild)
        for w in (self.x, self.y, self.h, self.t, self.kerf, self.finger,
                  self.step, self.text_h, self.hinge_d):
            w.valueChanged.connect(self._later)
        for w in (self.cols, self.rows, self.shelves, self.hinge_n):
            w.valueChanged.connect(self._later)
        for w in (self.closed, self.back, self.door, self.handle):
            w.toggled.connect(self._later)
        for w in (self.motif, self.where, self.cible):
            w.currentIndexChanged.connect(self._later)
        for w in (self.heights, self.text):
            w.textChanged.connect(self._later)

        self._kind_changed()

    # ---- petits aides ------------------------------------------------------- #
    def _mm(self, val, lo, hi, step=1.0, dec=1):
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setDecimals(dec)
        w.setSingleStep(step)
        w.setSuffix(" mm")
        w.setValue(val)
        return w

    @staticmethod
    def _show_row(form, widget, visible: bool) -> None:
        """Masque un champ ET son étiquette : seul, le libellé resterait."""
        widget.setVisible(visible)
        lab = form.labelForField(widget)
        if lab is not None:
            lab.setVisible(visible)

    def _later(self, *_):
        self._debounce.start()

    # ---- modèle actif -------------------------------------------------------- #
    def _kind_changed(self, *_):
        k = self.kind.currentData()
        self._show_row(self._dim_form, self.h, k != "etagere")
        self.heights.setVisible(k == "etagere")
        self.lbl_heights.setVisible(k == "etagere")
        for w, actif in ((self.closed, k == "boite"),
                         (self.cols, k == "bac"), (self.rows, k == "bac"),
                         (self.back, k == "etagere"),
                         (self.shelves, k == "armoire"),
                         (self.door, k == "armoire"),
                         (self.hinge_n, k == "armoire"),
                         (self.hinge_d, k == "armoire"),
                         (self.handle, k == "armoire")):
            self._show_row(self._opt_form, w, actif)
        self._rebuild()

    def params(self) -> dict:
        k = self.kind.currentData()
        p = {"thickness": self.t.value(), "kerf": self.kerf.value(),
             "finger": self.finger.value() or None,
             "x": self.x.value(), "y": self.y.value(), "h": self.h.value()}
        if k == "boite":
            p["closed"] = self.closed.isChecked()
        elif k == "bac":
            p["cols"] = self.cols.value()
            p["rows"] = self.rows.value()
        elif k == "etagere":
            p["heights"] = self._heights()
            p["back"] = self.back.isChecked()
        elif k == "armoire":
            p.update(shelves=self.shelves.value(), door=self.door.isChecked(),
                     hinge_holes=self.hinge_n.value(),
                     hinge_dia=self.hinge_d.value(),
                     handle=self.handle.isChecked())
        return p

    def _heights(self) -> list:
        out = []
        for part in self.heights.text().replace(";", ",").split(","):
            part = part.strip().replace(",", ".")
            try:
                v = float(part)
            except ValueError:
                continue
            if v > 0:
                out.append(v)
        return out or [100.0]

    # ---- construction --------------------------------------------------------- #
    def _rebuild(self):
        k = self.kind.currentData()
        fn = bg.GENERATORS[k][1]
        try:
            pieces = fn(**self.params())
        except Exception as e:  # noqa: BLE001
            self.pieces = []
            self.preview.show_pieces([])
            self.info.setText(f"Impossible de construire ce modèle : {e}")
            return
        bg.apply_decor(pieces, motif=self.motif.currentText(),
                       step=self.step.value(), text=self.text.text(),
                       text_height=self.text_h.value(),
                       where=self.where.currentText(),
                       target=self.cible.currentData())
        self.pieces = pieces
        self.preview.show_pieces(pieces)
        self.info.setText(bg.describe(pieces, self.t.value()))
