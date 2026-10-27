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
# Dose nécessaire pour traverser, en « % de puissance × minute par mm de
# course », PAR MILLIMÈTRE d'épaisseur. Calibrée sur une découpe RÉELLE :
# 4 mm de contreplaqué traversés en 1 passe à 80 % et 300 mm/min, soit
# 1 × 80 / 300 = 0,267 pour 4 mm.
DOSE_PAR_MM = 0.0667
MARGE_DEFAUT = 1.25          # on vise 25 % au-dessus du seuil mesuré


def cut_profile(thickness: float, passes: int = 3, power: float = 80.0,
                dose_par_mm: float = DOSE_PAR_MM,
                marge: float = MARGE_DEFAUT) -> dict:
    """Réglages de découpe : la VITESSE se déduit du nombre de passes.

    Ce qui traverse le bois, c'est l'énergie déposée par millimètre de course :
    ``passes × puissance / vitesse``. À énergie égale, **plus de passes permet
    d'aller plus vite** — et c'est préférable : chaque passe enlève une fine
    couche, le trait chauffe moins, brûle moins les bords et traverse plus
    proprement qu'une seule passe lente.

    La dose est calée sur une mesure réelle, avec une marge : viser pile le
    seuil laisserait des zones non traversées au moindre nœud.
    """
    t = max(0.1, float(thickness))
    n = max(1, int(passes))
    p = max(1.0, min(100.0, float(power)))
    dose = max(1e-6, dose_par_mm * t * max(1.0, marge))
    vitesse = n * p / dose
    return {"passes": n, "power": p, "speed": round(min(vitesse, 6000.0)),
            "dose": dose}


def cut_dose(passes: int, power: float, speed: float) -> float:
    """Dose effectivement déposée — pour se CALIBRER sur une découpe réussie."""
    return max(1, int(passes)) * float(power) / max(1e-6, float(speed))


def engrave_profile(thickness: float) -> dict:
    """Réglages de GRAVURE (décor) : on marque, on ne traverse pas."""
    return {"passes": 1, "power": 35.0, "speed": 1200.0}


# --------------------------------------------------------------------------- #
#  Pièces
# --------------------------------------------------------------------------- #
@dataclass
class Piece:
    """Un panneau : son contour, ses ouvertures, son décor, et sa POSE.

    ``place`` dit où le panneau se trouve une fois le meuble monté :
    ``(origine, u, v)``, trois vecteurs de l'espace. Un point local ``(a, b)``
    du panneau vaut ``origine + a·u + b·v``. C'est ce qui permet la vue 3D —
    et c'est au générateur de le renseigner, lui seul sait comment ça se monte.
    """
    name: str
    outline: Polygon
    holes: list = field(default_factory=list)     # list[Polygon] à découper
    decor: list = field(default_factory=list)     # géométries à GRAVER
    place: tuple | None = None                    # (origine, u, v) en 3D
    # Rang de peinture en vue 3D. Trier des panneaux par la profondeur de leur
    # CENTRE suffit presque toujours — mais pas pour une porte étroite devant
    # une tablette large, dont le centre est plus proche de l'oeil. Une porte
    # est TOUJOURS l'élément le plus avant : on la force au dernier rang.
    depth_group: int = 1

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


def _segments(length: float, finger: float) -> int:
    """Nombre IMPAIR de segments égaux d'environ ``finger`` de large.

    Impair pour que les deux extrémités appartiennent au même panneau : c'est
    ce qui remplit les deux coins.
    """
    m = max(3, int(round(length / max(finger, 0.1))))
    return m if m % 2 else m + 1


def _profile_alterne(length: float, t: float, finger: float, depth: float,
                     kerf: float, phase: int) -> list:
    """Peigne en segments égaux : ``phase`` 0 et 1 sont EXACTEMENT complémentaires.

    C'est le vrai assemblage d'angle : les deux panneaux portent des tenons qui
    s'alternent et remplissent la colonne d'angle. Un seul des deux avec des
    tenons, et ils sortaient dans le vide — l'arête verticale ne tenait rien.
    """
    m = _segments(length, finger)
    w = length / m
    j = kerf / 2.0 if depth > 0 else -kerf / 2.0
    pts = [(0.0, 0.0)]
    for k in range(m):
        if k % 2 != phase % 2:
            continue
        a = 0.0 if k == 0 else k * w - j
        b = length if k == m - 1 else (k + 1) * w + j
        pts += [(a, 0.0), (a, depth), (b, depth), (b, 0.0)]
    pts.append((length, 0.0))
    net = [pts[0]]
    for q in pts[1:]:
        if abs(q[0] - net[-1][0]) > 1e-9 or abs(q[1] - net[-1][1]) > 1e-9:
            net.append(q)
    return net


def _profile(length: float, t: float, finger: float, mode: str,
             kerf: float = 0.0, coin: bool = False,
             phase: int | None = None) -> list:
    """Points de (0,0) à (length,0). ``y`` positif = vers l'EXTÉRIEUR.

    ``kerf`` élargit les tenons et rétrécit les encoches d'un demi-trait, pour
    que l'assemblage soit juste une fois la matière enlevée.

    ``coin`` fait COMMENCER ET FINIR le motif par un tenon au lieu d'un creux.
    C'est indispensable sur les arêtes verticales d'une caisse : avec un creux
    aux deux bouts, ni le panneau avant ni le côté n'occupent le coin, et il
    reste un trou traversant de l'épaisseur du bois, en haut comme en bas.
    """
    if mode == FLAT or length <= 0:
        return [(0.0, 0.0), (length, 0.0)]
    depth = t if mode == FINGERS else -t
    if phase is not None:
        return _profile_alterne(length, t, finger, depth, kerf, phase)
    if coin:
        creux = finger_count(length, finger)          # n creux, n+1 tenons
        gap = (length - (creux + 1) * finger) / creux if creux else 0.0
        if gap > 0.1:
            j = kerf / 2.0 if mode == FINGERS else -kerf / 2.0
            pts: list = []
            for k in range(creux + 1):
                a = k * (finger + gap)
                # Les tenons des DEUX bouts affleurent le bord : ils ne sont
                # pas élargis de ce côté-là, sinon le panneau dépasserait.
                a2 = 0.0 if k == 0 else a - j
                b2 = length if k == creux else a + finger + j
                pts.append((0.0, 0.0) if k == 0 else (a2, 0.0))
                pts += [(a2, depth), (b2, depth), (b2, 0.0)]
            return pts
    n = finger_count(length, finger)
    gap = (length - n * finger) / (n + 1)
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
          edges=(FLAT, FLAT, FLAT, FLAT), kerf: float = 0.0,
          coins=(False, False, False, False),
          phases=(None, None, None, None)) -> Polygon:
    """Panneau rectangulaire ``w × h`` dont les 4 bords portent un profil.

    ``edges`` = (bas, droite, haut, gauche). ``coins`` dit, bord par bord, si
    le motif doit commencer et finir par un tenon — ce qu'il faut sur les
    arêtes verticales d'une caisse pour que les coins soient pleins.
    """
    pts: list = []
    for k, ((ox, oy), (dx, dy), (nx, ny)) in enumerate(_EDGES):
        length = w if dx else h
        prof = _profile(length, t, finger, edges[k], kerf, coins[k], phases[k])
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
        # Même compensation que les encoches de bord : une mortaise est une
        # coupe INTÉRIEURE, le trait l'élargit. On la dessine donc plus étroite
        # d'un demi-trait de chaque côté, comme on dessine le tenon plus large.
        # Sans cette symétrie, le tenon avait 0,2 mm de jeu ici et un
        # ajustement serré ailleurs — deux comportements pour un même geste.
        a, b = x + kerf / 2.0, x + finger - kerf / 2.0
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
    """Motif géométrique à GRAVER (jamais à découper) dans un panneau.

    Chaque trait est RABOTÉ sur le rectangle utile : une diagonale calculée
    « à peu près » déborde du panneau et vient graver la pièce voisine.
    """
    from shapely.geometry import box as _box

    x0, y0, x1, y1 = margin, margin, w - margin, h - margin
    if x1 <= x0 or y1 <= y0:
        return []
    cadre = _box(x0, y0, x1, y1)
    step = max(1.0, step)
    brut: list = []

    if kind == "rayures":
        x = x0
        while x <= x1 + 1e-9:
            brut.append(LineString([(x, y0), (x, y1)]))
            x += step
    elif kind == "croisillons":
        x = x0
        while x <= x1 + 1e-9:
            brut.append(LineString([(x, y0), (x, y1)]))
            x += step
        y = y0
        while y <= y1 + 1e-9:
            brut.append(LineString([(x0, y), (x1, y)]))
            y += step
    elif kind in ("chevrons", "losanges"):
        # Diagonales à 45° couvrant tout le cadre, puis rabotées dessus.
        etendue = (x1 - x0) + (y1 - y0)
        x = x0 - (y1 - y0)
        while x <= x1 + 1e-9:
            brut.append(LineString([(x, y0), (x + etendue, y0 + etendue)]))
            if kind == "losanges":
                brut.append(LineString([(x, y1), (x + etendue, y1 - etendue)]))
            x += step
    elif kind == "cadre":
        brut.append(LineString([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]))

    out = []
    for ligne in brut:
        coupe = ligne.intersection(cadre)
        if coupe.is_empty:
            continue
        for part in getattr(coupe, "geoms", [coupe]):
            if part.geom_type == "LineString" and part.length > 1e-6:
                out.append(part)
    return out


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
#  Poignées
# --------------------------------------------------------------------------- #
#  Une poignée se taille de deux façons : un trou DANS la façade, ou une
#  encoche qui mord sur son BORD. Les deux ne se gravent pas pareil — une
#  encoche doit être retirée du contour, sinon le laser découpe une boucle
#  fermée qui traverse le bord et libère un morceau de façade.
HANDLES = {
    "aucune": "Aucune",
    "trou_rond": "Trou rond",
    "oblong": "Trou oblong",
    "encoche": "Encoche sur le dessus",
    "vis": "Poignée rapportée (2 vis)",
}


def _capsule(x0: float, y0: float, x1: float, y1: float, r: float) -> Polygon:
    """Trou oblong : un segment épaissi, donc aux bouts arrondis."""
    return LineString([(x0, y0), (x1, y1)]).buffer(max(0.5, r),
                                                   quad_segs=24)


def handle_shapes(kind: str, w: float, h: float, taille: float = 60.0,
                  kerf: float = 0.0, vis_dia: float = 4.0) -> tuple:
    """Poignée d'une façade ``w × h``.

    :returns: ``(trous, entailles)`` — les trous se percent dans le panneau,
              les entailles se retranchent de son CONTOUR.
    """
    taille = max(4.0, float(taille))
    cx, cy = w / 2.0, h / 2.0
    j = kerf / 2.0                                # le trait mange la matière

    if kind == "trou_rond":
        return ([_disc(cx, cy, max(1.0, taille / 2.0 - j))], [])
    if kind == "oblong":
        demi = max(2.0, taille / 2.0 - taille / 8.0)
        r = max(1.5, taille / 8.0 - j)
        return ([_capsule(cx - demi, cy, cx + demi, cy, r)], [])
    if kind == "encoche":
        # Ouverte sur le dessus : on y passe les doigts. Elle est RETRANCHÉE
        # du contour, pas percée dedans.
        demi = max(3.0, taille / 2.0 - j)
        prof = max(6.0, taille / 4.0)
        y = h - prof / 2.0
        return ([], [_capsule(cx - demi + prof / 2.0, y,
                              cx + demi - prof / 2.0, y, prof / 2.0 + j)])
    if kind == "vis":
        r = max(0.8, vis_dia / 2.0 - j)
        demi = taille / 2.0
        return ([_disc(cx - demi, cy, r), _disc(cx + demi, cy, r)], [])
    return ([], [])


def apply_handle(piece: "Piece", kind: str, taille: float = 60.0,
                 kerf: float = 0.0, vis_dia: float = 4.0) -> None:
    """Pose la poignée sur une façade, sur place."""
    if not kind or kind == "aucune":
        return
    w, h = piece.size()
    trous, entailles = handle_shapes(kind, w, h, taille, kerf, vis_dia)
    piece.holes.extend(trous)
    for e in entailles:
        reste = piece.outline.difference(e)
        # Une entaille trop grande couperait la façade en deux : on refuse
        # plutôt que de livrer deux morceaux.
        if reste.geom_type == "Polygon" and not reste.is_empty:
            piece.outline = reste


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
# Arêtes VERTICALES d'une caisse : (bas, droite, haut, gauche). Seuls les
# panneaux avant/arrière comblent la colonne d'angle ; leur motif doit donc
# commencer et finir par un tenon, sinon le coin reste ouvert en haut et en
# bas. Les côtés, eux, n'ont pas de matière dans cette colonne : leur appliquer
# le même traitement ferait dégénérer le contour, sans rien remplir.
# Phases du peigne d'angle. Les arêtes VERTICALES joignent deux panneaux qui
# se partagent la colonne d'angle : l'un prend les segments pairs, l'autre les
# impairs. Un seul des deux avec des tenons, et ils sortaient dans le vide —
# l'arête ne tenait rien et les bouts ne s'emboîtaient pas.
_PH_AVANT = 0        # panneaux avant / arrière
_PH_COTE = 1         # panneaux gauche / droite


def _walls(x, y, h, t, f, kerf, with_top: bool, with_front: bool = True) -> list:
    """Les parois et le fond (plus le dessus, plus l'avant selon les cas).

    ``with_front=False`` pour une armoire : sa façade, c'est la PORTE. Les
    bords qui attendaient l'avant redeviennent droits — un tenon qui ne va
    nulle part est juste une dent cassée en puissance.
    """
    haut = NOTCHES if with_top else FLAT
    av = FINGERS if with_front else FLAT         # bord avant du fond / du dessus
    # (bas, droite, haut, gauche) : « bas » est le bord AVANT du fond et du
    # dessus, « haut » le bord arrière. Fond et dessus ont donc le même profil.
    horiz = (av, FINGERS, FINGERS, FINGERS)
    m = t / 2.0                                  # on pose les panneaux à mi-bois
    EX, EY, EZ = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    ph_av = (None, _PH_AVANT, None, _PH_AVANT)
    ph_cote = (None, _PH_COTE, None, _PH_COTE if with_front else None)
    bord_avant_cote = FINGERS if with_front else FLAT

    pieces = [
        Piece("fond", panel(x - 2 * t, y - 2 * t, t, f, horiz, kerf),
              place=((t, t, m), EX, EY)),
        Piece("arriere", panel(x - 2 * t, h, t, f,
                               (NOTCHES, FINGERS, haut, FINGERS), kerf,
                               phases=ph_av),
              place=((t, y - m, 0.0), EX, EZ)),
        Piece("gauche", panel(y - 2 * t, h, t, f,
                              (NOTCHES, FINGERS, haut, bord_avant_cote), kerf,
                              phases=ph_cote),
              place=((m, t, 0.0), EY, EZ)),
        Piece("droite", panel(y - 2 * t, h, t, f,
                              (NOTCHES, FINGERS, haut, bord_avant_cote), kerf,
                              phases=ph_cote),
              place=((x - m, t, 0.0), EY, EZ)),
    ]
    if with_front:
        pieces.insert(1, Piece("avant", panel(x - 2 * t, h, t, f,
                                              (NOTCHES, FINGERS, haut, FINGERS),
                                              kerf, phases=ph_av),
                               place=((t, m, 0.0), EX, EZ)))
    if with_top:
        pieces.append(Piece("dessus", panel(x - 2 * t, y - 2 * t, t, f,
                                            horiz, kerf),
                            place=((t, t, h - m), EX, EY)))
    return pieces


def make_box(x=150.0, y=100.0, h=60.0, thickness=4.0, finger=None,
             kerf=0.1, closed=False, cols=1, rows=1, **_) -> list:
    """Boîte ou plateau, aux cotes EXTÉRIEURES ``x × y × h``.

    ``cols`` et ``rows`` ajoutent des cloisons croisées : une boîte à
    compartiments se commande comme une boîte simple.
    """
    t = float(thickness)
    f = float(finger or max(2.0 * t, 6.0))
    pieces = _walls(x, y, h, t, f, kerf, with_top=bool(closed))
    hauteur = h - (2 * t if closed else t)       # sous le dessus s'il y en a un
    add_dividers(pieces, x - 2 * t, y - 2 * t, hauteur, t, kerf, cols, rows,
                 base=(t, t, t), finger=f)
    return pieces


def _mi_bois(longueur: float, hauteur: float, n_croisements: int, t: float,
             kerf: float, par_le_haut: bool) -> list:
    """Encoches à MI-BOIS pour croiser des cloisons perpendiculaires.

    L'une est fendue par le haut, l'autre par le bas, chacune sur la moitié de
    la hauteur : elles s'emboîtent et tiennent seules, sans colle, et se
    retirent pour redécouper autrement.
    """
    out = []
    for k in range(1, n_croisements + 1):
        pos = longueur * k / (n_croisements + 1)
        a, b = pos - t / 2.0 + kerf / 2.0, pos + t / 2.0 - kerf / 2.0
        if par_le_haut:
            out.append(Polygon([(a, hauteur / 2.0), (b, hauteur / 2.0),
                                (b, hauteur), (a, hauteur)]))
        else:
            out.append(Polygon([(a, 0.0), (b, 0.0),
                                (b, hauteur / 2.0), (a, hauteur / 2.0)]))
    return out


def dividers(iw: float, iy: float, hauteur: float, t: float, kerf: float,
             cols: int = 1, rows: int = 1, prefixe: str = "separateur",
             base=(0.0, 0.0, 0.0), finger: float | None = None) -> tuple:
    """Cloisons découpant l'intérieur en ``cols × rows`` cases.

    ``cols`` compte les cases dans la LARGEUR, ``rows`` dans la PROFONDEUR : il
    faut donc une cloison de moins que de cases dans chaque sens.

    Chaque cloison porte des TENONS à ses deux bouts, qui traversent les
    parois. Sans eux, une cloison seule — quand elle n'en croise aucune autre —
    ne serait tenue par rien et flotterait dans la caisse.

    :returns: ``(pieces, mortaises)``. Les mortaises sont à percer dans les
              parois nommées, en coordonnées locales de celles-ci.
    """
    cols, rows = max(1, int(cols)), max(1, int(rows))
    f = float(finger or max(2.0 * t, 6.0))
    bx, by, bz = base
    EX, EY, EZ = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    out: list = []
    mortaises: dict = {"avant": [], "arriere": [], "gauche": [], "droite": []}

    # --- cloisons dans la profondeur : elles butent sur l'avant et l'arrière #
    for i in range(cols - 1):
        xc = iw * (i + 1) / cols
        p = Piece(
            "%s_profondeur_%d" % (prefixe, i + 1),
            panel(iy, hauteur, t, f, (FLAT, FINGERS, FLAT, FINGERS), kerf),
            holes=_mi_bois(iy, hauteur, rows - 1, t, kerf, True),
            place=((bx + xc, by, bz), EY, EZ))
        out.append(p)
        for paroi in ("avant", "arriere"):
            mortaises[paroi] += edge_slots(hauteur, t, f, kerf, along="y",
                                           offset=bz, position=xc - t / 2.0)

    # --- cloisons dans la largeur : elles butent sur les deux côtés -------- #
    for i in range(rows - 1):
        yc = iy * (i + 1) / rows
        p = Piece(
            "%s_largeur_%d" % (prefixe, i + 1),
            panel(iw, hauteur, t, f, (FLAT, FINGERS, FLAT, FINGERS), kerf),
            holes=_mi_bois(iw, hauteur, cols - 1, t, kerf, False),
            place=((bx, by + yc, bz), EX, EZ))
        out.append(p)
        for paroi in ("gauche", "droite"):
            mortaises[paroi] += edge_slots(hauteur, t, f, kerf, along="y",
                                           offset=bz, position=yc - t / 2.0)
    return out, mortaises


def add_dividers(pieces: list, iw: float, iy: float, hauteur: float, t: float,
                 kerf: float, cols: int = 1, rows: int = 1,
                 base=(0.0, 0.0, 0.0), finger: float | None = None) -> list:
    """Ajoute les cloisons à une caisse ET perce les parois qui les reçoivent."""
    nouvelles, mortaises = dividers(iw, iy, hauteur, t, kerf, cols, rows,
                                    base=base, finger=finger)
    par_nom = {p.name: p for p in pieces}
    for paroi, trous in mortaises.items():
        p = par_nom.get(paroi)
        if p is not None:
            p.holes.extend(trous)
    pieces.extend(nouvelles)
    return pieces


def make_tray(x=200.0, y=150.0, h=60.0, thickness=4.0, finger=None,
              kerf=0.1, cols=2, rows=2, **_) -> list:
    """Bac à compartiments : un plateau et ses cloisons croisées à mi-bois."""
    t = float(thickness)
    f = float(finger or max(2.0 * t, 6.0))
    pieces = _walls(x, y, h, t, f, kerf, with_top=False)
    add_dividers(pieces, x - 2 * t, y - 2 * t, h - t, t, kerf, cols, rows,
                 base=(t, t, t), finger=f)
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

    EX, EY, EZ = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    cote = panel(y, hauteur, t, f, (FLAT, FLAT, FLAT, FLAT), kerf)
    pieces = [Piece("cote_gauche", cote, holes=list(fentes),
                    place=((t / 2.0, 0.0, 0.0), EY, EZ)),
              Piece("cote_droit", cote, holes=list(fentes),
                    place=((x - t / 2.0, 0.0, 0.0), EY, EZ))]
    for i in range(n_tab):
        pieces.append(Piece(
            "tablette_%d" % (i + 1),
            panel(largeur_tab, y, t, f, (FLAT, FINGERS, FLAT, FINGERS), kerf),
            place=((t, 0.0, niveaux[i] + t / 2.0), EX, EY)))
    if back:
        pieces.append(Piece("dos", panel(largeur_tab, hauteur, t, f,
                                         (FLAT, FINGERS, FLAT, FINGERS), kerf),
                            place=((t, y - t / 2.0, 0.0), EX, EZ)))
    return pieces


def _disc(cx: float, cy: float, r: float, seg: int = 48) -> Polygon:
    return Polygon([(cx + r * math.cos(2 * math.pi * i / seg),
                     cy + r * math.sin(2 * math.pi * i / seg))
                    for i in range(seg)])


def make_wall_cabinet(x=500.0, y=200.0, h=400.0, thickness=4.0, finger=None,
                      kerf=0.1, shelves=1, doors=1, hinge_holes=2,
                      hinge_dia=4.0, handle="trou_rond", handle_size=40.0,
                      cols=1, **kw) -> list:
    """Armoire murale : caisson fermé, dos plein, tablettes, portes.

    La charnière est MÉTALLIQUE et montée par l'utilisateur. On se contente
    donc de percer ses trous de vis — sur la porte et sur le montant, aux mêmes
    hauteurs. C'est l'alignement qui compte, pas le modèle de charnière.

    ``cols`` ajoute des cloisons VERTICALES qui traversent les tablettes par un
    croisement à mi-bois : chaque tablette est fendue par le bas, chaque
    cloison par l'arrière, et l'ensemble s'emboîte.
    """
    t = float(thickness)
    f = float(finger or max(2.0 * t, 6.0))
    if "door" in kw and "doors" not in kw:       # ancien nom booléen
        doors = 1 if kw["door"] else 0
    doors = max(0, min(2, int(doors)))
    shelves = max(0, int(shelves))
    cols = max(1, int(cols))

    # Pas de panneau avant : la façade, ce sont les portes. Le panneau arrière
    # fait office de dos — en ajouter un second ne tiendrait pas plus.
    pieces = _walls(x, y, h, t, f, kerf, with_top=True, with_front=False)

    iw, ih, iy = x - 2 * t, h - 2 * t, y - 2 * t
    z_tablettes = [ih * (k + 1) / (shelves + 1) for k in range(shelves)]

    # --- tablettes, enfilées dans des mortaises des côtés ------------------ #
    if shelves:
        fentes = []
        for z in z_tablettes:
            # ``z`` est compté depuis le fond INTÉRIEUR ; les coordonnées du
            # panneau, elles, partent du bas EXTÉRIEUR. Sans ce « + t », les
            # mortaises tombaient une épaisseur trop bas — la tablette restait
            # de niveau, mais pas à la hauteur annoncée.
            fentes += edge_slots(iy, t, f, kerf, along="x", offset=0.0,
                                 position=t + z)
        for p in pieces:
            if p.name in ("gauche", "droite"):
                p.holes.extend(fentes)

    # --- cloisons verticales, croisant les tablettes ----------------------- #
    x_cloisons = [iw * (j + 1) / cols for j in range(cols - 1)]
    for k in range(shelves):
        tab = Piece("tablette_%d" % (k + 1),
                    panel(iw, iy, t, f, (FLAT, FINGERS, FLAT, FINGERS), kerf),
                    place=((t, t, t + z_tablettes[k]), (1.0, 0.0, 0.0),
                           (0.0, 1.0, 0.0)))
        for xc in x_cloisons:                    # fendue par l'AVANT
            a, b = xc - t / 2.0 + kerf / 2.0, xc + t / 2.0 - kerf / 2.0
            tab.holes.append(Polygon([(a, 0.0), (b, 0.0),
                                      (b, iy / 2.0), (a, iy / 2.0)]))
        pieces.append(tab)
    for j, _xc in enumerate(x_cloisons):
        cl = Piece("cloison_%d" % (j + 1),
                   Polygon([(0, 0), (iy, 0), (iy, ih), (0, ih)]),
                   place=((t + _xc, t, t), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
        for z in z_tablettes:                    # fendue par l'ARRIÈRE
            a, b = z - t / 2.0 + kerf / 2.0, z + t / 2.0 - kerf / 2.0
            cl.holes.append(Polygon([(iy / 2.0, a), (iy, a), (iy, b),
                                     (iy / 2.0, b)]))
        pieces.append(cl)

    # --- portes ------------------------------------------------------------- #
    if doors:
        jeu = 1.0                                # jeu sur chaque bord
        r = max(0.5, float(hinge_dia) / 2.0 - kerf / 2.0)
        n_h = max(0, int(hinge_holes))
        dh = h - 2 * jeu
        largeurs = ([x - 2 * jeu] if doors == 1
                    else [(x - 3 * jeu) / 2.0] * 2)
        noms = ["porte"] if doors == 1 else ["porte_gauche", "porte_droite"]
        montants = ["gauche"] if doors == 1 else ["gauche", "droite"]
        for nom, dw, montant in zip(noms, largeurs, montants):
            trous = []
            cote_gond = 12.0 if montant == "gauche" else dw - 12.0
            for k in range(n_h):
                trous.append(_disc(cote_gond, dh * (k + 1) / (n_h + 1), r))
            dx = jeu if montant == "gauche" else 2 * jeu + largeurs[0]
            porte = Piece(nom, Polygon([(0, 0), (dw, 0), (dw, dh), (0, dh)]),
                          holes=trous,
                          place=((dx, -t / 2.0, jeu), (1.0, 0.0, 0.0),
                                 (0.0, 0.0, 1.0)),
                          depth_group=2)
            if handle and handle != "aucune":
                # Poignée du côté OPPOSÉ au gond : contre la charnière, elle
                # serait inatteignable.
                av = len(porte.holes)
                apply_handle(porte, handle, handle_size, kerf, hinge_dia)
                from shapely.affinity import translate as _tr
                decale = dw / 4.0 if montant == "gauche" else -dw / 4.0
                porte.holes[av:] = [_tr(g, decale, 0.0) for g in porte.holes[av:]]
            pieces.append(porte)
        # mêmes hauteurs sur les montants : l'alignement est tout
        if n_h:
            for p in pieces:
                if p.name in montants:
                    for k in range(n_h):
                        p.holes.append(_disc(y / 2.0, h * (k + 1) / (n_h + 1), r))
    return pieces


# --------------------------------------------------------------------------- #
#  Tiroir
# --------------------------------------------------------------------------- #
def make_drawer(x=300.0, y=250.0, h=80.0, thickness=4.0, finger=None,
                kerf=0.1, cols=1, rows=1, handle="oblong", handle_size=60.0,
                clearance=1.5, **_) -> list:
    """Tiroir : une caisse ouverte sur le dessus, avec poignée en façade.

    ``x``, ``y``, ``h`` sont les cotes du COMPARTIMENT qui doit le recevoir ;
    ``clearance`` est le jeu TOTAL de coulissement (2 mm de chaque côté pour
    4 mm saisis). Donner les cotes du casier plutôt que celles du tiroir évite
    le calcul mental où l'on se trompe d'un côté.
    """
    t = float(thickness)
    f = float(finger or max(2.0 * t, 6.0))
    jeu = max(0.0, float(clearance))
    # ``clearance`` est le jeu TOTAL, pas celui de chaque côté : saisir 4 donne
    # bien 4 mm de battement en largeur (2 de chaque côté). Compté par côté, un
    # « 4 mm » intuitif en aurait fait 8, et le tiroir aurait dansé.
    bx = max(4 * t, x - jeu)
    by = max(4 * t, y - jeu / 2.0)        # une seule fois : il va au fond
    bh = max(2 * t, h - jeu / 2.0)

    pieces = _walls(bx, by, bh, t, f, kerf, with_top=False)
    add_dividers(pieces, bx - 2 * t, by - 2 * t, bh - t, t, kerf, cols, rows,
                 base=(t, t, t), finger=f)
    for p in pieces:
        if p.name == "avant":
            apply_handle(p, handle, handle_size, kerf)
    return pieces


# --------------------------------------------------------------------------- #
#  Meuble de cuisine complet
# --------------------------------------------------------------------------- #
def _decale(pieces: list, prefixe: str, dx: float, dy: float, dz: float) -> list:
    """Renomme et repositionne un sous-ensemble dans le meuble qui l'accueille.

    Les générateurs produisent leurs pièces dans LEUR repère ; pour qu'un
    tiroir apparaisse à sa place dans la vue 3D du meuble, il faut décaler sa
    pose — sans toucher aux tracés de découpe, qui restent à plat.
    """
    out = []
    for p in pieces:
        pose = None
        if p.place:
            (ox, oy, oz), u, v = p.place
            pose = ((ox + dx, oy + dy, oz + dz), u, v)
        out.append(Piece(prefixe + p.name, p.outline, list(p.holes),
                         list(p.decor), pose, p.depth_group))
    return out


def _portes(nom_base: str, largeur: float, hauteur: float, z: float, y: float,
            t: float, kerf: float, nb: int, handle: str, handle_size: float,
            hinge_holes: int, hinge_dia: float) -> list:
    """Une ou deux portes couvrant une ouverture donnée.

    Gonds côté montant, poignée à l'opposé : contre la charnière elle serait
    inatteignable.
    """
    if nb <= 0 or hauteur <= 2.0:
        return []
    jeu = 1.0                                    # jeu sur chaque bord
    dh = hauteur - 2 * jeu
    largeurs = ([largeur - 2 * jeu] if nb == 1
                else [(largeur - 3 * jeu) / 2.0] * 2)
    noms = ([nom_base] if nb == 1
            else [nom_base + "_gauche", nom_base + "_droite"])
    montants = ["gauche"] if nb == 1 else ["gauche", "droite"]
    r = max(0.5, hinge_dia / 2.0 - kerf / 2.0)
    out = []
    for i, (nom, dw, montant) in enumerate(zip(noms, largeurs, montants)):
        trous = []
        cote_gond = 12.0 if montant == "gauche" else dw - 12.0
        for k in range(max(0, hinge_holes)):
            trous.append(_disc(cote_gond, dh * (k + 1) / (hinge_holes + 1), r))
        porte = Piece(nom, Polygon([(0, 0), (dw, 0), (dw, dh), (0, dh)]),
                      holes=trous, depth_group=2,
                      place=((jeu + i * (dw + jeu), -t / 2.0, z + jeu),
                             (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)))
        if handle and handle != "aucune":
            av = len(porte.holes)
            apply_handle(porte, handle, handle_size, kerf, hinge_dia)
            from shapely.affinity import translate as _tr
            decale = dw / 4.0 if montant == "gauche" else -dw / 4.0
            porte.holes[av:] = [_tr(g, decale, 0.0) for g in porte.holes[av:]]
        out.append(porte)
    _ = y
    return out


def make_kitchen(x=600.0, y=560.0, h=720.0, thickness=4.0, finger=None,
                 kerf=0.1, drawers=3, doors=0, drawer_heights=None,
                 drawer_clearance=4.0, cols=1, rows=1, handle="oblong",
                 handle_size=120.0, hinge_holes=2, hinge_dia=4.0, **_) -> list:
    """Meuble de cuisine : caisson, tiroirs complets, porte éventuelle.

    Le caisson est ouvert en façade ; sa hauteur utile est partagée entre des
    sections, de bas en haut : ``drawers`` tiroirs puis, s'il y a lieu, une
    section à porte. Une tablette sépare deux sections et sert de glissière au
    tiroir du dessus.

    **Chaque tiroir est un meuble complet**, généré par ``make_drawer`` : il
    reçoit donc ses propres compartiments (``cols`` × ``rows``) et sa poignée.
    ``drawer_clearance`` est le jeu de coulissement retiré de l'ouverture —
    4 mm par défaut, soit 2 mm de chaque côté en largeur.
    """
    t = float(thickness)
    f = float(finger or max(2.0 * t, 6.0))
    drawers = max(0, int(drawers))
    doors = max(0, min(2, int(doors)))
    jeu = max(0.0, float(drawer_clearance))

    pieces = _walls(x, y, h, t, f, kerf, with_top=True, with_front=False)
    iw, ih, iy = x - 2 * t, h - 2 * t, y - 2 * t

    sections = drawers + (1 if doors else 0)
    if sections == 0:
        return pieces

    # Hauteurs des sections : celles données, sinon un partage égal.
    libre = ih - (sections - 1) * t          # les tablettes mangent leur épaisseur
    donnees = [float(v) for v in (drawer_heights or []) if float(v) > 0]
    if len(donnees) >= sections:
        hauteurs = donnees[:sections]
    elif donnees:
        reste = max(10.0, libre - sum(donnees))
        hauteurs = donnees + [reste / (sections - len(donnees))] * (
            sections - len(donnees))
    else:
        hauteurs = [libre / sections] * sections

    # Tablettes intermédiaires : elles traversent les côtés par des mortaises.
    z, niveaux = t, []
    for i, hs in enumerate(hauteurs):
        niveaux.append(z)
        z += hs
        if i < sections - 1:
            fentes = edge_slots(iy, t, f, kerf, along="x", offset=0.0,
                                position=z)
            for p in pieces:
                if p.name in ("gauche", "droite"):
                    p.holes.extend(fentes)
            pieces.append(Piece(
                "tablette_%d" % (i + 1),
                panel(iw, iy, t, f, (FLAT, FINGERS, FLAT, FINGERS), kerf),
                place=((t, t, z), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))))
            z += t

    # Tiroirs : chacun est un meuble à part entière, posé dans son ouverture.
    for i in range(drawers):
        hs = hauteurs[i]
        tiroir = make_drawer(x=iw, y=iy, h=hs, thickness=t, finger=f, kerf=kerf,
                             cols=cols, rows=rows, handle=handle,
                             handle_size=min(handle_size, iw * 0.6),
                             clearance=jeu)
        pieces += _decale(tiroir, "tiroir%d_" % (i + 1),
                          t + jeu / 2.0, t, niveaux[i])

    # Porte : elle couvre la section du haut.
    if doors:
        pieces += _portes("porte", x, hauteurs[-1], niveaux[-1], y, t, kerf,
                          doors, handle, min(handle_size, 60.0), hinge_holes,
                          hinge_dia)
    return pieces


GENERATORS = {
    "boite": ("Boîte / plateau", make_box),
    "bac": ("Bac à compartiments", make_tray),
    "etagere": ("Casier d'étagère", make_shelf),
    "armoire": ("Armoire murale", make_wall_cabinet),
    "tiroir": ("Tiroir", make_drawer),
    "cuisine": ("Meuble de cuisine", make_kitchen),
}


# --------------------------------------------------------------------------- #
#  Décor appliqué aux pièces
# --------------------------------------------------------------------------- #
#  Façades : ce qui se voit une fois le meuble monté. C'est là qu'un motif ou
#  un nom a un sens — graver le fond d'un tiroir ne sert personne.
FACADES = ("avant", "porte", "porte_gauche", "porte_droite")


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


def describe(pieces: list, thickness: float, profil: dict | None = None) -> str:
    """Résumé lisible : ce qu'on va découper, et comment."""
    prof = profil or cut_profile(thickness)
    aire = sum(p.outline.area for p in pieces) / 1e6
    trous = sum(len(p.holes) for p in pieces)
    decor = sum(len(p.decor) for p in pieces)
    b = [p.size() for p in pieces]
    plus_grande = max(b, key=lambda s: s[0] * s[1]) if b else (0, 0)
    return (f"{len(pieces)} pièces, {trous} ouvertures, {decor} tracés de décor\n"
            f"Surface de panneau : {aire:.3f} m²   "
            f"Plus grande pièce : {plus_grande[0]:.0f} × {plus_grande[1]:.0f} mm\n"
            f"Découpe {thickness:g} mm : {prof['passes']} passes à "
            f"{prof['power']:.0f} % — {prof['speed']:.0f} mm/min "
            f"(dose {prof.get('dose', 0):.3f})")


# --------------------------------------------------------------------------- #
#  Vue 3D (projection isométrique)
# --------------------------------------------------------------------------- #
#  Pas de moteur 3D : une projection suffit à juger un meuble, et elle
#  n'ajoute aucune dépendance. Les panneaux sont traités comme des plans sans
#  épaisseur, triés du plus lointain au plus proche (algorithme du peintre).
_ISO_AZIMUT, _ISO_ELEVATION = 45.0, 35.264     # isométrie classique


def _camera(azimut: float, elevation: float) -> tuple:
    """Repère de la caméra : (droite, haut, vers l'œil).

    ``azimut`` tourne autour de l'axe vertical, ``elevation`` monte ou descend
    le point de vue. Les trois vecteurs sont orthonormés, donc projeter revient
    à trois produits scalaires.
    """
    a = math.radians(azimut)
    e = math.radians(elevation)
    ca, sa, ce, se = math.cos(a), math.sin(a), math.cos(e), math.sin(e)
    # L'oeil est du côté des Y NÉGATIFS : on regarde la FAÇADE du meuble, qui
    # est en y = 0. À l'envers, le panneau arrière passait devant la porte.
    droite = (ca, sa, 0.0)
    haut = (-se * sa, se * ca, ce)
    oeil = (sa * ce, -ca * ce, se)
    return droite, haut, oeil


def _dot(p, q) -> float:
    return p[0] * q[0] + p[1] * q[1] + p[2] * q[2]


def iso_point(x: float, y: float, z: float, azimut: float = _ISO_AZIMUT,
              elevation: float = _ISO_ELEVATION) -> tuple:
    """Point de l'espace -> écran. Y est la profondeur, Z la hauteur."""
    droite, haut, _oeil = _camera(azimut, elevation)
    return (_dot((x, y, z), droite), _dot((x, y, z), haut))


def _norm(u, v) -> tuple:
    """Normale du panneau : sert à l'éclairer, donc à lire son orientation."""
    nx = u[1] * v[2] - u[2] * v[1]
    ny = u[2] * v[0] - u[0] * v[2]
    nz = u[0] * v[1] - u[1] * v[0]
    n = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return (nx / n, ny / n, nz / n)


def project_iso(pieces: list, exploded: float = 0.0,
                azimut: float = _ISO_AZIMUT,
                elevation: float = _ISO_ELEVATION) -> list:
    """Projette le meuble ASSEMBLÉ, du plus lointain au plus proche.

    Chaque face : ``{"poly": Polygon écran, "shade": 0..1, "name": str,
    "depth": float}``. ``exploded`` écarte les panneaux le long de leur
    normale ; ``azimut`` et ``elevation`` font tourner le point de vue.
    """
    from shapely.affinity import affine_transform
    from shapely.ops import unary_union

    placees = [p for p in pieces if p.place]
    if not placees:
        return []
    droite, haut, oeil = _camera(azimut, elevation)

    def ecran(p3):
        return (_dot(p3, droite), _dot(p3, haut))

    faces = []
    for p in placees:
        (ox, oy, oz), u, v = p.place
        n = _norm(u, v)
        if exploded:
            ox += n[0] * exploded * 60.0
            oy += n[1] * exploded * 60.0
            oz += n[2] * exploded * 60.0

        # Les trous deviennent des vides : on voit à travers, comme en vrai.
        geom = p.outline
        if p.holes:
            try:
                geom = geom.difference(unary_union(p.holes))
            except Exception:  # noqa: BLE001
                pass
        if geom.is_empty:
            continue

        # La composition « local -> espace -> écran » reste AFFINE : une seule
        # matrice suffit, pas besoin de transformer point par point.
        ax, ay = ecran(u)
        bx, by = ecran(v)
        px, py = ecran((ox, oy, oz))
        vue = affine_transform(geom, [ax, bx, ay, by, px, py])
        if vue.is_empty:
            continue

        # Profondeur au CENTRE REEL du panneau. Avec une taille forfaitaire, un
        # grand panneau et un petit se trient n'importe comment, et le fond
        # passe devant la porte.
        b = geom.bounds
        ca, cb = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
        centre = (ox + u[0] * ca + v[0] * cb,
                  oy + u[1] * ca + v[1] * cb,
                  oz + u[2] * ca + v[2] * cb)

        # éclairage : un panneau tourné vers le haut est plus clair qu'un flanc
        lum = abs(n[0]) * 0.55 + abs(n[1]) * 0.75 + abs(n[2]) * 1.0
        faces.append({"poly": vue, "shade": max(0.35, min(1.0, lum)),
                      "name": p.name, "depth": _dot(centre, oeil),
                      "group": p.depth_group})
    # Une porte est au dernier plan quand on regarde la FAÇADE — et au tout
    # premier quand on tourne derrière le meuble, où elle est cachée par le
    # dos. Le rang suit donc le côté d'où l'on regarde.
    sens = 1 if oeil[1] <= 0 else -1
    faces.sort(key=lambda d: (sens * d["group"], d["depth"]))
    return faces
