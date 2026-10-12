"""Detection automatique de la graveuse sur le reseau (mDNS, sans dependance).

FluidNC publie son nom d'hote en mDNS (``fluidnc.local`` par defaut, reglable par
``hostname:`` dans son YAML). ATTENTION : depuis la v4.1.0 il n'annonce plus les
SERVICES ``_telnet._tcp`` / ``_http._tcp`` (regression amont) — un parcours de
services ne trouverait rien. On resout donc directement les noms d'hotes, puis on
VERIFIE chaque candidat en lisant sa banniere sur le port telnet : seule une
machine qui repond vraiment est proposee.

Trois sources, cumulees et dedupliquees :
1. requete mDNS brute (UDP 224.0.0.251:5353) — marche meme sans support OS ;
2. resolution systeme du ``.local`` (Windows 11, Bonjour, Avahi) ;
3. noms supplementaires fournis par l'utilisateur.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

MDNS_ADDR, MDNS_PORT = "224.0.0.251", 5353
DEFAULT_NAMES = ("fluidnc", "grblesp", "cnc", "laser")
TELNET_PORT = 23


@dataclass(frozen=True)
class Machine:
    """Une graveuse detectee et confirmee (elle a repondu sa banniere)."""
    ip: str
    firmware: str
    name: str = ""

    @property
    def is_fluidnc(self) -> bool:
        return "FluidNC" in self.firmware

    def label(self) -> str:
        who = self.name or self.ip
        return f"{who} ({self.ip}) — {self.firmware}" if self.firmware else who


# --------------------------------------------------------------------------- #
# mDNS minimal : requete A sur <nom>.local
# --------------------------------------------------------------------------- #
def _encode_name(name: str) -> bytes:
    out = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".") if p)
    return out + b"\x00"


def _read_name(buf: bytes, pos: int) -> int:
    """Avance au-dela d'un nom DNS (gere la compression par pointeur)."""
    while pos < len(buf):
        ln = buf[pos]
        if ln == 0:
            return pos + 1
        if ln & 0xC0 == 0xC0:               # pointeur : 2 octets, fin du nom
            return pos + 2
        pos += 1 + ln
    return pos


def mdns_resolve(hostname: str, timeout: float = 1.2) -> list[str]:
    """Resout ``<hostname>.local`` en adresses IPv4 par requete mDNS directe."""
    fqdn = hostname if hostname.endswith(".local") else hostname + ".local"
    query = (struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
             + _encode_name(fqdn) + struct.pack(">HH", 1, 1))   # QTYPE A, QCLASS IN
    found: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.sendto(query, (MDNS_ADDR, MDNS_PORT))
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(4096)
            except OSError:
                break
            for ip in _answers_a(data):
                if ip not in found:
                    found.append(ip)
    finally:
        sock.close()
    return found


def _answers_a(data: bytes) -> list[str]:
    """Extrait les adresses des enregistrements A d'une reponse DNS/mDNS."""
    if len(data) < 12:
        return []
    qd, an = struct.unpack(">HH", data[4:8])        # qdcount, ancount
    pos = 12
    for _ in range(qd):                     # saute les questions
        pos = _read_name(data, pos) + 4
    out: list[str] = []
    for _ in range(an):
        pos = _read_name(data, pos)
        if pos + 10 > len(data):
            break
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4 and pos + 4 <= len(data):
            out.append(socket.inet_ntoa(data[pos:pos + 4]))
        pos += rdlen
    return out


def os_resolve(hostname: str) -> list[str]:
    """Resolution systeme du ``.local`` (Windows 11 / Bonjour / Avahi)."""
    fqdn = hostname if hostname.endswith(".local") else hostname + ".local"
    try:
        infos = socket.getaddrinfo(fqdn, None, socket.AF_INET)
    except OSError:
        return []
    return list({i[4][0] for i in infos})


# --------------------------------------------------------------------------- #
# Verification : la machine repond-elle vraiment ?
# --------------------------------------------------------------------------- #
def probe(ip: str, port: int = TELNET_PORT, timeout: float = 1.0) -> str | None:
    """Ouvre le port telnet et renvoie la banniere (ou None si ce n'est pas une
    graveuse). On reveille le controleur comme le fait le sender."""
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except OSError:
        return None
    try:
        sock.settimeout(timeout)
        # FluidNC n'emet sa banniere QU'AU BOOT : sur un telnet ouvert apres coup
        # il ne dit rien. On demande $I (build info) -> [VER:...]. Pas de reveil
        # par ligne vide : elle repondrait 'ok' avant la version.
        sock.sendall(b"$I\n")
        buf, saw_ok = b"", False
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(512)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            for line in buf.decode(errors='replace').splitlines():
                line = line.strip()
                if line.startswith("[VER:"):          # reponse a $I
                    return line.strip("[]").rstrip(":")
                if line.startswith("Grbl") or "FluidNC" in line:
                    return line
                if line == "ok":
                    saw_ok = True
        return "contrôleur GRBL" if saw_ok else ""
    finally:
        sock.close()


def discover(names=DEFAULT_NAMES, timeout: float = 1.2) -> list[Machine]:
    """Cherche les graveuses du reseau et ne renvoie que celles qui repondent."""
    from concurrent.futures import ThreadPoolExecutor
    candidates: dict[str, str] = {}         # ip -> nom d'hote
    # Resolutions EN PARALLELE : un nom inexistant bloque plusieurs secondes cote
    # systeme, les enchainer rendait la detection interminable (15 s mesurees).
    with ThreadPoolExecutor(max_workers=16) as ex:
        jobs = {name: (ex.submit(mdns_resolve, name, timeout),
                       ex.submit(os_resolve, name)) for name in names}
        for name, (f_mdns, f_os) in jobs.items():
            for ip in f_mdns.result() + f_os.result():
                candidates.setdefault(ip, name)
    machines = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        probes = {ip: ex.submit(probe, ip) for ip in candidates}
        for ip, fut in probes.items():
            banner = fut.result()
            if banner is not None:          # None = rien n'ecoute -> on ignore
                machines.append(Machine(ip=ip, firmware=banner, name=candidates[ip]))
    machines.sort(key=lambda m: (not m.is_fluidnc, m.ip))
    return machines
