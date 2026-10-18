"""Rangement automatique sur plateaux.

Trois exigences, dans cet ordre : rien ne sort du plateau, rien ne se
chevauche, et ce qui ne tient pas est SIGNALÉ plutôt qu'empilé dans un coin.
"""

import pytest
from shapely.strtree import STRtree

from laserpcb import arrange as ar
from laserpcb.drawing import KIND_RECT, Shape

BED_W, BED_H = 400.0, 415.0


def _rect(w, h, x=0.0, y=0.0, ident=""):
    return Shape(KIND_RECT, {"x": x, "y": y, "w": w, "h": h}, id=ident)


def _check(shapes, bed_w=BED_W, bed_h=BED_H):
    """Rien hors plateau, aucun chevauchement ENTRE PIÈCES.

    Entre pièces, pas entre formes : un perçage recouvre forcément son
    panneau — c'est justement ce qui en fait la même pièce.
    """
    from shapely.ops import unary_union
    for plate in range(ar.plate_count(shapes)):
        sur = ar.shapes_on(shapes, plate)
        for g in (s.geometry() for s in sur):
            minx, miny, maxx, maxy = g.bounds
            assert minx >= -1e-6 and miny >= -1e-6, f"déborde en bas à gauche : {g.bounds}"
            assert maxx <= bed_w + 1e-6 and maxy <= bed_h + 1e-6, \
                f"déborde du plateau : {g.bounds}"
        pieces = [unary_union([x.geometry() for x in grp])
                  for grp in ar.group_shapes(sur)]
        tree = STRtree(pieces)
        for i, g in enumerate(pieces):
            for j in tree.query(g):
                if int(j) != i:
                    assert pieces[int(j)].intersection(g).area < 1e-6, \
                        "deux pièces se chevauchent"


def test_quelques_pieces_tiennent_sur_un_plateau():
    # des pieces DISTINCTES : superposees, elles ne feraient qu'une (cf.
    # regroupement contour + percages)
    shapes = [_rect(100, 80, x=i * 150.0, ident=f"o{i}") for i in range(6)]
    res = ar.arrange(shapes, BED_W, BED_H, gap=3, margin=3)
    assert res["plates"] == 1 and res["placed"] == 6 and not res["oversized"]
    assert {s.plate for s in shapes} == {0}
    _check(shapes)


def test_le_debordement_ouvre_un_nouveau_plateau():
    shapes = [_rect(380, 190, y=i * 250.0, ident=f"o{i}") for i in range(5)]
    res = ar.arrange(shapes, BED_W, BED_H, gap=3, margin=3)
    assert res["plates"] >= 3
    assert ar.plate_count(shapes) == res["plates"]
    _check(shapes)


def test_la_rotation_sauve_une_piece_trop_large():
    """400,2 mm ne passe pas dans 400 ; tourné, il passe dans 415.

    C'est le cas reel des panneaux d'etagere : sans rotation, huit pieces
    seraient declarees trop grandes.
    """
    s = _rect(400.2, 330.2, ident="panneau")
    res = ar.arrange([s], BED_W, BED_H, gap=3, margin=3, allow_rotate=True)
    assert not res["oversized"]
    assert s.rotation % 180 == pytest.approx(90.0)
    _check([s])

    t = _rect(400.2, 330.2, ident="panneau")
    res = ar.arrange([t], BED_W, BED_H, gap=3, margin=3, allow_rotate=False)
    assert res["oversized"] == ["panneau"]


def test_une_piece_vraiment_trop_grande_est_signalee():
    shapes = [_rect(900, 900, ident="enorme"), _rect(50, 50, ident="petite")]
    res = ar.arrange(shapes, BED_W, BED_H)
    assert res["oversized"] == ["enorme"]
    assert res["placed"] == 2            # placée quand même, mais annoncée


def test_lecart_demande_est_respecte():
    shapes = [_rect(100, 100, x=i * 150.0, ident=f"o{i}") for i in range(3)]
    ar.arrange(shapes, BED_W, BED_H, gap=10.0, margin=5.0)
    xs = sorted(s.geometry().bounds[0] for s in shapes)
    assert xs[0] == pytest.approx(5.0)               # marge respectée
    assert xs[1] - xs[0] == pytest.approx(110.0)     # 100 de large + 10 d'écart


def test_la_marge_du_bord_est_respectee():
    shapes = [_rect(50, 50, ident="o1")]
    ar.arrange(shapes, BED_W, BED_H, margin=12.0)
    minx, miny, _mx, _my = shapes[0].geometry().bounds
    assert (minx, miny) == (pytest.approx(12.0), pytest.approx(12.0))


def test_les_pieces_sans_geometrie_sont_ignorees():
    vide = Shape(KIND_RECT, {"x": 0, "y": 0, "w": 0, "h": 0}, id="vide")
    bonne = _rect(40, 40, ident="o1")
    res = ar.arrange([vide, bonne], BED_W, BED_H)
    assert res["placed"] == 1


def test_comptage_et_filtrage_des_plateaux():
    shapes = [_rect(10, 10, ident="a"), _rect(10, 10, ident="b"),
              _rect(10, 10, ident="c")]
    shapes[1].plate = 2
    assert ar.plate_count(shapes) == 3
    assert [s.id for s in ar.shapes_on(shapes, 0)] == ["a", "c"]
    assert [s.id for s in ar.shapes_on(shapes, 2)] == ["b"]
    assert ar.shapes_on(shapes, 1) == []
    assert ar.plate_count([]) == 1       # un plateau vide reste un plateau


# ---- une piece = un contour + ses percages --------------------------------- #
def test_les_percages_suivent_leur_piece():
    """Le defaut signale par l'utilisateur : les trous partaient ailleurs.

    Un percage n'a aucun sens hors de sa piece ; contour et trous doivent se
    deplacer ensemble.
    """
    panneau = _rect(300, 200, x=0, y=0, ident="panneau")
    t1 = _rect(10, 10, x=20, y=20, ident="t1")
    t2 = _rect(10, 10, x=260, y=160, ident="t2")
    autre = _rect(120, 120, x=500, y=0, ident="autre")

    groupes = ar.group_shapes([panneau, t1, t2, autre])
    tailles = sorted(len(g) for g in groupes)
    assert tailles == [1, 3], "le panneau et ses deux trous forment une piece"

    ar.arrange([panneau, t1, t2, autre], BED_W, BED_H, gap=5, margin=5)
    pg = panneau.geometry()
    assert pg.contains(t1.geometry()), "le trou a quitte son panneau"
    assert pg.contains(t2.geometry())
    assert t1.plate == panneau.plate == t2.plate
    _check([panneau, t1, t2, autre])


def test_les_percages_suivent_la_rotation():
    """Quand la piece tourne, ses trous tournent AVEC elle, pas sur eux-memes."""
    panneau = _rect(400.2, 200, ident="panneau")      # ne passe qu'en tournant
    trou = _rect(20, 10, x=30, y=20, ident="trou")
    ar.arrange([panneau, trou], BED_W, BED_H, gap=3, margin=3)
    assert panneau.rotation % 180 == pytest.approx(90.0)
    assert panneau.geometry().contains(trou.geometry()),         "le trou est reste en arriere pendant la rotation"
    _check([panneau, trou])


def test_deux_pieces_qui_se_TOUCHENT_restent_distinctes():
    """Bord a bord n'est pas « appartenir » : sinon rien ne serait placable."""
    a = _rect(100, 100, x=0, y=0, ident="a")
    b = _rect(100, 100, x=100, y=0, ident="b")        # bords jointifs
    assert len(ar.group_shapes([a, b])) == 2


def test_percer_avant_de_detourer():
    """Une pièce détourée n'est plus tenue : elle glisse ou colle au laser.

    L'ordre doit donc être : intérieurs d'abord, contour extérieur en dernier.
    """
    contour = _rect(200, 100, ident="contour")
    t1 = _rect(10, 10, x=20, y=20, ident="t1")
    t2 = _rect(10, 10, x=160, y=70, ident="t2")
    ordre = [s.id for s in ar.cut_order([contour, t1, t2])]
    assert ordre[-1] == "contour"
    assert set(ordre[:2]) == {"t1", "t2"}


def test_lordre_de_decoupe_ne_melange_pas_deux_pieces():
    """Chaque pièce est finie avant de passer à la suivante."""
    a, a1 = _rect(100, 100, ident="a"), _rect(8, 8, x=10, y=10, ident="a1")
    b, b1 = _rect(100, 100, x=300, ident="b"), _rect(8, 8, x=310, y=10, ident="b1")
    ordre = [s.id for s in ar.cut_order([a, b1, a1, b])]
    assert ordre.index("a1") < ordre.index("a")
    assert ordre.index("b1") < ordre.index("b")
    # pas d'entrelacement
    bloc_a = [i for i, n in enumerate(ordre) if n in ("a", "a1")]
    assert bloc_a == sorted(bloc_a) and bloc_a[1] - bloc_a[0] == 1


def test_le_decor_sans_aire_suit_son_panneau():
    """Un tracé gravé n'a pas d'aire : sans règle dédiée, il partait seul."""
    from shapely.geometry import LineString
    panneau = _rect(200, 100, ident="panneau")
    trait = Shape("trace", {"wkt": LineString([(20, 50), (180, 50)]).wkt},
                  id="decor")
    groupes = ar.group_shapes([panneau, trait])
    assert len(groupes) == 1, "le décor doit rester avec sa façade"
    ar.arrange([panneau, trait], BED_W, BED_H, gap=5, margin=5)
    assert trait.plate == panneau.plate
    assert panneau.geometry().contains(trait.geometry())
