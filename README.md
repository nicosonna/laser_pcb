# laser_pcb

Alternative maison à LightBurn pour **graver des PCB au laser** (Creality Falcon 2, firmware GRBL).

Import d'un PCB (**Gerber** ou **ODB++**), génération des parcours laser, et pilotage
du laser — avec un socle **100 % vectoriel (shapely)** comme source de vérité unique.

## Fonctionnalités

- **Import multi-format** : Gerber RS-274X + Excellon (via gerbonara) et ODB++ (parseur maison, export natif Fusion) — le tout converti en géométrie shapely commune.
- **Modes de gravure par calque** :
  - *Isolation offset* : contour(s) autour du cuivre.
  - *Isolation axe médian (net-aware)* : une seule passe centrée entre nets différents → footprint maximal, idéal fine-pitch. Ne coupe jamais dans un même net (ponts GND conservés).
  - *Négatif (masque peinture)* : remplissage hachuré des zones à brûler.
- **Paramètres adaptatifs** : vitesse et puissance interpolées par segment selon la clairance locale (serré → lent + doux, large → rapide + pleine puissance).
- **DRC** : courts-circuits inter-nets + canaux plus étroits que le kerf, signalés avant gravure.

## Structure

```
laserpcb/
├── geometry.py   Gerber/Excellon -> shapely (+ LabeledCopper)
├── odb.py        ODB++ -> shapely (net-aware via netlist)
├── netlist.py    mapping feature -> net (ODB++ eda/data)
└── toolpath.py   isolation, négatif, axe médian net-aware, adaptatif, DRC
scripts/          aperçus et prototypes de rendu
examples/         cartes de test (module_relais = Gerber, solar_pi_odb = ODB++)
```

## Installation

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

## Interface graphique

```bash
python -m laserpcb.gui.app
```

Importer un dossier PCB (Gerber ou ODB++) ou un ZIP Gerber, régler chaque calque
(mode, passes, vitesse/puissance ou adaptatif), prévisualiser les parcours, puis
**exporter le G-code** (fichier ou carte SD) ou **enregistrer le projet** `.lpcb`
(source embarquée, rouvrable plus tard).

## Aperçus rapides

```bash
python scripts/preview_odb.py examples/solar_pi_odb output/preview.png
python scripts/production_render.py examples/solar_pi_odb output/production.png
python scripts/proto_adaptive.py examples/solar_pi_odb top output/adaptive.png
```

## Tests

```bash
python tests/run_all.py           # unités + intégration
python tests/run_all.py --cov     # avec couverture
```

Plan de test détaillé : [`documentation/dev/test-plan.md`](documentation/dev/test-plan.md).
Règle : toute fonction ajoutée/modifiée a un test, couverture vérifiée à chaque ajout.

## Feuille de route

- [x] `gcode.py` — G-code GRBL Falcon 2 avec `F`/`S` adaptatif par segment
- [x] Suite de tests (unités + intégration, ~83 % de couverture)
- [x] GUI PySide6 — import, calques, réglages, aperçu, export G-code (fichier/SD)
- [x] Projets `.lpcb` — sauvegarde/réouverture avec source embarquée
- [ ] Envoi série direct GRBL (streaming + FakeGRBL pour tests)
- [ ] Optimisation perf de l'axe médian ; fiabilisation du mapping net pour le DRC
