"""Intégration bout-en-bout : ODB++ -> shapely -> axe médian net-aware -> G-code.

On travaille sur la zone du QFN (fenêtre réduite) pour rester rapide, puis on
reparse le G-code émis et on vérifie qu'il reproduit bien les parcours calculés.
"""

from __future__ import annotations

import math
import re

from shapely import box

from laserpcb import odb, toolpath as tp, gcode as gc
from laserpcb.geometry import LabeledCopper

ZONE = box(55, 36, 71, 52)


def _clip_labeled(odb_root) -> LabeledCopper:
    top = odb.load_labeled_copper(odb_root, "top")
    clipped = {n: g.intersection(ZONE) for n, g in top.net_geoms.items()}
    clipped = {n: g for n, g in clipped.items() if not g.is_empty}
    return LabeledCopper("top", clipped, ZONE, has_nets=True)


def _reparse_cut_length(prog: str) -> float:
    x = y = 0.0
    total = 0.0
    rx, ry = re.compile(r"X(-?[\d.]+)"), re.compile(r"Y(-?[\d.]+)")
    for line in prog.splitlines():
        if not line[:2] in ("G0", "G1"):
            continue
        nx = float(rx.search(line).group(1)) if rx.search(line) else x
        ny = float(ry.search(line).group(1)) if ry.search(line) else y
        if line.startswith("G1"):
            total += math.hypot(nx - x, ny - y)
        x, y = nx, ny
    return total


def test_full_pipeline_odb_to_gcode(odb_root):
    lab = _clip_labeled(odb_root)
    assert lab.has_nets and "GND" in lab.net_geoms

    cal = tp.Calibration()
    segs = tp.centerline_isolation(lab, kerf=cal.kerf, step=0.1)
    assert len(segs) > 100                       # QFN dense -> beaucoup de segments

    items = gc.items_from_segments(segs, cal)
    prog = gc.build(items, gc.GCodeConfig(), title="pipeline test")

    # le G-code contient de l'adaptatif : plusieurs vitesses F distinctes
    feeds = set(re.findall(r"F(\d+)", prog))
    assert len(feeds) > 3

    # longueur gravée relue ~= longueur calculée (tolérance : trajets G0 exclus)
    calc = gc.cut_length(items)
    reparsed = _reparse_cut_length(prog)
    assert reparsed >= calc * 0.98               # les coupes sont bien émises


def test_drc_runs_on_full_layer(odb_root):
    """Le DRC tourne sur un vrai calque et renvoie un résultat exploitable."""
    top = odb.load_labeled_copper(odb_root, "top")
    res = tp.drc(top, kerf=0.15)
    assert isinstance(res.shorts, list)
    # au moins le contact N$35/SW1 repéré (corroboré visuellement)
    pairs = {frozenset((s.net_a, s.net_b)) for s in res.shorts}
    assert frozenset(("N$35", "SW1")) in pairs


def test_drc_component_aware_reclassifies_wago(odb_root):
    """Conscience composants : le chevauchement GND/N$7 du connecteur WAGO P1 est
    reclassé « footprint » (faux court éliminé) ; N$35/SW1 reste un vrai court."""
    feats = odb.load_features_by_net(odb_root, "top")
    by_pair = {frozenset((s.net_a, s.net_b)): s for s in tp.drc(features=feats).shorts}

    wago = by_pair[frozenset(("GND", "N$7"))]
    assert wago.kind == "footprint" and wago.component == "P1"

    sw = by_pair[frozenset(("N$35", "SW1"))]
    assert sw.kind == "short"                 # piste sans composant -> vrai court
