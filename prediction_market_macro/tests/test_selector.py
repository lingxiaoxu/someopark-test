"""ML selector accounting — regression for the bucket eff-vs-outlay bug.

First selector run reported 39W-1L / +3686% ROI because a bucket structure's
`cost` is the EFFECTIVE price (legs sum − 1) while its cash outlay is 1+eff and
one leg pays $1 even on a miss: labelling wins as `payoff > cost` made every
bucket a "win". These tests pin the honest cash semantics (mirrors
walkforward._settle_struct).
"""
from prediction_market_macro.research.selector import settle_cash
from prediction_market_macro.strategy.edge import taker_fee


def test_bucket_miss_is_a_loss():
    # bucket: YES lo @0.55 + NO hi @0.47 → eff cost 0.02, outlay 1.02
    # miss → exactly one leg pays $1 → cash ≈ −0.02 − fees, NOT +0.98
    cash = settle_cash(payoff=1.0, leg_prices=[0.55, 0.47], count=1)
    assert cash < 0


def test_bucket_hit_wins_about_one_minus_eff():
    cash = settle_cash(payoff=2.0, leg_prices=[0.55, 0.47], count=1)
    fees = taker_fee(0.55, 1) + taker_fee(0.47, 1)
    assert abs(cash - (0.98 - fees)) < 1e-9
    assert cash > 0.9


def test_single_leg_matches_walkforward_semantics():
    # single YES @0.30, wins: (1 − 0.30) − fee
    cash = settle_cash(payoff=1.0, leg_prices=[0.30], count=3)
    assert abs(cash - ((1.0 - 0.30) * 3 - taker_fee(0.30, 3))) < 1e-9
    # loses: −0.30·count − fee
    cash = settle_cash(payoff=0.0, leg_prices=[0.30], count=3)
    assert cash < -0.9


def test_selector_experiment_row_shape(tmp_path, monkeypatch):
    # walkforward_eval stores under experiments('ml_selector', ...) — the
    # frontend export reads name='ml_selector' → key 'ml'; keep in lockstep
    import json
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from prediction_market_macro.ingest.store import init_db
    from prediction_market_macro.research import selector
    from prediction_market_macro.ops import frontend_export
    conn = init_db(tmp_path / "selector.db")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(selector, "build_dataset", lambda *args: [{
        "series": "KXCPI", "period": "2026-08", "close": now-timedelta(days=1),
        "entry_day": (now-timedelta(days=2)).date().isoformat(), "desc": "YES T0.2",
        "lead_days": 1, "cost": .8, "outlay": .8, "payoff": 1,
        "leg_prices": [.8], "x": [.7], "y": 1,
    }])
    result = selector.walkforward_eval(conn)
    settings = SimpleNamespace(output_dir=tmp_path, frontend_data=tmp_path)
    frontend_export.run_extended(conn, settings)
    exported = json.loads((tmp_path / "macro_walkforward.json").read_text())
    assert exported["ml"] == result
    assert exported["ml_ts"] == conn.execute(
        "SELECT created_ts FROM experiments WHERE name='ml_selector'").fetchone()[0]
    conn.close()
