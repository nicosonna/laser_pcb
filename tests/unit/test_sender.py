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
