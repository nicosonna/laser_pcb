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
| 4 | Projet .lpcb (round-trip embarqué) + Session GUI + smoke fenêtre headless | ✅ 2026-08-07 |
| 5 | Sender GRBL série + FakeGRBL (comme FakeMatek), streaming testé sans laser | ✅ 2026-08-07 |
| 6 | DRC conscient des composants (footprint vs vrai court) + perf axe médian | ✅ 2026-08-07 |
| 7 | GUI complète : par-face + miroir, trous (% Ø), plateau/origine absolue, aperçu brûlure, import TGZ, zoom, défauts Falcon 2 22 W | ✅ 2026-08-07 |
| 8 | E2E `scripts/e2e_test.py` (laser mocké, toutes faces×modes, streaming) + optim trajets (serpentin, −85 %) | ✅ 2026-08-07 |
| — | Vérifs de gravure sur Falcon 2 (procédure manuelle) | à faire |

> Un round **end-to-end** manuel/CI complémentaire : `python scripts/e2e_test.py`
> importe un projet, génère le G-code de chaque face × chaque mode, vérifie coords
> finies et dans le plateau, DRC, aperçu brûlure, puis **streame vers un FakeGRBL**
> (origine, pointeur, gravure). Validé sur le vrai `solar_pi.tar`.

## Leçon (2026-08-07) : tester le *produit*, pas seulement la *tuyauterie*

Le mode par défaut (axe médian) produisait un tracé inutilisable sur carte entière,
mais **tous les tests passaient** : ils validaient la mécanique (G-code GRBL valide,
coords finies et dans le plateau, F/S varie) — jamais la **justesse géométrique** du
tracé. Correctif : défaut = isolation offset, et un **invariant qualité**
(`test_isolation_hugs_copper_but_medial_axis_wanders`) : le tracé d'isolation doit
rester à ≈ kerf/2 du cuivre (l'axe médian s'en éloigne → aurait fait échouer le test).
Règle ajoutée : tout algorithme géométrique a au moins un test d'invariant sur la
*forme* de sa sortie, pas seulement sur sa validité formelle.

## Suite unités (`tests/unit/`) — 44 tests ✅ 2026-08-07

| Fichier | Tests | Code couvert | Points clés |
|---|---|---|---|
| `test_geometry.py` | 6 | `geometry` (Gerber→shapely) | contour = 80×135 mm exact, cuivre non vide et valide, 77 trous (dont Ø4 fixation), conversion primitive Circle/Line (aire/largeur), `label_by_component` |
| `test_odb.py` | 5 | `odb` (ODB++→shapely) | symboles `r/s/rect/oct/oval` (microns→mm), flash cercle/rect, contour 147×97 + 122 trous, `iter_features`=1048, `load_labeled_copper` net-aware (GND = plus gros net, `$NONE$` isolé) |
| `test_netlist.py` | 3 | `netlist` (eda/data) | ordre des calques, map top = 1047 entrées (GND dominant), calque inconnu → map vide (repli composantes) |
| `test_toolpath.py` | 11 | `toolpath` | isolation offset (contour élargi), négatif = carte−cuivre, hachures, **Calibration** (bornes + monotonie + clamp), `is_subkerf`, **axe médian net-aware** (coupe entre nets ≠, jamais dans un même net), **DRC** (short réel, nets séparés ignorés, aucun sans netlist, **classification footprint/court par composant**) |
| `test_gcode.py` | 6 | `gcode` | entête/pied GRBL (`G21/G90/M4 S0`…`M5/M2`), filtre parcours dégénérés, **F/S adaptatif par segment**, saut des mouvements nuls, `estimate_minutes`, `cut_length` |
| `test_project.py` | 2 | `project` (.lpcb) | round-trip nom/kind/kerf/calques, **source embarquée** recréée à l'octet près (portabilité) |
| `test_sender.py` | 6 | `sender` (GRBL) | contre **FakeGRBL** : nettoyage commentaires, streaming complet + progression, `error:` → exception, `should_stop` → soft-reset, statut/temps réel (`?`/`!`/`~`/reset), jog/home |

## Suite intégration (`tests/integration/`) — 15 tests ✅ 2026-08-07

> Régressions de la revue de code : contour panélisé **MultiPolygon** rendu sans
> crash, et ouverture de projet qui **ne clampe pas** les valeurs hors plage.

> Inclut aussi : import **ODB++ .tgz**, **évidement adaptatif** (vitesses bord/
> intérieur), **centrage plateau** décalant le G-code (`test_center_on_bed_offsets_gcode`),
> **export par face** distinct + miroir (`test_per_side_export_differs_and_mirrors`),
> et **trous** ajoutés au G-code (`test_holes_add_lines_to_gcode`).

| Test | Vérifie |
|---|---|
| `test_full_pipeline_odb_to_gcode` | Chaîne complète sur la zone du QFN : ODB++ → `LabeledCopper` net-aware → `centerline_isolation` (>100 segments) → `build` G-code. Le G-code contient plusieurs vitesses F (adaptatif effectif), et la longueur gravée **relue depuis le texte** ≈ longueur calculée. |
| `test_drc_runs_on_full_layer` | Le DRC tourne sur le vrai calque top et repère le contact **N$35/SW1** (corroboré visuellement sur la capture Fusion). |
| `test_drc_component_aware_reclassifies_wago` | Conscience composants : le chevauchement **GND/N$7** (connecteur WAGO **P1**) est reclassé « footprint » (faux court éliminé) ; **N$35/SW1** (piste sans composant) reste un vrai court. |
| `test_session_import_gerber` | `Session` importe un dossier Gerber : calques top+bottom, 77 trous, carte ≥ 80 mm. |
| `test_session_import_odb_and_gcode` | `Session` importe l'ODB++ et génère un G-code GRBL valide (`M4`…`M2`). |
| `test_session_project_roundtrip` | Save `.lpcb` → réouverture : réglages rechargés + géométrie **reconstruite depuis la source embarquée**. |
| `test_mainwindow_import_and_layers` | Smoke GUI **headless** (Qt offscreen) : `MainWindow` s'instancie (calques + panneau d'envoi), importe un PCB, rend le canvas sans erreur. Sauté si PySide6 absent. |
| `test_stream_worker_engraves_via_fakegrbl` | Le **thread de streaming** de la GUI envoie tout le G-code à un FakeGRBL (chaîne GUI→sender complète, sans laser). |

## Couverture (mesure du 2026-08-07, `.coveragerc` appliqué)

| Module | Couverture | Reste principalement |
|---|---|---|
| `gcode.py` | **99 %** | branche d'option rare |
| `canvas.py` | **94 %** | branches d'affichage |
| `project.py` | **94 %** | erreurs I/O |
| `netlist.py` | **91 %** | fichiers eda absents |
| `geometry.py` | **89 %** | replis Excellon/robustesse |
| `toolpath.py` | **86 %** | hachures obliques, `LayerJob` |
| `odb.py` | **83 %** | `_read_features` hérité, symboles exotiques |
| `gui/session.py` | **81 %** | branches d'erreur d'import |
| `gui/main_window.py` | 64 % | callbacks UI (dialogues) non exercés en headless |
| `gui/app.py` | 0 % | point d'entrée (`exec()`) |
| **TOTAL** | **83 %** | le cœur (géométrie→G-code) reste ≥ 83-99 % |

## Non testé automatiquement (à couvrir)

- **Gravure réelle Falcon 2** (procédure manuelle) : envoyer un `.gcode` d'isolation
  simple sur une plaque de test, vérifier kerf réel vs kerf configuré, et calibrer
  les points d'ancrage vitesse/puissance de `Calibration`.
- **Fiabilité du DRC** : ✅ fiabilisé le 2026-08-07 — mapping feature→net validé
  (aligné sur eda/data), et **classification par composant** (chevauchement
  intra-composant = « footprint » informationnel vs vrai court inter-composants).
  Le plus gros faux positif (GND/N$7 = connecteur WAGO P1) est désormais écarté.
  Reste à trancher côté design les vrais courts pad↔plan restants (GND/N$11, etc.).
- **Performance de l'axe médian** : ✅ optimisée le 2026-08-07 — clip **groupé**
  (`MultiLineString ∩ gap` en un appel) : **~5 s** au lieu de ~96 s sur la carte
  complète (~18×), même sortie. Reste à mesurer sur cartes plus grandes.
- **GUI PySide6** : à couvrir en pytest-qt une fois implémentée.
