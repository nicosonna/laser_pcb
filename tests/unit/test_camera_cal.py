"""Unites : correspondance pixel <-> mm de la camera de tete.

Methode : on se donne une calibration VRAIE, on fabrique ce que la camera
verrait d'un detail fixe pendant que la tete bouge, et on verifie que
``solve`` retrouve la verite. C'est le seul moyen de tester une calibration
sans machine : un test qui se contenterait de relire ce que le code a ecrit
ne prouverait rien.
"""

from __future__ import annotations

import math

import pytest

from laserpcb.camera_cal import (CalibrationError, PixelMm, solve, validate,
                                 warnings_for)


def _vraie(mm_px=0.12, angle=0.0, miroir=False, u0=400.0, v0=300.0,
           w=800, h=600) -> PixelMm:
    """Calibration de reference, construite depuis echelle/rotation/miroir."""
    t = math.radians(angle)
    a, b = mm_px * math.cos(t), -mm_px * math.sin(t)
    c, d = mm_px * math.sin(t), mm_px * math.cos(t)
    if miroir:
        a, c = -a, -c
    return PixelMm(u0, v0, a, b, c, d, w, h)


def _vue(cal: PixelMm, detail_mm: tuple, deplacements: list) -> list:
    """Pointes qu'un operateur releverait : pour chaque deplacement cumule de
    la tete, le pixel ou apparait un detail FIXE de la piece.

    Le detail est a ``detail_mm`` du tir initial ; la tete ayant bouge de
    ``D``, il se retrouve a ``detail_mm - D`` du tir.
    """
    return [(d, cal.pixel_of(detail_mm[0] - d[0], detail_mm[1] - d[1]))
            for d in deplacements]


DEPL = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def test_solve_retrouve_une_calibration_simple():
    vrai = _vraie(mm_px=0.12)
    got = solve((vrai.u0, vrai.v0), _vue(vrai, (3.0, -2.0), DEPL), (800, 600))
    for nom in ("a", "b", "c", "d"):
        assert getattr(got, nom) == pytest.approx(getattr(vrai, nom), abs=1e-9)
    assert got.mm_per_px()[0] == pytest.approx(0.12)


def test_solve_retrouve_une_camera_tournee():
    """Une camera vissee a la main n'est jamais d'aplomb : si la rotation
    n'etait pas captee, un clic en haut a droite enverrait la tete de biais."""
    vrai = _vraie(mm_px=0.09, angle=7.5)
    got = solve((vrai.u0, vrai.v0), _vue(vrai, (-4.0, 6.0), DEPL), (800, 600))
    assert got.rotation_deg() == pytest.approx(7.5, abs=1e-6)
    assert abs(got.skew_deg()) < 1e-6
    assert not got.mirrored()


def test_solve_retrouve_une_image_en_miroir():
    """``hmirror`` du capteur inverse l'image : sans le determinant negatif,
    la tete partirait du cote oppose au clic."""
    vrai = _vraie(mm_px=0.1, angle=3.0, miroir=True)
    got = solve((vrai.u0, vrai.v0), _vue(vrai, (5.0, 5.0), DEPL), (800, 600))
    assert got.mirrored()
    assert got.offset_mm(vrai.u0 + 100, vrai.v0)[0] == \
        pytest.approx(vrai.offset_mm(vrai.u0 + 100, vrai.v0)[0])


def test_le_sens_du_deplacement_est_le_bon():
    """La tete avance, le detail reste : il RECULE dans l'image. Un signe
    inverse ici, et le clic-pour-aller eloignerait la tete de la cible."""
    vrai = _vraie(mm_px=0.1)                   # 1 mm = 10 px, sans rotation
    obs = _vue(vrai, (0.0, 0.0), [(0.0, 0.0), (10.0, 0.0)])
    avant, apres = obs[0][1], obs[1][1]
    assert apres[0] == pytest.approx(avant[0] - 100.0), "doit reculer de 100 px"
    assert apres[1] == pytest.approx(avant[1])


def test_plus_de_trois_pointes_restent_exacts():
    """Les pointes en trop moyennent l'imprecision du clic ; sur des donnees
    parfaites elles ne doivent rien degrader."""
    vrai = _vraie(mm_px=0.11, angle=-4.0)
    depl = DEPL + [(0.0, 10.0), (-5.0, 5.0)]
    got = solve((vrai.u0, vrai.v0), _vue(vrai, (2.0, 2.0), depl), (800, 600))
    assert got.a == pytest.approx(vrai.a, abs=1e-9)
    assert got.d == pytest.approx(vrai.d, abs=1e-9)


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #
def test_le_point_de_tir_est_a_zero_mm():
    cal = _vraie()
    assert cal.offset_mm(cal.u0, cal.v0) == pytest.approx((0.0, 0.0))


def test_pixel_of_est_la_reciproque():
    cal = _vraie(mm_px=0.08, angle=11.0)
    for dx, dy in ((0.0, 0.0), (12.5, -3.25), (-40.0, 18.0)):
        u, v = cal.pixel_of(dx, dy)
        assert cal.offset_mm(u, v) == pytest.approx((dx, dy), abs=1e-9)


def test_changer_de_taille_dimage_ne_casse_pas_la_calibration():
    """Le vrai piege : passer de SVGA a XGA multiplie tous les pixels par
    1,28. Sans ramener a la taille de reference, la tete irait 28 % trop
    loin — et rien a l'ecran ne le dirait."""
    cal = _vraie(mm_px=0.12, w=800, h=600)
    u, v = 500.0, 400.0                        # un point physique, vu en SVGA
    attendu = cal.offset_mm(u, v)
    # Le meme point physique, vu en XGA (1024x768) : 1,28x plus loin en pixels.
    k = 1024 / 800
    got = cal.offset_mm(u * k, v * k, 1024, 768)
    assert got == pytest.approx(attendu, abs=1e-9)
    # Et la reciproque suit la meme regle.
    assert cal.pixel_of(*attendu, 1024, 768) == pytest.approx((u * k, v * k))


def test_le_reticule_se_deduit_de_la_calibration():
    """La vue ancre sa croix sur le centre de l'image : la calibration doit
    lui livrer un decalage, pas une position absolue."""
    cal = _vraie(u0=440.0, v0=280.0, w=800, h=600)
    assert cal.cross_offset() == pytest.approx((40.0, -20.0))
    assert _vraie(u0=400.0, v0=300.0).cross_offset() == pytest.approx((0.0, 0.0))


def test_summary_dit_lechelle_et_la_rotation():
    txt = _vraie(mm_px=0.125, angle=5.0).summary()
    assert "0.1250" in txt and "+5.0" in txt and "800×600" in txt
    assert "miroir" in _vraie(miroir=True).summary()


# --------------------------------------------------------------------------- #
# Refus
# --------------------------------------------------------------------------- #
def test_moins_de_trois_pointes_est_refuse():
    with pytest.raises(CalibrationError, match="trois pointés"):
        solve((400, 300), [((0, 0), (1, 1)), ((10, 0), (2, 2))], (800, 600))


def test_deux_pointes_identiques_sont_refuses():
    """Cliquer deux fois au meme endroit (tete pas bougee, ou clic distrait)
    ne donne aucune information : mieux vaut le dire que calculer n'importe
    quoi."""
    obs = [((0, 0), (400, 300)), ((10, 0), (400, 300)), ((10, 10), (400, 300))]
    with pytest.raises(CalibrationError, match="alignés ou nuls"):
        solve((400, 300), obs, (800, 600))


def test_des_deplacements_alignes_sont_refuses():
    """Bouger deux fois dans le meme sens ne donne qu'un axe sur deux."""
    vrai = _vraie()
    obs = _vue(vrai, (0.0, 0.0), [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)])
    with pytest.raises(CalibrationError, match="alignés ou nuls"):
        solve((vrai.u0, vrai.v0), obs, (800, 600))


def test_une_echelle_invraisemblable_est_refusee():
    """10 mm de tete pour 1 pixel d'image : c'est un pointe rate, pas une
    optique exotique. Accepter donnerait une tete qui part a l'autre bout du
    plateau au premier clic."""
    obs = [((0, 0), (400.0, 300.0)),
           ((10, 0), (399.0, 300.0)),
           ((10, 10), (399.0, 299.0))]
    with pytest.raises(CalibrationError, match="Échelle invraisemblable"):
        solve((400, 300), obs, (800, 600))


def test_validate_refuse_une_matrice_degeneree():
    with pytest.raises(CalibrationError, match="degénérée"):
        validate(PixelMm(0, 0, 0.1, 0.1, 0.1, 0.1, 800, 600))


# --------------------------------------------------------------------------- #
# Doutes signales sans refus
# --------------------------------------------------------------------------- #
def test_des_axes_hors_dequerre_sont_signales():
    """Un cisaillement n'existe pas sur une vraie optique : c'est le signe
    d'un pointe a cote. On le dit, mais on laisse l'utilisateur juger."""
    biais = PixelMm(400, 300, 0.1, 0.03, 0.0, 0.1, 800, 600)
    msgs = warnings_for(biais)
    assert any("équerre" in m for m in msgs), msgs
    validate(biais)                            # utilisable malgre tout


def test_des_echelles_tres_differentes_sont_signalees():
    msgs = warnings_for(PixelMm(400, 300, 0.2, 0.0, 0.0, 0.1, 800, 600))
    assert any("échelles X et Y" in m for m in msgs), msgs


def test_une_calibration_propre_ne_signale_rien():
    assert warnings_for(_vraie(mm_px=0.1, angle=6.0)) == []


def test_signaler_ne_leve_jamais_sur_une_matrice_degeneree():
    """``warnings_for`` passe AVANT ``validate`` dans l'assistant : il doit
    survivre a une colonne nulle, pas lever a sa place. C'est ``validate``
    qui refuse."""
    plat = PixelMm(0, 0, 0.0, 0.0, 0.0, 0.1, 800, 600)
    assert plat.skew_deg() == 0.0
    assert warnings_for(plat) == []
    with pytest.raises(CalibrationError):
        validate(plat)


# --------------------------------------------------------------------------- #
# Persistance
# --------------------------------------------------------------------------- #
def test_aller_retour_par_dictionnaire():
    cal = _vraie(mm_px=0.0975, angle=-2.5, u0=411.0, v0=295.5)
    relu = PixelMm.from_dict(cal.to_dict())
    assert relu == cal


def test_une_calibration_tronquee_est_refusee():
    """Une calibration a demi relue est pire que pas de calibration : la tete
    irait quelque part, mais pas la ou on a clique."""
    raw = _vraie().to_dict()
    del raw["ref_w"]
    with pytest.raises(CalibrationError, match="illisible"):
        PixelMm.from_dict(raw)

    raw = _vraie().to_dict()
    raw["a"] = "pas un nombre"
    with pytest.raises(CalibrationError, match="illisible"):
        PixelMm.from_dict(raw)


def test_une_calibration_memorisee_absurde_est_refusee():
    """Un reglage edite a la main, ou une version ancienne du format, ne doit
    pas pouvoir piloter la machine."""
    raw = _vraie().to_dict()
    raw["a"] = raw["d"] = 50.0                 # 50 mm/px
    with pytest.raises(CalibrationError, match="Échelle invraisemblable"):
        PixelMm.from_dict(raw)
