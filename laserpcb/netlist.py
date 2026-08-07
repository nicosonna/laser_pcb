"""Lecture de la netlist ODB++ : mapping feature -> net.

Le fichier ``steps/<step>/eda/data`` (Autodesk Fusion & co.) associe chaque feature
cuivre à son net :

    LYR solder_paste_top silkscreen_top soldermask_top top bottom ...   (ordre = index)
    NET GND;0=0;ID=2911
    FID C 3 14      -> feature 14 du calque d'index 3 (= top) appartient à GND

On en tire un dict {index_feature: nom_net} par calque, utilisé pour l'isolation
net-aware et le DRC. Absent (Gerber, ou ODB++ sans eda/data) -> on retombe sur un
étiquetage par composante connexe.
"""

from __future__ import annotations

import re
from pathlib import Path

from .odb import _step_dir


def _eda_path(job_root: str | Path, step: str | None = None) -> Path:
    return _step_dir(Path(job_root), step) / "eda" / "data"


def layer_order(job_root: str | Path, step: str | None = None) -> list[str]:
    """Liste ordonnée des calques (l'index = position dans cette liste)."""
    path = _eda_path(job_root, step)
    if not path.exists():
        return []
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("LYR "):
            return line.split()[1:]
    return []


def load_net_map(job_root: str | Path, layer: str,
                 step: str | None = None) -> dict[int, str]:
    """Renvoie {index_feature: nom_net} pour un calque, ou {} si indisponible."""
    path = _eda_path(job_root, step)
    if not path.exists():
        return {}
    layers = layer_order(job_root, step)
    if layer not in layers:
        return {}
    li = layers.index(layer)

    net_map: dict[int, str] = {}
    current: str | None = None
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"NET\s+(\S+?);", line)
        if m:
            current = m.group(1)
            continue
        m = re.match(r"FID\s+C\s+(\d+)\s+(\d+)", line)
        if m and int(m.group(1)) == li:
            net_map[int(m.group(2))] = current
    return net_map
