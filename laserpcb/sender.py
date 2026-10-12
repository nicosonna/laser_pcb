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


def explain(response: str) -> str:
    """« error:60 » -> « error:60 (carte SD illisible : ...) »."""
    if ":" not in response:
        return response
    try:
        code = int(response.split(":", 1)[1].strip())
    except ValueError:
        return response
    detail = GRBL_ERRORS.get(code)
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

    def __init__(self, transport: Transport, empty_reads_timeout: int = 2000):
        self.t = transport
        self._empty_timeout = empty_reads_timeout
        self._wlock = threading.Lock()       # sérialise les écritures (streaming vs temps
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

    def send_line(self, line: str) -> str:
        with self._iolock:
            self._write((line + "\n").encode())
            return self._read_response()

    def stream(self, gcode: str,
               progress: Callable[[int, int], None] | None = None,
               should_stop: Callable[[], bool] | None = None) -> dict:
        """Envoie tout le G-code ligne par ligne. Retourne un bilan."""
        lines = clean_lines(gcode)
        total = len(lines)
        for i, line in enumerate(lines):
            if should_stop and should_stop():
                self.soft_reset()
                return {"stopped": True, "sent": i, "total": total}
            self.send_line(line)
            if progress:
                progress(i + 1, total)
        return {"stopped": False, "sent": total, "total": total}

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
        self._write(b"!")

    def resume(self) -> None:
        self._write(b"~")

    def soft_reset(self) -> None:
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

    def goto_zero(self) -> str:
        """Retour à l'origine de travail en absolu."""
        return self.send_line("G90 G0 X0 Y0")

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
