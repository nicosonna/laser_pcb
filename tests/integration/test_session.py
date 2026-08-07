"""Intégration : Session GUI (logique métier, sans Qt).

Import Gerber + ODB++, génération G-code, aller-retour projet embarqué.
Mode isolation offset (rapide) pour rester léger — l'axe médian est testé ailleurs.
"""

from __future__ import annotations

from laserpcb.gui.session import Session
from laserpcb.project import MODE_ISOLATION

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
