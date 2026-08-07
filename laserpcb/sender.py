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


class GrblError(Exception):
    def __init__(self, response: str, line: str | None = None):
        super().__init__(f"GRBL {response}" + (f" sur « {line} »" if line else ""))
        self.response = response
        self.line = line


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
    def __init__(self, transport: Transport, empty_reads_timeout: int = 2000):
        self.t = transport
        self._empty_timeout = empty_reads_timeout
        self._wlock = threading.Lock()       # sérialise les écritures (streaming vs temps réel)

    def _write(self, data: bytes) -> int:
        """Écriture série protégée par verrou (pause/reprise depuis le thread GUI
        pendant que le thread de streaming écrit les lignes)."""
        with self._wlock:
            return self.t.write(data)

    # ---- connexion ---------------------------------------------------------- #
    def connect(self) -> None:
        self._write(b"\r\n\r\n")             # réveille GRBL
        time.sleep(0)                        # (no-op ; hook si transport réel lent)
        self._drain()

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
        self.send_line("$32=0")
        return self.send_line(f"M3 S{s}")

    def laser_off(self) -> str:
        self.send_line("M5")
        return self.send_line("$32=1")

    def close(self) -> None:
        self.t.close()
