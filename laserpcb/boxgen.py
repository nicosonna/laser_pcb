"""Générateur de boîtes à tenons (dans l'esprit de boxes.py).

Modèle PUR : aucune dépendance à Qt, tout se teste sans interface.

**L'assemblage.** Deux panneaux perpendiculaires se joignent par un peigne :
l'un porte des tenons qui dépassent de l'épaisseur du voisin, l'autre des
encoches de même profondeur, aux mêmes positions. Le motif commence et finit
par un creux et il est symétrique — il reste donc identique lu à l'envers, ce
qui évite toute question d'orientation au montage.

Conséquence sur les cotes : un panneau est dessiné à sa taille NOMINALE, et ses
tenons dépassent en plus. Les dimensions extérieures annoncées sont donc
respectées une fois la boîte assemblée.

**Le jeu de coupe.** Le laser enlève de la matière : sans compensation, un tenon
de 4 mm sort à 3,9 mm et flotte dans son encoche. ``kerf`` élargit les tenons
et rétrécit les encoches d'un demi-trait chacun.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

# Modes de bord
FLAT = "plat"
FINGERS = "tenons"
NOTCHES = "encoches"


# --------------------------------------------------------------------------- #
#  Réglages laser déduits de l'épaisseur
# --------------------------------------------------------------------------- #
def cut_profile(thickness: float) -> dict:
    """Passes / puissance / vitesse pour traverser ce bois.

    Calé sur un point MESURÉ par l'utilisateur : 4 mm de contreplaqué passent
    en 5 passes à 80 %. Le reste est extrapolé linéairement — à vérifier sur
    une chute avant de lancer une grande découpe, le bois variant beaucoup d'un
    lot à l'autre.
    """
    t = max(0.1, float(thickness))
    passes = max(1, int(math.ceil(t * 1.25)))      # 4 mm -> 5 passes
    return {"passes": passes, "power": 80.0, "speed": 300.0}


def engrave_profile(thickness: float) -> dict:
    """Réglages de GRAVURE (décor) : on marque, on ne traverse pas."""
    return {"passes": 1, "power": 35.0, "speed": 1200.0}


# --------------------------------------------------------------------------- #
#  Pièces
# --------------------------------------------------------------------------- #
@dataclass
class Piece:
    """Un panneau : son contour, ses ouvertures, son décor."""
    name: str
    outline: Polygon
    holes: list = field(default_factory=list)     # list[Polygon] à découper
    decor: list = field(default_factory=list)     # géométries à GRAVER

    def bounds(self):
        return self.outline.bounds

    def size(self):
        b = self.outline.bounds
        return (b[2] - b[0], b[3] - b[1])


# --------------------------------------------------------------------------- #
#  Bords et panneaux
# --------------------------------------------------------------------------- #
def finger_count(length: float, finger: float) -> int:
    """Nombre de tenons sur un bord, au moins un."""
    return max(1, int((length / max(finger, 0.1) - 1) // 2))


def _profile(length: float, t: float, finger: float, mode: str,
             kerf: float = 0.0) -> list:
    """Points de (0,0) à (length,0). ``y`` positif = vers l'EXTÉRIEUR.

    ``kerf`` élargit les tenons et rétrécit les encoches d'un demi-trait, pour
    que l'assemblage soit juste une fois la matière enlevée.
    """
    if mode == FLAT or length <= 0:
        return [(0.0, 0.0), (length, 0.0)]
    n = finger_count(length, finger)
    gap = (length - n * finger) / (n + 1)
    depth = t if mode == FINGERS else -t
    # tenon elargi / encoche elargie : dans les deux cas on retire kerf/2 de
    # chaque cote de la MATIERE, donc le tenon doit etre dessine plus large.
    j = kerf / 2.0 if mode == FINGERS else -kerf / 2.0

    pts = [(0.0, 0.0)]
    x = 0.0
    for _ in range(n):
        x += gap
        a = max(0.0, x - j)
        pts += [(a, 0.0), (a, depth)]
        x += finger
        b = min(length, x + j)
        pts += [(b, depth), (b, 0.0)]
    pts.append((length, 0.0))
    return pts


_EDGES = (                      # (origine, direction, normale sortante)
    ((0.0, 0.0), (1.0, 0.0), (0.0, -1.0)),      # bas
    ((1.0, 0.0), (0.0, 1.0), (1.0, 0.0)),       # droite
    ((1.0, 1.0), (-1.0, 0.0), (0.0, 1.0)),      # haut
    ((0.0, 1.0), (0.0, -1.0), (-1.0, 0.0)),     # gauche
)


def panel(w: float, h: float, t: float, finger: float,
          edges=(FLAT, FLAT, FLAT, FLAT), kerf: float = 0.0) -> Polygon:
    """Panneau rectangulaire ``w × h`` dont les 4 bords portent un profil.

    ``edges`` = (bas, droite, haut, gauche).
    """
    pts: list = []
    for k, ((ox, oy), (dx, dy), (nx, ny)) in enumerate(_EDGES):
        length = w if dx else h
        prof = _profile(length, t, finger, edges[k], kerf)
        base = (ox * w, oy * h)
        for (px, py) in prof:
            pts.append((base[0] + dx * px + nx * py,
                        base[1] + dy * px + ny * py))
    # points consécutifs identiques aux coins
    nets = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - nets[-1][0]) > 1e-9 or abs(p[1] - nets[-1][1]) > 1e-9:
            nets.append(p)
    return Polygon(nets)


def edge_slots(length: float, t: float, finger: float, kerf: float = 0.0,
               along: str = "x", offset: float = 0.0,
               position: float = 0.0) -> list:
    """Mortaises traversantes correspondant aux tenons d'un bord.

    Sert aux étagères : leurs tenons passent à travers la face des côtés, pas
    par un bord. Les fentes sont donc percées DANS le panneau.
    """
    n = finger_count(length, finger)
    gap = (length - n * finger) / (n + 1)
    out = []
    x = 0.0
    for _ in range(n):
        x += gap
        a, b = x - kerf / 2.0, x + finger + kerf / 2.0
        if along == "x":
            out.append(Polygon([(offset + a, position), (offset + b, position),
                                (offset + b, position + t),
                                (offset + a, position + t)]))
        else:
            out.append(Polygon([(position, offset + a), (position + t, offset + a),
                                (position + t, offset + b),
                                (position, offset + b)]))
        x += finger
    return out


# --------------------------------------------------------------------------- #
#  Décor
# --------------------------------------------------------------------------- #
def pattern(kind: str, w: float, h: float, step: float = 10.0,
            margin: float = 8.0) -> list:
    """Motif géométrique à GRAVER (jamais à découper) dans un panneau."""
    x0, y0 = margin, margin
    x1, y1 = w - margin, h - margin
    if x1 <= x0 or y1 <= y0:
        return []
    step = max(1.0, step)
    lines: list = []
    if kind == "rayures":
        x = x0
        while x <= x1:
            lines.append(LineString([(x, y0), (x, y1)]))
            x += step
    elif kind == "croisillons":
        x = x0
        while x <= x1:
            lines.append(LineString([(x, y0), (x, y1)]))
            x += step
        y = y0
        while y <= y1:
            lines.append(LineString([(x0, y), (x1, y)]))
            y += step
    elif kind == "chevrons":
        d = step
        x = x0 - (y1 - y0)
        while x <= x1:
            lines.append(LineString([(max(x0, x), y0 + max(0.0, x0 - x)),
                                     (min(x1, x + (y1 - y0)),
                                      y0 + min(y1 - y0, x1 - x))]))
            x += d
    elif kind == "losanges":
        d = step
        x = x0 - (y1 - y0)
        while x <= x1 + (y1 - y0):
            lines.append(LineString([(max(x0, x), y0 + max(0.0, x0 - x)),
                                     (min(x1, x + (y1 - y0)),
                                      y0 + min(y1 - y0, x1 - x))]))
            lines.append(LineString([(max(x0, x), y1 - max(0.0, x0 - x)),
                                     (min(x1, x + (y1 - y0)),
                                      y1 - min(y1 - y0, x1 - x))]))
            x += d
    elif kind == "cadre":
        lines.append(LineString([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]))
    return [ln for ln in lines if ln.length > 1e-6]


def text_decor(text: str, height: float, w: float, h: float,
               where: str = "centre"):
    """Texte vectorisé, placé sur un panneau. Gravé, jamais découpé."""
    if not text.strip():
        return []
    from . import toolpath as tp
    g = tp.text_polygons(text, height, 0.0, 0.0)
    if g.is_empty:
        return []
    minx, miny, maxx, maxy = g.bounds
    tw, th = maxx - minx, maxy - miny
    if where == "haut":
        x, y = (w - tw) / 2.0, h - th - max(6.0, height * 0.6)
    elif where == "bas":
        x, y = (w - tw) / 2.0, max(6.0, height * 0.6)
    else:
        x, y = (w - tw) / 2.0, (h - th) / 2.0
    from shapely.affinity import translate
    return [translate(g, x - minx, y - miny)]


# --------------------------------------------------------------------------- #
#  Générateurs
# --------------------------------------------------------------------------- #
#  Convention de montage, valable pour tous :
#    - fond / dessus : nominal (X-2t) × (Y-2t), TENONS sur les quatre bords
#    - avant / arrière : (X-2t) × H, encoches en bas, tenons sur les côtés
#    - gauche / droite : (Y-2t) × H, encoches en bas et sur les côtés
#  Les tenons comblent l'épaisseur du voisin : les cotes EXTÉRIEURES annoncées
#  sont donc celles de la boîte une fois assemblée.
# --------------------------------------------------------------------------- #
def _walls(x, y, h, t, f, kerf, with_top: bool) -> list:
    """Les quatre parois et le fond (plus le dessus si la boîte est fermée)."""
    haut = NOTCHES if with_top else FLAT
    pieces = [
        Piece("fond", panel(x - 2 * t, y - 2 * t, t, f,
                            (FINGERS, FINGERS, FINGERS, FINGERS), kerf)),
        Piece("avant", panel(x - 2 * t, h, t, f,
                             (NOTCHES, FINGERS, haut, FINGERS), kerf)),
        Piece("arriere", panel(x - 2 * t, h, t, f,
                               (NOTCHES, FINGERS, haut, FINGERS), kerf)),
        Piece("gauche", panel(y - 2 * t, h, t, f,
                              (NOTCHES, NOTCHES, haut, NOTCHES), kerf)),
        Piece("droite", panel(y - 2 * t, h, t, f,
                              (NOTCHES, NOTCHES, haut, NOTCHES), kerf)),
    ]
    if with_top:
        pieces.append(Piece("dessus", panel(x - 2 * t, y - 2 * t, t, f,
                                            (FINGERS, FINGERS, FINGERS, FINGERS),
                                            kerf)))
    return pieces


def make_box(x=150.0, y=100.0, h=60.0, thickness=4.0, finger=None,
             kerf=0.1, closed=False, **_) -> list:
    """Boîte ou plateau, aux cotes EXTÉRIEURES ``x × y × h``."""
    t = float(thickness)
    f = float(finger or max(2.0 * t, 6.0))
    return _walls(x, y, h, t, f, kerf, with_top=bool(closed))


def make_tray(x=200.0, y=150.0, h=60.0, thickness=4.0, finger=None,
              kerf=0.1, cols=2, rows=2, **_) -> list:
    """Bac à compartiments : un plateau et ses séparateurs à mi-bois.

    Les séparateurs se croisent par des fentes à mi-hauteur : ils tiennent
    seuls, sans colle, et se retirent pour changer le découpage.
    """
    t = float(thickness)
    f = float(finger or max(2.0 * t, 6.0))
    cols, rows = max(1, int(cols)), max(1, int(rows))
    pieces = _walls(x, y, h, t, f, kerf, with_top=False)
    iw, iy = x - 2 * t, y - 2 * t
    hs = h - t                                   # hauteur utile d'un séparateur

    def fentes(longueur, n_autres, par_le_haut):
        """Encoches à mi-bois, pour croiser les séparateurs perpendiculaires."""
        out = []
        for k in range(1, n_autres + 1):
            pos = longueur * k / (n_autres + 1)
            a, b = pos - t / 2.0 + kerf / 2.0, pos + t / 2.0 - kerf / 2.0
            if par_le_haut:
                out.append(Polygon([(a, hs / 2.0), (b, hs / 2.0),
                                    (b, hs), (a, hs)]))
            else:
                out.append(Polygon([(a, 0.0), (b, 0.0),
                                    (b, hs / 2.0), (a, hs / 2.0)]))
        return out

    for i in range(cols - 1):                    # séparateurs dans la profondeur
        pieces.append(Piece(
            "separateur_profondeur_%d" % (i + 1),
            Polygon([(0, 0), (iy, 0), (iy, hs), (0, hs)]),
            holes=fentes(iy, rows - 1, True)))
    for i in range(rows - 1):                    # séparateurs dans la largeur
        pieces.append(Piece(
            "separateur_largeur_%d" % (i + 1),
            Polygon([(0, 0), (iw, 0), (iw, hs), (0, hs)]),
            holes=fentes(iw, cols - 1, False)))
    return pieces


def make_shelf(x=400.0, y=300.0, heights=(100.0, 100.0, 100.0),
               thickness=4.0, finger=None, kerf=0.1, back=True, **_) -> list:
    """Casier d'étagère : deux côtés, N tablettes, dos optionnel.

    ``heights`` donne les hauteurs UTILES des compartiments, du bas vers le
    haut — le ``sh`` de boxes.py. Les tablettes traversent les côtés par des
    mortaises : l'ensemble se tient sans équerre.
    """
    t = float(thickness)
    f = float(finger or max(2.0 * t, 6.0))
    hs = [float(v) for v in heights if float(v) > 0] or [100.0]
    n_tab = len(hs) + 1                          # une tablette de plus que d'espaces
    hauteur = sum(hs) + n_tab * t

    z, niveaux = 0.0, []
    for i in range(n_tab):
        niveaux.append(z)
        if i < len(hs):
            z += t + hs[i]

    largeur_tab = x - 2 * t
    fentes = []
    for zi in niveaux:
        fentes += edge_slots(y, t, f, kerf, along="x", offset=0.0, position=zi)

    cote = panel(y, hauteur, t, f, (FLAT, FLAT, FLAT, FLAT), kerf)
    pieces = [Piece("cote_gauche", cote, holes=list(fentes)),
              Piece("cote_droit", cote, holes=list(fentes))]
    for i in range(n_tab):
        pieces.append(Piece(
            "tablette_%d" % (i + 1),
            panel(largeur_tab, y, t, f, (FLAT, FINGERS, FLAT, FINGERS), kerf)))
    if back:
        pieces.append(Piece("dos", panel(largeur_tab, hauteur, t, f,
                                         (FLAT, FINGERS, FLAT, FINGERS), kerf)))
    return pieces


def _disc(cx: float, cy: float, r: float, seg: int = 48) -> Polygon:
    return Polygon([(cx + r * math.cos(2 * math.pi * i / seg),
                     cy + r * math.sin(2 * math.pi * i / seg))
                    for i in range(seg)])


def make_wall_cabinet(x=500.0, y=200.0, h=400.0, thickness=4.0, finger=None,
                      kerf=0.1, shelves=1, door=True, hinge_holes=2,
                      hinge_dia=4.0, handle=True, **_) -> list:
    """Armoire murale : caisson fermé, dos plein, porte rapportée.

    La charnière est MÉTALLIQUE et montée par l'utilisateur. On se contente
    donc de percer ses trous de vis — sur la porte et sur le côté gauche, aux
    mêmes hauteurs. C'est l'alignement qui compte, pas le modèle de charnière.
    """
    t = float(thickness)
    f = float(finger or max(2.0 * t, 6.0))
    pieces = _walls(x, y, h, t, f, kerf, with_top=True)
    pieces.append(Piece("dos", panel(x - 2 * t, h - 2 * t, t, f,
                                     (FLAT, FLAT, FLAT, FLAT), kerf)))

    iw, ih = x - 2 * t, h - 2 * t
    shelves = max(0, int(shelves))
    if shelves:
        pas = ih / (shelves + 1)
        fentes = []
        for k in range(1, shelves + 1):
            fentes += edge_slots(y - 2 * t, t, f, kerf, along="x",
                                 offset=0.0, position=k * pas)
        for p in pieces:
            if p.name in ("gauche", "droite"):
                p.holes.extend(fentes)
        for k in range(shelves):
            pieces.append(Piece(
                "tablette_%d" % (k + 1),
                panel(iw, y - 2 * t, t, f, (FLAT, FINGERS, FLAT, FINGERS), kerf)))

    if door:
        jeu = 1.0                                # jeu de porte, sur chaque bord
        dw, dh = x - 2 * jeu, h - 2 * jeu
        trous = []
        r = max(0.5, float(hinge_dia) / 2.0 - kerf / 2.0)
        n_h = max(0, int(hinge_holes))
        for k in range(n_h):
            trous.append(_disc(12.0, dh * (k + 1) / (n_h + 1), r))
        if handle:
            trous.append(_disc(dw - 25.0, dh / 2.0, 5.0))
        pieces.append(Piece("porte",
                            Polygon([(0, 0), (dw, 0), (dw, dh), (0, dh)]),
                            holes=trous))
        for p in pieces:                         # mêmes hauteurs sur le côté
            if p.name == "gauche" and n_h:
                for k in range(n_h):
                    p.holes.append(_disc(y / 2.0, h * (k + 1) / (n_h + 1), r))
    return pieces


GENERATORS = {
    "boite": ("Boîte / plateau", make_box),
    "bac": ("Bac à compartiments", make_tray),
    "etagere": ("Casier d'étagère", make_shelf),
    "armoire": ("Armoire murale", make_wall_cabinet),
}


# --------------------------------------------------------------------------- #
#  Décor appliqué aux pièces
# --------------------------------------------------------------------------- #
#  Façades : ce qui se voit une fois le meuble monté. C'est là qu'un motif ou
#  un nom a un sens — graver le fond d'un tiroir ne sert personne.
FACADES = ("avant", "porte")


def apply_decor(pieces: list, motif: str = "", step: float = 10.0,
                text: str = "", text_height: float = 12.0,
                where: str = "centre", target: str = "facades",
                margin: float = 8.0) -> int:
    """Pose motif et texte sur les pièces choisies. Renvoie le nombre touché.

    GRAVURE, jamais découpe : le décor part sur un calque à faible puissance.
    """
    touchees = 0
    for p in pieces:
        if target == "aucune":
            break
        if target == "facades" and p.name not in FACADES:
            continue
        w, h = p.size()
        avant = len(p.decor)
        if motif and motif != "aucun":
            p.decor.extend(pattern(motif, w, h, step, margin))
        if text.strip():
            p.decor.extend(text_decor(text, text_height, w, h, where))
        if len(p.decor) > avant:
            touchees += 1
    return touchees


# --------------------------------------------------------------------------- #
#  Vers le projet
# --------------------------------------------------------------------------- #
def layout(pieces: list, gap: float = 6.0, wrap: float | None = None) -> list:
    """Pose les pièces côte à côte, sans se chevaucher.

    ``wrap`` passe à la rangée suivante au-delà de cette largeur — utile pour
    l'aperçu, où une seule rangée de quatre mètres serait illisible. Le
    placement définitif, lui, est l'affaire de « Arranger », qui connaît la
    taille réelle du plateau.
    """
    import math as _m
    from shapely.affinity import translate
    if wrap is None:
        aire = sum(p.outline.area for p in pieces) or 1.0
        wrap = _m.sqrt(aire) * 1.8        # grille à peu près carrée
    out, x, y, h_rang = [], 0.0, 0.0, 0.0
    for p in pieces:
        b = p.outline.bounds
        w, h = b[2] - b[0], b[3] - b[1]
        if x > 0 and x + w > wrap:
            x, y = 0.0, y + h_rang + gap
            h_rang = 0.0
        dx, dy = x - b[0], y - b[1]
        out.append(Piece(
            p.name,
            translate(p.outline, dx, dy),
            [translate(t, dx, dy) for t in p.holes],
            [translate(d, dx, dy) for d in p.decor]))
        x += w + gap
        h_rang = max(h_rang, h)
    return out


def to_shapes(pieces: list, layer_cut: str = "c1", layer_decor: str = "c2",
              gap: float = 6.0, existing=None) -> list:
    """Convertit les pièces en objets du projet.

    Contours et ouvertures vont sur le calque de DÉCOUPE, le décor sur celui de
    GRAVURE. Chaque géométrie devient un objet ``trace`` : elle est figée, comme
    un import SVG, parce qu'un panneau à tenons n'a pas de paramètres simples à
    rééditer.
    """
    from . import drawing as dr

    taken = list(existing or [])
    out = []

    def ajoute(geom, layer):
        if geom is None or geom.is_empty:
            return
        s = dr.Shape(dr.KIND_PATH, {"wkt": geom.wkt}, layer=layer,
                     id=dr.new_id("o", taken))
        taken.append(s)
        out.append(s)

    for p in layout(pieces, gap):
        ajoute(p.outline, layer_cut)
        for t in p.holes:
            ajoute(t, layer_cut)
        for d in p.decor:
            ajoute(d, layer_decor)
    return out


def describe(pieces: list, thickness: float) -> str:
    """Résumé lisible : ce qu'on va découper, et comment."""
    prof = cut_profile(thickness)
    aire = sum(p.outline.area for p in pieces) / 1e6
    trous = sum(len(p.holes) for p in pieces)
    decor = sum(len(p.decor) for p in pieces)
    b = [p.size() for p in pieces]
    plus_grande = max(b, key=lambda s: s[0] * s[1]) if b else (0, 0)
    return (f"{len(pieces)} pièces, {trous} ouvertures, {decor} tracés de décor\n"
            f"Surface de panneau : {aire:.3f} m²   "
            f"Plus grande pièce : {plus_grande[0]:.0f} × {plus_grande[1]:.0f} mm\n"
            f"Découpe {thickness:g} mm : {prof['passes']} passes à "
            f"{prof['power']:.0f} % — {prof['speed']:.0f} mm/min")
