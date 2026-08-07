"""Unités : projets .lpcb (round-trip + source embarquée)."""

from __future__ import annotations

from laserpcb.project import LayerConfig, Project, MODE_NEGATIVE


def test_project_roundtrip(tmp_path):
    p = Project(name="essai", source_kind="odbpp", kerf=0.2,
                layers=[LayerConfig(name="top", role="top", mode=MODE_NEGATIVE,
                                    passes=3, speed=600, power=70, adaptive=False)])
    f = tmp_path / "essai.lpcb"
    p.save(f)
    q = Project.load(f)
    assert q.name == "essai" and q.source_kind == "odbpp"
    assert q.kerf == 0.2
    assert len(q.layers) == 1
    lc = q.layers[0]
    assert lc.name == "top" and lc.mode == MODE_NEGATIVE
    assert lc.passes == 3 and lc.adaptive is False


def test_embed_and_materialize(gerber_dir, tmp_path):
    """Le dossier source embarqué se recrée à l'identique (portabilité)."""
    p = Project(name="g")
    p.embed_directory(gerber_dir)
    assert any(k.endswith("copper_top.gbr") for k in p.source_files)

    dest = tmp_path / "out"
    p.materialize(dest)
    original = (gerber_dir / "copper_top.gbr").read_bytes()
    restored = (dest / "copper_top.gbr").read_bytes()
    assert restored == original
