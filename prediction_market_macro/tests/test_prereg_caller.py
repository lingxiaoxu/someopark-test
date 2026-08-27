"""prereg.run_all: the weekly caller's load-bearing behaviors.

1. PENDING verdicts produce NO alert — the weekly line must not cry wolf for weeks.
2. A matured (non-PENDING) verdict alerts exactly ONCE — edge-triggered against the
   last alert text, so maturation is an event, not a weekly drumbeat.
3. A grader crashing must not silence the OTHER registrations.
4. Every name in REGS is actually dispatched to a grader — a registration that is
   documented but never run is the exact failure this module was built to end.
"""
from __future__ import annotations

import sqlite3

import pytest

from prediction_market_macro.research import prereg


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE alerts(ts TEXT, level TEXT, source TEXT, message TEXT)")
    return c


def _patch(monkeypatch, verdicts):
    def fake_graders():
        return {name: (lambda v: lambda _c: {"verdict": v})(v)
                for name, v in verdicts.items()}
    monkeypatch.setattr(prereg, "_graders", fake_graders)


def _alerts(conn):
    return [r["message"] for r in conn.execute("SELECT message FROM alerts")]


def test_pending_never_alerts(conn, monkeypatch):
    # built from REGS rather than a hand-listed three, so ADDING a registration cannot
    # pass this test by leaving the new grader unwired — the real risk is a registration
    # that exists in the docs and is never run.
    _patch(monkeypatch, {name: "PENDING — 1/8" for name in prereg.REGS})
    out = prereg.run_all(conn)
    assert set(out) == set(prereg.REGS)
    assert _alerts(conn) == []


def test_every_registration_in_REGS_has_a_real_grader_wired():
    """The unpatched dispatch. `REGS` is what the weekly line claims to cover; `_graders`
    is what it actually calls. PR-11 was written a week before it was wired, and during
    that week the only thing that would have noticed is this."""
    graders = prereg._graders()
    assert set(graders) == set(prereg.REGS)
    assert all(callable(g) for g in graders.values())


def test_matured_verdict_alerts_once_not_weekly(conn, monkeypatch):
    _patch(monkeypatch, {"pr1_claims": "PENDING — 3/8",
                         "pr2_argmax": "PENDING — 2/20",
                         "pr7_s2": "FAIL — roi gap +1.2pp < 5pp, rule removed"})
    prereg.run_all(conn)
    prereg.run_all(conn)                       # next week, same verdict
    assert len(_alerts(conn)) == 1
    assert _alerts(conn)[0].startswith("PREREG pr7_s2: FAIL")
    # ... but a CHANGED verdict text is a new event and must alert again
    _patch(monkeypatch, {"pr1_claims": "PENDING — 3/8",
                         "pr2_argmax": "PENDING — 2/20",
                         "pr7_s2": "FAIL — roi gap +0.9pp < 5pp, rule removed (n=31)"})
    prereg.run_all(conn)
    assert len(_alerts(conn)) == 2


def test_one_crashing_grader_does_not_silence_the_rest(conn, monkeypatch):
    def fake_graders():
        def boom(_c):
            raise RuntimeError("table missing")
        return {"pr1_claims": boom,
                "pr2_argmax": lambda _c: {"verdict": "PENDING — 0/20"},
                "pr7_s2": lambda _c: {"verdict": "PASS — adopted"}}
    monkeypatch.setattr(prereg, "_graders", fake_graders)
    out = prereg.run_all(conn)
    assert out["pr1_claims"].startswith("GRADER ERROR")
    msgs = _alerts(conn)
    # the crash itself matures into an alert too — a dead grader is exactly the
    # "matured but unread" failure this module exists to prevent
    assert any("pr1_claims" in m for m in msgs)
    assert any(m.startswith("PREREG pr7_s2: PASS") for m in msgs)
