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
MODE_HYBRID = "hybrid"          # offset propre + passe centrée dans les gaps serrés
MODE_ISOLATION = "isolation_offset"
MODE_CENTERLINE = "centerline"
MODE_NEGATIVE = "negative"


@dataclass
class LayerConfig:
    """Réglages laser d'un calque cuivre."""
    name: str                      # ex. "top", "bottom", "copper_top"
    role: str = "top"              # "top" | "bottom"
    enabled: bool = True
    # Défaut = hybride : contours offset propres, + passe unique centrée (axe médian)
    # automatiquement dans les gaps serrés (fine-pitch) pour éviter le double trait.
    mode: str = MODE_HYBRID
    passes: int = 1
    speed: float = 900.0           # mm/min (mode constant)
    power: float = 85.0            # % (mode constant)
    adaptive: bool = True          # si True, vitesse/puissance dérivées de la clairance
    rings: int = 1                 # anneaux (mode isolation_offset)


@dataclass
class Project:
    """Un projet complet, sérialisable en .lpcb."""
    name: str = "projet"
    source_kind: str = "gerber"    # "gerber" | "odbpp"
    source_files: dict[str, str] = field(default_factory=dict)  # relpath -> base64
    layers: list[LayerConfig] = field(default_factory=list)
    kerf: float = 0.10                         # spot Falcon 2 22 W (~0,08–0,1 mm)
    copper_margin: float = 0.10                # élargit pistes/pads (laser + perchlorure)
    origin: tuple[float, float] = (0.0, 0.0)   # position du PCB sur le plateau (mm)
    bed_width: float = 400.0                   # plateau Falcon 2 : 400 × 415 mm
    bed_height: float = 415.0
    calibration: dict = field(default_factory=dict)
    # Perçages (gravés en contour, une face à la fois)
    hole_enabled: bool = True
    hole_scale_percent: float = -30.0   # <0 rétrécit (évite de couper une piste)
    hole_speed: float = 600.0
    hole_power: float = 100.0
    hole_passes: int = 1
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
            copper_margin=d.get("copper_margin", 0.10),
            origin=tuple(d.get("origin", (0.0, 0.0))),
            bed_width=d.get("bed_width", 400.0),
            bed_height=d.get("bed_height", 415.0),
            calibration=d.get("calibration", {}),
            hole_enabled=d.get("hole_enabled", True),
            hole_scale_percent=d.get("hole_scale_percent", -30.0),
            hole_speed=d.get("hole_speed", 600.0),
            hole_power=d.get("hole_power", 100.0),
            hole_passes=d.get("hole_passes", 1),
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
