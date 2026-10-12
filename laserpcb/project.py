"""Projets laser_pcb (.lpcb) : sauvegarde/rechargement avec source embarquée.

Un projet contient une **copie** du PCB source (Gerber ou ODB++) encodée dans le
fichier, plus les réglages par calque et la calibration. Il est donc portable :
déplaçable et rouvrable même si l'original a bougé.

Format : JSON avec les fichiers source en base64. Extension conseillée : ``.lpcb``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA = 1

# Modes de gravure par calque
MODE_ISOLATION = "isolation_offset"   # contours qui épousent le cuivre
MODE_NEGATIVE = "negative"            # évidement des zones sans cuivre (masque)
MODE_COMBINED = "combined"            # isolation + évidement : 1 passe entre pistes proches
# (anciens modes retirés du choix utilisateur, gardés pour compat des projets)
MODE_HYBRID = "hybrid"
MODE_CENTERLINE = "centerline"


@dataclass
class LayerConfig:
    """Réglages laser d'un calque cuivre."""
    name: str                      # ex. "top", "bottom", "copper_top"
    role: str = "top"              # "top" | "bottom"
    enabled: bool = True
    mode: str = MODE_COMBINED       # isolation + évidement, 1 passe entre pistes proches
    # PROCÉDÉ = retirer la PEINTURE (masque), pas le cuivre -> puissance FAIBLE,
    # peu de passes. Défauts prudents façon calque "image" LightBurn ; à AFFINER par
    # un test matière (dépend de la peinture/épaisseur/couleur).
    passes: int = 2
    speed: float = 1500.0          # mm/min (retrait peinture sur les traits d'isolation)
    power: float = 30.0            # % (juste brûler la peinture, pas le cuivre/FR4)
    adaptive: bool = True
    rings: int = 1                 # anneaux (mode isolation_offset)
    crosshatch: bool = False       # trame croisée H+V (remplissage) : mouvement franc,
                                   # plus précis mais ~2× plus long — à activer au besoin


@dataclass
class Project:
    """Un projet complet, sérialisable en .lpcb."""
    name: str = "projet"
    source_kind: str = "gerber"    # "gerber" | "odbpp"
    source_files: dict[str, str] = field(default_factory=dict)  # relpath -> base64
    layers: list[LayerConfig] = field(default_factory=list)
    kerf: float = 0.10                         # spot Falcon 2 22 W (~0,08–0,1 mm)
    simplify_tolerance: float = 0.006          # mm : simplification des tracés avant
                                               # écriture (demi-pas moteur à 80 pas/mm).
                                               # Évite des segments que la machine ne
                                               # sait pas exécuter (micro-arrêts).
    copper_margin: float = 0.0                 # marge laser : élargit pistes/pads (compense laser + perchlorure),
                                               # plafonnée par net (jamais de pont) ; 0 = off (défaut)
    origin: tuple[float, float] = (0.0, 0.0)   # position du PCB sur le plateau (mm)
    bed_width: float = 400.0                   # plateau Falcon 2 : 400 × 415 mm
    bed_height: float = 415.0
    calibration: dict = field(default_factory=dict)
    # Perçages (gravés en contour, une face à la fois) — désactivés par défaut
    hole_enabled: bool = False
    hole_scale_percent: float = -30.0   # <0 rétrécit (évite de couper une piste)
    hole_speed: float = 600.0
    hole_power: float = 100.0
    hole_passes: int = 1
    # Découpe du contour de carte (FR4) — optionnelle. Là on coupe le cuivre+FR4 :
    # pleine puissance, basse vitesse, beaucoup de passes.
    cut_outline_enabled: bool = False
    cut_outline_speed: float = 300.0     # mm/min (basse vitesse)
    cut_outline_power: float = 100.0     # %
    cut_outline_passes: int = 35
    cut_outline_margin: float = 1.0      # mm de FR4 gardés autour du circuit
    # Pourtour gravé AU DÉBUT du job (puissance de gravure) puis pause M0 :
    # on marque la zone sur le martyr, on pose le PCB dedans, on reprend.
    mark_zone_enabled: bool = False
    # FINITION (après perchlorure) : retirer la peinture des zones à souder.
    # Export séparé : pads (zones de soudure), trous, texte de marquage.
    finish_pads: bool = True
    finish_holes: bool = True
    finish_text: str = ""
    finish_text_height: float = 3.0     # mm
    finish_text_x: float = 2.0          # position bas-gauche du texte (mm, repère carte)
    finish_text_y: float = 2.0
    finish_speed: float = 1500.0        # mm/min (retrait peinture)
    finish_power: float = 20.0          # % (30 brûlait : trop agressif pour la peinture)
    finish_passes: int = 1
    finish_pads_inset: float = 0.05     # retrait des pads (mm) : reste SUR le cuivre
    finish_hole_passes: int = 30        # trous en perçage laser (comme la découpe)
    finish_cut_enabled: bool = False    # découpe du contour EN FIN de finition
                                        # (réglages du groupe Découpe ; exclusif avec
                                        # la découpe du job principal)
    finish_ref_plate_corner: bool = False  # repère finition = coin de la plaque
                                        # DÉCOUPÉE (+marge, sans offset plateau).
                                        # Off (défaut) : même zéro que la gravure,
                                        # le centrage plateau est donc suivi.
    schema: int = SCHEMA

    # ---- source embarquée --------------------------------------------------- #
    def embed_directory(self, root: str | Path, patterns: list[str] | None = None) -> None:
        """Embarque (copie) le contenu d'un dossier source dans le projet."""
        root = Path(root)
        self.source_files = {}
        files = _iter_files(root, patterns)
        for f in files:
            rel = f.relative_to(root).as_posix()
            self.source_files[rel] = base64.b64encode(f.read_bytes()).decode("ascii")

    def materialize(self, dest: str | Path) -> Path:
        """Écrit la source embarquée sous ``dest`` et renvoie le dossier racine."""
        dest = Path(dest)
        for rel, b64 in self.source_files.items():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(b64))
        return dest

    # ---- (dé)sérialisation -------------------------------------------------- #
    def to_dict(self) -> dict:
        d = asdict(self)
        d["origin"] = list(self.origin)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        layers = [LayerConfig(**lc) for lc in d.get("layers", [])]
        return cls(
            name=d.get("name", "projet"),
            source_kind=d.get("source_kind", "gerber"),
            source_files=d.get("source_files", {}),
            layers=layers,
            kerf=d.get("kerf", 0.10),
            simplify_tolerance=d.get("simplify_tolerance", 0.006),
            copper_margin=d.get("copper_margin", 0.0),
            origin=tuple(d.get("origin", (0.0, 0.0))),
            bed_width=d.get("bed_width", 400.0),
            bed_height=d.get("bed_height", 415.0),
            calibration=d.get("calibration", {}),
            hole_enabled=d.get("hole_enabled", False),
            hole_scale_percent=d.get("hole_scale_percent", -30.0),
            hole_speed=d.get("hole_speed", 600.0),
            hole_power=d.get("hole_power", 100.0),
            hole_passes=d.get("hole_passes", 1),
            cut_outline_enabled=d.get("cut_outline_enabled", False),
            cut_outline_speed=d.get("cut_outline_speed", 300.0),
            cut_outline_power=d.get("cut_outline_power", 100.0),
            cut_outline_passes=d.get("cut_outline_passes", 35),
            cut_outline_margin=d.get("cut_outline_margin", 1.0),
            mark_zone_enabled=d.get("mark_zone_enabled", False),
            finish_pads=d.get("finish_pads", True),
            finish_holes=d.get("finish_holes", True),
            finish_text=d.get("finish_text", ""),
            finish_text_height=d.get("finish_text_height", 3.0),
            finish_text_x=d.get("finish_text_x", 2.0),
            finish_text_y=d.get("finish_text_y", 2.0),
            finish_speed=d.get("finish_speed", 1500.0),
            finish_power=d.get("finish_power", 20.0),
            finish_passes=d.get("finish_passes", 1),
            finish_pads_inset=d.get("finish_pads_inset", 0.05),
            finish_hole_passes=d.get("finish_hole_passes", 30),
            finish_cut_enabled=d.get("finish_cut_enabled", False),
            finish_ref_plate_corner=d.get("finish_ref_plate_corner", False),
            schema=d.get("schema", SCHEMA),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict()), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _iter_files(root: Path, patterns: list[str] | None):
    if patterns:
        out: list[Path] = []
        for pat in patterns:
            out.extend(root.rglob(pat))
        return sorted(set(out))
    return sorted(p for p in root.rglob("*") if p.is_file())
