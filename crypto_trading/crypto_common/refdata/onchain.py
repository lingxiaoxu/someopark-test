"""Low-frequency regime inputs (Plan 00 §2 `refdata/onchain.py`, Plan 08 §3.4).

BTC dominance via CoinGecko's free ``/global`` endpoint (keyless, snapshot
only — free tier has no history, so we record-forward daily). Appended,
date-deduped:

    price_data/regime/btc_dominance.csv    [date, btc_dominance_pct, ingested_at]

CLI (idempotent — run daily from the top-up cron):
    … -m crypto_trading.crypto_common.refdata.onchain
"""
from __future__ import annotations

import argparse
import logging

import pandas as pd
import requests

from crypto_trading.crypto_common.config import PRICE_DATA
from crypto_trading.crypto_common.timeutils import utc_day, utcnow

logger = logging.getLogger(__name__)

OUT = PRICE_DATA / "regime" / "btc_dominance.csv"
URL = "https://api.coingecko.com/api/v3/global"


def snapshot_dominance() -> pd.DataFrame:
    r = requests.get(URL, timeout=15, headers={"User-Agent": "someopark-crypto/0.1"})
    r.raise_for_status()
    pct = float(r.json()["data"]["market_cap_percentage"]["btc"])
    row = pd.DataFrame([{"date": utc_day(), "btc_dominance_pct": pct,
                         "ingested_at": int(utcnow().timestamp())}])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        df = pd.concat([pd.read_csv(OUT), row], ignore_index=True)
    else:
        df = row
    df = df.sort_values(["date", "ingested_at"]).drop_duplicates("date", keep="last")
    df.to_csv(OUT, index=False)
    logger.info("btc dominance %s: %.2f%% (%d rows total)", utc_day(), pct, len(df))
    return df


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    snapshot_dominance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
