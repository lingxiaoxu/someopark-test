"""prereg.run_all: the weekly caller's load-bearing behaviors.

1. PENDING verdicts produce NO alert — the weekly line must not cry wolf for weeks.
2. A matured (non-PENDING) verdict alerts exactly ONCE — edge-triggered against the
   last alert text, so maturation is an event, not a weekly drumbeat.
3. A grader crashing must not silence the OTHER registrations.
4. Every name in REGS is actually dispatched to a grader — a registration that is
   documented but never run is the exact failure this module was built to end.
5. A scorer that grades several registrations is called ONCE and fanned out, and the
   three CPI verdicts do not get crossed.
6. An undocumented fingerprint change alerts even while the verdict is PENDING, on its
   own prefix so it cannot re-trigger the verdict alert.
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


# --- the multi-verdict scorers ------------------------------------------------------

def test_a_scorer_grading_three_registrations_is_run_once_not_three_times(monkeypatch):
    """`shadow_nowcast` is a REPLAY — a predict per event per arm. Reading three verdicts
    off it must cost one call, or the weekly line pays triple for one answer."""
    calls = []

    class _M:
        __name__ = "shadow_nowcast"

        @staticmethod
        def run(_conn):
            calls.append(1)
            return {"code": {}, "pr8": {"verdict": "V-yoy", "core_verdict": "V-core"},
                    "pr10": {"verdict": "V-mom"}}

    monkeypatch.setattr(
        "prediction_market_macro.research.shadow_nowcast", _M, raising=False)
    g = prereg._graders()
    got = {k: g[k](None)["verdict"] for k in
           ("pr8_nowcast_yoy", "pr8_nowcast_core", "pr10_nowcast_mom")}
    assert len(calls) == 1
    # and not crossed — core must not read the headline's verdict, which is the one with
    # evidence behind it; core's adoption was a user decision on a tie
    assert got == {"pr8_nowcast_yoy": "V-yoy", "pr8_nowcast_core": "V-core",
                   "pr10_nowcast_mom": "V-mom"}


def test_a_dead_shared_scorer_is_not_retried_once_per_verdict(monkeypatch):
    calls = []

    class _M:
        __name__ = "shadow_nowcast"

        @staticmethod
        def run(_conn):
            calls.append(1)
            raise RuntimeError("table missing")

    monkeypatch.setattr(
        "prediction_market_macro.research.shadow_nowcast", _M, raising=False)
    g = prereg._graders()
    for k in ("pr8_nowcast_yoy", "pr8_nowcast_core", "pr10_nowcast_mom"):
        with pytest.raises(RuntimeError):
            g[k](None)
    assert len(calls) == 1


def test_each_multi_verdict_registration_matures_on_its_own_schedule(conn, monkeypatch):
    """PR-10's first forward event is 2026-09-11; PR-12's twelfth week is months later.
    One maturing must not wait for, or drag along, the other."""
    _patch(monkeypatch, {"pr10_nowcast_mom": "PASS — mean delta +0.51",
                         "pr12_width_natgas": "PENDING — 0/12 forward weeks"})
    prereg.run_all(conn)
    msgs = _alerts(conn)
    assert len(msgs) == 1 and msgs[0].startswith("PREREG pr10_nowcast_mom: PASS")


# --- the fingerprint channel --------------------------------------------------------

def _patch_code(monkeypatch, name, verdict, code):
    monkeypatch.setattr(prereg, "_graders",
                        lambda: {name: lambda _c: {"verdict": verdict, "code": code}})


CHANGED = {"code_changed_since_registration": True, "change_is_documented": False,
           "note": "model/cpi.py moved to deadbeef"}


def test_an_undocumented_code_change_alerts_even_while_the_verdict_is_pending(
        conn, monkeypatch):
    """The dangerous moment is mid-window: the paired comparison is being voided while
    the verdict channel is deliberately silent."""
    _patch_code(monkeypatch, "pr10_nowcast_mom", "PENDING — 2/6", CHANGED)
    prereg.run_all(conn)
    msgs = _alerts(conn)
    assert len(msgs) == 1
    assert msgs[0].startswith("PREREG-CODE pr10_nowcast_mom: CODE CHANGED")
    assert "deadbeef" in msgs[0]
    assert [r["level"] for r in conn.execute("SELECT level FROM alerts")] == ["warn"]


def test_a_documented_change_and_an_unchanged_file_both_stay_quiet(conn, monkeypatch):
    for code in ({"code_changed_since_registration": False,
                  "change_is_documented": True},
                 {"code_changed_since_registration": True,
                  "change_is_documented": True},
                 {}):
        _patch_code(monkeypatch, "pr10_nowcast_mom", "PENDING — 2/6", code)
        prereg.run_all(conn)
    assert _alerts(conn) == []


def test_the_code_alert_cannot_retrigger_the_verdict_alert(conn, monkeypatch):
    """Shared prefixes would make the two channels edge-trigger each other: verdict,
    then code, then the SAME verdict again because the newest row no longer matches."""
    _patch_code(monkeypatch, "pr7_s2", "FAIL — rule removed", CHANGED)
    prereg.run_all(conn)
    prereg.run_all(conn)                       # next week, nothing changed
    prereg.run_all(conn)
    msgs = _alerts(conn)
    assert sum(m.startswith("PREREG pr7_s2:") for m in msgs) == 1
    assert sum(m.startswith("PREREG-CODE pr7_s2:") for m in msgs) == 1
