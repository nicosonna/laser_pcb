"""Unites : detection reseau de la graveuse (mDNS + verification de banniere)."""

from __future__ import annotations

import socket
import struct
import threading

from laserpcb import discover as d


def test_mdns_query_and_a_record_parsing():
    """Encodage du nom + extraction des enregistrements A d'une reponse mDNS."""
    assert d._encode_name("fluidnc.local") == b"\x07fluidnc\x05local\x00"
    question = d._encode_name("fluidnc.local") + struct.pack(">HH", 1, 1)
    answer = (b"\xc0\x0c"                                   # nom compresse
              + struct.pack(">HHIH", 1, 1, 120, 4)          # A, IN, ttl, rdlen
              + socket.inet_aton("192.168.1.42"))
    packet = struct.pack(">HHHHHH", 0, 0x8400, 1, 1, 0, 0) + question + answer
    assert d._answers_a(packet) == ["192.168.1.42"]
    # une reponse sans enregistrement A ne doit rien rendre (ni planter)
    empty = struct.pack(">HHHHHH", 0, 0x8400, 0, 0, 0, 0)
    assert d._answers_a(empty) == [] and d._answers_a(b"") == []


def _fake_machine(banner: bytes) -> tuple[str, int, threading.Thread]:
    """Petit serveur TCP local qui repond une banniere, facon FluidNC."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()

    def serve():
        conn, _ = srv.accept()
        conn.recv(64)                      # le reveil \r\n\r\n
        conn.sendall(banner)
        conn.close()
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return host, port, t


def test_probe_reads_fluidnc_banner():
    """Une machine qui repond est confirmee avec sa version de firmware."""
    host, port, t = _fake_machine(b"Grbl 3.0 [FluidNC v3.7.18 (wifi)]\r\n")
    banner = d.probe(host, port, timeout=2.0)
    t.join(2)
    assert banner is not None and "FluidNC" in banner


def test_probe_returns_none_when_nothing_listens():
    """Rien n'ecoute -> None : la machine n'est PAS proposee a l'utilisateur."""
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    _, free_port = s.getsockname(); s.close()
    assert d.probe("127.0.0.1", free_port, timeout=0.3) is None


def test_machine_label_and_firmware_flag():
    m = d.Machine(ip="10.0.0.5", firmware="Grbl 3.0 [FluidNC v3.7]", name="fluidnc")
    assert m.is_fluidnc and "10.0.0.5" in m.label() and "fluidnc" in m.label()
    assert not d.Machine(ip="10.0.0.6", firmware="Grbl 1.1f").is_fluidnc
