"""Session : pont projet <-> géométrie <-> parcours <-> G-code (sans Qt).

Centralise l'état chargé et toute la logique métier, pour que la couche Qt reste
mince et que cette partie soit testable sans interface.
"""

from __future__ import annotations

import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..geometry import Hole, LabeledCopper
from .. import geometry as geo
from .. import odb
from .. import gcode as gc
from .. import toolpath as tp
from ..project import (LayerConfig, Project, MODE_HYBRID, MODE_CENTERLINE,
                       MODE_COMBINED, MODE_ISOLATION, MODE_NEGATIVE)


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
    active_side: str = "top"          # "top" | "bottom" : face vue et exportée
    mirror_bottom: bool = True        # miroir du bottom (plaque retournée)
    _workdir: Path | None = None
    _expand_cache: dict = field(default_factory=dict)  # cache cuivre élargi

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
        """Importe un PCB : dossier (Gerber/ODB++) ou archive ZIP / TGZ / TAR."""
        path = Path(path)
        if path.is_file():
            path = _extract_archive(path)
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

    def _expanded(self, layer: LoadedLayer) -> LabeledCopper:
        """Cuivre élargi de la marge (compensation laser + perchlorure), **plafonné
        par net** : jamais de pont entre deux nets, quelle que soit la marge."""
        margin = self.project.copper_margin
        if margin <= 0:
            return layer.labeled
        key = (id(layer), round(margin, 4), round(self.project.kerf, 4))
        if key not in self._expand_cache:
            self._expand_cache[key] = tp.expand_copper(
                layer.labeled, margin, min_gap=self.project.kerf)
        return self._expand_cache[key]

    def layer_items(self, layer: LoadedLayer):
        """Renvoie les items G-code (coords, feed, power) d'un calque selon son mode.

        Le cuivre est d'abord **élargi** de ``copper_margin`` (pistes/pads plus larges
        pour survivre au laser et à la gravure chimique).
        """
        cfg = layer.config
        cal = self.calibration()
        kerf = self.project.kerf
        base = self._expanded(layer)
        union = base.union

        if cfg.mode == MODE_COMBINED:
            tight, wide = tp.combined_isolation(base, self.board, kerf=kerf)
            items = gc.items_from_segments(tight, cal)     # passe unique dans les gaps serrés
            if not wide.is_empty:                          # évidement des grandes zones
                edge, interior = tp.clearing_zones(wide, union, kerf * 4)
                items += gc.items_from_paths(tp.hatch(edge, spacing=kerf), cal.v_slow, cal.p_full)
                items += gc.items_from_paths(tp.hatch(interior, spacing=kerf), cal.v_fast, cal.p_full)
            return items
        if cfg.mode == MODE_HYBRID:
            # contours sur le cuivre élargi (base), passes centrées sur l'origine
            rings, tight = tp.hybrid_isolation(base, layer.labeled, kerf=kerf)
            items = gc.items_from_paths(rings, cfg.speed, cfg.power)
            items += gc.items_from_segments(tight, cal)   # passes serrées adaptatives
            return items
        if cfg.mode == MODE_CENTERLINE:
            segs = tp.centerline_isolation(base, kerf=kerf)
            if cfg.adaptive:
                return gc.items_from_segments(segs, cal)
            paths = [tp.LineString(s.coords) for s in segs]
            return gc.items_from_paths(paths, cfg.speed, cfg.power)
        if cfg.mode == MODE_ISOLATION:
            rings = tp.isolation_rings(union, kerf=kerf, rings=cfg.rings)
            return gc.items_from_paths(rings, cfg.speed, cfg.power)
        if cfg.mode == MODE_NEGATIVE:
            copper = union
            region = tp.negative_region(copper, self.board)
            spacing = kerf
            if cfg.adaptive:
                # Évidement : liseré lent près des pistes, intérieur rapide au centre
                # des grandes zones (le laser retire tout, mais va vite là où il peut).
                margin = spacing * 4
                edge, interior = tp.clearing_zones(region, copper, margin)
                items = gc.items_from_paths(tp.hatch(edge, spacing=spacing),
                                            cal.v_slow, cal.p_full)
                items += gc.items_from_paths(tp.hatch(interior, spacing=spacing),
                                             cal.v_fast, cal.p_full)
                return items
            hatch = tp.hatch(region, spacing=spacing)
            return gc.items_from_paths(hatch, cfg.speed, cfg.power)
        return []

    def active_layers(self) -> list[LoadedLayer]:
        """Calques de la face active uniquement (on grave une face à la fois)."""
        return [l for l in self.layers if l.config.role == self.active_side]

    def board_center_x(self) -> float:
        minx, _miny, maxx, _maxy = self.board.bounds
        return (minx + maxx) / 2.0

    def is_mirrored(self) -> bool:
        return self.active_side == "bottom" and self.mirror_bottom

    def active_items(self) -> list:
        """Items G-code de la face active (cuivre + trous), NON miroités."""
        items = []
        for layer in self.active_layers():
            if layer.config.enabled:
                pcs = self.layer_items(layer)
                for _ in range(max(1, layer.config.passes)):
                    items.extend(pcs)
        p = self.project
        if p.hole_enabled and self.holes:
            rings = tp.hole_circles(self.holes, p.hole_scale_percent)
            hole_items = gc.items_from_paths(rings, p.hole_speed, p.hole_power)
            for _ in range(max(1, p.hole_passes)):
                items.extend(hole_items)
        return items

    def generate_gcode(self, config: gc.GCodeConfig | None = None) -> str:
        """G-code absolu de la **face active** : miroir (bottom) puis offset plateau.

        Le G-code est en G90 (absolu) depuis l'origine de travail réglée sur la
        machine ; ``project.origin`` positionne le PCB sur le plateau.
        """
        items = self.active_items()
        if self.is_mirrored() and self.board is not None:
            cx = self.board_center_x()
            items = [([(2 * cx - x, y) for (x, y) in coords], f, pw)
                     for coords, f, pw in items]
        ox, oy = self.project.origin
        if ox or oy:
            items = [([(x + ox, y + oy) for (x, y) in coords], f, pw)
                     for coords, f, pw in items]
        title = f"{self.project.name}_{self.active_side}"
        return gc.build(items, config or gc.GCodeConfig(), title=title)

    def center_on_bed(self) -> None:
        """Positionne le PCB au centre du plateau (règle ``project.origin``)."""
        if self.board is None:
            return
        minx, miny, maxx, maxy = self.board.bounds
        w, h = maxx - minx, maxy - miny
        self.project.origin = ((self.project.bed_width - w) / 2 - minx,
                               (self.project.bed_height - h) / 2 - miny)

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


def _extract_archive(path: Path) -> Path:
    """Extrait une archive ZIP / TGZ / TAR(.gz/.bz2) et renvoie sa racine."""
    name = path.name.lower()
    tmp = Path(tempfile.mkdtemp(prefix="lpcb_"))
    if name.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
    elif name.endswith((".tgz", ".tar.gz", ".tar", ".tar.bz2", ".tbz2", ".tar.xz")):
        with tarfile.open(path) as t:
            t.extractall(tmp)
    else:
        raise ValueError(f"Archive non reconnue : {path.name} (ZIP/TGZ/TAR attendus)")
    return _single_root(tmp)


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
