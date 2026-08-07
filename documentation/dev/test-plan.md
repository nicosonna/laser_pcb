# Plan de test

> Matrice test ↔ code ↔ attendu ↔ statut. À maintenir à jour à chaque modif.
> Exécution : machine de dev (aucun matériel laser requis — tout est vérifié en
> géométrie/G-code, jamais en gravant).

## Lancement (runner unique)

```bash
python tests/run_all.py                   # toutes les suites
python tests/run_all.py --only unit       # une seule suite
python tests/run_all.py -k centerline     # filtre pytest -k
python tests/run_all.py --cov             # avec couverture
python tests/run_all.py -v                # verbeux
```

Dépendances dev : `pytest`, `pytest-cov`. Les cartes d'exemple du repo servent de
fixtures réelles : `examples/module_relais` (Gerber, 80×135 mm) et
`examples/solar_pi_odb` (ODB++ Fusion, 147×97 mm).

## Stratégie

**Règle en vigueur : toute fonction ajoutée ou modifiée doit avoir un test,
rejoué immédiatement, et la couverture est vérifiée à chaque ajout.**

Principe : le socle est 100 % vectoriel, donc tout est testable **sans laser** —
on vérifie la géométrie shapely et le texte G-code produit. Le seul étage non
automatisable est la gravure physique sur le Falcon 2 (voir plus bas).

| Phase | Contenu | Statut |
|---|---|---|
| 1 | Unités par module sur cartes réelles + géométries synthétiques | ✅ 2026-08-07 |
| 2 | Intégration bout-en-bout ODB++ → axe médian → G-code → relecture | ✅ 2026-08-07 |
| 3 | Runner unique `tests/run_all.py` + `.coveragerc` | ✅ 2026-08-07 |
| 4 | Fiabilisation DRC (mapping feature→net) + tests de non-régression | à faire |
| 5 | Tests GUI PySide6 (pytest-qt) après implémentation | à faire |
| — | Vérifs de gravure sur Falcon 2 (procédure manuelle) | à faire |

## Suite unités (`tests/unit/`) — 30 tests ✅ 2026-08-07

| Fichier | Tests | Code couvert | Points clés |
|---|---|---|---|
| `test_geometry.py` | 6 | `geometry` (Gerber→shapely) | contour = 80×135 mm exact, cuivre non vide et valide, 77 trous (dont Ø4 fixation), conversion primitive Circle/Line (aire/largeur), `label_by_component` |
| `test_odb.py` | 5 | `odb` (ODB++→shapely) | symboles `r/s/rect/oct/oval` (microns→mm), flash cercle/rect, contour 147×97 + 122 trous, `iter_features`=1048, `load_labeled_copper` net-aware (GND = plus gros net, `$NONE$` isolé) |
| `test_netlist.py` | 3 | `netlist` (eda/data) | ordre des calques, map top = 1047 entrées (GND dominant), calque inconnu → map vide (repli composantes) |
| `test_toolpath.py` | 10 | `toolpath` | isolation offset (contour élargi), négatif = carte−cuivre, hachures, **Calibration** (bornes + monotonie + clamp), `is_subkerf`, **axe médian net-aware** (coupe entre nets ≠, jamais dans un même net), **DRC** (short réel détecté, nets séparés ignorés, aucun sans netlist) |
| `test_gcode.py` | 6 | `gcode` | entête/pied GRBL (`G21/G90/M4 S0`…`M5/M2`), filtre parcours dégénérés, **F/S adaptatif par segment**, saut des mouvements nuls, `estimate_minutes`, `cut_length` |

## Suite intégration (`tests/integration/`) — 2 tests ✅ 2026-08-07

| Test | Vérifie |
|---|---|
| `test_full_pipeline_odb_to_gcode` | Chaîne complète sur la zone du QFN : ODB++ → `LabeledCopper` net-aware → `centerline_isolation` (>100 segments) → `build` G-code. Le G-code contient plusieurs vitesses F (adaptatif effectif), et la longueur gravée **relue depuis le texte** ≈ longueur calculée (les coupes sont bien émises). |
| `test_drc_runs_on_full_layer` | Le DRC tourne sur le vrai calque top et repère le contact **N$35/SW1** (corroboré visuellement sur la capture Fusion). |

## Couverture (mesure du 2026-08-07, `.coveragerc` appliqué)

| Module | Couverture | Reste principalement |
|---|---|---|
| `gcode.py` | **98 %** | branches d'options rares |
| `netlist.py` | **91 %** | fichiers eda absents |
| `geometry.py` | **86 %** | replis Excellon/robustesse (ArcPoly dégénéré, GeometryCollection) |
| `toolpath.py` | **86 %** | hachures obliques, `LayerJob` |
| `odb.py` | **83 %** | `_read_features` hérité, branches de symbole exotiques |
| **TOTAL** | **87 %** | |

## Non testé automatiquement (à couvrir)

- **Gravure réelle Falcon 2** (procédure manuelle) : envoyer un `.gcode` d'isolation
  simple sur une plaque de test, vérifier kerf réel vs kerf configuré, et calibrer
  les points d'ancrage vitesse/puissance de `Calibration`.
- **Fiabilité du DRC** : les gros faux positifs à la frontière plan GND/piste sur
  solar_pi indiquent un mapping feature→net à valider (phase 4). Ajouter des
  fixtures de court-circuit connu vrai/faux.
- **Performance de l'axe médian** (~96 s pleine carte) : à optimiser puis mesurer
  (clip groupé + géométrie préparée).
- **GUI PySide6** : à couvrir en pytest-qt une fois implémentée.
