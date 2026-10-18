"""Éditeur vectoriel : poser, sélectionner et transformer des objets.

Posé sur la scène Qt du canevas (étape 1) et sur le modèle paramétrique
(étape 2). Voir ``documentation/dev/editeur-vectoriel.md``.

Trois choix structurants :

1. **L'annulation passe par des instantanés.** Chaque commande mémorise la
   liste complète des objets avant et après. C'est grossier en théorie, exact
   en pratique : quelques dizaines de petites dataclasses pèsent une misère, et
   aucune transformation ne peut échapper au suivi. Un système de deltas fins
   serait plus élégant et beaucoup plus facile à trouer.
2. **Les objets vivent en coordonnées MODÈLE**, pas écran. La scène applique le
   miroir (face arrière) et l'offset plateau ; l'éditeur passe donc par
   ``to_model``/``from_model`` à chaque création et chaque déplacement. Sans
   cela, dessiner sur la face arrière poserait les formes en miroir.
3. **Les poignées sont des éléments de scène à taille écran fixe**
   (``ItemIgnoresTransformations``) : elles restent saisissables à tout zoom.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QKeySequence, QPainterPath, QPen,
                           QUndoCommand, QUndoStack)
from PySide6.QtWidgets import (QGraphicsEllipseItem, QGraphicsItem,
                               QGraphicsPathItem, QGraphicsRectItem, QInputDialog)

from .. import drawing as dr

# Outils
TOOL_SELECT = "selection"
TOOL_RECT = "rect"
TOOL_ELLIPSE = "ellipse"
TOOL_POLYLINE = "polyline"
TOOL_TEXT = "texte"

_HANDLE = 8                    # côté d'une poignée, en pixels écran
_SEL_COLOR = "#0a84ff"


# --------------------------------------------------------------------------- #
#  Annulation
# --------------------------------------------------------------------------- #
class _Snapshot(QUndoCommand):
    """Remplace la liste des objets. Voir le choix 1 en tête de module."""

    def __init__(self, editor, before: list, after: list, text: str):
        super().__init__(text)
        self._editor = editor
        self._before = before
        self._after = after
        self._first = True

    def redo(self):
        if self._first:            # l'état est déjà en place à la 1re exécution
            self._first = False
            self._editor._refresh()
            return
        self._editor._restore(self._after)

    def undo(self):
        self._editor._restore(self._before)


# --------------------------------------------------------------------------- #
#  Éléments de scène
# --------------------------------------------------------------------------- #
class ShapeItem(QGraphicsPathItem):
    """Un objet dessiné, sélectionnable et déplaçable."""

    def __init__(self, shape: dr.Shape, editor: "DrawingEditor",
                 path: QPainterPath, color: str, filled: bool):
        super().__init__(path)
        # ATTENTION : surtout pas ``self.shape`` — ``shape()`` est une methode
        # VIRTUELLE de QGraphicsItem. L'ecraser par une dataclass fait renvoyer
        # n'importe quoi a Qt, qui attend un QPainterPath : violation d'acces
        # immediate, sans exception Python.
        self.model = shape
        self._editor = editor
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        pen = QPen(QColor(color))
        pen.setWidthF(1.6)
        pen.setCosmetic(True)
        self.setPen(pen)
        if filled:
            c = QColor(color)
            c.setAlphaF(0.25)
            self.setBrush(QBrush(c))
        self.setZValue(6)
        self._press_pos = None

    def mousePressEvent(self, event):
        self._press_pos = self.pos()
        self._editor._begin()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._press_pos is None:
            return
        delta = self.pos() - self._press_pos
        self._press_pos = None
        if abs(delta.x()) < 1e-9 and abs(delta.y()) < 1e-9:
            self._editor._abort()
            return
        # Qt a bougé l'ÉLÉMENT ; c'est le MODÈLE qui fait foi, on y reporte le
        # déplacement puis on remet l'élément à zéro (il sera reconstruit).
        for item in self.scene().selectedItems():
            if isinstance(item, ShapeItem):
                dx, dy = self._editor.delta_to_model(delta.x(), delta.y())
                dr.translate_shape(item.model, dx, dy)
                item.setPos(0, 0)
        self._editor._commit("Déplacer")


class _Handle(QGraphicsRectItem):
    """Poignée de taille fixe à l'écran : coin de redimensionnement ou rotation."""

    def __init__(self, editor: "DrawingEditor", role: str):
        super().__init__(QRectF(-_HANDLE / 2, -_HANDLE / 2, _HANDLE, _HANDLE))
        self._editor = editor
        self.role = role
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setZValue(20)
        rot = role == "rot"
        self.setBrush(QBrush(QColor("#ffffff" if not rot else "#ffe08a")))
        pen = QPen(QColor(_SEL_COLOR))
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setCursor(Qt.CursorShape.SizeAllCursor if rot
                       else Qt.CursorShape.SizeFDiagCursor)
        self.setAcceptHoverEvents(True)
        self._active = False

    def mousePressEvent(self, event):
        self._active = True
        self._editor._begin()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._active:
            self._editor._drag_handle(self.role, event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._active:
            self._active = False
            self._editor._commit("Redimensionner" if self.role != "rot"
                                 else "Tourner")
        event.accept()


# --------------------------------------------------------------------------- #
#  Contrôleur
# --------------------------------------------------------------------------- #
class DrawingEditor(QObject):
    """Outils de dessin greffés sur le canevas."""

    changed = Signal()             # le modèle a bougé (réaffichage + recalcul)
    tool_changed = Signal(str)
    selection_changed = Signal(int)

    def __init__(self, canvas, get_session):
        super().__init__(canvas)
        self.canvas = canvas
        self._get_session = get_session
        self.tool = TOOL_SELECT
        self.snap = False
        self.undo = QUndoStack(self)
        self._handles: list = []
        self._preview = None
        self._start = None
        self._poly: list = []
        self._before: list | None = None
        self._box0 = None          # emprise du modele au debut du geste
        self._pending = False      # un reaffichage est deja programme
        canvas.editor = self

    # ---- accès au modèle ----------------------------------------------------- #
    @property
    def project(self):
        return self._get_session().project

    def layers(self) -> list:
        s = self._get_session()
        if not s.project.work_layers:
            s.project.work_layers = dr.default_layers()
        return s.project.work_layers

    def layer_by_id(self, layer_id: str):
        return next((c for c in self.layers() if c.id == layer_id), None)

    def current_layer(self):
        lays = self.layers()
        return next((c for c in lays if c.id == getattr(self, "_layer_id", None)),
                    lays[0])

    def set_current_layer(self, layer_id: str) -> None:
        self._layer_id = layer_id

    # ---- coordonnées --------------------------------------------------------- #
    def to_model(self, pt: QPointF) -> tuple:
        return self.canvas.to_model(pt)

    def delta_to_model(self, dx: float, dy: float) -> tuple:
        """Un déplacement écran vers le modèle : seul le miroir compte ici."""
        return (-dx if self.canvas.mirrored() else dx, dy)

    def _snapped(self, pt: QPointF) -> QPointF:
        """Cale le point : d'abord sur un REPÈRE de la carte, sinon sur la grille.

        Les repères (coins et centre de la carte, centres des pads) priment :
        ils ne tombent presque jamais sur la grille, et ce sont eux qu'on vise
        quand on annote un circuit.
        """
        if not self.snap:
            return pt
        near = self._nearest_target(pt)
        if near is not None:
            return near
        step = max(self.canvas.grid_step(), 1e-6)
        return QPointF(round(pt.x() / step) * step, round(pt.y() / step) * step)

    def _nearest_target(self, pt: QPointF):
        """Repère le plus proche, s'il est à moins de 10 pixels à l'écran."""
        pts = self._targets()
        if not pts:
            return None
        px = max(self.canvas.px_per_mm(), 1e-6)
        seuil = 10.0 / px                      # 10 px, quel que soit le zoom
        best, dmin = None, seuil
        for (x, y) in pts:
            d = math.hypot(x - pt.x(), y - pt.y())
            if d < dmin:
                best, dmin = (x, y), d
        return QPointF(*best) if best else None

    def _targets(self) -> list:
        """Repères d'accrochage, en coordonnées de SCÈNE, mis en cache.

        Recalculer les centres de pads à chaque mouvement de souris serait
        intenable sur une carte chargée.
        """
        s = self._get_session()
        board = getattr(s, "board", None)
        key = (id(s), getattr(s, "active_side", ""), self.canvas.mirrored(),
               tuple(getattr(s.project, "origin", (0, 0))),
               None if board is None else board.bounds)
        if getattr(self, "_targets_key", None) == key:
            return self._targets_cache
        pts = []
        if board is not None and not board.is_empty:
            minx, miny, maxx, maxy = board.bounds
            pts += [(minx, miny), (maxx, miny), (minx, maxy), (maxx, maxy),
                    ((minx + maxx) / 2, (miny + maxy) / 2)]
        try:
            pads = s.pads_for(s.active_side)
        except Exception:  # noqa: BLE001
            pads = None
        if pads is not None and not pads.is_empty:
            geoms = list(getattr(pads, "geoms", [pads]))
            if len(geoms) <= 8000:             # au-delà, l'accrochage n'aide plus
                for g in geoms:
                    c = g.centroid
                    pts.append((c.x, c.y))
        self._targets_key = key
        self._targets_cache = [self.canvas.from_model(x, y) for (x, y) in pts]
        return self._targets_cache

    # ---- outils -------------------------------------------------------------- #
    def set_tool(self, tool: str) -> None:
        self._cancel_preview()
        self.tool = tool
        self.canvas.set_edit_cursor(tool)
        self.tool_changed.emit(tool)

    # ---- événements souris (délégués par le canevas) ------------------------- #
    def mouse_press(self, scene_pos: QPointF, button) -> bool:
        """Renvoie True si l'éditeur a consommé l'événement."""
        if button != Qt.MouseButton.LeftButton or self.tool == TOOL_SELECT:
            return False
        pt = self._snapped(scene_pos)
        if self.tool == TOOL_TEXT:
            self._place_text(pt)
            return True
        if self.tool == TOOL_POLYLINE:
            self._poly.append(pt)
            self._update_preview(pt)
            return True
        self._start = pt
        return True

    def mouse_move(self, scene_pos: QPointF) -> bool:
        if self.tool == TOOL_SELECT:
            return False
        if self._start is None and not self._poly:
            return False
        self._update_preview(self._snapped(scene_pos))
        return True

    def mouse_release(self, scene_pos: QPointF, button) -> bool:
        if button != Qt.MouseButton.LeftButton or self.tool in (
                TOOL_SELECT, TOOL_TEXT, TOOL_POLYLINE):
            return False
        if self._start is None:
            return False
        a, b = self._start, self._snapped(scene_pos)
        self._start = None
        self._cancel_preview()
        if abs(a.x() - b.x()) < 1e-6 or abs(a.y() - b.y()) < 1e-6:
            return True                       # clic sans glisser : rien à créer
        self._begin()
        x0, y0 = self.to_model(QPointF(min(a.x(), b.x()), min(a.y(), b.y())))
        x1, y1 = self.to_model(QPointF(max(a.x(), b.x()), max(a.y(), b.y())))
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        if self.tool == TOOL_RECT:
            s = dr.Shape(dr.KIND_RECT,
                         {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0})
        else:
            s = dr.Shape(dr.KIND_ELLIPSE,
                         {"cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                          "rx": (x1 - x0) / 2, "ry": (y1 - y0) / 2})
        self._add(s, "Rectangle" if self.tool == TOOL_RECT else "Ellipse")
        return True

    def finish_polyline(self, closed: bool = False) -> None:
        """Termine la polyligne en cours (double-clic, Entrée ou Échap)."""
        pts = list(self._poly)
        self._poly = []
        self._cancel_preview()
        if len(pts) < 2:
            return
        self._begin()
        model = [list(self.to_model(p)) for p in pts]
        self._add(dr.Shape(dr.KIND_POLYLINE, {"points": model, "closed": closed}),
                  "Polyligne")

    def _place_text(self, pt: QPointF) -> None:
        text, ok = QInputDialog.getText(self.canvas, "Texte", "Texte à graver :")
        if not ok or not text.strip():
            return
        h, ok = QInputDialog.getDouble(self.canvas, "Texte", "Hauteur (mm) :",
                                       4.0, 0.3, 200.0, 1)
        if not ok:
            return
        self._begin()
        x, y = self.to_model(pt)
        self._add(dr.Shape(dr.KIND_TEXT,
                           {"x": x, "y": y, "text": text, "height": h}), "Texte")

    # ---- aperçu pendant le tracé --------------------------------------------- #
    def _update_preview(self, pt: QPointF) -> None:
        path = QPainterPath()
        if self.tool in (TOOL_RECT, TOOL_ELLIPSE) and self._start is not None:
            r = QRectF(self._start, pt).normalized()
            (path.addRect if self.tool == TOOL_RECT else path.addEllipse)(r)
        elif self.tool == TOOL_POLYLINE and self._poly:
            path.moveTo(self._poly[0])
            for p in self._poly[1:]:
                path.lineTo(p)
            path.lineTo(pt)
        else:
            return
        if self._preview is None:
            self._preview = self.canvas.scene().addPath(path)
            pen = QPen(QColor(_SEL_COLOR))
            pen.setCosmetic(True)
            pen.setStyle(Qt.PenStyle.DashLine)
            self._preview.setPen(pen)
            self._preview.setZValue(30)
        else:
            self._preview.setPath(path)

    def _cancel_preview(self) -> None:
        if self._preview is not None and self._preview.scene() is not None:
            self.canvas.scene().removeItem(self._preview)
        self._preview = None
        self._start = None
        self._poly = []

    # ---- commandes ----------------------------------------------------------- #
    def _begin(self) -> None:
        """Mémorise l'état AVANT une modification (cf. choix 1)."""
        self._before = [s.to_dict() for s in self.project.shapes]

    def _abort(self) -> None:
        self._before = None

    def _commit(self, text: str) -> None:
        if self._before is None:
            return
        after = [s.to_dict() for s in self.project.shapes]
        if after == self._before:
            self._before = None
            return
        self.undo.push(_Snapshot(self, self._before, after, text))
        self._before = None

    def _add(self, shape: dr.Shape, text: str) -> None:
        shape.layer = self.current_layer().id
        shape.id = dr.new_id("o", self.project.shapes)
        self.project.shapes.append(shape)
        self._commit(text)

    def import_svg(self, path, width_mm: float | None = None) -> int:
        """Importe un SVG sur le calque courant, centré sur la carte.

        Renvoie le nombre d'objets posés. Tout l'import tient dans UNE commande
        d'annulation : un logo de cinquante chemins ne doit pas demander
        cinquante Ctrl+Z.
        """
        from .. import svgimport as svg
        geoms = svg.read_svg(path)
        if not geoms:
            return 0
        from shapely.ops import unary_union
        u = unary_union(geoms)
        minx, miny, maxx, maxy = u.bounds
        scale = (width_mm / (maxx - minx)) if (width_mm and maxx > minx) else 1.0
        w, h = (maxx - minx) * scale, (maxy - miny) * scale
        cx, cy = self._board_center()
        shapes = svg.shapes_from_svg(
            path, layer_id=self.current_layer().id,
            existing=self.project.shapes, target_width=width_mm,
            at=(cx - w / 2.0, cy - h / 2.0))
        if not shapes:
            return 0
        self._begin()
        self.project.shapes.extend(shapes)
        self._commit(f"Importer {len(shapes)} objet(s)")
        return len(shapes)

    def _board_center(self) -> tuple:
        """Centre de la carte, ou du plateau en l'absence de carte."""
        s = self._get_session()
        board = getattr(s, "board", None)
        if board is not None and not board.is_empty:
            minx, miny, maxx, maxy = board.bounds
            return ((minx + maxx) / 2.0, (miny + maxy) / 2.0)
        p = s.project
        return (p.bed_width / 2.0, p.bed_height / 2.0)

    def _restore(self, dicts: list) -> None:
        self.project.shapes[:] = [dr.Shape.from_dict(d) for d in dicts]
        self._refresh()

    def _refresh(self) -> None:
        """Demande un réaffichage, au PROCHAIN tour de boucle.

        Jamais tout de suite : un rendu reconstruit la scène
        (``scene.clear()``), ce qui détruit côté C++ l'élément dont on est en
        train de traiter l'événement souris. Qt y revient ensuite — et le
        process meurt sur une violation d'accès. Différer est ici une question
        de survie, pas de confort.
        """
        if self._pending:
            return
        self._pending = True
        QTimer.singleShot(0, self._emit_changed)

    def _emit_changed(self) -> None:
        self._pending = False
        self.changed.emit()

    def scene_cleared(self) -> None:
        """La scène va être vidée : lâcher les éléments qu'elle possède.

        Les poignées et l'aperçu lui appartiennent ; garder leurs enveloppes
        Python après destruction, c'est planter au premier appel.
        """
        self._handles = []
        self._preview = None

    def delete_selection(self) -> None:
        ids = {i.model.id for i in self.canvas.scene().selectedItems()
               if isinstance(i, ShapeItem)}
        if not ids:
            return
        self._begin()
        self.project.shapes[:] = [s for s in self.project.shapes if s.id not in ids]
        self._commit("Supprimer")

    def duplicate_selection(self, dx: float = 2.0, dy: float = -2.0) -> None:
        """Copie la sélection, légèrement décalée pour rester saisissable."""
        sel = self.selected_shapes()
        if not sel:
            return
        self._begin()
        copies = []
        for src in sel:
            copie = dr.Shape.from_dict(src.to_dict())
            copie.id = dr.new_id("o", self.project.shapes + copies)
            dr.translate_shape(copie, dx, dy)
            copies.append(copie)
        self.project.shapes.extend(copies)
        # La SÉLECTION suit la copie : on enchaîne naturellement sur un
        # déplacement, comme dans tout éditeur.
        self._sel_ids = {c.id for c in copies}
        self._commit("Dupliquer")

    def align_selection(self, how: str) -> None:
        """Aligne la sélection : ``gauche``, ``droite``, ``haut``, ``bas``,
        ``centre-h``, ``centre-v``.

        Sans effet à moins de deux objets : aligner un objet sur lui-même n'a
        pas de sens et encombrerait l'historique.
        """
        sel = [s for s in self.selected_shapes() if s.bounds() is not None]
        if len(sel) < 2:
            return
        boxes = {id(s): s.bounds() for s in sel}
        xs = [b[0] for b in boxes.values()]
        ys = [b[1] for b in boxes.values()]
        x1s = [b[2] for b in boxes.values()]
        y1s = [b[3] for b in boxes.values()]
        ref = {"gauche": min(xs), "droite": max(x1s),
               "bas": min(ys), "haut": max(y1s),
               "centre-h": (min(xs) + max(x1s)) / 2.0,
               "centre-v": (min(ys) + max(y1s)) / 2.0}.get(how)
        if ref is None:
            return
        self._begin()
        for s in sel:
            x0, y0, x1, y1 = boxes[id(s)]
            if how == "gauche":
                dr.translate_shape(s, ref - x0, 0)
            elif how == "droite":
                dr.translate_shape(s, ref - x1, 0)
            elif how == "bas":
                dr.translate_shape(s, 0, ref - y0)
            elif how == "haut":
                dr.translate_shape(s, 0, ref - y1)
            elif how == "centre-h":
                dr.translate_shape(s, ref - (x0 + x1) / 2.0, 0)
            else:
                dr.translate_shape(s, 0, ref - (y0 + y1) / 2.0)
        self._commit(f"Aligner ({how})")

    def center_selection_on_board(self) -> None:
        """Centre la sélection sur la carte (ou le plateau s'il n'y a pas de carte)."""
        sel = [s for s in self.selected_shapes() if s.bounds() is not None]
        if not sel:
            return
        bs = [s.bounds() for s in sel]
        minx, miny = min(b[0] for b in bs), min(b[1] for b in bs)
        maxx, maxy = max(b[2] for b in bs), max(b[3] for b in bs)
        cx, cy = self._board_center()
        self._begin()
        for s in sel:
            dr.translate_shape(s, cx - (minx + maxx) / 2.0, cy - (miny + maxy) / 2.0)
        self._commit("Centrer")

    def assign_layer(self, layer_id: str) -> None:
        """Affecte la sélection à un calque — le geste central du modèle LightBurn."""
        items = [i for i in self.canvas.scene().selectedItems()
                 if isinstance(i, ShapeItem)]
        if not items:
            return
        self._begin()
        ids = {i.model.id for i in items}
        for s in self.project.shapes:
            if s.id in ids:
                s.layer = layer_id
        self._commit("Changer de calque")

    def selected_shapes(self) -> list:
        return [i.model for i in self.canvas.scene().selectedItems()
                if isinstance(i, ShapeItem)]

    # ---- sélection ------------------------------------------------------------ #
    @property
    def selected_ids(self) -> set:
        return getattr(self, "_sel_ids", set())

    def note_selection(self) -> None:
        """Mémorise la sélection par identifiant.

        La scène est reconstruite à chaque rendu : sans cela, toucher une
        poignée ferait perdre la sélection qu'on est en train de manipuler.
        """
        self._sel_ids = {i.model.id for i in self.canvas.scene().selectedItems()
                         if isinstance(i, ShapeItem)}

    def select_all(self) -> None:
        for i in self.canvas.scene().items():
            if isinstance(i, ShapeItem):
                i.setSelected(True)

    # ---- poignées ------------------------------------------------------------ #
    # Coins nommés en Y VERS LE HAUT (celui de la scène et de la machine) :
    # « b » = bas (y minimal), « t » = haut (y maximal). Ne pas se fier à
    # QRectF.top()/bottom(), qui supposent l'inverse.
    _OPPOSITE = {"bl": "tr", "tr": "bl", "br": "tl", "tl": "br"}

    def selection_rect(self):
        """Emprise de la sélection, en coordonnées de scène.

        Calculée sur la GÉOMÉTRIE du modèle, surtout pas sur
        ``sceneBoundingRect()`` : celui-ci inclut la demi-épaisseur du trait,
        ce qui faussait tous les redimensionnements.
        """
        bounds = []
        for s in self.selected_shapes():
            b = s.bounds()
            if b is not None:
                bounds.append(b)
        if not bounds:
            return None
        return {"minx": min(b[0] for b in bounds), "miny": min(b[1] for b in bounds),
                "maxx": max(b[2] for b in bounds), "maxy": max(b[3] for b in bounds)}

    def _corner(self, box: dict, name: str) -> QPointF:
        """Coin du modèle ramené en coordonnées de scène."""
        x = box["minx"] if name[1] == "l" else box["maxx"]
        y = box["miny"] if name[0] == "b" else box["maxy"]
        sx, sy = self.canvas.from_model(x, y)
        return QPointF(sx, sy)

    def update_handles(self) -> None:
        for h in self._handles:
            if h.scene() is not None:
                self.canvas.scene().removeItem(h)
        self._handles = []
        items = [i for i in self.canvas.scene().selectedItems()
                 if isinstance(i, ShapeItem)]
        self._sel_ids = {i.model.id for i in items}
        self.selection_changed.emit(len(items))
        box = self.selection_rect()
        if box is None:
            return
        self._box0 = box
        spots = {name: self._corner(box, name)
                 for name in ("bl", "br", "tl", "tr")}
        top = self._corner(box, "tl")
        other = self._corner(box, "tr")
        spots["rot"] = QPointF((top.x() + other.x()) / 2, top.y())
        for role, pos in spots.items():
            h = _Handle(self, role)
            h.setPos(pos)
            self.canvas.scene().addItem(h)
            self._handles.append(h)

    def _drag_handle(self, role: str, pos: QPointF) -> None:
        shapes = self.selected_shapes()
        box = getattr(self, "_box0", None)
        if not shapes or box is None:
            return
        if role == "rot":
            cx = (box["minx"] + box["maxx"]) / 2.0
            cy = (box["miny"] + box["maxy"]) / 2.0
            mx, my = self.to_model(pos)
            ang = math.degrees(math.atan2(my - cy, mx - cx))
            if self.snap:
                ang = round(ang / 15.0) * 15.0
            for s in shapes:
                s.rotation = ang
            self._refresh()
            return

        fixed = self._corner(box, self._OPPOSITE[role])
        moving = self._corner(box, role)
        dx0, dy0 = moving.x() - fixed.x(), moving.y() - fixed.y()
        if abs(dx0) < 1e-9 or abs(dy0) < 1e-9:
            return
        sx = (pos.x() - fixed.x()) / dx0
        sy = (pos.y() - fixed.y()) / dy0
        if abs(sx) < 1e-3 or abs(sy) < 1e-3:
            return                       # aplatir a zero rendrait l objet irrecuperable
        # Le miroir de la face arrière ne change PAS le rapport d'échelle : une
        # réflexion conserve les proportions. Seule l'origine doit être ramenée
        # dans le repère du modèle.
        ox, oy = self.to_model(fixed)
        for s in shapes:
            dr.scale_shape(s, sx, sy, ox, oy)
        self._box0 = self.selection_rect() or box
        self._refresh()

    # ---- clavier -------------------------------------------------------------- #
    def key(self, event) -> bool:
        k = event.key()
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selection()
            return True
        if k == Qt.Key.Key_Escape:
            if self._poly:
                self.finish_polyline()
            else:
                self._cancel_preview()
                self.set_tool(TOOL_SELECT)
            return True
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._poly:
            self.finish_polyline()
            return True
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all()
            return True
        if (k == Qt.Key.Key_D
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.duplicate_selection()
            return True
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undo.undo()
            return True
        if event.matches(QKeySequence.StandardKey.Redo):
            self.undo.redo()
            return True
        return False
