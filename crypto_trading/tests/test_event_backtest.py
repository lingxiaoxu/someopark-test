"""Plan 02 event×perp preliminary-backtest tests — synthetic strips, no network."""
import math

import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_strategies.event_perp import backtest as bt


# ── synthetic strip builders ────────────────────────────────────────────────

def _between(k_lo, k_hi, yes_bid, yes_ask, size=100.0):
    return {"strike_type": "between", "floor_strike": k_lo, "cap_strike": k_hi,
            "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}",
            "yes_bid_size_fp": f"{size:.2f}", "yes_ask_size_fp": f"{size:.2f}"}


def _threshold(kind, k, yes_bid, yes_ask, size=100.0):
    m = {"strike_type": kind,
         "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}",
         "yes_bid_size_fp": f"{size:.2f}", "yes_ask_size_fp": f"{size:.2f}"}
    if kind == "greater":
        m["floor_strike"] = k
    else:
        m["cap_strike"] = k
    return m


def _gaussian_partition(mean, sd, lo, hi, step, spread, *, mispriced=False):
    """A clean, coherent bin partition summing to ~1 (a Gaussian), quoted with a
    symmetric spread. If mispriced, inflate every mid so the tile sum >> 1
    (a fee-positive SELL arb should then appear)."""
    edges = np.arange(lo, hi + step, step)
    markets = []
    scale = 1.6 if mispriced else 1.0
    for a, b in zip(edges[:-1], edges[1:]):
        # P(a ≤ S < b) under Normal(mean, sd)
        p = 0.5 * (math.erf((b - mean) / (sd * 2 ** 0.5))
                   - math.erf((a - mean) / (sd * 2 ** 0.5)))
        mid = min(0.99, p * scale)
        markets.append(_between(a, b, max(0.0, mid - spread / 2), min(1.0, mid + spread / 2)))
    # tails as threshold survival quotes
    surv_lo = 0.5 * (1 - math.erf((lo - mean) / (sd * 2 ** 0.5)))       # P(S ≥ lo)
    surv_hi = 0.5 * (1 - math.erf((hi - mean) / (sd * 2 ** 0.5)))       # P(S ≥ hi)
    markets.append(_threshold("greater", lo, max(0.0, surv_lo - spread / 2),
                              min(1.0, surv_lo + spread / 2)))
    markets.append(_threshold("greater", hi, max(0.0, surv_hi - spread / 2),
                              min(1.0, surv_hi + spread / 2)))
    return markets


# ── static-arb tests ────────────────────────────────────────────────────────

def test_clean_partition_has_no_fee_positive_arb(monkeypatch):
    rec = {"recv_ts": 1.0, "spot_est": 60000.0,
           "markets": _gaussian_partition(60000, 2000, 54000, 66000, 1000, 0.02)}
    monkeypatch.setattr(bt, "read_snapshots", lambda series, **kw: iter([rec]))
    r = bt.run_static_arb("KXBTC")
    assert r.n_snapshots == 1 and r.n_tile_evaluated == 1
    assert r.n_tile_buy_arb == 0 and r.n_tile_sell_arb == 0
    assert r.n_pair_violations == 0
    assert r.captured_credit == 0.0


def test_pairwise_monotonicity_violation_is_captured(monkeypatch):
    # two threshold quotes that CROSS: higher strike bid above lower strike ask
    markets = [_threshold("greater", 60000, 0.30, 0.35),
               _threshold("greater", 62000, 0.80, 0.85)]   # bid(62k)=0.80 > ask(60k)=0.35
    rec = {"recv_ts": 1.0, "spot_est": 60000.0, "markets": markets}
    monkeypatch.setattr(bt, "read_snapshots", lambda series, **kw: iter([rec]))
    r = bt.run_static_arb("KXBTC", fee_rate=0.0)      # no fees → clean arb
    assert r.n_pair_violations == 1
    # captured credit = net_credit(0.45) × min size(100, capped 50) = 22.5
    assert r.captured_credit == pytest.approx(0.45 * 50.0, abs=1e-6)


def test_mispriced_tile_flags_sell_arb(monkeypatch):
    rec = {"recv_ts": 1.0, "spot_est": 60000.0,
           "markets": _gaussian_partition(60000, 2000, 54000, 66000, 1000, 0.02,
                                          mispriced=True)}
    monkeypatch.setattr(bt, "read_snapshots", lambda series, **kw: iter([rec]))
    r = bt.run_static_arb("KXBTC", fee_rate=0.0)
    # inflated mids → Σbid > 1 → sell-the-tile arb when the partition is complete
    if r.n_tile_complete:
        assert r.n_tile_sell_arb >= 1 and r.captured_credit > 0
    assert r.best_sell_credit_net > 0


def test_high_fee_kills_marginal_arb(monkeypatch):
    markets = [_threshold("greater", 60000, 0.30, 0.35),
               _threshold("greater", 62000, 0.40, 0.45)]   # gross 0.05, thin
    rec = {"recv_ts": 1.0, "spot_est": 60000.0, "markets": markets}
    monkeypatch.setattr(bt, "read_snapshots", lambda series, **kw: iter([rec]))
    assert bt.run_static_arb("KXBTC", fee_rate=0.0).n_pair_violations == 1
    assert bt.run_static_arb("KXBTC", fee_rate=5.0).n_pair_violations == 0


# ── dislocation IC tests ────────────────────────────────────────────────────

def test_dislocation_ic_positive_on_mean_reverting_gap(monkeypatch):
    """Construct snapshots where the implied_mean/perp gap mean-reverts: perp
    lags and then converges to a stable implied_mean → positive IC by design."""
    rng = np.random.default_rng(0)
    mean = 60000.0
    recs = []
    perp = mean
    for i in range(200):
        # implied distribution centered at a stable mean (small noise)
        m = mean + rng.normal(0, 30)
        markets = _gaussian_partition(m, 2000, 54000, 66000, 1000, 0.02)
        # perp is pulled toward the implied mean but lags (AR(1) reversion)
        perp += 0.25 * (m - perp) + rng.normal(0, 20)
        recs.append({"recv_ts": float(i * 60), "spot_est": perp, "markets": markets,
                     "close_time": "2026-07-09T06:00:00Z"})    # single horizon
    monkeypatch.setattr(bt, "read_snapshots", lambda series, **kw: iter(recs))
    d = bt.run_dislocation_ic("KXBTC", fwd=3, zwin=40, use_poll_fallback=False)
    assert d["n_snapshots_used"] > 50
    assert d["IC_spearman_gapz_vs_fwd_convergence"] > 0.1   # reversion is predictive
    assert "positive" in d["IC_sign"]


def test_dislocation_insufficient_sample_returns_note(monkeypatch):
    recs = [{"recv_ts": float(i), "spot_est": 60000.0,
             "markets": _gaussian_partition(60000, 2000, 54000, 66000, 1000, 0.02),
             "close_time": "2026-07-09T06:00:00Z"}
            for i in range(5)]
    monkeypatch.setattr(bt, "read_snapshots", lambda series, **kw: iter(recs))
    d = bt.run_dislocation_ic("KXBTC", fwd=3, zwin=40, use_poll_fallback=False)
    assert d["n"] == 5 and "insufficient" in d["note"]


def test_run_orchestration_shape(monkeypatch):
    rec = {"recv_ts": 1.0, "spot_est": 60000.0,
           "markets": _gaussian_partition(60000, 2000, 54000, 66000, 1000, 0.02)}
    monkeypatch.setattr(bt, "read_snapshots", lambda series, **kw: iter([rec]))
    res = bt.run(["KXBTC"], fee_rate=0.07)
    assert res["PRELIMINARY"] is True
    assert "static_arb" in res["series"]["KXBTC"]
    assert "dislocation" in res["series"]["KXBTC"]
