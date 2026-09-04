"""The health check must FAIL on the defects it exists to catch.

A monitor that cannot go red is decoration. Each test below breaks exactly one
invariant — every one of them a defect that actually shipped in W7 v2/v3 during
2026-08-27..09-01 — and asserts the checker notices.
"""
from __future__ import annotations

import pytest

from crypto_trading.ops import w7_health as H


def _trade(ticker, cost, pnl_c=1.0, **kw):
    return {"ticker": ticker, "cost": cost, "pnl_c": pnl_c, "win": pnl_c > 0, **kw}


def _healthy_state():
    """Two booked trades (one inside the pre-registered primary cell), one
    observation-leg trade, one open position — all identities holding."""
    tr = [_trade("KXBTC15M-A", 0.90, +10.0), _trade("KXETH15M-B", 0.70, -70.0)]
    return {
        "trades": tr,
        "obs_trades": [_trade("KXSOL15M-C", 0.55, -45.0)],
        "positions": {"KXXRP15M-D": {"cost": 0.80, "close": "2026-09-02T12:00:00Z"}},
        "windows": {"2026-09-02T11:00:00Z": {"n": 2, "wins": 1, "sum_c": -60.0}},
        "windows_primary": {"2026-09-02T11:00:00Z": {"n": 1, "wins": 1, "sum_c": 10.0}},
        "cum_net_usd": -60.0 / 100 * 25,
    }


def test_books_pass_on_a_consistent_state():
    r = H.check_books(_healthy_state())
    assert r["status"] == H.PASS, r["detail"]


@pytest.mark.parametrize("mutate,expect", [
    # the paper book drifting away from the trades it is made of
    (lambda s: s.update(cum_net_usd=999.0), "cum_net_usd"),
    # window book no longer equals the trade book (a settlement routed wrong)
    (lambda s: s["windows"].update({"X": {"n": 1, "wins": 0, "sum_c": -500.0}}),
     "window book"),
    # primary-cell membership drifting from the frozen [0.85,0.98] definition —
    # this is the verdict's own denominator, so it must never be approximate
    (lambda s: s["windows_primary"].update({"Y": {"n": 3, "wins": 0, "sum_c": 0.0}}),
     "primary window count"),
    # v2's real defect: a fill priced outside the frozen band still booked
    (lambda s: s["trades"].append(_trade("KXBTC15M-OOB", 0.17, -17.0)),
     "outside"),
    # the observation leg must stay OUT of the books (it is the FLB's negative
    # print, not a bet)
    (lambda s: s["trades"].append(_trade("KXSOL15M-C", 0.55, -45.0)),
     "observation leg leaked"),
    # the same window settled twice would double-count evidence
    (lambda s: s["trades"].append(_trade("KXBTC15M-A", 0.90, +10.0)),
     "duplicate"),
    # settled and still open at once = a settlement that failed to close out
    (lambda s: s["positions"].update({"KXBTC15M-A": {"cost": 0.9}}),
     "both settled and open"),
])
def test_books_fail_on_each_broken_invariant(mutate, expect):
    st = _healthy_state()
    mutate(st)
    r = H.check_books(st)
    assert r["status"] == H.FAIL and expect in r["detail"], r["detail"]


def test_criteria_reports_progress_and_surfaces_a_kill():
    st = _healthy_state()
    r = H.check_criteria(st)
    assert r["status"] == H.PASS and r["primary_windows"] == 1
    st["killed"] = True
    st["killed_reason"] = "evidence: PRIMARY window t=-2.40"
    r = H.check_criteria(st)
    assert r["status"] == H.WARN and "KILLED" in r["detail"]


def test_mirror_fails_on_wrong_window_and_on_409(tmp_path, monkeypatch):
    """The 2026-09-01 defect: the mirror mapped a 15-minute window onto another
    (often already closed) market — HTTP 409, or a silent bet on a different
    close. Both must turn the check red; a clean run must not."""
    monkeypatch.setattr(H, "LOG_DIR", tmp_path)
    now = 1_788_000_000.0
    import time as _t
    ts = _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime(now - 600))

    def write(rows):
        (tmp_path / "log_2026-09-01.jsonl").write_text(
            "\n".join(__import__("json").dumps(r) for r in rows))

    base = {"strategy": "w7_noisefade", "action": "demo_mirror_result", "ts": ts}
    write([{**base, "status": "sent", "status_code": 201,
            "prod_close": "2026-09-01T21:15:00Z", "mapped_close": "2026-09-01T21:15:00Z"},
           {**base, "status": "no_demo_market"}])
    assert H.check_mirror(now)["status"] == H.PASS

    write([{**base, "status": "sent", "status_code": 409,
            "prod_close": "2026-09-01T21:15:00Z", "mapped_close": "2026-09-01T20:30:00Z"}])
    r = H.check_mirror(now)
    assert r["status"] == H.FAIL
    assert "409" in r["detail"] and "different close" in r["detail"]

    # A venue 5xx on the RIGHT window is the venue's problem, not ours: it must
    # not turn the light red, or the red light stops meaning anything. Measured
    # 2026-09-02, when one 503 out of 23 sends failed the whole check.
    ok = {**base, "status": "sent", "status_code": 201,
          "prod_close": "2026-09-01T21:15:00Z", "mapped_close": "2026-09-01T21:15:00Z"}
    write([ok] * 22 + [{**ok, "status_code": 503}])
    r = H.check_mirror(now)
    assert r["status"] == H.WARN and "transient" in r["detail"]
    # ...but a steady drip is an outage we would otherwise paper over
    write([ok] * 5 + [{**ok, "status_code": 503}] * 5)
    assert H.check_mirror(now)["status"] == H.FAIL


def test_run_aggregates_worst_status(tmp_path, monkeypatch):
    """Overall status is the worst of the parts — a green summary must be
    impossible while any single check is red."""
    import json

    state = tmp_path / "w7_noisefade_state.json"
    state.write_text(json.dumps(_healthy_state()))
    monkeypatch.setattr(H, "STATE", state)
    monkeypatch.setattr(H, "check_daemon", lambda now: {"status": H.PASS, "detail": ""})
    monkeypatch.setattr(H, "check_errors", lambda *a, **k: {"status": H.PASS, "detail": ""})
    monkeypatch.setattr(H, "check_mirror", lambda *a, **k: {"status": H.WARN, "detail": ""})
    monkeypatch.setattr(H, "check_recorders", lambda now: {"status": H.PASS, "detail": ""})
    monkeypatch.setattr(H, "check_disk", lambda: {"status": H.PASS, "detail": ""})
    assert H.run(now=1_788_000_000.0)["overall"] == H.WARN
    monkeypatch.setattr(H, "check_recorders",
                        lambda now: {"status": H.FAIL, "detail": "tape silent"})
    assert H.run(now=1_788_000_000.0)["overall"] == H.FAIL
    # an unreadable state file must be FAIL, never a silently green summary
    state.write_text("{ not json")
    assert H.run(now=1_788_000_000.0)["checks"]["books"]["status"] == H.FAIL
