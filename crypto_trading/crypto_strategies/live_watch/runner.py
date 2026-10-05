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
               w5_knockdown, w6_residual, w7_noisefade)

logger = logging.getLogger(__name__)

STRATS = {"w1": w1_basis, "w2": w2_chronos, "w3": w3_mom24, "w4": w4_carry,
          "w5": w5_knockdown, "w6": w6_residual, "w7": w7_noisefade}
CADENCE_S = {"w1": 60, "w2": 300, "w3": 3600, "w4": 86400, "w5": 90, "w6": 10, "w7": 60}

# W8 is an experiment living in UNTRACKED files and running in its own process.
# It was wired in here as a hard module-scope import, which armed a landmine:
# this file is tracked, so a `git clean`, a fresh clone, or simply retiring the
# experiment would leave an import the probes cannot satisfy — and W1-W7 would
# all fail to start (verified 2026-09-11; the live runner predates W8, so the
# import had never actually run). The probes are the product; they must not be
# able to die because an experiment was deleted.
try:
    from . import w8_complete_set
except ImportError as _e:                                    # pragma: no cover
    logger.info("w8 not available (%s) — W1-W7 unaffected", _e)
else:
    STRATS["w8"] = w8_complete_set
    CADENCE_S["w8"] = 2
# W7 table-driven prod entries at the non-T-8 bins (2026-10-02). Isolated like
# W8: if the module cannot load, W1-W7 must still run.
try:
    from . import w7_table
except ImportError as _e:                                    # pragma: no cover
    logger.warning("w7_table not available (%s) - W7 keeps its T-8 behaviour", _e)
else:
    STRATS["w7t"] = w7_table
    CADENCE_S["w7t"] = 10
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


def _publish_trading_allowlist() -> None:
    """Write THIS process's effective per-user live-trading allowlist to
    ~/.kalshi/trading_active.json (2026-10-01) so the web panel reports what
    the running W7 actually mirrors - the allowlist is fixed at import, so
    editing .env without a restart must not show as enabled."""
    try:
        import os
        from crypto_trading.crypto_common.config import USER_KEY_DIR, trading_user_ids
        USER_KEY_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(USER_KEY_DIR, 0o700)
        path = USER_KEY_DIR / "trading_active.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"user_ids": sorted(trading_user_ids()),
                                   "pid": os.getpid(), "published_at": time.time()}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        logger.info("per-user live allowlist published: %d user(s)", len(trading_user_ids()))
    except Exception as e:                                        # noqa: BLE001
        logger.warning("allowlist publish failed: %s", str(e)[:120])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default="all",
                    choices=["all", "w1", "w2", "w3", "w4", "w5", "w6", "w7", "w7t", "w8"])
    ap.add_argument("--loop", type=int, default=0,
                    help="seconds between iterations (0 = run once)")
    ap.add_argument("--confirm-spot", action="store_true",
                    help="W4: confirm the external spot leg is filled")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # W8 has a dedicated 2s process: a slow W1-W7 cycle/topup must not stall it.
    names = [n for n in STRATS if n != "w8"] if args.strategy == "all" else [args.strategy]

    if not args.loop:
        out = run_once(names, confirm_spot=args.confirm_spot)
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        return 0

    last_run = {n: 0.0 for n in names}
    last_topup = 0.0
    _TOPUP_THREAD = None
    logger.info("live_watch loop started: %s every %ss (cadence-gated)",
                names, args.loop)
    if "w7" in names:
        _publish_trading_allowlist()
    while True:
        now = time.time()
        if now - last_topup >= TOPUP_S:
            # In a background thread (2026-10-02): the top-up's subprocesses took
            # 54 s on the last restart and can take minutes on a slow network,
            # and a blocked loop can miss a W7 entry bin (+-0.6 min each). The
            # top-up only refreshes parquet that W4/W1 read; nothing waits on it.
            if _TOPUP_THREAD is None or not _TOPUP_THREAD.is_alive():
                import threading
                _TOPUP_THREAD = threading.Thread(target=data_topup, name="data-topup", daemon=True)
                _TOPUP_THREAD.start()
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
