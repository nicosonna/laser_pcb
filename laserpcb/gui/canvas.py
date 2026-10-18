"""Canevas d'aperçu : scène Qt en millimètres.

Porté de matplotlib vers ``QGraphicsView``. Matplotlib affiche très bien mais
ne sait pas éditer : pas de graphe de scène, pas de détection de clic sur un
objet, pas de poignées. La scène de Qt fournit tout cela nativement, et c'est
elle qui portera l'éditeur vectoriel (voir
``documentation/dev/editeur-vectoriel.md``).

Repère : **millimètres, Y vers le haut** comme la machine — d'où le miroir
vertical appliqué à la vue. Les textes portent donc
``ItemIgnoresTransformations``, sinon ils s'afficheraient à l'envers.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QPainter,
                           QPainterPath, QPen, QPolygonF)
from PySide6.QtWidgets import (QFileDialog, QGraphicsItem, QGraphicsRectItem,
                               QGraphicsScene, QGraphicsSimpleTextItem,
                               QGraphicsView, QLabel, QToolBar)
from shapely.affinity import scale as _scale, translate as _translate

_ROLE_COLOR = {"top": "#b5651d", "bottom": "#1d6fb5"}

# Couleurs de l'aperçu des parcours, par catégorie
_KIND_COLOR = {"contour": "#1f4fd8", "fill": "#e0645a", "hole": "#0a9d3a",
               "cut": "#ff8c00", "tight": "#d40fbf", "dessin": "#7a3fc4"}
_KIND_WIDTH = {"contour": 1.1, "fill": 0.5, "hole": 0.9, "cut": 1.3,
               "tight": 1.6, "dessin": 1.2}
_KIND_LABEL = {"contour": "tour du footprint", "fill": "remplissage",
               "hole": "trous", "cut": "découpe contour",
               "tight": "passe centrée (canal étroit)",
               "dessin": "dessin (formes, texte)"}

# Couches SOURCE (le dessin d'origine), affichables indépendamment des parcours
# Les pads avaient un bleu quasi identique a celui du "tour du footprint" :
# impossible de distinguer la couche source des parcours. Turquoise desormais.
_SRC_COLOR = {"carte": "#efe7c8", "cuivre": "#b5651d",
              "serigraphie": "#6b6b6b", "pads": "#00a39a"}

_BG = "#ffffff"
_GRID = "#ededed"
_GRID_STRONG = "#d4d4d4"

# Règles graduées : largeur de la bande de gauche, hauteur de celle du bas (px)
_RULER_W = 30
_RULER_H = 18


def _polys_to_path(polys, transform=None, tol=0.03):
    """UN seul ``QPainterPath`` pour TOUS les polygones.

    Un chemin unique plutôt que N éléments de scène : c'est ce qui tenait les
    grosses cartes à l'affichage avec matplotlib, et Qt y est tout aussi
    sensible. ``transform`` applique miroir/offset, ``tol`` simplifie pour
    l'affichage seulement — jamais pour le G-code.

    Les anneaux intérieurs deviennent des trous grâce à ``OddEvenFill``.
    """
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    rings = 0
    for poly in polys:
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        if transform is not None:
            poly = transform(poly)
        if tol:
            poly = poly.simplify(tol, preserve_topology=False)
        if poly.is_empty:
            continue
        for ring in [poly.exterior, *poly.interiors]:
            c = list(ring.coords)
            if len(c) < 3:
                continue
            path.addPolygon(QPolygonF([QPointF(x, y) for x, y in c]))
            path.closeSubpath()
            rings += 1
    return path if rings else None


def _nice_step(min_px: float, px_per_mm: float) -> float:
    """Plus petit pas « 1-2-5 » (en mm) occupant au moins ``min_px`` pixels.

    Les graduations suivent ainsi le zoom en restant sur des valeurs rondes :
    1, 2, 5, 10, 20, 50… plutôt que 3,7 mm.
    """
    import math
    if px_per_mm <= 0:
        return 1.0
    decade = 10.0 ** math.floor(math.log10(max(min_px / px_per_mm, 1e-9)))
    for mult in (1, 2, 5, 10):
        if decade * mult * px_per_mm >= min_px:
            return decade * mult
    return decade * 10


def _pen(color: str, width: float, cosmetic: bool = True,
         style=Qt.PenStyle.SolidLine) -> QPen:
    """Stylo d'épaisseur CONSTANTE à l'écran (comme les traits de matplotlib).

    Sans ``cosmetic``, l'épaisseur serait en millimètres : un zoom avant
    transformerait chaque trait en pâté, un zoom arrière le ferait disparaître.
    """
    p = QPen(QColor(color))
    p.setWidthF(width)
    p.setCosmetic(cosmetic)
    p.setStyle(style)
    p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setCapStyle(Qt.PenCapStyle.RoundCap)
    return p


class _PlateButton(QGraphicsRectItem):
    """Petite icone cliquable posee au bord d'un plateau.

    Taille ECRAN fixe (``ItemIgnoresTransformations``) : elle reste
    saisissable que l'on voie un plateau ou huit.
    """

    def __init__(self, glyph: str, tip: str, on_click, color: str = "#4a5568"):
        super().__init__(QRectF(-11, -11, 22, 22))
        self._cb = on_click
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setBrush(QBrush(QColor("#ffffff")))
        pen = QPen(QColor("#b8c0cc"))
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setZValue(25)
        self.setToolTip(tip)
        self.anchor = (0.0, 0.0, 0)       # (x_mm, y_mm, rang) — cf. _layout_plate_buttons
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptHoverEvents(True)
        txt = QGraphicsSimpleTextItem(glyph, self)
        f = QFont()
        f.setPointSize(10)
        txt.setFont(f)
        txt.setBrush(QBrush(QColor(color)))
        r = txt.boundingRect()
        txt.setPos(-r.width() / 2.0, -r.height() / 2.0)

    def hoverEnterEvent(self, event):
        self.setBrush(QBrush(QColor("#eef3ff")))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setBrush(QBrush(QColor("#ffffff")))
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.contains(event.pos()):
            self._cb()
        event.accept()


class PcbCanvas(QGraphicsView):
    """Aperçu du PCB et des parcours. Même API que la version matplotlib."""

    def __init__(self):
        self._scene = QGraphicsScene()
        super().__init__(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor(_BG)))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        # Sans le focus clavier, ni Suppr ni Ctrl+Z n'arriveraient a l'editeur.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.scale(1, -1)                  # Y vers le haut, comme la machine
        self._fit_rect: QRectF | None = None
        self._bed_rect: QRectF | None = None
        self._legend: list = []            # [(couleur, libellé)] en surimpression
        self._grid_step = 1.0
        # Tant que l'utilisateur n'a pas zoomé lui-même, la vue se recadre à
        # chaque redimensionnement. Sinon un rendu fait avant que la fenêtre ait
        # sa taille définitive laisserait la carte minuscule dans un coin.
        self._auto_fit = True
        # --- edition (branchee par DrawingEditor) ---
        self.editor = None
        self._mirror, self._cx, self._ox, self._oy = False, 0.0, 0.0, 0.0
        # Vue multi-plateaux : chaque plateau est decale de _plate_pitch en X.
        self._plate_pitch = 0.0
        self._nplates = 1
        self._plate_buttons = []
        self._rebuilding = False
        self._pan_from = None
        self._scene.selectionChanged.connect(self._on_selection)

    # ---- repere : ecran <-> modele ------------------------------------------ #
    def plate_at(self, pt) -> int:
        """Plateau sous ce point de la scene.

        Les plateaux sont poses cote a cote : la colonne dit lequel on vise.
        """
        if self._plate_pitch <= 0:
            return 0
        i = int((pt.x() - self._ox) // self._plate_pitch)
        return max(0, min(i, self._nplates - 1))

    def to_model(self, pt, plate: int | None = None) -> tuple:
        """Point de la SCENE vers les coordonnees du MODELE.

        La scene porte le miroir (face arriere), l'offset plateau et, en vue
        multi-plateaux, le decalage du plateau vise. Les objets dessines, eux,
        vivent dans le repere de LEUR plateau : sans ces inversions, une forme
        posee sur le plateau 3 arriverait a 800 mm de son origine.
        """
        i = self.plate_at(pt) if plate is None else plate
        x = pt.x() - self._ox - i * self._plate_pitch
        y = pt.y() - self._oy
        if self._mirror:
            x = 2 * self._cx - x
        return (x, y)

    def from_model(self, x: float, y: float, plate: int = 0) -> tuple:
        """Point du MODELE vers les coordonnees de la SCENE (inverse de to_model)."""
        if self._mirror:
            x = 2 * self._cx - x
        return (x + self._ox + plate * self._plate_pitch, y + self._oy)

    def mirrored(self) -> bool:
        return self._mirror

    def grid_step(self) -> float:
        return self._grid_step

    def set_edit_cursor(self, tool: str) -> None:
        from .editor import TOOL_SELECT
        if tool == TOOL_SELECT:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def _on_selection(self) -> None:
        if self.editor is not None and not self._rebuilding:
            self.editor.note_selection()
            self.editor.update_handles()

    # ---- navigation ---------------------------------------------------------- #
    def wheelEvent(self, event):
        """Zoom à la molette, centré sur le curseur."""
        if event.angleDelta().y() == 0:
            return
        self._auto_fit = False
        self.scale(*((1.25, 1.25) if event.angleDelta().y() > 0 else (0.8, 0.8)))
        self._layout_plate_buttons()
        self.viewport().update()           # la grille dépend de l'échelle

    def mousePressEvent(self, event):
        self._auto_fit = False             # déplacement à la main : on n'y touche plus
        if event.button() == Qt.MouseButton.MiddleButton:
            # Le bouton du milieu déplace TOUJOURS la vue : le bouton gauche est
            # pris par la sélection et le tracé dès qu'un outil est actif.
            self._pan_from = event.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        pos = self.mapToScene(event.position().toPoint())
        from .editor import TOOL_SELECT
        if (self.editor is not None and self._nplates > 1
                and self.editor.tool == TOOL_SELECT
                and event.button() == Qt.MouseButton.LeftButton
                and not self.itemAt(event.position().toPoint())):
            # Clic dans le vide d'un autre plateau : on s'y installe, comme
            # dans un trancheur 3D.
            cible = self.plate_at(pos)
            if cible != int(getattr(self.editor._get_session(), "active_plate", 0)):
                self.editor.set_plate(cible)
                event.accept()
                return
        if self.editor is not None and self.editor.mouse_press(pos, event.button()):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_from is not None:
            d = event.position().toPoint() - self._pan_from
            self._pan_from = event.position().toPoint()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - d.y())
            event.accept()
            return
        if self.editor is not None and self.editor.mouse_move(
                self.mapToScene(event.position().toPoint())):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pan_from is not None and event.button() == Qt.MouseButton.MiddleButton:
            self._pan_from = None
            self.set_edit_cursor(self.editor.tool if self.editor else "selection")
            event.accept()
            return
        if self.editor is not None and self.editor.mouse_release(
                self.mapToScene(event.position().toPoint()), event.button()):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        if self.editor is not None and self.editor.context_menu(
                self.mapToScene(event.pos()), event.globalPos()):
            event.accept()
            return
        super().contextMenuEvent(event)

    def mouseDoubleClickEvent(self, event):
        from .editor import TOOL_POLYLINE
        if self.editor is not None and self.editor.tool == TOOL_POLYLINE:
            self.editor.finish_polyline()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if self.editor is not None and self.editor.key(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._auto_fit:
            self._apply_fit()

    def _apply_fit(self) -> None:
        if self._fit_rect is not None and not self._fit_rect.isEmpty():
            self.fitInView(self._fit_rect, Qt.AspectRatioMode.KeepAspectRatio)
            self._layout_plate_buttons()
            self.viewport().update()

    def fit(self) -> None:
        """Cadre la vue sur le PCB positionné (ou sur le plateau à défaut)."""
        self._auto_fit = True
        self._apply_fit()

    def fit_bed(self) -> None:
        """Cadre la vue sur TOUT le plateau : on voit où la pièce est posée.

        Le cadrage par défaut serre la carte, ce qui met le pourtour du plateau
        hors champ — or c'est lui qui dit si la pièce tient et où elle est.
        """
        self._auto_fit = False
        if self._bed_rect is not None:
            r = QRectF(self._bed_rect)
            if self._nplates > 1:      # cadrer TOUS les plateaux
                r.setWidth(self._plate_pitch * (self._nplates - 1) + r.width())
            m = max(r.width(), r.height()) * 0.04
            self.fitInView(r.adjusted(-m, -m, m, m),
                           Qt.AspectRatioMode.KeepAspectRatio)
            self._layout_plate_buttons()
            self.viewport().update()

    def zoom_in(self) -> None:
        self._auto_fit = False
        self.scale(1.25, 1.25)
        self._layout_plate_buttons()
        self.viewport().update()

    def zoom_out(self) -> None:
        self._auto_fit = False
        self.scale(0.8, 0.8)
        self._layout_plate_buttons()
        self.viewport().update()

    def px_per_mm(self) -> float:
        return abs(self.transform().m11())

    def save_image(self, path: str, width: int = 2000) -> None:
        """Exporte l'aperçu en PNG (remplace le bouton d'enregistrement de matplotlib).

        ``QGraphicsScene.render()`` ignore la transformation de la VUE : sans le
        miroir ci-dessous, l'image sortait tête en bas alors que l'écran était
        juste. La légende est repeinte ensuite, en coordonnées image.
        """
        rect = self._fit_rect or self._scene.sceneRect()
        if rect.isEmpty():
            return
        h = max(1, int(width * rect.height() / rect.width()))
        img = QImage(width, h, QImage.Format.Format_ARGB32)
        img.fill(QColor(_BG))
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(0, h)             # Y vers le haut, comme à l'écran
        painter.scale(1, -1)
        self._scene.render(painter, QRectF(0, 0, width, h), rect)
        painter.resetTransform()
        self._paint_legend(painter, width, h)
        painter.end()
        img.save(path)

    # ---- fond et surimpression ------------------------------------------------ #
    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        """Grille millimétrique, au pas adapté au zoom.

        Remplace les axes gradués de matplotlib : dans un éditeur on veut une
        grille sur toute la surface, pas des graduations au bord.
        """
        painter.fillRect(rect, QColor(_BG))
        px_per_mm = self.px_per_mm()
        if px_per_mm <= 0:
            return
        step = 1.0
        while step * px_per_mm < 6:        # trop serré : on monte d'un cran
            step *= 10
        while step * px_per_mm > 60:       # trop lâche : on descend
            step /= 10
        self._grid_step = step

        def grid(pen, pas):
            if pas * px_per_mm < 4:        # illisible : inutile de le tracer
                return
            painter.setPen(pen)
            x = (int(rect.left() / pas) - 1) * pas
            while x < rect.right():
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                x += pas
            y = (int(rect.top() / pas) - 1) * pas
            while y < rect.bottom():
                painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
                y += pas

        grid(_pen(_GRID, 1.0), step)
        grid(_pen(_GRID_STRONG, 1.0), step * 10)

    def _paint_legend(self, painter: QPainter, vw: int, vh: int,
                      grid: bool = True) -> None:
        """Légende des catégories, en coordonnées ÉCRAN (ou image).

        Une légende qui grandirait avec le zoom serait inutilisable : elle est
        donc peinte hors de la transformation de la vue. Partagée avec
        l'export PNG, pour que l'image enregistrée porte la même.
        """
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        fm = QFontMetrics(font)

        if grid:
            step = self._grid_step
            txt = (f"grille {step:g} mm" if step >= 1
                   else f"grille {step * 1000:.0f} µm")
            painter.setPen(QPen(QColor("#8a8a8a")))
            painter.drawText(8, vh - 8, txt)

        if not self._legend:
            return
        pad, line_h, sw = 6, fm.height() + 2, 18
        w = max(fm.horizontalAdvance(l) for _c, l in self._legend) + sw + 3 * pad
        h = line_h * len(self._legend) + 2 * pad
        x0, y0 = vw - w - 8, 8
        painter.setBrush(QBrush(QColor(255, 255, 255, 220)))
        painter.setPen(QPen(QColor("#b0b0b0")))
        painter.drawRoundedRect(QRectF(x0, y0, w, h), 4, 4)
        y = y0 + pad + fm.ascent()
        for color, label in self._legend:
            painter.setPen(_pen(color, 2.0, cosmetic=False))
            painter.drawLine(QPointF(x0 + pad, y - fm.ascent() / 3.0),
                             QPointF(x0 + pad + sw, y - fm.ascent() / 3.0))
            painter.setPen(QPen(QColor("#222222")))
            painter.drawText(QPointF(x0 + pad + sw + pad, y), label)
            y += line_h

    def _paint_rulers(self, painter: QPainter, vw: int, vh: int) -> None:
        """Règles graduées en millimètres, à gauche et en bas.

        Remplacent les axes de matplotlib : contrairement à la grille seule,
        elles donnent la POSITION et pas seulement l'échelle. Graduées au pas de
        la grille, chiffrées tous les dix pas — donc jamais moins de 60 px entre
        deux nombres, quel que soit le zoom.
        """
        px = self.px_per_mm()
        if px <= 0:
            return
        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)
        fm = QFontMetrics(font)

        band = QColor("#f7f7f7")
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(band))
        painter.drawRect(0, 0, _RULER_W, vh)
        painter.drawRect(_RULER_W, vh - _RULER_H, vw - _RULER_W, _RULER_H)
        painter.setPen(QPen(QColor("#c8c8c8")))
        painter.drawLine(_RULER_W, 0, _RULER_W, vh)
        painter.drawLine(_RULER_W, vh - _RULER_H, vw, vh - _RULER_H)

        # Chiffrage au pas « 1-2-5 » le plus serré qui laisse 70 px entre deux
        # nombres : un pas fixe donnait trois valeurs sur tout l'écran.
        label = _nice_step(70, px)
        tick = label / 5.0 if label / 5.0 * px >= 8 else label
        top_left = self.mapToScene(0, 0)
        bot_right = self.mapToScene(vw, vh)
        x0, x1 = sorted((top_left.x(), bot_right.x()))
        y0, y1 = sorted((top_left.y(), bot_right.y()))

        def fmt(v):
            return f"{v:g}" if abs(v) >= 1 or v == 0 else f"{v:.2f}".rstrip("0")

        # --- bas : les X ---
        painter.setPen(QPen(QColor("#9a9a9a")))
        v = (int(x0 / tick) - 1) * tick
        while v < x1:
            dx = self.mapFromScene(QPointF(v, 0)).x()
            if dx > _RULER_W:
                big = abs(v / label - round(v / label)) < 1e-6
                painter.drawLine(int(dx), vh - _RULER_H,
                                 int(dx), vh - _RULER_H + (7 if big else 3))
                if big:
                    t = fmt(v)
                    painter.setPen(QPen(QColor("#555555")))
                    painter.drawText(int(dx) - fm.horizontalAdvance(t) // 2,
                                     vh - 4, t)
                    painter.setPen(QPen(QColor("#9a9a9a")))
            v += tick

        # --- gauche : les Y ---
        v = (int(y0 / tick) - 1) * tick
        while v < y1:
            dy = self.mapFromScene(QPointF(0, v)).y()
            if 0 < dy < vh - _RULER_H:
                big = abs(v / label - round(v / label)) < 1e-6
                painter.drawLine(_RULER_W - (7 if big else 3), int(dy),
                                 _RULER_W, int(dy))
                if big:
                    t = fmt(v)
                    painter.setPen(QPen(QColor("#555555")))
                    painter.drawText(_RULER_W - 9 - fm.horizontalAdvance(t),
                                     int(dy) + fm.ascent() // 2, t)
                    painter.setPen(QPen(QColor("#9a9a9a")))
            v += tick

        painter.setPen(QPen(QColor("#777777")))
        painter.drawText(3, vh - 5, "mm")

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """Règles, légende et repères, ancrés à la FENÊTRE."""
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        vw, vh = self.viewport().width(), self.viewport().height()
        self._paint_rulers(painter, vw, vh)
        self._paint_legend(painter, vw, vh, grid=False)
        painter.restore()

    # ---- construction de la scène --------------------------------------------- #
    def _text(self, x: float, y: float, text: str, color: str, size: int = 7):
        """Texte de repère, épargné par le miroir Y et par le zoom."""
        item = QGraphicsSimpleTextItem(text)
        item.setBrush(QBrush(QColor(color)))
        f = QFont()
        f.setPointSize(size)
        item.setFont(f)
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        item.setPos(x, y)
        item.setZValue(10)
        self._scene.addItem(item)
        return item

    def _add_path(self, path, *, fill=None, stroke=None, width=1.0, z=0, alpha=1.0):
        if path is None:
            return None
        item = self._scene.addPath(path)
        if fill is not None:
            c = QColor(fill)
            c.setAlphaF(alpha)
            item.setBrush(QBrush(c))
        else:
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        item.setPen(_pen(stroke, width) if stroke is not None
                    else QPen(Qt.PenStyle.NoPen))
        item.setZValue(z)
        return item

    def _draw_shapes(self, session, xf, vis) -> None:
        """Objets dessinés, chacun dans la couleur de son calque de travail.

        Le DESSIN est affiché, pas ses parcours : on veut voir la forme telle
        qu'on l'a posée, quel que soit le mode (trait ou remplissage) du calque,
        qui ne concerne que la gravure.

        Avec un éditeur branché, chaque forme devient un élément SÉLECTIONNABLE
        et déplaçable ; sans éditeur, un simple tracé (tests, export).
        """
        if not vis("dessin"):
            return
        proj = getattr(session, "project", None)
        toutes = list(getattr(proj, "shapes", ()) or ())
        if not toutes:
            return
        actif = int(getattr(session, "active_plate", 0))
        layers = {c.id: c for c in getattr(proj, "work_layers", ()) or ()}
        sel = self.editor.selected_ids if self.editor is not None else set()
        from shapely.geometry import LineString, MultiLineString, Polygon
        for shape in toutes:
            plate = int(getattr(shape, "plate", 0))
            if plate >= self._nplates:
                continue
            lay = layers.get(shape.layer)
            if lay is not None and not lay.enabled:
                continue
            color = lay.color if lay is not None else "#7a3fc4"
            try:
                geom = xf(shape.geometry())
                if plate:
                    from shapely.affinity import translate as _tr
                    geom = _tr(geom, plate * self._plate_pitch, 0.0)
            except Exception:  # noqa: BLE001
                continue        # une forme invalide ne doit pas vider l'aperçu
            if geom is None or geom.is_empty:
                continue
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.OddEvenFill)
            polys = lines = 0
            for g in getattr(geom, "geoms", [geom]):
                if isinstance(g, Polygon):
                    for ring in [g.exterior, *g.interiors]:
                        pts = [QPointF(x, y) for (x, y) in ring.coords]
                        if len(pts) < 3:
                            continue
                        path.addPolygon(QPolygonF(pts))
                        path.closeSubpath()
                        polys += 1
                elif isinstance(g, (LineString, MultiLineString)):
                    for line in getattr(g, "geoms", [g]):
                        pts = [QPointF(x, y) for (x, y) in line.coords]
                        if len(pts) < 2:
                            continue
                        path.moveTo(pts[0])
                        for pt in pts[1:]:
                            path.lineTo(pt)
                        lines += 1
            if not polys and not lines:
                continue
            # Un remplissage sur un tracé ouvert le refermerait visuellement :
            # on ne remplit que les formes réellement fermées.
            filled = bool(polys) and not lines
            if self.editor is None:
                self._add_path(path, fill=color if filled else None,
                               stroke=color, width=1.6, z=6,
                               alpha=0.25 if filled else 1.0)
                continue
            from .editor import ShapeItem
            # Seul le plateau ACTIF se manipule : sans cela, une poignee saisie
            # sur un plateau voisin travaillerait dans le mauvais repere.
            item = ShapeItem(shape, self.editor, path, color, filled,
                             interactive=(plate == actif))
            self._scene.addItem(item)
            if plate == actif and shape.id in sel:
                item.setSelected(True)

    def _plate_tools(self, index: int, dx: float, proj) -> None:
        """Icones d'un plateau, empilees le long de son bord gauche.

        Les gestes qu'on refait sans cesse quand on prepare une decoupe :
        centrer, ranger, vider.
        """
        ed = self.editor
        if ed is None:
            return
        actions = [
            ("⊕", "Centrer les pieces de ce plateau",
             lambda i=index: ed.center_plate(i)),
            ("≡", "Ranger les pieces de ce plateau",
             lambda i=index: ed.arrange_plate(i)),
            ("✕", "Vider ce plateau (supprime ses pieces)",
             lambda i=index: ed.clear_plate(i)),
        ]
        # En haut a DROITE, a l'interieur du plateau : a l'exterieur elles
        # chevauchaient le plateau voisin des qu'on en affiche plusieurs.
        for k, (glyph, tip, cb) in enumerate(actions):
            b = _PlateButton(glyph, tip, cb)
            # Ancrage en millimetres, ecart en PIXELS : la taille des icones est
            # fixe a l'ecran, donc leur espacement doit l'etre aussi. En
            # millimetres, elles se chevauchaient des qu'on dezoomait.
            b.anchor = (dx + proj.bed_width, proj.bed_height, k)
            self._scene.addItem(b)
            self._plate_buttons.append(b)
        self._layout_plate_buttons()

    def _layout_plate_buttons(self) -> None:
        """Replace les icones de plateau : ecart constant a l'ECRAN."""
        px = self.px_per_mm()
        if px <= 0:
            return
        for b in self._plate_buttons:
            x_mm, y_mm, k = b.anchor
            b.setPos(x_mm - (16 + k * 26) / px, y_mm - 16 / px)

    def render_session(self, session, toolpath_items=None, roles=None,
                       mirror=False, burn=False, burn_geom=None, burn_edges=False,
                       visible=None):
        """``visible`` : dict calque -> booleen. Absent = tout est affiche."""
        vis = (lambda key: True) if visible is None else (
            lambda key: visible.get(key, True))
        self._rebuilding = True            # la selection sera retablie apres
        if self.editor is not None:
            self.editor.scene_cleared()    # il ne doit plus toucher aux elements
        self._scene.clear()
        self._legend = []
        proj = session.project
        ox, oy = proj.origin
        cx = session.board_center_x() if session.board is not None else 0.0

        self._mirror, self._cx = bool(mirror), cx
        self._ox, self._oy = ox, oy

        def xf(geom):                       # miroir (bottom) puis offset plateau
            if mirror:
                geom = _scale(geom, xfact=-1, yfact=1, origin=(cx, 0))
            if ox or oy:
                geom = _translate(geom, ox, oy)
            return geom

        def xp(x, y):
            if mirror:
                x = 2 * cx - x
            return (x + ox, y + oy)

        # Plateaux : un par job, poses COTE A COTE comme dans un trancheur 3D.
        # On voit d'un coup d'oeil tout le travail, et sur quelle plaque chaque
        # piece ira — au lieu de naviguer a l'aveugle entre huit vues.
        self._nplates = (session.plate_count()
                         if hasattr(session, "plate_count") else 1)
        actif = int(getattr(session, "active_plate", 0))
        self._plate_pitch = proj.bed_width + max(30.0, proj.bed_width * 0.08)
        self._bed_rect = QRectF(0, 0, proj.bed_width, proj.bed_height)
        self._plate_buttons = []
        for i in range(self._nplates):
            dx = i * self._plate_pitch
            vif = (i == actif)
            bed = self._scene.addRect(
                QRectF(dx, 0, proj.bed_width, proj.bed_height))
            bed.setPen(_pen("#2b6cb0" if vif else "#9aa3ad", 2.2 if vif else 1.2,
                            style=(Qt.PenStyle.SolidLine if vif
                                   else Qt.PenStyle.DashLine)))
            bed.setBrush(QBrush(QColor(43, 108, 176, 16) if vif
                                else QColor(0, 0, 0, 8)))
            bed.setZValue(-1)
            etiquette = f"Plateau {i + 1}/{self._nplates}"
            if self._nplates == 1:
                etiquette = f"plateau {proj.bed_width:g} × {proj.bed_height:g} mm"
            self._text(dx + 1, proj.bed_height - 1, etiquette,
                       "#2b6cb0" if vif else "#8a8a8a", size=8 if vif else 7)
            # Les icones n'ont de sens qu'avec quelque chose dessus.
            if self._nplates > 1 or getattr(proj, "shapes", None):
                self._plate_tools(i, dx, proj)

        if (vis("carte") and session.board is not None
                and not session.board.is_empty):
            b = xf(session.board)
            self._add_path(_polys_to_path(getattr(b, "geoms", [b]), tol=0.02),
                           fill="#efe7c8", stroke="#7a6a2a", width=1.0, z=1)

        if burn:
            # Aperçu brûlure : zone gravée (déjà unionée dans le thread), en noir.
            if burn_geom is not None and not burn_geom.is_empty:
                merged = xf(burn_geom)
                self._add_path(_polys_to_path(getattr(merged, "geoms", [merged]),
                                              tol=0.02), fill="black", z=3)
            if burn_edges:
                # Bords du cuivre D'ORIGINE en rouge : on voit d'un coup d'œil si la
                # brûlure sépare bien deux cuivres (le noir doit être continu entre eux).
                for layer in session.layers:
                    if not layer.config.enabled:
                        continue
                    if roles is not None and layer.config.role not in roles:
                        continue
                    u = xf(layer.labeled.union)
                    self._add_path(_polys_to_path(getattr(u, "geoms", [u]), tol=0.02),
                                   stroke="#d81e1e", width=0.7, z=4)
        else:
            for layer in (session.layers if vis("cuivre") else ()):
                if not layer.config.enabled:
                    continue
                if roles is not None and layer.config.role not in roles:
                    continue
                color = _ROLE_COLOR.get(layer.config.role, "#333333")
                # transform + simplify sur TOUTE l'union en une fois, puis 1 chemin
                u = xf(layer.labeled.union).simplify(0.03, preserve_topology=False)
                self._add_path(_polys_to_path(getattr(u, "geoms", [u]), tol=0),
                               fill=color, z=2, alpha=0.55)
            # couches SOURCE optionnelles : on ne les calcule que si demandees
            for key, getter in (("serigraphie", session.silk_geometry),
                                ("pads", session.pads_for)):
                if not vis(key):
                    continue
                try:
                    geom = getter(session.active_side)
                except Exception:  # noqa: BLE001
                    continue
                if geom is None or geom.is_empty:
                    continue
                g = xf(geom)
                self._add_path(_polys_to_path(getattr(g, "geoms", [g]), tol=0.02),
                               fill=_SRC_COLOR[key], z=3, alpha=0.8)
            if session.holes and vis("hole"):       # tous les trous en UN chemin
                path = QPainterPath()
                for h in session.holes:
                    hx, hy = xp(h.x, h.y)
                    r = h.diameter / 2.0
                    path.addEllipse(QRectF(hx - r, hy - r, h.diameter, h.diameter))
                self._add_path(path, fill="white", stroke="#333333", width=0.3, z=4)
            self._draw_shapes(session, xf, vis)
            if toolpath_items:
                # couleur par catégorie (contour = tour du footprint, fill = remplissage…)
                groups: dict = {}
                for item in toolpath_items:
                    kind = item[3] if len(item) > 3 else "contour"
                    groups.setdefault(kind, []).append(item[0])
                for kind, segs in groups.items():
                    if not vis(kind):
                        continue
                    path = QPainterPath()
                    for coords in segs:
                        if len(coords) < 2:
                            continue
                        pts = [QPointF(*xp(x, y)) for (x, y) in coords]
                        path.moveTo(pts[0])
                        for pt in pts[1:]:
                            path.lineTo(pt)
                    self._add_path(path, stroke=_KIND_COLOR.get(kind, "#d81e1e"),
                                   width=_KIND_WIDTH.get(kind, 0.6), z=5)
                self._legend = [(_KIND_COLOR[k], lbl)
                                for k, lbl in _KIND_LABEL.items()
                                if k in groups and vis(k)]

        # vue ajustée au PCB positionné (molette pour voir tout le plateau)
        if session.board is not None and not session.board.is_empty:
            minx, miny, maxx, maxy = xf(session.board).bounds
            m = max(maxx - minx, maxy - miny) * 0.08 + 2
            self._fit_rect = QRectF(minx - m, miny - m,
                                    (maxx - minx) + 2 * m, (maxy - miny) + 2 * m)
        else:
            # « Ajuster » cadre le plateau de TRAVAIL ; « Plateau » les montre
            # tous (fit_bed). Deux gestes distincts, deux besoins distincts.
            self._fit_rect = QRectF(actif * self._plate_pitch, 0,
                                    proj.bed_width, proj.bed_height)
        self._scene.setSceneRect(
            self._scene.itemsBoundingRect().united(self._fit_rect)
            .adjusted(-20, -20, 20, 20))
        self._rebuilding = False
        if self.editor is not None:
            self.editor.update_handles()
        self.fit()


def make_nav_toolbar(canvas: PcbCanvas, parent=None) -> QToolBar:
    """Barre de navigation du canevas (remplace ``NavigationToolbar2QT``)."""
    tb = QToolBar("Vue", parent)
    tb.addAction("Ajuster", canvas.fit)
    a = tb.addAction("Plateau", canvas.fit_bed)
    a.setToolTip("Cadre toute la surface de la machine : on voit où la pièce "
                 "est posée et si elle tient.")
    tb.addAction("Zoom +", canvas.zoom_in)
    tb.addAction("Zoom −", canvas.zoom_out)

    def save():
        f, _ = QFileDialog.getSaveFileName(parent, "Enregistrer l'aperçu", "",
                                           "Image PNG (*.png)")
        if f:
            canvas.save_image(f)

    tb.addAction("Image…", save)
    return tb


def add_nav_hint(tb: QToolBar) -> None:
    """Rappel de navigation, ajoute EN FIN de barre.

    Au milieu, il repoussait les boutons suivants dans le menu debordant.
    """
    tb.addSeparator()
    hint = QLabel("  molette : zoom · milieu : déplacer  ", tb)
    hint.setStyleSheet("color:#777;")
    tb.addWidget(hint)
