"""Génération de G-code GRBL pour le Creality Falcon 2.

Transforme des parcours (isolation offset, axe médian, hachures de négatif) en
programme G-code. Point fort : chaque mouvement peut porter **sa propre vitesse F
et sa propre puissance S**, ce qui fait vivre les paramètres adaptatifs calculés
dans ``toolpath`` — impossible avec un réglage constant par calque façon LightBurn.

Conventions Falcon 2 / GRBL :
- Mode laser requis côté firmware : ``$32=1`` (à régler une fois sur la machine).
- ``M4`` = puissance dynamique (le laser s'éteint à l'arrêt et pendant les G0, et
  la puissance suit la vitesse) → bords propres. ``M3`` = puissance constante.
- Puissance ``S`` de 0 à ``$30`` (1000 par défaut). Vitesse ``F`` en mm/min.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .toolpath import Calibration, Segment


@dataclass
class GCodeConfig:
    max_s: int = 1000            # puissance S pleine échelle ($30)
    laser_mode: str = "M4"       # "M4" (dynamique) ou "M3" (constant)
    travel_feed: float = 3000.0  # mm/min des déplacements (info ; on utilise G0)
    precision: int = 3           # décimales des coordonnées
    optimize_order: bool = True  # réordonne les parcours pour réduire les trajets
    max_optimize: int = 20000    # au-delà, on n'optimise pas (trop long)
    return_to_origin: bool = True  # G0 X0 Y0 en fin de programme
    emit_laser_mode_setting: bool = False  # écrire "$32=1" dans le fichier (voir build)


# --------------------------------------------------------------------------- #
# Conversion parcours -> items (coords, feed mm/min, power %)
# --------------------------------------------------------------------------- #
def _polyline_length(coords) -> float:
    a = np.asarray(coords)
    return float(np.hypot(*(a[1:] - a[:-1]).T).sum()) if len(a) >= 2 else 0.0


def items_from_paths(paths, speed: float, power: float, min_length: float = 1e-3):
    """Parcours à réglage constant (MultiLineString ou liste de LineString)."""
    lines = getattr(paths, "geoms", paths)
    out = []
    for line in lines:
        coords = list(line.coords)
        if len(coords) >= 2 and _polyline_length(coords) >= min_length:
            out.append((coords, speed, power))
    return out


def items_from_segments(segments: list[Segment], calib: Calibration,
                        min_length: float = 1e-3):
    """Segments d'axe médian -> vitesse/puissance adaptatives par segment."""
    out = []
    for s in segments:
        if len(s.coords) >= 2 and _polyline_length(s.coords) >= min_length:
            v, p = calib.params(s.clearance)
            out.append((s.coords, v, p))
    return out


# --------------------------------------------------------------------------- #
# Optimisation de l'ordre (plus proche voisin, vectorisé)
# --------------------------------------------------------------------------- #
def optimize(items, max_nn: int = 20000, band: float = 2.0):
    """Réordonne les parcours pour réduire les trajets à vide.

    - Petit nombre de parcours : plus-proche-voisin (avec inversion) — quasi optimal.
    - Grand nombre : tri **boustrophédon** (serpentin par bandes horizontales,
      direction alternée) en O(n log n) — bien plus rapide que le PPV O(n²) et déjà
      très efficace pour réduire les déplacements sur une carte complète.
    """
    n = len(items)
    if n <= 1:
        return items
    if n > max_nn:
        return _boustrophedon(items, band)
    return _greedy_nearest(items)


def _greedy_nearest(items):
    n = len(items)
    starts = np.array([it[0][0] for it in items], dtype=float)
    ends = np.array([it[0][-1] for it in items], dtype=float)
    remaining = np.ones(n, dtype=bool)
    order = []
    cur = np.array([0.0, 0.0])
    for _ in range(n):
        ds = np.where(remaining, ((starts - cur) ** 2).sum(1), np.inf)
        de = np.where(remaining, ((ends - cur) ** 2).sum(1), np.inf)
        i_s = int(ds.argmin()); i_e = int(de.argmin())
        if de[i_e] < ds[i_s]:
            idx = i_e; coords = items[idx][0][::-1]   # on parcourt à l'envers
        else:
            idx = i_s; coords = items[idx][0]
        remaining[idx] = False
        order.append((coords, items[idx][1], items[idx][2]))
        cur = np.array(coords[-1])
    return order


def _boustrophedon(items, band: float):
    """Serpentin : bandes en Y, alternant le sens en X à chaque bande."""
    def key(item):
        x, y = item[0][0]
        b = int(y // band)
        return (b, x if b % 2 == 0 else -x)
    return sorted(items, key=key)


# --------------------------------------------------------------------------- #
# Émission du programme
# --------------------------------------------------------------------------- #
_EPS = 5e-4  # mm : en deçà, un déplacement/coupe est considéré nul


class _Emitter:
    def __init__(self, config: GCodeConfig):
        self.c = config
        self.lines: list[str] = []
        self._f = None
        self._s = None
        self._x = None
        self._y = None

    def _n(self, v):  # format compact d'une coordonnée
        return f"{v:.{self.c.precision}f}"

    def _at(self, x, y) -> bool:
        return (self._x is not None
                and abs(x - self._x) < _EPS and abs(y - self._y) < _EPS)

    def move_rapid(self, x, y):
        if self._at(x, y):
            return  # déjà à cet endroit (segments jointifs) -> pas de G0 inutile
        self.lines.append(f"G0 X{self._n(x)} Y{self._n(y)}")
        self._x, self._y = x, y

    def cut(self, coords, feed, power):
        s = int(round(power / 100.0 * self.c.max_s))
        for (x, y) in coords[1:]:
            if self._at(x, y):
                continue  # coupe de longueur nulle -> ignorée
            parts = [f"G1 X{self._n(x)} Y{self._n(y)}"]
            if feed != self._f:
                parts.append(f"F{feed:.0f}"); self._f = feed
            if s != self._s:
                parts.append(f"S{s}"); self._s = s
            self.lines.append(" ".join(parts))
            self._x, self._y = x, y


def build(items, config: GCodeConfig | None = None, passes: int = 1,
          title: str = "laserpcb job", prelude_items=None) -> str:
    """Construit le programme G-code complet à partir d'items (coords, feed, power).

    :param prelude_items: items gravés AVANT le job, suivis d'une pause ``M0``
        (marquage du pourtour : on grave la zone, on pose le PCB, on reprend).
    """
    config = config or GCodeConfig()
    if config.optimize_order:
        items = optimize(items, config.max_optimize)

    em = _Emitter(config)
    em.lines += [
        f"; {title}",
        "; Genere par laserpcb pour Creality Falcon 2 (GRBL)",
        "G21", "G90", "G94",              # mm, absolu, vitesse en unites/min
    ]
    # $32=1 (mode laser) est un REGLAGE MACHINE a faire UNE FOIS. Ecrit dans un
    # fichier de job, il fait planter l'apercu/frame de nombreux streamers
    # (LightBurn en import G-code, LaserGRBL...) -> "frame minuscule". Off par defaut.
    if config.emit_laser_mode_setting:
        em.lines.append("$32=1")
    em.lines.append(f"{config.laser_mode} S0")   # arme le laser, eteint

    if prelude_items:
        # Pourtour d'abord : grave la zone ou poser le PCB, puis PAUSE (M0).
        # L'operateur pose la plaque dans la zone marquee et appuie sur Reprendre.
        em.lines.append("; --- pourtour : gravure de la zone du PCB ---")
        for coords, feed, power in prelude_items:
            em.move_rapid(*coords[0])
            em.cut(coords, feed, power)
        em.lines.append("M5")
        em.lines.append("M0 ; placez le PCB dans la zone marquee puis Reprendre")
        em.lines.append(f"{config.laser_mode} S0")
        em._s = 0                          # forcer la re-emission de S apres la pause
    for p in range(passes):
        if passes > 1:
            em.lines.append(f"; --- passe {p + 1}/{passes} ---")
        for coords, feed, power in items:
            em.move_rapid(*coords[0])     # trajet laser eteint (G0)
            em.cut(coords, feed, power)
    em.lines.append("M5")                 # laser coupe
    if config.return_to_origin:
        em.lines.append("G0 X0 Y0")       # retour origine
    em.lines.append("M2")                 # fin de programme
    return "\n".join(em.lines) + "\n"


# --------------------------------------------------------------------------- #
# Estimations
# --------------------------------------------------------------------------- #
def cut_length(items) -> float:
    """Longueur totale gravée (mm)."""
    total = 0.0
    for coords, _f, _p in items:
        a = np.asarray(coords)
        total += float(np.hypot(*(a[1:] - a[:-1]).T).sum())
    return total


def estimate_minutes(items, passes: int = 1) -> float:
    """Temps de gravure approx (min), hors trajets — somme des longueurs / vitesses."""
    t = 0.0
    for coords, feed, _p in items:
        a = np.asarray(coords)
        length = float(np.hypot(*(a[1:] - a[:-1]).T).sum())
        if feed > 0:
            t += length / feed
    return t * passes


def estimate_total_minutes(items, travel_feed: float = 3000.0,
                           optimize_order: bool = True,
                           max_optimize: int = 20000) -> float:
    """Temps total approx (min) **incluant les déplacements à vide (G0)**.

    Estime dans l'ordre optimisé (comme le vrai fichier) : temps de coupe
    (longueur/vitesse) + temps de trajet entre parcours (distance/vitesse rapide)."""
    if optimize_order:
        items = optimize(items, max_optimize)
    t = 0.0
    prev = None
    for coords, feed, _p in items:
        if len(coords) < 2:
            continue
        if prev is not None and travel_feed > 0:
            t += float(np.hypot(coords[0][0] - prev[0], coords[0][1] - prev[1])) / travel_feed
        a = np.asarray(coords)
        length = float(np.hypot(*(a[1:] - a[:-1]).T).sum())
        if feed > 0:
            t += length / feed
        prev = coords[-1]
    return t


def format_duration(minutes: float) -> str:
    """Durée lisible : « 45 s », « 12,4 min », « 1 h 23 min »."""
    if minutes < 1:
        return f"{minutes * 60:.0f} s"
    h = int(minutes // 60)
    m = minutes - h * 60
    if h:
        return f"{h} h {m:.0f} min"
    return f"{m:.1f} min".replace(".", ",")
