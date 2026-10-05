"""Paths and frozen definitions for the W7 scenario tables (verified 2026-10-01)."""
from pathlib import Path

VERSION = "w7_scenarios_v3"     # v3 (2026-10-04): BAND 0.78-0.98 -> 0.80-0.97 (the prod entry band)

REPO = Path(__file__).resolve().parents[3]
CT = REPO / "crypto_trading"
STRIPS = CT / "price_data" / "kalshi" / "event_strips" / "prod"
HL = CT / "price_data" / "hyperliquid"
OUT = CT / "trading_signals" / "w7_scenarios"
CACHE = OUT / "cache"
FUNDING_CACHE = OUT / "data" / "hl_funding.csv"
W7_STATE = CT / "trading_signals" / "live_watch" / "w7_noisefade_state.json"
SNAPSHOT = REPO / "someo-park-investment-management" / "public" / "data" / "crypto_prediction" / "snapshot.json"

COINS = ("BTC", "ETH", "SOL", "DOGE", "XRP")
WINDOW_DAYS = 14                 # rolling table window
HISTORY_DAYS = 8                 # extra strike history for the 7-day vol median
SETTLE_LAG_S = 20 * 60           # only windows closed at least this long ago

# entry-time bins: minutes remaining, left-closed; label = bin centre
EDGES = [3.0, 4.5, 6.0, 7.5, 9.0, 10.5, 12.0]
LABELS = ["T-3.75", "T-5.25", "T-6.75", "T-8.25", "T-9.75", "T-11.25"]
CENTRES = dict(zip(LABELS, [3.75, 5.25, 6.75, 8.25, 9.75, 11.25]))
LIVE_BIN = "T-8.25"              # the bin that contains the live W7 entry (T-8.0 +/- 0.6)

BAND = (0.80, 0.97)              # = EventExecutionRouter.PROD_BAND (user 2026-10-04); was the paper MAIN band 0.78-0.98
FEE_MULT = 0.07                  # Kalshi taker fee per contract = FEE_MULT*c*(1-c)
SIZE_COL = {"BTC": "fill60"}     # live day sizes: BTC 60, others 40
DEFAULT_SIZE_COL = "fill40"

TREND_TH = 25.0                  # bp, market 60-min move
VOL_TH = 1.2                     # market 2h vol / own trailing 7-day median
FLOW_TH = 0.5                    # aligned 1-min sampled taker imbalance (live gate rule)
FUND_TH = 0.25                   # bp per 8h deviation of the 8h-mean funding from baseline
FUND_BASE = 0.0000125            # Hyperliquid hourly baseline (0.00125%/h)

MIN_N = 30                       # cells with fewer trades are not shown
STAR_N = 100                     # cells with fewer trades are flagged

ORDER = ["大单反向·低波动", "大单反向·高波动", "大单同向·低波动", "大单同向·高波动",
         "funding热门方拥挤", "funding对手方拥挤",
         "上涨·低波动", "横盘·低波动", "下跌·低波动", "上涨·高波动", "横盘·高波动", "下跌·高波动"]
POOLED = "5币合计"
