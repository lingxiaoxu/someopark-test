"""Plan 04 production-cell tests — frozen config, live_signal dry-run, OKX flag.
Synthetic tape only; no network. Mirrors test_liq_fill_aware's cascade builder."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common import bracket_watcher as bw
from crypto_trading.crypto_common import execution as ex
from crypto_trading.crypto_common import risk_kill as rk
from crypto_trading.crypto_common.bracket import BracketMonitor
from crypto_trading.crypto_common.execution import ExecutionRouter
from crypto_trading.crypto_strategies.liq_reversion import live_signal as ls
from crypto_trading.crypto_strategies.liq_reversion import strategy as st


# ── frozen config ────────────────────────────────────────────────────────────

def test_config_frozen_values():
    cfg = ls.load_config()
    assert cfg["universe"] == ["KXBTCPERP", "KXETHPERP"]      # composite-anchored only
    assert cfg["anchor"] == "composite"
    assert cfg["detector"]["overshoot_entry_bps"] == 15.0     # the winning cell
    assert cfg["detector"]["oi_drop_min"] > 0                 # OI signature mandatory
    assert cfg["entry"]["style"] == "maker"                   # taker was worse everywhere
    assert cfg["okx_confirm"]["enabled"] is True
    assert cfg["exits"]["tp_fraction"] == 0.5
    det = ls.detector_from_config(cfg)
    assert det.overshoot_entry_bps == 15.0 and det.oi_drop_min == 0.002


def test_wf_param_sets_centered_on_frozen_cell():
    assert "frozen_os15_oi002_tp05" in st.WF_PARAM_SETS
    frozen = st.WF_PARAM_SETS["frozen_os15_oi002_tp05"]
    assert frozen["overshoot_entry_bps"] == 15.0
    assert frozen["oi_drop_min"] == 0.002
    assert frozen["tp_fraction"] == 0.5
    assert len(st.WF_PARAM_SETS) == 8                         # DSR trial count


# ── synthetic tape (cascade ACTIVE at the end = live trigger) ────────────────

def _grid(n, sec=10):
    # recent-ish but fixed date; staleness is bypassed via allow_stale/cfg
    return pd.date_range("2026-07-25T00:00:00Z", periods=n, freq=f"{sec}s", tz="UTC")


def _tape(active_cascade: bool):
    """Cascade in the LAST 5 bars with a DECAYING sell burst + progressive OI
    decline, so the final bar is stretched + OI-dropping + one-sided + FADING —
    i.e. an ACTIVE entry-eligible event at 'now'."""
    n = 260
    g = _grid(n)
    csize = 1e-4
    index_u = np.full(n, 64000.0)
    mark_u = np.full(n, 64000.0)
    oi = np.full(n, 100000.0)
    tr = []
    m = n - 5
    for i in range(m):                                        # calm two-sided baseline
        tr.append((g[i], mark_u[i] * csize, 1.0, "bid"))
        tr.append((g[i], mark_u[i] * csize, 1.0, "ask"))
    if active_cascade:
        mark_u[m:] = 63750.0                                  # ~ -39bps overshoot held
        oi[m:] = [99800.0, 99500.0, 99200.0, 99000.0, 98900.0]   # progressive OI drop
        sell_sizes = [100.0, 80.0, 40.0, 20.0, 10.0]          # burst DECAYS → fading
        for j, sz in enumerate(sell_sizes):
            tr.append((g[m + j], mark_u[m + j] * csize - 0.0008, sz, "ask"))
    else:
        for i in range(m, n):
            tr.append((g[i], mark_u[i] * csize, 1.0, "bid"))
            tr.append((g[i], mark_u[i] * csize, 1.0, "ask"))
    mark_c = mark_u * csize
    stats = pd.DataFrame({"bid": mark_c - 0.0008, "ask": mark_c + 0.0008,
                          "price": mark_c, "oi": oi, "contract_size": csize}, index=g)
    trades = pd.DataFrame({"price": [t[1] for t in tr], "count": [t[2] for t in tr],
                           "taker_side": [t[3] for t in tr]},
                          index=pd.DatetimeIndex([t[0] for t in tr])).sort_index()
    index = pd.Series(index_u, index=g)
    return stats, trades, index


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "SIGNALS_DIR", tmp_path / "signals")
    monkeypatch.setattr(bw, "SIGNALS_DIR", tmp_path / "signals")
    monkeypatch.setattr(ls, "SIGNALS_DIR", tmp_path / "signals")
    monkeypatch.setattr(rk, "STATE_DIR", tmp_path / "state")
    return tmp_path


def _install(monkeypatch, stats, trades, index, *, liq_times=None, subaccount=0):
    monkeypatch.setattr(ls, "load_poll_market_stats", lambda *a, **k: stats)
    monkeypatch.setattr(ls, "load_poll_trades", lambda *a, **k: trades)
    monkeypatch.setattr(ls, "_index_series", lambda asset, **k: (index, "composite"))
    monkeypatch.setattr(ls, "load_okx_liq_times",
                        lambda sym: liq_times if liq_times is not None
                        else pd.DatetimeIndex([]))
    base = ls.load_config()
    base["subaccount"] = subaccount
    base["risk"]["staleness_max_min"] = 10_000_000            # synthetic tape is old
    monkeypatch.setattr(ls, "load_config", lambda: base)


def test_live_signal_fires_dry_run_on_active_cascade(wired, monkeypatch):
    stats, trades, index = _tape(active_cascade=True)
    _install(monkeypatch, stats, trades, index, subaccount=64)
    r = ls.run_live_signal(ticker="KXBTCPERP")
    assert r["status"] == "cascade_signal"
    assert r["direction"] == 1                                # fade the down-overshoot
    assert r["order"] == "dry_run"                            # gate keeps it inert
    assert r["bracket"]["armed"] is True
    # bracket persisted with the CONFIG subaccount (the position's home)
    router = ExecutionRouter("t", env="demo")
    b = BracketMonitor(router, state_path=bw.state_path("liq_reversion")).active()["KXBTCPERP"]
    assert b.subaccount == 64
    assert b.side == "bid" and b.take_profit > b.entry_price > b.stop_loss
    router.close()


def test_live_signal_quiet_on_calm_tape(wired, monkeypatch):
    stats, trades, index = _tape(active_cascade=False)
    _install(monkeypatch, stats, trades, index)
    r = ls.run_live_signal(ticker="KXBTCPERP")
    assert r["status"] == "no_cascade"
    assert "order" not in r


def test_okx_confirmation_flag(wired, monkeypatch):
    stats, trades, index = _tape(active_cascade=True)
    ev_time = stats.index[-4]                                 # near the cascade bars
    _install(monkeypatch, stats, trades, index,
             liq_times=pd.DatetimeIndex([ev_time]))
    r = ls.run_live_signal(ticker="KXBTCPERP")
    assert r["status"] == "cascade_signal" and r["okx_confirmed"] is True

    _install(monkeypatch, stats, trades, index, liq_times=pd.DatetimeIndex([]))
    r2 = ls.run_live_signal(ticker="KXBTCPERP")
    assert r2["status"] == "cascade_signal" and r2["okx_confirmed"] is False


def test_stale_tape_refuses_without_allow(wired, monkeypatch):
    stats, trades, index = _tape(active_cascade=True)
    _install(monkeypatch, stats, trades, index)
    cfg = ls.load_config()
    cfg["risk"]["staleness_max_min"] = 10                     # synthetic tape IS stale
    monkeypatch.setattr(ls, "load_config", lambda: cfg)
    r = ls.run_live_signal(ticker="KXBTCPERP")
    assert r["status"] == "stale_tape"
    r2 = ls.run_live_signal(ticker="KXBTCPERP", allow_stale=True)
    assert r2["status"] == "cascade_signal"                   # diagnostics path
