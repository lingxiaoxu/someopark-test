"""Pre-registered watchlist for the three unproven candidates (Plan 16).

REGISTRATION DATE: 2026-08-10. Everything below is frozen as of that date;
this module only re-MEASURES on accumulating data — it never re-selects.
The whole point is that data recorded after the registration date is the one
kind of evidence selection bias cannot touch.

  W1  S1 basis-selective, the exact tier-study cell:
      entry_k=3.5, min_abs=10bps, offset=10 ticks, abort=20bps, flow-fading
      filter ON, OI filter OFF, BTC. Holding is signal-driven (<15min).
      LIVE when: post-registration trades ≥30 AND NW-t ≥ 2 at tier 4 fees.

  W2  S8 Chronos frozen config: bolt-base / spot-composite input / 4h /
      price mode / ctx512 / 40bps band, BTC+ETH.
      LIVE when: post-registration NW-t ≥ 2 AND the post-registration subsample
      mean is positive (the decay flag must clear on NEW data, not the pooled).

  W3  S9 24h momentum, uniform continuation direction, |z|≥1 on
      mom_24h_volscaled, all 13 markets pooled.
      LIVE when: pooled n ≥ 1,260 with NW-t ≥ 2 (≈7 months), or NW-t ≥ 3
      earlier (stronger evidence can arrive faster if the mean holds).

Run monthly:  python -m crypto_trading.crypto_strategies.research_watchlist
Fees default to tier 4 (the capital-plan anchor): CRYPTO_FEE_TIER=4 is set
here unless the caller already exported one.
"""
from __future__ import annotations

import argparse
import json
import logging
import os

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.trade_stats import newey_west_tstat

logger = logging.getLogger(__name__)

REGISTRATION = pd.Timestamp("2026-08-10", tz="UTC")


def _nw(net: pd.Series) -> dict:
    if len(net) < 5:
        return {"n": len(net), "note": "insufficient"}
    t = newey_west_tstat(net.reset_index(drop=True))
    return {"n": len(net), "mean": round(float(net.mean()), 3),
            "hit": round(float((net > 0).mean()), 3),
            "nw_t": round(float(t["t_nw"]), 2)}


def watch_s1() -> dict:
    from dataclasses import replace
    from crypto_trading.crypto_strategies.basis_meanrev.improved import (
        ImprovedParams, prepare, run_config)
    prep = prepare("KXBTCPERP", "BTC")
    p = ImprovedParams(entry_k=3.5, min_abs_bps=10.0, offset_ticks=10)
    r = run_config(prep, p, ticker="KXBTCPERP")
    tp = r["trade_pnl"]
    out = {"frozen": "k3.5/abs10/off10/abort20/flowY/oiN @T4",
           "all": _nw(tp["net"])}
    if "entry_ts" in tp.columns:
        post = tp[pd.to_datetime(tp["entry_ts"], utc=True) >= REGISTRATION]
        out["post_registration"] = _nw(post["net"])
        out["live_when"] = "post n>=30 and nw_t>=2"
    return out


def watch_s8() -> dict:
    os.environ.setdefault("CHRONOS_MODEL", "amazon/chronos-bolt-base")
    import crypto_trading.crypto_strategies.research_chronos as rc
    from crypto_trading.crypto_common.loader import (load_index_composite,
                                                     load_poll_market_stats)
    pipe = rc._pipeline()
    parts = []
    for tk, a in [("KXBTCPERP", "BTC"), ("KXETHPERP", "ETH")]:
        comp = (load_index_composite(a)["vw_close"]
                .resample("5min", label="right", closed="right").last().dropna())
        st = load_poll_market_stats(tk)
        mid = ((st.bid + st.ask) / 2).dropna().resample(
            "5min", label="right", closed="right").last().dropna()
        src = comp.reindex(mid.index).ffill(limit=3).dropna()
        fc = rc.rolling_forecast(src, ctx_len=512, horizon=48, mode="price", pipe=pipe)
        if fc.empty:
            continue
        tp = rc.fill_aware(tk, fc["pred_bps"], 48, 40.0)
        if tp is not None and len(tp):
            parts.append(tp.assign(ticker=tk))
    if not parts:
        return {"error": "no trades"}
    tp = pd.concat(parts).sort_values("ts")
    post = tp[tp.ts >= REGISTRATION]
    return {"frozen": "bolt-base/composite/4h/price/ctx512/band40 @T4",
            "all": _nw(tp["net_bps"]),
            "post_registration": _nw(post["net_bps"]),
            "live_when": "post nw_t>=2 and post mean>0 (decay flag must clear)"}


def watch_s9() -> dict:
    import crypto_trading.crypto_strategies.research_overnight as ov
    parts = []
    for tk in ov.MARKETS:
        try:
            px = ov.hourly_close(tk)
        except FileNotFoundError:
            continue
        sig = ov.build_signals(px, tk)["mom_24h_volscaled"]
        if sig.dropna().empty:
            continue
        z = ov._tz(sig)
        fwd = (px.shift(-24) / px - 1.0) * 1e4
        d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
        hold = pd.Timedelta(hours=24)
        rows, until = [], None
        for ts, r in d.iterrows():
            if abs(r.z) < 1.0 or (until is not None and ts < until):
                continue
            rows.append({"ts": ts, "net": float(np.sign(r.z) * r.fwd) - ov.FEE_MM_BPS})
            until = ts + hold
        if rows:
            parts.append(pd.DataFrame(rows))
    if not parts:
        return {"error": "no episodes"}
    ep = pd.concat(parts).sort_values("ts")
    post = ep[ep.ts >= REGISTRATION]
    return {"frozen": "uniform continuation, |z|>=1, mom_24h_volscaled, 13 mkts",
            "all": _nw(ep["net"]), "post_registration": _nw(post["net"]),
            "live_when": "pooled n>=1260 with nw_t>=2, or nw_t>=3 earlier"}


def watch_s3_spot_hedged() -> dict:
    """W4 — S3-improved same-asset carry (short KXBTCPERP + long spot BTC,
    'always30' rule, frozen 2026-08-10). Its edge is structural income, so the
    live test is confirmation, not discovery:
    LIVE when: post-registration ≥60 days AND post gross NW-t ≥ 2 AND net > 0
    at the 50bps spot-RT scenario AND trailing-30d funding still positive."""
    from crypto_trading.crypto_strategies.funding_carry import spot_hedged
    r = spot_hedged.run(rule="always30")
    out = {"frozen": "short KXBTCPERP + long spot BTC, always-on, 30d-sum stop",
           "in_sample": {k: r[k] for k in ("days", "funding_ann_pct",
                                           "resid_ann_pct", "resid_vol_ann_pct",
                                           "gross_daily_nw_t")},
           "net_at_T4_spot50": next((s["ann_net_pct"] for s in r["scenarios"]
                                     if s["tier"] == 4 and s["spot_rt_bps"] == 50.0),
                                    None),
           "live_when": "post >=60d and post nw_t>=2 and net>0 @spot50 "
                        "and trailing-30d funding > 0"}
    if "post_registration" in r:
        out["post_registration"] = r["post_registration"]
    return out


def watch_s10_knockdown() -> dict:
    """W5 — knockdown replication (frozen 2026-08-11). The backtest already
    passed persistence + independent settlement + L2-depth gates (+24.5c/contract,
    NW-t 18.4); what remains is LIVE capture. This watch re-runs the canonical
    backtest on all accumulated tape AND reads the live probe's capture rate.
    LIVE when: probe capture_rate >= 0.25 over >= 7 days AND paper mean pnl
    within 50% of backtest AND the backtest number itself has not decayed."""
    from crypto_trading.crypto_strategies.event_binary import research_knockdown as rk
    ob = rk.build_ob_index("KXBTC")
    tp = rk.stream_trades("KXBTC", rk.PRIMARY, ob, strict=True)
    out = {"frozen": "zone.15-.45 dip5c tte5-45 depth>=50, KXBTC, taker",
           "backtest_all_tape": rk.summarize(tp, "canonical")}
    try:
        from crypto_trading.crypto_strategies.live_watch import common as lw
        st = lw.load_state("w5_knockdown")
        pr = st.get("probe", {})
        n = pr.get("signals", 0)
        out["live_probe"] = {**pr, "capture_rate":
                             round(pr.get("capturable", 0) / n, 3) if n else None,
                             "paper_cum_usd": st.get("cum_net_usd", 0.0),
                             "paper_trades": len(st.get("trades", []))}
    except Exception as e:                                  # noqa: BLE001
        out["live_probe"] = {"error": str(e)[:60]}
    out["live_when"] = ("capture_rate>=0.25 over >=7d AND paper mean within "
                        "50% of backtest AND backtest not decayed")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ.setdefault("CRYPTO_FEE_TIER", "4")

    out = {"registration": str(REGISTRATION.date()),
           "fee_tier": os.environ["CRYPTO_FEE_TIER"]}
    for name, fn in [("W1_S1_basis", watch_s1), ("W2_S8_chronos", watch_s8),
                     ("W3_S9_mom24h", watch_s9),
                     ("W4_S3_spot_hedged", watch_s3_spot_hedged),
                     ("W5_knockdown", watch_s10_knockdown)]:
        try:
            out[name] = fn()
        except Exception as e:                              # noqa: BLE001
            out[name] = {"error": str(e)[:80]}
        logger.info("%s done", name)

    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"watchlist_{stamp}.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
