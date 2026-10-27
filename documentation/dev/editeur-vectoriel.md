# Éditeur vectoriel (formes, lignes, texte)

> Note de conception. Décisions prises en discussion le 2026-10-18, **avant**
> toute ligne de code. À relire avant de commencer une étape.

## Objectif

Dessiner dans laser_pcb comme dans LightBurn : rectangles, ellipses, lignes,
polylignes et texte, posés à la souris, gravés par la chaîne existante.

Deux usages, **tous les deux retenus** :

1. **compléter un PCB** importé (logo, numéro de série, cadre, repère) ;
2. **graver sans PCB du tout** (boîtier, plaque, pochoir, gabarit).

Conséquence : le socle est conçu pour le cas général dès le départ, mais
l'annotation de PCB est livrée en premier.

## Ce qui existe déjà et sert de fondation

| Brique | Où | Remarque |
|---|---|---|
| Géométrie | shapely, partout | rectangles, cercles, polylignes : natif |
| Texte vectorisé | `toolpath.text_polygons()` | via les polices système (matplotlib TextPath) |
| Remplissage | `toolpath.hatch()` | trame simple ou croisée, avec ou sans contour |
| Chaîne G-code | `gcode.build()` / `optimize()` | un item = `(coords, vitesse, puissance, catégorie)` |
| Sauvegarde | `Project.to_dict()` → JSON `.lpcb` | dataclass sérialisée |

Un objet dessiné produit un item comme les autres : il hérite donc sans effort
de l'optimisation de parcours, des phases, de l'estimation de durée et de
l'aperçu couleur.

## Décisions actées

### 1. Les objets sont PARAMÉTRIQUES, pas figés

Un rectangle est `(x, y, largeur, hauteur, rotation)`, pas une liste de points.
La géométrie shapely en est **dérivée** et mise en cache.

*Pourquoi* : sans ça, un texte n'est plus rééditable après avoir été posé, et
chaque redimensionnement accumule des erreurs d'arrondi.

Exception : un tracé importé (SVG/DXF, plus tard) est un objet de type `path`
dont la géométrie est figée — il n'y a pas de paramètres à en extraire.

### 2. Socle de rendu : `QGraphicsView`, pas matplotlib

Le canevas actuel (`gui/canvas.py`) est en matplotlib : parfait pour afficher,
inadapté à l'édition — pas de graphe de scène, pas de détection de clic sur un
objet, pas de poignées. Tout serait à écrire à la main, et chaque mouvement de
souris redessinerait les ~19 000 tracés d'une carte.

`QGraphicsScene` fournit nativement sélection, déplacement, rectangle de
sélection, plans de profondeur et transformations. La carte devient un fond
statique ; les objets dessinés sont des éléments vivants.

**Piège connu** : Qt dessine Y vers le bas, la machine Y vers le haut. Se règle
par une transformation de la vue — et piège systématiquement le texte, qui se
retrouve en miroir si on l'oublie.

### 3. Modèle de calques façon LightBurn

Un **calque de travail** porte couleur, vitesse, puissance, passes et mode
(trait / remplissage / les deux). Chaque objet est affecté à un calque.

Ne pas confondre avec les **couches source** (cuivre dessus/dessous,
sérigraphie) issues du Gerber. Le gestionnaire de calques fait déjà la
séparation sous « Étapes + couches source » : s'appuyer dessus, et nommer les
deux notions distinctement dans l'interface.

### 4. L'annulation se conçoit au début

`QUndoStack` dès la mise en place du modèle : chaque action est une commande.
Rajouter annuler/refaire sur un éditeur déjà écrit revient à le réécrire.

## Découpage

| Étape | Contenu | Livré |
|---|---|---|
| ~~1. Socle~~ **fait** | canevas matplotlib → `QGraphicsView`, règles graduées, cadrage plateau | affichage plus fluide, position lisible |
| ~~2. Modèle~~ **fait** | `laserpcb/drawing.py` : objets paramétriques + calques de travail, sauvegarde `.lpcb`, parcours, étape « Dessin », couleurs | formes gravables, posées par le code |
| ~~3. Éditeur~~ **fait** | outils rect/ellipse/ligne/texte, sélection, poignées, rotation, annuler-refaire, aimantation | le LightBurn-like |
| ~~4. Confort~~ **fait** | aimantation (grille, pads, coins de carte), alignement, duplication, centrage | l'usage quotidien |
| ~~5. Import~~ **SVG fait** | SVG → objets `trace` (`laserpcb/svgimport.py`, via `svgelements`). DXF pas encore. | logos et dessins faits ailleurs |
| ~~6. Feuille vierge~~ **fait** | `Session.content_bounds()` : l'emprise vient du DESSIN à défaut de carte ; menu « Nouveau dessin » | laser_pcb généraliste |

Étapes 1 et 2 : courtes et mécaniques. **Étape 3 : longue**, et à valider par
allers-retours — poignées, aimantation et ressenti souris ne se jugent qu'à
l'usage.

## Ce que l'étape 2 a mis en place

- `laserpcb/drawing.py` — modèle PUR (aucun Qt) : `Shape` (rect, ellipse,
  polyligne, texte, tracé figé) et `WorkLayer` (couleur, vitesse, puissance,
  passes, mode). Géométrie **dérivée** des paramètres, rotation comprise.
- `Project.work_layers` / `Project.shapes` / `Project.drawing_enabled`,
  sérialisés dans le `.lpcb`. Un projet écrit avant le dessin se relit.
- `Session.drawing_items_tagged()` → catégorie `dessin`, intégrée au job complet
  (si la case est cochée) et lançable seule par le bouton d'étape **Dessin**.
- La signature du cache des parcours inclut le dessin : une forme modifiée est
  recalculée (sinon le cache aurait servi l'ancienne version).
- Aperçu : chaque forme s'affiche dans la couleur de SON calque, et le
  gestionnaire de calques gagne une ligne « Dessin ».

Point d'attention relevé à l'implémentation : un objet **ouvert** (polyligne non
fermée) n'a pas de remplissage possible. Plutôt que de ne rien graver, on se
rabat sur le trait.

## Deux pièges rencontrés à l'étape 3 (à ne pas réintroduire)

- **Ne jamais nommer un attribut ``shape`` sur un ``QGraphicsItem``.**
  ``shape()`` est une méthode VIRTUELLE de Qt, qui attend un ``QPainterPath``.
  L'écraser par une dataclass fait renvoyer n'importe quoi au moteur de scène :
  violation d'accès immédiate, sans la moindre exception Python. L'attribut
  s'appelle ``model``.
- **Un réaffichage ne doit jamais partir en plein événement souris.** Le rendu
  reconstruit la scène (``scene.clear()``), ce qui détruit côté C++ l'élément
  dont on traite justement l'événement ; Qt y revient ensuite et le process
  meurt. ``DrawingEditor._refresh()`` diffère donc l'émission au prochain tour
  de boucle, et le canevas prévient l'éditeur (``scene_cleared()``) avant de
  vider, pour qu'il lâche poignées et aperçu.

Troisième piège, plus discret : **l'emprise d'une sélection se calcule sur la
géométrie du MODÈLE**, jamais sur ``sceneBoundingRect()`` — celui-ci inclut la
demi-épaisseur du trait, ce qui faussait tous les redimensionnements (un
facteur 2 demandé donnait 1,89).

## Points réglés aux étapes 4 à 6

- **Import SVG** : trois conversions, chacune vérifiée par un test — unités
  (user units → mm via `width`/`viewBox`), axe Y (le SVG descend, la machine
  monte), et trous (règle pair-impair rendue par différences symétriques
  successives, bien plus sûr que de deviner les emboîtements). Un import entier
  tient dans UNE commande d'annulation.
- **Aimantation à deux niveaux** : les repères de la carte (coins, centre,
  centres des pads) priment sur la grille — ils ne tombent presque jamais
  dessus, et ce sont eux qu'on vise en annotant un circuit. Les repères sont
  mis en cache : les recalculer à chaque mouvement de souris serait intenable.
- **Travail sans PCB** : tout le logiciel supposait une carte. `content_bounds()`
  renvoie l'emprise de la carte, ou celle du dessin à défaut ; `plate_bounds`,
  `piece_origin` et `center_on_bed` en dépendent désormais. Cadrage, phases,
  envoi et G-code fonctionnent sur une feuille vierge.

## Plateaux multiples et rangement automatique

Un dessin de découpe tient rarement sur une plaque : les huit panneaux d'une
étagère font 400 × 330 mm chacun. D'où deux notions ajoutées après coup :

- **Un plateau = un job.** `Shape.plate` porte le numéro, `Session.active_plate`
  dit lequel est affiché et gravé. L'aperçu, les parcours et le G-code ne
  voient jamais que celui-là : on grave, on change la plaque, on passe au
  suivant.
- **`laserpcb/arrange.py`** range les pièces par étagères (*first-fit
  decreasing height*) et ouvre autant de plateaux qu'il en faut. Ce n'est pas
  l'optimum — le placement optimal est NP-difficile — mais c'est rapide,
  stable, et le résultat reste lisible : des rangées nettes plutôt que des
  pièces semées en biais.

**Une pièce n'est pas une forme.** Un panneau découpé, c'est son contour ET
ses perçages — souvent autant de chemins SVG distincts. Les ranger séparément
envoie les trous à l'autre bout de la plaque, où ils ne veulent plus rien dire.
`arrange.group_shapes()` réunit donc les formes qui se **recouvrent réellement**
(pas qui se touchent : deux panneaux posés bord à bord ne font pas une pièce),
et un groupe se déplace et tourne en bloc.

Tourner un groupe demande un report : chaque forme tourne autour de SON centre,
il faut donc la replacer là où la rotation du groupe l'aurait emmenée. Sans ce
report, les perçages restent en arrière pendant que le panneau pivote.

La **rotation à 90°** n'est pas un luxe : un panneau de 400,2 mm ne passe pas
dans 400 mm de large, mais passe dans 415 mm de haut. Sans elle, huit pièces
sur huit seraient déclarées trop grandes.

Ce qui ne tient pas, même tourné, est **signalé** et posé seul sur son plateau.
L'empiler discrètement dans un coin serait pire qu'un message.

Mesuré sur un StorageShelf réel : 408 formes regroupées en 9 pièces (8
panneaux portant 399 perçages, plus une réglette), 8 plateaux, 0 perçage sorti
de son panneau, 0 chevauchement entre pièces, 0 débordement — en 0,2 s.

## Vue multi-plateaux

Tous les plateaux sont affichés **côte à côte**, comme dans un trancheur 3D :
on voit d'un coup d'œil tout le travail et sur quelle plaque chaque pièce ira.
Le plateau actif est en bleu plein, les autres estompés.

Chaque plateau porte trois icônes : **centrer**, **ranger**, **vider**.

Trois pièges, tous rencontrés :

- **Le repère.** Une forme du plateau 3 vit dans le repère de SON plateau, pas
  à 800 mm de l'origine. `canvas.to_model(pt, plate)` et
  `from_model(x, y, plate)` portent le décalage ; `plate_at(pt)` dit quel
  plateau est sous le curseur. Un geste de tracé retient le plateau de son
  PREMIER point, sinon un glisser mordant sur le voisin couperait la forme.
- **L'espacement des icônes.** Leur taille est fixe à l'écran
  (`ItemIgnoresTransformations`) : leur espacement doit l'être aussi. Placées à
  un écart en millimètres, elles se chevauchaient dès qu'on dézoomait.
  `_layout_plate_buttons()` les repositionne à chaque changement d'échelle.
- **Deux cadrages distincts.** « Ajuster » cadre le plateau de TRAVAIL,
  « Plateau » les montre TOUS. Confondre les deux rend l'un des deux gestes
  impossible.

Seul le plateau actif est manipulable — les autres restent visibles mais
figés : une poignée saisie sur un plateau voisin travaillerait dans le mauvais
repère. En revanche, **lâcher une pièce sur le plateau d'à côté l'y transfère**
(`DrawingEditor.move_shape`), coordonnées locales conservées.

## Assistant de meubles (`laserpcb/boxgen.py`)

Dans l'esprit de boxes.py : on décrit le meuble par ses cotes, le logiciel en
déduit tous les panneaux et leurs assemblages à tenons. Cinq modèles — boîte
ou plateau, bac à compartiments, casier d'étagère, armoire murale, tiroir —
dans une fenêtre à part, avec aperçu immédiat.

Pour un **tiroir**, les cotes saisies sont celles du COMPARTIMENT à équiper, et
le jeu de coulissement est retiré ensuite : c'est le calcul où l'on se trompe
d'un côté.

**Les cloisons sont TENUES.** Chacune porte des tenons à ses deux bouts, qui
traversent les parois par des mortaises. Le croisement à mi-bois entre cloisons
ne suffit pas : en 1 × 4, une cloison ne croise rien et flotterait dans la
caisse — défaut constaté à l'usage.

Corollaire relevé au passage : une mortaise est une coupe INTÉRIEURE, que le
trait élargit ; un tenon est une coupe EXTÉRIEURE, que le trait rétrécit. Ils
doivent donc être dessinés en sens inverse — l'un plus large, l'autre plus
étroit d'un demi-trait. Les mortaises ne le faisaient pas : 0,2 mm de jeu là où
les encoches de bord donnaient un ajustement serré, deux comportements pour un
même geste. Après coupe, tenon et mortaise font maintenant la même cote.

**Poignées** : trou rond, trou oblong, encoche sur le dessus, ou deux trous de
vis pour une poignée du commerce. Un trou se perce DANS la façade ; une encoche
se retranche de son CONTOUR. Les confondre ferait découper une boucle fermée
traversant le bord, qui libérerait un morceau de façade. Sur une porte, la
poignée se place du côté OPPOSÉ au gond, sinon elle est inatteignable.

**L'arête verticale est un VRAI peigne.** Premier jet : l'avant portait des
tenons, le côté des encoches. Mais les deux panneaux ne se croisent pas dans la
même région de l'espace — le tenon de l'avant sortait dans le vide et l'encoche
du côté ne recevait rien. L'arête ne tenait donc rien du tout, défaut invisible
à l'écran et constaté sur une pièce montée.

Les deux panneaux portent maintenant des tenons qui **s'alternent** et se
partagent la colonne d'angle. Pour qu'ils soient exactement complémentaires,
l'arête est découpée en un nombre **impair** de segments égaux : l'avant prend
les pairs, le côté les impairs. Impair, donc les deux extrémités reviennent au
même panneau — ce qui remplit les deux coins.

Conséquence sur les cotes : les côtés font toute la profondeur (et non
`y - 2t`), leurs tenons comblant l'épaisseur du voisin.

Vérifié par échantillonnage : sur 400 hauteurs et quatre formats de caisse, la
colonne d'angle est occupée **exactement une fois** — jamais vide (coin percé),
jamais deux fois (panneaux qui ne rentrent pas).

**Les coins doivent être PLEINS.** Le motif commence et finit par un creux sur
les bords horizontaux — les coins y restent ainsi en matière pleine. Mais sur
les arêtes VERTICALES, ce même choix laissait un trou traversant de
l'épaisseur du bois en haut et en bas de chaque angle : ni le panneau avant ni
le côté n'occupaient la colonne d'angle. Défaut constaté sur une pièce réelle,
invisible à l'écran.

Les panneaux avant et arrière portent donc un tenon à chaque extrémité de
leurs arêtes verticales (`_COINS_V`). Les côtés, eux, gardent le motif
ordinaire : ils n'ont pas de matière dans cette colonne, et une encoche posée
pile dans le coin dégénère avec le bord voisin — le contour devient invalide.

**L'assemblage.** Deux panneaux perpendiculaires se joignent par un peigne :
l'un porte des tenons qui dépassent de l'épaisseur du voisin, l'autre des
encoches de même profondeur, aux mêmes positions. Le motif commence et finit
par un creux — les coins restent pleins — et il est symétrique, donc identique
lu à l'envers : aucune question d'orientation au montage.

Les tenons comblent l'épaisseur du voisin, donc **les cotes extérieures
annoncées sont celles du meuble assemblé**.

**Le jeu de coupe.** Le laser enlève de la matière : sans compensation, un tenon
sort trop petit et flotte. `kerf` élargit les tenons et rétrécit les encoches
d'un demi-trait chacun.

**La découpe se règle par la DOSE, pas par les passes.** Ce qui traverse le
bois, c'est l'énergie déposée par millimètre de course :
`passes × puissance / vitesse`. `cut_profile()` part de la dose nécessaire et
en déduit la VITESSE — le nombre de passes devient un choix libre.

Conséquence utile : à énergie égale, **plus de passes permet d'aller plus vite
à chaque passe**, ce qui brûle moins les bords qu'une seule passe lente. Le
temps total, lui, ne change pas.

La dose est calée sur une mesure RÉELLE : 4 mm de contreplaqué traversés en
1 passe à 80 % et 300 mm/min. Premier jet du générateur : 5 passes à 300 — soit
**cinq fois trop d'énergie**, découvert à l'usage (« le bois était coupé à 20 %
du temps »). D'où le bouton **« Recalibrer sur une découpe réussie »**, qui
redéfinit la dose à partir d'un essai de l'utilisateur et la mémorise : le bois
varie trop d'un lot à l'autre pour qu'une constante tienne.

**Le décor est de la GRAVURE.** Motifs géométriques et textes partent sur un
calque à faible puissance, par défaut sur les seules façades (avant, porte).
Les confondre avec la découpe ruinerait le panneau.

## Vue 3D du meuble assemblé

Pas de moteur 3D : une **projection isométrique** suffit à juger un meuble, et
elle n'ajoute aucune dépendance. Chaque panneau porte sa POSE — `Piece.place =
(origine, u, v)`, trois vecteurs de l'espace — et c'est au générateur de la
renseigner : lui seul sait comment ça se monte.

Les panneaux sont traités comme des plans sans épaisseur, peints du plus
lointain au plus proche (algorithme du peintre) et éclairés selon leur
orientation : c'est ce dégradé qui donne le relief. Les ouvertures sont
soustraites du contour, donc on voit **à travers** les mortaises.

Le point de vue **tourne à la souris** (azimut et élévation), molette pour le
zoom, double-clic pour revenir à la vue de départ. L'élévation est bornée à
±85° : au-delà on regarde le meuble par le dessus et la vue devient un plan.

Un curseur « Éclaté » écarte les panneaux le long de leur normale, pour
vérifier le montage.

Deux pièges du tri par profondeur, tous deux rencontrés :

- **la profondeur se prend au CENTRE RÉEL du panneau.** Avec une taille
  forfaitaire, une tablette large passait devant une porte étroite — le centre
  de la tablette étant plus proche de l'œil que celui de la porte ;
- **une porte est toujours l'élément le plus avant**, ce que le tri par
  centre ne peut pas voir. Elle est donc forcée au dernier rang
  (`Piece.depth_group`) — mais seulement quand on regarde la façade : si l'on
  tourne derrière le meuble, c'est le dos qu'on doit voir, et la porte repasse
  au tout premier rang.

Piège : une pièce sans pose disparaît de la vue **en silence**. Un test
parcourt les quatre générateurs et refuse toute pièce qui n'en a pas.

## Meuble de cuisine

Le modèle le plus composite : un caisson ouvert en façade, dont la hauteur
utile est partagée en sections — des tiroirs de bas en haut, puis une section
à porte s'il y a lieu. Une tablette sépare deux sections et sert de glissière
au tiroir du dessus.

**Chaque tiroir est un meuble complet**, produit par `make_drawer` : il reçoit
ses propres compartiments et sa poignée. Le générateur de cuisine ne fait que
composer des briques déjà éprouvées, puis décaler leur POSE pour qu'elles
apparaissent à leur place dans la vue 3D — les tracés de découpe, eux, restent
à plat.

**Le jeu de coulissement est TOTAL**, pas par côté : saisir 4 mm donne 4 mm de
battement en largeur (2 de chaque côté). Compté par côté, un « 4 mm » intuitif
en aurait fait 8 et le tiroir aurait dansé dans son logement.

## Sélection et presse-papiers

Une pièce est **sélectionnable depuis n'importe quel plateau** : cliquer dessus
bascule sur son plateau et conserve la sélection. Aller d'abord choisir le
plateau dans le sélecteur du haut était une corvée. Seul le plateau actif reste
DÉPLAÇABLE — une poignée saisie ailleurs travaillerait dans le mauvais repère.

**Copier / couper / coller** passent aussi par le presse-papiers système, en
JSON : on peut ainsi déplacer des pièces d'un projet à l'autre. Le collage
atterrit sur le plateau affiché, légèrement décalé — collé pile sur l'original,
on ne saurait plus lequel on manipule.

## Percer avant de détourer

`arrange.cut_order()` ordonne les formes pour que tout ce qui est intérieur —
perçages, mortaises, décor — passe **tant que la pièce est encore tenue par la
plaque**, le contour extérieur en dernier. Une pièce détourée trop tôt glisse,
bascule ou colle au laser.

Deux conditions pour que ça tienne jusqu'au bout :

- le regroupement doit rattacher au panneau un tracé **sans aire** (le décor
  gravé est une ligne) : sinon chaque trait de motif partait sur son propre
  plateau ;
- la catégorie `dessin` est **exclue de l'optimiseur de trajets**, qui
  réordonne librement et détruirait l'ordre. Entre pièces, un simple
  plus-proche-voisin limite les trajets à vide sans jamais les entrelacer.

## Reste à faire

- **Import DXF** (l'autre format de CAO). Le type d'objet `trace` est déjà en
  place : seul le lecteur manque.
- **Texte avancé** : choix de police, alignement, interlettrage, texte sur un
  chemin. L'étape 3 se limite à une chaîne dans une police système.
- **Gravure d'images matricielles** (photo) : toujours hors périmètre.

## Risques

- **Le portage du canevas touche l'écran le plus utilisé.** Moment le plus
  risqué du chantier : à faire d'un bloc, avec les tests comme filet, et pas en
  même temps qu'autre chose.
- **Les logos n'arrivent qu'à l'étape 5.** Un logo ne se dessine pas aux
  primitives : il vient d'un fichier. Si le besoin est pressant, un lecteur SVG
  minimal (chemins, rectangles, cercles) est peu coûteux et peut remonter à
  l'étape 2 — il ne demande pas l'éditeur.
- **Le texte « façon LightBurn »** (choix de police, alignement, interlettrage,
  texte sur un chemin) est un chantier à lui seul. L'étape 3 se limite à poser
  une chaîne dans une police système, à une taille donnée.

## Points encore ouverts

- Où vivent les objets dessinés : dans `Project` (donc dans le `.lpcb`) ou dans
  un document séparé ? La feuille vierge (étape 6) pousse vers un `Document`
  qui contient *éventuellement* un PCB, plutôt qu'un PCB qui contient
  éventuellement des dessins.
- Faut-il une catégorie d'item « dessin » dédiée et un bouton d'étape dans
  l'onglet d'envoi ? (probablement oui, cohérent avec `PHASES`)
- Gravure d'images matricielles (photo) : hors périmètre pour l'instant.
