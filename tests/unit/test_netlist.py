"""Unités : mapping feature -> net depuis la netlist ODB++ (eda/data)."""

from __future__ import annotations

from collections import Counter

from laserpcb import netlist


def test_layer_order(odb_root):
    layers = netlist.layer_order(odb_root)
    assert "top" in layers and "bottom" in layers
    # l'index de 'top' sert de clé FID dans eda/data
    assert layers.index("top") >= 0


def test_net_map_top(odb_root):
    m = netlist.load_net_map(odb_root, "top")
    assert len(m) == 1047            # 1048 features, une sans entrée FID
    assert max(m) == 1047
    # GND domine largement (plan de masse)
    common = Counter(m.values()).most_common(1)[0]
    assert common[0] == "GND"
    assert common[1] > 100


def test_net_map_absent_layer(odb_root):
    """Un calque inconnu -> map vide (repli composantes connexes en aval)."""
    assert netlist.load_net_map(odb_root, "inexistant") == {}
