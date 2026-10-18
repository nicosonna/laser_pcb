"""Intégration : Session GUI (logique métier, sans Qt).

Import Gerber + ODB++, génération G-code, aller-retour projet embarqué.
Mode isolation offset (rapide) pour rester léger — l'axe médian est testé ailleurs.
"""

from __future__ import annotations

import tarfile

import pytest

from laserpcb.gui.session import Session
from laserpcb.project import MODE_ISOLATION, MODE_NEGATIVE

GERBER = "examples/module_relais"
ODBPP = "examples/solar_pi_odb"


def _set_mode(session, mode):
    for layer in session.layers:
        layer.config.mode = mode


def test_session_import_gerber():
    s = Session()
    s.import_source(GERBER)
    assert s.project.source_kind == "gerber"
    roles = {l.config.role for l in s.layers}
    assert roles == {"top", "bottom"}
    assert len(s.holes) == 77
    assert s.board.bounds[2] > 79


def test_session_import_odb_and_gcode():
    s = Session()
    s.import_source(ODBPP)
    assert s.project.source_kind == "odbpp"
    _set_mode(s, MODE_ISOLATION)
    prog = s.generate_gcode()
    assert "M4 S0" in prog and prog.rstrip().endswith("M2")
    assert len(prog.splitlines()) > 100


def test_session_import_tgz(tmp_path):
    """Import d'un ODB++ compressé en .tgz."""
    tgz = tmp_path / "board.tgz"
    with tarfile.open(tgz, "w:gz") as t:
        t.add(ODBPP, arcname=".")
    s = Session()
    s.import_source(tgz)
    assert s.project.source_kind == "odbpp"
    assert len(s.layers) == 2
    assert s.board.bounds[2] > 145


def test_negative_adaptive_clearing_two_speeds():
    """Le mode négatif adaptatif évide vite au centre, lentement près des pistes."""
    s = Session()
    s.import_source(ODBPP)
    for layer in s.layers:
        layer.config.mode = MODE_NEGATIVE
        layer.config.adaptive = True
    feeds = {round(f) for _c, f, _p in s.layer_items(s.layers[0])}
    assert len(feeds) >= 2                    # au moins deux vitesses (bord / intérieur)


def _iso(session):
    for layer in session.layers:
        layer.config.mode = MODE_ISOLATION


def test_center_on_bed_offsets_gcode():
    """Centrer sur le plateau décale le G-code (travail en absolu)."""
    import re
    s = Session()
    s.import_source(ODBPP)
    _iso(s)
    s.center_on_bed()
    ox, oy = s.project.origin
    assert ox > 0 and oy > 0
    g = s.generate_gcode()
    xs = [float(v) for v in re.findall(r"G1 X(-?[\d.]+)", g)]
    assert min(xs) >= ox - 1                 # tous les traits décalés sur le plateau


def test_per_side_export_differs_and_mirrors():
    s = Session()
    s.import_source(ODBPP)
    _iso(s)
    s.mirror_bottom = True
    s.active_side = "top"; top = s.generate_gcode()
    s.active_side = "bottom"; bot = s.generate_gcode()
    assert top != bot                        # une face à la fois, contenu distinct
    assert "_top" in top and "_bottom" in bot


def test_copper_margin_default():
    """Marge cuivre par défaut : 0,1 mm (nouveau projet et ancien projet sans
    ce réglage)."""
    from laserpcb.project import Project
    assert Project().copper_margin == 0.1
    d = Project().to_dict(); d.pop("copper_margin")
    assert Project.from_dict(d).copper_margin == 0.1


def test_copper_margin_widens_copper():
    """La marge cuivre élargit bien pistes/pads (compensation laser + chimie)."""
    s = Session()
    s.import_source(ODBPP)
    layer = s.layers[0]
    s.project.copper_margin = 0.0
    area0 = s._expanded(layer).union.area
    s.project.copper_margin = 0.1
    area1 = s._expanded(layer).union.area
    assert area1 > area0


def test_combined_is_default_and_generates():
    """Le mode par défaut est combiné (isolation + évidement) et produit du G-code."""
    from laserpcb.project import MODE_COMBINED
    s = Session()
    s.import_source(ODBPP)
    assert all(l.config.mode == MODE_COMBINED for l in s.layers)
    g = s.generate_gcode()
    assert "M4 S0" in g and g.rstrip().endswith("M2")


def test_frame_gcode_frames_the_plate_without_computing_toolpaths():
    """Le cadre trace le rectangle de la PLAQUE (4 coins), pas celui des tracés :
    il doit les englober, et surtout n'exiger AUCUN calcul de parcours — sinon
    appuyer sur « Cadrer » fige l'interface le temps de tout recalculer."""
    import re
    s = Session()
    s.import_source(GERBER)
    _iso(s)
    frame = s.frame_gcode(power=1.0, loops=2)
    cuts = [l for l in frame.splitlines() if l.startswith("G1")]
    assert len(cuts) == 8                      # 4 coins x 2 boucles
    xs = {round(float(re.search(r"X(-?[\d.]+)", l).group(1)), 1) for l in cuts}
    ys = {round(float(re.search(r"Y(-?[\d.]+)", l).group(1)), 1) for l in cuts}
    assert len(xs) == 2 and len(ys) == 2       # un vrai rectangle
    # il englobe ce qui sera réellement gravé
    jminx, jminy, jmaxx, jmaxy = s.job_bounds()
    assert min(xs) <= jminx + 1e-6 and max(xs) >= jmaxx - 1e-6
    assert min(ys) <= jminy + 1e-6 and max(ys) >= jmaxy - 1e-6
    powers = {int(re.search(r"S(\d+)", l).group(1)) for l in cuts}
    assert powers == {10}                      # 1 % -> S10, ne grave pas
    # ... et il se calcule sans parcours : vrai même sur une session neuve
    s2 = Session()
    s2.import_source(GERBER)
    s2._items_cache = None
    assert s2.frame_gcode(power=1.0).count("G1") == 8
    assert s2._items_cache is None             # aucun parcours n'a été calculé


def test_mark_zone_engraves_rectangle_then_pauses():
    """Option pourtour : le job commence par le rectangle de la zone (puissance de
    gravure du calque) puis PAUSE M0 (poser le PCB) ; désactivée -> pas de M0."""
    import re
    s = Session()
    s.import_source(GERBER)
    _iso(s)
    s.project.mark_zone_enabled = True
    g = s.generate_gcode()
    assert "M0" in g
    body = g.splitlines()
    i_pause = next(i for i, l in enumerate(body) if l.startswith("M0"))
    prelude = "\n".join(body[:i_pause])
    assert "pourtour" in prelude                     # marquage avant la pause
    # le rectangle utilise la puissance de GRAVURE du calque (pas 1 % de cadrage)
    pw = s.active_layers()[0].config.power
    assert f"S{int(pw * 10)}" in prelude             # p.ex. 30 % -> S300
    # 4 coins du rectangle avant la pause
    assert len(re.findall(r"G1 X", prelude)) >= 4
    s.project.mark_zone_enabled = False
    assert "M0" not in s.generate_gcode()


def test_mark_zone_includes_cut_margin_when_cut_enabled():
    """Découpe active : le pourtour marqué = emprise de la PLAQUE (carte + marge),
    pour poser une plaque de la bonne taille."""
    import re
    s = Session()
    s.import_source(GERBER)
    _iso(s)
    s.project.mark_zone_enabled = True
    s.project.cut_outline_enabled = True
    s.project.cut_outline_margin = 2.0
    s.project.origin = (0.0, 0.0)   # on teste la geometrie, pas le placement
    g = s.generate_gcode()
    body = g.splitlines()
    i_pause = next(i for i, l in enumerate(body) if l.startswith("M0"))
    xs = [float(m.group(1)) for l in body[:i_pause]
          for m in [re.search(r"X(-?[\d.]+)", l)] if m]
    minx, _miny, maxx, _maxy = s.board.bounds
    assert min(xs) < minx - 1.5 and max(xs) > maxx + 1.5   # rectangle élargi de ~2 mm
    # et le garde-fou plateau
    assert s.fits_on_bed()
    s.project.bed_width = 80.0                              # plateau trop petit
    assert not s.fits_on_bed()


def test_holes_add_lines_to_gcode():
    s = Session()
    s.import_source(ODBPP)
    _iso(s)
    s.project.hole_enabled = True
    with_holes = len(s.generate_gcode().splitlines())
    s.project.hole_enabled = False
    without = len(s.generate_gcode().splitlines())
    assert with_holes > without


def test_session_project_roundtrip(tmp_path):
    s = Session()
    s.import_source(GERBER)
    _set_mode(s, MODE_ISOLATION)
    s.layers[0].config.power = 42.0
    f = tmp_path / "p.lpcb"
    s.save_project(f)

    s2 = Session.open_project(f)
    assert {l.config.role for l in s2.layers} == {"top", "bottom"}
    # réglage rechargé
    assert any(l.config.power == 42.0 for l in s2.layers)
    # source embarquée -> géométrie reconstruite
    assert s2.board.bounds[2] > 79


def test_finish_job_exports_pads_holes_text():
    """Finition (après perchlorure) : pads + trous + texte -> G-code séparé."""
    from laserpcb import odb
    s = Session()
    s.import_source(ODBPP)
    pads = odb.load_pads(s.source_root, "top")
    assert not pads.is_empty                       # l'ODB++ fournit bien les pads
    s.project.finish_text = "TEST"
    g = s.generate_finish_gcode()
    assert "_finition" in g.splitlines()[0]
    assert "G1" in g and g.rstrip().endswith("M2")
    # sans pads ni trous ni texte -> quasi vide (pas de G1)
    s.project.finish_pads = False
    s.project.finish_holes = False
    s.project.finish_text = ""
    assert "G1" not in s.generate_finish_gcode()


def test_kicad_named_gerber_zip_import(tmp_path):
    """Export KiCad : xxx-F_Cu / -B_Cu / -Edge_Cuts / -PTH.drl reconnus, couches
    mask/silk ignorées, et carte NORMALISÉE au coin (0,0) (KiCad exporte dans le
    repère de sa feuille, souvent en Y négatifs)."""
    import shutil, zipfile
    from pathlib import Path
    src = Path(GERBER) / "GerberFiles"
    z = tmp_path / "proj_kicad.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(src / "copper_top.gbr", "proj-F_Cu.gbr")
        zf.write(src / "copper_bottom.gbr", "proj-B_Cu.gbr")
        zf.write(src / "profile.gbr", "proj-Edge_Cuts.gbr")
        zf.write(src / "soldermask_top.gbr", "proj-F_Mask.gbr")     # à ignorer
        zf.write(src / "silkscreen_top.gbr", "proj-F_Silkscreen.gbr")
        drill = next((Path(GERBER) / "DrillFiles").iterdir())
        zf.write(drill, "proj-PTH.drl")
    s = Session()
    s.import_source(z)
    assert s.project.name == "proj_kicad"
    assert {l.config.role for l in s.layers} == {"top", "bottom"}
    assert len(s.holes) == 77
    minx, miny, maxx, _ = s.board.bounds
    assert abs(minx) < 1e-6 and abs(miny) < 1e-6 and maxx > 79   # coin à (0,0)


def test_holes_then_outline_cut_come_last_with_margin():
    """Ordre en phases : gravure -> TROUS -> DÉCOUPE en dernier (l'optimiseur ne
    mélange pas), et la découpe passe à `cut_outline_margin` mm du bord."""
    import re
    s = Session()
    s.import_source(GERBER)
    _iso(s)
    s.project.hole_enabled = True
    s.project.cut_outline_enabled = True
    s.project.cut_outline_margin = 2.0
    s.project.origin = (0.0, 0.0)   # on teste la geometrie, pas le placement
    g = s.generate_gcode()
    lines = g.splitlines()
    # vitesses distinctes par phase : gravure F1500, trous F600, découpe F300
    idx = {f: [i for i, l in enumerate(lines) if f in l]
           for f in ("F1500", "F600", "F300")}
    assert idx["F1500"] and idx["F600"] and idx["F300"]
    assert max(idx["F1500"]) < min(idx["F600"]) < min(idx["F300"])
    # la découpe déborde de ~2 mm du bord de la carte (80 x 135)
    cut_x = [float(m.group(1)) for i, l in enumerate(lines) if i >= min(idx["F300"])
             for m in [re.search(r"X(-?[\d.]+)", l)] if m]
    assert max(cut_x) > s.board.bounds[2] + 1.5      # ~82 mm
    assert min(cut_x) < s.board.bounds[0] - 1.5      # ~-2 mm


def test_finish_referenced_to_plate_corner_when_cut_enabled():
    """Découpe active : la finition est référencée au COIN de la plaque découpée
    (+marge, sans offset plateau) — sinon impossible d'aligner après le bain."""
    import re
    s = Session()
    s.import_source(ODBPP)
    s.project.finish_pads = True
    s.project.finish_holes = False
    s.project.origin = (100.0, 50.0)                 # offset plateau du job principal
    s.project.cut_outline_enabled = False
    base_x = [float(v) for v in re.findall(r"G1 X(-?[\d.]+)",
                                           s.generate_finish_gcode())]
    s.project.cut_outline_enabled = True
    s.project.cut_outline_margin = 2.0
    s.project.finish_ref_plate_corner = True   # opt-in explicite (défaut : centrage suivi)
    g = s.generate_finish_gcode()
    plate_x = [float(v) for v in re.findall(r"G1 X(-?[\d.]+)", g)]
    assert "coin de la plaque" in g.splitlines()[0]
    # sans découpe : coords = job principal (offset plateau 100) ;
    # avec découpe : coords depuis le coin de plaque = pads + 2 mm (pas de 100)
    assert min(base_x) > 90
    assert 1.5 < min(plate_x) < 15
    assert abs((min(base_x) - 100) - (min(plate_x) - 2)) < 1e-6   # même géométrie


def test_hole_min_diameter_skips_vias():
    """Seuil de Ø (2 mm par défaut) : vias et trous de connecteur ne sont pas
    percés au laser — ni par le job de trous, ni par la finition."""
    from laserpcb.geometry import Hole
    s = Session()
    s.import_source(ODBPP)
    p = s.project
    assert p.hole_min_diameter == 2.0
    s.holes = [Hole(10, 10, 0.8), Hole(20, 10, 1.0), Hole(30, 10, 2.7)]
    gros = len(s.hole_items())
    assert gros > 0
    p.hole_min_diameter = 0.0
    tous = len(s.hole_items())
    assert tous > gros                      # à 0 les trois trous reviennent
    p.hole_min_diameter = 2.0
    s.holes = [Hole(10, 10, 0.8), Hole(20, 10, 1.0)]
    assert s.hole_items() == []             # que des vias : aucun perçage
    # la finition applique le même seuil
    p.finish_pads = False; p.finish_text = ""; p.finish_holes = True
    assert [it for it in s.finish_items_tagged() if it[3] == "hole"] == []
    # un ancien projet sans ce réglage prend 2 mm
    d = p.to_dict(); d.pop("hole_min_diameter")
    assert type(p).from_dict(d).hole_min_diameter == 2.0


def test_finish_cut_and_hole_passes_and_inset():
    """Finition v2 : trous en perçage (30 passes), découpe optionnelle EN DERNIER
    (et alors même zéro que la gravure), retrait des pads, peinture à 15 %."""
    import re
    s = Session()
    s.import_source(ODBPP)
    p = s.project
    assert p.finish_power == 15.0 and p.finish_hole_passes == 30
    assert p.finish_pads_shrink == 10.0         # vernis épargne par défaut
    p.finish_text = ""
    p.finish_holes = True
    p.finish_cut_enabled = True
    p.cut_outline_enabled = True            # les deux cochés : la finition PRIME
    p.cut_outline_margin = 2.0
    g = s.generate_finish_gcode()
    assert "coin de la plaque" not in g.splitlines()[0]   # zéro machine conservé
    lines = g.splitlines()
    idx = {f: [i for i, l in enumerate(lines) if f in l]
           for f in ("F1500", "F600", "F300")}            # peinture / trous / découpe
    assert idx["F1500"] and idx["F600"] and idx["F300"]
    assert max(idx["F1500"]) < min(idx["F600"]) < min(idx["F300"])
    # le retrait des pads (en %) réduit la surface dénudée : la peinture reste
    # sur le pourtour du pad et fait vernis épargne, comme un vrai circuit
    p.finish_cut_enabled = False; p.finish_holes = False
    p.finish_pads_shrink = 0.0
    full = len([l for l in s.generate_finish_gcode().splitlines() if "G1" in l])
    p.finish_pads_shrink = 15.0
    inset = len([l for l in s.generate_finish_gcode().splitlines() if "G1" in l])
    assert inset < full


def test_finish_phase_button_matches_export_with_plate_corner_reference():
    """Le bouton « Finition » doit produire les MEMES coordonnees que l'export
    de finition, repere « coin de la plaque » compris (il l'ignorait)."""
    s = Session()
    s.import_source(ODBPP)
    p = s.project
    p.finish_text = ""; p.finish_holes = False; p.finish_cut_enabled = False
    p.cut_outline_margin = 2.0
    p.finish_ref_plate_corner = True
    p.origin = (144.0, 168.0)                   # plateau : centrage automatique
    export = s.generate_finish_gcode(include_cut=False)
    phase = s.generate_phase_gcode("finition")
    assert phase == export
    assert "coin de la plaque" in phase.splitlines()[0]
    p.finish_ref_plate_corner = False
    assert s.generate_phase_gcode("finition") == s.generate_finish_gcode(include_cut=False)


def test_plate_bounds_always_include_cut_margin():
    s = Session()
    s.import_source(ODBPP)
    p = s.project
    p.cut_outline_margin = 1.5
    p.cut_outline_enabled = False               # le bouton Decouper coupe quand meme
    b = s.board.bounds
    assert s.plate_bounds() == (b[0] - 1.5, b[1] - 1.5, b[2] + 1.5, b[3] + 1.5)


def test_drc_summary_cached_per_import():
    s = Session()
    s.import_source(ODBPP)
    d = s.drc_summary()
    assert set(d) == {l.config.name for l in s.layers}
    assert all(v >= 0 for v in d.values())
    assert s.drc_summary() is d                 # cache


def test_dessin_integre_au_job_et_lancable_seul():
    """Le dessin suit le même circuit que les trous ou la découpe.

    Il part dans le job complet quand il est activé, et son bouton d'étape le
    lance seul — indépendamment de cette case.
    """
    from laserpcb.drawing import KIND_RECT, KIND_TEXT, Shape
    s = Session()
    s.import_source(GERBER)
    s.active_side = "top"
    nu = len(s.active_items_tagged())

    s.project.shapes = [
        Shape(KIND_RECT, {"x": 5, "y": 5, "w": 20, "h": 10}, layer="c1", id="o1"),
        Shape(KIND_TEXT, {"x": 5, "y": 20, "text": "LASER", "height": 4},
              layer="c2", id="o2"),
    ]
    s.work_layers()                      # crée les calques par défaut
    dessin = s.drawing_items_tagged()
    assert dessin and all(it[3] == "dessin" for it in dessin)
    assert len(s.active_items_tagged()) == nu + len(dessin)

    # case décochée : hors du job complet, mais l'étape marche toujours
    s.project.drawing_enabled = False
    assert len(s.active_items_tagged()) == nu
    assert len(s.drawing_items_tagged()) == len(dessin)
    g = s.generate_phase_gcode("dessin")
    assert "G1" in g

    # un calque désactivé retire ses objets, et le cache le voit
    s.project.drawing_enabled = True
    s.layer_by_id("c1").enabled = False
    assert len(s.active_items_tagged()) < nu + len(dessin)


def test_modifier_une_forme_invalide_le_cache():
    """Un paramètre changé doit être suivi d'effet : sinon le cache ment."""
    from laserpcb.drawing import KIND_RECT, Shape
    s = Session()
    s.import_source(GERBER)
    s.active_side = "top"
    s.project.shapes = [Shape(KIND_RECT, {"x": 0, "y": 0, "w": 20, "h": 10},
                              layer="c1", id="o1")]
    s.work_layers()

    def largeur():
        xs = [x for it in s.drawing_items_tagged() for (x, _y) in it[0]]
        return max(xs) - min(xs)

    assert largeur() == pytest.approx(20.0)
    s.project.shapes[0].params["w"] = 40
    assert largeur() == pytest.approx(40.0)


def test_travailler_sans_aucun_pcb():
    """Feuille vierge : l'emprise du travail vient du DESSIN, pas d'une carte.

    C'est ce qui permet de graver un boîtier, une plaque ou un pochoir sans
    jamais importer de Gerber.
    """
    from laserpcb.drawing import KIND_RECT, KIND_TEXT, Shape
    s = Session()                                  # aucun import
    assert s.board is None and not s.layers
    assert s.content_bounds() is None              # rien à graver pour l'instant

    s.work_layers()
    s.project.shapes = [
        Shape(KIND_RECT, {"x": 10, "y": 10, "w": 60, "h": 40}, layer="c1", id="o1"),
        Shape(KIND_TEXT, {"x": 15, "y": 55, "text": "BOITIER", "height": 8},
              layer="c2", id="o2"),
    ]
    b = s.content_bounds()
    assert b is not None and b[0] == pytest.approx(10.0) and b[2] == pytest.approx(70.0)

    s.center_on_bed()
    cx = s.project.origin[0] + (b[0] + b[2]) / 2
    assert cx == pytest.approx(s.project.bed_width / 2)
    assert s.piece_origin() == pytest.approx(
        (b[0] + s.project.origin[0], b[1] + s.project.origin[1]))
    assert s.fits_on_bed()

    items = s.active_items_tagged()
    assert items and all(it[3] == "dessin" for it in items)
    g = s.generate_gcode()
    assert "G1" in g and "M2" in g
