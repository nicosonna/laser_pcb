"""Intégration : Session GUI (logique métier, sans Qt).

Import Gerber + ODB++, génération G-code, aller-retour projet embarqué.
Mode isolation offset (rapide) pour rester léger — l'axe médian est testé ailleurs.
"""

from __future__ import annotations

import tarfile

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


def test_frame_gcode_traces_only_bounding_rectangle():
    """Le cadre ne fait QUE le rectangle englobant (4 coins), pas tout le tracé,
    et à faible puissance — sinon le 'Frame' machine rejoue toute la gravure."""
    import re
    s = Session()
    s.import_source(GERBER)
    _iso(s)
    frame = s.frame_gcode(power=1.0, loops=2)
    cuts = [l for l in frame.splitlines() if l.startswith("G1")]
    assert len(cuts) == 8                     # 4 coins x 2 boucles
    minx, miny, maxx, maxy = s.job_bounds()
    xs = {round(float(re.search(r"X(-?[\d.]+)", l).group(1)), 1) for l in cuts}
    ys = {round(float(re.search(r"Y(-?[\d.]+)", l).group(1)), 1) for l in cuts}
    assert xs == {round(minx, 1), round(maxx, 1)}   # que les bords
    assert ys == {round(miny, 1), round(maxy, 1)}
    powers = {int(re.search(r"S(\d+)", l).group(1)) for l in cuts}
    assert powers == {10}                     # 1% -> S10, ne grave pas
    # bien plus court que la gravure complète
    assert len(cuts) < len(s.generate_gcode().splitlines())


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


def test_finish_cut_and_hole_passes_and_inset():
    """Finition v2 : trous en perçage (30 passes), découpe optionnelle EN DERNIER
    (et alors même zéro que la gravure), retrait des pads, peinture à 20 %."""
    import re
    s = Session()
    s.import_source(ODBPP)
    p = s.project
    assert p.finish_power == 20.0 and p.finish_hole_passes == 30
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
    # le retrait des pads réduit la surface dénudée
    p.finish_cut_enabled = False; p.finish_holes = False
    p.finish_pads_inset = 0.0
    full = len([l for l in s.generate_finish_gcode().splitlines() if "G1" in l])
    p.finish_pads_inset = 0.10
    inset = len([l for l in s.generate_finish_gcode().splitlines() if "G1" in l])
    assert inset < full
