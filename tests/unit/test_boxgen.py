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
    # Les côtés portent eux aussi des tenons d'angle, en alternance avec ceux
    # de l'avant : ils couvrent donc TOUTE la profondeur. Avant correction ils
    # s'arrêtaient à y-2t, et l'arête verticale ne s'emboîtait pas.
    cote_w, cote_h = pieces["gauche"].size()
    assert cote_w == pytest.approx(y)
    assert cote_h == pytest.approx(h)


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
def test_le_modele_colle_a_la_mesure_reelle():
    """Mesure de l'utilisateur : 4 mm traversés en 1 passe à 80 %, 300 mm/min.

    Sans marge, le modèle doit redonner exactement ce point. C'est lui qui
    ancre tout le reste.
    """
    p = bg.cut_profile(4.0, passes=1, power=80.0, marge=1.0)
    assert p["speed"] == pytest.approx(300.0, rel=0.01)


def test_la_dose_ne_depend_pas_du_nombre_de_passes():
    """Ce qui traverse, c'est l'énergie par mm : passes × puissance / vitesse.

    À énergie égale, plus de passes = plus vite à chaque passe.
    """
    doses = []
    for n in (1, 2, 3, 6, 10):
        p = bg.cut_profile(4.0, passes=n)
        doses.append(bg.cut_dose(p["passes"], p["power"], p["speed"]))
        assert p["passes"] == n
    for d in doses[1:]:
        assert d == pytest.approx(doses[0], rel=0.01)


def test_plus_de_passes_donne_une_vitesse_plus_grande():
    lent = bg.cut_profile(4.0, passes=2)["speed"]
    rapide = bg.cut_profile(4.0, passes=6)["speed"]
    assert rapide == pytest.approx(lent * 3, rel=0.01)


def test_plus_epais_donne_une_vitesse_plus_faible():
    for a, b in ((3.0, 4.0), (4.0, 8.0)):
        assert bg.cut_profile(b, passes=3)["speed"] <             bg.cut_profile(a, passes=3)["speed"]


def test_la_marge_ralentit_la_decoupe():
    """Viser pile le seuil laisserait des zones non traversées."""
    juste = bg.cut_profile(4.0, passes=3, marge=1.0)["speed"]
    sur = bg.cut_profile(4.0, passes=3, marge=1.25)["speed"]
    assert sur < juste
    assert sur == pytest.approx(juste / 1.25, rel=0.01)


def test_on_peut_se_recalibrer_sur_sa_propre_decoupe():
    """Le bois varie : la dose mesurée doit pouvoir remplacer la mienne."""
    mesure = bg.cut_dose(passes=2, power=70.0, speed=500.0)
    assert mesure == pytest.approx(2 * 70.0 / 500.0)
    dose_mm = mesure / 5.0                       # essai sur 5 mm
    p = bg.cut_profile(5.0, passes=2, power=70.0, dose_par_mm=dose_mm, marge=1.0)
    assert p["speed"] == pytest.approx(500.0, rel=0.01)


def test_la_vitesse_reste_dans_le_possible():
    """Une dose minuscule ne doit pas réclamer 50 000 mm/min à la machine."""
    p = bg.cut_profile(0.5, passes=30, power=100.0)
    assert p["speed"] <= 6000.0


def test_la_gravure_ne_traverse_pas():
    assert bg.engrave_profile(4.0)["power"] < bg.cut_profile(4.0)["power"]
    assert bg.engrave_profile(4.0)["passes"] == 1


# ---- décor ------------------------------------------------------------------ #
def test_le_decor_ne_va_que_sur_les_facades():
    """Sur une armoire, la seule façade est la porte : il n'y a pas d'avant."""
    pieces = bg.make_wall_cabinet(x=400, y=200, h=300, doors=1)
    n = bg.apply_decor(pieces, motif="croisillons", text="ESSAI", target="facades")
    assert n == 1
    assert bg.apply_decor(bg.make_wall_cabinet(doors=2), motif="rayures",
                          target="facades") == 2
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


# ---- compartiments --------------------------------------------------------- #
def test_une_boite_peut_avoir_des_compartiments():
    """Une boîte à cases se commande comme une boîte simple."""
    simple = {p.name for p in bg.make_box(x=300, y=200, h=80)}
    casee = [p for p in bg.make_box(x=300, y=200, h=80, cols=3, rows=2)]
    noms = {p.name for p in casee}
    assert not any(n.startswith("separateur") for n in simple)
    cloisons = [p for p in casee if p.name.startswith("separateur")]
    assert len(cloisons) == (3 - 1) + (2 - 1)
    assert noms - simple == {p.name for p in cloisons}


def test_les_cloisons_se_croisent_a_mi_bois():
    """L'une fendue par le haut, l'autre par le bas : elles s'emboîtent."""
    pieces = bg.make_tray(x=300, y=200, h=80, cols=2, rows=2, thickness=4)
    prof = next(p for p in pieces if p.name.startswith("separateur_profondeur"))
    larg = next(p for p in pieces if p.name.startswith("separateur_largeur"))
    assert prof.holes and larg.holes
    h = prof.size()[1]
    # la fente de l'une part du haut, celle de l'autre du bas
    assert max(t.bounds[3] for t in prof.holes) == pytest.approx(h)
    assert min(t.bounds[1] for t in larg.holes) == pytest.approx(0.0)


def test_larmoire_accepte_des_cloisons_verticales():
    """Les cloisons traversent les tablettes : chacune est fendue pour l'autre."""
    pieces = bg.make_wall_cabinet(x=600, y=250, h=450, shelves=2, cols=3)
    cloisons = [p for p in pieces if p.name.startswith("cloison")]
    tablettes = [p for p in pieces if p.name.startswith("tablette")]
    assert len(cloisons) == 2 and len(tablettes) == 2
    assert all(len(c.holes) == 2 for c in cloisons)    # une fente par tablette
    assert all(len(t.holes) == 2 for t in tablettes)   # une fente par cloison


# ---- portes ---------------------------------------------------------------- #
def test_une_armoire_na_pas_de_panneau_avant():
    """Sa façade, ce sont les portes : un panneau avant la condamnerait."""
    noms = {p.name for p in bg.make_wall_cabinet(doors=1)}
    assert "avant" not in noms
    assert "arriere" in noms                 # le panneau arrière fait le dos


def test_fond_et_dessus_sont_identiques():
    for pieces in (bg.make_wall_cabinet(x=600, y=250, h=450),
                   bg.make_box(x=300, y=200, h=100, closed=True)):
        d = {p.name: p.size() for p in pieces}
        assert d["fond"] == pytest.approx(d["dessus"])


def test_deux_portes_se_partagent_la_largeur():
    x = 600.0
    pieces = {p.name: p for p in bg.make_wall_cabinet(x=x, doors=2)}
    assert "porte_gauche" in pieces and "porte_droite" in pieces
    g, d = pieces["porte_gauche"].size()[0], pieces["porte_droite"].size()[0]
    assert g == pytest.approx(d)
    assert g * 2 < x and g * 2 > x - 10      # la largeur moins les jeux


def test_chaque_porte_sorganise_depuis_son_cote():
    """Gonds du côté du montant, poignée à l'opposé : sinon elles se gênent."""
    pieces = {p.name: p for p in bg.make_wall_cabinet(
        x=600, doors=2, hinge_holes=2, handle="trou_rond")}
    for nom, a_gauche in (("porte_gauche", True), ("porte_droite", False)):
        porte = pieces[nom]
        w = porte.size()[0]
        xs = [round(t.centroid.x, 3) for t in porte.holes]
        # les deux gonds partagent la MEME abscisse ; la poignée est la seule
        # à en avoir une autre — trier ne suffit pas, elle peut être avant eux
        gond = max(set(xs), key=xs.count)
        poignee = next(v for v in xs if v != gond)
        assert xs.count(gond) == 2
        if a_gauche:
            assert gond < w / 2 and poignee > w / 2
        else:
            assert gond > w / 2 and poignee < w / 2


def test_sans_porte_pas_de_trou_de_charniere():
    pieces = {p.name: p for p in bg.make_wall_cabinet(doors=0)}
    assert not any(n.startswith("porte") for n in pieces)
    for t in pieces["gauche"].holes:
        minx, miny, maxx, maxy = t.bounds
        plein = (maxx - minx) * (maxy - miny)
        # une mortaise remplit son rectangle ; un trou rond n'en couvre que 79 %
        assert t.area / plein > 0.95


# ---- motifs ----------------------------------------------------------------- #
@pytest.mark.parametrize("motif", ["rayures", "croisillons", "chevrons",
                                   "losanges", "cadre"])
def test_aucun_motif_ne_deborde_du_panneau(motif):
    """Une diagonale calculée « à peu près » va graver la pièce voisine."""
    w, h, m = 200.0, 100.0, 8.0
    traits = bg.pattern(motif, w, h, step=12.0, margin=m)
    assert traits
    for ln in traits:
        minx, miny, maxx, maxy = ln.bounds
        assert minx >= m - 1e-6 and miny >= m - 1e-6
        assert maxx <= w - m + 1e-6 and maxy <= h - m + 1e-6


# ---- vue 3D ----------------------------------------------------------------- #
@pytest.mark.parametrize("fabrique", [
    lambda: bg.make_box(cols=2, rows=2, closed=True),
    lambda: bg.make_tray(cols=3, rows=2),
    lambda: bg.make_shelf(heights=(100, 120)),
    lambda: bg.make_wall_cabinet(shelves=2, cols=2, doors=2),
])
def test_chaque_piece_sait_ou_elle_se_pose(fabrique):
    """Sans pose, un panneau est absent de la vue 3D — en silence."""
    pieces = fabrique()
    sans = [p.name for p in pieces if p.place is None]
    assert not sans, f"pièces sans pose : {sans}"
    for p in pieces:
        origine, u, v = p.place
        assert len(origine) == 3 and len(u) == 3 and len(v) == 3
        # u et v ne doivent pas être colinéaires, sinon le panneau est un trait
        n = bg._norm(u, v)
        assert abs(n[0]) + abs(n[1]) + abs(n[2]) > 0.9


def test_la_projection_rend_toutes_les_faces():
    pieces = bg.make_wall_cabinet(shelves=1, doors=2)
    faces = bg.project_iso(pieces)
    assert len(faces) == len(pieces)
    assert {f["name"] for f in faces} == {p.name for p in pieces}


def test_les_faces_sont_triees_du_lointain_au_proche():
    """Algorithme du peintre : mal trié, le dos passe devant la porte."""
    faces = bg.project_iso(bg.make_wall_cabinet(shelves=1, doors=1))
    # au sein d'un même rang, la profondeur est croissante
    for rang in {f["group"] for f in faces}:
        p = [f["depth"] for f in faces if f["group"] == rang]
        assert p == sorted(p)
    noms = [f["name"] for f in faces]
    assert noms.index("arriere") < noms.index("porte")


def test_la_porte_passe_derriere_quand_on_regarde_le_dos():
    """Au dernier plan vue de face, au tout premier vue de derrière.

    Une porte est devant le meuble : si l'on tourne derrière, c'est le dos
    qu'on doit voir, pas la porte peinte par-dessus.
    """
    pieces = bg.make_wall_cabinet(shelves=1, doors=1)
    devant = [f["name"] for f in bg.project_iso(pieces, azimut=45)]
    derriere = [f["name"] for f in bg.project_iso(pieces, azimut=200)]
    assert devant.index("porte") > devant.index("arriere")
    assert derriere.index("porte") < derriere.index("arriere")


def test_tourner_le_point_de_vue_change_le_dessin():
    pieces = bg.make_box(closed=True)
    a = bg.project_iso(pieces, azimut=30)[0]["poly"].bounds
    b = bg.project_iso(pieces, azimut=120)[0]["poly"].bounds
    assert a != b


def test_les_trous_se_voient_a_travers():
    """Une mortaise doit être un vide, pas un dessin sur le panneau."""
    pieces = [p for p in bg.make_wall_cabinet(shelves=2, doors=0)
              if p.name == "gauche"]
    assert pieces[0].holes
    face = bg.project_iso(pieces)[0]
    trous = sum(len(g.interiors) for g in
                getattr(face["poly"], "geoms", [face["poly"]]))
    assert trous >= len(pieces[0].holes) - 1       # mortaises au bord exclues


def test_un_panneau_horizontal_est_plus_clair_quun_flanc():
    """C'est ce dégradé qui donne le relief."""
    faces = {f["name"]: f for f in bg.project_iso(bg.make_box(closed=True))}
    assert faces["dessus"]["shade"] > faces["gauche"]["shade"]


def test_leclate_ecarte_les_panneaux():
    pieces = bg.make_wall_cabinet(shelves=1, doors=1)
    from shapely.ops import unary_union
    monte = unary_union([f["poly"] for f in bg.project_iso(pieces, 0.0)])
    eclate = unary_union([f["poly"] for f in bg.project_iso(pieces, 1.0)])
    a, b = monte.bounds, eclate.bounds
    assert (b[2] - b[0]) > (a[2] - a[0])
    assert (b[3] - b[1]) > (a[3] - a[1])


def test_une_piece_sans_pose_est_simplement_ignoree():
    """Mieux vaut une vue incomplète qu'un plantage."""
    p = bg.Piece("orpheline", bg.Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]))
    assert bg.project_iso([p]) == []


# ---- tiroir et poignées ----------------------------------------------------- #
def test_le_tiroir_entre_dans_son_compartiment():
    """Les cotes saisies sont celles du CASIER : le jeu est retiré ensuite."""
    jeu = 2.0
    pieces = {p.name: p for p in bg.make_drawer(x=380, y=280, h=110,
                                                thickness=4, clearance=jeu)}
    largeur = pieces["fond"].size()[0]
    hauteur = pieces["avant"].size()[1]
    # jeu TOTAL : 2 mm de chaque côté en largeur pour 4 mm saisis
    assert largeur == pytest.approx(380 - jeu)
    assert hauteur == pytest.approx(110 - jeu / 2)
    assert pieces["fond"].size()[1] == pytest.approx(280 - jeu / 2)


def test_un_tiroir_est_ouvert_sur_le_dessus():
    noms = {p.name for p in bg.make_drawer()}
    assert "dessus" not in noms and "avant" in noms


def test_le_tiroir_accepte_des_cloisons_dans_les_deux_sens():
    pieces = bg.make_drawer(cols=3, rows=2)
    prof = [p for p in pieces if "profondeur" in p.name]
    larg = [p for p in pieces if "largeur" in p.name]
    assert len(prof) == 2 and len(larg) == 1
    assert all(p.holes for p in prof + larg)           # elles se croisent


@pytest.mark.parametrize("kind", list(bg.HANDLES))
def test_chaque_poignee_donne_une_facade_valide(kind):
    pieces = {p.name: p for p in bg.make_drawer(x=380, y=280, h=110,
                                                handle=kind, handle_size=70)}
    av = pieces["avant"]
    assert av.outline.is_valid and not av.outline.is_empty
    assert av.outline.geom_type == "Polygon", "la façade ne doit pas se couper en deux"
    for t in av.holes:
        assert av.outline.contains(t.representative_point())


def test_une_encoche_mord_le_bord_les_autres_percent():
    """Une encoche retirée du CONTOUR, un trou percé DEDANS : pas pareil.

    Dessiner l'encoche comme un trou ferait découper une boucle fermée qui
    traverse le bord — et libérerait un morceau de façade.
    """
    plein = bg.make_drawer(handle="aucune")
    perce = bg.make_drawer(handle="oblong", handle_size=70)
    entaille = bg.make_drawer(handle="encoche", handle_size=70)
    a = next(p for p in plein if p.name == "avant")
    b = next(p for p in perce if p.name == "avant")
    c = next(p for p in entaille if p.name == "avant")
    assert b.outline.area == pytest.approx(a.outline.area)   # contour intact
    assert len(b.holes) == 1
    assert c.outline.area < a.outline.area                   # matière en moins
    assert not c.holes


def test_la_poignee_dune_porte_est_a_loppose_du_gond():
    """Contre la charnière, elle serait inatteignable."""
    pieces = {p.name: p for p in bg.make_wall_cabinet(
        x=600, doors=2, handle="oblong", handle_size=50, hinge_holes=2)}
    for nom, a_gauche in (("porte_gauche", True), ("porte_droite", False)):
        porte = pieces[nom]
        w = porte.size()[0]
        poignee = porte.holes[-1].centroid.x
        assert (poignee > w / 2) if a_gauche else (poignee < w / 2)


def test_sans_poignee_la_facade_reste_pleine():
    """Rien de percé, rien d'entaillé — mais l'aire n'est PAS celle du
    rectangle : les tenons dépassent et les encoches d'assemblage entament."""
    av = next(p for p in bg.make_drawer(handle="aucune") if p.name == "avant")
    ref = next(p for p in bg.make_drawer(handle="trou_rond") if p.name == "avant")
    assert not av.holes
    assert av.outline.area == pytest.approx(ref.outline.area)   # contour identique


# ---- les cloisons doivent être TENUES --------------------------------------- #
def test_une_cloison_seule_est_quand_meme_tenue():
    """Défaut signalé à l'usage : en 1 × 4, les cloisons ne croisaient rien.

    Sans tenon ni mortaise, elles flottaient librement dans le tiroir.
    """
    pieces = {p.name: p for p in bg.make_drawer(x=103, y=390, h=100,
                                                cols=1, rows=4)}
    cloisons = [p for n, p in pieces.items() if n.startswith("separateur")]
    assert len(cloisons) == 3
    # les parois qui les reçoivent sont percées
    assert pieces["gauche"].holes and pieces["droite"].holes
    assert len(pieces["gauche"].holes) == len(pieces["droite"].holes)
    # ... et pas les deux autres (aucune cloison dans ce sens)
    assert not pieces["arriere"].holes


def test_les_tenons_tombent_dans_les_mortaises():
    """Un décalage ici, et la cloison n'entre pas. C'est tout l'assemblage."""
    from shapely.geometry import box as _box
    t = 4.0
    pieces = {p.name: p for p in bg.make_drawer(x=103, y=390, h=100,
                                                thickness=t, cols=1, rows=4)}
    cloison, paroi = pieces["separateur_largeur_1"], pieces["gauche"]
    b = cloison.outline.bounds
    saillies = cloison.outline.difference(_box(b[0] + t, b[1], b[2] - t, b[3]))
    tenons = sorted((g.bounds[1], g.bounds[3])
                    for g in getattr(saillies, "geoms", [saillies])
                    if g.bounds[0] < b[0] + t + 0.01)
    x0 = min(h.bounds[0] for h in paroi.holes)
    mortaises = sorted((h.bounds[1], h.bounds[3]) for h in paroi.holes
                       if abs(h.bounds[0] - x0) < 0.5)
    assert len(tenons) == len(mortaises) >= 3
    for (ta, tb), (ma, mb) in zip(tenons, mortaises):
        # le fond du tiroir surélève la cloison de son épaisseur
        assert (ta + tb) / 2 + t == pytest.approx((ma + mb) / 2, abs=0.01)


def test_le_trait_est_compense_dans_le_MEME_sens_partout():
    """Tenon dessiné plus large, mortaise plus étroite : après coupe, ça joint.

    Les deux mécanismes — encoche de bord et mortaise traversante — doivent
    compenser pareil, sinon un même geste donne deux ajustements différents.
    """
    t, f, kerf = 4.0, 8.0, 0.2
    mortaise = bg.edge_slots(100.0, t, f, kerf, along="y", position=0.0)[0]
    largeur = mortaise.bounds[3] - mortaise.bounds[1]
    assert largeur == pytest.approx(f - kerf)        # dessinée plus ÉTROITE
    # le tenon correspondant est dessiné plus LARGE d'autant
    p = bg.panel(100.0, 50.0, t, f, (bg.FLAT, bg.FINGERS, bg.FLAT, bg.FLAT), kerf)
    sans = bg.panel(100.0, 50.0, t, f,
                    (bg.FLAT, bg.FINGERS, bg.FLAT, bg.FLAT), 0.0)
    n = bg.finger_count(50.0, f)
    assert p.area - sans.area == pytest.approx(n * kerf * t, rel=0.05)


def test_les_tablettes_sont_a_la_hauteur_annoncee():
    """Repères comptés depuis l'intérieur, panneau coté depuis l'extérieur.

    Sans le décalage d'une épaisseur, les mortaises tombaient 4 mm trop bas :
    la tablette restait de niveau, mais pas à la hauteur voulue.
    """
    t, h, n = 4.0, 300.0, 2
    pieces = {p.name: p for p in bg.make_wall_cabinet(
        x=400, y=200, h=h, thickness=t, shelves=n)}
    ih = h - 2 * t
    attendu = [t + ih * (k + 1) / (n + 1) for k in range(n)]
    trouve = sorted({round(o.bounds[1], 3) for o in pieces["gauche"].holes
                     if o.bounds[2] - o.bounds[0] > 5})
    assert trouve == pytest.approx(attendu, abs=0.01)
    # la pose 3D doit dire la même chose que la découpe
    for k, tab in enumerate(p for n_, p in sorted(pieces.items())
                            if n_.startswith("tablette")):
        assert tab.place[0][2] == pytest.approx(attendu[k], abs=0.01)


# ---- les coins doivent être pleins ------------------------------------------ #
def test_la_colonne_dangle_est_pleine_en_haut_et_en_bas():
    """Défaut constaté sur une pièce réelle : un trou traversant à chaque coin.

    Le motif commençait et finissait par un CREUX sur toutes les arêtes. Au
    coin, ni le panneau avant ni le côté n'occupaient la colonne : il restait
    un trou de l'épaisseur du bois, en haut comme en bas de chaque arête.
    """
    from shapely.geometry import Point
    t, h = 4.0, 100.0
    av = next(p for p in bg.make_box(x=200, y=150, h=h, thickness=t, closed=True)
              if p.name == "avant")
    for z in (0.5, 2.0, h / 2, h - 2.0, h - 0.5):
        # la colonne d'angle est à gauche du rectangle nominal
        assert av.outline.contains(Point(-t / 2, z)) or z == h / 2, \
            f"coin ouvert à z={z}"
    # aux deux extrémités, c'est un TENON, pas un creux
    assert av.outline.contains(Point(-t / 2, 0.5))
    assert av.outline.contains(Point(-t / 2, h - 0.5))


def test_le_motif_de_coin_commence_et_finit_par_un_tenon():
    pts = bg._profile(300.0, 4.0, 8.0, bg.FINGERS, 0.0, coin=True)
    assert pts[0] == (0.0, 0.0) and pts[1] == (0.0, 4.0)      # tenon dès 0
    assert pts[-1] == (300.0, 0.0) and pts[-2] == (300.0, 4.0)  # et jusqu'au bout
    # ... et il reste symétrique : aucun sens de montage
    xs = sorted(round(x, 6) for x, _y in pts)
    assert xs == sorted(round(300.0 - x, 6) for x, _y in pts)


def test_tous_les_contours_restent_valides():
    """Une encoche pile dans le coin dégénère avec le bord voisin."""
    for fabrique in (lambda: bg.make_box(closed=True, cols=2, rows=2),
                     lambda: bg.make_tray(cols=3, rows=2),
                     lambda: bg.make_shelf(heights=(100, 120)),
                     lambda: bg.make_wall_cabinet(shelves=2, cols=2, doors=2),
                     lambda: bg.make_drawer(cols=2, rows=3)):
        for p in fabrique():
            assert p.outline.is_valid, f"contour invalide : {p.name}"
            assert p.outline.geom_type == "Polygon"
