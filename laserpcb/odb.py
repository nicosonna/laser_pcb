"""Lecteur ODB++ (Autodesk Fusion, EAGLE, Allegro…) -> même géométrie shapely.

Point clé de l'architecture : ce module produit exactement les mêmes objets que
``geometry.py`` (CopperLayer / Hole / contour Polygon). Le format d'entrée change,
mais tout l'aval (isolation, négatif, G-code, GUI) reste identique.

Structure ODB++ (arborescence de dossiers) :
    <job>/steps/<step>/profile                 -> contour de carte
    <job>/steps/<step>/layers/<layer>/features -> features d'un calque
    ...through_hole/features                    -> perçages

Format « features » (ASCII, une entité par ligne) :
    $<n> <symbole>            définition de symbole (dimensions en MICRONS)
    L x1 y1 x2 y2 <sym> <pol> piste (segment épaissi par le symbole)
    P x y <sym> <pol> <orient> flash de pad
    S <pol> ... OB/OS/OC ... SE  surface pleine (plan de masse, région)
Les coordonnées sont en MILLIMÈTRES (UNITS=MM), les tailles de symbole en microns.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from shapely import box
from shapely.affinity import rotate, scale
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geometry import CopperLayer, Hole, LabeledCopper, label_by_component

_QUAD_SEGS = 24
_UM = 1000.0  # microns -> mm


# --------------------------------------------------------------------------- #
# Symboles (apertures)
# --------------------------------------------------------------------------- #
class Symbol:
    """Une aperture ODB++ : sait se « flasher » en polygone et donner sa largeur."""

    def __init__(self, name: str):
        self.name = name
        self.kind, self.dims = _parse_symbol(name)

    def flash(self, x: float, y: float, orient: float = 0.0) -> BaseGeometry | None:
        k, d = self.kind, self.dims
        if k == "round":
            return Point(x, y).buffer(d[0] / 2, quad_segs=_QUAD_SEGS)
        if k == "square":
            g = box(-d[0] / 2, -d[0] / 2, d[0] / 2, d[0] / 2)
        elif k == "rect":
            g = box(-d[0] / 2, -d[1] / 2, d[0] / 2, d[1] / 2)
        elif k == "oval":
            g = scale(Point(0, 0).buffer(0.5, quad_segs=_QUAD_SEGS), d[0], d[1])
        elif k == "octagon":
            g = _octagon(d[0], d[1])
        else:
            return None
        if orient:
            g = rotate(g, orient, origin=(0, 0), use_radians=False)
        from shapely.affinity import translate
        return translate(g, x, y)

    @property
    def width(self) -> float:
        """Largeur nominale du trait quand ce symbole trace une ligne (mm)."""
        return min(self.dims) if self.dims else 0.0


def _parse_symbol(name: str):
    """Décode un nom de symbole ODB++ en (kind, [dims_mm])."""
    n = name.lower()
    m = re.match(r"r([\d.]+)$", n)
    if m:
        return "round", [float(m.group(1)) / _UM]
    m = re.match(r"s([\d.]+)$", n)
    if m:
        return "square", [float(m.group(1)) / _UM]
    m = re.match(r"rect([\d.]+)x([\d.]+)", n)  # xr<rayon> de coin éventuel ignoré
    if m:
        return "rect", [float(m.group(1)) / _UM, float(m.group(2)) / _UM]
    m = re.match(r"oval([\d.]+)x([\d.]+)", n)
    if m:
        return "oval", [float(m.group(1)) / _UM, float(m.group(2)) / _UM]
    m = re.match(r"oct([\d.]+)x([\d.]+)", n)
    if m:
        return "octagon", [float(m.group(1)) / _UM, float(m.group(2)) / _UM]
    # Symbole non reconnu : on renverra None au flash (ignoré proprement).
    return "unknown", []


def _octagon(w: float, h: float) -> Polygon:
    """Octogone inscrit dans w x h (chanfrein à 45°, approximation raisonnable)."""
    cx, cy = w / 2, h / 2
    c = min(w, h) * 0.29289  # (1 - cos45) approx pour le chanfrein
    return Polygon([
        (-cx + c, -cy), (cx - c, -cy), (cx, -cy + c), (cx, cy - c),
        (cx - c, cy), (-cx + c, cy), (-cx, cy - c), (-cx, -cy + c),
    ])


_ORIENT = {"0": 0, "1": 90, "2": 180, "3": 270, "4": 0, "5": 90, "6": 180, "7": 270}


def _pad_orient(tok: list[str]) -> float:
    """Orientation d'un pad ODB++ (record P x y sym pol **dcode orient**).

    Le champ après le D-code (tok[6]) porte l'orientation (0-7 -> 0/90/180/270°) ;
    tok[5] est le D-code (toujours 0). Lire tok[5] rendait tous les pads à 0°.
    """
    if len(tok) > 6:
        return _ORIENT.get(tok[6].split(";")[0], 0)
    return 0


# --------------------------------------------------------------------------- #
# Parsing d'un fichier « features »
# --------------------------------------------------------------------------- #
def _read_features(path: Path):
    """Renvoie (symbols: dict[int,Symbol], geoms_pos, geoms_neg, holes-candidates)."""
    symbols: dict[int, Symbol] = {}
    pos: list[BaseGeometry] = []
    neg: list[BaseGeometry] = []
    flashes: list[tuple[float, float, Symbol]] = []  # pour repérer les perçages

    lines = path.read_text(errors="replace").splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        i += 1
        if not raw or raw.startswith("#"):
            continue

        if raw.startswith("$"):
            parts = raw.split()
            num = int(parts[0][1:])
            symbols[num] = Symbol(parts[1])
            continue

        tok = raw.split()
        rec = tok[0]

        if rec == "L" and len(tok) >= 7:
            x1, y1, x2, y2 = map(float, tok[1:5])
            sym = symbols.get(int(tok[5]))
            pol = tok[6]
            if sym and sym.width > 0:
                g = LineString([(x1, y1), (x2, y2)]).buffer(
                    sym.width / 2, cap_style="round", quad_segs=_QUAD_SEGS)
                (pos if pol == "P" else neg).append(g)

        elif rec == "P" and len(tok) >= 5:
            x, y = float(tok[1]), float(tok[2])
            sym = symbols.get(int(tok[3]))
            pol = tok[4]
            orient = _pad_orient(tok)
            if sym:
                g = sym.flash(x, y, orient)
                if g is not None:
                    (pos if pol == "P" else neg).append(g)
                    flashes.append((x, y, sym))

        elif rec == "S":
            pol = tok[1] if len(tok) > 1 else "P"
            surf, i = _read_surface(lines, i)
            if surf and not surf.is_empty:
                (pos if pol == "P" else neg).append(surf)

    return symbols, pos, neg, flashes


def _read_surface(lines, i):
    """Lit un bloc S…SE. Renvoie (polygone, index suivant)."""
    islands: list[Polygon] = []
    holes: list[Polygon] = []
    cur: list[tuple[float, float]] = []
    is_hole = False

    while i < len(lines):
        t = lines[i].strip().split()
        i += 1
        if not t:
            continue
        if t[0] == "SE":
            break
        if t[0] == "OB":  # début de contour
            cur = [(float(t[1]), float(t[2]))]
            is_hole = (len(t) > 3 and t[3] == "H")
        elif t[0] == "OS":  # segment droit
            cur.append((float(t[1]), float(t[2])))
        elif t[0] == "OC":  # arc : on tessellise vers le point d'arrivée
            xe, ye, xc, yc = map(float, t[1:5])
            cw = (len(t) > 5 and t[5] == "Y")
            cur.extend(_arc_points(cur[-1], (xe, ye), (xc, yc), cw))
        elif t[0] == "OE":  # fin de contour
            if len(cur) >= 3:
                (holes if is_hole else islands).append(Polygon(cur))
            cur = []

    surf = unary_union(islands) if islands else Polygon()
    if holes:
        surf = surf.difference(unary_union(holes))
    return surf, i


def _arc_points(p0, p1, center, cw, seg_deg=12.0):
    """Approxime un arc ODB++ par des segments (~12° par pas)."""
    cx, cy = center
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    r = math.hypot(p0[0] - cx, p0[1] - cy)
    if cw:
        while a1 > a0:
            a1 -= 2 * math.pi
    else:
        while a1 < a0:
            a1 += 2 * math.pi
    n = max(1, int(abs(a1 - a0) / math.radians(seg_deg)))
    return [(cx + r * math.cos(a0 + (a1 - a0) * k / n),
             cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(1, n + 1)]


def _finalize(pos, neg) -> MultiPolygon:
    geom = unary_union(pos) if pos else Polygon()
    if neg:
        geom = geom.difference(unary_union(neg))
    if geom.is_empty:
        return MultiPolygon()
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    return MultiPolygon([g for g in geom.geoms if g.geom_type == "Polygon"])


# --------------------------------------------------------------------------- #
# Parcours par-feature (conserve l'identité -> permet l'étiquetage par net)
# --------------------------------------------------------------------------- #
@dataclass
class Feature:
    index: int          # position 0-based dans le fichier (= index FID de la netlist)
    geom: BaseGeometry | None
    polarity: str       # "P" (ajout) ou "N" (retrait)
    rec: str            # "L", "P" ou "S"
    sym: "Symbol | None"


def iter_features(path: Path):
    """Génère chaque feature d'un fichier ``features`` dans l'ordre (index stable)."""
    lines = Path(path).read_text(errors="replace").splitlines()
    symbols: dict[int, Symbol] = {}
    i = idx = 0
    while i < len(lines):
        raw = lines[i].strip()
        i += 1
        if not raw or raw.startswith(("#", "F ", "ID", "UNITS")):
            continue
        if raw.startswith("$"):
            p = raw.split()
            symbols[int(p[0][1:])] = Symbol(p[1])
            continue
        tok = raw.split()
        rec = tok[0]
        geom = None
        pol = "P"
        sym = None
        if rec == "L" and len(tok) >= 7:
            x1, y1, x2, y2 = map(float, tok[1:5])
            sym = symbols.get(int(tok[5]))
            pol = tok[6]
            if sym and sym.width > 0:
                geom = LineString([(x1, y1), (x2, y2)]).buffer(
                    sym.width / 2, cap_style="round", quad_segs=_QUAD_SEGS)
        elif rec == "P" and len(tok) >= 5:
            x, y = float(tok[1]), float(tok[2])
            sym = symbols.get(int(tok[3]))
            pol = tok[4]
            orient = _pad_orient(tok)
            if sym:
                geom = sym.flash(x, y, orient)
        elif rec == "S":
            pol = tok[1] if len(tok) > 1 else "P"
            geom, i = _read_surface(lines, i)
        else:
            continue
        yield Feature(index=idx, geom=geom, polarity=pol, rec=rec, sym=sym)
        idx += 1


# --------------------------------------------------------------------------- #
# API publique — miroir de geometry.py
# --------------------------------------------------------------------------- #
def _step_dir(job_root: Path, step: str | None = None) -> Path:
    steps = Path(job_root) / "steps"
    if step:
        return steps / step
    return next(p for p in steps.iterdir() if p.is_dir())


def load_copper(job_root: str | Path, layer: str, step: str | None = None) -> CopperLayer:
    feats = _step_dir(Path(job_root), step) / "layers" / layer / "features"
    _, pos, neg, _ = _read_features(feats)
    return CopperLayer(name=layer, geometry=_finalize(pos, neg))


def load_outline(job_root: str | Path, step: str | None = None) -> Polygon:
    prof = _step_dir(Path(job_root), step) / "profile"
    _, pos, neg, _ = _read_features(prof)
    geom = _finalize(pos, neg)
    if geom.is_empty:
        return Polygon()
    # Le contour est une surface pleine : on renvoie le plus grand polygone.
    return max(geom.geoms, key=lambda p: p.area)


def load_drill(job_root: str | Path, layer: str = "through_+_hole",
               step: str | None = None) -> list[Hole]:
    feats = _step_dir(Path(job_root), step) / "layers" / layer / "features"
    if not feats.exists():
        return []
    _, _, _, flashes = _read_features(feats)
    holes = []
    for x, y, sym in flashes:
        if sym.kind == "round":
            holes.append(Hole(x=x, y=y, diameter=sym.dims[0]))
    return holes


def load_features_by_net(job_root: str | Path, layer: str,
                         step: str | None = None) -> list[tuple[int, BaseGeometry, str, str | None]]:
    """Features cuivre positives d'un calque : (index, géométrie, net, composant).

    Base du DRC conscient des composants : permet de distinguer un chevauchement
    intra-composant (empreinte) d'un vrai court inter-composants.
    """
    from .netlist import load_net_map, load_component_index

    feats_path = _step_dir(Path(job_root), step) / "layers" / layer / "features"
    net_map = load_net_map(job_root, layer, step)
    match = load_component_index(job_root, layer, step)

    out = []
    for f in iter_features(feats_path):
        if f.geom is None or f.geom.is_empty or f.polarity != "P":
            continue
        net = net_map.get(f.index) or "$NONE$"
        comp = match(f.geom) if match else None
        out.append((f.index, f.geom, net, comp))
    return out


def load_labeled_copper(job_root: str | Path, layer: str,
                        step: str | None = None) -> LabeledCopper:
    """Charge un calque cuivre ODB++ regroupé par NET (via la netlist).

    Pads sans net (``$NONE$``) : regroupés par composante connexe puis isolés par
    défaut (chaque pavé isolé = son propre calque logique) — conformément au choix
    « isoler par défaut ». Sans netlist disponible : repli composantes connexes.
    """
    from .netlist import load_net_map  # import tardif : évite l'import circulaire

    feats_path = _step_dir(Path(job_root), step) / "layers" / layer / "features"
    net_map = load_net_map(job_root, layer, step)
    board = load_outline(job_root, step)

    pos_by_net: dict[str, list[BaseGeometry]] = defaultdict(list)
    negatives: list[BaseGeometry] = []
    for f in iter_features(feats_path):
        if f.geom is None or f.geom.is_empty:
            continue
        if f.polarity == "P":
            net = net_map.get(f.index) or "$NONE$"
            pos_by_net[net].append(f.geom)
        else:
            negatives.append(f.geom)

    if not net_map:
        # Pas de netlist : étiquetage par composante connexe.
        copper = unary_union([g for gs in pos_by_net.values() for g in gs])
        if negatives:
            copper = copper.difference(unary_union(negatives))
        return label_by_component(layer, copper, board)

    neg_union = unary_union(negatives) if negatives else None
    net_geoms: dict[str, BaseGeometry] = {}
    for net, geoms in pos_by_net.items():
        g = unary_union(geoms)
        if neg_union is not None:
            g = g.difference(neg_union)
        if net == "$NONE$":
            # isoler chaque pavé sans net séparément
            for k, comp in enumerate(g.geoms if g.geom_type == "MultiPolygon" else [g]):
                if not comp.is_empty:
                    net_geoms[f"$NONE${k}"] = comp
        elif not g.is_empty:
            net_geoms[net] = g
    return LabeledCopper(layer=layer, net_geoms=net_geoms, board=board, has_nets=True)
