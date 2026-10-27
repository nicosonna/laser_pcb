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

Le plus simple : **double-cliquer sur `laser_pcb.bat`** à la racine du projet
(utilise le `.venv`, ouvre la GUI sans console). Ou en ligne de commande :

```bash
python -m laserpcb.gui.app
```

Importer un dossier PCB (Gerber ou ODB++) ou une **archive ZIP / TGZ / TAR**, régler
chaque calque (mode, passes, vitesse/puissance ou adaptatif), **zoomer** (molette /
barre d'outils), prévisualiser les parcours, puis **exporter le G-code** (fichier ou
carte SD) ou **enregistrer le projet** `.lpcb` (source embarquée, rouvrable plus tard).

Le **mode négatif adaptatif** évide entièrement les grandes zones sans cuivre
(rapide au centre, lent près des pistes) au lieu d'un simple détourage.

## Caméra de tête

Une **ESP32-CAM** montée sur la tête remplace le gabarit : on voit le plateau à
travers la tête, et un réticule logiciel marque le point visé. Onglet *Caméra*
dans la fenêtre. Firmware, câblage de la croix laser et flashage :
[`firmware/lasercam/README.md`](firmware/lasercam/README.md).

Elle se configure **sans reflashage** : au premier démarrage (ou si le réseau
mémorisé ne répond pas) elle ouvre son propre réseau `lasercam-setup` avec un
**portail captif** — on rejoint ce WiFi, la page de configuration s'ouvre
seule, on choisit le réseau. Dans ce mode « sans réseau » la caméra reste
entièrement utilisable sur `192.168.4.1` : on peut viser une pièce sans box ni
routeur, la graveuse étant alors pilotée en USB.

Vérification du montage sans ouvrir la GUI :

```bash
python scripts/camera_test.py            # détection mDNS, /status, image, flux
```

Le flux est **coupé automatiquement pendant une gravure** : le G-code part en
telnet sur le même WiFi, et la gigue d'un MJPEG peut affamer le tampon de la
machine. Pour garder la vidéo pendant une gravure, passer le job par la carte SD.

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
- [x] Envoi série direct GRBL — streaming + jog/home/pause + FakeGRBL pour tests
- [x] Optimisation perf de l'axe médian (clip groupé : ~96 s → ~5 s, ~18×)
- [x] DRC conscient des composants (footprint vs vrai court inter-composants)
- [x] GUI complète : gravure **par face** + miroir, **trous** (% du Ø), **plateau +
      origine absolue** (pointeur laser, définir 0,0), **aperçu brûlure**, import TGZ, zoom
- [x] Défauts calés **Creality Falcon 2 22 W** (plateau 400×415, kerf 0,1 mm)
- [x] Optimisation trajets à vide (ordre serpentin : −85 % sur gros jobs)
- [x] **Caméra de tête** (ESP32-CAM) : flux réseau, réticule logiciel, croix
      laser, flux coupé pendant la gravure — voir `firmware/lasercam/`
- [x] Caméra : configuration par **portail captif** (mode point d'accès), réglage
      en NVS — reconfigurable sans reflashage
- [ ] Caméra : clic-pour-aller (mapping pixel↔mm) puis alignement 2 points
      (translation + rotation du job, en remplacement du gabarit)
- [ ] Vérifications de gravure réelle sur le Falcon 2 (calibration kerf/puissance)
