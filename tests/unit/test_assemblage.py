"""Invariants d'ASSEMBLAGE des caisses — à ne jamais casser.

Ces tests ne vérifient pas des détails de dessin : ils vérifient qu'une caisse
**se monte**. Ils sont écrits pour résister aux évolutions du générateur — ils
portent sur le résultat géométrique, pas sur la façon de l'obtenir.

Trois défauts réels, constatés sur des pièces découpées, sont verrouillés ici :

1. les arêtes verticales ne s'emboîtaient pas du tout (les tenons sortaient
   dans le vide, les encoches en face ne recevaient rien) ;
2. la colonne d'angle restait ouverte en haut et en bas ;
3. les cloisons sans croisement n'étaient tenues par rien.
"""

import pytest
from shapely.geometry import Point

from laserpcb import boxgen as bg

T = 4.0                      # épaisseur de référence
HAUTEURS = 400               # nombre de hauteurs échantillonnées


# --------------------------------------------------------------------------- #
#  Outils
# --------------------------------------------------------------------------- #
def _occupe(piece, cote: str, t: float = T):
    """Fonction ``z -> bool`` : ce panneau occupe-t-il la colonne d'angle ?

    ``cote`` vaut ``"debut"`` (bord à l'origine) ou ``"fin"`` (bord opposé).
    On sonde à mi-épaisseur, juste à l'extérieur du rectangle nominal.
    """
    _minx, _miny, maxx, _maxy = piece.outline.bounds
    # Les tenons dépassent du rectangle nominal : on sonde juste à l'extérieur,
    # à mi-épaisseur, là où le voisin viendra se loger.
    x = (-t / 2.0) if cote == "debut" else (maxx - t / 2.0)
    geom = piece.outline
    return lambda z: geom.contains(Point(x, z))


def _colonne_parfaite(a, cote_a, b, cote_b, hauteur, t=T):
    """Les deux panneaux se partagent la colonne : jamais vide, jamais double."""
    fa, fb = _occupe(a, cote_a, t), _occupe(b, cote_b, t)
    vides, doubles = [], []
    for i in range(HAUTEURS):
        z = hauteur * (i + 0.5) / HAUTEURS
        pa, pb = fa(z), fb(z)
        if pa and pb:
            doubles.append(round(z, 2))
        elif not pa and not pb:
            vides.append(round(z, 2))
    return vides, doubles


# --------------------------------------------------------------------------- #
#  Le peigne d'angle
# --------------------------------------------------------------------------- #
def test_les_deux_phases_pavent_exactement_larete():
    """Phase 0 et phase 1 doivent se compléter au millimètre.

    C'est la base de tout : si elles se recouvrent, les panneaux ne rentrent
    pas ; si elles laissent un vide, le coin est percé.
    """
    for longueur in (60.0, 100.0, 137.0, 300.0, 412.5):
        a = bg._profile(longueur, T, 8.0, bg.FINGERS, 0.0, phase=0)
        b = bg._profile(longueur, T, 8.0, bg.FINGERS, 0.0, phase=1)

        def plages(pts):
            return [(pts[i][0], pts[i + 1][0]) for i in range(len(pts) - 1)
                    if pts[i][1] > 0 and pts[i + 1][1] > 0]

        toutes = sorted(plages(a) + plages(b))
        assert toutes, longueur
        assert toutes[0][0] == pytest.approx(0.0), longueur
        assert toutes[-1][1] == pytest.approx(longueur), longueur
        for i in range(len(toutes) - 1):
            assert toutes[i][1] == pytest.approx(toutes[i + 1][0]), \
                f"trou ou recouvrement à {longueur} mm"


def test_les_deux_extremites_appartiennent_au_meme_panneau():
    """Nombre IMPAIR de segments : c'est ce qui remplit les DEUX coins."""
    for longueur in (50.0, 100.0, 233.0, 400.0):
        assert bg._segments(longueur, 8.0) % 2 == 1
        p = bg._profile(longueur, T, 8.0, bg.FINGERS, 0.0, phase=0)
        assert p[1][1] > 0, "le motif doit commencer par un tenon"
        assert p[-2][1] > 0, "et finir par un tenon"


@pytest.mark.parametrize("dims", [(200, 150, 100), (103, 390, 100),
                                  (600, 250, 450), (80, 80, 60)])
def test_la_colonne_dangle_est_remplie_exactement_une_fois(dims):
    """Défaut constaté sur pièce réelle : coin ouvert en haut et en bas.

    À chaque hauteur, l'un OU l'autre panneau occupe la colonne — jamais les
    deux (ils ne rentreraient pas), jamais aucun (le coin serait percé).
    """
    x, y, h = dims
    p = {q.name: q for q in bg.make_box(x=x, y=y, h=h, thickness=T, kerf=0)}
    for devant, cote in (("avant", "gauche"), ("arriere", "gauche")):
        vides, doubles = _colonne_parfaite(p[devant], "debut", p[cote], "debut",
                                           h)
        assert not vides, f"{dims} : coin ouvert à z={vides[:3]}"
        assert not doubles, f"{dims} : panneaux superposés à z={doubles[:3]}"


def test_le_haut_et_le_bas_de_la_colonne_sont_pleins():
    """Le défaut se voyait précisément aux deux bouts."""
    h = 100.0
    p = {q.name: q for q in bg.make_box(x=200, y=150, h=h, thickness=T, kerf=0)}
    occupe_av = _occupe(p["avant"], "debut")
    occupe_co = _occupe(p["gauche"], "debut")
    for z in (0.2, 1.0, h - 1.0, h - 0.2):
        assert occupe_av(z) or occupe_co(z), f"coin ouvert à z={z}"


# --------------------------------------------------------------------------- #
#  Cotes et validité
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fabrique,nom", [
    (lambda: bg.make_box(closed=True, cols=2, rows=2), "boîte"),
    (lambda: bg.make_tray(cols=3, rows=2), "bac"),
    (lambda: bg.make_shelf(heights=(100, 120)), "casier"),
    (lambda: bg.make_wall_cabinet(shelves=2, cols=2, doors=2), "armoire"),
    (lambda: bg.make_drawer(cols=2, rows=3), "tiroir"),
])
def test_tous_les_panneaux_sont_decoupables(fabrique, nom):
    """Un contour invalide ou en plusieurs morceaux = une pièce perdue."""
    for p in fabrique():
        assert p.outline.is_valid, f"{nom} : contour invalide ({p.name})"
        assert p.outline.geom_type == "Polygon", \
            f"{nom} : {p.name} est en plusieurs morceaux"
        assert p.outline.area > 0
        for t in p.holes:
            assert t.is_valid and t.area > 0, f"{nom} : ouverture invalide"


@pytest.mark.parametrize("dims", [(200, 150, 100), (300, 200, 80), (90, 90, 90)])
def test_les_cotes_exterieures_sont_tenues(dims):
    """Les tenons comblent l'épaisseur du voisin : la caisse fait bien x × y × h."""
    x, y, h = dims
    p = {q.name: q for q in bg.make_box(x=x, y=y, h=h, thickness=T, kerf=0)}
    assert p["fond"].size() == pytest.approx((x, y))
    assert p["avant"].size() == pytest.approx((x, h))
    assert p["gauche"].size() == pytest.approx((y, h))


def test_les_panneaux_opposes_sont_identiques():
    """Avant/arrière et gauche/droite doivent être interchangeables."""
    p = {q.name: q for q in bg.make_box(x=200, y=150, h=100, thickness=T)}
    assert p["avant"].outline.area == pytest.approx(p["arriere"].outline.area)
    assert p["gauche"].outline.area == pytest.approx(p["droite"].outline.area)


# --------------------------------------------------------------------------- #
#  Compensation du trait
# --------------------------------------------------------------------------- #
def test_le_trait_est_compense_en_sens_inverse():
    """Tenon dessiné plus LARGE, encoche plus ÉTROITE : après coupe, ça joint."""
    kerf = 0.2
    large = bg.panel(200, 100, T, 8.0,
                     (bg.FINGERS, bg.FLAT, bg.FLAT, bg.FLAT), kerf)
    juste = bg.panel(200, 100, T, 8.0,
                     (bg.FINGERS, bg.FLAT, bg.FLAT, bg.FLAT), 0.0)
    assert large.area > juste.area
    creuse = bg.panel(200, 100, T, 8.0,
                      (bg.NOTCHES, bg.FLAT, bg.FLAT, bg.FLAT), kerf)
    plein = bg.panel(200, 100, T, 8.0,
                     (bg.NOTCHES, bg.FLAT, bg.FLAT, bg.FLAT), 0.0)
    assert creuse.area > plein.area        # l'encoche enlève MOINS de matière


def test_le_peigne_dangle_garde_ses_extremites_a_fleur():
    """Un tenon d'extrémité élargi ferait dépasser le panneau de sa cote."""
    for kerf in (0.0, 0.2, 0.5):
        p = bg._profile(100.0, T, 8.0, bg.FINGERS, kerf, phase=0)
        assert min(x for x, _y in p) == pytest.approx(0.0)
        assert max(x for x, _y in p) == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
#  Cloisons
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cols,rows", [(1, 4), (4, 1), (3, 2), (2, 2)])
def test_toute_cloison_est_tenue(cols, rows):
    """Une cloison qui n'en croise aucune autre doit quand même être tenue.

    Sinon elle flotte dans la caisse — défaut constaté en 1 × 4.
    """
    pieces = bg.make_drawer(x=300, y=400, h=100, thickness=T,
                            cols=cols, rows=rows)
    par_nom = {p.name: p for p in pieces}
    cloisons = [p for p in pieces if p.name.startswith("separateur")]
    assert len(cloisons) == (cols - 1) + (rows - 1)
    for c in cloisons:
        # Un rectangle nu n'est tenu par rien. Il faut soit des tenons — le
        # contour n'est alors plus un simple rectangle — soit des fentes de
        # croisement.
        denture = c.outline.area < c.outline.envelope.area - 1e-6
        assert denture or c.holes, f"{c.name} ne serait tenue par rien"
    if rows > 1:
        assert par_nom["gauche"].holes, "les côtés doivent recevoir les cloisons"
    if cols > 1:
        assert par_nom["avant"].holes or par_nom["arriere"].holes


def test_les_mortaises_sont_a_lendroit_des_tenons():
    """Un décalage ici et la cloison n'entre pas."""
    from shapely.geometry import box as _box
    pieces = {p.name: p for p in bg.make_drawer(x=103, y=390, h=100,
                                                thickness=T, cols=1, rows=4)}
    cloison, paroi = pieces["separateur_largeur_1"], pieces["gauche"]
    b = cloison.outline.bounds
    saillies = cloison.outline.difference(_box(b[0] + T, b[1], b[2] - T, b[3]))
    tenons = sorted((g.bounds[1] + g.bounds[3]) / 2
                    for g in getattr(saillies, "geoms", [saillies])
                    if g.bounds[0] < b[0] + T + 0.01)
    x0 = min(h.bounds[0] for h in paroi.holes)
    mortaises = sorted((h.bounds[1] + h.bounds[3]) / 2 for h in paroi.holes
                       if abs(h.bounds[0] - x0) < 0.5)
    assert len(tenons) == len(mortaises) >= 3
    for a, m in zip(tenons, mortaises):
        assert a + T == pytest.approx(m, abs=0.02)


# --------------------------------------------------------------------------- #
#  Meuble de cuisine
# --------------------------------------------------------------------------- #
def test_le_meuble_de_cuisine_contient_tout():
    """Caisson, tablettes de séparation, et un tiroir COMPLET par section."""
    pieces = bg.make_kitchen(x=600, y=560, h=720, thickness=T, drawers=3)
    noms = {p.name for p in pieces}
    for carcasse in ("fond", "arriere", "gauche", "droite", "dessus"):
        assert carcasse in noms
    assert "avant" not in noms, "la façade, ce sont les tiroirs"
    assert sum(1 for n in noms if n.startswith("tablette")) == 2   # entre 3 sections
    for i in (1, 2, 3):
        for part in ("fond", "avant", "arriere", "gauche", "droite"):
            assert f"tiroir{i}_{part}" in noms


def test_le_jeu_de_tiroir_est_un_jeu_TOTAL():
    """« 4 mm de tolérance » doit donner 4 mm, pas 8.

    Compté par côté, un « 4 mm » intuitif en aurait fait 8 et le tiroir aurait
    dansé dans son logement.
    """
    for jeu in (2.0, 4.0, 6.0):
        pieces = {p.name: p for p in bg.make_kitchen(
            x=600, y=560, h=720, thickness=T, drawers=2,
            drawer_clearance=jeu)}
        ouverture = 600 - 2 * T
        assert ouverture - pieces["tiroir1_fond"].size()[0] == pytest.approx(jeu)


def test_chaque_tiroir_tient_dans_sa_section():
    """Un tiroir plus haut que son ouverture ne rentrerait jamais."""
    h, n = 720.0, 3
    pieces = {p.name: p for p in bg.make_kitchen(
        x=600, y=560, h=h, thickness=T, drawers=n, drawer_clearance=4.0)}
    ih = h - 2 * T
    section = (ih - (n - 1) * T) / n
    for i in range(1, n + 1):
        hauteur = pieces[f"tiroir{i}_avant"].size()[1]
        assert hauteur < section, f"tiroir {i} trop haut pour sa section"
        assert hauteur > section - 5.0, f"tiroir {i} inutilement petit"


def test_les_tiroirs_sont_empiles_sans_se_chevaucher():
    """Chacun doit être posé à sa hauteur, du bas vers le haut."""
    n = 3
    pieces = {p.name: p for p in bg.make_kitchen(
        x=600, y=560, h=720, thickness=T, drawers=n)}
    z = [pieces[f"tiroir{i}_fond"].place[0][2] for i in range(1, n + 1)]
    assert z == sorted(z), "les tiroirs doivent monter"
    for i in range(n - 1):
        hauteur = pieces[f"tiroir{i + 1}_avant"].size()[1]
        assert z[i + 1] >= z[i] + hauteur, "deux tiroirs se chevauchent"


def test_les_compartiments_du_tiroir_suivent():
    """Les cases demandées se retrouvent DANS chaque tiroir."""
    pieces = bg.make_kitchen(drawers=2, cols=3, rows=2, thickness=T)
    for i in (1, 2):
        cloisons = [p for p in pieces
                    if p.name.startswith(f"tiroir{i}_separateur")]
        assert len(cloisons) == (3 - 1) + (2 - 1)


def test_une_section_a_porte_remplace_un_tiroir():
    sans = {p.name for p in bg.make_kitchen(drawers=3, doors=0)}
    avec = {p.name for p in bg.make_kitchen(drawers=3, doors=2)}
    assert not any(n.startswith("porte") for n in sans)
    assert {"porte_gauche", "porte_droite"} <= avec
    # la porte prend sa propre section : une tablette de plus qu'avec 3 tiroirs
    assert sum(1 for n in avec if n.startswith("tablette")) == 3


def test_le_meuble_de_cuisine_est_decoupable():
    for p in bg.make_kitchen(drawers=3, doors=1, cols=2, rows=2):
        assert p.outline.is_valid, p.name
        assert p.outline.geom_type == "Polygon", p.name
        assert p.place, f"{p.name} manquerait dans la vue 3D"
