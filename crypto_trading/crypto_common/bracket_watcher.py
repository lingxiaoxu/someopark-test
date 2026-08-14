"""Bracket watcher daemon — makes client-side TP/SL actually ENFORCED.

The gap this closes: strategies ARM brackets (persisted to a JSON state file),
but a bracket only protects a position while something watches the mark and
fires the close. This daemon is that something: it loads the persisted brackets,
polls the public perp mark (keyless /margin/markets), and on a trigger submits
the reduce_only IOC close through the demo-first-gated ExecutionRouter.

Honest limitation (unchanged): this is CLIENT-SIDE — it only protects while the
daemon runs. Run it supervised (launchd), and for a true always-on backstop also
set TP/SL in the Kalshi app. Persistence means a restart re-arms rather than
forgetting open stops.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_common.bracket_watcher \
        --strategy basis_meanrev [--interval 5] [--live] [--cycles 0]
Live closes require the full execution gate (prod + ALLOW_LIVE_ORDERS +
/margin/enabled + dedicated key); without it every close is a logged dry-run.
"""
from __future__ import annotations

import argparse
import logging
import time

from crypto_trading.crypto_common.bracket import BracketMonitor
from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.execution import ExecutionRouter
from crypto_trading.crypto_common.kalshi.rest_margin import KalshiMarginClient

logger = logging.getLogger(__name__)


def state_path(strategy: str):
    return SIGNALS_DIR / strategy / "brackets.json"


class BracketWatcher:
    def __init__(self, strategy: str, *, env: str = "prod", live: bool = False,
                 interval: float = 5.0):
        self.strategy = strategy
        self.interval = interval
        self.market = KalshiMarginClient(env=env, min_interval=0.15)
        self.monitor = BracketMonitor(ExecutionRouter(strategy, env=env), live=live,
                                      state_path=state_path(strategy))
        self.env = env

    def _mark_and_cross(self, ticker: str) -> tuple[float, float] | None:
        """(mark, marketable close price) from the public book. mark = mid;
        close price crosses the book so the reduce_only IOC actually fills."""
        try:
            m = self.market.market(ticker)
            bid, ask = float(m.get("bid") or 0), float(m.get("ask") or 0)
        except Exception as e:
            logger.debug("mark fetch failed for %s: %s", ticker, e)
            return None
        if bid <= 0 or ask <= 0:
            return None
        return (bid + ask) / 2, bid, ask   # type: ignore[return-value]

    def tick(self) -> list[dict]:
        fired = []
        for ticker, b in self.monitor.active().items():
            got = self._mark_and_cross(ticker)
            if got is None:
                continue
            mark, bid, ask = got
            # close a long by hitting the bid, a short by lifting the ask
            close_px = bid if b.side == "bid" else ask
            ev = self.monitor.on_mark(ticker, mark, close_px, subaccount=b_sub(b))
            if ev:
                logger.warning("BRACKET FIRED %s %s @mark %.4f → close %s (%s)",
                               ticker, ev["trigger"], mark, ev["order_status"],
                               "LIVE" if self.monitor.live else "dry-run")
                fired.append(ev)
        return fired

    def run(self, cycles: int = 0) -> None:
        n = 0
        logger.info("bracket watcher up: %s env=%s live=%s armed=%d", self.strategy,
                    self.env, self.monitor.live, len(self.monitor.active()))
        while True:
            self.tick()
            n += 1
            if cycles and n >= cycles:
                break
            time.sleep(self.interval)


def b_sub(bracket) -> int:
    return getattr(bracket, "subaccount", 0) or 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default="basis_meanrev")
    ap.add_argument("--env", default="prod", choices=["prod", "demo"])
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--live", action="store_true", help="arm live closes (still gated)")
    ap.add_argument("--cycles", type=int, default=0, help="0 = run until killed")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    BracketWatcher(args.strategy, env=args.env, live=args.live,
                   interval=args.interval).run(args.cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
