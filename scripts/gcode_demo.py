"""Démo génération G-code : isolation constante + axe médian adaptatif.

    python scripts/gcode_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from shapely import box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laserpcb import geometry as geo, odb, toolpath as tp, gcode as gc  # noqa: E402
from laserpcb.geometry import LabeledCopper  # noqa: E402

OUT = Path("output"); OUT.mkdir(exist_ok=True)


def demo_constant():
    print("== A) Isolation offset (réglage constant) — module_relais bottom ==")
    cop = geo.load_copper("examples/module_relais/GerberFiles/copper_bottom.gbr", "bottom")
    rings = tp.isolation_rings(cop.geometry, kerf=0.15, rings=1)
    items = gc.items_from_paths(rings, speed=900, power=85)
    prog = gc.build(items, gc.GCodeConfig(), passes=2, title="module_relais bottom - isolation")
    (OUT / "iso_bottom.gcode").write_text(prog)
    print(f"   parcours={len(items)}  longueur={gc.cut_length(items):.0f} mm  "
          f"temps~{gc.estimate_minutes(items, passes=2):.1f} min (2 passes)")
    print(f"   -> output/iso_bottom.gcode ({len(prog.splitlines())} lignes)")


def demo_adaptive():
    print("\n== B) Axe médian ADAPTATIF (F/S par segment) — QFN solar_pi ==")
    top = odb.load_labeled_copper("examples/solar_pi_odb", "top")
    zone = box(55, 36, 71, 52)
    clipped = {n: g.intersection(zone) for n, g in top.net_geoms.items()}
    clipped = {n: g for n, g in clipped.items() if not g.is_empty}
    lab = LabeledCopper(layer="top", net_geoms=clipped, board=zone, has_nets=True)

    cal = tp.Calibration()
    segs = tp.centerline_isolation(lab, kerf=cal.kerf, step=0.08)
    items = gc.items_from_segments(segs, cal)
    prog = gc.build(items, gc.GCodeConfig(), passes=1, title="solar_pi QFN - axe median adaptatif")
    (OUT / "adaptive_qfn.gcode").write_text(prog)

    feeds = sorted({round(f) for _, f, _ in items})
    powers = sorted({round(p) for _, _, p in items})
    print(f"   segments={len(items)}  longueur={gc.cut_length(items):.0f} mm  "
          f"temps~{gc.estimate_minutes(items):.1f} min")
    print(f"   vitesses distinctes : {feeds[0]}..{feeds[-1]} mm/min  "
          f"puissances : {powers[0]}..{powers[-1]} %")
    print(f"   -> output/adaptive_qfn.gcode ({len(prog.splitlines())} lignes)")

    print("\n   extrait (variation F/S visible) :")
    body = [l for l in prog.splitlines() if l.startswith("G1") and ("F" in l or "S" in l)]
    for l in body[:12]:
        print("     " + l)


if __name__ == "__main__":
    demo_constant()
    demo_adaptive()
