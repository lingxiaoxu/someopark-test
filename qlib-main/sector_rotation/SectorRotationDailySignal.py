"""
SectorRotationDailySignal.py — 板块轮动每日信号生成器
=======================================================
Production daily runner for the sector rotation strategy.

功能：
  1. 从 MacroStateStore (price_data/macro/) 加载宏观数据（无需重复下载）
  2. 从 yfinance（本地缓存）加载 ETF 历史价格
  3. 计算 composite signals → 目标权重（regime + momentum + value）
  4. 应用风险控制（vol scaling, VIX emergency de-risk, drawdown circuit breaker）
  5. 判断是否需要 rebalance（月首交易日 / VIX 紧急）
  6. 对比当前 inventory → 生成 ENTER/EXIT/INCREASE/DECREASE/HOLD 操作清单
  7. 计算交易费用（by liquidity tier）
  8. 输出每日报告（JSON + TXT）
  9. 更新 inventory（幂等：同日重跑不重复更新）

用法：
  conda run -n qlib_run --no-capture-output \\
    python qlib-main/sector_rotation/SectorRotationDailySignal.py \\
    --capital 1000000 [--date YYYY-MM-DD] [--dry-run] [--force-rebalance]
      [--value-source proxy]

目录（相对于 someopark-test/）：
  qlib-main/sector_rotation/SectorRotationDailySignal.py  ← 本文件
  qlib-main/sector_rotation/inventory_sector_rotation.json
  qlib-main/sector_rotation/trading_signals/              ← JSON + TXT 报告
  qlib-main/sector_rotation/inventory_history/            ← 历史快照
  qlib-main/sector_rotation/data/cache/                   ← ETF 价格缓存
  price_data/macro/                                       ← MacroStateStore 数据（不重下载）
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── Path setup ───────────────────────────────────────────────────────────────
_THIS_DIR    = Path(__file__).parent.resolve()   # sector_rotation/
_QLIB_DIR    = _THIS_DIR.parent.resolve()        # qlib-main/
_PROJECT_DIR = _QLIB_DIR.parent.resolve()        # someopark-test/

sys.path.insert(0, str(_QLIB_DIR))      # sector_rotation.* imports
sys.path.insert(0, str(_PROJECT_DIR))   # MacroStateStore

# ── Sector-rotation module imports ───────────────────────────────────────────
from sector_rotation.data.loader import load_config, load_prices
from sector_rotation.data.universe import get_tickers
from sector_rotation.signals.composite import compute_composite_signals
from sector_rotation.portfolio.optimizer import optimize_weights
from sector_rotation.portfolio.risk import apply_risk_controls
from sector_rotation.portfolio.rebalance import (
    compute_turnover,
    get_first_trading_day_of_month,
    should_emergency_rebalance,
)
from sector_rotation.backtest.costs import compute_transaction_costs

# ── Optional: MacroStateStore (reads price_data/macro/ parquets) ─────────────
try:
    from MacroStateStore import MacroStateStore as _MacroStateStore
    _MACRO_STORE_AVAILABLE = True
except Exception:
    _MACRO_STORE_AVAILABLE = False

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("SectorRotationDailySignal")

# ── Constants ─────────────────────────────────────────────────────────────────
INVENTORY_PATH       = _THIS_DIR / "inventory_sector_rotation.json"
SIGNALS_DIR          = _THIS_DIR / "trading_signals"
INVENTORY_HISTORY_DIR = _THIS_DIR / "inventory_history"
CACHE_DIR            = _PROJECT_DIR / "price_data" / "sector_etfs"

DEFAULT_CAPITAL  = 1_000_000
PRICE_START      = "2017-01-01"   # needs long history for signal warmup
CONFIG_PATH      = _THIS_DIR / "config.yaml"

# Weight-change threshold below which we don't rebalance a sector (3%)
REBALANCE_THRESHOLD = 0.03

# Actions
ACTION_ENTER    = "ENTER"
ACTION_EXIT     = "EXIT"
ACTION_INCREASE = "INCREASE"
ACTION_DECREASE = "DECREASE"
ACTION_HOLD     = "HOLD"
ACTION_FLAT     = "FLAT"           # no position, no signal
ACTION_EMERGENCY = "EMERGENCY_DERISK"


# ─────────────────────────────────────────────────────────────────────────────
# Inventory helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_inventory() -> dict:
    if INVENTORY_PATH.exists():
        with open(INVENTORY_PATH) as f:
            return json.load(f)
    return {
        "as_of": None,
        "last_updated": None,
        "capital": DEFAULT_CAPITAL,
        "holdings": {},
        "cash_weight": 0.0,
        "prev_weights": {},
        "prev_composite_scores": {},
        "rebalance_history": [],
    }


def save_inventory(inv: dict, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[DRY RUN] Inventory not saved.")
        return
    INVENTORY_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = INVENTORY_HISTORY_DIR / f"inventory_sector_rotation_{ts}.json"
    with open(bak, "w") as f:
        json.dump(inv, f, indent=2)
    with open(INVENTORY_PATH, "w") as f:
        json.dump(inv, f, indent=2)
    log.info(f"Inventory saved → {INVENTORY_PATH.name}  (backup: {bak.name})")


# ─────────────────────────────────────────────────────────────────────────────
# Macro data loading  (MacroStateStore → 不重复下载，直接读 price_data/macro/)
# ─────────────────────────────────────────────────────────────────────────────

def _load_macro_from_store(start: str, end: str) -> Optional[pd.DataFrame]:
    """
    从 MacroStateStore 加载宏观数据（读 price_data/macro/ parquets，无需 API 调用）。

    单位转换：
      hy_spread / ig_spread : MacroStateStore 存 % (e.g. 2.86)
                              → ×100 → bps (e.g. 286)，与 regime.py 阈值一致
      yield_curve           : % (e.g. 0.51)，regime.py threshold=0.5 也是 %，不转换
      fin_stress / nfci     : 已中心化指数，直接用原始值
    """
    if not _MACRO_STORE_AVAILABLE:
        return None
    try:
        store = _MacroStateStore()
        df = store.load(start, end)
        if df.empty:
            log.warning("MacroStateStore returned empty DataFrame.")
            return None

        col_map = {
            "vix":           "vix",
            "yield_curve":   "yield_curve",
            "hy_spread":     "hy_spread",       # % → bps below
            "ig_spread":     "ig_spread",       # % → bps below
            "breakeven_10y": "breakeven_10y",
            "fin_stress":    "fin_stress",      # raw STLFSI4
            "nfci":          "nfci",            # raw NFCI
            "effr":          "effr",
            "consumer_sent": "consumer_sent",
            "icsa":          "icsa",
        }
        present = {k: v for k, v in col_map.items() if k in df.columns}
        result = df[list(present.keys())].rename(columns=present).copy()

        # % → bps（只有 credit spreads 需要）
        for col in ("hy_spread", "ig_spread"):
            if col in result.columns:
                result[col] = result[col] * 100.0

        result = result.astype(float)
        log.info(
            f"MacroStateStore: loaded {len(result)} rows "
            f"({result.index[0].date()} → {result.index[-1].date()}), "
            f"cols={list(result.columns)}"
        )
        return result
    except Exception as e:
        log.warning(f"MacroStateStore load failed: {e}")
        return None


def _load_macro_fallback(start: str, end: str) -> pd.DataFrame:
    """Fallback：直接从 FRED API 拉取（需要 FRED_API_KEY）。"""
    from sector_rotation.data.loader import load_macro_data
    api_key = os.environ.get("FRED_API_KEY")
    return load_macro_data(
        start=start, end=end,
        api_key=api_key,
        cache_dir=CACHE_DIR,
        cache_max_age_hours=8.0,
    )


def load_macro(start: str, end: str) -> pd.DataFrame:
    """优先用 MacroStateStore；失败则 fallback 到 FRED API。"""
    macro = _load_macro_from_store(start, end)
    if macro is not None and len(macro) >= 252:
        return macro
    if macro is not None and len(macro) > 0:
        log.warning(
            f"MacroStateStore only has {len(macro)} rows — "
            "fewer than 252 (1yr warmup). Using FRED fallback for full history."
        )
    log.info("Falling back to FRED API for macro data.")
    return _load_macro_fallback(start, end)


# ─────────────────────────────────────────────────────────────────────────────
# Price loading (yfinance, cached)
# ─────────────────────────────────────────────────────────────────────────────

def load_etf_prices(tickers: List[str], benchmark: str, end: str) -> pd.DataFrame:
    """
    Load ETF + benchmark adjusted close prices from yfinance (cached).
    Returns DataFrame: DatetimeIndex, columns = tickers + benchmark.
    """
    all_tickers = tickers + ([benchmark] if benchmark not in tickers else [])
    prices = load_prices(
        tickers=all_tickers,
        start=PRICE_START,
        end=end,
        source="yfinance",
        cache_dir=CACHE_DIR,
        force_refresh=False,
        cache_max_age_hours=8.0,
    )
    return prices


# ─────────────────────────────────────────────────────────────────────────────
# Rebalance decision
# ─────────────────────────────────────────────────────────────────────────────

def _should_rebalance(
    signal_date: date,
    inv: dict,
    macro_recent: pd.DataFrame,
    cfg: dict,
    force: bool = False,
    emergency_active: bool = False,
) -> Tuple[bool, str]:
    """
    Returns (should_rebalance: bool, reason: str).
    Reasons: 'first_run' | 'monthly_rebalance' | 'emergency_vix' | 'no_rebalance' | 'forced'
    """
    if force:
        return True, "forced"
    if not inv.get("holdings"):
        return True, "first_run"

    # Emergency VIX check (with cooldown: only trigger on first crossing, not every day)
    vix_threshold = float(cfg.get("rebalance", {}).get("emergency_derisk_vix", 35.0))
    if should_emergency_rebalance(
        macro_recent, pd.Series(dtype=float),
        vix_threshold=vix_threshold,
        emergency_active=emergency_active,
    ):
        return True, "emergency_vix"

    # Monthly: first trading day of the month
    first_day = get_first_trading_day_of_month(signal_date.year, signal_date.month)
    if first_day is not None and signal_date == first_day.date():
        return True, "monthly_rebalance"

    return False, "no_rebalance"


# ─────────────────────────────────────────────────────────────────────────────
# Weight / share helpers
# ─────────────────────────────────────────────────────────────────────────────

def _weights_to_shares(
    weights: pd.Series,
    prices: pd.Series,
    capital: float,
) -> pd.Series:
    """Convert target weights → integer shares (floor)."""
    shares = {}
    for ticker, w in weights.items():
        price = float(prices.get(ticker, 0.0))
        shares[ticker] = int(math.floor(w * capital / price)) if price > 0 else 0
    return pd.Series(shares, dtype=int)


def _determine_actions(
    target_weights: pd.Series,
    current_weights: pd.Series,
    threshold: float = REBALANCE_THRESHOLD,
) -> Dict[str, str]:
    """
    Determine per-sector action.
    Actions: ENTER / EXIT / INCREASE / DECREASE / HOLD / FLAT
    """
    all_tickers = target_weights.index.union(current_weights.index)
    actions: Dict[str, str] = {}
    for t in all_tickers:
        cur = float(current_weights.get(t, 0.0))
        tgt = float(target_weights.get(t, 0.0))
        delta = tgt - cur
        if cur == 0.0 and tgt == 0.0:
            actions[t] = ACTION_FLAT
        elif cur == 0.0 and tgt > 0.0:
            actions[t] = ACTION_ENTER
        elif cur > 0.0 and tgt == 0.0:
            actions[t] = ACTION_EXIT
        elif delta > threshold:
            actions[t] = ACTION_INCREASE
        elif delta < -threshold:
            actions[t] = ACTION_DECREASE
        else:
            actions[t] = ACTION_HOLD
    return actions


def _build_trade_list(
    target_shares: pd.Series,
    current_shares: Dict[str, int],
    prices: pd.Series,
    actions: Dict[str, str],
    capital: float,
) -> List[dict]:
    """Build ordered list of trades with dollar amounts."""
    trades = []
    all_tickers = sorted(set(target_shares.index) | set(current_shares.keys()))
    for t in all_tickers:
        action = actions.get(t, ACTION_HOLD)
        if action in (ACTION_FLAT, ACTION_HOLD):
            continue
        tgt_sh = int(target_shares.get(t, 0))
        cur_sh = int(current_shares.get(t, 0))
        delta_sh = tgt_sh - cur_sh
        if delta_sh == 0:
            continue
        price = float(prices.get(t, 0.0))
        trades.append({
            "ticker":         t,
            "action":         action,
            "side":           "BUY" if delta_sh > 0 else "SELL",
            "delta_shares":   abs(delta_sh),
            "current_shares": cur_sh,
            "target_shares":  tgt_sh,
            "price":          round(price, 2),
            "est_value":      round(abs(delta_sh) * price, 2),
            "est_cost_bps":   None,  # filled in by caller
        })
    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Inventory update  (幂等)
# ─────────────────────────────────────────────────────────────────────────────

def _update_inventory(
    inv: dict,
    signal_date: date,
    target_weights: pd.Series,
    target_shares: pd.Series,
    prices_today: pd.Series,
    actions: Dict[str, str],
    cash_weight: float,
    regime_label: str,
    rebalance_reason: str,
    composite_scores: pd.Series,
    capital: float,
) -> dict:
    """
    Update inventory with today's positions.
    幂等：若 last_updated == signal_date，跳过更新。
    """
    today_str = signal_date.isoformat()

    if inv.get("last_updated") == today_str:
        log.info(f"Inventory already up to date for {today_str} — skipping.")
        return inv

    new_holdings: dict = {}
    for t, w in target_weights.items():
        if w <= 0:
            continue
        prev = inv.get("holdings", {}).get(t, {})
        action = actions.get(t, ACTION_HOLD)
        # days_held: reset on ENTER, increment on HOLD/INCREASE/DECREASE
        if action == ACTION_ENTER:
            days_held = 1
        else:
            days_held = prev.get("days_held", 0) + 1

        new_holdings[t] = {
            "weight":               round(float(w), 6),
            "shares":               int(target_shares.get(t, 0)),
            "last_price":           round(float(prices_today.get(t, 0.0)), 4),
            "cost_basis":           prev.get("cost_basis", round(float(prices_today.get(t, 0.0)), 4))
                                    if action != ACTION_ENTER
                                    else round(float(prices_today.get(t, 0.0)), 4),
            "entry_date":           prev.get("entry_date", today_str)
                                    if action != ACTION_ENTER else today_str,
            "last_rebalance_date":  today_str,
            "days_held":            days_held,
            "action_today":         action,
        }

    # Append rebalance history entry
    history: list = inv.get("rebalance_history", [])
    history.append({
        "date":             today_str,
        "reason":           rebalance_reason,
        "regime":           regime_label,
        "weights":          {t: round(float(w), 6) for t, w in target_weights.items()},
        "cash_weight":      round(float(cash_weight), 6),
        "composite_scores": {t: round(float(s), 4) for t, s in composite_scores.items()},
    })
    # Keep last 36 months
    history = history[-36:]

    inv["holdings"]               = new_holdings
    inv["cash_weight"]            = round(float(cash_weight), 6)
    inv["capital"]                = capital
    inv["prev_weights"]           = {t: round(float(w), 6) for t, w in target_weights.items()}
    inv["prev_composite_scores"]  = {t: round(float(s), 4) for t, s in composite_scores.items()}
    inv["as_of"]                  = today_str
    inv["last_updated"]           = today_str
    inv["last_daily_update"]      = today_str
    inv["rebalance_history"]      = history
    return inv


# ─────────────────────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────────────────────

def _write_report_json(report: dict, signal_date: date) -> Path:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SIGNALS_DIR / f"sr_daily_report_{signal_date.strftime('%Y%m%d')}_{ts}.json"

    # Clean non-serialisable floats
    def _clean(obj):
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
            return round(obj, 6)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_clean(v) for v in obj]
        return obj

    with open(path, "w") as f:
        json.dump(_clean(report), f, indent=2)
    log.info(f"Report (JSON) → {path.name}")
    return path


def _write_report_txt(report: dict, signal_date: date) -> Path:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SIGNALS_DIR / f"sr_daily_report_{signal_date.strftime('%Y%m%d')}_{ts}.txt"

    lines = []
    sep = "=" * 64

    lines.append(sep)
    lines.append(f"  SECTOR ROTATION DAILY SIGNAL  —  {signal_date}")
    lines.append(sep)

    # ── Regime ────────────────────────────────────────────────────
    regime = report.get("regime", {})
    vix = regime.get("vix")
    hy  = regime.get("hy_spread_bps")
    yc  = regime.get("yield_curve_pct")
    fs  = regime.get("fin_stress")
    nf  = regime.get("nfci")
    lines.append("")
    lines.append(f"  Regime : {regime.get('label', 'n/a').upper()}")
    if vix is not None:
        lines.append(f"  VIX    : {vix:.1f}  "
                     f"HY={hy:.0f}bps  Curve={yc:+.2f}%  "
                     f"FSI={fs:.3f}  NFCI={nf:.3f}" if all(x is not None for x in [hy, yc, fs, nf])
                     else f"  VIX    : {vix:.1f}")

    # ── Rebalance decision ─────────────────────────────────────────
    lines.append("")
    rebalance = report.get("rebalance_decision", {})
    will_rb = rebalance.get("rebalance", False)
    reason  = rebalance.get("reason", "")
    lines.append(f"  Rebalance : {'YES' if will_rb else 'NO'}  ({reason})")

    if not will_rb:
        lines.append("  → No trades today. Showing current holdings for reference.")

    # ── Target weights ─────────────────────────────────────────────
    lines.append("")
    lines.append(f"  {'SECTOR':<6} {'TARGET%':>8} {'PREV%':>8} {'DELTA%':>8} {'SIGNAL':>8}  ACTION")
    lines.append("  " + "-" * 60)
    for sig in sorted(report.get("signals", []), key=lambda x: -x.get("target_weight", 0)):
        t   = sig["ticker"]
        tgt = sig.get("target_weight", 0) * 100
        prv = sig.get("current_weight", 0) * 100
        dlt = tgt - prv
        sc  = sig.get("composite_score", 0)
        act = sig.get("action", "")
        lines.append(f"  {t:<6} {tgt:>7.1f}% {prv:>7.1f}% {dlt:>+7.1f}% {sc:>8.3f}  {act}")

    cash = report.get("cash_weight", 0) * 100
    if cash > 0.1:
        lines.append(f"  {'CASH':<6} {cash:>7.1f}%")

    # ── Trades ────────────────────────────────────────────────────
    trades = report.get("trades", [])
    if trades:
        lines.append("")
        capital = report.get("capital", 0)
        lines.append(f"  TRADES  (@${capital:,.0f})")
        lines.append("  " + "-" * 60)
        for tr in trades:
            side  = tr["side"]
            delta = tr["delta_shares"]
            price = tr["price"]
            val   = tr["est_value"]
            lines.append(
                f"  {tr['ticker']:<6} {side:<4} {delta:>5} sh @ ${price:>8.2f}  = ${val:>9,.0f}"
            )
        costs = report.get("transaction_costs", {})
        if costs:
            lines.append(f"  Est. transaction cost: ${costs.get('total_cost_usd', 0):,.0f} "
                         f"({costs.get('total_cost_bps', 0):.1f} bps)")

    # ── Signal components ─────────────────────────────────────────
    lines.append("")
    lines.append(f"  {'SECTOR':<6} {'CS_MOM':>8} {'TS_MULT':>8} {'COMPOSITE':>10}")
    lines.append("  " + "-" * 40)
    for sig in sorted(report.get("signals", []), key=lambda x: -x.get("composite_score", 0)):
        t    = sig["ticker"]
        cs   = sig.get("cs_mom", float("nan"))
        ts   = sig.get("ts_mult", float("nan"))
        comp = sig.get("composite_score", float("nan"))
        cs_s   = f"{cs:>8.3f}" if not math.isnan(cs) else "     n/a"
        ts_s   = f"{ts:>8.3f}" if not math.isnan(ts) else "     n/a"
        comp_s = f"{comp:>10.3f}" if not math.isnan(comp) else "       n/a"
        lines.append(f"  {t:<6} {cs_s} {ts_s} {comp_s}")

    lines.append("")
    lines.append(sep)

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    log.info(f"Report (TXT) → {path.name}")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run_daily_signal(
    signal_date: Optional[date] = None,
    capital: float = DEFAULT_CAPITAL,
    dry_run: bool = False,
    force_rebalance: bool = False,
    value_source: str = "proxy",
    config_path: Optional[Path] = None,
) -> dict:
    """
    Run the full daily signal pipeline.

    Parameters
    ----------
    signal_date   : Target date (default = latest weekday).
    capital       : Portfolio capital in USD.
    dry_run       : If True, compute everything but do NOT update inventory.
    force_rebalance: Force rebalance regardless of schedule.
    value_source  : "proxy" | "polygon" (recommended: full TTM P/E via Polygon API) | "constituents" (yfinance, limited history).
    config_path   : Path to config.yaml (default: sector_rotation/config.yaml).

    Returns
    -------
    report dict (also written to trading_signals/).
    """
    # ── 0. Resolve date ───────────────────────────────────────────
    if signal_date is None:
        today = date.today()
        # Roll back to last weekday if weekend
        while today.weekday() >= 5:
            today -= timedelta(days=1)
        signal_date = today
    log.info(f"Signal date: {signal_date}")

    # ── 1. Load config ────────────────────────────────────────────
    cfg = load_config(config_path or CONFIG_PATH)

    # ── 1b. Apply selected param set (written by SectorRotationBatchRun --select)
    _sel_path = CONFIG_PATH.parent / "selected_param_set.json"
    if _sel_path.exists():
        try:
            from sector_rotation.SectorRotationStrategyRuns import (
                PARAM_SETS as _PARAM_SETS,
                apply_param_set as _apply_param_set,
            )
            _sel = json.loads(_sel_path.read_text())
            _ps_name = _sel.get("param_set")
            if _ps_name and _ps_name in _PARAM_SETS:
                cfg = _apply_param_set(cfg, _PARAM_SETS[_ps_name])
                log.info(
                    f"[PARAM SELECT] Active: {_ps_name} | "
                    f"recent_sr12m={_sel.get('recent_sharpe_12m', '?')} | "
                    f"selected={_sel.get('selected_at', '?')}"
                )
            else:
                log.warning(
                    f"[PARAM SELECT] Unknown param set '{_ps_name}' in "
                    f"selected_param_set.json — using config.yaml defaults"
                )
        except Exception as _e:
            log.warning(f"[PARAM SELECT] Failed to apply selected_param_set.json: {_e}")

    etf_tickers = cfg["universe"]["etfs"]           # e.g. ["XLE", "XLB", ...]
    benchmark   = cfg["universe"]["benchmark"]      # "SPY"
    port_cfg    = cfg.get("portfolio", {})
    reb_cfg     = cfg.get("rebalance", {})
    risk_cfg    = cfg.get("risk", {})
    cost_cfg    = cfg.get("costs", {})
    sig_cfg     = cfg.get("signals", {})

    end_date_str = signal_date.strftime("%Y-%m-%d")

    # ── 2. Load prices ─────────────────────────────────────────────
    log.info("Loading ETF prices...")
    prices_all = load_etf_prices(etf_tickers, benchmark, end=end_date_str)
    etf_prices = prices_all[[t for t in etf_tickers if t in prices_all.columns]]
    bench_prices = prices_all[[benchmark]] if benchmark in prices_all.columns else None

    # Prices as of signal_date
    prices_today = prices_all.iloc[-1]  # last available row

    # ── 3. Load macro ──────────────────────────────────────────────
    log.info("Loading macro data...")
    macro = load_macro(start=PRICE_START, end=end_date_str)

    # Align macro to price index (forward-fill gaps up to 5 bdays)
    macro = macro.reindex(prices_all.index, method="ffill", limit=5)

    # Most recent macro row for regime/risk checks
    macro_recent = macro.dropna(how="all").tail(5)

    # ── 4. Compute composite signals ──────────────────────────────
    log.info("Computing composite signals...")
    regime_cfg = sig_cfg.get("regime", {})
    regime_method = regime_cfg.get("method", "rules")

    # Keys forwarded to compute_regime_rules() / compute_regime_hmm()
    _REGIME_DETECT_KEYS = {
        "vix_high_threshold", "vix_extreme_threshold", "hy_spread_high_bps",
        "yield_curve_inversion", "ism_expansion", "smoothing_days",
    }
    regime_kwargs = {k: v for k, v in regime_cfg.items() if k in _REGIME_DETECT_KEYS}

    # Regime-conditional weight multipliers (passed as regime_multipliers)
    from sector_rotation.signals.composite import DEFAULT_REGIME_WEIGHT_MULTIPLIERS
    raw_rw = regime_cfg.get("regime_weights")
    regime_multipliers = raw_rw if isinstance(raw_rw, dict) else DEFAULT_REGIME_WEIGHT_MULTIPLIERS

    # Defensive sector config
    defensive_tickers = regime_cfg.get("defensive_sectors") or None
    defensive_bonus = float(regime_cfg.get("defensive_bonus_risk_off", 0.30))

    polygon_api_key = os.environ.get("POLYGON_API_KEY") if value_source == "polygon" else None

    # Build signal_kwargs for new bonus signals
    stm_cfg = sig_cfg.get("short_term_momentum", {})
    erm_cfg = sig_cfg.get("earnings_revision", {})
    rsb_cfg = sig_cfg.get("relative_strength_breakout", {})
    _signal_kwargs = {
        "stm_enabled": stm_cfg.get("enabled", False),
        "stm_lookback": stm_cfg.get("lookback_months", 6),
        "stm_skip": stm_cfg.get("skip_months", 1),
        "stm_zscore_window": stm_cfg.get("zscore_window", 24),
        "erm_enabled": erm_cfg.get("enabled", False),
        "erm_lookback_quarters": erm_cfg.get("lookback_quarters", 4),
        "rsb_enabled": rsb_cfg.get("enabled", False),
        "rsb_lookback_days": rsb_cfg.get("lookback_days", 63),
    }

    # Inject bonus weights
    _sig_weights = sig_cfg.get("weights") or {}
    _sig_weights.setdefault("short_term_momentum_bonus",
                            stm_cfg.get("weight_bonus", 0.0))
    _sig_weights.setdefault("earnings_revision_bonus",
                            erm_cfg.get("weight_bonus", 0.0))
    _sig_weights.setdefault("rs_breakout_bonus",
                            rsb_cfg.get("weight_bonus", 0.0))

    # Benchmark for RS breakout
    _bench_series = prices_all[benchmark].squeeze() if benchmark in prices_all.columns else None

    composite, regime_monthly, components = compute_composite_signals(
        prices=etf_prices,
        macro=macro,
        weights=_sig_weights,
        regime_multipliers=regime_multipliers,
        defensive_tickers=defensive_tickers,
        defensive_bonus=defensive_bonus,
        regime_method=regime_method,
        value_source=value_source,
        value_cache_dir=CACHE_DIR,
        polygon_api_key=polygon_api_key,
        regime_kwargs=regime_kwargs,
        signal_kwargs=_signal_kwargs,
        benchmark_prices=_bench_series,
    )

    # Latest month-end composite scores
    latest_composite = composite.dropna(how="all")
    if latest_composite.empty:
        log.error("No valid composite signals — aborting.")
        return {}
    scores_today = latest_composite.iloc[-1]

    # Latest regime
    regime_label_monthly = regime_monthly.iloc[-1] if len(regime_monthly) > 0 else "risk_on"
    log.info(f"Latest composite scores:\n{scores_today.round(3).to_string()}")
    log.info(f"Regime: {regime_label_monthly}")

    # ── 5. Optimize weights ────────────────────────────────────────
    log.info("Optimizing weights...")
    daily_returns = etf_prices.pct_change().dropna()
    target_weights_raw = optimize_weights(
        scores=scores_today,
        returns=daily_returns,
        method=port_cfg.get("optimizer", "inv_vol"),
        cov_method=port_cfg.get("cov", {}).get("method", "ledoit_wolf"),
        cov_lookback_days=port_cfg.get("cov", {}).get("lookback_days", 252),
        top_n=port_cfg.get("top_n_sectors", 4),
        min_score=port_cfg.get("min_zscore", -0.5),
        max_weight=port_cfg.get("constraints", {}).get("max_weight", 0.40),
        min_weight=port_cfg.get("constraints", {}).get("min_weight", 0.0),
    )

    # ── 6. Apply risk controls ─────────────────────────────────────
    log.info("Applying risk controls...")
    # Approximate portfolio returns: equal-weight sector basket
    portfolio_returns = daily_returns.mean(axis=1)

    prog_cfg   = risk_cfg.get("vix_progressive_derisk", {})
    prog_tiers = prog_cfg.get("tiers", []) if prog_cfg.get("enabled", False) else []

    target_weights, cash_weight, risk_flags = apply_risk_controls(
        weights=target_weights_raw,
        portfolio_returns=portfolio_returns,
        macro=macro_recent,
        sector_returns=daily_returns,
        benchmark_returns=(
            bench_prices[benchmark].pct_change().dropna()
            if bench_prices is not None and benchmark in bench_prices.columns
            else None
        ),
        vol_target=risk_cfg.get("vol_scaling", {}).get("target_vol_annual", 0.12),
        vol_estimation_window=risk_cfg.get("vol_scaling", {}).get("estimation_window", 20),
        vol_historical_window=risk_cfg.get("vol_scaling", {}).get("historical_window", 252),
        vol_scale_threshold=risk_cfg.get("vol_scaling", {}).get("scale_threshold", 1.5),
        vol_scaling_enabled=risk_cfg.get("vol_scaling", {}).get("enabled", True),
        vix_emergency_threshold=reb_cfg.get("emergency_derisk_vix", 35.0),
        emergency_cash_pct=reb_cfg.get("emergency_cash_pct", 0.50),
        dd_halve_threshold=risk_cfg.get("drawdown", {}).get("cumulative_dd_halve", -0.15),
        dd_recovery_threshold=risk_cfg.get("drawdown", {}).get("cumulative_dd_recovery", -0.10),
        max_weight=port_cfg.get("constraints", {}).get("max_weight", 0.40),
        vix_progressive_tiers=prog_tiers,
    )

    log.info(f"Target weights (post-risk):\n{target_weights.round(3).to_string()}")
    log.info(f"Cash allocation: {cash_weight:.1%}  Risk flags: {risk_flags}")

    # ── 7. Rebalance decision ──────────────────────────────────────
    inv = load_inventory()
    inv["capital"] = capital

    # VIX emergency cooldown: read persisted state, clear if VIX has recovered
    vix_threshold    = float(cfg.get("rebalance", {}).get("emergency_derisk_vix", 35.0))
    vix_recovery     = vix_threshold * float(cfg.get("rebalance", {}).get("vix_recovery_factor", 0.80))
    emergency_active = bool(inv.get("emergency_mode_active", False))
    if emergency_active and not macro_recent.empty and "vix" in macro_recent.columns:
        current_vix = float(macro_recent["vix"].dropna().iloc[-1]) if not macro_recent["vix"].dropna().empty else vix_threshold
        if current_vix < vix_recovery:
            emergency_active = False
            log.info(f"VIX emergency cleared: VIX={current_vix:.1f} < recovery threshold {vix_recovery:.1f}")

    will_rebalance, rebalance_reason = _should_rebalance(
        signal_date, inv, macro_recent, cfg,
        force=force_rebalance,
        emergency_active=emergency_active,
    )
    # Persist emergency state: set True on trigger, False on recovery or monthly rebalance
    if rebalance_reason == "emergency_vix":
        emergency_active = True
    elif rebalance_reason == "monthly_rebalance":
        emergency_active = False   # Monthly rebalance resets emergency mode
    inv["emergency_mode_active"] = emergency_active

    log.info(f"Rebalance: {will_rebalance}  reason={rebalance_reason}")

    # Current holdings from inventory
    current_weights = pd.Series(
        {t: d.get("weight", 0.0) for t, d in inv.get("holdings", {}).items()},
        dtype=float,
    )
    current_shares: Dict[str, int] = {
        t: int(d.get("shares", 0)) for t, d in inv.get("holdings", {}).items()
    }

    # If no rebalance today: keep current weights, only update prices
    if not will_rebalance:
        effective_weights = current_weights if not current_weights.empty else target_weights
        effective_shares  = current_shares
        actions = {t: ACTION_HOLD for t in effective_weights.index}
    else:
        # Apply zscore threshold filter (only rebalance sectors with significant change)
        from sector_rotation.portfolio.rebalance import apply_zscore_threshold_filter
        prev_scores = pd.Series(inv.get("prev_composite_scores", {}), dtype=float)
        # First run or no prev scores: skip threshold filter (all positions are new)
        if prev_scores.empty:
            filtered_weights, rebalanced, held = target_weights, list(target_weights.index), []
        else:
            filtered_weights, rebalanced, held = apply_zscore_threshold_filter(
                new_scores=scores_today,
                prev_scores=prev_scores,
                new_weights=target_weights,
                prev_weights=current_weights,
                threshold=float(reb_cfg.get("zscore_change_threshold", 0.3)),
            )
        # Cap turnover
        max_turnover = float(reb_cfg.get("max_monthly_turnover", 0.80))
        from sector_rotation.portfolio.rebalance import cap_turnover
        filtered_weights = cap_turnover(filtered_weights, current_weights, max_turnover)

        effective_weights = filtered_weights
        effective_shares  = _weights_to_shares(effective_weights, prices_today, capital)
        actions = _determine_actions(effective_weights, current_weights)

    # ── 8. Build trade list ────────────────────────────────────────
    trades = _build_trade_list(
        target_shares=pd.Series(effective_shares if isinstance(effective_shares, dict) else effective_shares, dtype=int),
        current_shares=current_shares,
        prices=prices_today,
        actions=actions,
        capital=capital,
    )

    # Transaction costs
    prev_w_series = current_weights.reindex(effective_weights.index, fill_value=0.0)
    cost_info = compute_transaction_costs(
        prev_weights=prev_w_series,
        new_weights=effective_weights,
        portfolio_value=capital,
    )
    log.info(
        f"Transaction cost: ${cost_info['total_cost_usd']:,.0f} "
        f"({cost_info['total_cost_bps']:.1f} bps), "
        f"turnover={cost_info['turnover_pct']:.1f}%"
    )

    # ── 9. Assemble signal list ────────────────────────────────────
    signal_list = []
    for t in sorted(set(etf_tickers) | set(current_weights.index)):
        tgt_w = float(effective_weights.get(t, 0.0))
        cur_w = float(current_weights.get(t, 0.0))
        signal_list.append({
            "ticker":          t,
            "action":          actions.get(t, ACTION_FLAT),
            "target_weight":   round(tgt_w, 6),
            "current_weight":  round(cur_w, 6),
            "delta_weight":    round(tgt_w - cur_w, 6),
            "target_shares":   int(effective_shares.get(t, 0)) if isinstance(effective_shares, dict) else int(effective_shares.get(t, 0)),
            "current_shares":  current_shares.get(t, 0),
            "price":           round(float(prices_today.get(t, 0.0)), 2),
            "composite_score": round(float(scores_today.get(t, float("nan"))), 4),
            "cs_mom":          round(float(components["cs_mom"].iloc[-1].get(t, float("nan"))), 4)
                               if "cs_mom" in components and not components["cs_mom"].empty else float("nan"),
            "ts_mult":         round(float(components["ts_mult"].iloc[-1].get(t, float("nan"))), 4)
                               if "ts_mult" in components and not components["ts_mult"].empty else float("nan"),
        })

    # ── 10. Update inventory ───────────────────────────────────────
    if will_rebalance:
        eff_shares_series = pd.Series(
            effective_shares if isinstance(effective_shares, dict) else effective_shares,
            dtype=int,
        )
        inv = _update_inventory(
            inv=inv,
            signal_date=signal_date,
            target_weights=effective_weights,
            target_shares=eff_shares_series,
            prices_today=prices_today,
            actions=actions,
            cash_weight=cash_weight,
            regime_label=regime_label_monthly,
            rebalance_reason=rebalance_reason,
            composite_scores=scores_today,
            capital=capital,
        )
    else:
        # Non-rebalance day: update last_price + increment days_held (idempotent via last_daily_update)
        today_str = signal_date.isoformat()
        already_updated = inv.get("last_daily_update") == today_str
        for t, holding in inv.get("holdings", {}).items():
            p = float(prices_today.get(t, holding.get("last_price", 0.0)))
            if p > 0:
                holding["last_price"] = round(p, 4)
            if not already_updated:
                holding["days_held"] = holding.get("days_held", 0) + 1
        if not already_updated:
            inv["as_of"] = today_str
            inv["last_daily_update"] = today_str

    save_inventory(inv, dry_run=dry_run)

    # ── 11. Get macro snapshot for report ─────────────────────────
    macro_last = macro_recent.iloc[-1] if not macro_recent.empty else pd.Series(dtype=float)

    # ── 12. Assemble full report ───────────────────────────────────
    report = {
        "generated_at":  datetime.now().isoformat(),
        "signal_date":   signal_date.isoformat(),
        "capital":       capital,
        "dry_run":       dry_run,
        "regime": {
            "label":          regime_label_monthly,
            "vix":            float(macro_last.get("vix", float("nan"))) if not macro_last.empty else None,
            "hy_spread_bps":  float(macro_last.get("hy_spread", float("nan"))) if not macro_last.empty else None,
            "yield_curve_pct":float(macro_last.get("yield_curve", float("nan"))) if not macro_last.empty else None,
            "fin_stress":     float(macro_last.get("fin_stress", float("nan"))) if not macro_last.empty else None,
            "nfci":           float(macro_last.get("nfci", float("nan"))) if not macro_last.empty else None,
            "effr":           float(macro_last.get("effr", float("nan"))) if not macro_last.empty else None,
            "breakeven_10y":  float(macro_last.get("breakeven_10y", float("nan"))) if not macro_last.empty else None,
        },
        "rebalance_decision": {
            "rebalance": will_rebalance,
            "reason":    rebalance_reason,
        },
        "risk_flags":          str(risk_flags),
        "cash_weight":         round(float(cash_weight), 6),
        "signals":             signal_list,
        "trades":              trades,
        "transaction_costs":   cost_info,
        "holdings_summary": {
            "n_positions": sum(1 for s in signal_list if s["target_weight"] > 0),
            "invested_pct": round(sum(s["target_weight"] for s in signal_list) * 100, 2),
            "cash_pct":     round(cash_weight * 100, 2),
        },
    }

    # ── 13. Write reports ──────────────────────────────────────────
    _write_report_json(report, signal_date)
    _write_report_txt(report, signal_date)

    return report


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _latest_weekday() -> date:
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Sector Rotation Daily Signal Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard daily run (proxy P/E, fast)
  conda run -n qlib_run python sector_rotation/SectorRotationDailySignal.py --capital 1000000

  # With real constituent P/E (slow on first run, cached afterwards)
  conda run -n qlib_run python sector_rotation/SectorRotationDailySignal.py \\
    --capital 1000000 --value-source constituents

  # Dry run for today
  conda run -n qlib_run python sector_rotation/SectorRotationDailySignal.py --dry-run

  # Force rebalance on a specific date
  conda run -n qlib_run python sector_rotation/SectorRotationDailySignal.py \\
    --date 2026-04-01 --capital 1000000 --force-rebalance
""",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Signal date YYYY-MM-DD (default: latest weekday)",
    )
    parser.add_argument(
        "--capital", type=float, default=DEFAULT_CAPITAL,
        help=f"Portfolio capital USD (default: {DEFAULT_CAPITAL:,.0f})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute all signals but do NOT update inventory",
    )
    parser.add_argument(
        "--force-rebalance", action="store_true",
        help="Force rebalance even if today is not a scheduled date",
    )
    parser.add_argument(
        "--value-source", choices=["proxy", "constituents", "external", "polygon"],
        default="proxy",
        help=(
            "P/E data source for value signal. "
            "'proxy'=price-to-5yr-avg (fast, no extra downloads); "
            "'polygon'=real TTM P/E from Polygon quarterly EPS, full history (recommended); "
            "'constituents'=real TTM P/E from yfinance (only last 4-8 quarters, not recommended)"
        ),
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to config.yaml (default: sector_rotation/config.yaml)",
    )
    args = parser.parse_args()

    sig_date = date.fromisoformat(args.date) if args.date else _latest_weekday()

    report = run_daily_signal(
        signal_date=sig_date,
        capital=args.capital,
        dry_run=args.dry_run,
        force_rebalance=args.force_rebalance,
        value_source=args.value_source,
        config_path=Path(args.config) if args.config else None,
    )

    # Print summary to stdout
    if report:
        print()
        print("=" * 64)
        print(f"  SECTOR ROTATION  —  {sig_date}  (dry_run={args.dry_run})")
        print("=" * 64)
        print(f"  Regime : {report.get('regime', {}).get('label', 'n/a').upper()}")
        print(f"  Rebalance : {report['rebalance_decision']['rebalance']}  "
              f"({report['rebalance_decision']['reason']})")
        print()
        print(f"  {'SECTOR':<6} {'TARGET%':>8} {'DELTA%':>8}  ACTION")
        print("  " + "-" * 38)
        for s in sorted(report.get("signals", []), key=lambda x: -x.get("target_weight", 0)):
            if s["target_weight"] > 0 or s["current_weight"] > 0:
                print(f"  {s['ticker']:<6} {s['target_weight']*100:>7.1f}%"
                      f" {s['delta_weight']*100:>+7.1f}%  {s['action']}")
        cash = report.get("cash_weight", 0)
        if cash > 0.001:
            print(f"  {'CASH':<6} {cash*100:>7.1f}%")
        print()
        trades = report.get("trades", [])
        if trades:
            print(f"  TRADES ({len(trades)}):")
            for tr in trades:
                print(f"    {tr['ticker']:<6} {tr['side']:<4} {tr['delta_shares']:>5} sh "
                      f"@ ${tr['price']:>8.2f}  = ${tr['est_value']:>9,.0f}")
            c = report.get("transaction_costs", {})
            print(f"  Est. cost: ${c.get('total_cost_usd', 0):,.0f}  "
                  f"({c.get('total_cost_bps', 0):.1f} bps)")
        else:
            print("  No trades.")
        print("=" * 64)
