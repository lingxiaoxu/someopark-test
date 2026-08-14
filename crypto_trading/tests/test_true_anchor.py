"""True-anchor research wiring tests — synthetic, no network."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_strategies.liq_reversion import widened as w


def synth_stats(n=400):
    idx = pd.date_range("2026-07-20", periods=n, freq="10s", tz="UTC")
    return pd.DataFrame({"bid": 6.40, "ask": 6.41, "price": 6.405,
                         "contract_size": 1e-4}, index=idx)


def test_anchor_prefers_composite_when_available(monkeypatch):
    idx = pd.date_range("2026-07-20", periods=100, freq="1min", tz="UTC")
    fake = pd.DataFrame({"vw_close": np.full(100, 64000.0)}, index=idx)
    monkeypatch.setattr(w, "load_index_composite", lambda a: fake)
    anchor, kind = w.anchor_for("KXSOLPERP", synth_stats(), mode="auto")
    assert kind == "composite" and anchor.iloc[0] == 64000.0


def test_anchor_self_median_forced(monkeypatch):
    # even with a composite available, mode=self_median must use the fallback
    idx = pd.date_range("2026-07-20", periods=100, freq="1min", tz="UTC")
    monkeypatch.setattr(w, "load_index_composite",
                        lambda a: pd.DataFrame({"vw_close": np.full(100, 64000.0)}, index=idx))
    anchor, kind = w.anchor_for("KXSOLPERP", synth_stats(), mode="self_median")
    assert kind == "self_median"
    assert abs(anchor.dropna().iloc[-1] - 64050.0) < 1.0   # 6.405/1e-4


def test_anchor_composite_mode_raises_when_missing(monkeypatch):
    monkeypatch.setattr(w, "load_index_composite",
                        lambda a: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(FileNotFoundError):
        w.anchor_for("KXSOLPERP", synth_stats(), mode="composite")


def test_ticker_asset_map_covers_all_composite_assets():
    # every mapped asset must be a real spot symbol (SHIB-style names excluded)
    assert w.COMPOSITE_ASSET["KXSOLPERP"] == "SOL"
    assert w.COMPOSITE_ASSET["KXBCHPERP"] == "BCH"
    assert "KXKSHIBPERP" not in w.COMPOSITE_ASSET    # 1000-SHIB unit ≠ spot SHIB
    assert "KXHYPEPERP" not in w.COMPOSITE_ASSET     # no US spot venue
