"""Session : pont projet <-> géométrie <-> parcours <-> G-code (sans Qt).

Centralise l'état chargé et toute la logique métier, pour que la couche Qt reste
mince et que cette partie soit testable sans interface.
"""

from __future__ import annotations

import tarfile
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..geometry import Hole, LabeledCopper
from .. import geometry as geo
from .. import odb
from .. import drawing as dr
from .. import gcode as gc
from .. import toolpath as tp
from ..project import (LayerConfig, Project, MODE_HYBRID, MODE_CENTERLINE,
                       MODE_COMBINED, MODE_ISOLATION, MODE_NEGATIVE)


@dataclass
class LoadedLayer:
    config: LayerConfig
    labeled: LabeledCopper


# Noms de fichiers cuivre/contour selon l'outil d'export (motifs ORDONNÉS, du plus
# précis au plus générique — pas de motif flou qui attraperait silkscreen/mask) :
# Fusion 360 : copper_top.gbr / profile.gbr ; KiCad : xxx-F_Cu.gbr / xxx-Edge_Cuts.gbr ;
# Protel/Altium : .gtl / .gbl / .gko / .gm1.
_COPPER_PATTERNS = {
    "top": ["*opper_top*.gbr", "*F_Cu*.gbr", "*-F?Cu*.gbr", "*.gtl"],
    "bottom": ["*opper_bottom*.gbr", "*B_Cu*.gbr", "*-B?Cu*.gbr", "*.gbl"],
}
# Serigraphie : encombrement des composants et leurs noms. Fusion :
# silkscreen_top.gbr ; KiCad : xxx-F_Silkscreen.gbr ; Protel : .gto / .gbo
_SILK_PATTERNS = {
    "top": ["*ilkscreen_top*.gbr", "*F_Silkscreen*.gbr", "*F?Silkscreen*.gbr",
            "*.gto"],
    "bottom": ["*ilkscreen_bottom*.gbr", "*B_Silkscreen*.gbr",
               "*B?Silkscreen*.gbr", "*.gbo"],
}
_PROFILE_PATTERNS = ["profile.gbr", "*rofile*.gbr", "*Edge_Cuts*.gbr", "*Edge?Cuts*.gbr",
                     "*outline*.gbr", "*.gko", "*.gm1"]


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
    _silk_cache: dict = field(default_factory=dict)    # cache serigraphie
    _items_cache: tuple | None = None   # (cle de reglages, parcours) : evite de tout
                                        # recalculer entre apercu, brulure et export
    # Un SEUL calcul lourd a la fois : l'apercu, l'apercu brulure, l'export et les
    # boutons d'etape tournent chacun dans leur thread et partagent cette session.
    # Deux calculs concurrents se marchaient dessus (caches mutes en parallele,
    # geometries partagees) -> plantage natif, sans trace Python.
    _compute_lock: object = field(default_factory=threading.RLock, repr=False)
    active_plate: int = 0      # plateau affiche et grave
    _origin_shift: tuple = (0.0, 0.0)  # translation appliquée à l'import (KiCad -> (0,0))

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
        name = path.name
        if path.is_file():
            name = path.stem.removesuffix(".tar")   # nom du zip, pas du dossier temp
            path = _extract_archive(path)
        kind = self.detect_kind(path)
        self._drc_cache = None
        self.source_root = path
        self.project.source_kind = kind
        self.project.name = name if path.name.startswith("lpcb_") else path.name
        if kind == "gerber":
            self._load_gerber(path)
        else:
            self._load_odb(path)
        self._normalize_origin()
        self.center_on_bed()        # une piece importee est centree sur le plateau
        self.project.embed_directory(path)

    def _load_gerber(self, root: Path) -> None:
        """Charge un jeu de Gerber : **Fusion** (copper_top.gbr, profile.gbr) ou
        **KiCad** (xxx-F_Cu.gbr, xxx-Edge_Cuts.gbr, xxx-PTH.drl) ou Protel (.gtl)."""
        gdir = root if list(root.glob("*.gbr")) else root / "GerberFiles"
        self.holes = []
        for xln in list(root.rglob("*.xln")) + list(root.rglob("*.drl")):
            self.holes.extend(geo.load_drill(xln))
        coppers = {}
        for role in ("top", "bottom"):
            f = _first(gdir, _COPPER_PATTERNS[role])
            if f:
                coppers[role] = geo.load_copper(f, role)
        # Contour : profile Fusion / Edge_Cuts KiCad ; sinon repli = emprise du cuivre
        profile = _first(gdir, _PROFILE_PATTERNS)
        board = None
        if profile is not None:
            try:
                board = geo.load_outline(profile)
            except Exception:
                board = None
        if (board is None or board.is_empty) and coppers:
            from shapely import box as _box
            from shapely.ops import unary_union as _uu
            minx, miny, maxx, maxy = _uu([c.geometry for c in coppers.values()]).bounds
            board = _box(minx - 1, miny - 1, maxx + 1, maxy + 1)
        self.board = board
        self.layers = [
            LoadedLayer(LayerConfig(name=role, role=role),
                        geo.label_by_component(role, cl.geometry, board))
            for role, cl in coppers.items()]

    def _normalize_origin(self) -> None:
        """Ramène le coin bas-gauche de la CARTE à (0,0).

        KiCad exporte dans le repère de sa feuille (souvent des Y négatifs et un
        gros offset X) ; nous, on grave en absolu depuis le coin de la plaque.
        Sans ça, le G-code partirait hors zone de travail."""
        if self.board is None or self.board.is_empty:
            return
        minx, miny, _mx, _my = self.board.bounds
        if abs(minx) < 1e-9 and abs(miny) < 1e-9:
            return
        from shapely.affinity import translate
        dx, dy = -minx, -miny
        self._origin_shift = (dx, dy)
        self.board = translate(self.board, dx, dy)
        for lay in self.layers:
            lb = lay.labeled
            lay.labeled = LabeledCopper(
                lb.layer,
                {n: translate(g, dx, dy) for n, g in lb.net_geoms.items()},
                translate(lb.board, dx, dy), lb.has_nets)
        self.holes = [Hole(h.x + dx, h.y + dy, h.diameter) for h in self.holes]
        self._expand_cache.clear()
        self._pads_cache.clear()            # les pads bruts seraient décalés
        self._silk_cache.clear()

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

    @staticmethod
    def _guard_rings(rings, layer: LoadedLayer, kerf: float):
        """INVARIANT DE SÉCURITÉ : un anneau ne mord jamais le cuivre D'ORIGINE.

        Dans les géométries limites (micro-dégagements intra-net à la frontière des
        seuils), la fermeture morphologique peut produire des fragments d'anneau qui
        traversent le cuivre. Tout tronçon fautif est **replié** sur un anneau collé
        au cuivre d'origine (à exactement kerf/2 : ne mord jamais) — plutôt que
        supprimé, ce qui laissait un filament de peinture non brûlé (« trou »)."""
        orig = layer.labeled.union
        guard = orig.buffer(kerf / 2 * 0.98)
        kept = rings.difference(guard)
        removed = rings.intersection(guard)
        out = tp._explode_lines(kept)
        if not removed.is_empty:
            fallback = orig.buffer(kerf / 2, join_style="mitre", mitre_limit=5.0).boundary
            out += tp._explode_lines(fallback.intersection(removed.buffer(kerf)))
        if not out:
            return out
        # On RECOLLE les fragments contigus. Le decoupage ci-dessus eclate un anneau
        # en milliers de morceaux (surtout avec une marge cuivre), et Douglas-Peucker
        # doit conserver les EXTREMITES de chaque morceau : d'ou des dizaines de
        # milliers de micro-segments que la simplification ne peut pas enlever.
        from shapely.ops import linemerge
        merged = linemerge(out)
        return tp._explode_lines(merged)

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
                rings = self._guard_rings(rings, layer, kerf)
                # anneau de contour = LE TOUR DU FOOTPRINT (bord net, hugs cuivre),
                # N passes ; puis évidement du fond (H+V si trame croisée).
                items = _tag(gc.items_from_paths(rings, cfg.speed, cfg.power),
                             "contour") * passes
                fills = []
                if not wide.is_empty:
                    edge, interior = tp.clearing_zones(wide, union, kerf * 4)
                    angles = (0.0, 90.0) if cfg.crosshatch else (0.0,)
                    for i, ang in enumerate(angles):
                        fills.append((ex.submit(tp.hatch, edge, spacing=kerf,
                                                angle=ang, outline=(i == 0)),
                                      cal.v_slow))
                        fills.append((ex.submit(tp.hatch, interior, spacing=kerf,
                                                angle=ang, outline=(i == 0)),
                                      cal.v_fast))
                tight = f_tight.result()
                if tight:
                    # Les passes centrees sont des aretes de Voronoi : des milliers
                    # de troncons de 2 points. On CHAINE les troncons contigus en
                    # polylignes -- memes traits graves, mais Douglas-Peucker peut
                    # enfin travailler dessus (un trace de 2 points est intouchable)
                    # et la machine arrete de hesiter sur des micro-segments.
                    from shapely.ops import linemerge
                    paths = tp._explode_lines(
                        linemerge([tp.LineString(s.coords) for s in tight]))
                    items += _tag(gc.items_from_paths(paths, cfg.speed, cfg.power),
                                  "tight") * passes
                for fut, speed in fills:
                    items += _tag(gc.items_from_paths(fut.result(), speed, cal.p_full),
                                  "fill")
                # Dégagements RÉSIDUELS : portions du vide D'ORIGINE que ni le
                # remplissage (hors liseré), ni les anneaux, ni les passes centrées
                # ne couvrent (canaux même-net préservés, artefacts de fermeture…).
                # On les nettoie partout où le faisceau passe SANS toucher le cuivre
                # -> le résultat reste fidèle à l'artwork du PCB. (Fentes < kerf :
                # physiquement impossibles à nettoyer, lamelles même-net inoffensives.)
                orig_gap = self.board.difference(layer.labeled.union)
                lisere = union.buffer(kerf, join_style="mitre", mitre_limit=5.0)
                residual = orig_gap.intersection(lisere)
                # seuls anneaux + passes centrées peuvent couvrir le liseré (le
                # remplissage est PAR CONSTRUCTION hors liseré) -> buffer léger
                cover_lines = list(rings) + [tp.LineString(s.coords) for s in tight]
                if cover_lines:
                    residual = residual.difference(
                        tp.MultiLineString(cover_lines).buffer(kerf / 2))
                safe = residual.buffer(-kerf / 2 * 1.02)
                if not safe.is_empty:
                    clean = list(tp.hatch(safe, spacing=kerf * 0.8).geoms)
                    # + contour de chaque zone sûre : couvre les bandes trop courtes
                    # pour recevoir une ligne de balayage
                    for poly in getattr(safe, "geoms", [safe]):
                        if poly.geom_type != "Polygon" or poly.is_empty:
                            continue
                        clean.append(tp.LineString(poly.exterior.coords))
                        clean += [tp.LineString(r.coords) for r in poly.interiors]
                    # On ecarte ici les bavures plus courtes que le faisceau : le
                    # spot les couvre deja, et leurs extremites fabriquent des
                    # segments de quelques microns. Filtre LOCAL au nettoyage :
                    # surtout pas sur les passes centrees (courtes mais vitales).
                    clean = [g for g in clean if g.length >= kerf / 2]
                    items += _tag(gc.items_from_paths(clean, cfg.speed, cfg.power),
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
            rings = self._guard_rings(rings, layer, kerf)
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
                    for i, ang in enumerate(angles):
                        fills.append((ex.submit(tp.hatch, edge, spacing=spacing,
                                                angle=ang, outline=(i == 0)),
                                      cal.v_slow))
                        fills.append((ex.submit(tp.hatch, interior, spacing=spacing,
                                                angle=ang, outline=(i == 0)),
                                      cal.v_fast))
                    items = []
                    for fut, speed in fills:
                        items += _tag(gc.items_from_paths(fut.result(), speed, cal.p_full),
                                      "fill")
                return items
            items = []
            for i, ang in enumerate(angles):
                items += _tag(gc.items_from_paths(
                    tp.hatch(region, spacing=spacing, angle=ang, outline=(i == 0)),
                    cfg.speed, cfg.power), "fill")
            return items
        return []

    def drc_summary(self) -> dict:
        """Courts-circuits inter-nets par calque, d'apres le DRC de toolpath.

        PIEGE (session de test 13.10.2026) : le DRC existait mais n'etait
        appele nulle part dans l'interface — un court-circuit dans le Gerber
        partait en gravure sans un mot. Calcule une fois par import (cache),
        a appeler hors du fil graphique (quelques dixiemes de seconde).
        """
        cache = getattr(self, "_drc_cache", None)
        if cache is None:
            cache = {}
            for layer in self.layers:
                try:
                    r = tp.drc(layer.labeled)
                    cache[layer.config.name] = len(r.real_shorts)
                except Exception:  # noqa: BLE001
                    cache[layer.config.name] = -1       # inconnu
            self._drc_cache = cache
        return cache

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

    def _items_key(self) -> tuple:
        """Signature des reglages qui changent les parcours (le cache en depend).

        La position plateau et le miroir n en font PAS partie : ils s appliquent
        apres coup dans ``placed_items``, sans recalcul."""
        p = self.project
        return (self.active_side, round(p.kerf, 6), round(p.copper_margin, 6),
                p.hole_enabled, p.hole_scale_percent, p.hole_min_diameter,
                p.hole_speed, p.hole_power,
                p.hole_passes, p.cut_outline_enabled, p.cut_outline_speed,
                p.cut_outline_power, p.cut_outline_passes, p.cut_outline_margin,
                tuple(sorted(p.calibration.items())),
                # Le DESSIN fait partie des parcours : sans sa signature ici, une
                # forme modifiee resterait invisible, servie par le cache.
                p.drawing_enabled, self.active_plate,
                tuple((s.kind, s.layer, s.plate, round(s.rotation, 6),
                       repr(sorted(s.params.items()))) for s in p.shapes),
                tuple((c.id, c.enabled, c.mode, c.passes, c.speed, c.power)
                      for c in p.work_layers),
                tuple((l.config.name, l.config.enabled, l.config.mode, l.config.passes,
                       l.config.speed, l.config.power, l.config.adaptive,
                       l.config.rings, l.config.crosshatch) for l in self.layers))

    def invalidate_items(self) -> None:
        """Oublie les parcours calcules (a appeler quand la geometrie change)."""
        self._items_cache = None

    def active_items_tagged(self) -> list:
        """Items de la face active, avec cache : l apercu, l apercu brulure et
        l export se partagent le meme calcul tant que les reglages ne bougent pas."""
        key = self._items_key()
        with self._compute_lock:
            if self._items_cache is not None and self._items_cache[0] == key:
                return self._items_cache[1]
            items = self._compute_active_items_tagged()
            self._items_cache = (key, items)
            return items

    def _compute_active_items_tagged(self) -> list:
        """Items de la face active **avec catégorie** (contour / fill / hole / cut),
        NON miroités. Sert au G-code (on ignore la catégorie) ET à l'aperçu couleur.

        Les passes sont gérées **dans** ``_layer_items_tagged`` (le contour est répété
        N fois, mais pas l'évidement raster qui ne se fait qu'en 1 passe)."""
        items = []
        for layer in self.active_layers():
            if layer.config.enabled:
                items.extend(self._layer_items_tagged(layer))
        p = self.project
        if p.hole_enabled:
            items += self.hole_items()
        if p.cut_outline_enabled:
            items += self.cut_items()
        if p.drawing_enabled:
            items += self.drawing_items_tagged()
        return items

    def hole_items(self) -> list:
        """Perçages, INDÉPENDAMMENT de la case « Trous » : le bouton dédié de
        l'onglet d'envoi doit pouvoir les lancer à tout moment. Aucun calcul de
        parcours : c'est instantané même sur une carte lourde."""
        p = self.project
        if not self.holes:
            return []
        rings = tp.hole_circles(self.holes, p.hole_scale_percent,
                                skip_below=p.hole_min_diameter)
        if rings.is_empty:
            return []
        one = _tag(gc.items_from_paths(rings, p.hole_speed, p.hole_power), "hole")
        return one * max(1, p.hole_passes)

    def cut_items(self) -> list:
        """Découpe du contour (FR4), INDÉPENDAMMENT de la case « Découpe » :
        pleine puissance, N passes, ``cut_outline_margin`` mm de FR4 conservés
        autour du circuit. Instantané également."""
        p = self.project
        if self.board is None or self.board.is_empty:
            return []
        from shapely.geometry import LineString
        b = self.board
        if p.cut_outline_margin:
            b = b.buffer(p.cut_outline_margin, join_style="mitre", mitre_limit=5.0)
        lines = []
        for poly in getattr(b, "geoms", [b]):
            lines.append(LineString(poly.exterior.coords))
            lines += [LineString(r.coords) for r in poly.interiors]
        one = _tag(gc.items_from_paths(lines, p.cut_outline_speed, p.cut_outline_power),
                   "cut")
        return one * max(1, p.cut_outline_passes)

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
                    f = _first(gdir, _COPPER_PATTERNS[role])
                    if f:
                        geom = geo.load_pads(f)
                else:
                    geom = odb.load_pads(root, role)
        except Exception:
            pass                                   # pas de pads -> finition trous/texte
        dx, dy = self._origin_shift
        if (dx or dy) and not geom.is_empty:       # même repère que la carte normalisée
            from shapely.affinity import translate
            geom = translate(geom, dx, dy)
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
                from shapely.affinity import scale as _scale
                from shapely.geometry import MultiPolygon as _MP
                k = 1.0 - max(0.0, p.finish_pads_shrink) / 100.0
                tool = []
                for g in getattr(pads, "geoms", [pads]):
                    if g.geom_type != "Polygon" or g.is_empty:
                        continue
                    # 1. retrait en POURCENTAGE : la peinture conservée sur le
                    #    pourtour du pad fait vernis épargne, comme un vrai circuit.
                    target = _scale(g, k, k, origin="centroid") if k < 1 else g
                    # 2. compensation du FAISCEAU : le trait brûle kerf/2 de chaque
                    #    côté de son centre. Sans cette érosion, le parcours suivait
                    #    le bord de la zone visée et débordait de 50 um — soit bien
                    #    plus que les 12 um de vernis d'un petit pad, qui était donc
                    #    purement et simplement annulé.
                    eroded = target.buffer(-kerf / 2.0)
                    for part in getattr(eroded, "geoms", [eroded]):
                        if part.geom_type == "Polygon" and not part.is_empty:
                            tool.append(part)
                    if eroded.is_empty:
                        # pad plus petit que le faisceau : on le brûle d'un point,
                        # plutôt que de le sauter (il faut bien pouvoir souder)
                        tool.append(target)
                if tool:
                    fill_geoms.append((_MP(tool), True))
        if p.finish_text.strip():
            txt = tp.text_polygons(p.finish_text, p.finish_text_height,
                                   p.finish_text_x, p.finish_text_y)
            if not txt.is_empty:
                fill_geoms.append((txt, False))     # traits fins : pas de contour
        for geom, outline in fill_geoms:
            if outline:
                rings = [tp.LineString(r.coords)
                         for poly in getattr(geom, "geoms", [geom])
                         if poly.geom_type == "Polygon"
                         for r in [poly.exterior, *poly.interiors]]
                items += _tag(gc.items_from_paths(rings, p.finish_speed,
                                                  p.finish_power), "contour")
            spacing = kerf if outline else kerf * 0.8
            items += _tag(gc.items_from_paths(tp.hatch(geom, spacing=spacing),
                                              p.finish_speed, p.finish_power), "fill")
        items *= max(1, p.finish_passes)
        # Trous : PERÇAGE laser (N passes pleines, comme la découpe) — après la peinture
        if p.finish_holes and self.holes:
            circles = tp.hole_circles(self.holes, p.hole_scale_percent,
                                      skip_below=p.hole_min_diameter)
            hole_items = _tag(gc.items_from_paths(circles, p.hole_speed, p.hole_power),
                              "hole")
            items += hole_items * max(1, p.finish_hole_passes)
        # Découpe du contour EN FIN de finition (mêmes réglages que le groupe Découpe)
        if p.finish_cut_enabled and self.board is not None and not self.board.is_empty:
            from shapely.geometry import LineString
            b = self.board
            if p.cut_outline_margin:
                b = b.buffer(p.cut_outline_margin, join_style="mitre", mitre_limit=5.0)
            lines = []
            for poly in getattr(b, "geoms", [b]):
                lines.append(LineString(poly.exterior.coords))
                lines += [LineString(r.coords) for r in poly.interiors]
            cut = _tag(gc.items_from_paths(lines, p.cut_outline_speed,
                                           p.cut_outline_power), "cut")
            items += cut * max(1, p.cut_outline_passes)
        return items

    def generate_finish_gcode(self, config: gc.GCodeConfig | None = None,
                              stats: dict | None = None,
                              include_cut: bool = True) -> str:
        """G-code de finition (face active).

        Découpe du pourtour ACTIVE : après le bain, la plaque est déjà découpée —
        la seule référence physique restante est son COIN. Le G-code est alors
        exprimé depuis le coin de la plaque (miroir conservé, +marge, sans offset
        plateau) : définir 0,0 sur le coin bas-gauche de la plaque et lancer.
        Sinon : mêmes coordonnées que la gravure (même zéro machine).
        """
        tagged = [it for it in self.finish_items_tagged()
                  if include_cut or it[3] != "cut"]
        if not tagged:
            return ""
        p = self.project
        title = f"{self.project.name}_{self.active_side}_finition"
        # Repère : par DÉFAUT même zéro machine que la gravure (le centrage plateau
        # est donc suivi — l'ancien basculement automatique était un bug). L'option
        # ``finish_ref_plate_corner`` exprime la finition depuis le coin de la
        # plaque DÉCOUPÉE (+marge) pour réaligner après le bain.
        plate_ref = (p.finish_ref_plate_corner and p.cut_outline_margin > 0
                     and not p.finish_cut_enabled)
        if plate_ref:
            if self.is_mirrored() and self.board is not None:
                cx = self.board_center_x()
                tagged = [([(2 * cx - x, y) for (x, y) in c], f, pw, k)
                          for c, f, pw, k in tagged]
            m = p.cut_outline_margin
            placed = [([(x + m, y + m) for (x, y) in c], f, pw, k)
                      for c, f, pw, k in tagged]
            title += " (0,0 = coin de la plaque decoupee)"
        else:
            placed = self.placed_items(tagged)
        # Phases : peinture (pads/texte) -> trous -> découpe, jamais mélangées
        config = config or gc.GCodeConfig(
            simplify_tolerance=self.project.simplify_tolerance)
        def phase(*kinds, exclude=False):
            return [(c, f, pw) for (c, f, pw, k) in placed
                    if (k in kinds) != exclude]
        ordered = gc.optimize(phase("hole", "cut", exclude=True), config.max_optimize)
        ordered += gc.optimize(phase("hole"), config.max_optimize)
        ordered += phase("cut")
        from dataclasses import replace
        return gc.build(ordered, replace(config, optimize_order=False), title=title,
                        stats=stats)

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
        # (active_items prend deja le verrou de calcul)
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
        un calcul déjà fait (3- OU 4-uplets, la catégorie éventuelle est préservée)."""
        items = self.active_items() if items is None else items
        if self.is_mirrored() and self.board is not None:
            cx = self.board_center_x()
            items = [([(2 * cx - x, y) for (x, y) in coords], *rest)
                     for coords, *rest in items]
        ox, oy = self.project.origin
        if ox or oy:
            items = [([(x + ox, y + oy) for (x, y) in coords], *rest)
                     for coords, *rest in items]
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

    def piece_origin(self) -> tuple[float, float] | None:
        """Coin bas-gauche de la PIÈCE, en coordonnées de travail.

        C'est le point d'où part la gravure une fois la carte positionnée sur le
        plateau — à ne pas confondre avec le 0,0 machine, dont il est décalé par
        ``project.origin`` (centrage). Même emprise que le cadrage.
        """
        b = self.content_bounds()
        if b is None:
            return None
        ox, oy = self.project.origin
        # Emprise du TRAVAIL, sans la marge de decoupe : c est d ici que part
        # la gravure. (Le cadrage, lui, encadre la PLAQUE a positionner.)
        return (b[0] + ox, b[1] + oy)

    def frame_gcode(self, power: float = 1.0, feed: float = 3000.0,
                    loops: int = 2, max_s: int = 1000) -> str:
        """G-code de **cadrage** : trace le rectangle englobant du job à faible
        puissance (visible mais sans graver), pour positionner la plaque.

        Contrairement au bouton « Frame » de la machine — qui, sur un parcours
        vectoriel, rejoue tout le tracé — ceci ne fait QUE les 4 coins.

        On encadre l'emprise de la **plaque** (contour de carte + marge de découpe),
        pas celle des parcours : c'est la plaque qu'on positionne, et surtout cela
        n'exige AUCUN calcul de parcours — le cadrage reste instantané même sur une
        carte lourde, alors que passer par ``job_bounds`` figeait l'interface
        plusieurs minutes.
        """
        b = self.plate_bounds()
        if b is None:
            b = self.job_bounds()
        if b is None:
            return ""
        ox, oy = self.project.origin
        minx, miny, maxx, maxy = (b[0] + ox, b[1] + oy, b[2] + ox, b[3] + oy)
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

    def generate_gcode(self, config: gc.GCodeConfig | None = None, items=None,
                       stats: dict | None = None) -> str:
        """G-code absolu de la **face active** : miroir (bottom) puis offset plateau.

        Le G-code est en G90 (absolu) depuis l'origine de travail réglée sur la
        machine ; ``project.origin`` positionne le PCB sur le plateau. ``items``
        réutilise un calcul déjà fait (tagué de préférence : l'ordre en PHASES —
        gravure, puis TROUS, puis DÉCOUPE du pourtour en dernier — s'appuie sur
        les catégories ; l'optimiseur ne mélange jamais les phases).
        """
        config = config or gc.GCodeConfig(
            simplify_tolerance=self.project.simplify_tolerance)
        placed = self.placed_items(self.active_items_tagged() if items is None
                                   else items)
        if placed and len(placed[0]) > 3:
            # phases : tout le reste -> trous -> découpe (chacune optimisée à part)
            def phase(*kinds, exclude=False):
                return [(c, f, p) for (c, f, p, k) in placed
                        if (k in kinds) != exclude]
            engrave = phase("hole", "cut", "dessin", exclude=True)
            ordered = gc.optimize(engrave, config.max_optimize)
            # Le DESSIN garde son ordre : il porte « percer avant de detourer »,
            # que l'optimiseur de trajets detruirait en le reordonnant.
            ordered += phase("dessin")
            ordered += gc.optimize(phase("hole"), config.max_optimize)
            ordered += phase("cut")            # passes de découpe : ordre conservé
            from dataclasses import replace
            placed, config = ordered, replace(config, optimize_order=False)
        title = f"{self.project.name}_{self.active_side}"
        prelude = None
        if self.project.mark_zone_enabled and self.board is not None:
            # Pourtour à puissance de GRAVURE (celle du calque actif) : marque sur le
            # support l'emprise de la PLAQUE (carte + marge de découpe si active) —
            # c'est là que la plaque se pose — puis pause M0 (poser, reprendre).
            minx, miny, maxx, maxy = self.plate_bounds()  # miroir : emprise inchangée
            ox, oy = self.project.origin
            minx, maxx, miny, maxy = minx + ox, maxx + ox, miny + oy, maxy + oy
            rect = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
            lay = next((l for l in self.active_layers() if l.config.enabled), None)
            speed = lay.config.speed if lay else 1500.0
            power = lay.config.power if lay else 30.0
            prelude = [(rect, speed, power)]
        return gc.build(placed, config, title=title, prelude_items=prelude,
                        stats=stats)

    def work_mode(self) -> str:
        """Nature du travail : ``"pcb"`` si une carte est chargee, sinon ``"dessin"``.

        Sert a replier l'interface : tous les reglages de gravure chimique
        (marge cuivre, trous, decoupe FR4, serigraphie, finition apres
        perchlorure) n'ont aucun sens sur une plaque ou un boitier.
        """
        return "pcb" if self.layers else "dessin"

    def content_bounds(self) -> tuple | None:
        """Emprise du TRAVAIL : la carte si elle existe, sinon le dessin.

        C'est elle qui permet de travailler sans PCB du tout — plaque, boîtier,
        pochoir — là où tout le logiciel supposait une carte importée.
        """
        if self.board is not None and not self.board.is_empty:
            return self.board.bounds
        g = self.drawing_geometry()
        return None if g is None or g.is_empty else g.bounds

    def plate_bounds(self) -> tuple[float, float, float, float] | None:
        """Emprise de la PLAQUE nécessaire : le travail, élargi de la marge de
        découpe quand la découpe du pourtour est activée."""
        b = self.content_bounds()
        if b is None:
            return None
        minx, miny, maxx, maxy = b
        # Le bouton « Decouper » coupe TOUJOURS avec la marge : c'est elle la
        # plaque reelle, que la case « Decoupe » du job soit cochee ou non.
        m = max(0.0, self.project.cut_outline_margin)
        return (minx - m, miny - m, maxx + m, maxy + m)

    def fits_on_bed(self) -> bool:
        """La plaque (carte + marge de découpe) tient-elle sur le plateau ?"""
        b = self.plate_bounds()
        if b is None:
            return True
        return (b[2] - b[0] <= self.project.bed_width
                and b[3] - b[1] <= self.project.bed_height)

    # Étapes lançables séparément depuis l'onglet d'envoi. Les réglages viennent
    # de l'onglet Calques ; les CASES À COCHER n'ont volontairement aucun effet ici
    # (elles disent ce qui part dans le job complet, pas ce qu'on peut lancer).
    PHASES = {
        "gravure": "masque (contours, remplissage, passes centrées)",
        "trous": "perçages",
        "decoupe": "découpe du contour (FR4)",
        "finition": "empreintes de composants (pads, après perchlorure)",
        "serigraphie": "encombrement et noms des composants (marquage léger)",
        "dessin": "formes, lignes et textes dessinés",
    }

    def silk_geometry(self, role: str):
        """Encombrement des composants + leurs noms, pour la face ``role``.

        Gerber : on prend la couche SÉRIGRAPHIE du projet — c'est exactement ce
        qu'on veut (contours de boîtiers et références), déjà dessiné par l'EDA.
        ODB++ : pas de couche équivalente, on la reconstruit depuis les composants
        (rectangle d'encombrement + référence vectorisée).
        """
        from shapely.geometry import MultiPolygon
        if role in self._silk_cache:
            return self._silk_cache[role]
        geom = MultiPolygon()
        root = self.source_root
        try:
            if root is not None and self.project.source_kind == "gerber":
                gdir = root if list(root.glob("*.gbr")) else root / "GerberFiles"
                f = _first(gdir, _SILK_PATTERNS[role])
                if f:
                    geom = geo.load_copper(f, role).geometry
            elif root is not None:
                geom = self._silk_from_components(role)
        except Exception:
            pass                         # pas de sérigraphie : le bouton sera sans effet
        dx, dy = self._origin_shift
        if (dx or dy) and not geom.is_empty:
            from shapely.affinity import translate
            geom = translate(geom, dx, dy)
        self._silk_cache[role] = geom
        return geom

    def _silk_from_components(self, role: str):
        """ODB++ : rectangle d'encombrement de chaque composant + sa référence."""
        from shapely.affinity import rotate as _rot, translate as _tr
        from shapely.geometry import box as _box
        from shapely.ops import unary_union
        from .. import bom
        parts = []
        for c in bom.load_components(self.source_root):
            if c.side != role:
                continue
            w = h = 0.0
            if c.size:                             # ex. "4.4 × 1.8 mm"
                try:
                    a, b = c.size.replace(" mm", "").split("\u00d7")
                    w, h = float(a), float(b)
                except ValueError:
                    pass
            if w > 0 and h > 0:
                ring = _box(-w / 2, -h / 2, w / 2, h / 2).exterior.buffer(0.08)
                parts.append(_tr(_rot(ring, c.rot, origin=(0, 0)), c.x, c.y))
            txt = tp.text_polygons(c.refdes, self.project.silk_text_height)
            if not txt.is_empty:
                minx, miny, maxx, maxy = txt.bounds
                parts.append(_tr(txt, c.x - (maxx - minx) / 2,
                                 c.y - (maxy - miny) / 2))
        return unary_union(parts) if parts else _box(0, 0, 0, 0)

    def silk_items(self) -> list:
        """Marquage : contour + remplissage de la sérigraphie, puissance faible."""
        p = self.project
        geom = self.silk_geometry(self.active_side)
        if geom is None or geom.is_empty:
            return []
        # PAS de tracé de contour ici, contrairement aux pads : les traits de
        # sérigraphie font ~142 um pour un faisceau de 100 um. Suivre leurs DEUX
        # bords pose deux brûlures qui débordent de 50 um de chaque côté — les
        # contre-formes des lettres (R, 8, 0) se referment et le texte devient
        # illisible. Un simple remplissage suffit à couvrir un trait aussi fin.
        items = _tag(gc.items_from_paths(tp.hatch(geom, spacing=p.kerf * 0.8),
                                         p.silk_speed, p.silk_power), "fill")
        return items * max(1, p.silk_passes)

    # ---- dessin vectoriel ---------------------------------------------------- #
    def work_layers(self) -> list:
        """Calques de travail du dessin, créés à la première utilisation."""
        if not self.project.work_layers:
            self.project.work_layers = dr.default_layers()
        return self.project.work_layers

    def layer_by_id(self, layer_id: str):
        return next((c for c in self.work_layers() if c.id == layer_id), None)

    def plate_count(self) -> int:
        """Nombre de plateaux du dessin (toujours au moins un).

        Le plateau ACTIF compte même s'il est vide. Sans cela, « + Plateau »
        ne produisait rien de visible : un plateau n'existait que par les
        pièces qu'il portait, donc un plateau neuf n'existait pas.
        """
        from .. import arrange as ar
        return max(ar.plate_count(self.project.shapes),
                   int(getattr(self, "active_plate", 0)) + 1)

    def plate_shapes(self, plate: int | None = None) -> list:
        """Formes d'UN plateau. Un plateau = un job : la gravure, l'aperçu et
        le G-code ne voient jamais que celui qui est actif."""
        from .. import arrange as ar
        return ar.shapes_on(self.project.shapes,
                            self.active_plate if plate is None else plate)

    def drawing_items_tagged(self) -> list:
        """Parcours des objets dessinés, catégorie « dessin ».

        INDÉPENDANT de la case du job complet : le bouton d'étape doit pouvoir
        les lancer seuls, comme les trous ou la découpe.
        """
        p = self.project
        if not p.shapes:
            return []
        out = []
        # PERCER AVANT DE DETOURER : une piece detouree n'est plus tenue par la
        # plaque. L'ordre est calcule ici et doit survivre jusqu'au G-code.
        from .. import arrange as ar
        for shape in ar.cut_order(self.plate_shapes()):
            layer = self.layer_by_id(shape.layer)
            if layer is None or not layer.enabled:
                continue
            out += _tag(dr.shape_items(shape, layer, p.kerf), "dessin")
        return out

    def drawing_geometry(self):
        """Union des objets dessinés — pour l'aperçu et l'emprise."""
        from shapely.ops import unary_union
        geoms = [s.geometry() for s in self.plate_shapes()]
        geoms = [g for g in geoms if g is not None and not g.is_empty]
        return unary_union(geoms) if geoms else None

    def generate_phase_gcode(self, phase: str,
                             config: gc.GCodeConfig | None = None) -> str:
        """G-code d'UNE étape, placé comme le job complet (miroir + plateau)."""
        if phase == "finition":
            # PIEGE (relecture 13.10.2026) : la finition passait par
            # placed_items et ignorait ``finish_ref_plate_corner`` : avec le
            # repere « coin de la plaque » coche, le bouton envoyait le job
            # ~150 mm a cote de l'export. Un seul chemin pour les deux.
            return self.generate_finish_gcode(config, include_cut=False)
        with self._compute_lock:
            return self._phase_gcode(phase, config)

    def _phase_gcode(self, phase: str, config=None) -> str:
        if phase == "gravure":
            items = [it for it in self.active_items_tagged()
                     if it[3] in ("contour", "tight", "fill")]
        elif phase == "trous":
            items = self.hole_items()
        elif phase == "decoupe":
            items = self.cut_items()
        elif phase == "finition":
            items = [it for it in self.finish_items_tagged() if it[3] != "cut"]
        elif phase == "serigraphie":
            items = self.silk_items()
        elif phase == "dessin":
            items = self.drawing_items_tagged()
        else:
            raise ValueError(f"étape inconnue : {phase}")
        if not items:
            return ""
        config = config or gc.GCodeConfig(
            simplify_tolerance=self.project.simplify_tolerance)
        if phase in ("decoupe", "trous"):
            # tours successifs sur la même figure : on garde l'ordre tel quel
            from dataclasses import replace
            config = replace(config, optimize_order=False)
        placed = [(c, f, pw) for (c, f, pw, *_k) in self.placed_items(items)]
        title = f"{self.project.name}_{self.active_side}_{phase}"
        return gc.build(placed, config, title=title)

    def center_on_bed(self) -> None:
        """Positionne le PCB au centre du plateau (règle ``project.origin``)."""
        b = self.content_bounds()
        if b is None:
            return
        minx, miny, maxx, maxy = b
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
        s._normalize_origin()
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
