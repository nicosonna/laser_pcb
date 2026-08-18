"""Session : pont projet <-> géométrie <-> parcours <-> G-code (sans Qt).

Centralise l'état chargé et toute la logique métier, pour que la couche Qt reste
mince et que cette partie soit testable sans interface.
"""

from __future__ import annotations

import tarfile
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
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
    _pads_cache: dict = field(default_factory=dict)    # cache pads (zones de soudure)

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
        """Items G-code (coords, feed, power) d'un calque — API stable (3-uplets)."""
        return [(c, f, p) for (c, f, p, _k) in self._layer_items_tagged(layer)]

    def _layer_items_tagged(self, layer: LoadedLayer):
        """Comme ``layer_items`` mais chaque item porte une **catégorie** en 4e position
        ("contour" = tour du footprint, "fill" = remplissage) pour l'aperçu couleur.

        Le cuivre est d'abord **élargi** de ``copper_margin`` (pistes/pads plus larges
        pour survivre au laser et à la gravure chimique).
        """
        cfg = layer.config
        cal = self.calibration()
        kerf = self.project.kerf
        passes = max(1, cfg.passes)
        base = self._expanded(layer)
        union = base.union

        if cfg.mode == MODE_COMBINED:
            # PERF multi-cœurs : shapely/scipy libèrent le GIL -> l'axe médian
            # (passes centrées) et les remplissages tournent EN PARALLÈLE.
            with ThreadPoolExecutor(max_workers=8) as ex:
                # Canaux ÉTROITS (gap < 2·kerf : gap inversé OU traits qui se
                # chevaucheraient) : les anneaux y sont retirés (fermeture morpho)
                # -> UN SEUL passage centré, affiché à part.
                f_tight = ex.submit(tp.tight_gap_passes, base, kerf=kerf)
                rings, wide = tp.combined_isolation(base, self.board, kerf=kerf)
                # anneau de contour = LE TOUR DU FOOTPRINT (bord net, hugs cuivre),
                # N passes ; puis évidement du fond (H+V si trame croisée).
                items = _tag(gc.items_from_paths(rings, cfg.speed, cfg.power),
                             "contour") * passes
                fills = []
                if not wide.is_empty:
                    edge, interior = tp.clearing_zones(wide, union, kerf * 4)
                    angles = (0.0, 90.0) if cfg.crosshatch else (0.0,)
                    for ang in angles:
                        fills.append((ex.submit(tp.hatch, edge, spacing=kerf, angle=ang),
                                      cal.v_slow))
                        fills.append((ex.submit(tp.hatch, interior, spacing=kerf, angle=ang),
                                      cal.v_fast))
                tight = f_tight.result()
                if tight:
                    paths = [tp.LineString(s.coords) for s in tight]
                    items += _tag(gc.items_from_paths(paths, cfg.speed, cfg.power),
                                  "tight") * passes
                for fut, speed in fills:
                    items += _tag(gc.items_from_paths(fut.result(), speed, cal.p_full),
                                  "fill")
            return items
        if cfg.mode == MODE_HYBRID:
            # contours sur le cuivre élargi (base), passes centrées sur l'origine
            rings, tight = tp.hybrid_isolation(base, layer.labeled, kerf=kerf)
            items = _tag(gc.items_from_paths(rings, cfg.speed, cfg.power), "contour")
            items += _tag(gc.items_from_segments(tight, cal), "contour")
            return items
        if cfg.mode == MODE_CENTERLINE:
            segs = tp.centerline_isolation(base, kerf=kerf)
            if cfg.adaptive:
                return _tag(gc.items_from_segments(segs, cal), "contour")
            paths = [tp.LineString(s.coords) for s in segs]
            return _tag(gc.items_from_paths(paths, cfg.speed, cfg.power), "contour")
        if cfg.mode == MODE_ISOLATION:
            rings = tp.isolation_rings(union, kerf=kerf, rings=cfg.rings)
            return _tag(gc.items_from_paths(rings, cfg.speed, cfg.power), "contour") * passes
        if cfg.mode == MODE_NEGATIVE:
            copper = union
            region = tp.negative_region(copper, self.board)
            spacing = kerf
            # Trame croisée : balayage horizontal PUIS vertical -> mouvement franc H/V,
            # aucun contour ondulé (précis, sans oscillation), meilleur retrait peinture.
            angles = (0.0, 90.0) if cfg.crosshatch else (0.0,)
            if cfg.adaptive:
                # Évidement : liseré lent près des pistes, intérieur rapide au centre
                # des grandes zones. Remplissages calculés en parallèle (multi-cœurs).
                margin = spacing * 4
                edge, interior = tp.clearing_zones(region, copper, margin)
                with ThreadPoolExecutor(max_workers=8) as ex:
                    fills = []
                    for ang in angles:
                        fills.append((ex.submit(tp.hatch, edge, spacing=spacing, angle=ang),
                                      cal.v_slow))
                        fills.append((ex.submit(tp.hatch, interior, spacing=spacing, angle=ang),
                                      cal.v_fast))
                    items = []
                    for fut, speed in fills:
                        items += _tag(gc.items_from_paths(fut.result(), speed, cal.p_full),
                                      "fill")
                return items
            items = []
            for ang in angles:
                items += _tag(gc.items_from_paths(tp.hatch(region, spacing=spacing, angle=ang),
                                                  cfg.speed, cfg.power), "fill")
            return items
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
        """Items G-code (coords, feed, power) de la face active — API stable (3-uplets)."""
        return [(c, f, p) for (c, f, p, _k) in self.active_items_tagged()]

    def active_items_tagged(self) -> list:
        """Items de la face active **avec catégorie** (contour / fill / hole / cut),
        NON miroités. Sert au G-code (on ignore la catégorie) ET à l'aperçu couleur.

        Les passes sont gérées **dans** ``_layer_items_tagged`` (le contour est répété
        N fois, mais pas l'évidement raster qui ne se fait qu'en 1 passe)."""
        items = []
        for layer in self.active_layers():
            if layer.config.enabled:
                items.extend(self._layer_items_tagged(layer))
        p = self.project
        if p.hole_enabled and self.holes:
            rings = tp.hole_circles(self.holes, p.hole_scale_percent)
            hole_items = _tag(gc.items_from_paths(rings, p.hole_speed, p.hole_power), "hole")
            for _ in range(max(1, p.hole_passes)):
                items.extend(hole_items)
        # Découpe du contour de carte (FR4), optionnelle : pleine puissance, N passes
        if p.cut_outline_enabled and self.board is not None and not self.board.is_empty:
            from shapely.geometry import LineString
            lines = [LineString(self.board.exterior.coords)]
            lines += [LineString(r.coords) for r in self.board.interiors]
            cut = _tag(gc.items_from_paths(lines, p.cut_outline_speed, p.cut_outline_power), "cut")
            items += cut * max(1, p.cut_outline_passes)
        return items

    def estimate_minutes(self, items=None) -> float:
        """Durée de gravure estimée (min) de la face active, trajets à vide inclus.
        ``items`` permet de réutiliser un calcul de parcours déjà fait (évite un
        recalcul lourd)."""
        items = self.active_items() if items is None else items
        return gc.estimate_total_minutes(items)

    # ------------------------------------------------------------------ #
    # Finition (après perchlorure) : pads + trous + texte
    # ------------------------------------------------------------------ #
    def pads_for(self, role: str):
        """Zones de soudure (pads) de la face ``role`` — chargées à la demande."""
        from shapely.geometry import MultiPolygon
        if role in self._pads_cache:
            return self._pads_cache[role]
        geom = MultiPolygon()
        root = self.source_root
        try:
            if root is not None:
                if self.project.source_kind == "gerber":
                    gdir = root if list(root.glob("*.gbr")) else root / "GerberFiles"
                    f = _first(gdir, [f"*opper_{role}*.gbr", f"*{role}*.gbr"])
                    if f:
                        geom = geo.load_pads(f)
                else:
                    geom = odb.load_pads(root, role)
        except Exception:
            pass                                   # pas de pads -> finition trous/texte
        self._pads_cache[role] = geom
        return geom

    def finish_items_tagged(self) -> list:
        """Job de FINITION (après perchlorure) : retirer la peinture des zones de
        soudure (pads), des trous, et graver un texte — face active, non miroité.

        Chaque zone est détourée (bord net) puis remplie (hachures au pas du kerf)."""
        p = self.project
        kerf = p.kerf
        items = []
        fill_geoms = []
        if p.finish_pads:
            pads = self.pads_for(self.active_side)
            if pads is not None and not pads.is_empty:
                fill_geoms.append(pads)
        if p.finish_text.strip():
            txt = tp.text_polygons(p.finish_text, p.finish_text_height,
                                   p.finish_text_x, p.finish_text_y)
            if not txt.is_empty:
                fill_geoms.append(txt)
        for geom in fill_geoms:
            rings = [tp.LineString(r.coords)
                     for poly in getattr(geom, "geoms", [geom])
                     for r in [poly.exterior, *poly.interiors]]
            items += _tag(gc.items_from_paths(rings, p.finish_speed, p.finish_power),
                          "contour")
            items += _tag(gc.items_from_paths(tp.hatch(geom, spacing=kerf),
                                              p.finish_speed, p.finish_power), "fill")
        if p.finish_holes and self.holes:
            circles = tp.hole_circles(self.holes, p.hole_scale_percent)
            items += _tag(gc.items_from_paths(circles, p.hole_speed, p.hole_power),
                          "hole")
        return items * max(1, p.finish_passes)

    def generate_finish_gcode(self, config: gc.GCodeConfig | None = None) -> str:
        """G-code de finition (face active, miroir/offset comme la gravure)."""
        items = [(c, f, pw) for (c, f, pw, _k) in self.finish_items_tagged()]
        placed = self.placed_items(items)
        title = f"{self.project.name}_{self.active_side}_finition"
        return gc.build(placed, config or gc.GCodeConfig(), title=title)

    def burn_geometry(self):
        """Géométrie de la zone gravée (union des traits épaissis du kerf) — calcul
        lourd, à lancer dans un thread (pas dans le rendu qui est sur la GUI).

        Perf grands PCB : buffer **vectorisé** (un appel C pour tous les traits) puis
        union **hiérarchique en parallèle** (multi-cœurs, shapely libère le GIL)."""
        import numpy as np
        import shapely
        from shapely.geometry import LineString
        from shapely.ops import unary_union
        coords = [c for c, _f, _p in self.active_items() if len(c) >= 2]
        if not coords:
            return None
        lines = np.empty(len(coords), dtype=object)
        lines[:] = [LineString(c) for c in coords]
        # quad_segs=3 : capuchons grossiers (rayon kerf/2 ~ 0,05 mm -> invisible),
        # 2-3× moins de sommets à unioner.
        strokes = shapely.buffer(lines, self.project.kerf / 2,
                                 cap_style="round", quad_segs=3)
        # union par morceaux en parallèle, puis union finale (le meilleur mesuré :
        # 50 s -> 27 s sur solar_pi ; un arbre de fusion complet est CONTRE-productif)
        parts = [strokes[i:i + 2000] for i in range(0, len(strokes), 2000)]
        with ThreadPoolExecutor(max_workers=8) as ex:
            partials = list(ex.map(unary_union, parts))
        return unary_union(partials)

    def placed_items(self, items=None) -> list:
        """``active_items`` **placés** : miroir (bottom) puis offset plateau — c.-à-d.
        les coordonnées machine réelles envoyées à la graveuse. ``items`` réutilise
        un calcul déjà fait (sinon recalcule ``active_items``)."""
        items = self.active_items() if items is None else items
        if self.is_mirrored() and self.board is not None:
            cx = self.board_center_x()
            items = [([(2 * cx - x, y) for (x, y) in coords], f, pw)
                     for coords, f, pw in items]
        ox, oy = self.project.origin
        if ox or oy:
            items = [([(x + ox, y + oy) for (x, y) in coords], f, pw)
                     for coords, f, pw in items]
        return items

    def job_bounds(self) -> tuple[float, float, float, float] | None:
        """Rectangle englobant (minx, miny, maxx, maxy) du job placé, ou None."""
        xs, ys = [], []
        for coords, _f, _p in self.placed_items():
            for x, y in coords:
                xs.append(x); ys.append(y)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def frame_gcode(self, power: float = 1.0, feed: float = 3000.0,
                    loops: int = 2, max_s: int = 1000) -> str:
        """G-code de **cadrage** : trace le rectangle englobant du job à faible
        puissance (visible mais sans graver), pour positionner la plaque.

        Contrairement au bouton « Frame » de la machine — qui, sur un parcours
        vectoriel, rejoue tout le tracé — ceci ne fait QUE les 4 coins."""
        b = self.job_bounds()
        if b is None:
            return ""
        minx, miny, maxx, maxy = b
        s = int(round(max(0.0, power) / 100.0 * max_s))
        corners = [(maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
        lines = [
            f"; cadre {self.project.name}_{self.active_side} "
            f"({maxx - minx:.1f} x {maxy - miny:.1f} mm) — positionnement",
            "G21", "G90", "G94",
            "M3 S0",                                   # constant : point visible en mouvement
            f"G0 X{minx:.3f} Y{miny:.3f}",
        ]
        for _ in range(max(1, loops)):
            for x, y in corners:
                lines.append(f"G1 X{x:.3f} Y{y:.3f} F{feed:.0f} S{s}")
        lines += ["M5", "M2"]
        return "\n".join(lines) + "\n"

    def generate_gcode(self, config: gc.GCodeConfig | None = None, items=None) -> str:
        """G-code absolu de la **face active** : miroir (bottom) puis offset plateau.

        Le G-code est en G90 (absolu) depuis l'origine de travail réglée sur la
        machine ; ``project.origin`` positionne le PCB sur le plateau. ``items``
        réutilise un ``active_items`` déjà calculé (évite un recalcul lourd).
        """
        placed = self.placed_items(items)
        title = f"{self.project.name}_{self.active_side}"
        prelude = None
        if self.project.mark_zone_enabled and self.board is not None:
            # Pourtour à puissance de GRAVURE (celle du calque actif) : marque sur le
            # support l'emprise de la CARTE (pas des parcours) — c'est là que la
            # plaque se pose — puis pause M0 (poser la plaque, reprendre).
            minx, miny, maxx, maxy = self.board.bounds   # miroir : emprise inchangée
            ox, oy = self.project.origin
            minx, maxx, miny, maxy = minx + ox, maxx + ox, miny + oy, maxy + oy
            rect = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
            lay = next((l for l in self.active_layers() if l.config.enabled), None)
            speed = lay.config.speed if lay else 1500.0
            power = lay.config.power if lay else 30.0
            prelude = [(rect, speed, power)]
        return gc.build(placed, config or gc.GCodeConfig(), title=title,
                        prelude_items=prelude)

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


def _tag(items, kind: str) -> list:
    """Ajoute une catégorie ("contour"/"fill"/"hole"/"cut") à des items (coords,f,p)."""
    return [(c, f, p, kind) for (c, f, p) in items]


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
