"""Rangement automatique des formes sur un ou plusieurs plateaux.

Un dessin de découpe tient rarement sur une seule plaque : les huit panneaux
d'une étagère font 400 × 330 mm chacun. Plutôt que de replacer les pièces à la
main puis de relancer le travail autant de fois, on les range une fois, le
logiciel ouvre les plateaux nécessaires, et chaque plateau devient un job.

**Une pièce n'est pas une forme.** Un panneau découpé, c'est son contour ET ses
perçages — souvent autant de chemins SVG distincts. Les ranger séparément
enverrait les trous à l'autre bout de la plaque : ils ne veulent plus rien dire
hors de leur pièce. Les formes qui s'interpénètrent sont donc regroupées, et un
groupe se déplace et tourne **en bloc**.

Algorithme de placement : **étagères, par hauteur décroissante** (first-fit
decreasing height). Ce n'est pas l'optimum théorique — le placement optimal est
NP-difficile — mais c'est rapide, stable, et le résultat reste lisible pour
l'opérateur : les pièces s'alignent en rangées nettes au lieu d'être semées en
biais.

La rotation à 90° est essayée quand la pièce ne rentre pas autrement. Elle est
indispensable ici : un panneau de 400,2 mm ne passe pas dans 400 mm de large,
mais passe dans 415 mm de haut.
"""

from __future__ import annotations

from . import drawing as dr


# --------------------------------------------------------------------------- #
#  Regroupement : contour + perçages = une pièce
# --------------------------------------------------------------------------- #
def group_shapes(shapes: list) -> list:
    """Regroupe les formes appartenant à une MÊME pièce.

    Deux formes qui s'interpénètrent — typiquement un perçage dans son panneau
    — sont solidaires. Deux pièces posées côte à côte ne le sont pas.
    """
    from shapely.strtree import STRtree

    geoms, valides = [], []
    for s in shapes:
        g = s.geometry()
        if g is None or g.is_empty:
            continue
        geoms.append(g)
        valides.append(s)
    n = len(valides)
    if n == 0:
        return []

    parent = list(range(n))

    def racine(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]       # compression de chemin
            i = parent[i]
        return i

    def fusion(a: int, b: int) -> None:
        ra, rb = racine(a), racine(b)
        if ra != rb:
            parent[rb] = ra

    tree = STRtree(geoms)
    for i, g in enumerate(geoms):
        for j in tree.query(g):
            j = int(j)
            if j <= i:
                continue
            # RECOUVREMENT reel, pas simple contact : deux panneaux poses bord
            # a bord se « touchent » au sens de shapely et seraient fusionnes
            # en une seule piece impossible a placer.
            if geoms[j].intersection(g).area > 1e-9:
                fusion(i, j)

    groupes: dict = {}
    for i, s in enumerate(valides):
        groupes.setdefault(racine(i), []).append(s)
    return list(groupes.values())


def _bbox(groupe: list):
    bs = [s.bounds() for s in groupe]
    bs = [b for b in bs if b is not None]
    if not bs:
        return None
    return (min(b[0] for b in bs), min(b[1] for b in bs),
            max(b[2] for b in bs), max(b[3] for b in bs))


def _translate_group(groupe: list, dx: float, dy: float) -> None:
    for s in groupe:
        dr.translate_shape(s, dx, dy)


def _rotate_group(groupe: list, cx: float, cy: float) -> None:
    """Tourne un groupe de 90° autour de ``(cx, cy)``, en bloc.

    Chaque forme tourne autour de SON centre ; il faut donc la replacer là où
    la rotation du groupe l'aurait emmenée. Sans ce report, les perçages
    resteraient en place pendant que leur panneau pivote.
    """
    for s in groupe:
        b = s.bounds()
        if b is None:
            continue
        sx, sy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
        dx, dy = sx - cx, sy - cy
        nx, ny = cx - dy, cy + dx            # rotation de +90° : (x,y) -> (-y,x)
        s.rotation = (s.rotation + 90.0) % 360.0
        b2 = s.bounds()
        if b2 is None:
            continue
        sx2, sy2 = (b2[0] + b2[2]) / 2.0, (b2[1] + b2[3]) / 2.0
        dr.translate_shape(s, nx - sx2, ny - sy2)


# --------------------------------------------------------------------------- #
#  Placement
# --------------------------------------------------------------------------- #
class _Plate:
    """Un plateau en cours de remplissage, vu comme une pile d'étagères."""

    def __init__(self, width: float, height: float, gap: float):
        self.w, self.h, self.gap = width, height, gap
        self.shelves: list = []        # [y, hauteur, x_courant]
        self.used = 0.0                # hauteur consommée

    def place(self, w: float, h: float):
        """Place une pièce w×h et renvoie son coin bas-gauche, ou ``None``."""
        for sh in self.shelves:
            if w <= self.w - sh[2] + 1e-9 and h <= sh[1] + 1e-9:
                x = sh[2]
                sh[2] = x + w + self.gap
                return (x, sh[0])
        if h <= self.h - self.used + 1e-9 and w <= self.w + 1e-9:
            y = self.used
            self.shelves.append([y, h, w + self.gap])
            self.used = y + h + self.gap
            return (0.0, y)
        return None


def arrange(shapes: list, bed_w: float, bed_h: float, gap: float = 3.0,
            margin: float = 3.0, allow_rotate: bool = True,
            first_plate: int = 0) -> dict:
    """Range ``shapes`` sur autant de plateaux que nécessaire.

    Modifie les objets sur place : ``plate``, position, et rotation éventuelle.
    Les formes solidaires (contour + perçages) restent groupées.

    :returns: ``{"plates": n, "placed": n, "groups": n, "oversized": [ids]}`` —
              les pièces trop grandes pour le plateau sont signalées, pas
              silencieusement empilées dans un coin.
    """
    usable_w = max(1e-6, bed_w - 2 * margin)
    usable_h = max(1e-6, bed_h - 2 * margin)

    items = []
    for groupe in group_shapes(shapes):
        b = _bbox(groupe)
        if b is None:
            continue
        items.append([groupe, b[2] - b[0], b[3] - b[1]])
    # Hauteur décroissante : les étagères basses se remplissent de grandes
    # pièces, les chutes servent aux petites.
    items.sort(key=lambda it: (-max(it[1], it[2]), -it[1] * it[2]))

    plates: list = []
    oversized: list = []
    placed = 0

    def new_plate():
        plates.append(_Plate(usable_w, usable_h, gap))
        return plates[-1]

    for groupe, w, h in items:
        tourne = False
        pos = None
        index = 0
        for index, plate in enumerate(plates):
            pos = plate.place(w, h)
            if pos is None and allow_rotate and abs(w - h) > 1e-9:
                pos = plate.place(h, w)
                tourne = pos is not None
            if pos is not None:
                break
        if pos is None:
            plate = new_plate()
            index = len(plates) - 1
            pos = plate.place(w, h)
            if pos is None and allow_rotate and abs(w - h) > 1e-9:
                pos = plate.place(h, w)
                tourne = pos is not None
        if pos is None:
            # Plus grande que le plateau, même tournée : on la met seule, à sa
            # place, et on le DIT. La masquer serait pire.
            oversized.append(groupe[0].id or "?")
            plate = new_plate()
            index = len(plates) - 1
            pos = (0.0, 0.0)
            plate.used = usable_h          # ce plateau est considéré plein

        if tourne:
            b = _bbox(groupe)
            _rotate_group(groupe, (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)
        b = _bbox(groupe)
        if b is None:
            continue
        _translate_group(groupe, margin + pos[0] - b[0], margin + pos[1] - b[1])
        for s in groupe:
            s.plate = first_plate + index
        placed += len(groupe)

    return {"plates": len(plates), "placed": placed, "groups": len(items),
            "oversized": oversized}


def plate_count(shapes: list) -> int:
    """Nombre de plateaux occupés (au moins un)."""
    return max([int(getattr(s, "plate", 0)) for s in shapes], default=0) + 1


def shapes_on(shapes: list, plate: int) -> list:
    return [s for s in shapes if int(getattr(s, "plate", 0)) == plate]
