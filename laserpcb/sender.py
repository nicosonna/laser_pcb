"""Streamer GRBL pour le Creality Falcon 2 (liaison série USB).

Protocole simple et sûr : on envoie une ligne, on attend ``ok`` (ou ``error:``),
ligne suivante. Suffisant et robuste pour de la gravure PCB.

Le transport est abstrait : une vraie liaison série (``SerialTransport``, pyserial)
ou un **FakeGrbl** en mémoire pour tester tout le streaming **sans matériel**
(dans l'esprit du FakeMatek de fpv_car).

Commandes temps réel GRBL (octets uniques, hors flux de lignes) :
    ?  état · !  feed hold · ~  reprise · 0x18  soft-reset
"""

from __future__ import annotations

import collections
import re
import threading
import time
from typing import Callable, Protocol


class Transport(Protocol):
    def write(self, data: bytes) -> int: ...
    def readline(self) -> bytes: ...
    def close(self) -> None: ...


# Codes d'erreur GRBL/FluidNC les plus utiles, en clair (le code brut
# « error:60 » n'aide pas l'utilisateur a comprendre qu'il manque une carte SD).
GRBL_ERRORS = {
    1: "commande G-code non reconnue",
    2: "nombre mal forme dans la commande",
    3: "commande $ inconnue de ce firmware",
    8: "commande $ refusee : la machine doit etre a l'arret",
    9: "refuse : la machine est en ALARME (deverrouillez ou faites l'origine)",
    15: "deplacement hors course",
    20: "commande G-code non supportee",
    60: "carte SD illisible : absente, mal inseree ou non formatee en FAT32",
    61: "echec de lecture du repertoire de la carte SD",
    62: "fichier introuvable sur la carte SD",
    63: "le fichier de la carte SD est vide",
}


# Les ALARMES ont leur propre numerotation, SANS rapport avec celle des erreurs :
# appliquer la table des error: a un ALARM: donne un message faux (ALARM:2 n'est
# pas « nombre mal forme » mais un depassement de course machine).
GRBL_ALARMS = {
    1: "fin de course declenche : position perdue, refaire l'origine",
    2: "la commande SORT DE LA COURSE MACHINE (soft limit) : le job depasse la "
       "zone de travail, verifiez la position sur le plateau et l'origine",
    3: "reinitialisation pendant un mouvement : position perdue, refaire l'origine",
    4: "echec de palpage",
    5: "echec de palpage",
    6: "origine interrompue par une reinitialisation",
    7: "origine : porte de securite ouverte",
    8: "origine : degagement du fin de course impossible",
    9: "origine : fin de course introuvable (verifiez les capteurs)",
}


def explain(response: str) -> str:
    """« error:60 » -> « error:60 (carte SD illisible...) », et idem pour ALARM."""
    if ":" not in response:
        return response
    head, _, rest = response.partition(":")
    try:
        code = int(rest.strip())
    except ValueError:
        return response
    table = GRBL_ALARMS if head.strip().upper() == "ALARM" else GRBL_ERRORS
    detail = table.get(code)
    return f"{response} ({detail})" if detail else response


class GrblError(Exception):
    def __init__(self, response: str, line: str | None = None):
        super().__init__(explain(response) + (f" sur « {line} »" if line else ""))
        self.response = response
        self.line = line

    @property
    def code(self) -> int:
        """Numero d'erreur GRBL (0 si indeterminable)."""
        try:
            return int(self.response.split(":", 1)[1].strip())
        except (IndexError, ValueError):
            return 0


# --------------------------------------------------------------------------- #
# Transport série réel (pyserial)
# --------------------------------------------------------------------------- #
class SerialTransport:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.5):
        import serial  # import tardif : pyserial optionnel pour les tests
        self.ser = serial.Serial(port, baud, timeout=timeout)

    def write(self, data: bytes) -> int:
        return self.ser.write(data)

    def readline(self) -> bytes:
        return self.ser.readline()

    def close(self) -> None:
        self.ser.close()


def list_ports() -> list[str]:
    """Liste les ports série disponibles (vide si pyserial absent)."""
    try:
        from serial.tools import list_ports as lp
        return [p.device for p in lp.comports()]
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# GRBL en mémoire pour les tests (aucun matériel requis)
# --------------------------------------------------------------------------- #
class FakeGrbl:
    """Simulateur GRBL minimal : répond ``ok``, bannière au reset, état sur ``?``."""

    REALTIME = {0x3F, 0x21, 0x7E, 0x18}  # ? ! ~ reset

    def __init__(self, fail_on: str | None = None):
        self._queue: collections.deque[bytes] = collections.deque()
        self._buf = bytearray()
        self.received: list[str] = []       # lignes G-code reçues
        self.state = "Idle"
        self.reset_count = 0
        self.fail_on = fail_on               # sous-chaîne qui déclenche une erreur
        self._emit("Grbl 1.1f ['$' for help]")

    def _emit(self, text: str) -> None:
        self._queue.append((text + "\r\n").encode())

    def write(self, data: bytes) -> int:
        for b in bytes(data):
            if b in self.REALTIME:
                self._realtime(b)
            elif b == 0x0A:                  # \n -> fin de ligne
                self._line(self._buf.decode(errors="replace").strip())
                self._buf.clear()
            elif b != 0x0D:                  # ignore \r
                self._buf.append(b)
        return len(data)

    def _realtime(self, b: int) -> None:
        if b == 0x3F:                        # ?
            self._emit(f"<{self.state}|MPos:0.000,0.000,0.000|FS:0,0>")
        elif b == 0x21:                      # !
            self.state = "Hold"
        elif b == 0x7E:                      # ~
            self.state = "Run"
        elif b == 0x18:                      # soft reset
            self.reset_count += 1
            self.state = "Idle"
            self._buf.clear()
            self._emit("Grbl 1.1f ['$' for help]")

    def _line(self, line: str) -> None:
        if not line:
            return
        self.received.append(line)
        if self.fail_on and self.fail_on in line:
            self._emit("error:2")            # commande GRBL invalide
        else:
            self._emit("ok")

    def readline(self) -> bytes:
        return self._queue.popleft() if self._queue else b""

    def close(self) -> None:
        pass


class FakeFluidNC(FakeGrbl):
    """Simulateur FluidNC : banniere FluidNC, carte SD, et refus de ``$32``.

    FluidNC configure le mode laser dans son YAML, pas par ``$32`` : le refuser
    ici garantit que le pointeur du panneau reste utilisable malgre l'erreur.
    """

    BANNER = "Grbl 3.0 [FluidNC v3.7.18 (wifi) '$' for help]"

    def __init__(self, fail_on: str | None = None, sd: dict[str, int] | None = None):
        self.sd = dict(sd or {})             # nom -> taille
        self.ran: list[str] = []             # fichiers lances via $SD/Run
        super().__init__(fail_on=fail_on)
        self._queue.clear()
        self._emit(self.BANNER)

    def _realtime(self, b: int) -> None:
        super()._realtime(b)
        if b == 0x18:                        # re-emettre NOTRE banniere
            self._queue.pop()
            self._emit(self.BANNER)

    def _line(self, line: str) -> None:
        if not line:
            return
        self.received.append(line)
        if line.startswith("$32"):
            self._emit("error:3")            # reglage inconnu chez FluidNC
        elif line == "$SD/List":
            for name, size in self.sd.items():
                self._emit(f"[FILE:{name}|SIZE:{size}]")
            self._emit("ok")
        elif line.startswith("$SD/Run="):
            name = line.split("=", 1)[1]
            if name in self.sd:
                self.ran.append(name)
                self._emit("ok")
            else:
                self._emit("error:9")
        else:
            super()._line(line)
            self.received.pop()              # super() l'a deja enregistree


# --------------------------------------------------------------------------- #
# Nettoyage du G-code
# --------------------------------------------------------------------------- #
_COMMENT = re.compile(r"\([^)]*\)")


def gcode_bounds(gcode: str):
    """Emprise (minx, miny, maxx, maxy) des X/Y d'un G-code, commentaires
    exclus — un nom de projet « X12 » dans l'entete ne doit pas compter.
    None si aucune coordonnee."""
    xs, ys = [], []
    for line in clean_lines(gcode):
        for m in re.finditer(r"X(-?[0-9.]+)", line):
            xs.append(float(m.group(1)))
        for m in re.finditer(r"Y(-?[0-9.]+)", line):
            ys.append(float(m.group(1)))
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def clean_lines(gcode: str) -> list[str]:
    """Retire commentaires ``;`` / ``(...)`` et lignes vides."""
    out = []
    for raw in gcode.splitlines():
        line = _COMMENT.sub("", raw)
        line = line.split(";", 1)[0].strip()
        if line:
            out.append(line)
    return out


# --------------------------------------------------------------------------- #
# Streamer
# --------------------------------------------------------------------------- #
class GrblSender:
    firmware: str = ""          # banniere du controleur (rempli par connect)
    log = None                  # callback(sens, texte) pour le terminal du GUI

    def __init__(self, transport: Transport, empty_reads_timeout: int = 2000):
        self.t = transport
        self._empty_timeout = empty_reads_timeout
        self._wlock = threading.Lock()       # sérialise les écritures (streaming vs temps
        self._quiet = False                  # coupe le journal pendant la gravure
        self._iolock = threading.RLock()     # sérialise les ÉCHANGES complets
                                             # (ordre + sa réponse) : sinon un
                                             # sondage d'état volerait le « ok »
                                             # d'un jog en cours réel)

    def _write(self, data: bytes) -> int:
        """Écriture série protégée par verrou (pause/reprise depuis le thread GUI
        pendant que le thread de streaming écrit les lignes)."""
        with self._wlock:
            return self.t.write(data)

    # ---- connexion ---------------------------------------------------------- #
    def connect(self) -> None:
        """Reveille le controleur et memorise sa banniere (GRBL ou FluidNC)."""
        self._write(b"\r\n\r\n")             # reveille le controleur
        time.sleep(0)                        # (no-op ; hook si transport reel lent)
        self.firmware = ""
        for _ in range(200):                 # la banniere arrive dans les 1res lignes
            raw = self.t.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if line.startswith("Grbl") or "FluidNC" in line:
                self.firmware = line
                break
        if not self.firmware:
            # FluidNC n'emet sa banniere qu'au BOOT : sur une liaison telnet
            # ouverte apres coup, rien n'arrive. On demande $I (build info).
            try:
                for text in self.send_collect(chr(36) + 'I'):
                    if text.startswith('[VER:'):
                        self.firmware = text.strip('[]').rstrip(':')
                        break
            except (GrblError, TimeoutError):
                pass                 # controleur muet : on reste generique
        self._drain()

    @property
    def is_fluidnc(self) -> bool:
        """FluidNC annonce « Grbl 3.0 [FluidNC vX.Y.Z ...] » dans sa banniere."""
        return "FluidNC" in self.firmware

    def _drain(self) -> None:
        while self.t.readline():
            pass

    # ---- protocole ligne/réponse ------------------------------------------- #
    def _read_response(self) -> str:
        empties = 0
        while True:
            raw = self.t.readline()
            if not raw:
                # En pause (feed hold) la machine n'acquitte plus rien : ce
                # silence est normal et ne doit pas finir en TimeoutError (qui
                # coupait le flux et desactivait « Reprise », machine bloquee en
                # Hold avec son tampon plein).
                if not getattr(self, "_holding", False):
                    empties += 1
                if empties > self._empty_timeout:
                    raise TimeoutError("aucune réponse de GRBL")
                continue
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            if line == "ok":
                return line
            if line.startswith("error") or line.startswith("ALARM"):
                raise GrblError(line)
            # message poussé (état <...>, [MSG:...], bannière) -> ignoré ici

    def _emit_log(self, way: str, text: str) -> None:
        """Journalise un echange. Muet pendant la gravure : des dizaines de
        milliers de lignes noieraient le terminal et ralentiraient le flux."""
        if self.log is None or self._quiet:
            return
        try:
            self.log(way, text)
        except Exception:  # noqa: BLE001
            pass            # le journal ne doit JAMAIS casser une commande

    def send_line(self, line: str) -> str:
        with self._iolock:
            self._emit_log(">", line)
            self._write((line + "\n").encode())
            reply = self._read_response()
            self._emit_log("<", reply)
            return reply

    def stream(self, gcode: str,
               progress: Callable[[int, int], None] | None = None,
               should_stop: Callable[[], bool] | None = None) -> dict:
        """Envoie tout le G-code ligne par ligne. Retourne un bilan."""
        lines = clean_lines(gcode)
        total = len(lines)
        self._quiet = True              # flux : on ne journalise pas ligne a ligne
        try:
            for i, line in enumerate(lines):
                if should_stop and should_stop():
                    self.soft_reset()
                    return {"stopped": True, "sent": i, "total": total}
                self.send_line(line)
                if progress:
                    progress(i + 1, total)
            return {"stopped": False, "sent": total, "total": total}
        finally:
            # PIEGE (relecture 13.10.2026) : une erreur GRBL en plein flux
            # laissait le terminal muet pour le reste de la session.
            self._quiet = False

    # ---- commandes temps réel ---------------------------------------------- #
    def status(self) -> str:
        """Etat courant (Idle/Run/Alarm/Hold...). Bloquant : a appeler
        depuis un thread, jamais depuis le fil de l'interface."""
        with self._iolock:
            return self._status()

    def _status(self) -> str:
        self._write(b"?")
        for _ in range(100):
            raw = self.t.readline()
            if not raw:
                continue
            line = raw.decode(errors="replace").strip()
            if line.startswith("<"):
                return line[1:].split("|")[0].split(">")[0]
        return "?"

    def feed_hold(self) -> None:
        self._holding = True
        self._write(b"!")

    def resume(self) -> None:
        self._holding = False
        self._write(b"~")

    def soft_reset(self) -> None:
        self._holding = False
        self._write(b"\x18")
        self._drain()

    # ---- mouvements -------------------------------------------------------- #
    def jog(self, dx: float = 0.0, dy: float = 0.0, feed: float = 1000.0) -> str:
        return self.send_line(f"$J=G91 G21 X{dx:.3f} Y{dy:.3f} F{feed:.0f}")

    def home(self) -> str:
        return self.send_line("$H")

    def unlock(self) -> str:
        return self.send_line("$X")

    # ---- origine de travail (absolu) --------------------------------------- #
    def set_work_zero(self) -> str:
        """Définit la position actuelle comme origine de travail (0,0)."""
        return self.send_line("G10 L20 P1 X0 Y0")

    def machine_space(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """Plage de coordonnees MACHINE utilisable par axe : ((x0, x1), (y0, y1)).

        PIEGE (relecture 13.10.2026) : un GRBL classique fait son homing au
        coin MAX et travaille en coordonnees NEGATIVES (MPos de -course a 0,
        G54 = -400,-415) ; d'autres configurations (FluidNC selon le YAML)
        travaillent en positif (0 a course). L'ancien controle supposait le
        positif : sur une machine en negatif il refusait TOUS les jobs, et la
        « correction » (G54 a 0,0) placait l'origine de travail au coin de
        butee -> job physiquement hors enveloppe.

        Heuristique : en espace positif un decalage de travail est toujours
        >= 0 ; un G54 franchement negatif signe donc l'espace negatif. A
        verifier sur une machine inconnue avec ``$#`` et ``?``.
        """
        tx, ty = self.max_travel()
        ox, oy = self.work_offset()
        neg = ox < -1.0 or oy < -1.0
        return ((-tx, 0.0) if neg else (0.0, tx), (-ty, 0.0) if neg else (0.0, ty))

    def clear_work_offset(self) -> str:
        """Remet l'origine de travail (G54) sur le COIN BAS de l'espace machine.

        ``G10 L2 P1`` fixe l'origine du repere 1 en coordonnees machine absolues
        (a ne pas confondre avec ``G10 L20`` qui la place a la position COURANTE).
        En espace positif c'est 0,0 ; en espace negatif c'est (-course, -course),
        de sorte que les coordonnees de travail (positives) restent dans
        l'enveloppe. Utile quand un ancien « Definir 0,0 » decale tout le travail.
        """
        (x0, _), (y0, _) = self.machine_space()
        return self.send_line(f"G10 L2 P1 X{x0:g} Y{y0:g}")

    def goto_zero(self) -> str:
        """Retour à l'origine de travail en absolu."""
        return self.send_line("G90 G0 X0 Y0")

    def goto(self, x: float, y: float) -> str:
        """Déplacement ABSOLU vers un point des coordonnées de travail."""
        return self.send_line(f"G90 G0 X{x:.3f} Y{y:.3f}")

    def laser_on(self, percent: float = 1.0, max_s: int = 1000) -> str:
        """Allume le laser à faible puissance, à l'arrêt, pour repérer la position.

        On désactive le mode laser ($32=0) pour un point fixe (sinon GRBL ne tire
        que pendant les déplacements). ``laser_off`` restaure le mode laser.
        """
        s = int(round(max(0.0, min(percent, 100.0)) / 100.0 * max_s))
        self._try("$32=0")
        return self.send_line(f"M3 S{s}")

    def laser_off(self) -> str:
        self.send_line("M5")
        return self._try("$32=1")

    def _try(self, line: str) -> str:
        """Envoie une commande FACULTATIVE : une erreur n'interrompt pas l'action.

        FluidNC configure le mode laser dans son YAML (``laser_mode:``) et peut
        refuser ``$32`` ; le pointeur reste utilisable (M3 S faible), simplement
        le comportement depend alors du reglage de la machine.
        """
        try:
            return self.send_line(line)
        except GrblError:
            return "ignored"

    # ---- limites machine ----------------------------------------------------- #
    def work_offset(self) -> tuple[float, float]:
        """Decalage de l'origine de travail G54 (X, Y) en coordonnees machine."""
        for line in self.send_collect("$#"):
            if line.startswith("[G54:"):
                vals = line.strip("[]").split(":", 1)[1].split(",")
                return (float(vals[0]), float(vals[1]))
        return (0.0, 0.0)

    def max_travel(self) -> tuple[float, float]:
        """Course machine configuree ($130, $131), en mm. (0, 0) si inconnue."""
        tr = {}
        for line in self.send_collect("$$"):
            k, _, v = line.partition("=")
            if k.strip() in ("$130", "$131"):
                try:
                    tr[k.strip()] = float(v)
                except ValueError:
                    pass
        return (tr.get("$130", 0.0), tr.get("$131", 0.0))

    def out_of_travel(self, bounds) -> str:
        """Verifie qu'un job tient dans la course, OFFSET DE TRAVAIL COMPRIS.

        Sans ce controle la machine part, grave, puis s'arrete en ALARM:2 au
        premier point hors course -- avec une plaque deja entamee.
        ``bounds`` = (minx, miny, maxx, maxy) en coordonnees de TRAVAIL.
        Renvoie un message d'explication, ou "" si tout va bien.
        """
        if not bounds:
            return ""
        tx, ty = self.max_travel()
        if not tx or not ty:
            return ""                       # course inconnue : on ne bloque pas
        ox, oy = self.work_offset()
        (x0, x1), (y0, y1) = self.machine_space()
        minx, miny, maxx, maxy = bounds
        bad = []
        if ox + maxx > x1 + 1e-6 or ox + minx < x0 - 1e-6:
            bad.append(f"X {ox + minx:.0f}..{ox + maxx:.0f} mm (course {x0:.0f}..{x1:.0f})")
        if oy + maxy > y1 + 1e-6 or oy + miny < y0 - 1e-6:
            bad.append(f"Y {oy + miny:.0f}..{oy + maxy:.0f} mm (course {y0:.0f}..{y1:.0f})")
        if not bad:
            return ""
        return ("Le job sort de la course machine : " + " et ".join(bad) + "."
                + chr(10) + chr(10)
                + f"Origine de travail decalee de X{ox:+.0f} Y{oy:+.0f}. "
                + "Soit remettez cette origine a zero (Home puis Definir 0,0 au "
                + "point d origine machine), soit mettez Position X/Y a 0 dans "
                + "le logiciel.")


    # ---- carte SD FluidNC --------------------------------------------------- #
    def send_collect(self, line: str) -> list[str]:
        """Envoie une commande et renvoie les lignes recues jusqu'au ``ok``."""
        with self._iolock:
            return self._collect(line)

    def _collect(self, line: str) -> list[str]:
        out: list[str] = []
        self._write((line + chr(10)).encode())
        empties = 0
        while True:
            raw = self.t.readline()
            if not raw:
                empties += 1
                if empties > self._empty_timeout:
                    raise TimeoutError("aucune reponse du controleur")
                continue
            text = raw.decode(errors="replace").strip()
            if not text:
                continue
            if text == "ok":
                return out
            if text.startswith("error") or text.startswith("ALARM"):
                raise GrblError(text, line)
            out.append(text)

    def sd_list(self) -> dict[str, int]:
        """Fichiers de la carte SD FluidNC : ``{nom: taille_octets}``.

        Reponses de la forme ``[FILE:/job.gcode|SIZE:12345]``.
        """
        files: dict[str, int] = {}
        for text in self.send_collect("$SD/List"):
            if "FILE:" not in text:
                continue
            body = text.strip("[]")
            name = body.split("FILE:", 1)[1].split("|")[0].strip()
            size = 0
            if "SIZE:" in body:
                digits = "".join(ch for ch in body.split("SIZE:", 1)[1] if ch.isdigit())
                size = int(digits or 0)
            files[name] = size
        return files

    def sd_run(self, name: str) -> str:
        """Lance la gravure d'un fichier de la carte SD, en autonome."""
        path = name if name.startswith("/") else "/" + name
        return self.send_line(f"$SD/Run={path}")

    def close(self) -> None:
        self.t.close()


# --------------------------------------------------------------------------- #
# Transport réseau FluidNC (ESP32) — telnet brut, port 23
# --------------------------------------------------------------------------- #
class TelnetTransport:
    """Liaison WiFi vers FluidNC : même protocole GRBL, sans câble USB.

    Expose la même interface que ``SerialTransport`` (write/readline/close), et
    la même sémantique de ``readline`` : renvoie ``b""`` quand rien n'arrive dans
    le délai imparti (le sender compte ces lectures vides pour son timeout).
    """

    def __init__(self, host: str, port: int = 23, timeout: float = 0.5,
                 connect_timeout: float = 5.0):
        import socket
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.sock.settimeout(timeout)
        self._buf = b""

    def write(self, data: bytes) -> int:
        self.sock.sendall(data)
        return len(data)

    def readline(self) -> bytes:
        while b"\n" not in self._buf:
            try:
                chunk = self.sock.recv(4096)
            except OSError:                  # timeout : pas de données pour l'instant
                break
            if not chunk:                    # connexion fermée par la machine
                break
            self._buf += chunk
        if b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            return line + b"\n"
        out, self._buf = self._buf, b""
        return out

    def close(self) -> None:
        self.sock.close()


def fluidnc_upload(host: str, filename: str, gcode: str,
                   timeout: float = 120.0) -> None:
    """Téléverse un G-code sur la **carte SD** de FluidNC (HTTP multipart).

    Le format exact de ``/upload`` n'est pas garanti d'une version à l'autre et
    des échecs silencieux existent (fichier de 0 octet si la carte est absente ou
    mal formatée) : l'appelant DOIT vérifier avec ``GrblSender.sd_list``.
    """
    import urllib.request
    payload = gcode.encode()
    name = filename if filename.startswith("/") else "/" + filename
    bnd = "----laserpcb" + hex(abs(hash(filename)) & 0xFFFFFF)[2:]
    parts = []

    def field(key: str, value: str) -> None:
        parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="{key}"'
                     f'\r\n\r\n{value}\r\n'.encode())

    field("path", "/")
    field(f"{name}S", str(len(payload)))      # indice de taille (convention ESP3D)
    parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="myfile[]"; '
                 f'filename="{name}"\r\nContent-Type: application/octet-stream'
                 f'\r\n\r\n'.encode())
    parts.append(payload)
    parts.append(f"\r\n--{bnd}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"http://{host}/upload", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={bnd}",
                 "Content-Length": str(len(body))})
    urllib.request.urlopen(req, timeout=timeout).read()
