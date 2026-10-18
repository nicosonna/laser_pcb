"""Générateur de boîtes : assemblages, cotes, réglages et décor.

Ce qui doit être vrai pour qu'un meuble se monte :
les tenons d'un panneau remplissent exactement les encoches de son voisin, les
cotes extérieures annoncées sont celles de la boîte assemblée, et le décor est
GRAVÉ — jamais découpé.
"""

import pytest

from laserpcb import boxgen as bg


# ---- assemblage ------------------------------------------------------------ #
def test_tenons_et_encoches_sont_complementaires():
    """Ce qui dépasse d'un côté doit manquer exactement de l'autre."""
    t, f, L, H = 4.0, 8.0, 200.0, 100.0
    male = bg.panel(L, H, t, f, (bg.FINGERS, bg.FLAT, bg.FLAT, bg.FLAT))
    femelle = bg.panel(L, H, t, f, (bg.NOTCHES, bg.FLAT, bg.FLAT, bg.FLAT))
    nominal = L * H
    ajout = male.area - nominal
    retrait = nominal - femelle.area
    assert ajout > 0
    assert ajout == pytest.approx(retrait, rel=1e-9)


def test_le_motif_commence_et_finit_par_un_creux():
    """Les coins restent pleins : un tenon au ras du bord serait fragile."""
    pts = bg._profile(100.0, 4.0, 8.0, bg.FINGERS)
    assert pts[0] == (0.0, 0.0) and pts[-1] == (100.0, 0.0)
    assert all(abs(y) < 1e-9 for (_x, y) in (pts[0], pts[1], pts[-2], pts[-1]))


def test_le_motif_est_symetrique():
    """Symétrique, donc identique lu à l'envers : aucun sens de montage."""
    L = 137.0
    pts = bg._profile(L, 4.0, 9.0, bg.FINGERS)
    miroir = sorted(round(L - x, 6) for (x, _y) in pts)
    assert sorted(round(x, 6) for (x, _y) in pts) == miroir


def test_le_kerf_elargit_les_tenons_et_retrecit_les_encoches():
    """Sans compensation, le tenon sort trop petit et flotte dans son logement."""
    t, f, L = 4.0, 8.0, 100.0
    sans = bg.panel(L, 50, t, f, (bg.FINGERS, bg.FLAT, bg.FLAT, bg.FLAT), kerf=0.0)
    avec = bg.panel(L, 50, t, f, (bg.FINGERS, bg.FLAT, bg.FLAT, bg.FLAT), kerf=0.4)
    assert avec.area > sans.area                      # tenons élargis
    sans_n = bg.panel(L, 50, t, f, (bg.NOTCHES, bg.FLAT, bg.FLAT, bg.FLAT), kerf=0.0)
    avec_n = bg.panel(L, 50, t, f, (bg.NOTCHES, bg.FLAT, bg.FLAT, bg.FLAT), kerf=0.4)
    assert avec_n.area > sans_n.area                  # encoches rétrécies


def test_un_bord_plat_reste_droit():
    p = bg.panel(100, 50, 4, 8, (bg.FLAT, bg.FLAT, bg.FLAT, bg.FLAT))
    assert p.area == pytest.approx(100 * 50)
    assert p.bounds == (0.0, 0.0, 100.0, 50.0)


# ---- cotes ----------------------------------------------------------------- #
def test_les_cotes_exterieures_sont_respectees():
    """Les tenons comblent l'épaisseur du voisin : la boîte fait bien x × y × h."""
    x, y, h, t = 300.0, 200.0, 150.0, 4.0
    pieces = {p.name: p for p in bg.make_box(x=x, y=y, h=h, thickness=t, kerf=0)}
    fond_w, fond_h = pieces["fond"].size()
    assert fond_w == pytest.approx(x)          # nominal + tenons des deux côtés
    assert fond_h == pytest.approx(y)
    avant_w, avant_h = pieces["avant"].size()
    assert avant_w == pytest.approx(x)
    assert avant_h == pytest.approx(h)
    cote_w, _ = pieces["gauche"].size()
    assert cote_w == pytest.approx(y - 2 * t)  # les côtés s'insèrent entre


def test_une_boite_fermee_a_un_dessus():
    ouverte = {p.name for p in bg.make_box(closed=False)}
    fermee = {p.name for p in bg.make_box(closed=True)}
    assert "dessus" not in ouverte
    assert fermee - ouverte == {"dessus"}


def test_le_casier_a_une_tablette_de_plus_que_de_compartiments():
    pieces = bg.make_shelf(heights=(100, 120, 90), thickness=4)
    tablettes = [p for p in pieces if p.name.startswith("tablette")]
    assert len(tablettes) == 4
    cotes = [p for p in pieces if p.name.startswith("cote")]
    assert len(cotes) == 2
    # hauteur totale = compartiments + épaisseurs des tablettes
    assert cotes[0].size()[1] == pytest.approx(100 + 120 + 90 + 4 * 4)
    assert all(p.holes for p in cotes), "les côtés doivent porter les mortaises"


def test_larmoire_perce_la_charniere_aux_memes_hauteurs():
    """La charnière est métallique : seul l'alignement des trous compte."""
    pieces = {p.name: p for p in bg.make_wall_cabinet(
        x=400, y=200, h=300, thickness=4, hinge_holes=2, door=True)}
    porte = pieces["porte"]
    cote = pieces["gauche"]
    assert len(porte.holes) == 3            # 2 charnières + 1 poignée
    hauteurs_porte = sorted(round(t.centroid.y, 1) for t in porte.holes[:2])
    hauteurs_cote = sorted(round(t.centroid.y, 1) for t in cote.holes[-2:])
    assert hauteurs_porte == pytest.approx(hauteurs_cote, abs=2.0)


def test_le_bac_a_des_separateurs_a_mi_bois():
    pieces = bg.make_tray(cols=3, rows=2, thickness=4)
    seps = [p for p in pieces if p.name.startswith("separateur")]
    assert len(seps) == 3                   # (3-1) + (2-1)
    assert any(p.holes for p in seps), "les séparateurs se croisent par des fentes"


# ---- réglages laser --------------------------------------------------------- #
def test_lepaisseur_commande_les_passes():
    """Point mesuré par l'utilisateur : 4 mm = 5 passes à 80 %."""
    p = bg.cut_profile(4.0)
    assert p["passes"] == 5 and p["power"] == 80.0
    assert bg.cut_profile(2.0)["passes"] < p["passes"]
    assert bg.cut_profile(8.0)["passes"] > p["passes"]


def test_la_gravure_ne_traverse_pas():
    assert bg.engrave_profile(4.0)["power"] < bg.cut_profile(4.0)["power"]
    assert bg.engrave_profile(4.0)["passes"] == 1


# ---- décor ------------------------------------------------------------------ #
def test_le_decor_ne_va_que_sur_les_facades():
    pieces = bg.make_wall_cabinet(x=400, y=200, h=300, door=True)
    n = bg.apply_decor(pieces, motif="croisillons", text="ESSAI", target="facades")
    assert n == 2                                   # avant + porte
    for p in pieces:
        if p.name in bg.FACADES:
            assert p.decor
        else:
            assert not p.decor


def test_le_decor_part_sur_le_calque_de_gravure():
    """Le confondre avec la découpe ruinerait la façade."""
    pieces = bg.make_box(x=200, y=150, h=80)
    bg.apply_decor(pieces, motif="rayures", target="toutes")
    shapes = bg.to_shapes(pieces, layer_cut="coupe", layer_decor="grave")
    coupe = [s for s in shapes if s.layer == "coupe"]
    grave = [s for s in shapes if s.layer == "grave"]
    assert coupe and grave
    # un tracé de décor n'a pas d'aire : c'est une ligne, pas une découpe
    assert all(s.geometry().area == 0 for s in grave)


def test_un_motif_tient_dans_son_panneau():
    traits = bg.pattern("croisillons", 200.0, 100.0, step=15.0, margin=8.0)
    assert traits
    for ln in traits:
        minx, miny, maxx, maxy = ln.bounds
        assert minx >= 8.0 - 1e-9 and miny >= 8.0 - 1e-9
        assert maxx <= 192.0 + 1e-9 and maxy <= 92.0 + 1e-9


def test_un_panneau_trop_petit_ne_recoit_pas_de_motif():
    assert bg.pattern("croisillons", 10.0, 10.0, step=5.0, margin=8.0) == []


def test_la_mise_en_page_ne_superpose_rien():
    pieces = bg.layout(bg.make_wall_cabinet(), gap=5.0)
    from shapely.strtree import STRtree
    geoms = [p.outline for p in pieces]
    tree = STRtree(geoms)
    for i, g in enumerate(geoms):
        for j in tree.query(g):
            if int(j) != i:
                assert geoms[int(j)].intersection(g).area < 1e-6
