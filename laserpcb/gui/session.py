"""Session : pont projet <-> géométrie <-> parcours <-> G-code (sans Qt).

Centralise l'état chargé et toute la logique métier, pour que la couche Qt reste
mince et que cette partie soit testable sans interface.
"""

from __future__ import annotations

import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..geometry import Hole, LabeledCopper
from .. import geometry as geo
from .. import odb
from .. import gcode as gc
from .. import toolpath as tp
from ..project import (LayerConfig, Project,
                       MODE_CENTERLINE, MODE_ISOLATION, MODE_NEGATIVE)


@dataclass
class LoadedLayer:
    config: LayerConfig
    labeled: LabeledCopper


@dataclass
class Session:
    """État complet d'un travail en cours."""
    project: Project = field(default_factory=Project)
    board = None
    holes: list[Hole] = field(default_factory=list)
    layers: list[LoadedLayer] = field(default_factory=list)
    source_root: Path | None = None
    _workdir: Path | None = None

    # ------------------------------------------------------------------ #
    # Détection / import de source
    # ------------------------------------------------------------------ #
    @staticmethod
    def detect_kind(root: Path) -> str:
        root = Path(root)
        if (root / "steps").is_dir() or list(root.glob("*.odbjob")):
            return "odbpp"
        if list(root.rglob("*.gbr")) or list(root.rglob("*.gtl")):
            return "gerber"
        raise ValueError(f"Format PCB non reconnu dans {root}")

    def import_source(self, path: str | Path) -> None:
        """Importe un dossier PCB (Gerber ou ODB++) ou un ZIP Gerber."""
        path = Path(path)
        if path.is_file() and path.suffix.lower() == ".zip":
            tmp = Path(tempfile.mkdtemp(prefix="lpcb_"))
            with zipfile.ZipFile(path) as z:
                z.extractall(tmp)
            path = _single_root(tmp)
        kind = self.detect_kind(path)
        self.source_root = path
        self.project.source_kind = kind
        self.project.name = path.name
        if kind == "gerber":
            self._load_gerber(path)
        else:
            self._load_odb(path)
        self.project.embed_directory(path)

    def _load_gerber(self, root: Path) -> None:
        gdir = root if list(root.glob("*.gbr")) else root / "GerberFiles"
        profile = _first(gdir, ["profile.gbr", "*rofile*.gbr", "*.gko", "*.gm1"])
        self.board = geo.load_outline(profile)
        self.holes = []
        for xln in list(root.rglob("*.xln")) + list(root.rglob("*.drl")):
            self.holes.extend(geo.load_drill(xln))
        self.layers = []
        for role, pat in (("top", "*opper_top*.gbr"), ("bottom", "*opper_bottom*.gbr")):
            f = _first(gdir, [pat, f"*{role}*.gbr"])
            if f:
                lab = geo.load_labeled_copper(f, profile, name=role)
                self.layers.append(LoadedLayer(LayerConfig(name=role, role=role), lab))

    def _load_odb(self, root: Path) -> None:
        self.board = odb.load_outline(root)
        self.holes = odb.load_drill(root)
        self.layers = []
        for role in ("top", "bottom"):
            try:
                lab = odb.load_labeled_copper(root, role)
            except Exception:
                continue
            self.layers.append(LoadedLayer(LayerConfig(name=role, role=role), lab))

    # ------------------------------------------------------------------ #
    # Génération des parcours et du G-code
    # ------------------------------------------------------------------ #
    def calibration(self) -> tp.Calibration:
        c = tp.Calibration(kerf=self.project.kerf)
        for k, v in self.project.calibration.items():
            if hasattr(c, k):
                setattr(c, k, v)
        return c

    def layer_items(self, layer: LoadedLayer):
        """Renvoie les items G-code (coords, feed, power) d'un calque selon son mode."""
        cfg = layer.config
        cal = self.calibration()
        if cfg.mode == MODE_CENTERLINE:
            segs = tp.centerline_isolation(layer.labeled, kerf=self.project.kerf)
            if cfg.adaptive:
                return gc.items_from_segments(segs, cal)
            paths = [tp.LineString(s.coords) for s in segs]
            return gc.items_from_paths(paths, cfg.speed, cfg.power)
        if cfg.mode == MODE_ISOLATION:
            rings = tp.isolation_rings(layer.labeled.union, kerf=self.project.kerf,
                                       rings=cfg.rings)
            return gc.items_from_paths(rings, cfg.speed, cfg.power)
        if cfg.mode == MODE_NEGATIVE:
            region = tp.negative_region(layer.labeled.union, self.board)
            hatch = tp.hatch(region, spacing=self.project.kerf)
            return gc.items_from_paths(hatch, cfg.speed, cfg.power)
        return []

    def generate_gcode(self, config: gc.GCodeConfig | None = None) -> str:
        items = []
        for layer in self.layers:
            if layer.config.enabled:
                pcs = self.layer_items(layer)
                for _ in range(max(1, layer.config.passes)):
                    items.extend(pcs)
        return gc.build(items, config or gc.GCodeConfig(), title=self.project.name)

    # ------------------------------------------------------------------ #
    # Projet
    # ------------------------------------------------------------------ #
    def sync_project(self) -> Project:
        self.project.layers = [l.config for l in self.layers]
        return self.project

    def save_project(self, path: str | Path) -> None:
        self.sync_project().save(path)

    @classmethod
    def open_project(cls, path: str | Path) -> "Session":
        proj = Project.load(path)
        s = cls(project=proj)
        s._workdir = Path(tempfile.mkdtemp(prefix="lpcb_proj_"))
        root = proj.materialize(s._workdir)
        s.source_root = root
        if proj.source_kind == "gerber":
            s._load_gerber(root)
        else:
            s._load_odb(root)
        # ré-applique les réglages enregistrés
        saved = {lc.name: lc for lc in proj.layers}
        for layer in s.layers:
            if layer.config.name in saved:
                layer.config = saved[layer.config.name]
        return s


def _single_root(tmp: Path) -> Path:
    entries = [p for p in tmp.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return tmp


def _first(folder: Path, patterns: list[str]) -> Path | None:
    for pat in patterns:
        hits = sorted(folder.glob(pat))
        if hits:
            return hits[0]
    return None
