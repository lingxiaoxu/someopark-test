"""Paths and knobs for W13 = W11 with MAKER execution (resting orders at touch - 1c, re-posted on every book
refresh until 45 s before the close; fills judged by the book crossing our price; maker fee 0). Everything W13
writes lives under OUT. Models are READ from W11's model directory (never trained here) so both mirrors score
the same models; W13 never trains."""
from __future__ import annotations
from pathlib import Path
from crypto_trading.crypto_common.config import SIGNALS_DIR, PRICE_DATA

NAME = "w13_maker_mirror"
OUT = SIGNALS_DIR / "w13_maker_mirror"
MODELS = SIGNALS_DIR / "w11_prod_mirror" / "models"   # READ-ONLY: W11 trains, W13 scores the same stamps
TRAIN_ENABLED = False
DATA = OUT / "data"                # DATA/<stamp>/{seq_<coin>.npy, seq_<coin>_meta.parquet, FR.parquet}
STATE = OUT / "state.json"
SLOTS = OUT / "slots.json"         # W13's OWN top1/top2 + window-cap ledger (live_plan.SLOTS_FILE is pointed here in-process)
LOG_DIR = OUT / "logs"
REPORT = OUT / "report"
COINS = ("BTC", "ETH", "SOL", "DOGE", "XRP")
STEP = 5                           # seconds per sequence step
K = 180                            # steps per 15-minute window
BINS = {"T-9.75": 63, "T-8.25": 81, "T-6.75": 99, "T-5.25": 117}   # steps elapsed since open at each bin centre
CONTRACTS_PAPER = 25               # the walk size W7 prices its favourite with
TRAIN_DAYS = 14
RETRAIN_EVERY_S = 24 * 3600
LOOP_S = 2                         # fast loop: the scanner bins fire 1-2 s after production's own book read (see observer.tick)
T8_POLL_S = 10                     # how often the W7 live log tail is read for the T-8 trigger
KEEP_STAMPS = 3
CHASE = {"improve_c": 0.01,            # rest at touch - 1c
         "stop_before_close_s": 45,    # give up (cancel) 45 s before the close
         "tail_bytes": 400_000,        # strips orderbook file tail read per poll
         "poll_s": 15}                 # live book poll while orders rest (the recorder itself only writes every 90 s); never within 8 s of a scanner bin centre
CHRONOS_MODEL = "amazon/chronos-bolt-small"
VARIANTS = ("base",                                            # W7 prod rules, nothing else
            "f_lgbm", "f_lgbm_01", "f_lgbm_02", "f_lgbm_np",       # take only if LightGBM p(fav) - price >= 0 / 0.01 / 0.02; no-price LightGBM
            "f_gru", "f_gru_np", "f_chronos", "f_chronos_base",     # the sequence models as the same filter
            "f_fair", "f_blend1", "f_blend2",                       # fair value Φ(d/σ√τ); logistic blends market+fair, +distance/momentum
            "f_hour", "floor82", "f_zrule",                         # skip UTC 09-12; entry >= 0.82; the two index-distance rules
            "eq_size", "size_kelly_mkt", "size_edge", "cap_total",   # sizing: equal; by price band (>=.90 x1.5, .85-.90 x1, <.85 x0.5); by model edge; cross-coin window cap
            "night_full",                                           # night band (UTC 0-5) sized like the day band (40/30 instead of the night 20/20)
            "combo")                                                # LightGBM filter + skip 09-12 + equal size
CAP_TOTAL = {"day": 120, "night": 75}   # cap_total: contracts per window across the 5 coins (~75% of the 5-coin base: day 160, night 100 since 2026-10-04)
VAR_DESC = {"base": "base = W7 实盘规则", "f_lgbm": "只下 LGBM ≥ 市场价", "f_lgbm_01": "只下 LGBM − 市场价 ≥ +0.01", "f_lgbm_02": "只下 LGBM − 市场价 ≥ +0.02",
            "f_lgbm_np": "只下 LGBM(不用价格) ≥ 市场价", "f_gru": "只下 GRU ≥ 市场价", "f_gru_np": "只下 GRU(不用价格) ≥ 市场价", "f_chronos": "只下 Chronos-small ≥ 市场价",
            "f_chronos_base": "只下 Chronos-base ≥ 市场价", "f_fair": "只下 公允价 ≥ 市场价", "f_blend1": "只下 逻辑回归(市场+公允) ≥ 市场价", "f_blend2": "只下 逻辑回归(市场+公允+距离/动量) ≥ 市场价",
            "f_hour": "跳过 UTC 09–12", "floor82": "入场价 ≥ 0.82", "f_zrule": "跳过 价≥0.90且指数贴着行权价 / 价<0.84且指数远离", "eq_size": "等仓(每笔=基础仓位, 上限 2×)",
            "size_kelly_mkt": "按价配仓(≥.90 ×1.5 / .85–.90 ×1 / <.85 ×0.5)", "size_edge": "按 LGBM edge 配仓(×0.5–×1.5)", "cap_total": "跨币每窗总上限(日 120 / 夜 75 张)",
            "night_full": "夜盘(UTC 0–5)用日盘仓位 40/30", "combo": "LGBM 过滤 + 跳过 09–12 + 等仓"}
HOUR_SKIP = range(9, 13)           # UTC hours (window close) the f_hour variant skips
FLOOR82 = 0.82
STRIPS = PRICE_DATA / "kalshi" / "event_strips" / "prod"
HL = PRICE_DATA / "hyperliquid"
INDEX = PRICE_DATA / "index_proxy" / "live"
DERIBIT = PRICE_DATA / "offshore" / "deribit"
