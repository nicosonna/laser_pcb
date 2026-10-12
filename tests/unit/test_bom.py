"""Unités : nomenclature (BOM) depuis les couches composants ODB++."""

from __future__ import annotations

from laserpcb import bom

ODBPP = "examples/solar_pi_odb"


def test_load_components_reads_cmp_and_props():
    comps = bom.load_components(ODBPP)
    assert len(comps) > 5
    bt1 = next(c for c in comps if c.refdes == "BT1")
    assert bt1.part == "BAT_1042P" and bt1.side in ("top", "bottom")
    assert bt1.prop("MF") == "Keystone Electronics"      # propriétés PRP lues
    assert bt1.prop("PACKAGE") == ""                     # 'None' -> vide


def test_aggregate_groups_same_part_and_sorts_naturally():
    comps = bom.load_components(ODBPP)
    lines = bom.aggregate(comps)
    batt = next(l for l in lines if l.part == "BAT_1042P")
    assert batt.qty >= 2 and batt.refs[:2] == ["BT1", "BT2"]   # BT1+BT2 groupés
    assert sum(l.qty for l in lines) == len(comps)             # rien de perdu
    # tri naturel : R2 avant R10
    assert bom._nat_key("R2") < bom._nat_key("R10")


def test_csv_and_html_render():
    lines = bom.aggregate(bom.load_components(ODBPP))
    csv = bom.to_csv(lines)
    assert csv.splitlines()[0].startswith("Qte;References")
    assert "BAT_1042P" in csv
    page = bom.to_html(lines, "solar_pi")
    assert "<table>" in page and "BAT_1042P" in page
    assert "références" in page                                # bandeau de synthèse


def test_aliexpress_links_prefer_mpn():
    """La recherche AliExpress vise la réf. fabricant si connue, sinon la valeur."""
    l_mpn = bom.BomLine(qty=1, refs=["U1"], part="ESP32", value="ESP32-WROVER-I",
                        manufacturer="Espressif",
                        mpn="ESP32-WROVER-I(M213EH2864PH3Q0)",
                        description="d", sides="top")
    assert "ESP32-WROVER-I%28M213EH2864PH3Q0%29" in bom.aliexpress_url(l_mpn)
    l_val = bom.BomLine(qty=6, refs=["C3"], part="0805", value="100nF",
                        manufacturer="", mpn="", description="d", sides="top")
    assert bom.search_query(l_val) == "100nF 0805"
    url = bom.aliexpress_url(l_val)
    assert url.startswith("https://www.aliexpress.com/wholesale?SearchText=")
    assert "100nF+0805" in url
    # rendus : la colonne AliExpress est présente
    assert "aliexpress" in bom.to_csv([l_val]).lower()
    assert "AliExpress" in bom.to_html([l_val], "t")


def test_package_sizes_from_pkg_records():
    """Le boîtier a ses dimensions réelles (records PKG d'eda/data)."""
    comps = bom.load_components(ODBPP)
    c1206 = next(c for c in comps if c.part == "C1206")
    assert c1206.size == "4.4 × 1.8 mm"        # PKG C1206 : bbox ±2.2 × ±0.9
    lines = bom.aggregate(comps)
    l = next(l for l in lines if l.part == "C1206")
    assert l.size == "4.4 × 1.8 mm"
    assert "Dimensions" in bom.to_html(lines, "t") and "Boitier" in bom.to_csv(lines)
