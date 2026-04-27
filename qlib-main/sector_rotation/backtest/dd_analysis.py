"""回撤分析脚本 — 找出 MaxDD 的来源、期间持仓、与 SPY 对比"""
import os, sys, warnings
warnings.filterwarnings("ignore")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SR   = os.path.join(REPO, "qlib-main", "sector_rotation")
sys.path.insert(0, os.path.join(REPO, "qlib-main"))
sys.path.insert(0, REPO)
os.chdir(SR)

import yaml, numpy as np, pandas as pd
with open("config.yaml") as f:
    cfg = yaml.safe_load(f)

from sector_rotation.backtest.engine import SectorRotationBacktest
print("Running backtest …")
bt = SectorRotationBacktest(cfg)
r  = bt.run()

ec  = r.equity_curve
ret = r.daily_returns

# ── 1. 回撤序列 ───────────────────────────────────────────────────────────────
roll_max = ec.cummax()
dd_series = (ec - roll_max) / roll_max
print("\n=== Max Drawdown ===")
print(f"Max DD : {dd_series.min():.2%}   trough date: {dd_series.idxmin().date()}")
print()

# ── 2. 全部 >5% 回撤区间 ──────────────────────────────────────────────────────
in_dd = False
episodes = []
peak_dt = peak_val = None
for dt, val in ec.items():
    if not in_dd:
        if peak_val is None or val >= peak_val:
            peak_dt, peak_val = dt, val
        elif (val - peak_val) / peak_val < -0.05:
            in_dd = True
    else:
        if val >= peak_val:
            trough_val = ec.loc[peak_dt:dt].min()
            trough_dt  = ec.loc[peak_dt:dt].idxmin()
            episodes.append({
                "peak_date":     peak_dt.date(),
                "trough_date":   trough_dt.date(),
                "recovery_date": dt.date(),
                "drawdown_pct":  round((trough_val - peak_val) / peak_val * 100, 2),
                "days_to_trough": (trough_dt - peak_dt).days,
                "recovery_days":  (dt - peak_dt).days,
            })
            in_dd = False
            peak_dt, peak_val = dt, val

if in_dd:
    trough_val = ec.loc[peak_dt:].min()
    trough_dt  = ec.loc[peak_dt:].idxmin()
    episodes.append({
        "peak_date":     peak_dt.date(),
        "trough_date":   trough_dt.date(),
        "recovery_date": None,
        "drawdown_pct":  round((trough_val - peak_val) / peak_val * 100, 2),
        "days_to_trough": (trough_dt - peak_dt).days,
        "recovery_days":  None,
    })

ep_df = pd.DataFrame(episodes).sort_values("drawdown_pct")
print("=== All drawdown episodes > 5% ===")
print(ep_df.to_string(index=False))
print()

# ── 3. 最大回撤期间月度持仓 ───────────────────────────────────────────────────
max_dd_trough = dd_series.idxmin()
peak_before   = ec.loc[:max_dd_trough][
    ec.loc[:max_dd_trough] == ec.loc[:max_dd_trough].cummax()
].index[-1]
print(f"=== MaxDD period: {peak_before.date()} → {max_dd_trough.date()} ===")

# weights at each rebalance during the drawdown
wh = r.weights_history
w_in_dd = wh.loc[
    (wh.index >= peak_before) & (wh.index <= max_dd_trough)
].dropna(how="all")
# keep only sectors that had >1% allocation at any point
active_cols = w_in_dd.columns[(w_in_dd > 0.01).any()]
print("Weights at each rebalance:")
print(w_in_dd[active_cols].round(3).to_string())
print()

# daily return contribution by sector
SECTORS = cfg["universe"]["etfs"]
from sector_rotation.data.loader import load_prices, load_returns
prices = load_prices(
    tickers   = SECTORS + [cfg["universe"]["benchmark"]],
    start     = cfg["data"].get("price_start", "2017-01-01"),
    source    = cfg["data"].get("price_source", "yfinance"),
    cache_dir = None,
)
dr = load_returns(prices)
dr_dd = dr.loc[peak_before:max_dd_trough]

# reconstruct approximate daily portfolio weights (hold weights between rebalances)
port_weights_daily = pd.DataFrame(index=dr_dd.index, columns=SECTORS, dtype=float)
current_w = pd.Series(0.0, index=SECTORS)
all_dates  = list(dr_dd.index)
for dt in all_dates:
    if dt in wh.index:
        current_w = wh.loc[dt].reindex(SECTORS, fill_value=0.0)
    port_weights_daily.loc[dt] = current_w.values

contrib = (port_weights_daily * dr_dd[SECTORS]).fillna(0.0)
print("=== Cumulative return contribution by sector during MaxDD ===")
cum_contrib = contrib.sum().sort_values()
print(cum_contrib.round(4).to_string())
print()
total_strat = (1 + ret.loc[peak_before:max_dd_trough]).prod() - 1
print(f"Strategy total: {total_strat:.2%}")
if r.benchmark_returns is not None:
    bench = r.benchmark_returns.reindex(ret.index)
    total_bench = (1 + bench.loc[peak_before:max_dd_trough]).prod() - 1
    print(f"SPY total:      {total_bench:.2%}")
print()

# ── 4. 每个重大回撤期间的 regime ──────────────────────────────────────────────
print("=== Regime during each drawdown episode ===")
rh = r.regime_history
for _, row in ep_df.iterrows():
    p, t = pd.Timestamp(row["peak_date"]), pd.Timestamp(row["trough_date"])
    regimes_in = rh.loc[p:t] if len(rh) else pd.Series()
    mode_regime = regimes_in.mode().iloc[0] if len(regimes_in) else "N/A"
    print(f"  {row['peak_date']} → {row['trough_date']}: DD={row['drawdown_pct']:.1f}%  regime={mode_regime}")

# ── 5. VIX during max DD ─────────────────────────────────────────────────────
from sector_rotation.data.loader import load_macro_data
from pathlib import Path
macro = load_macro_data(
    start="2017-01-01",
    api_key=os.environ.get("FRED_API_KEY"),
    cache_dir=Path("../../price_data/sector_etfs"),
)
print()
print("=== VIX levels during MaxDD ===")
vix_in_dd = macro.loc[peak_before:max_dd_trough, "vix"].dropna()
print(f"VIX: min={vix_in_dd.min():.1f}  max={vix_in_dd.max():.1f}  mean={vix_in_dd.mean():.1f}")
print()

# monthly VIX
monthly_vix = vix_in_dd.resample("ME").mean()
for dt, v in monthly_vix.items():
    regime_val = rh.loc[dt] if dt in rh.index else "N/A"
    # find weights at that time
    w_at = wh.loc[:dt].iloc[-1] if len(wh.loc[:dt]) else pd.Series()
    top2 = w_at.nlargest(3).round(2).to_dict() if len(w_at) else {}
    print(f"  {dt.date()}  VIX={v:.1f}  regime={regime_val}  top3={top2}")
