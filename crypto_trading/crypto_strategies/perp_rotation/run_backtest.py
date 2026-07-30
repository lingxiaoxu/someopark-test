"""Plan 05 backtest runner (smoke + research CLI).

Builds the panels (Kalshi by default; ``--proxy`` = offshore OKX panel with
730d history, labeled proxy — factor prototyping ONLY, plan §8), derives the
regime-input frame, and runs the daily engine end-to-end.

Regime inputs built here:
  * btc_rvol          — 30d realized vol of the BTC leg (365-annualized, %)
  * funding           — BTC per-cycle funding (daily sum / 3 cycles)
  * basis_dispersion  — NEUTRAL constant 30 bps until the recorded Kalshi
                        basis frames accumulate (between the tight/wide
                        brackets → contributes zero regime score; documented
                        stand-in, not a measurement)
  * btc_dominance     — from refdata/onchain daily csv when present

CLI (from repo root):
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.perp_rotation.run_backtest
        [--proxy] [--fees projected|zero] [--start ...] [--end ...] [--weekly]
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd
import yaml

from crypto_trading.crypto_common.backtest.daily_engine import PerpRotationBacktest
from crypto_trading.crypto_common.config import PRICE_DATA
from crypto_trading.crypto_common.regime import realized_vol
from crypto_trading.crypto_strategies.perp_rotation.data.loader import (build_funding_panel,
                                                                        build_perp_panel,
                                                                        proxy_panel)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"
NEUTRAL_BASIS_DISPERSION_BPS = 30.0


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def build_regime_inputs(prices: pd.DataFrame, funding: pd.DataFrame,
                        btc_col: str = "KXBTCPERP") -> pd.DataFrame:
    frame = pd.DataFrame(index=prices.index)
    if btc_col in prices.columns:
        frame["btc_rvol"] = realized_vol(prices[btc_col], window=30)
    else:
        frame["btc_rvol"] = realized_vol(prices.iloc[:, 0], window=30)
    if btc_col in funding.columns:
        frame["funding"] = funding[btc_col] / 3.0     # daily sum → per-cycle approx
    else:
        frame["funding"] = 0.0
    frame["basis_dispersion"] = NEUTRAL_BASIS_DISPERSION_BPS
    dom_path = PRICE_DATA / "regime" / "btc_dominance.csv"
    if dom_path.exists():
        dom = pd.read_csv(dom_path, parse_dates=["date"])
        dom_s = pd.Series(dom.btc_dominance_pct.values,
                          index=pd.DatetimeIndex(dom.date).tz_localize("UTC"))
        frame["btc_dominance"] = dom_s.reindex(frame.index, method="ffill")
    # PIT: every column is measured WITH day-T information (rvol includes
    # r(T); funding settles during T) yet is consumed at the day-T rebalance
    # before r(T) accrues — the emergency/progressive de-risk would otherwise
    # dodge the very crash bar that triggers it. Lag one day.
    return frame.shift(1)


# ── walk-forward wiring (Plan 05 §8) ─────────────────────────────────────────
WF_PARAM_SETS: dict[str, dict] = {
    f"top{n}_{freq}_lev{lv}": {"top_n": n, "frequency": freq, "leverage_max": lv}
    for n in (3, 4, 6)
    for freq in ("daily", "weekly")
    for lv in (1.0, 2.0)
}   # 12 sets


def _relax_short_history(cfg: dict, n_days: int) -> None:
    """Same short-history relaxation main() applies (scaffold-grade < 60 days)."""
    if n_days >= 60:
        return
    cfg.setdefault("universe", {})["listing_history_floor_days"] = max(5, n_days // 3)
    cfg.setdefault("portfolio", {}).setdefault("cov", {})["min_periods"] = max(10, n_days // 3)
    sig = cfg.setdefault("signals", {})
    sig["cs_lookback"] = min(sig.get("cs_lookback", 30), max(5, n_days // 3))
    sig["ts_lookback"] = min(sig.get("ts_lookback", 30), max(5, n_days // 3))
    sig["cs_zscore_window"] = 0
    sig["carry_lookback_days"] = min(sig.get("carry_lookback_days", 90), max(10, n_days // 2))


def wf_run_backtest(params: dict, start, end) -> dict:
    """WF engine on the Kalshi perp panel → {"equity_curve", +benchmark cols}."""
    cfg = load_config()
    cfg.setdefault("costs", {})["fee_scenario"] = params.get("fee_scenario", "projected")
    if "top_n" in params:
        cfg.setdefault("portfolio", {})["top_n"] = int(params["top_n"])
    if "leverage_max" in params:
        cfg.setdefault("portfolio", {})["leverage_max"] = float(params["leverage_max"])
    if "frequency" in params:
        cfg.setdefault("rebalance", {})["frequency"] = params["frequency"]
    prices, volumes, oi = build_perp_panel(start=str(start), end=str(end))
    funding = build_funding_panel(start=str(start), end=str(end))
    _relax_short_history(cfg, len(prices))
    regime = build_regime_inputs(prices, funding)
    result = PerpRotationBacktest(cfg).run(prices, funding, regime,
                                           volumes_notional=volumes, oi_notional=oi,
                                           start=str(start), end=str(end))
    out = {"equity_curve": result.equity_curve}
    if result.benchmark_equity is not None:
        out["btc_hodl_equity"] = result.benchmark_equity
    if getattr(result, "benchmark_ew_equity", None) is not None:
        out["ew_basket_equity"] = result.benchmark_ew_equity
    return out


def wf_prices() -> pd.DataFrame:
    """Daily reference frame defining the fold grid (the Kalshi perp panel index)."""
    prices, _, _ = build_perp_panel()
    return prices


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--proxy", action="store_true",
                    help="use the offshore OKX panel (730d, labeled proxy)")
    ap.add_argument("--fees", default=None, choices=[None, "zero", "projected"])
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--weekly", action="store_true", help="weekly rebalance")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config()
    if args.fees:
        cfg.setdefault("costs", {})["fee_scenario"] = args.fees
    if args.weekly:
        cfg.setdefault("rebalance", {})["frequency"] = "weekly"

    if args.proxy:
        prices, volumes, funding = proxy_panel(start=args.start, end=args.end)
        oi = None
        cfg["_panel_source"] = str(prices.attrs.get("source", "proxy"))
        # proxy panel has 5 deep names — drop the activation complaint to gate=5
        cfg.setdefault("universe", {})["min_perps_to_activate"] = min(
            cfg.get("universe", {}).get("min_perps_to_activate", 6), len(prices.columns))
        # proxy history predates Kalshi listings — the floor applies to DATA days
    else:
        prices, volumes, oi = build_perp_panel(start=args.start, end=args.end)
        funding = build_funding_panel(start=args.start, end=args.end)
        cfg["_panel_source"] = "kalshi"
        # ~1 month of history: soften the floor so the smoke can run (plan §8
        # history caveat — results are scaffold-grade until history accumulates)
        n_days = len(prices)
        if n_days < 60:
            cfg.setdefault("universe", {})["listing_history_floor_days"] = max(5, n_days // 3)
            cfg.setdefault("portfolio", {}).setdefault("cov", {})["min_periods"] = max(
                10, n_days // 3)
            # signal windows must fit inside the available history or the
            # composite is all-NaN and the engine never trades
            sig = cfg.setdefault("signals", {})
            sig["cs_lookback"] = min(sig.get("cs_lookback", 30), max(5, n_days // 3))
            sig["ts_lookback"] = min(sig.get("ts_lookback", 30), max(5, n_days // 3))
            sig["cs_zscore_window"] = 0        # disable rolling z-norm (template supports 0)
            sig["carry_lookback_days"] = min(sig.get("carry_lookback_days", 90),
                                             max(10, n_days // 2))
            logger.warning("short Kalshi history (%d days): floor/cov/signal windows "
                           "relaxed — scaffold-grade results only", n_days)

    regime_inputs = build_regime_inputs(prices, funding)

    engine = PerpRotationBacktest(cfg)
    result = engine.run(prices, funding, regime_inputs,
                        volumes_notional=volumes, oi_notional=oi,
                        start=args.start, end=args.end)
    print(result.summary())
    if result.stop_loss_events:
        print(f"stop-loss events: {len(result.stop_loss_events)}")
    if len(result.weights_history):
        last_w = result.weights_history.iloc[-1].sort_values(ascending=False)
        print("\nlatest weights:")
        print(last_w[last_w > 0].round(4).to_string())
    else:
        print("\n(no rebalances executed — history shorter than signal warmup)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
