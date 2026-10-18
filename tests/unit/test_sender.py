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


def test_alarm_codes_are_not_confused_with_error_codes():
    """ALARM et error ont des numérotations SÉPARÉES : appliquer la table des
    erreurs à une alarme donnait un message faux (ALARM:2 n'est pas « nombre mal
    formé » mais un dépassement de course machine)."""
    from laserpcb.sender import explain
    assert "COURSE MACHINE" in explain("ALARM:2")
    assert "nombre mal forme" not in explain("ALARM:2")
    assert "nombre mal forme" in explain("error:2")
    assert "fin de course introuvable" in explain("ALARM:9")
    assert explain("ALARM:99") == "ALARM:99"          # code inconnu : tel quel


def test_out_of_travel_accounts_for_work_offset():
    """Le contrôle de course doit inclure le DÉCALAGE de l'origine de travail :
    un job qui tient en coordonnées de travail peut sortir en coordonnées machine."""
    from laserpcb.sender import FakeFluidNC, GrblSender

    class _Machine(FakeFluidNC):
        def _line(self, line):
            if line == "$#":
                self._emit("[G54:150.000,200.000,0.000]")
                self._emit("ok")
            elif line == "$$":
                self._emit("$130=400.000"); self._emit("$131=415.000")
                self._emit("ok")
            else:
                super()._line(line)

    s = GrblSender(_Machine())
    s.connect()
    assert s.work_offset() == (150.0, 200.0) and s.max_travel() == (400.0, 415.0)
    # job centré : 150+256 = 406 > 400 -> refusé, avec les chiffres
    msg = s.out_of_travel((143.95, 168.4, 256.15, 246.65))
    assert msg and "406" in msg and "400" in msg
    # même job ramené à l'origine de travail : passe
    assert s.out_of_travel((0.0, 0.0, 112.1, 78.1)) == ""


def test_clear_work_offset_uses_absolute_form():
    """G10 L2 (origine ABSOLUE) et non L20 (position courante) : sinon on
    remplacerait un décalage par un autre."""
    from laserpcb.sender import FakeFluidNC
    fake = FakeFluidNC()
    s = GrblSender(fake)
    s.connect()
    s.clear_work_offset()
    assert "G10 L2 P1 X0 Y0" in fake.received
    assert not any("L20" in line for line in fake.received)


class _FakeWithLimits(FakeGrbl):
    """FakeGRBL qui connait sa course ($130/$131) et son decalage G54."""

    def __init__(self, travel=(400.0, 415.0), g54=(0.0, 0.0)):
        super().__init__()
        self.travel, self.g54 = travel, g54

    def _line(self, line: str) -> None:
        self.received.append(line)
        if line == "$$":
            self._emit(f"$130={self.travel[0]:.3f}")
            self._emit(f"$131={self.travel[1]:.3f}")
        elif line == "$#":
            self._emit(f"[G54:{self.g54[0]:.3f},{self.g54[1]:.3f},0.000]")
        self._emit("ok")


def test_stream_resets_quiet_after_error():
    """Une erreur GRBL en plein flux ne doit pas laisser le terminal muet."""
    fake = FakeGrbl(fail_on="G1")
    s = GrblSender(fake)
    s.connect()
    with pytest.raises(GrblError):
        s.stream("G21\nG1 X5 F600\n")
    assert s._quiet is False


def test_out_of_travel_positive_space():
    s = GrblSender(_FakeWithLimits(g54=(10.0, 10.0)))
    s.connect()
    assert s.machine_space() == ((0.0, 400.0), (0.0, 415.0))
    assert s.out_of_travel((0, 0, 100, 100)) == ""
    assert "X" in s.out_of_travel((0, 0, 395, 100))          # 10 + 395 > 400


def test_out_of_travel_negative_space_grbl_classic():
    """GRBL classique : homing au coin max, MPos de -course a 0, G54 = -course.
    Un job positif en coordonnees de travail tient ; l'ancien controle le
    refusait toujours et sa correction envoyait l'origine au coin de butee."""
    fake = _FakeWithLimits(g54=(-400.0, -415.0))
    s = GrblSender(fake)
    s.connect()
    assert s.machine_space() == ((-400.0, 0.0), (-415.0, 0.0))
    assert s.out_of_travel((0, 0, 300, 300)) == ""
    assert "X" in s.out_of_travel((0, 0, 405, 100))
    s.clear_work_offset()
    assert fake.received[-1] == "G10 L2 P1 X-400 Y-415"      # coin BAS de l'espace


def test_feed_hold_suspends_response_timeout():
    """En pause la machine n'acquitte plus : pas de TimeoutError tant que ca dure."""
    fake = FakeGrbl()
    s = GrblSender(fake)
    s.connect()
    s._empty_timeout = 3
    s.feed_hold()
    fake._queue.clear()
    # 20 lectures vides en Hold, puis la reponse arrive : pas d'exception
    import itertools
    n = itertools.count()

    def readline():
        if next(n) < 20:
            return b""
        return b"ok\r\n"
    fake.readline = readline
    assert s._read_response() == "ok"
    s.resume()
    n = itertools.count()
    with pytest.raises(TimeoutError):                       # hors pause : timeout
        s._read_response()


def test_gcode_bounds_ignores_comments():
    from laserpcb.sender import gcode_bounds
    g = "; projet X12 Y999\nG21\nG0 X10 Y20 (X500)\nG1 X30 Y40 F600 S10\nM5\n"
    assert gcode_bounds(g) == (10.0, 20.0, 30.0, 40.0)
    assert gcode_bounds("; rien\nM5\n") is None
