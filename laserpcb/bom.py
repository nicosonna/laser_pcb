"""Nomenclature (BOM) : composants du circuit depuis l'ODB++.

Les couches ``comp_+_top`` / ``comp_+_bot`` listent chaque composant (record
``CMP`` : position, rotation, réf, boîtier) et ses propriétés (records ``PRP`` :
VALUE, MF fabricant, MP réf. fabricant, DESCRIPTION…). On agrège par (part,
valeur) pour produire une nomenclature groupée : quantité + liste des réfs.

Sorties : tableau (GUI), CSV (tableur), HTML autonome et imprimable.
Les Gerber seuls ne contiennent pas les composants -> BOM vide.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_COMP_DIRS = {"top": "comp_+_top", "bottom": "comp_+_bot"}


@dataclass
class Component:
    """Un composant placé (record CMP + propriétés PRP)."""
    refdes: str                 # ex. "BT1", "R12"
    part: str                   # nom du boîtier/part ODB++ (ex. "C1206")
    x: float
    y: float
    rot: float
    side: str                   # "top" | "bottom"
    size: str = ""              # dimensions du boîtier "L × l mm" (record PKG)
    props: dict[str, str] = field(default_factory=dict)

    def prop(self, name: str) -> str:
        v = self.props.get(name, "").strip()
        return "" if v in ("None", "-") else v


@dataclass
class BomLine:
    """Une ligne de nomenclature : un même (part, valeur) regroupé."""
    qty: int
    refs: list[str]
    part: str                   # nom du boîtier (footprint)
    value: str
    manufacturer: str
    mpn: str
    description: str
    sides: str                  # "top", "bottom" ou "top+bottom"
    size: str = ""              # dimensions du boîtier "L × l mm"


# --------------------------------------------------------------------------- #
# Lecture ODB++
# --------------------------------------------------------------------------- #
def _step_dir(job_root: Path, step: str | None = None) -> Path:
    steps = Path(job_root) / "steps"
    if step:
        return steps / step
    return next(p for p in steps.iterdir() if p.is_dir())


def _load_package_sizes(job_root: Path, step: str | None = None) -> list[str]:
    """Dimensions des boîtiers depuis ``eda/data`` (records ``PKG name pitch
    xmin ymin xmax ymax``), indexées dans l'ordre — le champ 1 du CMP y renvoie."""
    path = _step_dir(job_root, step) / "eda" / "data"
    sizes: list[str] = []
    if not path.exists():
        return sizes
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("PKG "):
            t = raw.split()
            try:
                w = float(t[5]) - float(t[3])
                h = float(t[6]) - float(t[4])
                sizes.append(f"{w:.1f} × {h:.1f} mm")
            except (IndexError, ValueError):
                sizes.append("")
    return sizes


def load_components(job_root: str | Path, step: str | None = None) -> list[Component]:
    """Tous les composants du job (top + bottom), avec leurs propriétés."""
    out: list[Component] = []
    pkg_sizes = _load_package_sizes(Path(job_root), step)
    for side, comp_dir in _COMP_DIRS.items():
        path = _step_dir(Path(job_root), step) / "layers" / comp_dir / "components"
        if not path.exists():
            continue
        current: Component | None = None
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("CMP "):
                t = line.split()
                if len(t) >= 7:
                    pkg_id = int(t[1]) if t[1].isdigit() else -1
                    size = pkg_sizes[pkg_id] if 0 <= pkg_id < len(pkg_sizes) else ""
                    current = Component(refdes=t[6], part=t[7] if len(t) > 7 else "",
                                        x=float(t[2]), y=float(t[3]),
                                        rot=float(t[4]), side=side, size=size)
                    out.append(current)
            elif line.startswith("PRP ") and current is not None:
                m = re.match(r"PRP\s+(\S+)\s+'(.*)'\s*$", line)
                if m:
                    current.props[m.group(1)] = m.group(2).strip()
    return out


# --------------------------------------------------------------------------- #
# Agrégation
# --------------------------------------------------------------------------- #
def _nat_key(ref: str):
    """Tri naturel des réfs : R2 avant R10."""
    m = re.match(r"([A-Za-z]+)(\d+)", ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


def aggregate(components: list[Component]) -> list[BomLine]:
    """Regroupe par (part, valeur) -> lignes triées par première réf."""
    groups: dict[tuple, list[Component]] = {}
    for c in components:
        key = (c.part, c.prop("VALUE"))
        groups.setdefault(key, []).append(c)
    lines = []
    for (part, value), comps in groups.items():
        refs = sorted((c.refdes for c in comps), key=_nat_key)
        sides = sorted({c.side for c in comps})
        first = comps[0]
        lines.append(BomLine(
            qty=len(comps), refs=refs, part=part, value=value,
            manufacturer=first.prop("MF"), mpn=first.prop("MP"),
            description=" ".join(first.prop("DESCRIPTION").split()),
            sides="+".join(sides), size=first.size))
    return sorted(lines, key=lambda l: _nat_key(l.refs[0]))


# --------------------------------------------------------------------------- #
# Recherche fournisseur (AliExpress)
# --------------------------------------------------------------------------- #
def search_query(line: BomLine) -> str:
    """Termes de recherche pour une ligne : réf. fabricant si connue (précis),
    sinon valeur + nom de part (ex. « 100nF 0805 »)."""
    if line.mpn:
        return line.mpn
    parts = [line.value or line.part]
    if line.value and line.part and line.part.lower() not in line.value.lower():
        parts.append(line.part)
    return " ".join(p for p in parts if p)


def aliexpress_url(line: BomLine) -> str:
    """Lien de recherche AliExpress pré-rempli pour cette ligne."""
    from urllib.parse import quote_plus
    return ("https://www.aliexpress.com/wholesale?SearchText="
            + quote_plus(search_query(line)))


# --------------------------------------------------------------------------- #
# Rendus
# --------------------------------------------------------------------------- #
_CSV_HEAD = ["Qte", "References", "Valeur", "Boitier", "Dimensions", "Fabricant",
             "Ref fabricant", "Face", "Description", "AliExpress"]


def to_csv(lines: list[BomLine]) -> str:
    """CSV (séparateur ;, ouvrable dans un tableur FR)."""
    def esc(v: str) -> str:
        return '"' + v.replace('"', '""') + '"' if any(ch in v for ch in ';"\n') else v
    rows = [";".join(_CSV_HEAD)]
    for l in lines:
        rows.append(";".join(esc(v) for v in (
            str(l.qty), " ".join(l.refs), l.value, l.part, l.size,
            l.manufacturer, l.mpn, l.sides, l.description, aliexpress_url(l))))
    return "\n".join(rows) + "\n"


def to_html(lines: list[BomLine], title: str = "Nomenclature") -> str:
    """Page HTML autonome, propre à l'écran comme à l'impression."""
    total = sum(l.qty for l in lines)
    e = html.escape
    rows = []
    for i, l in enumerate(lines):
        url = aliexpress_url(l)
        rows.append(f"""      <tr>
        <td class="qty">{l.qty}</td>
        <td class="refs">{e(" ".join(l.refs))}</td>
        <td><b>{e(l.value or l.part)}</b></td>
        <td>{e(l.part)}</td>
        <td class="size">{e(l.size)}</td>
        <td>{e(l.manufacturer)}</td>
        <td class="mpn">{e(l.mpn)}</td>
        <td class="side">{e(l.sides)}</td>
        <td class="desc">{e(l.description)}</td>
        <td class="shop"><a class="ali" href="{e(url)}" target="_blank"
            rel="noopener">🔍 AliExpress</a></td>
      </tr>""")
    body = "\n".join(rows)
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>BOM — {e(title)}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font: 14px/1.45 system-ui, "Segoe UI", sans-serif; margin: 2rem auto;
         max-width: 1100px; color: #1c2430; padding: 0 1rem; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 .2rem; }}
  .meta {{ color: #5a6572; margin-bottom: 1.2rem; }}
  .badge {{ display: inline-block; background: #eef3fb; border: 1px solid #c9d8ef;
           border-radius: 999px; padding: .1rem .7rem; margin-right: .5rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th {{ text-align: left; font-size: .78rem; letter-spacing: .04em;
       text-transform: uppercase; color: #47566b; border-bottom: 2px solid #2b70d6;
       padding: .45rem .6rem; }}
  td {{ border-bottom: 1px solid #e3e8ef; padding: .45rem .6rem; vertical-align: top; }}
  tr:nth-child(even) td {{ background: #f7f9fc; }}
  .qty {{ text-align: center; font-weight: 700; }}
  .refs {{ font-family: ui-monospace, Consolas, monospace; font-size: .85rem; }}
  .mpn {{ font-family: ui-monospace, Consolas, monospace; font-size: .85rem; }}
  .side {{ white-space: nowrap; }}
  .desc {{ color: #4a5563; font-size: .85rem; }}
  .size {{ white-space: nowrap; color: #47566b; font-size: .85rem; }}
  .shop {{ white-space: nowrap; }}
  a.ali {{ color: #c8461f; text-decoration: none; font-weight: 600;
          font-size: .85rem; }}
  a.ali:hover {{ text-decoration: underline; }}
  #openall {{ background: #c8461f; color: #fff; border: 0; border-radius: 6px;
             padding: .35rem .9rem; cursor: pointer; font: inherit; }}
  footer {{ margin-top: 1.4rem; color: #8a94a1; font-size: .8rem; }}
  @media print {{ body {{ margin: 0; }} .badge {{ border-color: #999; }}
                 .shop, #openall {{ display: none; }} }}
</style>
</head>
<body>
  <h1>Nomenclature — {e(title)}</h1>
  <div class="meta">
    <span class="badge">{len(lines)} références</span>
    <span class="badge">{total} composants</span>
    <span class="badge">{date.today().isoformat()}</span>
    <button id="openall" title="Ouvre un onglet de recherche AliExpress par ligne
(autorisez les pop-ups pour ce fichier)">🛒 Tout ouvrir sur AliExpress</button>
  </div>
  <table>
    <thead>
      <tr><th>Qté</th><th>Références</th><th>Valeur</th><th>Boîtier</th>
          <th>Dimensions</th><th>Fabricant</th><th>Réf. fabricant</th><th>Face</th>
          <th>Description</th><th>Achat</th></tr>
    </thead>
    <tbody>
{body}
    </tbody>
  </table>
  <footer>Généré par laser_pcb. Liens = recherche pré-remplie : choisissez le bon
  lot/vendeur puis ajoutez au panier.</footer>
  <script>
    document.getElementById("openall").addEventListener("click", () => {{
      document.querySelectorAll("a.ali").forEach((a, i) =>
        setTimeout(() => window.open(a.href, "_blank"), i * 150));
    }});
  </script>
</body>
</html>
"""
