# lasercam — caméra d'alignement sur la tête du laser

Firmware pour la **Joy-IT SBC-ESP32-Cam** (clone AI-Thinker : OV2640 2 MP,
4 Mo de PSRAM). Elle sert deux choses sur le réseau :

- un **flux vidéo** que `laser_pcb` affiche avec un réticule logiciel, pour
  positionner la pièce sans gabarit ;
- une sortie **croix laser** (GPIO13), pilotée par HTTP.

## Les deux modes

Le mode est décidé **au démarrage**, une fois pour toutes :

| | Quand | Où la joindre |
|-|-------|---------------|
| **Station** | un réseau est mémorisé et répond | `<nom>.local` (mDNS) |
| **Point d'accès** | aucun réseau mémorisé, ou injoignable | `192.168.4.1`, réseau `lasercam-setup` (mot de passe `laserpcb`) |

En point d'accès, la carte ouvre un **portail captif** : le téléphone ou le PC
qui rejoint `lasercam-setup` voit la page de configuration s'ouvrir toute
seule. On choisit le réseau dans la liste, on tape le mot de passe, la carte
redémarre et le rejoint.

**La caméra reste entièrement utilisable dans ce mode** — image, flux, croix
laser, tout répond sur `192.168.4.1`. C'est le mode « sans réseau » : on vise
une pièce sur le terrain, sans box ni routeur. Seule limite, la graveuse n'est
alors pas sur le même réseau que la caméra : il faut la piloter en USB.

Le réglage vit en **NVS** (`Preferences`), pas dans le code : on reconfigure la
carte sans la reflasher. Pour la faire changer de réseau, deux chemins :

- `http://<nom>.local/wifi` — la même page, servie aussi sur le réseau
  (le bouton **Réseau…** de l'onglet *Caméra* l'ouvre) ;
- `http://<nom>.local/forget` — elle oublie le réseau et redémarre sur son
  portail.

Et si le réseau mémorisé disparaît (déménagement de la machine, box changée),
elle rouvre son portail d'elle-même au démarrage suivant : elle ne devient
jamais injoignable. En revanche une coupure **en cours de route** ne la fait pas
basculer — elle tente de se reconnecter en boucle, parce que basculer sur le
portail en pleine gravure couperait la liaison.

## Pourquoi la PSRAM compte

Les 4 Mo de PSRAM de cette carte permettent deux tampons d'image en 800×600.
Sans eux, l'OV2640 est cantonné au QVGA avec un seul tampon — donc un flux
saccadé et une précision de pointage divisée par trois. Le firmware détecte la
PSRAM et se replie tout seul si elle manque.

## Câblage de la croix laser

Un module croix 5 V tire 30 à 80 mA : **beaucoup trop pour une broche d'ESP32**
(12 mA raisonnables). Il faut un transistor.

```
            +5 V ─────────────┬──────────────┐
                              │              │
                        [module croix]   [ESP32-CAM 5V]
                              │
GPIO13 ──[ 100 Ω ]──┬── G  ┌──┴──┐
                    │      │ MOSFET N (AO3400, BSS138, 2N7002)
               [ 100 kΩ ]  │  D (drain) = module croix
                    │      │  S (source) = GND
                   GND     └──┬──┘
                              │
            GND ──────────────┴──────────────┘
```

Le **100 kΩ de tirage au repos est obligatoire**. Au reset, les GPIO de l'ESP32
repassent en entrée : sans lui, la grille du MOSFET flotte et la croix peut
s'allumer toute seule pendant le démarrage.

### Pourquoi GPIO13

Avec la caméra active, presque toutes les broches sont prises. GPIO13 est la
seule broche du connecteur à la fois libre et **sans rôle de strapping** au
démarrage. À éviter : GPIO12 (MTDI, fixe la tension du flash), GPIO15 (MTDO),
GPIO2 et GPIO0 (mode téléchargement), GPIO16 (PSRAM), GPIO14/15/2 (carte SD).
Le firmware n'utilise pas la carte SD, justement pour garder GPIO13 libre.

GPIO4 pilote la **LED blanche** de la carte, utile pour éclairer le FR4 (son
cuivre et sa résine ont des reflets proches). Elle chauffe vite : le firmware la
pilote en PWM, reste sous `v=80` en usage continu.

## Flashage

La carte n'a pas d'USB : il faut un adaptateur **USB-TTL 3,3 V** (FTDI, CP2102).

| ESP32-CAM | Adaptateur |
|-----------|------------|
| 5V        | 5V         |
| GND       | GND        |
| U0T (IO1) | RX         |
| U0R (IO3) | TX         |
| IO0       | GND *(seulement pour flasher)* |

1. Arduino IDE → Préférences → URL de gestionnaire de cartes :
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
2. Carte : **AI Thinker ESP32-CAM**, `Partition Scheme: Huge APP (3MB)`,
   `PSRAM: Enabled`.
3. `IO0` relié à `GND`, puis reset de la carte → elle est en mode flash.
4. **Rien à configurer** : téléverser tel quel, la carte ouvrira son portail.
   Si tu préfères qu'elle connaisse déjà le réseau au premier démarrage, copier
   `secrets.h.example` en **`secrets.h`** et le remplir — ce fichier est ignoré
   par git, et il ne sert que d'amorce quand la NVS est encore vide. Ensuite
   c'est le portail qui commande.
5. Débrancher `IO0` de `GND`, reset. Le moniteur série (115200) affiche l'IP.

### En ligne de commande

`arduino-cli` est livré avec Arduino IDE 2.x
(`…\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`) :

```bash
URL=https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32 --additional-urls $URL
arduino-cli compile -b esp32:esp32:esp32cam firmware/lasercam
arduino-cli upload -b esp32:esp32:esp32cam -p COM6 firmware/lasercam
```

Pas d'options a passer a la FQBN : `esp32:esp32:esp32cam` a deja `huge_app` et
la PSRAM activee par defaut. En ajouter leve `invalid option 'PSRAM'`.

`IO0` doit être à la masse **au moment du reset** pour que le téléversement
passe. En revanche le reset lui-même peut être automatique : si `EN` est relié
au `RTS` de l'adaptateur (cas du montage ici), `esptool` le pulse tout seul
— il annonce alors `Hard resetting via RTS pin`. Seul le pont `IO0`–`GND`
reste manuel. Même ligne pour lire la bannière de démarrage sans débrancher :
ouvrir le port, basculer `RTS` vrai puis faux, et écouter.

## Vérification

Navigateur sur `http://lasercam.local/` (ou l'IP) : image live, boutons croix et
lumière. Ou sans GUI :

```bash
python scripts/camera_test.py
```

## API

| Route | Port | Effet |
|-------|------|-------|
| `/` | 80 | page de contrôle de secours |
| `/status` | 80 | JSON : `fw`, `version`, `mode` (`sta`/`ap`), `host`, `ip`, `ssid`, `rssi`, `psram`, `framesize`, `quality`, `laser`, `led`, `uptime_s` |
| `/capture` | 80 | une image JPEG |
| `/control?var=framesize&val=8` | 80 | réglages capteur (`framesize`, `quality`, `hmirror`, `vflip`, `contrast`, `brightness`, `saturation`, `awb`, `aec`, `agc`, `gainceiling`) |
| `/laser?on=1&ms=60000` | 80 | croix laser, extinction auto après `ms` |
| `/led?v=0..255` | 80 | LED blanche |
| `/wifi` | 80 | page de configuration réseau (les deux modes) |
| `/save` | 80 | **POST** du formulaire, puis redémarrage |
| `/forget` | 80 | oublie le réseau, redémarre sur le portail |
| `/stream` | **81** | flux MJPEG `multipart/x-mixed-replace` |

`/save` est en **POST** et pas en GET : un mot de passe n'a rien à faire dans
une URL, qui finit dans l'historique du navigateur et dans les journaux. Le
corps est décodé à la main (`url_decode`) car `httpd_query_key_value`
d'ESP-IDF ne touche ni aux `%xx` ni aux `+` — sans ça, un mot de passe
contenant une espace serait enregistré de travers et la carte deviendrait
injoignable sans reflashage.

Le flux est sur un **second serveur** : le httpd d'ESP-IDF ne traite qu'une
requête à la fois, et un flux MJPEG ne se termine jamais — sur un serveur unique
il bloquerait `/laser` et `/control` pour de bon.

Valeurs de `framesize` utiles : `5` = QVGA 320×240, `8` = VGA 640×480,
`9` = SVGA 800×600, `10` = XGA 1024×768.

## Sécurité

- **La croix s'éteint toute seule** après 120 s sans nouvelle commande
  (`LASER_TIMEOUT_MS`). Une liaison WiFi qui tombe ne laisse pas un faisceau
  allumé sur la machine.
- Elle est mise à l'état bas **avant** le WiFi et la caméra au démarrage.
- Pas d'authentification sur l'API : cette carte n'a rien à faire sur un réseau
  ouvert. Un module croix est typiquement classe 2/3R — il ne se regarde pas en
  face, même dirigé vers le plateau.
- Le portail de configuration est derrière le **WPA2 du point d'accès** : il
  faut connaître le mot de passe `laserpcb` pour y arriver. Change-le dans
  `AP_PASS` si la machine vit dans un lieu partagé.

## Montage mécanique

- **Mise au point** : la lentille est à vis, focale fixe. Règle-la une fois,
  tête à la hauteur de gravure, puis **bloque-la** (vernis à ongles). Si elle
  tourne, tout l'étalonnage pixel↔mm est à refaire.
- **Fumée** : la lentille se voile vite. Monte la caméra décalée du panache, et
  colle un verre sacrificiel devant.
- **Câble** : alimente en 5 V par un câble le long de la chaîne porte-câbles,
  sans rien prélever sur l'électronique de la machine. Laisse du mou : un câble
  qui tire sur la tête, c'est de la précision perdue.
- **Antenne** : antenne PCB intégrée, dans un cadre métallique. Si le RSSI lu
  dans `/status` est pire que −75 dBm, le flux va hacher.

## Bande passante — le vrai piège

Le G-code part en telnet vers FluidNC sur le **même WiFi**. Mesuré sur ce
montage (SVGA, qualité 12, RSSI −59 dBm) : **19,8 img/s, 360 ko/s**, soit
2,9 Mbit/s en continu — et la gigue que ça crée peut affamer le tampon de la
machine en pleine gravure.

`laser_pcb` **coupe le flux au démarrage d'un job** et refuse de le rouvrir
pendant (voir `CameraPanel.set_locked`). De toute façon la fumée masque la vue.
Pour garder la vidéo pendant une gravure : passer le job par la **carte SD**, la
machine devient autonome et le réseau est libre.
