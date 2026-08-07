"""Round de test complet bout-en-bout (laser mocké FakeGRBL).

Exerce toutes les fonctionnalités sur un vrai projet, génère le G-code de chaque
face, le streame vers un FakeGRBL, et vérifie la validité (coords finies, dans le
plateau, protocole GRBL). Retourne un code != 0 si un problème est détecté.

    python scripts/e2e_test.py [chemin_projet]   # défaut : examples/solar_pi_odb
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laserpcb.gui.session import Session                       # noqa: E402
from laserpcb.project import (MODE_CENTERLINE, MODE_ISOLATION,  # noqa: E402
                              MODE_NEGATIVE)
from laserpcb.sender import FakeGrbl, GrblSender, clean_lines   # noqa: E402
from laserpcb import toolpath as tp, odb                        # noqa: E402

FAILURES: list[str] = []


def check(cond, msg):
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        FAILURES.append(msg)


def coords_of(gcode):
    xs = [float(v) for v in re.findall(r"X(-?[\d.]+)", gcode)]
    ys = [float(v) for v in re.findall(r"Y(-?[\d.]+)", gcode)]
    return xs, ys


def main(path):
    print(f"=== E2E : {path} ===")
    s = Session()
    s.import_source(path)
    print(f"\n[import] {s.project.source_kind}, {len(s.layers)} calques, {len(s.holes)} trous, "
          f"board={tuple(round(v) for v in s.board.bounds)}")
    check(len(s.layers) >= 1, "au moins un calque cuivre")
    check(len(s.holes) > 0, "trous détectés")

    # --- centrage plateau ---
    s.center_on_bed()
    print(f"[plateau] {s.project.bed_width}x{s.project.bed_height} mm, origin={tuple(round(v,1) for v in s.project.origin)}")

    # --- chaque face x chaque mode ---
    for side in ("top", "bottom"):
        s.active_side = side
        for mode in (MODE_CENTERLINE, MODE_ISOLATION, MODE_NEGATIVE):
            for layer in s.active_layers():
                layer.config.mode = mode
                layer.config.adaptive = True
            g = s.generate_gcode()
            xs, ys = coords_of(g)
            print(f"[{side}/{mode}] {len(g.splitlines())} lignes, "
                  f"X[{min(xs):.0f},{max(xs):.0f}] Y[{min(ys):.0f},{max(ys):.0f}]")
            check("M4 S0" in g and g.rstrip().endswith("M2"), f"{side}/{mode}: G-code GRBL valide")
            check(all(map(lambda v: v == v, xs + ys)), f"{side}/{mode}: coords finies (pas de NaN)")
            check(min(xs) >= -1 and max(xs) <= s.project.bed_width + 1,
                  f"{side}/{mode}: X dans le plateau")
            check(min(ys) >= -1 and max(ys) <= s.project.bed_height + 1,
                  f"{side}/{mode}: Y dans le plateau")

    # --- DRC conscient des composants ---
    if s.project.source_kind == "odbpp":
        feats = odb.load_features_by_net(s.source_root, "top")
        res = tp.drc(features=feats)
        print(f"[DRC] {len(res.real_shorts)} vrais courts, {len(res.footprint_overlaps)} footprints")
        check(any(x.kind == "footprint" for x in res.shorts) or not res.shorts,
              "DRC classe les footprints")

    # --- aperçu brûlure (géométrie non vide) ---
    s.active_side = "top"
    for layer in s.active_layers():
        layer.config.mode = MODE_ISOLATION
    items = s.active_items()
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    burn = unary_union([LineString(c).buffer(s.project.kerf / 2) for c, _f, _p in items if len(c) >= 2])
    print(f"[brulure] surface gravee ~ {burn.area:.0f} mm2")
    check(burn.area > 0, "aperçu brûlure : surface non nulle")

    # --- laser mocké : streaming complet du G-code top ---
    s.active_side = "top"
    g = s.generate_gcode()
    fake = FakeGrbl()
    sender = GrblSender(fake)
    sender.connect()
    sender.set_work_zero()
    sender.laser_on(1.0); sender.laser_off()
    prog_lines = clean_lines(g)
    result = sender.stream(g)
    sender.goto_zero()
    print(f"[laser mocké] streamé {result['sent']}/{result['total']} lignes, stopped={result['stopped']}")
    check(result["sent"] == len(prog_lines) and not result["stopped"], "streaming complet sans erreur")
    check("G10 L20 P1 X0 Y0" in fake.received, "origine 0,0 définie")
    check(any(l.startswith("M3 S") for l in fake.received), "pointeur laser allumé")

    print("\n=== " + ("TOUT VERT" if not FAILURES else f"{len(FAILURES)} PROBLÈME(S)") + " ===")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "examples/solar_pi_odb"
    sys.exit(main(p))
