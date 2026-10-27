"""Correspondance pixel <-> mm pour la camera de tete.

La camera est vissee sur la tete : l'image et le laser se deplacent ensemble.
Un pixel de l'image correspond donc toujours au meme ecart en mm par rapport
au point de tir, et cet ecart est **affine** :

    [dx]   [a  b] [u - u0]
    [dy] = [c  d] [v - v0]

Les quatre coefficients portent a la fois l'echelle (mm par pixel), la
rotation de la camera par rapport aux axes de la machine, et un eventuel
miroir (``hmirror``/``vflip`` du capteur). Ecrire plutot « echelle + angle »
serait plus lisible, mais supposerait les pixels carres et la camera posee
d'aplomb -- ce qu'une camera vissee a la main n'est jamais.

Les deux inconnues se mesurent **separement**, et c'est ce qui rend la
calibration courte :

* ``(u0, v0)``, le pixel ou le laser tire, se lit au pointeur basse
  puissance : c'est le seul point de l'image dont on connaisse la position en
  mm sans rien supposer.
* la matrice se mesure sans jamais savoir ou se trouve le repere vise. On
  regarde un detail fixe de la piece se deplacer dans l'image pendant que la
  tete bouge d'une distance connue ; les differences eliminent la position
  inconnue du detail. Comme la camera avance et que le detail reste, le
  deplacement vu est l'oppose de celui de la tete -- d'ou les signes.

**Piege a eviter** : les coordonnees pixel dependent de la taille d'image.
Passer de SVGA a XGA multiplie tous les ``u, v`` par 1,28, et une calibration
prise en SVGA deviendrait fausse en silence. On memorise donc la taille de
reference, et chaque pixel y est ramene avant calcul.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

# Garde-fous : en dehors, ce n'est pas une calibration mais une erreur de
# pointe. 0,002 mm/px serait un microscope, 2 mm/px une camera a 2 m du
# plateau -- ni l'un ni l'autre n'est ce montage.
MM_PER_PX_MIN = 0.002
MM_PER_PX_MAX = 2.0

# Au-dela, un des pointes est manifestement a cote du repere.
SKEW_MAX_DEG = 10.0
SCALE_RATIO_MAX = 1.25


class CalibrationError(ValueError):
    """Donnees de calibration inexploitables.

    Le message est destine a l'ecran : il dit quoi refaire, pas ce qui a
    mathematiquement echoue.
    """


@dataclass(frozen=True)
class PixelMm:
    """Calibration pixel -> mm d'un montage donne.

    ``u0, v0`` : pixel du tir. ``a, b, c, d`` : la matrice. ``ref_w, ref_h`` :
    taille d'image a laquelle tout cela a ete mesure.
    """

    u0: float
    v0: float
    a: float
    b: float
    c: float
    d: float
    ref_w: int
    ref_h: int

    # ---- usage ------------------------------------------------------------- #
    def offset_mm(self, u: float, v: float,
                  img_w: int | None = None, img_h: int | None = None) -> tuple:
        """Ecart en mm, depuis le point de tir, du pixel ``(u, v)``."""
        u, v = self._to_ref(u, v, img_w, img_h)
        du, dv = u - self.u0, v - self.v0
        return (self.a * du + self.b * dv, self.c * du + self.d * dv)

    def pixel_of(self, dx: float, dy: float,
                 img_w: int | None = None, img_h: int | None = None) -> tuple:
        """Reciproque : pixel d'un ecart en mm. Sert a dessiner une cible."""
        det = self.a * self.d - self.b * self.c
        u = self.u0 + (self.d * dx - self.b * dy) / det
        v = self.v0 + (-self.c * dx + self.a * dy) / det
        return self._from_ref(u, v, img_w, img_h)

    def with_spot(self, u: float, v: float,
                  img_w: int | None = None, img_h: int | None = None) -> "PixelMm":
        """Meme matrice, nouveau point de tir.

        Les deux inconnues de la calibration ne vieillissent pas au meme
        rythme. La matrice tient a l'optique et a l'angle de la camera sur son
        support ; le point de tir, lui, tient a sa POSITION par rapport a la
        buse — et c'est cela qu'un coup de coude, un demontage de lentille ou
        une vis reprise deplacent. Le refaire coute un clic, contre quatre
        pointes et deux deplacements de tete pour tout reprendre.

        La taille d'image est ramenee a celle de la mesure d'origine : etalonner
        en XGA une calibration prise en SVGA doit rester juste.
        """
        u, v = self._to_ref(u, v, img_w, img_h)
        return replace(self, u0=u, v0=v)

    def _to_ref(self, u, v, img_w, img_h) -> tuple:
        if img_w and img_h and (img_w != self.ref_w or img_h != self.ref_h):
            return (u * self.ref_w / img_w, v * self.ref_h / img_h)
        return (float(u), float(v))

    def _from_ref(self, u, v, img_w, img_h) -> tuple:
        if img_w and img_h and (img_w != self.ref_w or img_h != self.ref_h):
            return (u * img_w / self.ref_w, v * img_h / self.ref_h)
        return (float(u), float(v))

    # ---- lecture humaine --------------------------------------------------- #
    def mm_per_px(self) -> tuple:
        """Echelle sur chaque axe de l'image (norme des colonnes)."""
        return (math.hypot(self.a, self.c), math.hypot(self.b, self.d))

    def rotation_deg(self) -> float:
        """Rotation de la camera par rapport aux axes machine."""
        return math.degrees(math.atan2(self.c, self.a))

    def skew_deg(self) -> float:
        """Ecart a l'angle droit entre les deux axes de l'image.

        Une camera reelle n'a pas de cisaillement : une valeur non nulle
        denonce un pointe approximatif, pas une propriete de l'optique.
        """
        su, sv = self.mm_per_px()
        if su <= 0 or sv <= 0:
            return 0.0
        cos = (self.a * self.b + self.c * self.d) / (su * sv)
        return math.degrees(math.acos(max(-1.0, min(1.0, cos)))) - 90.0

    def mirrored(self) -> bool:
        """Vrai si l'image est en miroir (determinant negatif)."""
        return (self.a * self.d - self.b * self.c) < 0

    def cross_offset(self) -> tuple:
        """Decalage du reticule, en pixels depuis le CENTRE de l'image.

        C'est la forme attendue par la vue, qui ancre sa croix sur le centre.
        """
        return (self.u0 - self.ref_w / 2.0, self.v0 - self.ref_h / 2.0)

    def summary(self) -> str:
        su, sv = self.mm_per_px()
        miroir = ", miroir" if self.mirrored() else ""
        return (f"{su:.4f} × {sv:.4f} mm/px, rotation {self.rotation_deg():+.1f}°"
                f"{miroir} — mesurée en {self.ref_w}×{self.ref_h}")

    # ---- persistance ------------------------------------------------------- #
    def to_dict(self) -> dict:
        return {"u0": self.u0, "v0": self.v0, "a": self.a, "b": self.b,
                "c": self.c, "d": self.d, "ref_w": self.ref_w,
                "ref_h": self.ref_h}

    @classmethod
    def from_dict(cls, raw: dict) -> "PixelMm":
        """Relit une calibration memorisee. Leve si elle est incomplete.

        Une calibration a demi relue serait pire que pas de calibration : la
        tete irait quelque part, mais pas la ou on a clique.
        """
        try:
            cal = cls(float(raw["u0"]), float(raw["v0"]),
                      float(raw["a"]), float(raw["b"]),
                      float(raw["c"]), float(raw["d"]),
                      int(raw["ref_w"]), int(raw["ref_h"]))
        except (KeyError, TypeError, ValueError) as e:
            raise CalibrationError(f"Calibration illisible : {e}") from e
        validate(cal)
        return cal


def _solve2(p: list, r: list) -> list:
    """Moindres carres de ``p · X = r``, avec ``X`` 2x2.

    Deux observations suffisent et donnent la solution exacte ; au-dela, les
    pointes supplementaires moyennent l'imprecision du clic.
    """
    sxx = sum(px * px for px, _ in p)
    sxy = sum(px * py for px, py in p)
    syy = sum(py * py for _, py in p)
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-9:
        raise CalibrationError(
            "Les déplacements vus dans l'image sont alignés ou nuls : "
            "bougez bien une fois en X et une fois en Y, et pointez le même "
            "détail à chaque fois.")
    bx = (sum(px * rx for (px, _), (rx, _ry) in zip(p, r)),
          sum(py * rx for (_px, py), (rx, _ry) in zip(p, r)))
    by = (sum(px * ry for (px, _), (_rx, ry) in zip(p, r)),
          sum(py * ry for (_px, py), (_rx, ry) in zip(p, r)))
    return [[(syy * bx[0] - sxy * bx[1]) / det,
             (syy * by[0] - sxy * by[1]) / det],
            [(-sxy * bx[0] + sxx * bx[1]) / det,
             (-sxy * by[0] + sxx * by[1]) / det]]


def validate(cal: PixelMm) -> None:
    """Refuse une calibration qui ne peut pas decrire ce montage."""
    if abs(cal.a * cal.d - cal.b * cal.c) < 1e-12:
        raise CalibrationError(
            "Calibration degénérée : les deux déplacements n'ont pas donné "
            "deux directions distinctes dans l'image.")
    su, sv = cal.mm_per_px()
    for val in (su, sv):
        if not (MM_PER_PX_MIN <= val <= MM_PER_PX_MAX):
            raise CalibrationError(
                f"Échelle invraisemblable ({val:.4f} mm/px) : le détail pointé "
                f"n'est probablement pas le même d'un pointé à l'autre, ou le "
                f"pas de déplacement ne correspond pas à ce que la tête a fait.")


def warnings_for(cal: PixelMm) -> list:
    """Doutes a afficher sans refuser : la calibration reste utilisable."""
    out = []
    if abs(cal.skew_deg()) > SKEW_MAX_DEG:
        out.append(f"les axes de l'image ne sont pas à l'équerre "
                   f"({cal.skew_deg():+.1f}°) — un pointé est sans doute "
                   f"à côté du détail")
    su, sv = cal.mm_per_px()
    ratio = max(su, sv) / min(su, sv) if min(su, sv) > 0 else 0.0
    if ratio > SCALE_RATIO_MAX:
        out.append(f"les échelles X et Y diffèrent de {ratio:.2f}× — "
                   f"inattendu sur des pixels carrés")
    return out


def solve(spot: tuple, obs: list, img_size: tuple) -> PixelMm:
    """Construit la calibration a partir des pointes de l'assistant.

    ``spot`` : pixel du tir, releve au pointeur.
    ``obs``  : liste de ``((Mx, My), (u, v))`` -- deplacement cumule de la
    tete, et pixel du MEME detail fixe a ce moment-la. Trois entrees au
    minimum, la premiere servant de reference.
    ``img_size`` : taille de l'image pendant la mesure.
    """
    if len(obs) < 3:
        raise CalibrationError(
            "Il faut trois pointés du même détail : un avant de bouger, un "
            "après le déplacement en X, un après celui en Y.")
    (d0, q0) = obs[0]
    p, r = [], []
    for (d, q) in obs[1:]:
        p.append((q[0] - q0[0], q[1] - q0[1]))
        # La tete avance, le detail reste : vu de l'image, il recule.
        r.append((-(d[0] - d0[0]), -(d[1] - d0[1])))
    x = _solve2(p, r)
    cal = PixelMm(float(spot[0]), float(spot[1]),
                  x[0][0], x[1][0], x[0][1], x[1][1],
                  int(img_size[0]), int(img_size[1]))
    validate(cal)
    return cal
