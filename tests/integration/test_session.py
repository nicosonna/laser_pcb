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


def test_hybrid_is_default_and_generates():
    """Le mode par défaut est l'hybride et produit un G-code valide."""
    from laserpcb.project import MODE_HYBRID
    s = Session()
    s.import_source(ODBPP)
    assert all(l.config.mode == MODE_HYBRID for l in s.layers)
    g = s.generate_gcode()
    assert "M4 S0" in g and g.rstrip().endswith("M2")


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
