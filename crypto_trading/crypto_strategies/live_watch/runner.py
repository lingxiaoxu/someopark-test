"""live_watch runner — single entry point for the four watchlist strategies.

    python -m crypto_trading.crypto_strategies.live_watch.runner            # all, once
    …runner --strategy w4 --confirm-spot                                    # W4 with spot-leg confirmation
    …runner --loop 300                                                      # daemon: every 5 min
                                                                            #  (w1 every loop, w2 every loop,
                                                                            #   w3 hourly, w4 daily)

Every run is a dry-run unless the strategy is enabled in config.yaml AND the
global execution gates are open. All reports append to
trading_signals/live_watch/log_YYYY-MM-DD.jsonl.
"""
from __future__ import annotations

import argparse
import json
import logging
import time

import pandas as pd

from . import (common, w1_basis, w2_chronos, w3_mom24, w4_carry,
               w5_knockdown, w6_residual)

logger = logging.getLogger(__name__)

STRATS = {"w1": w1_basis, "w2": w2_chronos, "w3": w3_mom24, "w4": w4_carry,
          "w5": w5_knockdown, "w6": w6_residual}
CADENCE_S = {"w1": 60, "w2": 300, "w3": 3600, "w4": 86400, "w5": 90, "w6": 10}
TOPUP_S = 21600          # 6h: keep the data the modules depend on fresh


def data_topup() -> None:
    """Refresh funding + composite parquet in-process.

    W4 refuses to act on stale funding and W1's watchlist backtest reads the
    composite parquet — both go stale without ``pipeline.sh daily``, which only
    runs under launchd (not installed). The watch daemon therefore maintains
    its own inputs: measured 196h-stale funding and a composite that ended 8
    days back, both silently degrading the observation record.
    """
    import subprocess
    import sys
    for mod, args in ((".crypto_common.kalshi.backfill", []),
                      (".crypto_common.refdata.index",
                       ["backfill", "--assets", "BTC,ETH", "--days", "3"])):
        try:
            subprocess.run([sys.executable, "-m", "crypto_trading" + mod, *args],
                           capture_output=True, timeout=1800, check=False)
        except Exception as e:                              # noqa: BLE001
            logger.warning("topup %s failed: %s", mod, e)
    logger.info("data top-up done (funding + composite)")


def run_once(names: list[str], *, confirm_spot: bool = False) -> dict:
    cfg = common.load_cfg()
    out = {}
    for n in names:
        try:
            mod = STRATS[n]
            out[n] = (mod.run(cfg, confirm_spot=confirm_spot) if n == "w4"
                      else mod.run(cfg))
        except Exception as e:                              # noqa: BLE001
            logger.exception("[%s] run failed", n)
            out[n] = {"strategy": n, "status": "ERROR", "error": str(e)[:200]}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default="all",
                    choices=["all", "w1", "w2", "w3", "w4", "w5", "w6"])
    ap.add_argument("--loop", type=int, default=0,
                    help="seconds between iterations (0 = run once)")
    ap.add_argument("--confirm-spot", action="store_true",
                    help="W4: confirm the external spot leg is filled")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    names = list(STRATS) if args.strategy == "all" else [args.strategy]

    if not args.loop:
        out = run_once(names, confirm_spot=args.confirm_spot)
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        return 0

    last_run = {n: 0.0 for n in names}
    last_topup = 0.0
    logger.info("live_watch loop started: %s every %ss (cadence-gated)",
                names, args.loop)
    while True:
        now = time.time()
        if now - last_topup >= TOPUP_S:
            data_topup()
            last_topup = now
        due = [n for n in names if now - last_run[n] >= CADENCE_S[n]]
        if due:
            out = run_once(due)
            for n in due:
                last_run[n] = now
                s = out[n].get("status") or \
                    {k: v.get("status") for k, v in
                     (out[n].get("markets") or {}).items()}
                logger.info("[%s] %s", n, s)
        time.sleep(max(5, args.loop))


if __name__ == "__main__":
    raise SystemExit(main())
