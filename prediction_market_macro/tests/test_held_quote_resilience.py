"""2026-09-14: one leg missed a quote round and the whole panel read "unpriceable".

The mechanism (_maintain_positions re-quotes every held leg each 900s pass) was already
right; what was missing was a retry inside the pass and any visibility when it failed.
"""
import sqlite3

from prediction_market_macro.ingest import kalshi_md
from prediction_market_macro.jobs import tick


class FakeMD:
    """snapshot_tickers that fails `fail_first` on the first call only."""

    def __init__(self, fail_first):
        self.fail_first = set(fail_first)
        self.calls = []

    def snapshot_tickers(self, tickers, *, before_each=None):
        tickers = sorted(tickers)
        self.calls.append(tickers)
        bad = self.fail_first if len(self.calls) == 1 else set()
        return {"refreshed": [t for t in tickers if t not in bad],
                "failed": {t: "timeout" for t in tickers if t in bad}}


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,"
                 " level TEXT, source TEXT, message TEXT, acked INTEGER NOT NULL DEFAULT 0)")
    return conn


def _run(monkeypatch, md, conn):
    """Drive _maintain_positions with two held legs, stubbing everything downstream."""
    import types
    pos = [{"id": 1, "series": "KXNATGASW", "ts_utc": "t",
            "fills": [{"ticker": "A"}, {"ticker": "B"}]}]
    fake = types.SimpleNamespace(
        exits=types.SimpleNamespace(shadow_run=lambda *a, **k: None, run=lambda *a, **k: None),
        ledger=types.SimpleNamespace(open_positions=lambda c: pos),
        pnl=types.SimpleNamespace(mark_all=lambda c: 0),
        predict_all=types.SimpleNamespace(run=lambda *a, **k: None),
        trading_kalshi=types.SimpleNamespace(sync=lambda c: None))
    import prediction_market_macro.ops as ops_pkg
    for name, mod in vars(fake).items():
        monkeypatch.setattr(ops_pkg, name, mod, raising=False)
    monkeypatch.setattr(tick, "_drain_freezes", lambda c: None)
    monkeypatch.setattr(tick, "_export_frontend", lambda c, s: None)
    tick._maintain_positions(conn, object(), md)


def test_a_transient_quote_failure_is_retried_inside_the_same_pass(monkeypatch):
    conn, md = _db(), FakeMD({"B"})
    _run(monkeypatch, md, conn)
    assert md.calls == [["A", "B"], ["B"]], "the failed leg must be retried immediately"
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0, \
        "a recovered leg is not worth an alert"


def test_a_leg_that_fails_twice_raises_an_alert_naming_it(monkeypatch):
    class AlwaysFails(FakeMD):
        def snapshot_tickers(self, tickers, *, before_each=None):
            self.calls.append(sorted(tickers))
            return {"refreshed": [], "failed": {t: "timeout" for t in sorted(tickers)}}

    conn, md = _db(), AlwaysFails(set())
    _run(monkeypatch, md, conn)
    assert len(md.calls) == 2
    row = conn.execute("SELECT level, source, message FROM alerts").fetchone()
    assert row["level"] == "warn" and row["source"] == "quotes"
    assert row["message"].startswith("held_leg_requote_failed:") and "A" in row["message"]


def test_held_leg_requests_get_a_retry_and_a_longer_timeout():
    """tries=1/timeout=5 was one hiccup away from an unpriceable panel."""
    assert kalshi_md._HELD_TRIES >= 2
    assert kalshi_md._HELD_TIMEOUT >= 8
    import inspect
    src = inspect.getsource(kalshi_md.KalshiMD.snapshot_tickers)
    assert "tries=_HELD_TRIES" in src and "timeout=_HELD_TIMEOUT" in src
