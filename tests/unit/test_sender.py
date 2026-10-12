"""Unités : streamer GRBL contre un FakeGRBL (aucun matériel requis)."""

from __future__ import annotations

import pytest

from laserpcb.sender import FakeGrbl, GrblSender, GrblError, clean_lines


def test_clean_lines_strips_comments():
    g = "; entete\nG21\nG1 X1 (rapide) ; note\n\nM2\n"
    assert clean_lines(g) == ["G21", "G1 X1", "M2"]


def test_stream_sends_all_lines_and_progress():
    fake = FakeGrbl()
    s = GrblSender(fake)
    s.connect()
    seen = []
    res = s.stream("G21\nG1 X5 F600 S500\nM5\n", progress=lambda i, n: seen.append((i, n)))
    assert res == {"stopped": False, "sent": 3, "total": 3}
    assert fake.received == ["G21", "G1 X5 F600 S500", "M5"]
    assert seen[-1] == (3, 3)


def test_stream_raises_on_error():
    fake = FakeGrbl(fail_on="G1")            # GRBL renverra error: sur les G1
    s = GrblSender(fake)
    s.connect()
    with pytest.raises(GrblError):
        s.stream("G21\nG1 X5\n")


def test_should_stop_aborts_and_resets():
    fake = FakeGrbl()
    s = GrblSender(fake)
    s.connect()
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2                 # stoppe après 2 lignes

    res = s.stream("G21\nG1 X1\nG1 X2\nG1 X3\n", should_stop=stop)
    assert res["stopped"] is True
    assert res["sent"] == 2
    assert fake.reset_count == 1             # soft-reset émis


def test_status_and_realtime():
    fake = FakeGrbl()
    s = GrblSender(fake)
    s.connect()
    assert s.status() == "Idle"
    s.feed_hold()
    assert s.status() == "Hold"
    s.resume()
    assert s.status() == "Run"
    s.soft_reset()
    assert fake.reset_count == 1
    assert s.status() == "Idle"


def test_jog_and_home():
    fake = FakeGrbl()
    s = GrblSender(fake)
    s.connect()
    s.jog(dx=1.0, dy=-2.0, feed=800)
    s.home()
    assert any(l.startswith("$J=G91") for l in fake.received)
    assert "$H" in fake.received


def test_origin_and_pointer():
    """Outils d'origine : définir 0,0, y aller, pointeur laser (mode laser OFF)."""
    fake = FakeGrbl()
    s = GrblSender(fake)
    s.connect()
    s.set_work_zero()
    s.goto_zero()
    s.laser_on(1.0)
    s.laser_off()
    assert "G10 L20 P1 X0 Y0" in fake.received
    assert "G90 G0 X0 Y0" in fake.received
    assert any(l.startswith("M3 S") for l in fake.received)   # pointeur allumé
    assert "$32=0" in fake.received and "$32=1" in fake.received  # mode laser off/on
    assert "M5" in fake.received


def test_fluidnc_banner_and_pointer_despite_rejected_setting():
    """FluidNC : banniere reconnue, et le pointeur marche meme si $32 est refuse
    (chez FluidNC le mode laser est dans le YAML, pas dans $32)."""
    from laserpcb.sender import FakeFluidNC
    fake = FakeFluidNC()
    s = GrblSender(fake)
    s.connect()
    assert s.is_fluidnc and "FluidNC" in s.firmware
    s.laser_on(1.0)                       # ne doit PAS lever malgre error:3
    assert "M3 S10" in fake.received
    s.laser_off()
    assert "M5" in fake.received


def test_fluidnc_sd_list_and_run():
    """Carte SD : listing (nom + taille) et lancement autonome."""
    from laserpcb.sender import FakeFluidNC, GrblError
    fake = FakeFluidNC(sd={"/job.gcode": 2048})
    s = GrblSender(fake)
    s.connect()
    assert s.sd_list() == {"/job.gcode": 2048}
    s.sd_run("job.gcode")
    assert fake.ran == ["/job.gcode"]
    # fichier absent -> erreur remontee (pas de gravure fantome)
    try:
        s.sd_run("absent.gcode")
    except GrblError:
        pass
    else:
        raise AssertionError("un fichier absent doit lever une erreur")


def test_telnet_transport_readline_buffers_partial_chunks():
    """Le transport WiFi doit reconstituer les lignes coupees en morceaux TCP,
    et rendre b'' quand rien n'arrive (meme semantique que pyserial)."""
    from laserpcb.sender import TelnetTransport
    t = TelnetTransport.__new__(TelnetTransport)     # sans ouvrir de socket
    t._buf = b""
    chunks = [b"Grbl 3.0 [Fl", b"uidNC v3.7]\r\nok\r\n", b""]
    class _S:
        def recv(self, n): return chunks.pop(0) if chunks else b""
    t.sock = _S()
    assert t.readline() == b"Grbl 3.0 [FluidNC v3.7]\r\n"
    assert t.readline() == b"ok\r\n"
    assert t.readline() == b""


def test_error_codes_are_explained_in_plain_language():
    """Un code brut n'aide pas : error:60 doit parler de carte SD."""
    from laserpcb.sender import GrblError, explain
    assert "carte SD" in explain("error:60")
    assert "ALARME" in explain("error:9")
    assert explain("error:999") == "error:999"       # code inconnu : tel quel
    e = GrblError("error:60", "$SD/List")
    assert e.code == 60 and "carte SD" in str(e) and "$SD/List" in str(e)
