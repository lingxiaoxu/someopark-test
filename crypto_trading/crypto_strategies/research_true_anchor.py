"""Research: do TRUE spot composites rescue the alt universe for Plans 01/04?

The earlier alt widening failed hard (n=967, hit 34%, NW-t −13.9) using a
self-median anchor; the diagnosis was "the true spot anchor is load-bearing".
2026-07-26 the alt composites landed (60d Coinbase+Kraken+Bitstamp VWAP for
SOL/XRP/DOGE/LTC/BCH[+LINK/NEAR/SUI when backfilled]). This is the direct test:

  [1] Plan 04 cascade fade per alt, SAME cells, self-median vs composite anchor
      (A/B on identical tapes) + pooled composite-anchor ladder.
  [2] Plan 01 selective, PRE-REGISTERED config (k3.5/abs10/off10/abort20/flowY —
      never re-tuned), run per new market = out-of-sample replication tests.
  [3] Combined portfolio: all Plan-01 + Plan-04(frozen 15bps) trades pooled —
      "are we significant TODAY with the wider true-anchor universe?"

Artifacts: trading_signals/research/true_anchor_<ts>.{json,md}

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.research_true_anchor
"""
from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.loader import (load_index_composite,
                                                 load_poll_market_stats,
                                                 load_poll_trades)
from crypto_trading.crypto_common.trade_stats import (newey_west_tstat,
                                                      trade_significance_report)
from crypto_trading.crypto_strategies.basis_meanrev.improved import (ImprovedParams,
                                                                     prepare, run_config)
from crypto_trading.crypto_strategies.liq_reversion.widened import (COMPOSITE_ASSET,
                                                                    run_widened)

logger = logging.getLogger(__name__)

# markets whose composite exists (checked at runtime)
CANDIDATES = [("KXBTCPERP", "BTC"), ("KXETHPERP", "ETH"), ("KXSOLPERP", "SOL"),
              ("KXXRPPERP", "XRP"), ("KXDOGEPERP", "DOGE"), ("KXLTCPERP", "LTC"),
              ("KXBCHPERP", "BCH"), ("KXLINKPERP", "LINK"), ("KXNEARPERP", "NEAR"),
              ("KXSUIPERP", "SUI")]

# Plan 01 pre-registered config (BTC sweep top cell — NEVER re-tuned here;
# identical to research_conditioning.P01_BEST)
P01_BEST = ImprovedParams(entry_k=3.5, min_abs_bps=10.0, offset_ticks=10,
                          abort_bps=20.0, flow_filter=True, oi_confirm=False)


def available_markets() -> list[tuple[str, str]]:
    out = []
    for tkr, asset in CANDIDATES:
        try:
            if len(load_index_composite(asset)):
                out.append((tkr, asset))
        except FileNotFoundError:
            pass
    return out


def median_spread_bps(stats: pd.DataFrame) -> float:
    m = stats[["bid", "ask"]].dropna()
    mid = (m.bid + m.ask) / 2
    return float((1e4 * (m.ask - m.bid) / mid).median())


def task1_anchor_ab(markets, preloaded) -> dict:
    """Plan 04 A/B: composite vs self-median anchors on identical alt tapes."""
    alts = [t for t, a in markets if t not in ("KXBTCPERP", "KXETHPERP")]
    ab = {}
    for mode in ("composite", "self_median"):
        r = run_widened(tickers=alts, ladder=(10.0, 15.0, 20.0), styles=("maker",),
                        anchor_mode=mode, preloaded=preloaded)
        ab[mode] = r
    rows = []
    for mode, r in ab.items():
        t = r["cells"]
        for _, c in t.iterrows():
            rows.append({"anchor": mode, "ticker": c.ticker, "os_bps": c.os_bps,
                         "trades": c.trades, "net": c.net, "hit": c.hit})
    return {"table": pd.DataFrame(rows), "runs": ab}


def task1_full_pooled(markets, preloaded) -> dict:
    """Composite-anchor ladder across ALL markets with composites (incl BTC/ETH)."""
    tickers = [t for t, _ in markets]
    return run_widened(tickers=tickers, ladder=(10.0, 15.0, 20.0), styles=("maker",),
                       anchor_mode="composite", preloaded=preloaded)


def task2_plan01(markets, preloaded) -> pd.DataFrame:
    rows = []
    trades_all = []
    for tkr, asset in markets:
        try:
            prep = prepare(tkr, asset)
            r = run_config(prep, P01_BEST, ticker=tkr)
        except Exception as e:
            logger.warning("plan01 %s failed: %s", tkr, str(e)[:100])
            continue
        tp = r["trade_pnl"]
        stats = preloaded[tkr][0] if tkr in preloaded else load_poll_market_stats(tkr)
        spread = median_spread_bps(stats)
        n = len(tp)
        nw = newey_west_tstat(tp["net"]) if n >= 5 else None
        rows.append({"ticker": tkr, "n": n,
                     "hit": float((tp.net > 0).mean()) if n else None,
                     "mean": float(tp.net.mean()) if n else None,
                     "net": float(tp.net.sum()) if n else 0.0,
                     "nw_t": nw["t_nw"] if nw else None,
                     "median_spread_bps": round(spread, 1)})
        if n:
            trades_all.append(tp.assign(ticker=tkr))
    table = pd.DataFrame(rows)
    pooled = pd.concat(trades_all).sort_values("entry_ts") if trades_all else pd.DataFrame()
    return table, pooled


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    markets = available_markets()
    logger.info("markets with composites: %s", [t for t, _ in markets])

    # preload tapes once (the expensive part)
    preloaded = {}
    for tkr, _ in markets:
        try:
            preloaded[tkr] = (load_poll_market_stats(tkr),
                              load_poll_trades(tkr).sort_index())
        except Exception as e:
            logger.warning("preload %s failed: %s", tkr, str(e)[:80])

    out = {"markets": [t for t, _ in markets]}

    # [1] anchor A/B on alts + full composite pooled
    ab = task1_anchor_ab(markets, preloaded)
    full = task1_full_pooled(markets, preloaded)
    out["anchor_ab"] = ab["table"].to_dict("records")
    out["plan04_pooled_composite"] = full["pooled"]

    # [2] Plan 01 pre-registered per market
    p01_table, p01_pooled = task2_plan01(markets, preloaded)
    out["plan01_per_market"] = p01_table.to_dict("records")
    if len(p01_pooled) >= 5:
        rep = trade_significance_report(p01_pooled["net"], k=5, n_trials=1)
        out["plan01_pooled"] = {"n": len(p01_pooled),
                                "hit": float((p01_pooled.net > 0).mean()),
                                "net": float(p01_pooled.net.sum()),
                                "nw_t": rep["t_nw"], "dsr": rep["dsr"],
                                "frac_positive_folds": rep["purged_cv"]["frac_positive"]}

    # [3] combined portfolio: Plan01 pooled + Plan04 frozen cell (15bps maker composite)
    p04_all = full["all_trades"]
    p04_frozen = (p04_all[p04_all.os_bps == 15.0] if len(p04_all) else pd.DataFrame())
    combo_parts = []
    if len(p01_pooled):
        combo_parts.append(p01_pooled[["entry_ts", "net"]].assign(strat="p01"))
    if len(p04_frozen):
        combo_parts.append(p04_frozen[["entry_ts", "net"]].assign(strat="p04"))
    if combo_parts:
        combo = pd.concat(combo_parts).sort_values("entry_ts")
        rep = trade_significance_report(combo["net"], k=5, n_trials=4)  # 3 ladder + p01
        out["combined_portfolio"] = {"n": len(combo),
                                     "p01_n": int((combo.strat == "p01").sum()),
                                     "p04_n": int((combo.strat == "p04").sum()),
                                     "net": float(combo.net.sum()),
                                     "hit": float((combo.net > 0).mean()),
                                     "nw_t": rep["t_nw"], "dsr": rep["dsr"],
                                     "frac_positive_folds": rep["purged_cv"]["frac_positive"],
                                     "significant": rep["significant"]}

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"true_anchor_{stamp}.json").write_text(
        json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
