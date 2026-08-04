"""ops/backfill.py — one-off / re-runnable history loads for the alt sources (PLAN §28).

Separate from `ops/refresh.py` on purpose. refresh is the daily lane: small, idempotent,
runs unattended, and every step is expected to finish in seconds. These are minutes-long
walks over an entire archive, and they are meant to be run deliberately and then
reasoned about, not scheduled.

    conda run -n someopark_run python -m prediction_market_macro.ops.backfill --all
    conda run -n someopark_run python -m prediction_market_macro.ops.backfill --fed --weather

Every source here writes a `knowledge_time` (PLAN §5-bis 口5: a backfilled feature with
no vintage is banned from pre-launch backtests), and none of them is wired into a model
by this script — wiring is §29 and goes through the §7-bis shadow → gate → pool ladder.
"""
from __future__ import annotations

import argparse
import time
import traceback

from prediction_market_macro.config.settings import load_settings
from prediction_market_macro.ingest.store import init_db


def run(fed: bool = False, news: bool = False, weather: bool = False,
        fed_refetch: bool = False, news_since: str = "2021-01-01",
        weather_since: str = "2005-01-01") -> dict:
    s = load_settings()
    conn = init_db(s.db_path)
    out: dict[str, object] = {}

    def step(name, fn):
        t0 = time.time()
        try:
            out[name] = {"result": fn(), "secs": round(time.time() - t0, 1)}
            print(f"  ✓ {name}: {out[name]}")
        except Exception as e:                                     # noqa: BLE001
            out[name] = {"error": str(e)[:300]}
            print(f"  ✗ {name}: {e}")
            print(traceback.format_exc())

    if fed:
        from prediction_market_macro.ingest import fed_text
        step("fed_statements", lambda: fed_text.backfill_statements(conn, refetch=fed_refetch))
    if news:
        # Kept runnable and OFF by default. Measured 2026-08-04 the Polygon news archive
        # is a retail equity wire (GlobeNewswire / Motley Fool / Zacks / Benzinga, 100%
        # ticker-tagged), macro headlines are 0.3-3.2% of volume, and the volume itself
        # swings 6 -> 4372 articles/week on Polygon's own coverage changes. See §28.2:
        # rejected as a macro feature source, not merely as too short.
        from prediction_market_macro.ingest import market_data
        step("news", lambda: market_data.backfill_news(conn, s.polygon_api_key,
                                                       since=news_since))
    if weather:
        from prediction_market_macro.ingest import weather as wx
        step("weather", lambda: wx.pull(conn, start=weather_since))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="fed + weather (news is opt-in, see §28.2)")
    ap.add_argument("--fed", action="store_true")
    ap.add_argument("--news", action="store_true")
    ap.add_argument("--weather", action="store_true")
    ap.add_argument("--fed-refetch", action="store_true",
                    help="re-fetch statements we already have (upgrades an 'eod' stamp"
                         " to the page-parsed release time)")
    ap.add_argument("--news-since", default="2021-01-01")
    ap.add_argument("--weather-since", default="2005-01-01")
    a = ap.parse_args()
    run(fed=a.fed or a.all, news=a.news, weather=a.weather or a.all,
        fed_refetch=a.fed_refetch, news_since=a.news_since,
        weather_since=a.weather_since)


if __name__ == "__main__":
    main()
