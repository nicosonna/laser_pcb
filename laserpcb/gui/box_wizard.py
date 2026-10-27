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

from PySide6.QtCore import QSettings, Qt, QRectF, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QGraphicsScene,
                               QGraphicsView, QGroupBox, QHBoxLayout, QLabel,
                               QInputDialog, QLineEdit, QMessageBox,
                               QPushButton, QSlider, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)
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


class _Preview3D(QGraphicsView):
    """Le meuble ASSEMBLÉ, en projection isométrique.

    Pas de moteur 3D : une projection suffit à juger un meuble et n'ajoute
    aucune dépendance. Les panneaux sont peints du plus lointain au plus
    proche, éclairés selon leur orientation — c'est ce dégradé qui donne le
    relief.
    """

    def __init__(self):
        self._scene = QGraphicsScene()
        super().__init__(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setBackgroundBrush(QBrush(QColor("#f7f8fa")))
        self.setMinimumSize(430, 330)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Glisser : tourner autour du meuble · molette : zoom · "
                        "double-clic : vue de départ")
        self.scale(1, -1)                     # Z vers le haut
        self.azimut, self.elevation = 45.0, 25.0
        self._pieces: list = []
        self._exploded = 0.0
        self._drag = None

    # ---- faire tourner le point de vue -------------------------------------- #
    def mousePressEvent(self, event):
        self._drag = event.position()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is None:
            return
        d = event.position() - self._drag
        self._drag = event.position()
        self.azimut = (self.azimut + d.x() * 0.45) % 360.0
        # Au-delà de ±85° on regarde le meuble par le dessus : la vue devient
        # un plan, illisible. On s'arrête avant.
        self.elevation = max(-85.0, min(85.0, self.elevation + d.y() * 0.35))
        self._redraw(fit=False)
        event.accept()

    def mouseReleaseEvent(self, event):
        self._drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self.azimut, self.elevation = 45.0, 25.0
        self._redraw(fit=True)
        event.accept()

    def wheelEvent(self, event):
        if event.angleDelta().y():
            k = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
            self.scale(k, k)
        event.accept()

    def show_pieces(self, pieces: list, exploded: float = 0.0) -> None:
        self._pieces, self._exploded = pieces, exploded
        self._redraw(fit=True)

    def _redraw(self, fit: bool = True) -> None:
        self._scene.clear()
        pieces, exploded = self._pieces, self._exploded
        for face in bg.project_iso(pieces, exploded, self.azimut, self.elevation):
            teinte = QColor("#c8a165")        # bois clair
            k = face["shade"]
            teinte = QColor(int(teinte.red() * k), int(teinte.green() * k),
                            int(teinte.blue() * k))
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.OddEvenFill)
            trace = False
            for g in getattr(face["poly"], "geoms", [face["poly"]]):
                if g.geom_type != "Polygon" or g.is_empty:
                    continue
                for ring in [g.exterior, *g.interiors]:
                    pts = [QPointF(x, y) for (x, y) in ring.coords]
                    if len(pts) >= 3:
                        path.addPolygon(QPolygonF(pts))
                        path.closeSubpath()
                        trace = True
            if not trace:
                continue
            item = self._scene.addPath(path)
            item.setBrush(QBrush(teinte))
            pen = QPen(QColor("#5c4326"))
            pen.setWidthF(1.0)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setToolTip(face["name"])
        # On ne recadre QUE si le meuble a changé : recadrer pendant qu'on
        # tourne ferait sauter la vue à chaque mouvement de souris.
        r = self._scene.itemsBoundingRect()
        if fit and not r.isEmpty():
            m = max(r.width(), r.height()) * 0.05
            self.fitInView(r.adjusted(-m, -m, m, m),
                           Qt.AspectRatioMode.KeepAspectRatio)


class BoxWizard(QDialog):
    """Décrire un meuble, le voir, puis l'insérer dans le projet."""

    def __init__(self, parent=None, thickness: float = 4.0, kerf: float = 0.1):
        super().__init__(parent)
        self.setWindowTitle("Créer une boîte, un casier ou une armoire")
        self.setModal(False)                 # on peut garder l'app sous les yeux
        self.pieces: list = []
        # La dose survit à la fermeture : c'est une propriété du BOIS et de la
        # machine, pas du meuble en cours.
        cfg = QSettings("laser_pcb", "laser_pcb")
        self.dose_par_mm = float(cfg.value("dose_par_mm", bg.DOSE_PAR_MM,
                                           type=float))

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
        self.cols.setToolTip("Nombre de cases dans la largeur. Les cloisons se "
                             "croisent à mi-bois et se retirent sans outil.")
        self.rows = QSpinBox(); self.rows.setRange(1, 20); self.rows.setValue(2)
        self.back = QCheckBox("Dos"); self.back.setChecked(True)
        self.shelves = QSpinBox(); self.shelves.setRange(0, 20); self.shelves.setValue(1)
        self.drawers = QSpinBox(); self.drawers.setRange(0, 12)
        self.drawers.setValue(3)
        self.drawers.setToolTip(
            "Nombre de tiroirs, de bas en haut. Chacun est un meuble complet : "
            "caisse, façade, poignée et compartiments.")
        self.doors = QComboBox()
        self.doors.addItem("Sans porte", 0)
        self.doors.addItem("Une porte", 1)
        self.doors.addItem("Deux portes", 2)
        self.doors.setCurrentIndex(1)
        self.doors.setToolTip("Deux portes se partagent la largeur et "
                              "s'ouvrent chacune de son côté.")
        self.hinge_n = QSpinBox(); self.hinge_n.setRange(0, 8); self.hinge_n.setValue(2)
        self.hinge_d = self._mm(4.0, 1.0, 20.0)
        self.hinge_d.setToolTip("Diamètre des vis de la charnière MÉTALLIQUE que "
                                "vous monterez. Les trous sont percés à la même "
                                "hauteur sur la porte et sur le côté.")
        self.handle = QComboBox()
        for cle, libelle in bg.HANDLES.items():
            self.handle.addItem(libelle, cle)
        self.handle.setCurrentIndex(list(bg.HANDLES).index("oblong"))
        self.handle.setToolTip(
            "Trou rond ou oblong : percé dans la façade.\n"
            "Encoche : taillée dans le bord du dessus, on y passe les doigts.\n"
            "Poignée rapportée : deux trous de vis pour une poignée du commerce.")
        self.handle_size = self._mm(60.0, 5.0, 300.0)
        self.handle_size.setToolTip("Largeur de la poignée (diamètre pour un "
                                    "trou rond, entraxe pour deux vis).")
        self.clearance = self._mm(1.5, 0.0, 20.0)
        self.clearance.setToolTip(
            "Jeu TOTAL de coulissement : 4 mm donnent 2 mm de chaque côté. "
            "Les cotes saisies sont celles du COMPARTIMENT qui doit recevoir "
            "le tiroir, pas du tiroir lui-même.")
        for libelle, w in (("", self.closed),
                           ("Compartiments (largeur)", self.cols),
                           ("Compartiments (profondeur)", self.rows),
                           ("", self.back),
                           ("Tablettes", self.shelves),
                           ("Tiroirs", self.drawers),
                           ("Portes", self.doors),
                           ("Trous de charnière", self.hinge_n),
                           ("Ø vis charnière", self.hinge_d),
                           ("Poignée", self.handle),
                           ("Taille poignée", self.handle_size),
                           ("Jeu de coulissement", self.clearance)):
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

        # ---- découpe : la vitesse se DÉDUIT du nombre de passes ------------ #
        gc = QGroupBox("Découpe")
        fc = QFormLayout(gc)
        self.passes = QSpinBox()
        self.passes.setRange(1, 30)
        self.passes.setValue(3)
        self.passes.setToolTip(
            "Ce qui traverse le bois, c'est l'énergie déposée par millimètre "
            "de course. À énergie égale, plus de passes permet d'aller plus "
            "vite à chaque passe — et ça brûle moins les bords.")
        self.power = QDoubleSpinBox()
        self.power.setRange(1, 100)
        self.power.setDecimals(0)
        self.power.setSuffix(" %")
        self.power.setValue(80)
        self.marge = QDoubleSpinBox()
        self.marge.setRange(0, 200)
        self.marge.setDecimals(0)
        self.marge.setSuffix(" %")
        self.marge.setValue(25)
        self.marge.setToolTip(
            "Marge au-dessus du seuil mesuré. Viser pile le seuil laisse des "
            "zones non traversées au moindre nœud.")
        self.lbl_speed = QLabel("—")
        self.lbl_speed.setStyleSheet("font-weight:bold;")
        self.lbl_speed.setToolTip("Vitesse calculée. Elle découle des passes, "
                                  "de la puissance et de la dose mesurée.")
        self.btn_calib = QPushButton("Recalibrer sur une découpe réussie…")
        self.btn_calib.setToolTip(
            "Le bois varie beaucoup d'un lot à l'autre. Indiquez les réglages "
            "d'une découpe qui a JUSTE traversé : tout le reste s'y ajuste.")
        self.btn_calib.clicked.connect(self._calibrer)
        fc.addRow("Passes", self.passes)
        fc.addRow("Puissance", self.power)
        fc.addRow("Marge", self.marge)
        fc.addRow("Vitesse", self.lbl_speed)
        fc.addRow(self.btn_calib)
        gauche.addWidget(gc)

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
        self.preview3d = _Preview3D()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.preview, "Panneaux à découper")
        self.tabs.addTab(self.preview3d, "Meuble assemblé")
        self.tabs.currentChanged.connect(lambda _i: self._rebuild())
        droite.addWidget(self.tabs, 1)

        ligne = QHBoxLayout()
        aide = QLabel("  Glisser pour tourner · molette : zoom · "
                      "double-clic : vue de départ  ")
        aide.setStyleSheet("color:#777;")
        ligne.addWidget(aide)
        ligne.addStretch(1)
        ligne.addWidget(QLabel("Éclaté"))
        self.explode = QSlider(Qt.Orientation.Horizontal)
        self.explode.setRange(0, 100)
        self.explode.setToolTip("Écarte les panneaux pour voir l'intérieur et "
                                "vérifier le montage.")
        self.explode.valueChanged.connect(self._later)
        ligne.addWidget(self.explode, 1)
        droite.addLayout(ligne)
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
        for w in (self.cols, self.rows, self.shelves, self.drawers,
                  self.hinge_n, self.passes):
            w.valueChanged.connect(self._later)
        for w in (self.power, self.marge):
            w.valueChanged.connect(self._later)
        for w in (self.closed, self.back):
            w.toggled.connect(self._later)
        self.doors.currentIndexChanged.connect(self._kind_changed)
        self.handle.currentIndexChanged.connect(self._kind_changed)
        for w in (self.handle_size, self.clearance):
            w.valueChanged.connect(self._later)
        for w in (self.motif, self.where, self.cible):
            w.currentIndexChanged.connect(self._later)
        for w in (self.heights, self.text):
            w.textChanged.connect(self._later)

        self._kind_changed()

    # ---- découpe ------------------------------------------------------------- #
    def cut_profile(self) -> dict:
        """Réglages de découpe correspondant aux champs."""
        return bg.cut_profile(self.t.value(), passes=self.passes.value(),
                              power=self.power.value(),
                              dose_par_mm=self.dose_par_mm,
                              marge=1.0 + self.marge.value() / 100.0)

    def _calibrer(self):
        """Recalage sur une découpe qui a JUSTE traversé.

        C'est la seule mesure qui vaille : l'épaisseur annoncée d'un
        contreplaqué et sa densité réelle n'ont qu'un rapport lointain.
        """
        ep, ok = QInputDialog.getDouble(
            self, "Recalibrer",
            "Épaisseur du bois de l'essai (mm) :", self.t.value(), 0.5, 50.0, 1)
        if not ok:
            return
        n, ok = QInputDialog.getInt(
            self, "Recalibrer", "Nombre de passes qui ont JUSTE traversé :",
            1, 1, 50)
        if not ok:
            return
        pw, ok = QInputDialog.getDouble(
            self, "Recalibrer", "Puissance de l'essai (%) :", 80.0, 1.0, 100.0, 0)
        if not ok:
            return
        v, ok = QInputDialog.getDouble(
            self, "Recalibrer", "Vitesse de l'essai (mm/min) :",
            300.0, 1.0, 10000.0, 0)
        if not ok:
            return
        self.dose_par_mm = bg.cut_dose(n, pw, v) / max(0.1, ep)
        QSettings("laser_pcb", "laser_pcb").setValue("dose_par_mm",
                                                     self.dose_par_mm)
        QMessageBox.information(
            self, "Recalibrer",
            f"Dose retenue : {self.dose_par_mm:.4f} par mm d'épaisseur."
            + chr(10) + chr(10)
            + "Tous les réglages de découpe s'y ajustent désormais.")
        self._rebuild()

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
        # Pour un tiroir on saisit le COMPARTIMENT qui doit l'accueillir : le
        # jeu est retiré ensuite, et on évite le calcul mental où l'on se
        # trompe d'un côté.
        self.box_dims.setTitle("Cotes du compartiment à équiper"
                               if k == "tiroir" else "Cotes extérieures")
        self._show_row(self._dim_form, self.h, k != "etagere")
        avec_hauteurs = k in ("etagere", "cuisine")
        self.heights.setVisible(avec_hauteurs)
        self.lbl_heights.setVisible(avec_hauteurs)
        self.lbl_heights.setText("Compartiments (mm)" if k == "etagere"
                                 else "Hauteurs des sections (mm)")
        self.heights.setToolTip(
            "Hauteurs UTILES des compartiments, du bas vers le haut."
            if k == "etagere" else
            "Hauteur de chaque section, du bas vers le haut. Laisser vide pour "
            "un partage égal.")
        if k == "cuisine" and self.clearance.value() < 3.5:
            # Un tiroir de cuisine demande plus de jeu qu'un bac posé.
            self.clearance.setValue(4.0)
        portes = (k in ("armoire", "cuisine")) and self.doors.currentData() > 0
        poignee = portes or k in ("tiroir", "cuisine")
        for w, actif in ((self.closed, k == "boite"),
                         (self.cols, k in ("boite", "bac", "armoire", "tiroir",
                                           "cuisine")),
                         (self.rows, k in ("boite", "bac", "tiroir", "cuisine")),
                         (self.back, k == "etagere"),
                         (self.shelves, k == "armoire"),
                         (self.drawers, k == "cuisine"),
                         (self.doors, k in ("armoire", "cuisine")),
                         (self.hinge_n, portes),
                         (self.hinge_d, portes),
                         (self.handle, poignee),
                         (self.handle_size,
                          poignee and self.handle.currentData() != "aucune"),
                         (self.clearance, k in ("tiroir", "cuisine"))):
            self._show_row(self._opt_form, w, actif)
        self._rebuild()

    def params(self) -> dict:
        k = self.kind.currentData()
        p = {"thickness": self.t.value(), "kerf": self.kerf.value(),
             "finger": self.finger.value() or None,
             "x": self.x.value(), "y": self.y.value(), "h": self.h.value()}
        if k == "boite":
            p["closed"] = self.closed.isChecked()
            p["cols"] = self.cols.value()
            p["rows"] = self.rows.value()
        elif k == "bac":
            p["cols"] = self.cols.value()
            p["rows"] = self.rows.value()
        elif k == "etagere":
            p["heights"] = self._heights()
            p["back"] = self.back.isChecked()
        elif k == "armoire":
            p.update(shelves=self.shelves.value(),
                     doors=self.doors.currentData(),
                     cols=self.cols.value(),
                     hinge_holes=self.hinge_n.value(),
                     hinge_dia=self.hinge_d.value(),
                     handle=self.handle.currentData(),
                     handle_size=self.handle_size.value())
        elif k == "cuisine":
            p.update(drawers=self.drawers.value(),
                     doors=self.doors.currentData(),
                     drawer_heights=self._heights(vide_si_defaut=True),
                     drawer_clearance=self.clearance.value(),
                     cols=self.cols.value(), rows=self.rows.value(),
                     handle=self.handle.currentData(),
                     handle_size=self.handle_size.value(),
                     hinge_holes=self.hinge_n.value(),
                     hinge_dia=self.hinge_d.value())
        elif k == "tiroir":
            p.update(cols=self.cols.value(), rows=self.rows.value(),
                     handle=self.handle.currentData(),
                     handle_size=self.handle_size.value(),
                     clearance=self.clearance.value())
        return p

    def _heights(self, vide_si_defaut: bool = False) -> list:
        """Hauteurs saisies. ``vide_si_defaut`` renvoie [] plutôt qu'un repli :
        pour un meuble de cuisine, aucune saisie signifie « partage égal »."""
        out = []
        for part in self.heights.text().replace(";", ",").split(","):
            part = part.strip().replace(",", ".")
            try:
                v = float(part)
            except ValueError:
                continue
            if v > 0:
                out.append(v)
        if vide_si_defaut:
            return out
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
            self.preview3d.show_pieces([])
            self.info.setText(f"Impossible de construire ce modèle : {e}")
            return
        bg.apply_decor(pieces, motif=self.motif.currentText(),
                       step=self.step.value(), text=self.text.text(),
                       text_height=self.text_h.value(),
                       where=self.where.currentText(),
                       target=self.cible.currentData())
        self.pieces = pieces
        if self.tabs.currentIndex() == 1:
            self.preview3d.show_pieces(pieces, self.explode.value() / 100.0)
        else:
            self.preview.show_pieces(pieces)
        prof = self.cut_profile()
        self.lbl_speed.setText(f"{prof['speed']:.0f} mm/min")
        self.info.setText(bg.describe(pieces, self.t.value(), prof))
